# Design Document: DingTalk (钉钉) Channel Integration

**Author:** Research Branch
**Status:** Draft
**Date:** 2026-03-19

---

## 1. Background & Motivation

OpenClaw on AgentCore currently supports **Telegram**, **Slack**, and **Feishu** as messaging channels. DingTalk (钉钉) is Alibaba's enterprise collaboration platform, widely used in China with over 700 million registered users. Adding DingTalk as a fourth channel extends the bot's reach to the largest enterprise IM user base in China.

This document analyses the DingTalk Bot API, maps it to the existing channel architecture, and proposes a concrete implementation plan.

> **DingTalk supports two event reception modes:**
> 1. **HTTP Callback (Webhook)** — same model as Telegram/Slack/Feishu. Compatible with our Lambda architecture.
> 2. **Stream Mode** — persistent TLS long connection (similar to WebSocket). Requires a long-running process, NOT compatible with Lambda.
>
> This design uses the **HTTP Callback (Webhook)** mode to maintain architectural consistency.
> Stream Mode is documented as a future alternative in Section 12.

> **Important caveat:** DingTalk's official documentation and SDKs have shifted heavily toward Stream Mode.
> HTTP Callback mode is still supported but receives less documentation attention. We should monitor
> for deprecation signals. The `sessionWebhook` reply mechanism (Section 3.8) provides a valuable
> simplification that partially mitigates this risk.

---

## 2. Existing Channel Architecture Summary

The current system follows a consistent pattern for each channel:

```
Channel App (Telegram/Slack/Feishu)
    |
    v  (webhook HTTPS POST)
API Gateway HTTP API
    |  POST /webhook/{channel}
    v
Router Lambda
    |  1. Validate webhook signature
    |  2. Extract channel_user_id + message text
    |  3. Self-invoke async (return 200 immediately)
    |  4. Resolve user identity (DynamoDB)
    |  5. Invoke AgentCore Runtime
    |  6. Send response back via channel API
    v
AgentCore per-user microVM
```

### Per-Channel Touchpoints (what changes per channel):

| Component | What varies per channel |
|---|---|
| **Secrets Manager** | `openclaw/channels/{channel}` — credentials format differs |
| **API Gateway route** | `POST /webhook/{channel}` |
| **Router Lambda** | Webhook validation, message extraction, response sending |
| **CDK router_stack.py** | Route registration, env var for secret |
| **CDK security_stack.py** | Channel secret placeholder |
| **Cron Lambda** | `deliver_response()` channel dispatch |
| **Setup script** | `scripts/setup-{channel}.sh` |

---

## 3. DingTalk Bot API Analysis

### 3.1 Authentication Model

DingTalk uses an **AppKey + AppSecret** model (similar to Feishu's App ID + App Secret):

| Credential | Purpose | Storage Format |
|---|---|---|
| `appKey` | Application identifier (also called `clientId` in Stream SDK) | Part of JSON secret |
| `appSecret` | Application secret key (also called `clientSecret` in Stream SDK) | Part of JSON secret |
| `robotCode` | Robot identifier (usually same as `appKey`) | Part of JSON secret |

**Access Token Flow:**
```
POST https://api.dingtalk.com/v1.0/oauth2/accessToken
Content-Type: application/json

{
    "appKey": "dingXXXXXXXX",
    "appSecret": "XXXXXXXX"
}

Response:
{
    "accessToken": "xxxx",
    "expireIn": 7200
}
```

- Token expires in **2 hours** (7200 seconds) — same as Feishu
- Must be cached and refreshed before expiry
- All OpenAPI calls use `x-acs-dingtalk-access-token: {accessToken}` header

**Comparison with existing channels:**

| Aspect | Telegram | Slack | Feishu | DingTalk |
|---|---|---|---|---|
| Auth credential | Single bot token (static) | Bot token + signing secret (static) | App ID + App Secret → tenant_access_token (dynamic, 2h) | AppKey + AppSecret → accessToken (dynamic, 2h) |
| API auth header | Token in URL | `Authorization: Bearer {bot_token}` | `Authorization: Bearer {tenant_access_token}` | `x-acs-dingtalk-access-token: {accessToken}` |
| Token refresh | None needed | None needed | Required every 2 hours | Required every 2 hours |

### 3.2 Webhook Event Subscription

DingTalk supports two modes for receiving events:
1. **HTTP Callback (Webhook)** — same model as Telegram/Slack/Feishu (used in this design)
2. **Stream Mode** — persistent TLS connection (not suitable for Lambda)

#### Registering the Webhook URL

In the DingTalk developer console (open.dingtalk.com):
1. Go to your app → "Event Subscriptions" (事件与回调)
2. Select "HTTP Mode" (HTTP 模式) instead of "Stream Mode"
3. Set the Request URL to: `https://{api-gateway-url}/webhook/dingtalk`
4. DingTalk will send a verification request to confirm the endpoint

#### Webhook Signature Verification

DingTalk HTTP callback events include a signature in the request header for verification:

| Header | Description |
|---|---|
| `timestamp` | Unix timestamp in milliseconds |
| `sign` | HMAC-SHA256 signature |

**Signature computation:**
```python
import hmac
import hashlib
import base64

string_to_sign = f"{timestamp}\n{app_secret}"
hmac_code = hmac.new(
    app_secret.encode("utf-8"),
    string_to_sign.encode("utf-8"),
    digestmod=hashlib.sha256,
).digest()
expected_sign = base64.b64encode(hmac_code).decode("utf-8")
```

Compare `expected_sign` with the `sign` header value. Additionally validate that `timestamp` is within a reasonable window (e.g., 1 hour) to prevent replay attacks.

**Comparison with other channels:**

| Channel | Signature Algorithm | Key Material |
|---|---|---|
| Telegram | Static secret_token header | Webhook secret |
| Slack | HMAC-SHA256 over `v0:timestamp:body` | Signing secret |
| Feishu | SHA-256 over `timestamp+nonce+encrypt_key+body` | Encrypt key |
| DingTalk | HMAC-SHA256 over `timestamp\n+appSecret` | App secret |

### 3.3 Required Event Subscriptions

The bot should subscribe to the following events on the DingTalk developer console:

| Event | Description | Priority |
|---|---|---|
| Robot message callback (机器人消息回调) | New message received by robot | **Phase 1 (MVP)** |
| `chat_add_member` | Bot added to group chat | Phase 2 (optional) |
| `chat_remove_member` | Bot removed from group chat | Phase 2 (optional) |

For Phase 1 MVP, **only the robot message callback** is required. Unlike Feishu/Slack which use event subscription APIs, DingTalk's robot message callback is configured separately in the "Robot" (机器人) section of the developer console.

### 3.4 Required Bot Permissions

Configure the following permissions (scopes) in the DingTalk developer console:

| Permission | Scope ID | Purpose |
|---|---|---|
| Robot send messages | `Robot.Message.Send` | Send replies via OpenAPI |
| Enterprise member read | `Contact.User.Read` | Get sender display name (optional) |
| Chat info read | `Chat.Info.Read` | Identify group chat context (optional) |

> **Note:** When using `sessionWebhook` to reply (recommended for Phase 1), no additional permissions
> are needed for sending messages. The `sessionWebhook` is a pre-authorized temporary URL.

### 3.5 Group Chat Support

DingTalk bots operate in both **1-to-1 (单聊)** and **Group (群聊)** modes. The `conversationType` field distinguishes them:

| `conversationType` | Meaning | Behavior |
|---|---|---|
| `"1"` | 1-to-1 direct message | All messages are directed to the bot |
| `"2"` | Group chat | Bot only receives messages when **@mentioned** |

**Group chat considerations:**
- In group chats, the message `text.content` includes `@BotName` mention text — strip before passing to AgentCore
- The `isInAtList` field indicates whether the bot was @mentioned
- `conversationId` is the group conversation ID, not the sender's 1-to-1 chat
- `senderId` / `senderStaffId` still identifies the individual user for identity resolution
- For identity mapping, we use `dingtalk:{senderStaffId}` (employee ID) or `dingtalk:{senderId}` (DingTalk user ID)
- Replies via `sessionWebhook` go back to the **same conversation** (works for both 1-to-1 and group)

**Phase 1 recommendation:** Support both 1-to-1 and group chat from the start (same as Feishu). The `sessionWebhook` reply mechanism handles both cases transparently.

### 3.6 DingTalk App Lifecycle

**Important:** A DingTalk app must go through several steps before it's usable:

Steps on the DingTalk developer console (open.dingtalk.com):
1. **Create Application** (创建应用) — enterprise self-built app (企业内部应用)
2. **Add Robot capability** (添加机器人能力) — configure robot name, avatar
3. **Configure Permissions** (权限管理) — see Section 3.4
4. **Configure Message Reception**:
   - Option A: HTTP Mode — set Request URL to webhook endpoint
   - Option B: Stream Mode — not used in this design
5. **Publish / Release** (发布) — make the bot available to all org members
   - During development, the bot is accessible by the developer and test users
   - After publishing, all users in the organization can find and use the bot

### 3.7 Event Payload Structure (Robot Message Callback)

When a user sends a message to the bot (1-to-1 or @mention in group), DingTalk POSTs the following JSON to the webhook URL:

```json
{
    "msgId": "msg_unique_id",
    "msgtype": "text",
    "text": {
        "content": "Hello bot"
    },
    "senderId": "dingtalk_user_id",
    "senderStaffId": "employee_id_in_org",
    "senderNick": "User Display Name",
    "senderCorpId": "corp_id",
    "conversationId": "cidXXXXXXXXXX",
    "conversationType": "1",
    "chatbotUserId": "chatbot_dingtalk_id",
    "robotCode": "dingXXXXXXXX",
    "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=xxx",
    "sessionWebhookExpiredTime": 1773645636000,
    "createAt": 1773645036000,
    "isInAtList": false,
    "atUsers": [],
    "isAdmin": false
}
```

**Key fields mapping:**

| Our concept | DingTalk field | Notes |
|---|---|---|
| Channel user ID | `senderStaffId` or `senderId` | `senderStaffId` is org-scoped employee ID; `senderId` is DingTalk-wide user ID |
| Chat ID (for replies) | `conversationId` | Used for OpenAPI replies; `sessionWebhook` is simpler |
| Chat type | `conversationType` | `"1"` (1-to-1) or `"2"` (group) |
| Message text | `text.content` | Plain text content |
| Message type | `msgtype` | `text`, `picture`, `richText`, `audio`, `video`, `file` |
| Event dedup ID | `msgId` | For idempotency |
| Reply webhook | `sessionWebhook` | Temporary URL for direct reply (expires at `sessionWebhookExpiredTime`) |
| @mentions (group) | `atUsers` + `isInAtList` | Array of `{dingtalkId}` — strip from text |
| Robot code | `robotCode` | Robot identifier, usually same as `appKey` |

**Image message payload:**
```json
{
    "msgtype": "picture",
    "content": {
        "downloadCode": "XXXXX",
        "pictureDownloadUrl": "https://..."
    },
    "senderId": "...",
    ...
}
```

### 3.8 Sending Messages — Two Approaches

#### Approach A: sessionWebhook (Recommended for Phase 1)

Every incoming message includes a `sessionWebhook` URL — a temporary, pre-authenticated webhook that allows direct reply **without an access token**:

```
POST {sessionWebhook}
Content-Type: application/json

{
    "msgtype": "text",
    "text": {
        "content": "Hello from bot"
    }
}
```

**Advantages:**
- No access_token needed — eliminates the token cache/refresh complexity
- Works for both 1-to-1 and group chats automatically
- Simpler implementation than Feishu/Slack reply flows

**Limitations:**
- Expires after a period (typically ~1 hour, check `sessionWebhookExpiredTime`)
- Only available as a reply to an incoming message — cannot send proactive messages
- Not suitable for cron Lambda (no incoming message context)

**Markdown reply:**
```json
{
    "msgtype": "markdown",
    "markdown": {
        "title": "Bot Reply",
        "text": "## Hello\n\nThis is **markdown** content"
    }
}
```

DingTalk natively supports Markdown in bot messages — no conversion needed (unlike Telegram's HTML or Feishu's post format).

#### Approach B: Robot OpenAPI (Required for Cron Lambda)

For proactive messages (cron jobs, notifications), use the Robot Send API with access_token:

```
POST https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend
x-acs-dingtalk-access-token: {accessToken}
Content-Type: application/json

{
    "robotCode": "dingXXXXXXXX",
    "userIds": ["senderStaffId"],
    "msgKey": "sampleText",
    "msgParam": "{\"content\":\"Hello from cron\"}"
}
```

For group messages:
```
POST https://api.dingtalk.com/v1.0/robot/groupMessages/send
x-acs-dingtalk-access-token: {accessToken}
Content-Type: application/json

{
    "robotCode": "dingXXXXXXXX",
    "openConversationId": "cidXXXXXXXXXX",
    "msgKey": "sampleText",
    "msgParam": "{\"content\":\"Hello from cron\"}"
}
```

**Message types (`msgKey`):**

| msgKey | Description | msgParam format |
|---|---|---|
| `sampleText` | Plain text | `{"content": "text"}` |
| `sampleMarkdown` | Markdown | `{"title": "title", "text": "## markdown"}` |
| `sampleImageMsg` | Image | `{"photoURL": "https://..."}` |

### 3.9 Image Upload Support (Phase 2)

DingTalk image handling differs from Telegram/Slack/Feishu:

1. **Receiving images:** The message has `msgtype: "picture"` with `content.downloadCode` or `content.pictureDownloadUrl`
2. **Download image:** Use the `pictureDownloadUrl` directly (may require access_token as query param) or use the Robot file download API:
   ```
   POST https://api.dingtalk.com/v1.0/robot/messageFiles/download
   x-acs-dingtalk-access-token: {accessToken}

   {
       "downloadCode": "XXXXX",
       "robotCode": "dingXXXXXXXX"
   }
   ```
3. **Upload to S3:** Same as Telegram/Slack/Feishu — `{namespace}/_uploads/img_{ts}_{hex}.{ext}`
4. **Pass to AgentCore:** Structured message with `images[{s3Key, contentType}]` (existing flow)

**Phase 1:** Text-only. Image support deferred to Phase 2 pending download API confirmation.

### 3.10 Rate Limits

| API | Rate Limit |
|---|---|
| Send 1-to-1 message (batchSend) | 20 messages/second per app, 200/min per user |
| Send group message | 20 messages/second per app |
| Get access_token | 100/min |
| sessionWebhook reply | ~20 messages/min per session |

These are well within our usage patterns (per-user bot, not bulk messaging).

---

## 4. Proposed Implementation

### 4.1 Secrets Manager Secret Format

Store in `openclaw/channels/dingtalk` as JSON:

```json
{
    "appKey": "dingXXXXXXXX",
    "appSecret": "XXXXXXXXXXXXXXXX",
    "robotCode": "dingXXXXXXXX"
}
```

> `robotCode` is typically the same as `appKey`, but stored separately for clarity.

### 4.2 Router Lambda Changes

#### 4.2.1 New Environment Variable

```python
DINGTALK_TOKEN_SECRET_ID = os.environ.get("DINGTALK_TOKEN_SECRET_ID", "")
```

#### 4.2.2 DingTalk Credentials Helper

```python
def _get_dingtalk_credentials():
    """Return (app_key, app_secret, robot_code) from DingTalk secret."""
    raw = _get_secret(DINGTALK_TOKEN_SECRET_ID)
    if not raw:
        return "", "", ""
    try:
        data = json.loads(raw)
        return (
            data.get("appKey", ""),
            data.get("appSecret", ""),
            data.get("robotCode", data.get("appKey", "")),
        )
    except (json.JSONDecodeError, TypeError):
        return "", "", ""
```

#### 4.2.3 Access Token Cache (for Cron/OpenAPI only)

```python
_dingtalk_token_cache = {"token": "", "expires_at": 0}

def _get_dingtalk_access_token():
    """Get or refresh DingTalk access_token (2h TTL, refresh 5 min early)."""
    if _dingtalk_token_cache["token"] and time.time() < _dingtalk_token_cache["expires_at"] - 300:
        return _dingtalk_token_cache["token"]

    app_key, app_secret, _ = _get_dingtalk_credentials()
    if not app_key or not app_secret:
        logger.error("DingTalk appKey/appSecret not configured")
        return ""

    url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
    data = json.dumps({"appKey": app_key, "appSecret": app_secret}).encode()
    req = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"})

    try:
        resp = urllib_request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        token = result.get("accessToken", "")
        expire = result.get("expireIn", 7200)
        if token:
            _dingtalk_token_cache["token"] = token
            _dingtalk_token_cache["expires_at"] = time.time() + expire
            return token
        logger.error("DingTalk token error: %s", result)
    except Exception as e:
        logger.error("Failed to get DingTalk access_token: %s", e)
    return ""
```

#### 4.2.4 Webhook Validation

```python
def validate_dingtalk_webhook(headers, body_bytes):
    """Validate DingTalk webhook using HMAC-SHA256 signature.

    Signature = Base64(HMAC-SHA256(appSecret, timestamp + "\n" + appSecret))
    Returns False (fail-closed) if appSecret is not configured.
    """
    _, app_secret, _ = _get_dingtalk_credentials()
    if not app_secret:
        logger.error("DingTalk appSecret not configured — rejecting request (fail-closed)")
        return False

    timestamp = headers.get("timestamp", "")
    sign = headers.get("sign", "")

    if not timestamp or not sign:
        logger.warning("DingTalk webhook missing signature headers")
        return False

    # Validate timestamp is within 1 hour to prevent replay attacks
    try:
        ts_ms = int(timestamp)
        if abs(time.time() * 1000 - ts_ms) > 3600000:
            logger.warning("DingTalk webhook timestamp too old: %s", timestamp)
            return False
    except ValueError:
        logger.warning("DingTalk webhook invalid timestamp: %s", timestamp)
        return False

    # Compute expected signature
    string_to_sign = f"{timestamp}\n{app_secret}"
    hmac_code = hmac.new(
        app_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    expected_sign = base64.b64encode(hmac_code).decode("utf-8")

    if not hmac.compare_digest(expected_sign, sign):
        logger.warning("DingTalk webhook signature mismatch")
        return False

    return True
```

#### 4.2.5 Message Extraction

```python
def _handle_dingtalk_webhook(body, headers, body_bytes):
    """Handle DingTalk robot message callback."""

    # Validate signature
    if not validate_dingtalk_webhook(headers, body_bytes):
        return {"statusCode": 401, "body": "Unauthorized"}

    # Extract message fields
    msg_type = body.get("msgtype", "")
    sender_staff_id = body.get("senderStaffId", "")
    sender_id = body.get("senderId", "")
    conversation_id = body.get("conversationId", "")
    conversation_type = body.get("conversationType", "")
    session_webhook = body.get("sessionWebhook", "")
    msg_id = body.get("msgId", "")
    robot_code = body.get("robotCode", "")

    # Use senderStaffId (org-scoped) if available, fallback to senderId
    channel_user_id = sender_staff_id or sender_id
    if not channel_user_id:
        logger.warning("DingTalk message with no sender ID")
        return {"statusCode": 200, "body": "OK"}

    # Extract text
    text = ""
    if msg_type == "text":
        text_obj = body.get("text", {})
        text = text_obj.get("content", "").strip() if isinstance(text_obj, dict) else ""
    elif msg_type == "richText":
        # Extract text from richText content
        rich_text = body.get("content", {}).get("richText", [])
        text_parts = []
        for section in rich_text:
            for item in section.get("text", []):
                if "text" in item:
                    text_parts.append(item["text"])
        text = " ".join(text_parts).strip()
    elif msg_type == "picture":
        text = ""  # Image-only — Phase 2
    else:
        text = str(body.get("text", {}).get("content", ""))

    # Group chat: strip @bot mention from text
    if conversation_type == "2":
        # DingTalk prepends @BotName to the text content in group chats
        # The bot's nick may appear at the start of the text
        at_users = body.get("atUsers", [])
        # Also strip any leading/trailing whitespace and @mentions
        text = text.strip()
        if not text:
            text = "hi"  # Default prompt when only @mentioned with no text

    if not text and msg_type == "text":
        return {"statusCode": 200, "body": "OK"}

    return {
        "channel": "dingtalk",
        "channel_user_id": channel_user_id,
        "chat_id": conversation_id,
        "text": text,
        "event_id": msg_id,
        "session_webhook": session_webhook,
    }
```

#### 4.2.6 Progress Notification

```python
def _dingtalk_progress_notify(session_webhook, stop_event, notify_after_s=30):
    """Send a one-time progress message after waiting notify_after_s seconds.

    Same pattern as Slack/Feishu progress notify.
    """
    if stop_event.wait(notify_after_s):
        return  # AgentCore responded before timeout
    _send_dingtalk_via_session_webhook(session_webhook, "Working on your request...")
```

#### 4.2.7 Send Response via sessionWebhook

```python
def _send_dingtalk_via_session_webhook(session_webhook, text):
    """Send a message via DingTalk sessionWebhook (no access_token needed)."""
    if not session_webhook:
        logger.error("No DingTalk sessionWebhook available")
        return

    MAX_DINGTALK_TEXT_LEN = 20000  # Conservative limit

    chunks = (
        [text[i : i + MAX_DINGTALK_TEXT_LEN] for i in range(0, len(text), MAX_DINGTALK_TEXT_LEN)]
        if len(text) > MAX_DINGTALK_TEXT_LEN
        else [text]
    )

    for chunk in chunks:
        # Use markdown for rich formatting support
        payload = json.dumps({
            "msgtype": "markdown",
            "markdown": {
                "title": "Reply",
                "text": chunk,
            },
        }).encode()

        req = urllib_request.Request(
            session_webhook,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib_request.urlopen(req, timeout=10)
        except Exception as e:
            logger.error("Failed to send DingTalk message via sessionWebhook: %s", e)
```

#### 4.2.8 Send Response via OpenAPI (for Cron Lambda)

```python
def send_dingtalk_message(user_id, text, conversation_id=None):
    """Send a message via DingTalk Robot OpenAPI (requires access_token).

    Used by cron Lambda for proactive messages.
    For reply to incoming messages, prefer sessionWebhook (no token needed).
    """
    token = _get_dingtalk_access_token()
    if not token:
        logger.error("No DingTalk access_token available")
        return

    _, _, robot_code = _get_dingtalk_credentials()
    if not robot_code:
        logger.error("DingTalk robotCode not configured")
        return

    MAX_DINGTALK_TEXT_LEN = 20000

    chunks = (
        [text[i : i + MAX_DINGTALK_TEXT_LEN] for i in range(0, len(text), MAX_DINGTALK_TEXT_LEN)]
        if len(text) > MAX_DINGTALK_TEXT_LEN
        else [text]
    )

    for chunk in chunks:
        if conversation_id:
            # Group message
            url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            payload = json.dumps({
                "robotCode": robot_code,
                "openConversationId": conversation_id,
                "msgKey": "sampleMarkdown",
                "msgParam": json.dumps({"title": "Reply", "text": chunk}),
            }).encode()
        else:
            # 1-to-1 message
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            payload = json.dumps({
                "robotCode": robot_code,
                "userIds": [user_id],
                "msgKey": "sampleMarkdown",
                "msgParam": json.dumps({"title": "Reply", "text": chunk}),
            }).encode()

        req = urllib_request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-acs-dingtalk-access-token": token,
            },
        )
        try:
            urllib_request.urlopen(req, timeout=10)
        except Exception as e:
            logger.error("Failed to send DingTalk message to %s: %s", user_id, e)
```

### 4.3 CDK Infrastructure Changes

#### 4.3.1 security_stack.py

Add `"dingtalk"` to the channel list:

```python
channel_names = ["whatsapp", "telegram", "discord", "slack", "feishu", "dingtalk"]
```

This creates `openclaw/channels/dingtalk` in Secrets Manager.

#### 4.3.2 router_stack.py

1. **Add API Gateway route:**
```python
self.http_api.add_routes(
    path="/webhook/dingtalk",
    methods=[apigwv2.HttpMethod.POST],
    integration=lambda_integration,
)
```

2. **Add Lambda environment variable:**
```python
"DINGTALK_TOKEN_SECRET_ID": dingtalk_token_secret_name,
```

3. **Update constructor parameters** to accept `dingtalk_token_secret_name`.

#### 4.3.3 app.py

Pass `dingtalk_token_secret_name` from SecurityStack to RouterStack.

#### 4.3.4 cron_stack.py

Add `DINGTALK_TOKEN_SECRET_ID` env var to cron executor Lambda.

### 4.4 Cron Lambda Changes

Add DingTalk message delivery to `deliver_response()`:

```python
def deliver_response(channel, channel_target, response_text):
    response_text = _extract_text_from_content_blocks(response_text)

    if channel == "telegram":
        # ... existing ...
    elif channel == "slack":
        # ... existing ...
    elif channel == "feishu":
        # ... existing ...
    elif channel == "dingtalk":
        send_dingtalk_message(channel_target, response_text)
    else:
        logger.warning("Unknown channel type: %s", channel)
```

The cron Lambda needs the `_get_dingtalk_access_token()` helper and `send_dingtalk_message()` function.

### 4.5 Setup Script

Create `scripts/setup-dingtalk.sh`:

```bash
#!/bin/bash
# Set up DingTalk Robot event subscription and add deployer to allowlist.
#
# Prerequisites:
#   - DingTalk app created at https://open.dingtalk.com
#   - CDK stacks deployed (OpenClawRouter, OpenClawSecurity)

set -euo pipefail
REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-us-west-2}}"
TABLE_NAME="${IDENTITY_TABLE_NAME:-openclaw-identity}"

echo "=== OpenClaw DingTalk Setup ==="

# Step 1: Display webhook URL
API_URL=$(aws cloudformation describe-stacks \
    --stack-name OpenClawRouter \
    --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
    --output text --region "$REGION")
WEBHOOK_URL="${API_URL}webhook/dingtalk"

echo "Your DingTalk webhook URL is:"
echo "  $WEBHOOK_URL"
echo ""
echo "Configure in DingTalk Developer Console (https://open.dingtalk.com):"
echo ""
echo "  Step A: Create & configure the app (if not done yet)"
echo "    1. Create an enterprise self-built app (企业内部应用)"
echo "    2. Add Robot capability (添加机器人能力)"
echo "    3. Permissions (权限管理) -> add scope: Robot.Message.Send"
echo ""
echo "  Step B: Configure message reception"
echo "    1. Robot config (机器人配置) -> Message reception mode (消息接收模式)"
echo "    2. Select HTTP Mode (HTTP 模式)"
echo "    3. Set Request URL to:"
echo "       $WEBHOOK_URL"
echo ""
echo "  Step C: Publish the app (发布应用)"
echo "    - During dev, only the creator can test"
echo "    - After publishing, all org members can use the bot"
echo ""
read -rp "Press Enter once you've completed the above steps..."
echo ""

# Step 2: Store credentials
read -rp "Enter your DingTalk AppKey (clientId): " APP_KEY
read -rp "Enter your DingTalk AppSecret (clientSecret): " APP_SECRET
read -rp "Enter your Robot Code (usually same as AppKey, press Enter to use AppKey): " ROBOT_CODE
ROBOT_CODE="${ROBOT_CODE:-$APP_KEY}"

aws secretsmanager update-secret \
    --secret-id openclaw/channels/dingtalk \
    --secret-string "{\"appKey\":\"${APP_KEY}\",\"appSecret\":\"${APP_SECRET}\",\"robotCode\":\"${ROBOT_CODE}\"}" \
    --region "$REGION"

# Step 3: Add to allowlist
echo ""
echo "To find your DingTalk staffId, message the bot — the rejection reply will show your ID."
read -rp "Enter your DingTalk staffId (e.g. manager1234): " DINGTALK_USER_ID
CHANNEL_KEY="dingtalk:${DINGTALK_USER_ID}"
NOW_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

aws dynamodb put-item \
    --table-name "$TABLE_NAME" \
    --region "$REGION" \
    --item "{
        \"PK\": {\"S\": \"ALLOW#${CHANNEL_KEY}\"},
        \"SK\": {\"S\": \"ALLOW\"},
        \"channelKey\": {\"S\": \"${CHANNEL_KEY}\"},
        \"addedAt\": {\"S\": \"${NOW_ISO}\"}
    }"

echo ""
echo "=== Setup complete ==="
echo "  Webhook URL: $WEBHOOK_URL"
echo "  Allowlisted: $CHANNEL_KEY"
```

### 4.6 Router Lambda Handler Wiring

In the main `handler()` function, add DingTalk routing:

```python
# Sync path (return 200 immediately, self-invoke async)
elif path == "/webhook/dingtalk":
    # Validate signature
    if not validate_dingtalk_webhook(headers, body_bytes):
        return {"statusCode": 401, "body": "Unauthorized"}
    # Self-invoke async
    _self_invoke_async(event)
    return {"statusCode": 200, "body": "OK"}

# Async path (actual message processing)
elif path == "/webhook/dingtalk" and is_async:
    result = _handle_dingtalk_webhook(body, headers, body_bytes)
    if isinstance(result, dict) and "statusCode" in result:
        return result
    handle_dingtalk(result)
```

### 4.7 DingTalk vs Lark Interoperability Domain

DingTalk has only one API domain:

| Version | Domain |
|---|---|
| DingTalk (China) | `https://api.dingtalk.com` |
| DingTalk (International) | `https://api.dingtalk.com` (same) |
| Legacy OApi | `https://oapi.dingtalk.com` (sessionWebhook uses this) |

No domain switching needed (unlike Feishu/Lark).

---

## 5. DynamoDB Identity Mapping

DingTalk users follow the same pattern as Telegram/Slack/Feishu:

| PK | SK | Example |
|---|---|---|
| `CHANNEL#dingtalk:staffId123` | `PROFILE` | Channel→user lookup |
| `USER#user_abc123` | `CHANNEL#dingtalk:staffId123` | User's bound DingTalk channel |
| `ALLOW#dingtalk:staffId123` | `ALLOW` | DingTalk user allowlist entry |

Cross-channel binding works identically — a DingTalk user can generate a bind code and link to their Telegram/Slack/Feishu identity.

---

## 6. Architecture Diagram (Updated)

```
                                                    +-----------------------------------------+
                                                    |             End Users                    |
                                                    | Telegram  Slack  Feishu  DingTalk        |
                                                    +--+---------+------+--------+------------+
                                                       |         |      |        |
                                            (webhook HTTPS over internet)
                                                       |         |      |        |
+------------------------------------------------------+---------+------+--------+-------------+
|  AWS Account                                                                                  |
|                                                                                               |
|  +----------------------------------------------+                                            |
|  |  API Gateway HTTP API                        |                                            |
|  |  (openclaw-router)                           |                                            |
|  |                                              |                                            |
|  |  POST /webhook/telegram  --> Lambda          |                                            |
|  |  POST /webhook/slack     --> Lambda          |                                            |
|  |  POST /webhook/feishu    --> Lambda          |                                            |
|  |  POST /webhook/dingtalk  --> Lambda          |                                            |
|  |  GET  /health            --> Lambda          |                                            |
|  +----------------------+-----------------------+                                            |
|                         |                                                                     |
|  +----------------------v-----------------------+                                            |
|  |  Router Lambda (openclaw-router)             |                                            |
|  |                                              |                                            |
|  |  1. Validate webhook:                        |                                            |
|  |     - Telegram: X-Telegram-Bot-Api-Secret-   |                                            |
|  |       Token header                           |                                            |
|  |     - Slack: X-Slack-Signature HMAC-SHA256   |                                            |
|  |     - Feishu: X-Lark-Signature SHA-256       |                                            |
|  |     - DingTalk: HMAC-SHA256(timestamp+secret)|                                            |
|  |  2. Self-invoke async                        |                                            |
|  |  3. Resolve user in DynamoDB                 |                                            |
|  |  4. Get/create AgentCore session             |                                            |
|  |  5. InvokeAgentRuntime                       |                                            |
|  |  6. Send response back to channel:           |                                            |
|  |     - DingTalk: sessionWebhook (no token)    |                                            |
|  |       or OpenAPI (with access_token)         |                                            |
|  +----------------------------------------------+                                            |
```

---

## 7. Key Differences from Telegram/Slack/Feishu

| Aspect | Impact | Mitigation |
|---|---|---|
| **sessionWebhook reply** | Simplifies reply flow — no access_token needed for replies | Use sessionWebhook in Router Lambda; use OpenAPI only in Cron Lambda |
| **Dynamic access_token** (2h TTL) | Same as Feishu — must cache and refresh | In-memory cache with early refresh (5 min before expiry). Only needed for Cron Lambda |
| **No URL verification challenge** | Unlike Slack/Feishu, DingTalk doesn't send a challenge request | One less handler to implement |
| **No payload encryption** | Unlike Feishu's optional AES-256-CBC encryption, DingTalk uses HMAC signature only | Simpler implementation, no crypto dependency |
| **Sender ID is `senderStaffId`** | Org-scoped employee ID (not DingTalk-wide user ID) | Map as `dingtalk:{senderStaffId}` in DynamoDB; fallback to `senderId` if staff ID is empty |
| **Native Markdown support** | DingTalk bot messages support Markdown natively | No Markdown-to-HTML or Markdown-to-post conversion needed (unlike Telegram/Feishu) |
| **Official SDK focus on Stream** | HTTP Callback mode documentation is sparse | Implementation based on SDK source code analysis + community references; monitor for deprecation |
| **robotCode required for OpenAPI** | Robot identifier needed for proactive messages | Store in Secrets Manager alongside appKey/appSecret |
| **Group chat @mention** | Text content includes `@BotName` prefix in group chats | Strip mention text before passing to AgentCore |
| **sessionWebhook expiry** | Webhook URL expires (~1 hour) | Only use for immediate reply; cron Lambda uses OpenAPI |

---

## 8. DingTalk vs Feishu — Implementation Comparison

Since both are Chinese enterprise IM platforms with similar API patterns, here's a direct comparison:

| Feature | Feishu | DingTalk | Complexity |
|---|---|---|---|
| **Credentials** | appId + appSecret + verificationToken + encryptKey | appKey + appSecret + robotCode | DingTalk simpler (3 vs 4 fields) |
| **Token API** | `POST /open-apis/auth/v3/tenant_access_token/internal` | `POST /v1.0/oauth2/accessToken` | Same complexity |
| **Token TTL** | 2 hours | 2 hours | Same |
| **Webhook signature** | SHA-256(timestamp+nonce+encryptKey+body) | HMAC-SHA256(appSecret, timestamp+"\n"+appSecret) | DingTalk simpler (no nonce, no body) |
| **Payload encryption** | Optional AES-256-CBC | None | DingTalk simpler |
| **URL verification** | Yes (challenge/response) | No | DingTalk simpler |
| **Reply mechanism** | Send API with tenant_access_token | sessionWebhook (no token) or OpenAPI | DingTalk simpler for replies |
| **Message format** | JSON-encoded string in `content` field | Direct `text.content` string | DingTalk simpler |
| **Group @mention** | `mentions` array with `key` tags | `atUsers` array + text prefix | Similar |
| **Markdown** | Custom "post" rich text format | Native Markdown | DingTalk simpler |
| **Image download** | `/open-apis/im/v1/images/{image_key}` | `/v1.0/robot/messageFiles/download` | Similar |
| **Feishu/Lark domain** | Two domains (feishu.cn / larksuite.com) | One domain (api.dingtalk.com) | DingTalk simpler |

**Summary: DingTalk integration is simpler than Feishu** in almost every dimension. The `sessionWebhook` reply mechanism and native Markdown support are significant simplifications.

---

## 9. Implementation Phases

### Phase 1: Text Messaging (MVP)

**Scope:**
- CDK: Add `dingtalk` secret, API Gateway route, Lambda env vars
- Router Lambda: DingTalk webhook validation, text message extraction, sessionWebhook reply
- Group chat: 1-to-1 and @mention in group both supported
- Cron Lambda: DingTalk message delivery via OpenAPI (with access_token)
- Setup script: `scripts/setup-dingtalk.sh`
- Tests: Unit tests for webhook validation, message extraction, response sending

**Not in scope:** Image upload, rich text (beyond markdown), file attachments.

**Estimated changes:**

| File | Change Type | Size |
|---|---|---|
| `stacks/security_stack.py` | Add `"dingtalk"` to channel list | 1 line |
| `stacks/router_stack.py` | Add route + env var + constructor param | ~20 lines |
| `app.py` | Pass dingtalk secret name | ~3 lines |
| `stacks/cron_stack.py` | Add env var | ~2 lines |
| `lambda/router/index.py` | DingTalk handlers (validation, extract, sessionWebhook reply, OpenAPI send, token cache) | ~180 lines |
| `lambda/cron/index.py` | DingTalk delivery | ~40 lines |
| `scripts/setup-dingtalk.sh` | New file | ~60 lines |
| `lambda/router/test_dingtalk.py` | Unit tests | ~150 lines |
| `docs/architecture.md` | Update diagrams | ~10 lines |
| `CLAUDE.md` | Update channel list, add DingTalk references | ~20 lines |

**Total: ~490 lines** (slightly less than Feishu due to simpler reply mechanism)

### Phase 2: Image Support + Proactive Messages

- Image download via Robot file download API → S3 → Bedrock multimodal
- Proactive message sending (not just reply) via OpenAPI

### Phase 3: Rich Features

- Interactive card messages (DingTalk card templates)
- Message update/recall
- Typing indicator (DingTalk doesn't have a native typing API)

---

## 10. Testing Plan

### Unit Tests (`lambda/router/test_dingtalk.py`)

```python
# Test cases:
# 1. validate_dingtalk_webhook — valid signature → True
# 2. validate_dingtalk_webhook — invalid signature → False
# 3. validate_dingtalk_webhook — missing headers → False
# 4. validate_dingtalk_webhook — expired timestamp → False
# 5. _handle_dingtalk_webhook — text message (1-to-1) → extracts sender, text, sessionWebhook
# 6. _handle_dingtalk_webhook — text message (group, @mention) → strips @mention, extracts text
# 7. _handle_dingtalk_webhook — non-text message (picture) → returns empty text (Phase 2)
# 8. _send_dingtalk_via_session_webhook — success → POST to sessionWebhook
# 9. _send_dingtalk_via_session_webhook — long text → splits into chunks
# 10. _get_dingtalk_access_token — token cached → returns cached
# 11. _get_dingtalk_access_token — token expired → fetches new
# 12. send_dingtalk_message — 1-to-1 → calls batchSend API
# 13. send_dingtalk_message — group → calls groupMessages/send API
# 14. send_dingtalk_message — API error → logs and continues
```

### E2E Tests

Extend `tests/e2e/bot_test.py` with DingTalk webhook simulation (similar to existing Telegram/Feishu tests).

---

## 11. Security Considerations

| Concern | Mitigation |
|---|---|
| DingTalk credentials exposure | Stored in Secrets Manager with KMS CMK encryption |
| Webhook replay attacks | HMAC-SHA256 signature includes timestamp; validated within 1-hour window |
| Token leakage | `access_token` cached in Lambda memory only, 2h TTL, never logged |
| sessionWebhook abuse | URLs are temporary (~1h expiry), generated per-message by DingTalk, not stored |
| Cross-org access | `senderStaffId` is org-scoped; `senderCorpId` can be validated against expected corp |
| Proactive message abuse | OpenAPI requires valid access_token; rate limited by DingTalk (20 msg/s) |

---

## 12. Future Alternative: Stream Mode

If DingTalk deprecates HTTP Callback mode or a customer requires Stream Mode (e.g., no public URL requirement), the architecture would change:

```
+---------------------------+
|  ECS Fargate Task         |  <- Long-running process (~$3/month)
|  dingtalk-stream-connector|
|                           |
|  1. TLS connect to        |
|     DingTalk Stream API   |
|  2. Receive messages      |
|  3. Forward to Router     |
|     Lambda (internal      |
|     invoke or HTTP)       |
+---------------------------+
            |
            v
  Router Lambda (existing)
  -> Same user resolution
  -> Same AgentCore invocation
  -> Reply via OpenAPI (not sessionWebhook)
```

**Stream Mode connection flow** (from SDK analysis):
1. `POST https://api.dingtalk.com/v1.0/gateway/connections/open` with `clientId` + `clientSecret`
2. Response: `{endpoint: "wss://...", ticket: "..."}`
3. Connect WebSocket: `wss://{endpoint}?ticket={ticket}`
4. Receive messages as JSON frames with `headers` + `data`
5. Send `AckMessage` to confirm processing

**Decision criteria for switching to Stream Mode:**
- DingTalk officially deprecates HTTP Callback
- Customer cannot expose a public HTTPS endpoint
- Need for real-time typing indicators or presence events

---

## 13. References

### Official Documentation (DingTalk Open Platform)
- **Developer Console**: https://open.dingtalk.com
- **Server API Overview**: https://open.dingtalk.com/document/orgapp/overview-of-server-api
- **Robot Overview**: https://open.dingtalk.com/document/orgapp/the-creation-and-installation-of-the-application-robot-in-the
- **Robot Message Reception**: https://open.dingtalk.com/document/orgapp/receive-message
- **Stream Mode Introduction**: https://open.dingtalk.com/document/orgapp/introduction-to-stream-mode
- **Stream Mode Protocol**: https://open.dingtalk.com/document/direction/stream-mode-protocol-access-description
- **Event Subscription Configuration**: https://open.dingtalk.com/document/orgapp/configure-event-subcription

### Official SDKs (Source Code — used for API analysis in this document)
- **DingTalk SDK (multi-language)**: https://github.com/aliyun/dingtalk-sdk
  - Python Robot API: `dingtalk/python/alibabacloud_dingtalk/robot_1_0/client.py` — send/recall/query message endpoints
  - Python OAuth2 API: `dingtalk/python/alibabacloud_dingtalk/oauth2_1_0/models.py` — `GetAccessTokenRequest(app_key, app_secret)` → `accessToken` + `expireIn`
  - Python IM API: `dingtalk/python/alibabacloud_dingtalk/im_1_0/client.py` — group management, robot-to-conversation
- **DingTalk Stream SDK (Python)**: https://github.com/open-dingtalk/dingtalk-stream-sdk-python
  - `dingtalk_stream/chatbot.py` — `ChatbotMessage` class (payload field mapping), `reply_text()`/`reply_markdown()` via sessionWebhook
  - `dingtalk_stream/frames.py` — Stream message frame structure (EventMessage, CallbackMessage, AckMessage)
  - `dingtalk_stream/stream.py` — Stream connection flow (`/v1.0/gateway/connections/open` → WebSocket endpoint + ticket)
  - `dingtalk_stream/credential.py` — `Credential(client_id, client_secret)`

### Official Tutorials
- **DingTalk Tutorial (Python)**: https://github.com/open-dingtalk/dingtalk-tutorial-python
  - `bot_echo_text/echo_text.py` — Stream mode bot example (ChatbotHandler + DingTalkStreamClient)
  - `event_chat_update/event_handler.py` — Event subscription example

### Community / Developer Resources
- **DingTalk Developer Pedia**: https://opensource.dingtalk.com/developerpedia/docs/explore/tutorials/stream/overview
- **DingTalk Open Source**: https://github.com/open-dingtalk

### Key API Endpoints (extracted from SDK source)

| API | Method | Endpoint | Auth |
|---|---|---|---|
| Get access_token | POST | `https://api.dingtalk.com/v1.0/oauth2/accessToken` | appKey + appSecret in body |
| Send 1-to-1 (batch) | POST | `https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend` | `x-acs-dingtalk-access-token` header |
| Send group message | POST | `https://api.dingtalk.com/v1.0/robot/groupMessages/send` | `x-acs-dingtalk-access-token` header |
| Send private chat | POST | `https://api.dingtalk.com/v1.0/robot/privateChatMessages/send` | `x-acs-dingtalk-access-token` header |
| Query read status | GET | `https://api.dingtalk.com/v1.0/robot/oToMessages/readStatus` | `x-acs-dingtalk-access-token` header |
| Recall messages | POST | `https://api.dingtalk.com/v1.0/robot/otoMessages/batchRecall` | `x-acs-dingtalk-access-token` header |
| Download file | POST | `https://api.dingtalk.com/v1.0/robot/messageFiles/download` | `x-acs-dingtalk-access-token` header |
| Reply via session | POST | `https://oapi.dingtalk.com/robot/sendBySession?session=xxx` | None (pre-authorized URL) |
| Open Stream connection | POST | `https://api.dingtalk.com/v1.0/gateway/connections/open` | clientId + clientSecret in body |

### Existing Project References
- **Feishu channel design** (same architecture pattern): `docs/design-feishu-channel.md`
- **Adding a New Channel checklist**: `CLAUDE.md` → "Adding a New Channel" section
- **Router Lambda (current channels)**: `lambda/router/index.py`
- **Feishu tests (reference for DingTalk tests)**: `lambda/router/test_feishu.py`

---

## 14. Open Questions

1. **senderStaffId vs senderId:** Which should be the primary identity key? `senderStaffId` is org-scoped (more stable), but may be empty for external users. `senderId` is DingTalk-wide. Current recommendation: prefer `senderStaffId`, fallback to `senderId`.

2. **sessionWebhook reliability:** The sessionWebhook URL is undocumented in terms of exact expiry time and rate limits. Need to test in practice and have OpenAPI as fallback.

3. **HTTP Callback deprecation risk:** DingTalk's official documentation strongly favors Stream Mode. While HTTP Callback is still functional, we should monitor for deprecation announcements. The implementation should be structured so that switching to Stream Mode (via Fargate connector) requires minimal Router Lambda changes.

4. **Cross-org bots:** DingTalk supports ISV (third-party) apps that work across organizations. This design assumes enterprise self-built apps (single org). ISV support would require `suiteTicket` + `corpAccessToken` flow — a Phase 3+ enhancement.

5. **`base64` import:** The signature verification requires `base64.b64encode()`. Verify this is already imported in the Router Lambda (it may need to be added).

6. **DingTalk International:** Does `api.dingtalk.com` work for international DingTalk instances, or is there a separate domain? Initial research suggests the same domain works globally.
