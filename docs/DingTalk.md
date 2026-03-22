# DingTalk Integration Design (Option B: ECS Fargate)

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
   a. Extract senderId, conversationType, text/image
   b. Dedup check (in-memory, 5min TTL)
   c. Handle bind/link commands
   d. `resolve_user("dingtalk", senderId)` via DynamoDB
   e. `get_or_create_session(userId)` via DynamoDB
   f. If image: download from DingTalk API, upload to S3, build structured message
   g. `invoke_agent_runtime(session, userId, actorId, "dingtalk", message)`
   h. Extract response text (handle content blocks)
   i. Send reply via DingTalk Robot API

## DingTalk APIs Used

| Purpose | API | Method |
|---------|-----|--------|
| Get access token | `api.dingtalk.com/v1.0/oauth2/accessToken` | POST |
| Send DM | `api.dingtalk.com/v1.0/robot/oToMessages/batchSend` | POST |
| Send to group | `api.dingtalk.com/v1.0/robot/groupMessages/send` | POST |
| Download image | `api.dingtalk.com/v1.0/robot/messageFiles/download` | POST |

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

For picture messages: `msgtype: "picture"`, content has `downloadCode`.

## Files

### New

| File | Purpose |
|------|---------|
| `dingtalk-bridge/bridge.py` | Main service: Stream client, user resolution, AgentCore invocation, reply, image handling, health check |
| `dingtalk-bridge/requirements.txt` | `dingtalk-stream`, `boto3` |
| `dingtalk-bridge/Dockerfile` | Python 3.13-slim ARM64 container |
| `stacks/dingtalk_stack.py` | CDK: ECS cluster, Fargate service, task def, IAM, SG, logging |
| `scripts/setup-dingtalk.sh` | Store credentials in Secrets Manager, add user to allowlist |

### Modified

| File | Change |
|------|--------|
| `stacks/security_stack.py` | Added `"dingtalk"` to `channel_names` for Secrets Manager placeholder |
| `app.py` | Import + instantiate `DingTalkStack` |
| `stacks/cron_stack.py` | Accept + pass `dingtalk_token_secret_name` to cron Lambda env |
| `lambda/cron/index.py` | Added `send_dingtalk_message()` + wired into `deliver_response()` |

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

```bash
# Phase 1: Deploy CDK stacks (creates ECS infrastructure + DingTalk secret)
cdk deploy OpenClawSecurity OpenClawDingTalk OpenClawCron --require-approval never

# Phase 2: Store credentials
./scripts/setup-dingtalk.sh

# Phase 3: Force ECS service restart (pick up credentials)
aws ecs update-service --cluster openclaw-dingtalk \
  --service openclaw-dingtalk-bridge --force-new-deployment \
  --region $CDK_DEFAULT_REGION
```

## Scope Exclusions (v1)

Not in initial implementation:
- AI Card streaming (plain text replies only)
- Video/audio/file sending
- File attachment extraction (.docx, .pdf parsing)
- Progress notification during long tasks
- Screenshot delivery ([SCREENSHOT:key] markers)
- Markdown-to-DingTalk formatting conversion
