"""OpenClaw Admin API Lambda — single function, path-based routing."""
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Configuration ---
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
IDENTITY_TABLE_NAME = os.environ["IDENTITY_TABLE_NAME"]
S3_USER_FILES_BUCKET = os.environ["S3_USER_FILES_BUCKET"]
WEBHOOK_SECRET_ID = os.environ.get("WEBHOOK_SECRET_ID", "")
TELEGRAM_SECRET_ID = os.environ.get("TELEGRAM_SECRET_ID", "")
SLACK_SECRET_ID = os.environ.get("SLACK_SECRET_ID", "")
FEISHU_SECRET_ID = os.environ.get("FEISHU_SECRET_ID", "")
DINGTALK_SECRET_ID = os.environ.get("DINGTALK_SECRET_ID", "")
WS_BRIDGE_BOTS_SECRET_ID = os.environ.get("WS_BRIDGE_BOTS_SECRET_ID", "")
ROUTER_API_URL = os.environ.get("ROUTER_API_URL", "")
SKILL_EVAL_FUNCTION_NAME = os.environ.get("SKILL_EVAL_FUNCTION_NAME", "")
AGENTCORE_RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")

# --- AWS Clients ---
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
identity_table = dynamodb.Table(IDENTITY_TABLE_NAME)
s3_client = boto3.client(
    "s3", region_name=AWS_REGION,
    config=boto3.session.Config(signature_version="s3v4"),
)
secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)
scheduler_client = boto3.client("scheduler", region_name=AWS_REGION)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)
agentcore_client = boto3.client("bedrock-agentcore", region_name=AWS_REGION)

# --- Secret cache (15 min TTL) ---
_SECRET_CACHE_TTL = 900
_secret_cache = {}

CHANNEL_SECRET_IDS = {
    "telegram": TELEGRAM_SECRET_ID,
    "slack": SLACK_SECRET_ID,
    "feishu": FEISHU_SECRET_ID,
    "dingtalk": DINGTALK_SECRET_ID,
}

# Placeholder length used by CDK-generated secrets
_PLACEHOLDER_LEN = 32


def _get_secret(secret_id):
    """Fetch secret value with 15-min cache."""
    if not secret_id:
        return ""
    cached = _secret_cache.get(secret_id)
    if cached:
        val, ts = cached
        if time.time() - ts < _SECRET_CACHE_TTL:
            return val
    try:
        resp = secrets_client.get_secret_value(SecretId=secret_id)
        val = resp["SecretString"]
        _secret_cache[secret_id] = (val, time.time())
        return val
    except ClientError as e:
        logger.error("Failed to get secret %s: %s", secret_id, e)
        return ""


def _json_response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _get_admin_sub(event):
    """Extract admin Cognito sub claim from JWT authorizer context."""
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except (KeyError, TypeError):
        return "unknown"


def _audit_log(admin_sub, action, target, detail=""):
    """Emit structured audit log for mutating operations."""
    logger.info(
        "AUDIT admin=%s action=%s target=%s detail=%s",
        admin_sub, action, target, detail,
    )


# ---- Route Dispatch ----

ROUTES = {}


def route(method, path):
    """Decorator to register a route handler."""
    def decorator(fn):
        ROUTES[(method, path)] = fn
        return fn
    return decorator


# ---- Stats ----

def _handle_get_stats(event):
    """GET /api/stats — aggregate dashboard statistics."""
    items = []
    params = {"FilterExpression": "begins_with(PK, :u) OR begins_with(PK, :a)",
              "ExpressionAttributeValues": {":u": "USER#", ":a": "ALLOW#"}}
    while True:
        resp = identity_table.scan(**params)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        params["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    total_users = 0
    total_allow = 0
    channel_dist = {}

    for item in items:
        pk = item.get("PK", "")
        sk = item.get("SK", "")
        if pk.startswith("USER#") and sk == "PROFILE":
            total_users += 1
        elif pk.startswith("USER#") and sk.startswith("CHANNEL#"):
            ch_key = sk.replace("CHANNEL#", "")
            ch_type = ch_key.split(":")[0] if ":" in ch_key else ch_key
            channel_dist[ch_type] = channel_dist.get(ch_type, 0) + 1
        elif pk.startswith("ALLOW#"):
            total_allow += 1

    # Channel config status
    channels = {}
    for name, sid in CHANNEL_SECRET_IDS.items():
        val = _get_secret(sid)
        channels[name] = {"configured": bool(val) and len(val) != _PLACEHOLDER_LEN}

    return _json_response(200, {
        "totalUsers": total_users,
        "totalAllowlisted": total_allow,
        "channelDistribution": channel_dist,
        "channels": channels,
    })


# Register stats route
route("GET", "/api/stats")(_handle_get_stats)


# ---- Channel Management ----

SUPPORTED_CHANNELS = {"telegram", "slack", "feishu", "dingtalk"}


@route("GET", "/api/channels")
def _handle_get_channels(event):
    """GET /api/channels — list all channels with config status and webhook URLs."""
    channels = []
    for name in SUPPORTED_CHANNELS:
        sid = CHANNEL_SECRET_IDS.get(name, "")
        val = _get_secret(sid)
        configured = bool(val) and len(val) != _PLACEHOLDER_LEN
        channels.append({
            "name": name,
            "configured": configured,
            "webhookUrl": f"{ROUTER_API_URL}webhook/{name}" if ROUTER_API_URL else "",
        })
    return _json_response(200, {"channels": channels})


@route("PUT", "/api/channels/{channel}")
def _handle_put_channel(event):
    """PUT /api/channels/{channel} — update channel credentials."""
    channel = event["pathParameters"]["channel"]
    if channel not in SUPPORTED_CHANNELS:
        return _json_response(400, {"error": f"Unknown channel: {channel}"})

    sid = CHANNEL_SECRET_IDS.get(channel)
    if not sid:
        return _json_response(400, {"error": f"No secret configured for {channel}"})

    body = event["parsedBody"]

    # Build secret value based on channel type
    if channel == "telegram":
        secret_val = body.get("botToken", "")
    elif channel == "slack":
        secret_val = json.dumps({
            "botToken": body.get("botToken", ""),
            "signingSecret": body.get("signingSecret", ""),
        })
    elif channel == "feishu":
        secret_val = json.dumps({
            "appId": body.get("appId", ""),
            "appSecret": body.get("appSecret", ""),
            "verificationToken": body.get("verificationToken", ""),
            "encryptKey": body.get("encryptKey", ""),
        })
    elif channel == "dingtalk":
        secret_val = json.dumps({
            "clientId": body.get("clientId", ""),
            "clientSecret": body.get("clientSecret", ""),
        })
    else:
        secret_val = json.dumps(body)

    try:
        secrets_client.put_secret_value(SecretId=sid, SecretString=secret_val)
        # Invalidate cache
        _secret_cache.pop(sid, None)
    except ClientError as e:
        logger.error("Failed to update secret for %s: %s", channel, e)
        return _json_response(500, {"error": "Failed to update credentials"})

    admin_sub = _get_admin_sub(event)
    _audit_log(admin_sub, "UPDATE_CHANNEL", channel)
    return _json_response(200, {"message": f"{channel} credentials updated"})


@route("DELETE", "/api/channels/{channel}")
def _handle_delete_channel(event):
    """DELETE /api/channels/{channel} — reset credentials to placeholder."""
    channel = event["pathParameters"]["channel"]
    if channel not in SUPPORTED_CHANNELS:
        return _json_response(400, {"error": f"Unknown channel: {channel}"})

    sid = CHANNEL_SECRET_IDS.get(channel)
    if not sid:
        return _json_response(400, {"error": f"No secret configured for {channel}"})

    placeholder = "x" * _PLACEHOLDER_LEN
    try:
        secrets_client.put_secret_value(SecretId=sid, SecretString=placeholder)
        _secret_cache.pop(sid, None)
    except ClientError as e:
        logger.error("Failed to reset secret for %s: %s", channel, e)
        return _json_response(500, {"error": "Failed to reset credentials"})

    admin_sub = _get_admin_sub(event)
    _audit_log(admin_sub, "RESET_CHANNEL", channel)
    return _json_response(200, {"message": f"{channel} credentials reset"})


@route("POST", "/api/channels/telegram/webhook")
def _handle_register_telegram_webhook(event):
    """POST /api/channels/telegram/webhook — register Telegram webhook."""
    token = _get_secret(TELEGRAM_SECRET_ID)
    if not token or len(token) == _PLACEHOLDER_LEN:
        return _json_response(400, {"error": "Telegram bot token not configured"})

    webhook_secret = _get_secret(WEBHOOK_SECRET_ID)
    webhook_url = f"{ROUTER_API_URL}webhook/telegram"

    url = (
        f"https://api.telegram.org/bot{token}/setWebhook"
        f"?url={urllib.parse.quote(webhook_url, safe='')}"
        f"&secret_token={urllib.parse.quote(webhook_secret, safe='')}"
    )
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        logger.error("Telegram setWebhook failed: %s", e)
        return _json_response(502, {"error": f"Telegram API error: {e}"})

    admin_sub = _get_admin_sub(event)
    _audit_log(admin_sub, "REGISTER_WEBHOOK", "telegram")
    return _json_response(200, {"telegramResponse": result})


# ---- WS Bridge Multi-Bot Management ----

_BOT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$")


def _get_ws_bridge_bots():
    """Load bots array from WS Bridge secret."""
    raw = _get_secret(WS_BRIDGE_BOTS_SECRET_ID)
    if not raw or len(raw) == _PLACEHOLDER_LEN:
        return []
    try:
        data = json.loads(raw)
        return data.get("bots", [])
    except (json.JSONDecodeError, AttributeError):
        return []


def _save_ws_bridge_bots(bots):
    """Save bots array back to WS Bridge secret."""
    secrets_client.put_secret_value(
        SecretId=WS_BRIDGE_BOTS_SECRET_ID,
        SecretString=json.dumps({"bots": bots}),
    )
    _secret_cache.pop(WS_BRIDGE_BOTS_SECRET_ID, None)


@route("GET", "/api/ws-bridge/bots")
def _handle_get_bots(event):
    """GET /api/ws-bridge/bots — list all multi-bot configurations."""
    bots = _get_ws_bridge_bots()
    # Strip credentials from response
    safe_bots = []
    for b in bots:
        safe_bots.append({
            "id": b.get("id", ""),
            "channel": b.get("channel", ""),
            "enabled": b.get("enabled", True),
            "hasCredentials": bool(b.get("credentials")),
        })
    return _json_response(200, {"bots": safe_bots})


@route("POST", "/api/ws-bridge/bots")
def _handle_add_bot(event):
    """POST /api/ws-bridge/bots — add a new bot."""
    body = event["parsedBody"]
    bot_id = body.get("id", "").strip()
    channel = body.get("channel", "")
    credentials = body.get("credentials", {})

    if not bot_id or not _BOT_ID_RE.match(bot_id):
        return _json_response(400, {"error": "Invalid bot id (alphanumeric, hyphens, underscores, 1-48 chars)"})
    if channel not in ("dingtalk", "feishu"):
        return _json_response(400, {"error": "Channel must be 'dingtalk' or 'feishu'"})
    if not credentials:
        return _json_response(400, {"error": "Credentials required"})

    bots = _get_ws_bridge_bots()
    if any(b.get("id") == bot_id for b in bots):
        return _json_response(409, {"error": f"Bot '{bot_id}' already exists"})

    bots.append({
        "id": bot_id,
        "channel": channel,
        "enabled": body.get("enabled", True),
        "credentials": credentials,
    })

    try:
        _save_ws_bridge_bots(bots)
    except ClientError as e:
        logger.error("Failed to save ws-bridge bots: %s", e)
        return _json_response(500, {"error": "Failed to save bot configuration"})

    admin_sub = _get_admin_sub(event)
    _audit_log(admin_sub, "ADD_BOT", bot_id, {"channel": channel})
    return _json_response(201, {"message": f"Bot '{bot_id}' added"})


@route("PUT", "/api/ws-bridge/bots/{botId}")
def _handle_update_bot(event):
    """PUT /api/ws-bridge/bots/{botId} — update a bot's config."""
    bot_id = event["pathParameters"]["botId"]
    body = event["parsedBody"]
    bots = _get_ws_bridge_bots()

    idx = next((i for i, b in enumerate(bots) if b.get("id") == bot_id), None)
    if idx is None:
        return _json_response(404, {"error": f"Bot '{bot_id}' not found"})

    if "enabled" in body:
        bots[idx]["enabled"] = bool(body["enabled"])
    if "credentials" in body and body["credentials"]:
        bots[idx]["credentials"] = body["credentials"]

    try:
        _save_ws_bridge_bots(bots)
    except ClientError as e:
        logger.error("Failed to update ws-bridge bot %s: %s", bot_id, e)
        return _json_response(500, {"error": "Failed to update bot"})

    admin_sub = _get_admin_sub(event)
    _audit_log(admin_sub, "UPDATE_BOT", bot_id)
    return _json_response(200, {"message": f"Bot '{bot_id}' updated"})


@route("DELETE", "/api/ws-bridge/bots/{botId}")
def _handle_delete_bot(event):
    """DELETE /api/ws-bridge/bots/{botId} — remove a bot."""
    bot_id = event["pathParameters"]["botId"]
    bots = _get_ws_bridge_bots()

    new_bots = [b for b in bots if b.get("id") != bot_id]
    if len(new_bots) == len(bots):
        return _json_response(404, {"error": f"Bot '{bot_id}' not found"})

    try:
        _save_ws_bridge_bots(new_bots)
    except ClientError as e:
        logger.error("Failed to delete ws-bridge bot %s: %s", bot_id, e)
        return _json_response(500, {"error": "Failed to delete bot"})

    admin_sub = _get_admin_sub(event)
    _audit_log(admin_sub, "DELETE_BOT", bot_id)
    return _json_response(200, {"message": f"Bot '{bot_id}' deleted"})


# ---- User Management ----

@route("GET", "/api/users")
def _handle_get_users(event):
    """GET /api/users — list all users with bound channels."""
    qs = event.get("queryParams", {})
    limit = min(int(qs.get("limit", "50")), 200)
    next_token = qs.get("nextToken")

    params = {
        "FilterExpression": "begins_with(PK, :u)",
        "ExpressionAttributeValues": {":u": "USER#"},
        "Limit": limit * 5,  # Over-fetch since filter is post-scan
    }
    if next_token:
        params["ExclusiveStartKey"] = json.loads(
            urllib.parse.unquote(next_token)
        )

    items = []
    resp = identity_table.scan(**params)
    items.extend(resp.get("Items", []))
    result_next = resp.get("LastEvaluatedKey")

    # Group by userId
    users_map = {}
    for item in items:
        pk = item.get("PK", "")
        sk = item.get("SK", "")
        if not pk.startswith("USER#"):
            continue
        user_id = pk.replace("USER#", "")
        if user_id not in users_map:
            users_map[user_id] = {"userId": user_id, "channels": []}

        if sk == "PROFILE":
            users_map[user_id]["displayName"] = item.get("displayName", "")
            users_map[user_id]["createdAt"] = item.get("createdAt", "")
        elif sk.startswith("CHANNEL#"):
            users_map[user_id]["channels"].append({
                "channelKey": sk.replace("CHANNEL#", ""),
                "channel": item.get("channel", ""),
                "channelUserId": item.get("channelUserId", ""),
            })

    users = sorted(users_map.values(), key=lambda u: u.get("createdAt", ""), reverse=True)

    result = {"users": users[:limit]}
    if result_next:
        result["nextToken"] = urllib.parse.quote(json.dumps(result_next, default=str))
    return _json_response(200, result)


@route("GET", "/api/users/{userId}")
def _handle_get_user(event):
    """GET /api/users/{userId} — user detail."""
    user_id = event["pathParameters"]["userId"]

    resp = identity_table.query(
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": f"USER#{user_id}"},
    )
    items = resp.get("Items", [])
    if not items:
        return _json_response(404, {"error": "User not found"})

    profile = {}
    channels = []
    session = None
    cron_jobs = []

    for item in items:
        sk = item.get("SK", "")
        if sk == "PROFILE":
            profile = {
                "userId": item.get("userId", ""),
                "displayName": item.get("displayName", ""),
                "createdAt": item.get("createdAt", ""),
            }
        elif sk.startswith("CHANNEL#"):
            channels.append({
                "channelKey": sk.replace("CHANNEL#", ""),
                "channel": item.get("channel", ""),
                "channelUserId": item.get("channelUserId", ""),
                "boundAt": item.get("boundAt", ""),
            })
        elif sk == "SESSION":
            session = {
                "sessionId": item.get("sessionId", ""),
                "createdAt": item.get("createdAt", ""),
                "lastActivity": item.get("lastActivity", ""),
            }
        elif sk.startswith("CRON#"):
            cron_jobs.append({
                "name": sk.replace("CRON#", ""),
                "expression": item.get("expression", ""),
                "message": item.get("message", ""),
                "timezone": item.get("timezone", ""),
                "channel": item.get("channel", ""),
            })

    return _json_response(200, {
        **profile,
        "channels": channels,
        "session": session,
        "cronJobs": cron_jobs,
    })


@route("DELETE", "/api/users/{userId}")
def _handle_delete_user(event):
    """DELETE /api/users/{userId} — delete user and cascade."""
    user_id = event["pathParameters"]["userId"]
    admin_sub = _get_admin_sub(event)

    resp = identity_table.query(
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": f"USER#{user_id}"},
    )
    items = resp.get("Items", [])
    if not items:
        return _json_response(404, {"error": "User not found"})

    channel_keys = []
    for item in items:
        sk = item.get("SK", "")

        # Delete CHANNEL# reverse mapping
        if sk.startswith("CHANNEL#"):
            ch_key = sk.replace("CHANNEL#", "")
            channel_keys.append(ch_key)
            try:
                identity_table.delete_item(Key={"PK": f"CHANNEL#{ch_key}", "SK": "PROFILE"})
            except ClientError as e:
                logger.error("Failed to delete CHANNEL# record: %s", e)

        # Delete EventBridge schedules for CRON# records
        if sk.startswith("CRON#"):
            schedule_name = sk.replace("CRON#", "")
            try:
                scheduler_client.delete_schedule(
                    Name=schedule_name, GroupName="openclaw-cron",
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "ResourceNotFoundException":
                    logger.error("Failed to delete schedule %s: %s", schedule_name, e)

        # Delete the USER# record itself
        try:
            identity_table.delete_item(Key={"PK": f"USER#{user_id}", "SK": sk})
        except ClientError as e:
            logger.error("Failed to delete USER# record %s: %s", sk, e)

    # Delete ALLOW# records for all channel keys
    for ch_key in channel_keys:
        try:
            identity_table.delete_item(Key={"PK": f"ALLOW#{ch_key}", "SK": "ALLOW"})
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                logger.error("Failed to delete ALLOW# for %s: %s", ch_key, e)

    _audit_log(admin_sub, "DELETE_USER", user_id,
               f"channels={channel_keys}")
    return _json_response(200, {"message": f"User {user_id} deleted"})


@route("DELETE", "/api/users/{userId}/channels/{channelKey}")
def _handle_delete_user_channel(event):
    """DELETE /api/users/{userId}/channels/{channelKey} — unbind a channel."""
    user_id = event["pathParameters"]["userId"]
    channel_key = event["pathParameters"]["channelKey"]
    admin_sub = _get_admin_sub(event)

    # Delete USER# CHANNEL# back-reference
    try:
        identity_table.delete_item(
            Key={"PK": f"USER#{user_id}", "SK": f"CHANNEL#{channel_key}"}
        )
    except ClientError as e:
        logger.error("Failed to delete user channel: %s", e)

    # Delete CHANNEL# PROFILE mapping
    try:
        identity_table.delete_item(
            Key={"PK": f"CHANNEL#{channel_key}", "SK": "PROFILE"}
        )
    except ClientError as e:
        logger.error("Failed to delete channel profile: %s", e)

    _audit_log(admin_sub, "UNBIND_CHANNEL", f"{user_id}/{channel_key}")
    return _json_response(200, {"message": f"Channel {channel_key} unbound from {user_id}"})


# ---- Allowlist ----

@route("GET", "/api/allowlist")
def _handle_get_allowlist(event):
    """GET /api/allowlist — list all allowlist entries."""
    qs = event.get("queryParams", {})
    limit = min(int(qs.get("limit", "50")), 200)
    next_token = qs.get("nextToken")

    params = {
        "FilterExpression": "begins_with(PK, :a)",
        "ExpressionAttributeValues": {":a": "ALLOW#"},
        "Limit": limit * 2,
    }
    if next_token:
        params["ExclusiveStartKey"] = json.loads(
            urllib.parse.unquote(next_token)
        )

    resp = identity_table.scan(**params)
    entries = []
    for item in resp.get("Items", []):
        entries.append({
            "channelKey": item.get("channelKey", item.get("PK", "").replace("ALLOW#", "")),
            "addedAt": item.get("addedAt", ""),
        })

    result = {"entries": entries[:limit]}
    if resp.get("LastEvaluatedKey"):
        result["nextToken"] = urllib.parse.quote(
            json.dumps(resp["LastEvaluatedKey"], default=str)
        )
    return _json_response(200, result)


@route("POST", "/api/allowlist")
def _handle_post_allowlist(event):
    """POST /api/allowlist — add allowlist entry."""
    body = event["parsedBody"]
    channel_key = body.get("channelKey", "").strip()
    if not channel_key or ":" not in channel_key:
        return _json_response(400, {"error": "channelKey must be in format 'channel:id'"})

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        identity_table.put_item(Item={
            "PK": f"ALLOW#{channel_key}",
            "SK": "ALLOW",
            "channelKey": channel_key,
            "addedAt": now_iso,
        })
    except ClientError as e:
        logger.error("Failed to add allowlist entry: %s", e)
        return _json_response(500, {"error": "Failed to add allowlist entry"})

    admin_sub = _get_admin_sub(event)
    _audit_log(admin_sub, "ADD_ALLOWLIST", channel_key)
    return _json_response(200, {"message": f"Added {channel_key} to allowlist"})


@route("DELETE", "/api/allowlist/{channelKey}")
def _handle_delete_allowlist(event):
    """DELETE /api/allowlist/{channelKey} — remove allowlist entry."""
    channel_key = event["pathParameters"]["channelKey"]
    admin_sub = _get_admin_sub(event)

    try:
        identity_table.delete_item(Key={"PK": f"ALLOW#{channel_key}", "SK": "ALLOW"})
    except ClientError as e:
        logger.error("Failed to delete allowlist entry: %s", e)
        return _json_response(500, {"error": "Failed to delete allowlist entry"})

    _audit_log(admin_sub, "REMOVE_ALLOWLIST", channel_key)
    return _json_response(200, {"message": f"Removed {channel_key} from allowlist"})


# ---- File Management ----

_VALID_NAMESPACE = re.compile(r"^[a-zA-Z0-9_-]+$")
_TEXT_EXTENSIONS = {".md", ".json", ".txt", ".js", ".ts", ".py", ".yaml", ".yml",
                    ".toml", ".cfg", ".ini", ".sh", ".html", ".css", ".xml", ".csv"}


def _validate_namespace(ns):
    return bool(_VALID_NAMESPACE.match(ns))


def _validate_path(path):
    return ".." not in path.split("/")


@route("GET", "/api/files")
def _handle_list_namespaces(event):
    """GET /api/files — list all user namespaces, enriched with user info."""
    qs = event.get("queryParams", {})
    continuation = qs.get("nextToken")

    params = {"Bucket": S3_USER_FILES_BUCKET, "Delimiter": "/"}
    if continuation:
        params["ContinuationToken"] = continuation

    resp = s3_client.list_objects_v2(**params)
    namespaces = [
        p["Prefix"].rstrip("/") for p in resp.get("CommonPrefixes", [])
    ]

    # Build namespace -> user mapping from DynamoDB CHANNEL# records
    ns_user_map = {}
    try:
        scan_resp = identity_table.scan(
            FilterExpression="begins_with(PK, :ch) AND SK = :sk",
            ExpressionAttributeValues={":ch": "CHANNEL#", ":sk": "PROFILE"},
        )
        for item in scan_resp.get("Items", []):
            channel_key = item.get("PK", "").replace("CHANNEL#", "")
            ns = channel_key.replace(":", "_")
            ns_user_map[ns] = {
                "userId": item.get("userId", ""),
                "displayName": item.get("displayName", ""),
                "channelKey": channel_key,
            }
    except ClientError:
        logger.exception("Failed to scan CHANNEL# records for namespace mapping")

    entries = []
    for ns in namespaces:
        entry = {"namespace": ns}
        if ns in ns_user_map:
            entry.update(ns_user_map[ns])
        entries.append(entry)

    result = {"namespaces": entries}
    if resp.get("NextContinuationToken"):
        result["nextToken"] = resp["NextContinuationToken"]
    return _json_response(200, result)


@route("GET", "/api/files/{namespace}")
def _handle_list_files(event):
    """GET /api/files/{namespace} — list files/folders in a namespace prefix."""
    namespace = event["pathParameters"]["namespace"]
    if not _validate_namespace(namespace):
        return _json_response(400, {"error": "Invalid namespace"})

    qs = event.get("queryParams", {})
    prefix = qs.get("prefix", "")  # Optional sub-path (e.g., ".openclaw/skills/")
    continuation = qs.get("nextToken")
    limit = min(int(qs.get("limit", "200")), 1000)

    s3_prefix = f"{namespace}/{prefix}" if prefix else f"{namespace}/"
    params = {
        "Bucket": S3_USER_FILES_BUCKET,
        "Prefix": s3_prefix,
        "Delimiter": "/",
        "MaxKeys": limit,
    }
    if continuation:
        params["ContinuationToken"] = continuation

    resp = s3_client.list_objects_v2(**params)

    # Folders (CommonPrefixes)
    base_len = len(f"{namespace}/")
    folders = []
    for cp in resp.get("CommonPrefixes", []):
        full_prefix = cp["Prefix"]
        rel = full_prefix[base_len:]  # Relative to namespace, keeps trailing /
        folder_name = rel.rstrip("/").rsplit("/", 1)[-1]
        folders.append({"name": folder_name, "prefix": rel})

    # Files (Contents)
    files = []
    for obj in resp.get("Contents", []):
        key = obj["Key"]
        rel_path = key[base_len:]
        if not rel_path or rel_path.endswith("/"):
            continue
        file_name = rel_path.rsplit("/", 1)[-1]
        files.append({
            "name": file_name,
            "path": rel_path,
            "size": obj.get("Size", 0),
            "lastModified": obj.get("LastModified", ""),
        })

    result = {"folders": folders, "files": files}
    if resp.get("NextContinuationToken"):
        result["nextToken"] = resp["NextContinuationToken"]
    return _json_response(200, result)


@route("GET", "/api/files/{namespace}/{path+}")
def _handle_get_file(event):
    """GET /api/files/{namespace}/{path+} — get file content or presigned URL."""
    namespace = event["pathParameters"]["namespace"]
    file_path = event["pathParameters"]["path"]

    if not _validate_namespace(namespace) or not _validate_path(file_path):
        return _json_response(400, {"error": "Invalid path"})

    s3_key = f"{namespace}/{file_path}"
    ext = os.path.splitext(file_path)[1].lower()

    try:
        if ext in _TEXT_EXTENSIONS:
            resp = s3_client.get_object(Bucket=S3_USER_FILES_BUCKET, Key=s3_key)
            size = resp.get("ContentLength", 0)
            if size > 1_048_576:  # 1 MB
                url = s3_client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": S3_USER_FILES_BUCKET, "Key": s3_key},
                    ExpiresIn=300,
                )
                return _json_response(200, {"presignedUrl": url, "size": size})
            content = resp["Body"].read().decode("utf-8", errors="replace")
            return _json_response(200, {"content": content, "size": size})
        else:
            # Binary file — return presigned URL
            head = s3_client.head_object(Bucket=S3_USER_FILES_BUCKET, Key=s3_key)
            url = s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": S3_USER_FILES_BUCKET, "Key": s3_key},
                ExpiresIn=300,
            )
            return _json_response(200, {
                "presignedUrl": url, "size": head.get("ContentLength", 0),
            })
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return _json_response(404, {"error": "File not found"})
        raise


@route("DELETE", "/api/files/{namespace}/{path+}")
def _handle_delete_file(event):
    """DELETE /api/files/{namespace}/{path+} — delete a file."""
    namespace = event["pathParameters"]["namespace"]
    file_path = event["pathParameters"]["path"]

    if not _validate_namespace(namespace) or not _validate_path(file_path):
        return _json_response(400, {"error": "Invalid path"})

    s3_key = f"{namespace}/{file_path}"
    try:
        s3_client.delete_object(Bucket=S3_USER_FILES_BUCKET, Key=s3_key)
    except ClientError as e:
        logger.error("Failed to delete S3 object %s: %s", s3_key, e)
        return _json_response(500, {"error": "Failed to delete file"})

    admin_sub = _get_admin_sub(event)
    _audit_log(admin_sub, "DELETE_FILE", s3_key)
    return _json_response(200, {"message": f"Deleted {file_path}"})


# ---- Skill Eval ----

@route("GET", "/api/skill-eval/{namespace}")
def _handle_get_skill_eval(event):
    """GET /api/skill-eval/{namespace} — get latest scan result for a user."""
    namespace = event["pathParameters"]["namespace"]
    if not _validate_namespace(namespace):
        return _json_response(400, {"error": "Invalid namespace"})

    # Look up userId from namespace
    channel_key = namespace.replace("_", ":", 1)
    user_id = ""
    try:
        resp = identity_table.get_item(
            Key={"PK": f"CHANNEL#{channel_key}", "SK": "PROFILE"}
        )
        item = resp.get("Item")
        if item:
            user_id = item.get("userId", "")
    except ClientError:
        pass

    # Try to get latest scan from USER# record first, fall back to SCAN#
    pk = f"USER#{user_id}" if user_id else f"SCAN#{namespace}"
    try:
        resp = identity_table.get_item(
            Key={"PK": pk, "SK": "SKILLSCAN#latest"}
        )
        item = resp.get("Item")
        if item:
            # Convert Decimal to int for JSON serialization
            result = {k: (int(v) if hasattr(v, 'as_integer_ratio') else v) for k, v in item.items()}
            return _json_response(200, result)
    except ClientError as e:
        logger.error("Failed to get scan result: %s", e)

    return _json_response(404, {"error": "No scan results found"})


@route("POST", "/api/skill-eval/{namespace}")
def _handle_post_skill_eval(event):
    """POST /api/skill-eval/{namespace} — trigger skill scan.

    Body: {"action": "audit"} or {"action": "eval"}
    """
    namespace = event["pathParameters"]["namespace"]
    if not _validate_namespace(namespace):
        return _json_response(400, {"error": "Invalid namespace"})

    if not SKILL_EVAL_FUNCTION_NAME:
        return _json_response(503, {"error": "Skill eval Lambda not configured"})

    body = event.get("parsedBody", {})
    action = body.get("action", "audit")
    if action not in ("audit", "eval"):
        return _json_response(400, {"error": "action must be 'audit' or 'eval'"})

    # Invoke skill-eval Lambda (synchronous for audit, could be async for eval)
    invoke_type = "RequestResponse" if action == "audit" else "Event"
    try:
        resp = lambda_client.invoke(
            FunctionName=SKILL_EVAL_FUNCTION_NAME,
            InvocationType=invoke_type,
            Payload=json.dumps({"action": action, "namespace": namespace}),
        )

        if invoke_type == "RequestResponse":
            payload = json.loads(resp["Payload"].read().decode("utf-8"))
            result = payload.get("body", payload)
            return _json_response(200, result)
        else:
            # Async invocation — return immediately
            return _json_response(202, {
                "message": f"Eval started for {namespace}",
                "action": action,
            })
    except ClientError as e:
        logger.error("Failed to invoke skill-eval: %s", e)
        return _json_response(500, {"error": "Failed to invoke skill eval"})


# ---- Sessions ----

@route("GET", "/api/sessions")
def _handle_get_sessions(event):
    """GET /api/sessions — list all active runtime sessions from DynamoDB."""
    items = []
    params = {
        "FilterExpression": "begins_with(PK, :u) AND SK = :sk",
        "ExpressionAttributeValues": {":u": "USER#", ":sk": "SESSION"},
    }
    while True:
        resp = identity_table.scan(**params)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        params["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    # Enrich with user profile info
    sessions = []
    for item in items:
        user_id = item.get("PK", "").replace("USER#", "")
        sessions.append({
            "userId": user_id,
            "sessionId": item.get("sessionId", ""),
            "createdAt": item.get("createdAt", ""),
            "lastActivity": item.get("lastActivity", ""),
        })

    # Sort by lastActivity descending
    sessions.sort(key=lambda s: s.get("lastActivity", ""), reverse=True)

    # Fetch display names in batch
    user_ids = [s["userId"] for s in sessions]
    for s in sessions:
        try:
            resp = identity_table.get_item(
                Key={"PK": f"USER#{s['userId']}", "SK": "PROFILE"}
            )
            profile = resp.get("Item", {})
            s["displayName"] = profile.get("displayName", "")
        except ClientError:
            s["displayName"] = ""

    return _json_response(200, {"sessions": sessions})


@route("POST", "/api/sessions/{sessionId}/stop")
def _handle_stop_session(event):
    """POST /api/sessions/{sessionId}/stop — stop an AgentCore runtime session."""
    session_id = event["pathParameters"]["sessionId"]
    admin_sub = _get_admin_sub(event)

    if not AGENTCORE_RUNTIME_ARN:
        return _json_response(503, {"error": "AGENTCORE_RUNTIME_ARN not configured"})

    try:
        agentcore_client.stop_runtime_session(
            runtimeSessionId=session_id,
            agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
        )
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "ResourceNotFoundException":
            # Session already stopped / not found — clean up DynamoDB anyway
            logger.info("Session %s not found on runtime, cleaning up DynamoDB", session_id)
        else:
            logger.error("Failed to stop session %s: %s", session_id, e)
            return _json_response(500, {"error": f"Failed to stop session: {error_code}"})

    # Remove SESSION record from DynamoDB (find user by scanning)
    try:
        resp = identity_table.scan(
            FilterExpression="SK = :sk AND sessionId = :sid",
            ExpressionAttributeValues={":sk": "SESSION", ":sid": session_id},
        )
        for item in resp.get("Items", []):
            identity_table.delete_item(
                Key={"PK": item["PK"], "SK": "SESSION"}
            )
    except ClientError as e:
        logger.error("Failed to clean up session record: %s", e)

    _audit_log(admin_sub, "STOP_SESSION", session_id)
    return _json_response(200, {"message": f"Session {session_id} stopped"})


def _match_route(method, path):
    """Match request to a route handler, supporting path parameters."""
    # Exact match first
    if (method, path) in ROUTES:
        return ROUTES[(method, path)], {}

    # Pattern matching for parameterized routes
    for (route_method, route_path), handler_fn in ROUTES.items():
        if route_method != method:
            continue
        route_parts = route_path.split("/")
        path_parts = path.split("/")

        # Handle greedy {path+} parameter
        if route_parts and route_parts[-1].endswith("+}"):
            if len(path_parts) >= len(route_parts):
                params = {}
                match = True
                for i, rp in enumerate(route_parts[:-1]):
                    if rp.startswith("{") and rp.endswith("}"):
                        params[rp[1:-1]] = urllib.parse.unquote(path_parts[i])
                    elif rp != path_parts[i]:
                        match = False
                        break
                if match:
                    param_name = route_parts[-1][1:-2]  # strip { and +}
                    params[param_name] = "/".join(
                        urllib.parse.unquote(p) for p in path_parts[len(route_parts) - 1:]
                    )
                    return handler_fn, params
            continue

        if len(route_parts) != len(path_parts):
            continue
        params = {}
        match = True
        for rp, pp in zip(route_parts, path_parts):
            if rp.startswith("{") and rp.endswith("}"):
                params[rp[1:-1]] = urllib.parse.unquote(pp)
            elif rp != pp:
                match = False
                break
        if match:
            return handler_fn, params

    return None, {}


def handler(event, context):
    """Lambda entry point — route dispatch."""
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "GET")
    path = http.get("path", "")

    handler_fn, path_params = _match_route(method, path)
    if not handler_fn:
        return _json_response(404, {"error": "Not found"})

    event["pathParameters"] = path_params

    # Parse query string
    qs = event.get("queryStringParameters") or {}
    event["queryParams"] = qs

    # Parse body
    body = event.get("body")
    if body:
        try:
            event["parsedBody"] = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            event["parsedBody"] = {}
    else:
        event["parsedBody"] = {}

    try:
        return handler_fn(event)
    except Exception:
        logger.exception("Unhandled error in %s %s", method, path)
        return _json_response(500, {"error": "Internal server error"})
