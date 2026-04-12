# AgentCore Gateway 集成指南

## 概述

OpenClaw 通过 AgentCore Gateway 技能，可以直接调用注册在 Gateway 上的外部 API 工具。Gateway 统一管理 API 认证（API Key、OAuth、IAM），agent 无需直接处理凭证。

## 架构

```
用户 → 飞书/Telegram → Router Lambda → AgentCore Runtime（容器）
                                              │
                                              ├── agentcore-gateway skill
                                              │     ├── gateway_list_tools（发现工具）
                                              │     └── gateway_call_tool（调用工具）
                                              │           │
                                              │           ▼ SigV4 签名
                                              │     AgentCore Gateway（MCP 协议）
                                              │           │
                                              │           ├── Token Vault（自动注入 API Key）
                                              │           ├── Target: 天气 API
                                              │           ├── Target: 足球数据 API
                                              │           └── Target: ...（可扩展）
```

## 部署步骤

### 1. 部署 Gateway 基础设施

```bash
# 部署 VPC Endpoint（容器 DNS 解析 *.gateway.bedrock-agentcore 域名）
cdk deploy OpenClawVpc --require-approval never

# 部署 Gateway（IAM 授权模式，MCP 协议）
cdk deploy OpenClawGateway --require-approval never

# 更新 Runtime 执行角色（添加 InvokeGateway 权限）
cdk deploy OpenClawAgentCore --require-approval never
```

### 2. 注册 API Target

使用脚本添加 sample target：

```bash
# 添加足球数据 API（football-data.org）
./scripts/manage-gateway-targets.sh add football <FOOTBALL_DATA_API_KEY>

# 添加天气 API（OpenWeatherMap）
./scripts/manage-gateway-targets.sh add weather <OPENWEATHERMAP_API_KEY>

# 查看已注册的 targets
./scripts/manage-gateway-targets.sh list
```

脚本会自动：
- 创建/更新 Token Vault 中的 API Key Credential Provider
- 在 Gateway 上注册 Target（含 OpenAPI Schema）
- 幂等操作，重复运行不会创建重复 target

### 3. 更新容器镜像

```bash
# 修改 cdk.json 中的 image_version（递增）
# 重新构建镜像 + 注入 AGENTCORE_GATEWAY_URL 环境变量
./scripts/deploy.sh --runtime-only
```

## 在龙虾中使用

### 自动发现

龙虾启动后会加载 `agentcore-gateway` 技能。用户可以直接用自然语言提问，龙虾会自动：

1. 调用 `gateway_list_tools` 发现 Gateway 上可用的工具
2. 选择合适的工具
3. 调用 `gateway_call_tool` 执行

### 使用示例

| 用户说 | 龙虾做什么 |
|---|---|
| "英超积分榜" | → `getStandings`（competition=PL） |
| "今天有什么足球比赛" | → `listMatches` |
| "利物浦最近的比赛" | → `getTeamMatches` |
| "英超射手榜前10" | → `getScorers`（competition=PL, limit=10） |
| "北京今天天气怎么样" | → `getCurrentWeather`（q=Beijing） |

### 技能详情

`agentcore-gateway` 技能提供两个工具：

**gateway_list_tools** — 列出 Gateway 上所有可用工具
```bash
node /skills/agentcore-gateway/list.js
```

**gateway_call_tool** — 调用指定工具
```bash
node /skills/agentcore-gateway/call.js <tool_name> '<json_arguments>'
```

示例：
```bash
# 查询英超积分榜
node /skills/agentcore-gateway/call.js football-data-v4___getStandings '{"id":"PL"}'

# 查询北京天气
node /skills/agentcore-gateway/call.js openweathermap-current___getCurrentWeather '{"q":"Beijing","units":"metric"}'
```

工具名格式：`{target名}___{operationId}`

## 添加新的 API Target

### 方式一：修改脚本

在 `scripts/manage-gateway-targets.sh` 中添加新的 case：

1. 定义 OpenAPI Schema（JSON 格式）
2. 在 `case "$TARGET"` 中添加新分支
3. 运行 `./scripts/manage-gateway-targets.sh add <新target> <API_KEY>`

### 方式二：直接用 AWS CLI

```bash
# 1. 创建 API Key Credential Provider
aws bedrock-agentcore-control create-api-key-credential-provider \
  --name "my-api-key" \
  --api-key "YOUR_KEY" \
  --region us-west-2

# 2. 获取 Gateway ID
GW_ID=$(aws cloudformation describe-stacks \
  --stack-name OpenClawGateway \
  --query "Stacks[0].Outputs[?OutputKey=='GatewayId'].OutputValue" \
  --output text --region us-west-2)

# 3. 创建 Target
aws bedrock-agentcore-control create-gateway-target \
  --gateway-id "$GW_ID" \
  --name "my-api" \
  --description "My custom API" \
  --target-configuration '{"mcp":{"openApiSchema":{"inlinePayload":"..."}}}' \
  --credential-provider-configurations '[...]' \
  --region us-west-2
```

## IAM 权限说明

| 角色 | 权限 | 用途 |
|---|---|---|
| `openclaw-gateway-role` | `GetWorkloadAccessToken` | Gateway 获取 workload identity token |
| | `GetResourceApiKey` | Gateway 从 Token Vault 读取 API key 配置 |
| | `secretsmanager:GetSecretValue` | Token Vault 底层从 Secrets Manager 取 key 值 |
| `openclaw-agentcore-execution-role` | `InvokeGateway` | 容器调用 Gateway MCP 端点 |
| scoped credentials（per-user） | `InvokeGateway` | 用户隔离的 session 中调用 Gateway |

## 排障

### 容器报 `AGENTCORE_GATEWAY_URL not set`
- 确认 `OpenClawGateway` stack 已部署
- 重新运行 `./scripts/deploy.sh --runtime-only`

### Gateway 报 `GetWorkloadAccessToken` 403
- 确认 `cdk deploy OpenClawGateway` 已完成（gateway role 需要此权限）

### Gateway 报 `GetResourceApiKey` 403
- 同上，gateway role 需要此权限

### Gateway 报 `secretsmanager:GetSecretValue` 403
- 同上，gateway role 需要此权限（Token Vault 底层用 Secrets Manager）

### DNS 解析失败（容器内）
- 确认 VPC Endpoint `com.amazonaws.{region}.bedrock-agentcore.gateway` 已创建
- `cdk deploy OpenClawVpc`

### Skill 不在列表中
- 确认 `bridge/Dockerfile` 中有 `COPY skills/agentcore-gateway`
- bump `image_version` 并重新 `./scripts/deploy.sh --runtime-only`
