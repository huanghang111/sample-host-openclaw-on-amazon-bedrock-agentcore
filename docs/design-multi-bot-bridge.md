# Design Document: Multi-Bot WebSocket Bridge (DingTalk + Feishu)

**Status:** Implemented
**Date:** 2026-04-06
**Branch:** `feature/multi-feishu-bots`

**Official References:**
- Feishu/Lark: https://github.com/larksuite/openclaw-lark (OpenClaw official plugin, TypeScript, WebSocket + multi-account)
- DingTalk: https://github.com/DingTalk-Real-AI/dingtalk-openclaw-connector (OpenClaw official connector, Stream mode)

---

## 1. Background & Motivation

### Current State

OpenClaw on AgentCore supports 5 messaging channels via 2 architectures:

| Channel | Architecture | Connection Mode | Runtime |
|---------|-------------|-----------------|---------|
| Telegram | Router Lambda + API Gateway | Webhook (HTTP) | Serverless |
| Slack | Router Lambda + API Gateway | Webhook (HTTP) | Serverless |
| Feishu (webhook) | Router Lambda + API Gateway | Webhook (HTTP) | Serverless |
| DingTalk | WS Bridge (ECS Fargate) | WebSocket (Stream) | Long-running |
| Feishu (WebSocket) | WS Bridge (ECS Fargate) | WebSocket (lark-oapi) | Long-running |

The **WS Bridge** (`ws-bridge/`) is a unified ECS Fargate service that replaced the standalone `dingtalk-bridge/`. It manages multiple DingTalk and Feishu bot instances via WebSocket connections, with a shared core for identity resolution, AgentCore invocation, and media handling.

### Problem (solved)

1. **Feishu also supports (and officially recommends) WebSocket** — the `lark-oapi` Python SDK provides `lark.ws.Client` for long-lived connections.
2. **Multi-bot support needed** — running multiple Feishu or DingTalk bots (e.g., different bots for different teams/tenants).
3. **Code duplication** — the old DingTalk bridge duplicated ~40% of Router Lambda logic.
4. **Infrastructure duplication** — separate Fargate services per channel would duplicate ECS clusters, SGs, IAM roles.

### Non-Goals

- **Existing Feishu webhook is NOT modified.** `POST /webhook/feishu` continues to work via Router Lambda. The WS Bridge is a parallel path.
- **Telegram and Slack remain on Router Lambda.** Mature webhook APIs — no need to change.
- **OpenClaw native channel support is NOT used.** OpenClaw runs headless; all messages are bridged externally.
- **Per-bot AI personality / model selection is NOT in scope.** The bridge is a message transport layer. AI behavior belongs in AgentCore, not the bridge.

---

## 2. Architecture Overview

### 2.1 WS Bridge Architecture

```
  ┌─────────────────────────────────────────────────────┐
  │              ECS Fargate Service                     │
  │              (openclaw-ws-bridge)                    │
  │                                                     │
  │  ┌───────────────────────────────────────────────┐  │
  │  │         Shared Core (core/shared.py)          │  │
  │  │  - Identity resolution (DynamoDB)             │  │
  │  │  - AgentCore invocation + retry               │  │
  │  │  - Content block extraction                   │  │
  │  │  - S3 file upload/download                    │  │
  │  │  - Secret caching (15 min TTL)                │  │
  │  │  - Message dedup (per-bot, thread-safe)       │  │
  │  │  - Outbound file/screenshot delivery          │  │
  │  └──────────────┬────────────────────────────────┘  │
  │                 │                                    │
  │  ┌──────────────┴────────────────────────────────┐  │
  │  │          Bot Manager (manager.py)             │  │
  │  │  - Load bot configs from Secrets Manager      │  │
  │  │  - Start/stop adapters in dedicated threads   │  │
  │  │  - Exponential backoff on crash-restart       │  │
  │  │  - Per-bot health tracking                    │  │
  │  │  - Graceful shutdown (SIGTERM)                │  │
  │  └──────┬───────────────────────┬────────────────┘  │
  │         │                       │                    │
  │  ┌──────▼──────┐         ┌──────▼──────┐            │
  │  │  DingTalk   │         │   Feishu    │            │
  │  │  Adapter    │   ...   │   Adapter   │            │
  │  │  (thread)   │         │  (thread)   │            │
  │  │             │         │             │            │
  │  │ dingtalk_   │         │ lark-oapi   │            │
  │  │ stream SDK  │         │ Python SDK  │            │
  │  │             │         │ (WSClient)  │            │
  │  │ Bot A       │         │ Bot X       │            │
  │  │ Bot B       │         │ Bot Y       │            │
  │  └──────┬──────┘         └──────┬──────┘            │
  │         │  WebSocket            │  WebSocket         │
  └─────────┼───────────────────────┼────────────────────┘
            │                       │
            ▼                       ▼
     DingTalk Server          Feishu Server
```

### 2.2 Channel Architecture Overview

```
                        ┌───────────────────────┐
                        │   Messaging Channels   │
                        └───┬───────┬───────┬───┘
                            │       │       │
                 Webhook    │       │       │   WebSocket
              ┌─────────────┘       │       └──────────────┐
              │                     │                      │
              ▼                     ▼                      ▼
  ┌───────────────────┐  ┌──────────────────┐  ┌──────────────────────┐
  │  Router Lambda    │  │  Router Lambda   │  │  WS Bridge (Fargate) │
  │  + API Gateway    │  │  + API Gateway   │  │                      │
  │                   │  │                  │  │  DingTalk Bot A, B   │
  │  Telegram         │  │  Feishu webhook  │  │  Feishu Bot X, Y     │
  │  Slack            │  │  (unchanged)     │  │  [future channels]   │
  └────────┬──────────┘  └────────┬─────────┘  └──────────┬───────────┘
           │                      │                        │
           └──────────────────────┴────────────────────────┘
                                  │
                                  ▼
                        ┌──────────────────┐
                        │  Shared Backend  │
                        │  - DynamoDB      │
                        │  - AgentCore     │
                        │  - S3            │
                        └──────────────────┘
```

---

## 3. Bot Configuration Model

### 3.1 Configuration Source

Bot configurations stored in **Secrets Manager** as a single JSON secret:

```
openclaw/ws-bridge/bots
```

Managed interactively via `./scripts/setup-multi-bot.sh` (add/list/remove/restart).

Secret value (JSON):

```json
{
  "bots": [
    {
      "id": "dingtalk-main",
      "channel": "dingtalk",
      "enabled": true,
      "credentials": {
        "clientId": "ding...",
        "clientSecret": "..."
      }
    },
    {
      "id": "feishu-main",
      "channel": "feishu",
      "enabled": true,
      "credentials": {
        "appId": "cli_...",
        "appSecret": "..."
      }
    }
  ]
}
```

#### Config Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes | Unique bot identifier, alphanumeric + hyphens/underscores, 1-48 chars |
| `channel` | string | Yes | `"dingtalk"` or `"feishu"` |
| `enabled` | bool | Yes | Whether the bot is active |
| `credentials` | object | Yes | Channel-specific credentials |

**DingTalk credentials:** `{"clientId": "...", "clientSecret": "..."}`
**Feishu credentials:** `{"appId": "...", "appSecret": "..."}`

Design principle: **The bridge is a message transport layer.** Bot config contains only connection credentials and routing info.

### 3.2 Bot ID Convention

Convention: `{channel}-{name}` (e.g., `dingtalk-main`, `feishu-sales`). Validated at startup by `BotConfig.validate()`.

### 3.3 User ID Namespace

User IDs are channel-scoped (not bot-scoped):

- DingTalk: `dingtalk:{staffId}`
- Feishu: `feishu:{open_id}`

Same user on different bots within the same platform shares one AgentCore session and file namespace.

### 3.4 Bot-Aware Identity Tracking

The bridge records `lastBotId` on the user's SESSION record (conditional write — only when the bot changes):

| PK | SK | New Attributes |
|---|---|---|
| `USER#user_abc123` | `SESSION` | `lastBotId` (string), `lastBotChannel` (string) |

Implementation: `core/identity.py:IdentityService.update_bot_preference()` uses `ConditionExpression` to skip DynamoDB writes when the value hasn't changed.

Used by cron Lambda for bot-aware delivery (§9).

### 3.5 Per-Bot Allowlist (DynamoDB)

Bot-level allowlists use DynamoDB (not Secrets Manager) for dynamic updates without Fargate restarts:

```
Inbound message → Global allowlist check → Bot allowlist check → Process
```

DynamoDB schema:

| PK | SK | Purpose |
|---|---|---|
| `BOT_ALLOW#{bot_id}#{actor_id}` | `BOT_ALLOW` | Bot-level allowlist entry |
| `BOT_META#{bot_id}` | `BOT_META` | Restricted flag (`restricted: true`) |

**Rules:**
1. Global allowlist (`ALLOW#` records) checked first
2. If `BOT_META#{bot_id}` has `restricted: true`, check for `BOT_ALLOW#{bot_id}#{actor_id}`
3. If no `BOT_META` record exists, bot is open to all globally-allowed users

Implementation: `core/identity.py:IdentityService.check_bot_allowlist()`

**Admin commands** (`scripts/manage-allowlist.sh`):

```bash
./scripts/manage-allowlist.sh add-bot dingtalk-sales dingtalk:staff001
./scripts/manage-allowlist.sh remove-bot dingtalk-sales dingtalk:staff001
./scripts/manage-allowlist.sh list-bot dingtalk-sales
```

The `add-bot` command creates both the `BOT_ALLOW#` entry and the `BOT_META#` restricted flag. `remove-bot` cleans up the `BOT_META#` record when the last entry is removed.

### 3.6 DingTalk Multi-Bot Specifics

| Scenario | Bot Config | User ID Space |
|----------|-----------|---------------|
| Same org, different bots | Different `clientId`, same org | Same `staffId` — same identity |
| Different orgs (ISV) | Different `clientId`, different orgs | Different `staffId` — different identities |

Key: `robotCode` = `clientId`. Each adapter uses its own `clientId` for all API calls. Access token is per-bot (`_token_cache` on each `DingTalkAdapter` instance).

### 3.7 Feishu Multi-Bot Specifics

| Scenario | Bot Config | User ID Space |
|----------|-----------|---------------|
| Same tenant, different apps | Different `appId`, same tenant | Same `open_id` — same identity |
| Different tenants (ISV) | Different `appId`, different tenants | Different `open_id` — different identities |

Key findings:
- Each `FeishuAdapter` fetches its bot name and `open_id` via `/bot/v3/info` HTTP API on startup for `@mention` stripping
- **Different apps in the same tenant get different `open_id`s for the same user.** Verified: user messaging bot-1 gets `feishu:ou_c543...`, messaging bot-2 gets `feishu:ou_4e70...`. These are treated as separate user identities (different AgentCore sessions, different S3 namespaces). This is by design — Feishu `open_id` is app-scoped
- Image download requires `message_id` + `file_key` (both captured in `InboundMessage`)
- The `lark-oapi` SDK manages per-app token refresh internally via `lark.Client`

---

## 4. Implementation

### 4.1 Directory Structure

```
ws-bridge/
  Dockerfile                    # Python 3.13-slim, ARM64, port 8080
  requirements.txt              # dingtalk-stream>=0.24.0, lark-oapi>=1.4.0, boto3>=1.35.0
  main.py                       # Entry point: logging, signal handling, startup
  manager.py                    # Bot lifecycle manager (thread-per-bot, exponential backoff)
  health.py                     # HTTP health check server (/health, port 8080)
  core/
    __init__.py
    shared.py                   # SharedCore: wires services + shared message processing flow
    identity.py                 # IdentityService: resolve_user, sessions, bind, bot allowlist
    agentcore.py                # AgentCoreService: invoke with retry (3 attempts, 5/15/30s delays)
    content.py                  # extract_text_from_content_blocks (nested JSON unwrapping)
    s3.py                       # S3Service: upload image/file, fetch, head, presigned URLs
    secrets.py                  # Secret caching (15 min TTL), bot config loading
    dedup.py                    # DedupService: thread-safe per-bot message dedup (5 min TTL)
    outbound.py                 # Screenshot/file delivery, S3 URL→marker conversion
  adapters/
    __init__.py
    base.py                     # BotConfig, BotStatus, InboundMessage, ChannelAdapter ABC
    dingtalk.py                 # DingTalkAdapter: Stream SDK, per-bot token, media upload/download
    feishu.py                   # FeishuAdapter: lark-oapi WS, @mention strip, Feishu API send/download
```

### 4.2 ChannelAdapter Interface

```python
class ChannelAdapter(ABC):
    def start(self): ...          # Blocking — runs in dedicated thread
    def stop(self): ...           # Called from main thread on SIGTERM
    def send_text(self, receiver_id, text, *, is_group, conversation_id): ...
    def send_image(self, receiver_id, image_url_or_media_id, *, ...): ...
    def send_file(self, receiver_id, media_id, filename, file_type, *, ...): ...
    def send_link(self, receiver_id, title, text, message_url, *, ...): ...
    def upload_media(self, file_bytes, filename, media_type) -> str|None: ...
    def download_media(self, download_code, max_bytes, *, message_id) -> (bytes|None, str): ...
```

The `send_link` and `upload_media` methods enable the shared outbound delivery logic (`core/outbound.py`) to work identically across DingTalk and Feishu — the adapter handles platform-specific message formats.

### 4.3 Threading Model

**Hybrid threading** — different strategies for each SDK:

**DingTalk: thread-per-bot.** `dingtalk_stream.DingTalkStreamClient` manages its own asyncio event loop internally. Each DingTalk bot runs in a dedicated daemon thread via `_run_bot()` with exponential backoff (5s → 60s).

**Feishu: all bots share ONE thread.** `lark_oapi.ws.client` uses a **module-level** asyncio event loop variable (`loop = asyncio.get_event_loop()` at import time). All internal operations (`_connect`, `_ping_loop`, `_receive_message_loop`, `_handle_message`) reference this global `loop`. Multiple Feishu bots **cannot** run in separate threads because they'd overwrite each other's loop reference.

Solution: `BotManager._run_feishu_group()` creates one fresh asyncio event loop, patches `lark_oapi.ws.client.loop`, then connects all Feishu clients as concurrent tasks on that single loop:

```
BotManager.load_and_start()
  ├── Thread: bot-dingtalk-main     → _run_bot(DingTalkAdapter)     [own event loop]
  ├── Thread: bot-dingtalk-team-b   → _run_bot(DingTalkAdapter)     [own event loop]
  └── Thread: feishu-group          → _run_feishu_group([adapter1, adapter2])
                                       ├── patch lark_oapi.ws.client.loop
                                       ├── adapter1._build_feishu_client() + _connect()
                                       ├── adapter2._build_feishu_client() + _connect()
                                       └── await _select() (infinite sleep, tasks run concurrently)
```

Thread safety:
- **boto3 clients**: thread-safe by design
- **Dedup cache / Secret cache**: protected by `threading.Lock`
- **AgentCore invocation**: stateless per call
- **Feishu message callbacks** (`_on_message`): run in the feishu-group thread's event loop, dispatch to `core.process_message()` synchronously (boto3 is sync)

### 4.4 Message Processing Flow

Implemented in `core/shared.py:SharedCore.process_message()`:

1. **Dedup** — keyed by `bot_id:message_id`
2. **Bind/link commands** — `link CODE` or `link accounts`
3. **Identity resolution** — global allowlist check
4. **Bot allowlist** — `BOT_META#` / `BOT_ALLOW#` check
5. **Media handling** — download from platform, upload to S3
6. **Session + bot preference** — `get_or_create_session()` + conditional `update_bot_preference()`
7. **AgentCore invocation** — with 3-attempt retry
8. **Content extraction** — nested JSON unwrapping
9. **Screenshot delivery** — `[SCREENSHOT:key]` markers → adapter.send_image
10. **S3 URL conversion** — model-generated S3 URLs → `[SEND_FILE:path]`
11. **File delivery** — native upload (≤10MB) or presigned URL link card
12. **Text reply** — via adapter.send_text

### 4.5 DingTalk Adapter

File: `adapters/dingtalk.py` (~280 lines)

- Per-bot access token cache (`_token_cache` dict)
- `_make_handler()` creates a `dingtalk_stream.ChatbotHandler` subclass that converts raw callback data to `InboundMessage` and dispatches to `core.process_message` via thread pool
- `_send_robot_message()` generic sender — all send methods (text, image, file, link) delegate to it with different `msgKey` / `msgParam`
- `download_media()` — two-step: DingTalk API → OSS download URL → fetch bytes, with HTTP→HTTPS upgrade
- Message text extraction handles `text`, `richText`, `picture`, `file`, `video` message types

### 4.6 Feishu Adapter

File: `adapters/feishu.py` (~340 lines)

- **Two clients**: `lark.Client` for HTTP API calls (send, download), `lark.ws.Client` for WebSocket events
- **`lark.Client.builder()`** takes no args — chain `.app_id(x).app_secret(y).domain(url).build()`
- **`lark.ws.Client()`** is a constructor, NOT a builder — `lark.ws.Client(app_id, app_secret, event_handler=handler, log_level=..., domain=...)`
- **`_fetch_bot_info()`** uses direct HTTP API (`GET /open-apis/bot/v3/info` with `tenant_access_token`) because `lark_oapi.Client` has no `bot` module. Fetches `bot_name` + `open_id` for @mention stripping
- **`_build_feishu_client()`** called by `BotManager._run_feishu_group()` before connect — constructs both clients and event handler. Must be called after `lark_oapi.ws.client.loop` is patched
- `_on_message()` parses `message.content` JSON, strips bot mentions using `message.mentions[]` array matching `self._bot_open_id`
- `send_text/image/file` use `lark_oapi.api.im.v1.CreateMessageRequest`
- `send_link` uses `post` (rich text) message type with `[{tag:"a", href:url}]`
- `upload_media` — images via `/im/v1/images`, files via `/im/v1/files` with auto-detected Feishu file type
- `download_media` — `GetMessageResourceRequest` with `message_id` + `file_key`

**lark-oapi gotchas (learned the hard way):**
- `lark.ws.Client` has NO builder pattern — use constructor directly
- `lark.Client.builder()` takes ZERO args — not `builder(app_id, app_secret)`
- No `lark_oapi.api.bot` module exists — use HTTP API for bot info
- Module-level `loop` in `lark_oapi.ws.client` — must patch before constructing `ws.Client` (ExpiringCache.__init__ calls `loop.create_task()`)
- `ws.Client.start()` calls `loop.run_until_complete()` — fails with "This event loop is already running" if another coroutine is driving the loop

---

## 5. Infrastructure (CDK)

### 5.1 OpenClawWsBridge Stack

File: `stacks/ws_bridge_stack.py` — replaces `stacks/dingtalk_stack.py` (legacy kept but not deployed).

| Resource | Name |
|----------|------|
| Stack | `OpenClawWsBridge` |
| ECS Cluster | `openclaw-ws-bridge` |
| Service | `openclaw-ws-bridge` |
| Log group | `/openclaw/ws-bridge` |
| Docker context | `ws-bridge/` |
| Task def | ARM64, 256 CPU / 512 MB (configurable) |

**Opt-in** via `ws_bridge_enabled: true` in `cdk.json`. When disabled, no ECS resources are created.

Container env vars:
```
AGENTCORE_RUNTIME_ARN, AGENTCORE_QUALIFIER, IDENTITY_TABLE_NAME,
WS_BRIDGE_BOTS_SECRET_ID, USER_FILES_BUCKET, AWS_REGION,
REGISTRATION_OPEN, HEALTH_PORT=8080
```

### 5.2 IAM Permissions (task role)

Same as the old DingTalk bridge — both channels need identical AWS access:

- `bedrock-agentcore:InvokeAgentRuntime` + `InvokeAgentRuntimeForUser`
- `dynamodb:GetItem/PutItem/UpdateItem/DeleteItem/Query`
- `secretsmanager:GetSecretValue/DescribeSecret` (scoped to `openclaw/*`)
- `s3:PutObject` (scoped to `*/_uploads/*`), `s3:GetObject/HeadObject`
- `kms:Decrypt/GenerateDataKey`

### 5.3 Secrets Manager

| Secret | Purpose |
|--------|---------|
| `openclaw/ws-bridge/bots` | Bot configuration JSON (created by `OpenClawSecurity` stack) |
| `openclaw/channels/dingtalk` | Kept — cron Lambda fallback |
| `openclaw/channels/feishu` | Kept — Router Lambda (webhook) + cron Lambda fallback |

### 5.4 cdk.json

```json
{
  "ws_bridge_enabled": true,
  "ws_bridge_cpu": 256,
  "ws_bridge_memory_mb": 512
}
```

### 5.5 Deploy Script

`scripts/deploy.sh` deploys the WS Bridge in **Phase 3** (dependent stacks):

```
Phase 1: OpenClawVpc, OpenClawSecurity, OpenClawGuardrails, OpenClawAgentCore, OpenClawObservability
Phase 2: Starter Toolkit (Runtime, ECR, Docker build)
Phase 3: OpenClawRouter, OpenClawCron, OpenClawWsBridge, OpenClawTokenMonitoring
```

Orphan cleanup handles both old `/openclaw/dingtalk-bridge` and new `/openclaw/ws-bridge` log groups.

---

## 6. Health Check & Observability

### 6.1 Health Endpoint

```
GET /health → 200 (if any bot connected) or 503

{
  "status": "ok",
  "service": "openclaw-ws-bridge",
  "bots": {
    "dingtalk-main": {"status": "connected", "channel": "dingtalk",
                      "uptime_s": 3600, "thread_alive": true},
    "feishu-main":   {"status": "connected", "channel": "feishu",
                      "uptime_s": 3580, "thread_alive": true}
  }
}
```

ECS health check calls this endpoint every 30s. Service is healthy if at least one bot is connected with a live thread.

### 6.2 CloudWatch

- Log group: `/openclaw/ws-bridge`
- All log lines include `bot=<bot_id>` prefix for per-bot filtering
- Container Insights enabled

---

## 7. Cron Integration

### 7.1 Bot-Aware Cron Delivery

The cron Lambda (`lambda/cron/index.py`) uses a **3-level credential fallback** for DingTalk/Feishu delivery:

1. **Preferred bot** — user's `lastBotId` from DynamoDB → lookup in `openclaw/ws-bridge/bots`
2. **Any enabled bot** for the channel from WS Bridge config
3. **Legacy per-channel secret** (`openclaw/channels/{channel}`)

Implementation: `_resolve_bot_credentials()` returns `(credentials_dict, source_description)`. Dedicated `_send_dingtalk_with_creds()` and `_send_feishu_with_creds()` acquire per-bot access tokens from the resolved credentials.

### 7.2 Cron Stack Changes

- Constructor: added `ws_bridge_bots_secret_name` parameter
- Lambda env var: `WS_BRIDGE_BOTS_SECRET_ID`
- IAM: Secrets Manager already scoped to `openclaw/*` prefix — no additional policy needed

---

## 8. Operations

### 8.1 Setup Script

```bash
./scripts/setup-multi-bot.sh              # Interactive menu
./scripts/setup-multi-bot.sh add          # Add a DingTalk or Feishu bot
./scripts/setup-multi-bot.sh list         # List configured bots
./scripts/setup-multi-bot.sh remove       # Remove a bot
./scripts/setup-multi-bot.sh restart      # Force ECS redeployment
```

The `add` command:
1. Prompts for channel (DingTalk / Feishu) and credentials
2. Verifies credentials against the platform API
3. Saves to Secrets Manager
4. Optionally adds the user to the global allowlist
5. Optionally restarts the ECS service

### 8.2 Fresh Deployment

```bash
# 1. Full deploy (creates all infrastructure)
./scripts/deploy.sh

# 2. Add bot(s)
./scripts/setup-multi-bot.sh add

# 3. Verify
./scripts/setup-multi-bot.sh list
```

### 8.3 Adding a Bot to Running Service

```bash
./scripts/setup-multi-bot.sh add      # adds bot + restarts ECS
```

### 8.4 Monitoring

```bash
# ECS service status
aws ecs describe-services --cluster openclaw-ws-bridge --services openclaw-ws-bridge \
  --region us-west-2 --query 'services[0].{status:status,running:runningCount}'

# Recent logs
aws logs tail /openclaw/ws-bridge --region us-west-2 --since 5m

# Filter by bot
aws logs filter-log-events --log-group-name /openclaw/ws-bridge --region us-west-2 \
  --filter-pattern "bot=dingtalk-main" --start-time $(date -d '5 min ago' +%s000)
```

---

## 9. Security

### 9.1 Credential Isolation

- Bot credentials stored in Secrets Manager (KMS-encrypted), never in env vars
- Each adapter owns its own access token cache — no cross-bot leakage
- Credentials cached in memory with 15 min TTL

### 9.2 User Namespace Isolation

- User files scoped to `{channel}_{userId}/` in S3
- Cross-channel binding works across bots (same DynamoDB identity table)
- Different bots on the same platform share user identity (by design — §3.3)

### 9.3 Network

- Fargate task in VPC private subnets (no public IP)
- Egress: HTTPS only (port 443) for platform WebSocket + AWS APIs
- VPC endpoint for `bedrock-agentcore` data plane

### 9.4 Bot-level Access Control

- **Per-bot allowlist** (§3.5): DynamoDB-backed, dynamically updatable without restart
- **Platform-level visibility**: Feishu app visibility scope + DingTalk org membership

---

## 10. Python SDK Choices

| Channel | SDK | PyPI Package | Blocking Call | Event Loop | Official Reference |
|---------|-----|-------------|---------------|------------|--------------------|
| DingTalk | dingtalk-stream | `dingtalk-stream>=0.24.0` | `start_forever()` | Per-instance (internal) | [dingtalk-openclaw-connector](https://github.com/DingTalk-Real-AI/dingtalk-openclaw-connector) |
| Feishu | lark-oapi | `lark-oapi>=1.4.0` | `client.start()` | **Module-level global** | [openclaw-lark](https://github.com/larksuite/openclaw-lark) |

Both SDKs handle WebSocket connection + auto-reconnection + event dispatching + access token management. Both use **blocking** start calls, but with different event loop models:
- **DingTalk**: each `DingTalkStreamClient` creates its own event loop → safe to run in separate threads
- **Feishu**: `lark_oapi.ws.client` uses a module-level `loop` variable shared by ALL instances → all Feishu bots must run on one shared thread (see §4.3)

### Why Python (not Node.js)

- DingTalk bridge was already Python and proven in production
- `lark-oapi` Python SDK has full WebSocket support
- Router Lambda and cron Lambda are Python — shared patterns
- Single language in the container

---

## 11. Comparison: Official openclaw-lark Plugin

| Aspect | Official Plugin | Our WS Bridge |
|--------|----------------|---------------|
| Runtime | Inside OpenClaw process | Standalone ECS Fargate |
| Language | TypeScript (plugin SDK) | Python |
| Integration | `openclaw/plugin-sdk` | Direct Bedrock AgentCore API |
| Multi-account | `accounts` map in config | Secrets Manager JSON |
| Features | Rich (cards, docs, bitable) | Text, images, files |
| Warm-up | N/A (runs with OpenClaw) | Not affected (always running) |

Our architecture runs OpenClaw headless — the official plugin's multi-account model inspired our bot configuration design.

---

## 12. Open Questions

### Resolved

1. ~~**Fargate sizing**~~ 256 CPU / 512 MB handles 8-10 bots. Configurable via `ws_bridge_cpu` / `ws_bridge_memory_mb`.
2. ~~**Hot reload**~~ Deferred. Requires ECS restart (`setup-multi-bot.sh restart`).
3. ~~**Feishu @mention stripping**~~ Fetches bot info via HTTP API (`/bot/v3/info`), uses `mentions[]` array matching `_bot_open_id`.
4. ~~**Feishu event decryption**~~ SDK handles internally in WebSocket mode.
5. ~~**Threading model**~~ Hybrid: DingTalk thread-per-bot, Feishu all-on-one-thread. Forced by `lark-oapi` module-level event loop (§4.3).
6. ~~**Per-bot access token**~~ DingTalk: isolated per adapter instance. Feishu: SDK manages internally per `lark.Client`.
7. ~~**Bot allowlist optimization**~~ Option A implemented: `BOT_META#` record with `restricted` flag.
8. ~~**lark-oapi API surface**~~ `ws.Client` is a constructor (not builder). `Client.builder()` takes no args. No `api.bot` module — use HTTP API for bot info. See §4.6 gotchas.
9. ~~**Feishu open_id scope**~~ Verified: `open_id` is **app-scoped** within the same tenant. Same user messaging two different Feishu apps gets different `open_id`s → different user identities (§3.7).

### Open

10. **Cross-tenant Feishu identity**: For ISV apps spanning multiple tenants, `union_id` would unify identity. Current `feishu:{open_id}` naturally separates by app. Not yet needed.
11. **DingTalk ISV tokens**: ISV apps use a different OAuth flow. Current implementation supports internal apps only.
12. **Feishu message card callbacks**: Not handled. Plain messages only — card callbacks deferred.
13. **Same-user identity across Feishu apps**: Same user gets different `open_id` per app. If cross-app identity is desired, would need `union_id` or manual binding. Current behavior (separate identities) is acceptable for multi-tenant use cases but may surprise same-tenant multi-bot deployments.

---

## 13. Future Work

- Hot reload: watch Secrets Manager for config changes without restart
- Per-bot rate limiting and token budgets
- Per-bot tool restrictions (AgentCore-level)
- Per-bot conversation isolation (separate sessions per bot)
- HA: `desired_count=2` with message dedup preventing double-processing
- Feishu interactive message cards
