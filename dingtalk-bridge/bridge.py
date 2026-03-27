"""DingTalk Bridge — Long-running ECS Fargate service for DingTalk Robot.

Maintains a WebSocket connection to DingTalk via Stream mode, receives bot
messages, resolves user identity via DynamoDB, invokes per-user AgentCore
Runtime sessions, and sends responses back via DingTalk Robot API.

Architecture: same message-handling pattern as Router Lambda, but long-running
instead of event-driven (DingTalk uses client-initiated WebSocket, not webhooks).
"""

import asyncio
import json
import logging
import os
import re
import signal
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib import request as urllib_request

import boto3
from botocore.config import Config
import botocore.exceptions
from botocore.exceptions import ClientError
import dingtalk_stream
from dingtalk_stream import AckMessage

logger = logging.getLogger("dingtalk-bridge")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
logger.addHandler(handler)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DINGTALK_SECRET_ID = os.environ.get("DINGTALK_SECRET_ID", "")
AGENTCORE_RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]
AGENTCORE_QUALIFIER = os.environ["AGENTCORE_QUALIFIER"]
IDENTITY_TABLE_NAME = os.environ["IDENTITY_TABLE_NAME"]
USER_FILES_BUCKET = os.environ.get("USER_FILES_BUCKET", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
REGISTRATION_OPEN = os.environ.get("REGISTRATION_OPEN", "false").lower() == "true"
AGENTCORE_READ_TIMEOUT = 580  # seconds — generous for long subagent tasks
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8080"))

DINGTALK_API = "https://api.dingtalk.com"

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 3_750_000
CONTENT_TYPE_TO_EXT = {
    "image/jpeg": "jpeg", "image/png": "png",
    "image/gif": "gif", "image/webp": "webp",
}
MAX_DINGTALK_TEXT_LEN = 20000
MAX_FILE_BYTES = 20_000_000  # 20 MB for files/videos
FILE_EXT_MAP = {
    "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm",
    "video/avi": "avi", "video/x-msvideo": "avi",
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/zip": "zip", "application/x-zip-compressed": "zip",
    "application/x-rar-compressed": "rar",
    "text/plain": "txt", "text/csv": "csv",
    "audio/mpeg": "mp3", "audio/wav": "wav", "audio/ogg": "ogg",
}
SCREENSHOT_MARKER_RE = re.compile(r"\[SCREENSHOT:([^\]]+)\]")
SEND_FILE_MARKER_RE = re.compile(r"\[SEND_FILE:([^\]]+)\]")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi"}

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
identity_table = dynamodb.Table(IDENTITY_TABLE_NAME)
agentcore_client = boto3.client(
    "bedrock-agentcore",
    region_name=AWS_REGION,
    config=Config(
        read_timeout=AGENTCORE_READ_TIMEOUT,
        connect_timeout=10,
        retries={"max_attempts": 0},
    ),
)
secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)
s3_client = boto3.client("s3", region_name=AWS_REGION)

# ---------------------------------------------------------------------------
# Secret / token caching
# ---------------------------------------------------------------------------

_SECRET_CACHE_TTL = 900  # 15 min
_secret_cache: dict[str, tuple[str, float]] = {}


def _get_secret(secret_id: str) -> str:
    cached = _secret_cache.get(secret_id)
    if cached:
        value, fetched_at = cached
        if time.time() - fetched_at < _SECRET_CACHE_TTL:
            return value
    if not secret_id:
        return ""
    try:
        resp = secrets_client.get_secret_value(SecretId=secret_id)
        value = resp["SecretString"]
        _secret_cache[secret_id] = (value, time.time())
        return value
    except Exception as e:
        logger.warning("Failed to fetch secret %s: %s", secret_id, e)
        return ""


def _get_dingtalk_credentials() -> tuple[str, str]:
    raw = _get_secret(DINGTALK_SECRET_ID)
    if not raw:
        return "", ""
    try:
        data = json.loads(raw)
        return data.get("clientId", ""), data.get("clientSecret", "")
    except (json.JSONDecodeError, TypeError):
        return "", ""


_dingtalk_token_cache: dict[str, object] = {"token": "", "expires_at": 0}


def _get_dingtalk_access_token() -> str:
    if _dingtalk_token_cache["token"] and time.time() < _dingtalk_token_cache["expires_at"] - 100:
        return _dingtalk_token_cache["token"]

    client_id, client_secret = _get_dingtalk_credentials()
    if not client_id or not client_secret:
        logger.error("DingTalk clientId/clientSecret not configured")
        return ""

    url = f"{DINGTALK_API}/v1.0/oauth2/accessToken"
    data = json.dumps({"appKey": client_id, "appSecret": client_secret}).encode()
    req = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        resp = urllib_request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        token = result.get("accessToken", "")
        expire_in = result.get("expireIn", 7200)
        if token:
            _dingtalk_token_cache["token"] = token
            _dingtalk_token_cache["expires_at"] = time.time() + expire_in
            return token
        logger.error("DingTalk access token response missing accessToken: %s", result)
    except Exception as e:
        logger.error("Failed to get DingTalk access token: %s", e)
    return ""


# ---------------------------------------------------------------------------
# DynamoDB identity helpers (same pattern as Router Lambda)
# ---------------------------------------------------------------------------

BIND_CODE_TTL_SECONDS = 600


def is_user_allowed(channel: str, channel_user_id: str) -> bool:
    if REGISTRATION_OPEN:
        return True
    channel_key = f"{channel}:{channel_user_id}"
    try:
        resp = identity_table.get_item(Key={"PK": f"ALLOW#{channel_key}", "SK": "ALLOW"})
        return "Item" in resp
    except ClientError as e:
        logger.error("Allowlist check failed: %s", e)
    return False


def resolve_user(channel: str, channel_user_id: str, display_name: str = "") -> tuple[str | None, bool]:
    channel_key = f"{channel}:{channel_user_id}"
    pk = f"CHANNEL#{channel_key}"

    try:
        resp = identity_table.get_item(Key={"PK": pk, "SK": "PROFILE"})
        if "Item" in resp:
            return resp["Item"]["userId"], False
    except ClientError as e:
        logger.error("DynamoDB get_item failed: %s", e)

    if not is_user_allowed(channel, channel_user_id):
        logger.warning("User %s not on allowlist", channel_key)
        return None, False

    user_id = f"user_{uuid.uuid4().hex[:16]}"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    try:
        identity_table.put_item(
            Item={"PK": f"USER#{user_id}", "SK": "PROFILE", "userId": user_id,
                  "createdAt": now_iso, "displayName": display_name or channel_user_id},
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.error("Failed to create user profile: %s", e)

    try:
        identity_table.put_item(
            Item={"PK": pk, "SK": "PROFILE", "userId": user_id, "channel": channel,
                  "channelUserId": channel_user_id, "displayName": display_name or channel_user_id,
                  "boundAt": now_iso},
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            resp = identity_table.get_item(Key={"PK": pk, "SK": "PROFILE"})
            if "Item" in resp:
                return resp["Item"]["userId"], False
        logger.error("Failed to create channel mapping: %s", e)

    try:
        identity_table.put_item(
            Item={"PK": f"USER#{user_id}", "SK": f"CHANNEL#{channel_key}",
                  "channel": channel, "channelUserId": channel_user_id, "boundAt": now_iso})
    except ClientError:
        pass

    logger.info("New user created: %s for %s", user_id, channel_key)
    return user_id, True


def get_or_create_session(user_id: str) -> str:
    pk = f"USER#{user_id}"
    try:
        resp = identity_table.get_item(Key={"PK": pk, "SK": "SESSION"})
        if "Item" in resp:
            identity_table.update_item(
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
        identity_table.put_item(
            Item={"PK": pk, "SK": "SESSION", "sessionId": session_id,
                  "createdAt": now_iso, "lastActivity": now_iso})
    except ClientError as e:
        logger.error("Failed to create session: %s", e)
    logger.info("New session: %s for %s", session_id, user_id)
    return session_id


def create_bind_code(user_id: str) -> str:
    code = uuid.uuid4().hex[:8].upper()
    ttl = int(time.time()) + BIND_CODE_TTL_SECONDS
    identity_table.put_item(
        Item={"PK": f"BIND#{code}", "SK": "BIND", "userId": user_id,
              "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ttl": ttl})
    return code


def redeem_bind_code(code: str, channel: str, channel_user_id: str, display_name: str = "") -> tuple[str | None, bool]:
    code = code.strip().upper()
    try:
        resp = identity_table.get_item(Key={"PK": f"BIND#{code}", "SK": "BIND"})
        item = resp.get("Item")
        if not item or item.get("ttl", 0) < int(time.time()):
            return None, False
        user_id = item["userId"]
        channel_key = f"{channel}:{channel_user_id}"
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        identity_table.put_item(
            Item={"PK": f"CHANNEL#{channel_key}", "SK": "PROFILE", "userId": user_id,
                  "channel": channel, "channelUserId": channel_user_id,
                  "displayName": display_name or channel_user_id, "boundAt": now_iso})
        identity_table.put_item(
            Item={"PK": f"USER#{user_id}", "SK": f"CHANNEL#{channel_key}",
                  "channel": channel, "channelUserId": channel_user_id, "boundAt": now_iso})
        identity_table.delete_item(Key={"PK": f"BIND#{code}", "SK": "BIND"})
        logger.info("Bind code %s redeemed: %s -> %s", code, channel_key, user_id)
        return user_id, True
    except ClientError as e:
        logger.error("Bind code redemption failed: %s", e)
        return None, False


# ---------------------------------------------------------------------------
# AgentCore invocation
# ---------------------------------------------------------------------------

MAX_INVOKE_RETRIES = 3
INVOKE_RETRY_DELAYS = [5, 15, 30]  # seconds between retries (cold start can take 30-60s)


def invoke_agent_runtime(session_id: str, user_id: str, actor_id: str, message) -> dict:
    payload = json.dumps({
        "action": "chat",
        "userId": user_id,
        "actorId": actor_id,
        "channel": "dingtalk",
        "message": message,
    }).encode()

    last_error = None
    for attempt in range(MAX_INVOKE_RETRIES):
        try:
            logger.info("Invoking AgentCore: session=%s user=%s attempt=%d", session_id, user_id, attempt + 1)
            resp = agentcore_client.invoke_agent_runtime(
                agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
                qualifier=AGENTCORE_QUALIFIER,
                runtimeSessionId=session_id,
                runtimeUserId=actor_id,
                payload=payload,
                contentType="application/json",
                accept="application/json",
            )
            MAX_RESPONSE_BYTES = 500_000
            body = resp.get("response")
            if body:
                body_bytes = body.read(MAX_RESPONSE_BYTES + 1) if hasattr(body, "read") else str(body).encode()[:MAX_RESPONSE_BYTES]
                body_text = body_bytes.decode("utf-8", errors="replace")[:MAX_RESPONSE_BYTES]
                logger.info("AgentCore response len=%d first200=%s", len(body_text), body_text[:200])
                try:
                    return json.loads(body_text)
                except json.JSONDecodeError:
                    return {"response": body_text}
            return {"response": "No response from agent."}
        except (botocore.exceptions.ConnectionClosedError, ConnectionResetError) as e:
            last_error = e
            if attempt < MAX_INVOKE_RETRIES - 1:
                delay = INVOKE_RETRY_DELAYS[attempt]
                logger.warning("AgentCore connection reset (attempt %d/%d), retrying in %ds: %s",
                               attempt + 1, MAX_INVOKE_RETRIES, delay, e)
                time.sleep(delay)
            else:
                logger.error("AgentCore invocation failed after %d attempts: %s", MAX_INVOKE_RETRIES, e, exc_info=True)
        except Exception as e:
            logger.error("AgentCore invocation failed: %s", e, exc_info=True)
            return {"response": "Sorry, I'm having trouble right now. Please try again later."}

    return {"response": "Sorry, I'm having trouble right now. Please try again later."}


# ---------------------------------------------------------------------------
# Content block extraction (ported from Router Lambda)
# ---------------------------------------------------------------------------

def _extract_text_from_content_blocks(text: str) -> str:
    if not text or not isinstance(text, str):
        return text
    result = text
    decoder = json.JSONDecoder(strict=False)
    for _ in range(10):
        prev = result
        rebuilt = []
        i = 0
        while i < len(result):
            pos = result.find("[{", i)
            if pos == -1:
                rebuilt.append(result[i:])
                break
            rebuilt.append(result[i:pos])
            try:
                blocks, end = decoder.raw_decode(result, pos)
                if isinstance(blocks, list) and blocks and all(isinstance(b, dict) for b in blocks):
                    has_typed = any(b.get("type") for b in blocks)
                    if has_typed:
                        parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
                        rebuilt.append("".join(parts))
                        i = end
                        continue
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            remainder = result[pos:]
            if re.match(r'^\[\{\s*"', remainder) or remainder.strip() == "[{":
                break
            rebuilt.append("[")
            i = pos + 1
        result = "".join(rebuilt)
        if result == prev:
            break
    return result


# ---------------------------------------------------------------------------
# DingTalk message sending
# ---------------------------------------------------------------------------

def send_dingtalk_message(receiver_id: str, text: str, *, is_group: bool = False,
                          conversation_id: str = "") -> None:
    token = _get_dingtalk_access_token()
    if not token:
        logger.error("No DingTalk access token available")
        return

    client_id, _ = _get_dingtalk_credentials()
    headers = {
        "Content-Type": "application/json",
        "x-acs-dingtalk-access-token": token,
    }

    chunks = [text[i:i + MAX_DINGTALK_TEXT_LEN]
              for i in range(0, len(text), MAX_DINGTALK_TEXT_LEN)] if len(text) > MAX_DINGTALK_TEXT_LEN else [text]

    for chunk in chunks:
        if is_group:
            url = f"{DINGTALK_API}/v1.0/robot/groupMessages/send"
            data = json.dumps({
                "robotCode": client_id,
                "openConversationId": conversation_id or receiver_id,
                "msgKey": "sampleText",
                "msgParam": json.dumps({"content": chunk}),
            }).encode()
        else:
            url = f"{DINGTALK_API}/v1.0/robot/oToMessages/batchSend"
            data = json.dumps({
                "robotCode": client_id,
                "userIds": [receiver_id],
                "msgKey": "sampleText",
                "msgParam": json.dumps({"content": chunk}),
            }).encode()

        req = urllib_request.Request(url, data=data, headers=headers)
        try:
            urllib_request.urlopen(req, timeout=15)
        except Exception as e:
            logger.error("Failed to send DingTalk message to %s: %s", receiver_id, e)


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------

def _get_dingtalk_download_url(download_code: str) -> str:
    """Get the actual download URL from DingTalk messageFiles/download API."""
    token = _get_dingtalk_access_token()
    client_id, _ = _get_dingtalk_credentials()
    if not token or not client_id:
        return ""

    url = f"{DINGTALK_API}/v1.0/robot/messageFiles/download"
    data = json.dumps({"downloadCode": download_code, "robotCode": client_id}).encode()
    logger.info("Requesting DingTalk download URL: downloadCode=%s", download_code[:30])
    req = urllib_request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "x-acs-dingtalk-access-token": token,
    })
    try:
        resp = urllib_request.urlopen(req, timeout=30)
        body = resp.read(1_000_000)
        result = json.loads(body.decode("utf-8", errors="replace"))
        download_url = result.get("downloadUrl", "")
        if not download_url:
            logger.error("DingTalk download API missing downloadUrl: %s", json.dumps(result, ensure_ascii=False)[:500])
            return ""
        # DingTalk OSS URLs may use HTTP — upgrade to HTTPS (OSS supports both)
        if download_url.startswith("http://"):
            download_url = "https://" + download_url[7:]
        logger.info("DingTalk download URL obtained (len=%d)", len(download_url))
        return download_url
    except Exception as e:
        logger.error("Failed to get DingTalk download URL: %s", e)
        return ""


def _download_from_url(download_url: str, max_bytes: int = 4 * 1024 * 1024) -> tuple[bytes | None, str]:
    """Download file bytes from a URL. Returns (bytes, content_type)."""
    try:
        req = urllib_request.Request(download_url)
        resp = urllib_request.urlopen(req, timeout=60)
        content_type = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
        file_bytes = resp.read(max_bytes + 1)
        if len(file_bytes) > max_bytes:
            logger.warning("Downloaded file exceeds size limit: %d > %d", len(file_bytes), max_bytes)
            return None, ""
        logger.info("Downloaded from URL: %d bytes, type=%s", len(file_bytes), content_type)
        return file_bytes, content_type
    except Exception as e:
        logger.error("Failed to download from URL: %s", e)
        return None, ""


def _download_dingtalk_image(download_code: str) -> tuple[bytes | None, str]:
    """Download image from DingTalk using downloadCode (two-step: get URL, then download).
    Returns (bytes, content_type)."""
    download_url = _get_dingtalk_download_url(download_code)
    if not download_url:
        return None, ""

    image_bytes, content_type = _download_from_url(download_url, max_bytes=MAX_IMAGE_BYTES)
    if not image_bytes:
        return None, ""

    # Infer content type from URL extension if response type is generic
    if content_type in ("application/octet-stream", "binary/octet-stream", ""):
        url_lower = download_url.split("?")[0].lower()
        if url_lower.endswith(".jpg") or url_lower.endswith(".jpeg"):
            content_type = "image/jpeg"
        elif url_lower.endswith(".png"):
            content_type = "image/png"
        elif url_lower.endswith(".gif"):
            content_type = "image/gif"
        elif url_lower.endswith(".webp"):
            content_type = "image/webp"
        else:
            content_type = "image/jpeg"  # safe default for DingTalk images

    if content_type not in ALLOWED_IMAGE_TYPES:
        logger.warning("DingTalk image type %s not allowed", content_type)
        return None, ""

    logger.info("DingTalk image ready: %d bytes, type=%s", len(image_bytes), content_type)
    return image_bytes, content_type


def _upload_image_to_s3(image_bytes: bytes, namespace: str, content_type: str) -> str | None:
    if not USER_FILES_BUCKET or content_type not in ALLOWED_IMAGE_TYPES:
        return None
    if len(image_bytes) > MAX_IMAGE_BYTES:
        logger.warning("Image too large: %d bytes", len(image_bytes))
        return None
    ext = CONTENT_TYPE_TO_EXT.get(content_type, "bin")
    s3_key = f"{namespace}/_uploads/img_{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
    try:
        s3_client.put_object(Bucket=USER_FILES_BUCKET, Key=s3_key, Body=image_bytes, ContentType=content_type)
        logger.info("Uploaded image to s3://%s/%s", USER_FILES_BUCKET, s3_key)
        return s3_key
    except Exception as e:
        logger.error("S3 image upload failed: %s", e)
        return None


def _download_dingtalk_media(download_code: str, max_bytes: int = MAX_FILE_BYTES) -> tuple[bytes | None, str]:
    """Download any file type from DingTalk using downloadCode (two-step: get URL, then download).
    Returns (bytes, content_type)."""
    download_url = _get_dingtalk_download_url(download_code)
    if not download_url:
        return None, ""
    return _download_from_url(download_url, max_bytes=max_bytes)


def _upload_file_to_s3(file_bytes: bytes, namespace: str, content_type: str,
                       prefix: str = "file", ext: str = "") -> str | None:
    """Upload a file to S3 under the user's namespace. Returns S3 key or None."""
    if not USER_FILES_BUCKET:
        return None
    if not ext:
        ext = FILE_EXT_MAP.get(content_type, "bin")
    s3_key = f"{namespace}/_uploads/{prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
    try:
        s3_client.put_object(Bucket=USER_FILES_BUCKET, Key=s3_key, Body=file_bytes, ContentType=content_type)
        logger.info("Uploaded file to s3://%s/%s (%d bytes)", USER_FILES_BUCKET, s3_key, len(file_bytes))
        return s3_key
    except Exception as e:
        logger.error("S3 file upload failed: %s", e)
        return None


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# Screenshot delivery helpers
# ---------------------------------------------------------------------------

def _extract_screenshots(text: str) -> tuple[str, list[str]]:
    """Extract [SCREENSHOT:key] markers from text. Returns (clean_text, [s3_keys])."""
    keys = SCREENSHOT_MARKER_RE.findall(text)
    clean = SCREENSHOT_MARKER_RE.sub("", text).strip()
    return clean, keys


def _fetch_s3_image(s3_key: str, namespace: str) -> bytes | None:
    """Fetch screenshot image bytes from S3. Returns None on error or invalid key."""
    if ".." in s3_key:
        logger.error("Rejected S3 screenshot key with path traversal: %s", s3_key)
        return None
    expected_prefix = f"{namespace}/_screenshots/"
    if not s3_key.startswith(expected_prefix):
        logger.error("Rejected S3 screenshot key outside namespace: %s (expected: %s)", s3_key, expected_prefix)
        return None
    try:
        resp = s3_client.get_object(Bucket=USER_FILES_BUCKET, Key=s3_key)
        return resp["Body"].read()
    except Exception as e:
        logger.error("Failed to fetch screenshot from S3: %s — %s", s3_key, e)
        return None


def _send_dingtalk_image(receiver_id: str, image_url: str, *, is_group: bool = False,
                         conversation_id: str = "") -> None:
    """Send an image to DingTalk using a URL (presigned S3 URL)."""
    token = _get_dingtalk_access_token()
    if not token:
        return
    client_id, _ = _get_dingtalk_credentials()
    headers = {
        "Content-Type": "application/json",
        "x-acs-dingtalk-access-token": token,
    }
    if is_group:
        url = f"{DINGTALK_API}/v1.0/robot/groupMessages/send"
        body = json.dumps({
            "robotCode": client_id,
            "openConversationId": conversation_id or receiver_id,
            "msgKey": "sampleImageMsg",
            "msgParam": json.dumps({"photoURL": image_url}),
        }).encode()
    else:
        url = f"{DINGTALK_API}/v1.0/robot/oToMessages/batchSend"
        body = json.dumps({
            "robotCode": client_id,
            "userIds": [receiver_id],
            "msgKey": "sampleImageMsg",
            "msgParam": json.dumps({"photoURL": image_url}),
        }).encode()
    req = urllib_request.Request(url, data=body, headers=headers)
    try:
        urllib_request.urlopen(req, timeout=15)
    except Exception as e:
        logger.error("Failed to send DingTalk image to %s: %s", receiver_id, e)


def _deliver_screenshot(s3_key: str, namespace: str, sender_id: str,
                        conversation_id: str, is_dm: bool) -> None:
    """Fetch a screenshot from S3 and send it to DingTalk as an image."""
    image_bytes = _fetch_s3_image(s3_key, namespace)
    if not image_bytes:
        return
    try:
        presigned_url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": USER_FILES_BUCKET, "Key": s3_key},
            ExpiresIn=3600,
        )
    except Exception as e:
        logger.error("Failed to generate presigned URL for %s: %s", s3_key, e)
        return
    if is_dm:
        _send_dingtalk_image(sender_id, presigned_url)
    else:
        _send_dingtalk_image(conversation_id, presigned_url, is_group=True,
                             conversation_id=conversation_id)


# ---------------------------------------------------------------------------
# Outbound file delivery helpers
# ---------------------------------------------------------------------------

def _extract_send_files(text: str) -> tuple[str, list[str]]:
    """Extract [SEND_FILE:path] markers from text. Returns (clean_text, [relative_paths])."""
    paths = SEND_FILE_MARKER_RE.findall(text)
    clean = SEND_FILE_MARKER_RE.sub("", text).strip()
    return clean, paths


def _generate_presigned_url(s3_key: str) -> str | None:
    """Generate a presigned GET URL for an S3 key (1h expiry)."""
    try:
        return s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": USER_FILES_BUCKET, "Key": s3_key},
            ExpiresIn=3600,
        )
    except Exception as e:
        logger.error("Failed to generate presigned URL for %s: %s", s3_key, e)
        return None


def _send_dingtalk_link(receiver_id: str, title: str, text: str, message_url: str,
                        *, is_group: bool = False, conversation_id: str = "") -> None:
    """Send a link card to DingTalk (for file/video downloads)."""
    token = _get_dingtalk_access_token()
    if not token:
        return
    client_id, _ = _get_dingtalk_credentials()
    headers = {
        "Content-Type": "application/json",
        "x-acs-dingtalk-access-token": token,
    }
    msg_param = json.dumps({"title": title, "text": text, "messageUrl": message_url, "picUrl": ""})
    if is_group:
        url = f"{DINGTALK_API}/v1.0/robot/groupMessages/send"
        body = json.dumps({
            "robotCode": client_id,
            "openConversationId": conversation_id or receiver_id,
            "msgKey": "sampleLink",
            "msgParam": msg_param,
        }).encode()
    else:
        url = f"{DINGTALK_API}/v1.0/robot/oToMessages/batchSend"
        body = json.dumps({
            "robotCode": client_id,
            "userIds": [receiver_id],
            "msgKey": "sampleLink",
            "msgParam": msg_param,
        }).encode()
    req = urllib_request.Request(url, data=body, headers=headers)
    try:
        urllib_request.urlopen(req, timeout=15)
    except Exception as e:
        logger.error("Failed to send DingTalk link to %s: %s", receiver_id, e)


def _deliver_file(relative_path: str, namespace: str, sender_id: str,
                  conversation_id: str, is_dm: bool) -> None:
    """Deliver a user file from S3 to DingTalk. Images sent inline, others as link cards."""
    if ".." in relative_path:
        logger.error("Rejected SEND_FILE with path traversal: %s", relative_path)
        return

    s3_key = f"{namespace}/{relative_path}"

    # Verify the file exists
    try:
        head = s3_client.head_object(Bucket=USER_FILES_BUCKET, Key=s3_key)
        file_size = head.get("ContentLength", 0)
    except Exception as e:
        logger.error("SEND_FILE target not found in S3: %s — %s", s3_key, e)
        return

    presigned_url = _generate_presigned_url(s3_key)
    if not presigned_url:
        return

    # Determine file type from extension
    filename = relative_path.rsplit("/", 1)[-1] if "/" in relative_path else relative_path
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""

    if ext in IMAGE_EXTENSIONS:
        # Send as inline image
        if is_dm:
            _send_dingtalk_image(sender_id, presigned_url)
        else:
            _send_dingtalk_image(conversation_id, presigned_url,
                                 is_group=True, conversation_id=conversation_id)
    else:
        # Send as link card (files, videos, etc.)
        size_str = _format_size(file_size)
        if ext in VIDEO_EXTENSIONS:
            desc = f"Video · {size_str} · Click to download"
        else:
            desc = f"File · {size_str} · Click to download"
        if is_dm:
            _send_dingtalk_link(sender_id, filename, desc, presigned_url)
        else:
            _send_dingtalk_link(conversation_id, filename, desc, presigned_url,
                                is_group=True, conversation_id=conversation_id)

    logger.info("Delivered file to DingTalk: %s (%s)", filename, ext or "no ext")


# ---------------------------------------------------------------------------
# Bind / link command helpers
# ---------------------------------------------------------------------------

def _is_bind_command(text: str) -> tuple[bool, str]:
    if not text:
        return False, ""
    parts = text.strip().split()
    if len(parts) == 2 and parts[0].lower() in ("link", "bind"):
        code = parts[1].strip().upper()
        if len(code) == 8 and code.isalnum():
            return True, code
    return False, ""


def _is_link_command(text: str) -> bool:
    if not text:
        return False
    return text.strip().lower() in ("link accounts", "link account", "link")


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

# In-memory dedup cache
_processed_messages: dict[str, float] = {}
_DEDUP_TTL = 300  # 5 min


def _cleanup_dedup():
    now = time.time()
    expired = [k for k, v in _processed_messages.items() if now - v > _DEDUP_TTL]
    for k in expired:
        del _processed_messages[k]


def _process_message_sync(data: dict) -> None:
    """Synchronous message processing (runs in thread executor)."""
    try:
        _process_message_inner(data)
    except Exception:
        logger.error("Unhandled error processing message", exc_info=True)


def _process_message_inner(data: dict) -> None:
    """Inner message processing logic."""
    msg_id = data.get("msgId", "")
    if msg_id:
        if msg_id in _processed_messages:
            logger.info("Dedup: skipping already-processed message %s", msg_id)
            return
        _processed_messages[msg_id] = time.time()
        if len(_processed_messages) > 100:
            _cleanup_dedup()

    # Extract message content
    msg_type = data.get("msgtype", "text")
    sender_id = data.get("senderStaffId") or data.get("senderId", "")
    sender_nick = data.get("senderNick", "")
    conversation_type = data.get("conversationType", "1")  # "1"=DM, "2"=group
    conversation_id = data.get("conversationId", "")
    is_dm = conversation_type == "1"

    # Extract text
    text = ""
    if msg_type == "text":
        text_obj = data.get("text", {})
        text = (text_obj.get("content", "") if isinstance(text_obj, dict) else str(text_obj)).strip()
    elif msg_type == "richText":
        rich_text = data.get("content", {}).get("richText", [])
        for item in rich_text:
            if "text" in item:
                text += item["text"]
        text = text.strip()

    # Extract image download code
    download_code = ""
    if msg_type == "picture":
        content = data.get("content", {})
        logger.info("Picture message raw content type=%s value=%s", type(content).__name__, str(content)[:300])
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                content = {}
        download_code = content.get("downloadCode", "") or content.get("pictureDownloadCode", "")
        logger.info("Picture downloadCode=%s (len=%d)", download_code[:30] if download_code else "(empty)", len(download_code))

    # Extract file info
    file_download_code = ""
    file_name = ""
    if msg_type == "file":
        content = data.get("content", {})
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                content = {}
        file_download_code = content.get("downloadCode", "")
        file_name = content.get("fileName", "unknown_file")

    # Extract video info
    video_download_code = ""
    video_duration = ""
    if msg_type == "video":
        content = data.get("content", {})
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                content = {}
        video_download_code = content.get("downloadCode", "")
        video_duration = content.get("duration", "")

    has_image = bool(download_code)
    has_file = bool(file_download_code)
    has_video = bool(video_download_code)

    if not text and not has_image and not has_file and not has_video:
        logger.info("Ignoring empty message from %s", sender_id)
        return

    if not sender_id:
        logger.warning("Message missing senderId, skipping")
        return

    if len(sender_id) > 128:
        logger.warning("sender_id too long (%d), rejecting", len(sender_id))
        return

    actor_id = f"dingtalk:{sender_id}"
    channel_target = sender_id if is_dm else f"group:{conversation_id}"

    # Handle bind command
    is_bind, code = _is_bind_command(text)
    if is_bind:
        bound_user, success = redeem_bind_code(code, "dingtalk", sender_id, sender_nick)
        msg = "Accounts linked successfully!" if success else "Invalid or expired link code."
        if is_dm:
            send_dingtalk_message(sender_id, msg)
        else:
            send_dingtalk_message(conversation_id, msg, is_group=True, conversation_id=conversation_id)
        return

    # Resolve user identity
    resolved_user_id, is_new = resolve_user("dingtalk", sender_id, sender_nick)
    if resolved_user_id is None:
        rejection = (
            f"Sorry, this bot is private and requires an invitation.\n\n"
            f"Your ID: dingtalk:{sender_id}\n\n"
            f"Send this ID to the bot admin to request access."
        )
        if is_dm:
            send_dingtalk_message(sender_id, rejection)
        else:
            send_dingtalk_message(conversation_id, rejection, is_group=True, conversation_id=conversation_id)
        return

    # Handle link-accounts command
    if _is_link_command(text):
        bind_code = create_bind_code(resolved_user_id)
        link_msg = (
            f"Your link code is: {bind_code}\n\n"
            f"Enter this code on another channel within 10 minutes by typing: link {bind_code}"
        )
        if is_dm:
            send_dingtalk_message(sender_id, link_msg)
        else:
            send_dingtalk_message(conversation_id, link_msg, is_group=True, conversation_id=conversation_id)
        return

    # Build message payload
    agent_message = text
    namespace = actor_id.replace(":", "_")
    if has_image:
        image_bytes, content_type = _download_dingtalk_image(download_code)
        if image_bytes:
            s3_key = _upload_image_to_s3(image_bytes, namespace, content_type)
            if s3_key:
                agent_message = {
                    "text": text or "What is this image?",
                    "images": [{"s3Key": s3_key, "contentType": content_type}],
                }
            else:
                _send_reply(sender_id, conversation_id, is_dm, "Sorry, I couldn't process that image.")
                return
        else:
            _send_reply(sender_id, conversation_id, is_dm, "Sorry, I couldn't download that image.")
            return
    elif has_file:
        file_bytes, content_type = _download_dingtalk_media(file_download_code)
        if file_bytes:
            ext = FILE_EXT_MAP.get(content_type, "")
            if not ext and "." in file_name:
                ext = file_name.rsplit(".", 1)[-1].lower()
            s3_key = _upload_file_to_s3(file_bytes, namespace, content_type, prefix="file", ext=ext or "bin")
            if s3_key:
                size_str = _format_size(len(file_bytes))
                relative_path = s3_key.split("/", 1)[1] if "/" in s3_key else s3_key
                agent_message = (
                    (f"{text}\n\n" if text else "")
                    + f"[User uploaded a file: {file_name} ({content_type}, {size_str})]\n"
                      f"[File saved to: {relative_path}]\n"
                      f"[Use the file management tools to read or process this file.]"
                )
            else:
                _send_reply(sender_id, conversation_id, is_dm, "Sorry, I couldn't save that file. Please try again.")
                return
        else:
            _send_reply(sender_id, conversation_id, is_dm, "Sorry, I couldn't download that file from DingTalk.")
            return
    elif has_video:
        video_bytes, content_type = _download_dingtalk_media(video_download_code)
        if video_bytes:
            ext = content_type.split("/")[-1] if "/" in content_type else "mp4"
            if ext not in ("mp4", "mov", "webm", "avi"):
                ext = "mp4"
            s3_key = _upload_file_to_s3(video_bytes, namespace, content_type, prefix="vid", ext=ext)
            if s3_key:
                size_str = _format_size(len(video_bytes))
                relative_path = s3_key.split("/", 1)[1] if "/" in s3_key else s3_key
                duration_info = f", {video_duration}s" if video_duration else ""
                agent_message = (
                    (f"{text}\n\n" if text else "")
                    + f"[User uploaded a video ({content_type}, {size_str}{duration_info})]\n"
                      f"[Video saved to: {relative_path}]\n"
                      f"[Use the file management tools to access this video.]"
                )
            else:
                _send_reply(sender_id, conversation_id, is_dm, "Sorry, I couldn't save that video. Please try again.")
                return
        else:
            _send_reply(sender_id, conversation_id, is_dm, "Sorry, I couldn't download that video from DingTalk.")
            return

    # Get or create session
    session_id = get_or_create_session(resolved_user_id)

    image_count = 0 if isinstance(agent_message, str) else len(agent_message.get("images", []))
    logger.info(
        "DingTalk: user=%s actor=%s session=%s text_len=%d images=%d dm=%s",
        resolved_user_id, actor_id, session_id, len(text), image_count, is_dm,
    )

    # Invoke AgentCore
    result = invoke_agent_runtime(session_id, resolved_user_id, actor_id, agent_message)
    response_text = result.get("response", "Sorry, I couldn't process your message.")
    response_text = _extract_text_from_content_blocks(response_text)
    if not response_text or not response_text.strip():
        response_text = "Sorry, I received an empty response. Please try again."

    # Deliver screenshots as images before sending text
    clean_text, screenshot_keys = _extract_screenshots(response_text)
    if screenshot_keys:
        for key in screenshot_keys:
            _deliver_screenshot(key, namespace, sender_id, conversation_id, is_dm)
        response_text = clean_text

    # Deliver outbound files (images inline, files/videos as link cards)
    clean_text, file_paths = _extract_send_files(response_text)
    if file_paths:
        for path in file_paths:
            _deliver_file(path, namespace, sender_id, conversation_id, is_dm)
        response_text = clean_text

    logger.info("Response len=%d first200=%s", len(response_text), response_text[:200])

    # Send reply
    if response_text.strip():
        _send_reply(sender_id, conversation_id, is_dm, response_text)
    logger.info("Reply sent to %s (dm=%s)", sender_id, is_dm)


def _send_reply(sender_id: str, conversation_id: str, is_dm: bool, text: str) -> None:
    if is_dm:
        send_dingtalk_message(sender_id, text)
    else:
        send_dingtalk_message(conversation_id, text, is_group=True, conversation_id=conversation_id)


class RobotMessageHandler(dingtalk_stream.ChatbotHandler):
    """DingTalk Stream callback handler for robot messages."""

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        data = callback.data
        logger.info("Received message: msgtype=%s from=%s",
                     data.get("msgtype"), data.get("senderNick", "unknown"))

        # Process in background thread (boto3 is synchronous)
        asyncio.get_event_loop().run_in_executor(None, _process_message_sync, data)

        # ACK immediately
        return AckMessage.STATUS_OK, "OK"


# ---------------------------------------------------------------------------
# Health check HTTP server
# ---------------------------------------------------------------------------

_websocket_connected = False


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({
                "status": "ok",
                "service": "openclaw-dingtalk-bridge",
                "websocket": "connected" if _websocket_connected else "disconnected",
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress request logs


def _start_health_server():
    server = HTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health check server started on port %d", HEALTH_PORT)
    return server


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _websocket_connected

    logger.info("Starting DingTalk Bridge...")

    # Start health check server
    health_server = _start_health_server()

    # Fetch credentials
    client_id, client_secret = _get_dingtalk_credentials()
    if not client_id or not client_secret:
        logger.error("DingTalk credentials not configured (DINGTALK_SECRET_ID=%s)", DINGTALK_SECRET_ID)
        # Keep running for health check — ECS will restart if needed
        signal.pause()
        return

    logger.info("DingTalk credentials loaded (clientId=%s...)", client_id[:8])

    # Pre-warm access token
    token = _get_dingtalk_access_token()
    if token:
        logger.info("DingTalk access token acquired")
    else:
        logger.warning("Failed to acquire initial DingTalk access token — will retry on first message")

    # Create DingTalk Stream client
    credential = dingtalk_stream.Credential(client_id, client_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(
        dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
        RobotMessageHandler(),
    )

    _websocket_connected = True
    logger.info("DingTalk Stream client starting...")

    # Graceful shutdown
    def _shutdown(signum, frame):
        global _websocket_connected
        logger.info("Received signal %d, shutting down...", signum)
        _websocket_connected = False
        health_server.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # start_forever() blocks and handles reconnection internally
    try:
        client.start_forever()
    except SystemExit:
        logger.info("DingTalk Bridge stopped")
    except Exception as e:
        logger.error("DingTalk Stream client exited: %s", e, exc_info=True)
        _websocket_connected = False


if __name__ == "__main__":
    main()
