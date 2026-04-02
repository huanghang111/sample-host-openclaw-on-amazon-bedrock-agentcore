# DingTalk Integration Design (ECS Fargate)

## Why ECS Fargate (not Lambda)

DingTalk Robot uses **Stream mode** — a client-initiated, long-lived WebSocket connection.
The bot's code connects OUT to DingTalk's servers; DingTalk never sends HTTP requests to you.
This is fundamentally different from Telegram/Slack/Feishu which use inbound HTTP webhooks.

- Lambda + API Gateway cannot act as a persistent WebSocket client
- DingTalk delivers ALL messages for one bot to a single WebSocket connection
- A singleton long-running process is required — ECS Fargate is the right fit

## Architecture

```
  Telegram ──webhook──┐
  Slack    ──webhook──┼──▶ API Gateway ──▶ Router Lambda ──┐
  Feishu   ──webhook──┘                                    │
                                                           ▼
                                                    DynamoDB Identity
                                                           │
                                                           ▼
                                                    AgentCore Runtime
                                                    (per-user containers)
                                                           ▲
                                                           │
  DingTalk Cloud ◀══WebSocket══▶ ECS Fargate ──────────────┘
                                 (dingtalk-bridge)
                                       │
                                       ├── resolve_user() ──▶ DynamoDB
                                       ├── invokeAgentRuntime() ──▶ AgentCore
                                       ├── download image ──▶ S3
                                       └── send reply ──▶ DingTalk REST API
```

## Message Flow

1. DingTalk Stream delivers callback message via WebSocket
2. Bridge ACKs immediately (DingTalk requires fast ACK)
3. Background thread processes:
   a. Extract senderId, conversationType, text/image/file/video
   b. Dedup check (in-memory, 5min TTL)
   c. Handle bind/link commands
   d. `resolve_user("dingtalk", senderId)` via DynamoDB
   e. `get_or_create_session(userId)` via DynamoDB
   f. If image: download from DingTalk API, upload to S3, build multimodal structured message
   g. If file/video: download from DingTalk API, upload to S3, build text message with file metadata
   h. `invoke_agent_runtime(session, userId, actorId, "dingtalk", message)`
   i. Extract response text (handle content blocks)
   j. Extract `[SCREENSHOT:key]` markers → deliver screenshots as images
   k. Send text reply via DingTalk Robot API

## DingTalk APIs Used

| Purpose | API | Method | Notes |
|---------|-----|--------|-------|
| Get access token | `api.dingtalk.com/v1.0/oauth2/accessToken` | POST | |
| Send DM | `api.dingtalk.com/v1.0/robot/oToMessages/batchSend` | POST | msgKey: sampleText, sampleImageMsg, sampleFile, sampleLink |
| Send to group | `api.dingtalk.com/v1.0/robot/groupMessages/send` | POST | Same msgKeys as DM |
| Get file download URL | `api.dingtalk.com/v1.0/robot/messageFiles/download` | POST | Returns OSS signed URL, not file bytes |
| Upload media | `oapi.dingtalk.com/media/upload?type={type}` | POST (multipart) | type=image (≤1MB), file (≤10MB), video (≤10MB) |

## DingTalk Stream Protocol

The `dingtalk-stream` Python package handles:
1. POST `api.dingtalk.com/v1.0/gateway/connections/open` with clientId/clientSecret → get WSS URL
2. Connect to DingTalk-provided WebSocket
3. Receive messages on `TOPIC_ROBOT`, ACK each with `AckMessage.STATUS_OK`
4. Heartbeat + auto-reconnect handled by `start_forever()`

## Callback Data Structure

```json
{
  "msgtype": "text",
  "text": { "content": " message text" },
  "senderStaffId": "staffXXX",
  "senderId": "$:LWCP_v1:$XXX",
  "senderNick": "User Name",
  "conversationType": "1",       // "1"=DM, "2"=group
  "conversationId": "cidXXX",
  "conversationTitle": "Group Name",
  "msgId": "msgXXX",
  "robotCode": "dingXXX",
  "isInAtList": true,
  "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=XXX"
}
```

For picture messages: `msgtype: "picture"`, content has `downloadCode` and `pictureDownloadCode`.
For file messages: `msgtype: "file"`, content has `downloadCode` and `fileName`.
For video messages: `msgtype: "video"`, content has `downloadCode` and `duration`.

## Files

### New

| File | Purpose |
|------|---------|
| `dingtalk-bridge/bridge.py` | Main service: Stream client, user resolution, AgentCore invocation, reply, image/file/video handling, screenshot delivery, health check |
| `dingtalk-bridge/test_media.py` | Unit tests for media download, upload, screenshot delivery (28 tests) |
| `dingtalk-bridge/requirements.txt` | `dingtalk-stream`, `boto3` |
| `dingtalk-bridge/Dockerfile` | Python 3.13-slim ARM64 container |
| `stacks/dingtalk_stack.py` | CDK: ECS cluster, Fargate service, task def, IAM, SG, logging |
| `scripts/setup-dingtalk.sh` | Store credentials in Secrets Manager, add user to allowlist (Chinese UI) |
| `docs/dingtalk-setup-zh.md` | Chinese setup guide for end users |

### Modified

| File | Change |
|------|--------|
| `stacks/security_stack.py` | Added `"dingtalk"` to `channel_names` for Secrets Manager placeholder |
| `app.py` | Import + instantiate `DingTalkStack` |
| `stacks/cron_stack.py` | Accept + pass `dingtalk_token_secret_name` to cron Lambda env |
| `lambda/cron/index.py` | Added `send_dingtalk_message()` + wired into `deliver_response()` |
| `scripts/deploy.sh` | Added `OpenClawDingTalk` to Phase 3; fixed CodeBuild Dockerfile, orphaned resource cleanup, runtime ID extraction, endpoint API |
| `.gitignore` | Added `.bedrock_agentcore.yaml` and `.bedrock_agentcore/` (deployment-specific) |
| `cdk.json` | Model set to `global.anthropic.claude-opus-4-6-v1` (Claude 4.6), region `us-west-2` |

### Unchanged

All existing infrastructure: VPC, Router Lambda, API Gateway, bridge containers,
AgentCore Runtime, observability, token monitoring.

## CDK Stack: OpenClawDingTalk

Resources:
- ECS Cluster (`openclaw-dingtalk`) with Container Insights
- Fargate Task Definition (256 CPU, 512 MiB, ARM64)
- Fargate Service (desired count=1, circuit breaker with rollback)
- Security Group (egress-only HTTPS 443)
- CloudWatch Log Group (`/openclaw/dingtalk-bridge`)
- Container image built from `dingtalk-bridge/` via `ContainerImage.from_asset()`

IAM task role permissions (same scope as Router Lambda):
- `bedrock-agentcore:InvokeAgentRuntime` on runtime ARN
- DynamoDB CRUD on identity table
- `secretsmanager:GetSecretValue` on `openclaw/*`
- `kms:Decrypt` + `kms:GenerateDataKey` on CMK
- `s3:PutObject` on `*/_uploads/*`

Environment variables: `AGENTCORE_RUNTIME_ARN`, `AGENTCORE_QUALIFIER`,
`IDENTITY_TABLE_NAME`, `DINGTALK_SECRET_ID`, `USER_FILES_BUCKET`, `AWS_REGION`,
`REGISTRATION_OPEN`, `HEALTH_PORT`.

## Credentials

Stored in Secrets Manager: `openclaw/channels/dingtalk`
```json
{"clientId": "dingXXXXXXXX", "clientSecret": "XXXXXXXX"}
```

## User Identity

- Channel key format: `dingtalk:<staffId>`
- Actor ID format: `dingtalk:<staffId>`
- Namespace format: `dingtalk_<staffId>` (for S3)
- Allowlist: `ALLOW#dingtalk:<staffId>` in DynamoDB

## Cron Delivery

Channel target format for `deliver_response()`:
- DM: `<staffId>` (plain string)
- Group: `group:<conversationId>` (prefixed)

## Deployment

### Standard deployment (recommended)

```bash
# Full 3-phase deploy (CDK + AgentCore Starter Toolkit + CDK dependent stacks)
./scripts/deploy.sh
```

This handles everything: VPC, Security, AgentCore Runtime (CodeBuild), Router,
Cron, DingTalk ECS, TokenMonitoring. The deploy script automatically:
- Checks/upgrades CDK bootstrap version
- Cleans orphaned resources from previous deploys (dashboards, log groups, DynamoDB tables, S3 buckets)
- Generates a CodeBuild-compatible Dockerfile (rewrites COPY paths with `bridge/` prefix)
- Extracts runtime ID from `.bedrock_agentcore.yaml` and updates `cdk.json`
- Defaults endpoint ID to `DEFAULT` when the control plane API returns `None`

### Post-deploy: configure DingTalk

```bash
# Store DingTalk credentials and add yourself to the allowlist
./scripts/setup-dingtalk.sh

# Force ECS service restart (pick up new credentials immediately)
aws ecs update-service --cluster openclaw-dingtalk \
  --service openclaw-dingtalk-bridge --force-new-deployment \
  --region $CDK_DEFAULT_REGION
```

### User onboarding

For many users, set `"registration_open": true` in `cdk.json` then `./scripts/deploy.sh --phase3`.
For controlled access, use `./scripts/manage-allowlist.sh add dingtalk:<staffId>`.

See `docs/dingtalk-setup-zh.md` for the full Chinese guide.

## deploy.sh Fixes (learned the hard way)

Issues discovered during fresh deployment testing and their fixes in `deploy.sh`:

| Issue | Root Cause | Fix |
|-------|-----------|-----|
| CloudWatch dashboard "already exists" | Dashboards persist after stack deletion | `cleanup_orphaned_resources()` deletes orphaned dashboards before deploy |
| Log group / DynamoDB table "already exists" | Explicit names + RETAIN policy survive stack deletion | Cleanup function handles `/openclaw/api-access`, `/openclaw/lambda/*`, `openclaw-identity` |
| S3 bucket "already exists" | Non-empty buckets retained by CloudFormation | Cleanup deletes all object versions via boto3 then removes bucket |
| CDK bootstrap too old (v27 < v30) | Early validation requires newer bootstrap | `ensure_bootstrap()` checks SSM parameter, auto-upgrades |
| Dockerfile COPY paths wrong in CodeBuild | `agentcore configure` expands source_path to project root | `sed` rewrites COPY paths with `bridge/` prefix after configure |
| Runtime ID extraction fails | `agentcore status` output not JSON-parseable | Read directly from `.bedrock_agentcore.yaml` |
| Endpoint ID empty/"None" | Wrong API name + AWS CLI literal "None" | Fixed to `bedrock-agentcore-control list-agent-runtime-endpoints`, default to `DEFAULT` |
| AgentCore SG blocks VPC deletion | ENIs managed by AgentCore persist ~24h | Known issue; use `--retain-resources` or `FORCE_DELETE_STACK`, tag VPC for later cleanup |
| S3 bucket "NoSuchBucket" despite CDK showing CREATE_COMPLETE | Bucket deleted externally but CDK RETAIN policy doesn't recreate | `verify_s3_bucket()` in deploy.sh recreates bucket with full CDK-matching config |
| S3 presigned URL "InvalidToken" | Bucket recreated without KMS encryption; scoped credentials expect KMS context | `verify_s3_bucket()` checks and repairs encryption, versioning, SSL policy, lifecycle |
| DingTalk image download timeout | `messageFiles/download` returns Alibaba OSS URL using HTTP (port 80), SG only allows 443 | Bridge upgrades `http://` to `https://` (OSS supports both) |
| DingTalk download returns JSON not image bytes | `messageFiles/download` is a two-step API: returns `{downloadUrl}` JSON, not file bytes | Bridge calls API for URL, then fetches bytes from the URL separately |
| `agentcore` CLI not found | Not in PATH or `~/.local/bin/`, but in `.venv/bin/` | Deploy script checks PATH, `.venv/bin/`, `~/.local/bin/` in order |
| DingTalk log group blocks redeploy | `/openclaw/dingtalk-bridge` persists after stack deletion | Added to `cleanup_orphaned_resources()` |

## Teardown

```bash
# 1. Destroy Starter Toolkit resources
agentcore destroy --agent openclaw_agent --force

# 2. Destroy CDK stacks
cdk destroy --all --force

# 3. If VPC stack fails (AgentCore ENIs), force-delete retaining the SG:
aws cloudformation delete-stack --stack-name OpenClawVpc \
  --region us-west-2 --deletion-mode FORCE_DELETE_STACK
```

Note: AgentCore ENIs take up to 24 hours to release. The orphaned VPC/SG/subnets
can be manually deleted afterward, or tagged for later cleanup.

## Media Support

### Receiving from users (user → bot)
- **Images** (`picture` msgtype): Two-step download (get OSS URL via `messageFiles/download` API, then fetch bytes), uploaded to S3, sent to Bedrock as multimodal content
- **Files** (`file` msgtype): Same two-step download, uploaded to S3 under `{namespace}/_uploads/file_*`, agent notified with file name/type/path (max 20 MB)
- **Videos** (`video` msgtype): Same two-step download, uploaded to S3 under `{namespace}/_uploads/vid_*`, agent notified with duration/type/path (max 20 MB)

### Download Flow (two-step)
```
1. POST api.dingtalk.com/v1.0/robot/messageFiles/download
   Body: {"downloadCode": "...", "robotCode": "..."}
   Response: {"downloadUrl": "https://wukong-file-im-*.oss-cn-*.aliyuncs.com/..."}

2. GET the downloadUrl → actual file bytes
   Note: URL may be HTTP — bridge upgrades to HTTPS (SG only allows 443)
```

### Sending to users (bot → user)
- **Text**: Via `sampleText` msgKey (chunked at 20,000 chars)
- **Screenshots**: `[SCREENSHOT:key]` markers extracted, image bytes fetched from S3, uploaded to DingTalk via `media/upload?type=image`, sent via `sampleImageMsg` (permanent, inline preview). Falls back to `sampleFile` for screenshots >1MB
- **Images ≤1MB** (.jpg/.png/.webp): Uploaded to DingTalk via `media/upload?type=image`, sent via `sampleImageMsg` (inline preview, permanent)
- **Images >1MB** (.jpg/.png/.webp): Uploaded via `media/upload?type=file`, sent via `sampleFile` (native file, permanent)
- **GIFs**: Always sent via `media/upload?type=file` → `sampleFile` (DingTalk `sampleImageMsg` doesn't render GIF animations)
- **Files/Videos ≤10MB**: Uploaded to DingTalk via `media/upload?type=file`, sent via `sampleFile` msgKey (native file bubble, permanent)
- **Files/Videos >10MB**: Fallback to `sampleLink` link card with presigned URL (1h expiry) — DingTalk OAPI `media/upload` hard limit is 10MB

### Upload Flow (outbound, S3 → DingTalk)
```
1. Download file bytes from S3 (user's namespace)

2. POST oapi.dingtalk.com/media/upload?access_token=TOKEN&type=file
   Content-Type: multipart/form-data
   Body: media=<file bytes>
   Response: {"errcode": 0, "media_id": "@lAjPM3...", "type": "file"}

3. Send via Robot API:
   - sampleImageMsg: {"photoURL": "<media_id>"}  (images ≤1MB)
   - sampleFile: {"mediaId": "<media_id>", "fileName": "...", "fileType": "..."}
```

### `[SEND_FILE:path]` Marker Convention
The agent includes `[SEND_FILE:relative_path]` in its response to send a file to the user.
- `relative_path` is relative to the user's S3 namespace (e.g., `documents/report.pdf`, `_uploads/file_123.xlsx`)
- The bridge validates the path (no `..` traversal), verifies the file exists in S3, and delivers via DingTalk native API
- The marker is automatically stripped from the response text before sending

### S3 URL Interception
The bridge also detects raw S3 URLs (presigned or plain) in agent responses and automatically converts them to `[SEND_FILE:path]` markers. This is a safety net — the model sometimes generates S3 URLs via exec despite instructions to use `[SEND_FILE:path]`.

### DingTalk OAPI Media Upload Limits
| Type | Max Size |
|------|----------|
| image | 1 MB |
| voice | 2 MB |
| video | 10 MB |
| file | 10 MB |

### Limitations
- Files/videos are stored in S3 but not parsed — the agent is told about the file and can use `s3-user-files` tools to access it
- Bedrock multimodal only supports images (jpeg, png, gif, webp) — video/audio/document content is not sent to the model directly
- Files >10MB fall back to presigned URL link cards (1h expiry) — DingTalk OAPI hard limit

## Scope Exclusions (v1)

Not in initial implementation:
- AI Card streaming (plain text replies only)
- Progress notification during long tasks
- Markdown-to-DingTalk formatting conversion
- Cron response screenshot/file delivery (cron Lambda lacks S3 access)
