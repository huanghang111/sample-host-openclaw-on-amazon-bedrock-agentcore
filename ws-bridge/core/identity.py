"""DynamoDB identity resolution — shared across all channel adapters.

Extracted from dingtalk-bridge/bridge.py (resolve_user, get_or_create_session,
bind/link, allowlist, bot-preference tracking).
"""

import logging
import time
import uuid

from botocore.exceptions import ClientError

logger = logging.getLogger("ws-bridge.identity")

BIND_CODE_TTL_SECONDS = 600


class IdentityService:
    """Thread-safe identity resolution backed by DynamoDB."""

    def __init__(self, identity_table, registration_open: bool = False):
        self.table = identity_table
        self.registration_open = registration_open

    # ------------------------------------------------------------------
    # Allowlist
    # ------------------------------------------------------------------

    def is_user_allowed(self, channel: str, channel_user_id: str) -> bool:
        if self.registration_open:
            return True
        channel_key = f"{channel}:{channel_user_id}"
        try:
            resp = self.table.get_item(Key={"PK": f"ALLOW#{channel_key}", "SK": "ALLOW"})
            return "Item" in resp
        except ClientError as e:
            logger.error("Allowlist check failed: %s", e)
        return False

    def check_bot_allowlist(self, bot_id: str, actor_id: str) -> bool:
        """Check if user is allowed to use this specific bot.

        Returns True if:
        - The bot has no allowlist entries (open to all globally-allowed users)
        - The user is explicitly listed in the bot's allowlist
        """
        # Check if user is explicitly allowed
        try:
            resp = self.table.get_item(
                Key={"PK": f"BOT_ALLOW#{bot_id}#{actor_id}", "SK": "BOT_ALLOW"})
            if "Item" in resp:
                return True
        except ClientError as e:
            logger.error("Bot allowlist check failed: %s", e)
            return True  # fail-open on error to avoid blocking all users

        # Check if bot is restricted (has any allowlist entries)
        try:
            resp = self.table.get_item(
                Key={"PK": f"BOT_META#{bot_id}", "SK": "BOT_META"})
            if "Item" in resp and resp["Item"].get("restricted"):
                return False  # Bot is restricted and user is not on the list
        except ClientError as e:
            logger.error("Bot meta check failed: %s", e)

        return True  # Not restricted = open to all

    # ------------------------------------------------------------------
    # User resolution
    # ------------------------------------------------------------------

    def resolve_user(self, channel: str, channel_user_id: str,
                     display_name: str = "") -> tuple[str | None, bool]:
        """Resolve a channel user to an internal user ID.

        Returns (user_id, is_new). Returns (None, False) if not allowed.
        """
        channel_key = f"{channel}:{channel_user_id}"
        pk = f"CHANNEL#{channel_key}"

        try:
            resp = self.table.get_item(Key={"PK": pk, "SK": "PROFILE"})
            if "Item" in resp:
                return resp["Item"]["userId"], False
        except ClientError as e:
            logger.error("DynamoDB get_item failed: %s", e)

        if not self.is_user_allowed(channel, channel_user_id):
            logger.warning("User %s not on allowlist", channel_key)
            return None, False

        user_id = f"user_{uuid.uuid4().hex[:16]}"
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        try:
            self.table.put_item(
                Item={"PK": f"USER#{user_id}", "SK": "PROFILE", "userId": user_id,
                      "createdAt": now_iso, "displayName": display_name or channel_user_id},
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                logger.error("Failed to create user profile: %s", e)

        try:
            self.table.put_item(
                Item={"PK": pk, "SK": "PROFILE", "userId": user_id, "channel": channel,
                      "channelUserId": channel_user_id, "displayName": display_name or channel_user_id,
                      "boundAt": now_iso},
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                resp = self.table.get_item(Key={"PK": pk, "SK": "PROFILE"})
                if "Item" in resp:
                    return resp["Item"]["userId"], False
            logger.error("Failed to create channel mapping: %s", e)

        try:
            self.table.put_item(
                Item={"PK": f"USER#{user_id}", "SK": f"CHANNEL#{channel_key}",
                      "channel": channel, "channelUserId": channel_user_id, "boundAt": now_iso})
        except ClientError:
            pass

        logger.info("New user created: %s for %s", user_id, channel_key)
        return user_id, True

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def get_or_create_session(self, user_id: str) -> str:
        pk = f"USER#{user_id}"
        try:
            resp = self.table.get_item(Key={"PK": pk, "SK": "SESSION"})
            if "Item" in resp:
                self.table.update_item(
                    Key={"PK": pk, "SK": "SESSION"},
                    UpdateExpression="SET lastActivity = :now",
                    ExpressionAttributeValues={":now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                )
                return resp["Item"]["sessionId"]
        except ClientError as e:
            logger.error("DynamoDB session lookup failed: %s", e)

        session_id = f"ses_{user_id}_{uuid.uuid4().hex[:12]}"
        if len(session_id) < 33:
            session_id += "_" + uuid.uuid4().hex[:33 - len(session_id)]
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            self.table.put_item(
                Item={"PK": pk, "SK": "SESSION", "sessionId": session_id,
                      "createdAt": now_iso, "lastActivity": now_iso})
        except ClientError as e:
            logger.error("Failed to create session: %s", e)
        logger.info("New session: %s for %s", session_id, user_id)
        return session_id

    def update_bot_preference(self, user_id: str, bot_id: str, channel: str):
        """Record which bot the user last interacted with (conditional write)."""
        try:
            self.table.update_item(
                Key={"PK": f"USER#{user_id}", "SK": "SESSION"},
                UpdateExpression="SET lastBotId = :bid, lastBotChannel = :ch",
                ConditionExpression="attribute_not_exists(lastBotId) OR lastBotId <> :bid",
                ExpressionAttributeValues={":bid": bot_id, ":ch": channel},
            )
        except ClientError as e:
            # ConditionalCheckFailedException is expected (no change) — silently ignore
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                logger.warning("Failed to update bot preference: %s", e)

    # ------------------------------------------------------------------
    # Cross-channel binding
    # ------------------------------------------------------------------

    def create_bind_code(self, user_id: str) -> str:
        code = uuid.uuid4().hex[:8].upper()
        ttl = int(time.time()) + BIND_CODE_TTL_SECONDS
        self.table.put_item(
            Item={"PK": f"BIND#{code}", "SK": "BIND", "userId": user_id,
                  "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ttl": ttl})
        return code

    def redeem_bind_code(self, code: str, channel: str, channel_user_id: str,
                         display_name: str = "") -> tuple[str | None, bool]:
        code = code.strip().upper()
        try:
            resp = self.table.get_item(Key={"PK": f"BIND#{code}", "SK": "BIND"})
            item = resp.get("Item")
            if not item or item.get("ttl", 0) < int(time.time()):
                return None, False
            user_id = item["userId"]
            channel_key = f"{channel}:{channel_user_id}"
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.table.put_item(
                Item={"PK": f"CHANNEL#{channel_key}", "SK": "PROFILE", "userId": user_id,
                      "channel": channel, "channelUserId": channel_user_id,
                      "displayName": display_name or channel_user_id, "boundAt": now_iso})
            self.table.put_item(
                Item={"PK": f"USER#{user_id}", "SK": f"CHANNEL#{channel_key}",
                      "channel": channel, "channelUserId": channel_user_id, "boundAt": now_iso})
            self.table.delete_item(Key={"PK": f"BIND#{code}", "SK": "BIND"})
            logger.info("Bind code %s redeemed: %s -> %s", code, channel_key, user_id)
            return user_id, True
        except ClientError as e:
            logger.error("Bind code redemption failed: %s", e)
            return None, False
