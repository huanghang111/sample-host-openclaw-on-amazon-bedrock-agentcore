"""SharedCore — wires all core services together and implements the shared
message processing flow used by all channel adapters.

Thread-safe: multiple bot threads call process_message() concurrently.
"""

import logging
import os
import re
import time

import boto3
from botocore.config import Config

from ws_bridge.adapters.base import ChannelAdapter, InboundMessage
from ws_bridge.core.agentcore import AgentCoreService
from ws_bridge.core.content import extract_text_from_content_blocks
from ws_bridge.core.dedup import DedupService
from ws_bridge.core.identity import IdentityService
from ws_bridge.core.outbound import (
    convert_s3_urls_to_markers,
    deliver_file,
    deliver_screenshot,
    extract_screenshots,
    extract_send_files,
    format_size,
)
from ws_bridge.core.s3 import (
    ALLOWED_IMAGE_TYPES,
    FILE_EXT_MAP,
    S3Service,
)
from ws_bridge.core import secrets as secrets_mod

logger = logging.getLogger("ws-bridge.core")

AGENTCORE_READ_TIMEOUT = 580
MAX_FILE_BYTES = 20_000_000


class SharedCore:
    """Shared core services, initialized once, used by all adapters."""

    def __init__(self):
        aws_region = os.environ.get("AWS_REGION", "us-west-2")
        registration_open = os.environ.get("REGISTRATION_OPEN", "false").lower() == "true"

        # AWS clients
        self._secrets_client = boto3.client("secretsmanager", region_name=aws_region)
        dynamodb = boto3.resource("dynamodb", region_name=aws_region)
        identity_table = dynamodb.Table(os.environ["IDENTITY_TABLE_NAME"])
        s3_client = boto3.client("s3", region_name=aws_region)
        agentcore_client = boto3.client(
            "bedrock-agentcore",
            region_name=aws_region,
            config=Config(
                read_timeout=AGENTCORE_READ_TIMEOUT,
                connect_timeout=10,
                retries={"max_attempts": 0},
            ),
        )

        # Core services
        self.identity = IdentityService(identity_table, registration_open)
        self.agentcore = AgentCoreService(
            agentcore_client,
            os.environ["AGENTCORE_RUNTIME_ARN"],
            os.environ["AGENTCORE_QUALIFIER"],
        )
        self.s3 = S3Service(s3_client, os.environ.get("USER_FILES_BUCKET", ""))
        self.dedup = DedupService()
        self._bots_secret_id = os.environ.get("WS_BRIDGE_BOTS_SECRET_ID", "")

    def get_bot_configs(self):
        """Load bot configs from Secrets Manager."""
        return secrets_mod.get_bot_configs(self._secrets_client, self._bots_secret_id)

    # ------------------------------------------------------------------
    # Shared message processing flow
    # ------------------------------------------------------------------

    def process_message(self, adapter: ChannelAdapter, msg: InboundMessage):
        """Common message processing flow for all channels. Thread-safe."""
        try:
            self._process_message_inner(adapter, msg)
        except Exception:
            logger.error("Unhandled error processing message bot=%s", msg.bot_id,
                         exc_info=True)

    def _process_message_inner(self, adapter: ChannelAdapter, msg: InboundMessage):
        # 1. Dedup
        if self.dedup.is_duplicate(msg.bot_id, msg.message_id):
            return

        # 2. Handle bind/link commands
        if self._handle_bind_commands(adapter, msg):
            return

        # 3. Resolve identity (global allowlist)
        actor_id = f"{msg.channel}:{msg.sender_id}"

        if not msg.text and not msg.image_download_code and not msg.file_download_code and not msg.video_download_code:
            logger.info("Ignoring empty message from %s", actor_id)
            return

        if not msg.sender_id or len(msg.sender_id) > 128:
            logger.warning("Invalid sender_id, skipping")
            return

        user_id, is_new = self.identity.resolve_user(
            msg.channel, msg.sender_id, msg.sender_name)
        if user_id is None:
            self._send_reply(adapter, msg,
                f"Sorry, this bot is private and requires an invitation.\n\n"
                f"Your ID: {actor_id}\n\n"
                f"Send this ID to the bot admin to request access.")
            return

        # 4. Check bot-level allowlist
        if not self.identity.check_bot_allowlist(adapter.config.id, actor_id):
            self._send_reply(adapter, msg,
                f"You don't have access to bot '{adapter.config.id}'. "
                f"Contact the admin to request access.")
            return

        # 5. Handle media (download from platform, upload to S3)
        namespace = actor_id.replace(":", "_")
        agent_message = self._prepare_agent_message(adapter, msg, namespace)
        if agent_message is None:
            return  # Media processing failed, error already sent

        # 6. Get/create session + update bot preference
        session_id = self.identity.get_or_create_session(user_id)
        self.identity.update_bot_preference(user_id, msg.bot_id, msg.channel)

        image_count = 0 if isinstance(agent_message, str) else len(agent_message.get("images", []))
        logger.info("bot=%s user=%s actor=%s session=%s text_len=%d images=%d dm=%s",
                     msg.bot_id, user_id, actor_id, session_id,
                     len(msg.text), image_count, not msg.is_group)

        # 7. Invoke AgentCore
        result = self.agentcore.invoke(session_id, user_id, actor_id, agent_message,
                                        channel=msg.channel)
        response_text = result.get("response", "Sorry, I couldn't process your message.")
        response_text = extract_text_from_content_blocks(response_text)
        if not response_text or not response_text.strip():
            response_text = "Sorry, I received an empty response. Please try again."

        # 8. Deliver screenshots
        clean_text, screenshot_keys = extract_screenshots(response_text)
        if screenshot_keys:
            for key in screenshot_keys:
                deliver_screenshot(key, namespace, adapter, msg, self.s3)
            response_text = clean_text

        # 9. Convert S3 URLs to markers
        response_text = convert_s3_urls_to_markers(response_text, namespace)

        # 10. Deliver outbound files
        clean_text, file_paths = extract_send_files(response_text)
        if file_paths:
            for path in file_paths:
                deliver_file(path, namespace, adapter, msg, self.s3)
            response_text = clean_text

        logger.info("Response len=%d first200=%s", len(response_text), response_text[:200])

        # 11. Send text reply
        if response_text.strip():
            self._send_reply(adapter, msg, response_text)
        logger.info("Reply sent to %s (dm=%s)", msg.sender_id, not msg.is_group)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _send_reply(self, adapter: ChannelAdapter, msg: InboundMessage, text: str):
        if msg.is_group:
            adapter.send_text(msg.conversation_id, text,
                              is_group=True, conversation_id=msg.conversation_id)
        else:
            adapter.send_text(msg.sender_id, text)

    def _handle_bind_commands(self, adapter: ChannelAdapter, msg: InboundMessage) -> bool:
        """Handle bind/link commands. Returns True if handled."""
        text = msg.text.strip() if msg.text else ""
        if not text:
            return False

        # Check for "bind CODE" / "link CODE"
        parts = text.split()
        if len(parts) == 2 and parts[0].lower() in ("link", "bind"):
            code = parts[1].strip().upper()
            if len(code) == 8 and code.isalnum():
                bound_user, success = self.identity.redeem_bind_code(
                    code, msg.channel, msg.sender_id, msg.sender_name)
                reply = "Accounts linked successfully!" if success else "Invalid or expired link code."
                self._send_reply(adapter, msg, reply)
                return True

        # Check for "link accounts" / "link"
        if text.lower() in ("link accounts", "link account", "link"):
            actor_id = f"{msg.channel}:{msg.sender_id}"
            user_id, _ = self.identity.resolve_user(
                msg.channel, msg.sender_id, msg.sender_name)
            if user_id:
                bind_code = self.identity.create_bind_code(user_id)
                self._send_reply(adapter, msg,
                    f"Your link code is: {bind_code}\n\n"
                    f"Enter this code on another channel within 10 minutes by typing: link {bind_code}")
            else:
                self._send_reply(adapter, msg,
                    f"You need to be registered before linking accounts.\n\n"
                    f"Your ID: {actor_id}")
            return True

        return False

    def _prepare_agent_message(self, adapter: ChannelAdapter,
                                msg: InboundMessage, namespace: str):
        """Prepare the agent message, handling media downloads and S3 uploads.

        Returns the message (str or dict) or None if processing failed.
        """
        text = msg.text

        if msg.image_download_code:
            image_bytes, content_type = adapter.download_media(
                msg.image_download_code, 3_750_000, message_id=msg.message_id)
            if image_bytes:
                s3_key = self.s3.upload_image(image_bytes, namespace, content_type)
                if s3_key:
                    return {
                        "text": text or "What is this image?",
                        "images": [{"s3Key": s3_key, "contentType": content_type}],
                    }
                else:
                    self._send_reply(adapter, msg, "Sorry, I couldn't process that image.")
                    return None
            else:
                self._send_reply(adapter, msg, "Sorry, I couldn't download that image.")
                return None

        if msg.file_download_code:
            file_bytes, content_type = adapter.download_media(
                msg.file_download_code, MAX_FILE_BYTES, message_id=msg.message_id)
            if file_bytes:
                ext = FILE_EXT_MAP.get(content_type, "")
                if not ext and "." in msg.file_name:
                    ext = msg.file_name.rsplit(".", 1)[-1].lower()
                s3_key = self.s3.upload_file(file_bytes, namespace, content_type,
                                              prefix="file", ext=ext or "bin")
                if s3_key:
                    size_str = format_size(len(file_bytes))
                    relative_path = s3_key.split("/", 1)[1] if "/" in s3_key else s3_key
                    return (
                        (f"{text}\n\n" if text else "")
                        + f"[User uploaded a file: {msg.file_name} ({content_type}, {size_str})]\n"
                          f"[File saved to: {relative_path}]\n"
                          f"[Use the file management tools to read or process this file.]"
                    )
                else:
                    self._send_reply(adapter, msg, "Sorry, I couldn't save that file. Please try again.")
                    return None
            else:
                self._send_reply(adapter, msg, "Sorry, I couldn't download that file.")
                return None

        if msg.video_download_code:
            video_bytes, content_type = adapter.download_media(
                msg.video_download_code, MAX_FILE_BYTES, message_id=msg.message_id)
            if video_bytes:
                ext = content_type.split("/")[-1] if "/" in content_type else "mp4"
                if ext not in ("mp4", "mov", "webm", "avi"):
                    ext = "mp4"
                s3_key = self.s3.upload_file(video_bytes, namespace, content_type,
                                              prefix="vid", ext=ext)
                if s3_key:
                    size_str = format_size(len(video_bytes))
                    relative_path = s3_key.split("/", 1)[1] if "/" in s3_key else s3_key
                    return (
                        (f"{text}\n\n" if text else "")
                        + f"[User uploaded a video ({content_type}, {size_str})]\n"
                          f"[Video saved to: {relative_path}]\n"
                          f"[Use the file management tools to access this video.]"
                    )
                else:
                    self._send_reply(adapter, msg, "Sorry, I couldn't save that video. Please try again.")
                    return None
            else:
                self._send_reply(adapter, msg, "Sorry, I couldn't download that video.")
                return None

        return text
