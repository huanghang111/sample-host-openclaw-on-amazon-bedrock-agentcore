# 钉钉频道接入指南

本文档介绍如何将钉钉机器人接入 OpenClaw on AgentCore Runtime。

## 前置条件

- 已通过 `./scripts/deploy.sh` 完成全量部署（3 阶段）
- 拥有钉钉企业管理员权限（用于创建企业内部应用）

## 步骤 1：创建钉钉机器人

### 1.1 创建应用

1. 访问 [钉钉开放平台](https://open-dev.dingtalk.com/)
2. 点击 **"应用开发"**

### 1.2 添加机器人能力

1. 在应用详情页，点击 **一键创建 OpenClaw 机器人应用**

### 1.3 获取凭证

1. 完成创建后，进入 **"凭证与基础信息"** 页面
2. 复制 **AppKey**（即 Client ID）
3. 复制 **AppSecret**（即 Client Secret）

> **重要**：Client ID 和 Client Secret 是机器人的唯一凭证，请妥善保管，不要泄露。

## 步骤 2：配置凭证并接入

运行设置脚本：

```bash
./scripts/setup-dingtalk.sh
```

脚本会引导你完成以下操作：

1. 输入上一步获取的 AppKey 和 AppSecret
2. 将凭证存入 AWS Secrets Manager（`openclaw/channels/dingtalk`）
3. 输入你的钉钉用户 ID，加入白名单

完成后，重启 ECS 服务以加载凭证：

```bash
aws ecs update-service --cluster openclaw-dingtalk \
  --service openclaw-dingtalk-bridge --force-new-deployment \
  --region us-west-2
```

## 步骤 3：添加用户

### 方式一：开放注册（推荐，适合大量用户）

在 `cdk.json` 中设置：

```json
"registration_open": true
```

然后重新部署：

```bash
./scripts/deploy.sh --phase3
```

开放注册后，任何钉钉用户给机器人发消息即可自动注册，无需逐个添加白名单。

### 方式二：白名单管理（精确控制访问权限）

#### 添加单个用户

```bash
./scripts/manage-allowlist.sh add dingtalk:用户ID
```

#### 批量添加

将用户 ID 写入文件（每行一个），然后批量导入：

```bash
# dingtalk_users.txt 示例：
# 01455368144039922107
# 06283719502847261033

while read -r uid; do
  ./scripts/manage-allowlist.sh add "dingtalk:$uid"
done < dingtalk_users.txt
```

#### 查看白名单

```bash
./scripts/manage-allowlist.sh list
```

#### 移除用户

```bash
./scripts/manage-allowlist.sh remove dingtalk:用户ID
```

### 用户如何获取自己的钉钉 ID

如果用户不知道自己的 ID：

1. 用户给机器人发一条任意消息
2. 机器人回复拒绝消息，其中包含用户 ID（例如 `dingtalk:01455368144039922107`）
3. 用户将此 ID 发给管理员
4. 管理员执行 `./scripts/manage-allowlist.sh add dingtalk:01455368144039922107`
5. 用户再次发消息即可正常使用

## 凭证格式

存储在 Secrets Manager 中的凭证格式（`openclaw/channels/dingtalk`）：

```json
{"clientId": "dingXXXXXXXX", "clientSecret": "XXXXXXXX"}
```

## 用户身份标识

| 字段 | 格式 | 示例 |
|------|------|------|
| 频道标识 | `dingtalk:<staffId>` | `dingtalk:01455368144039922107` |
| S3 命名空间 | `dingtalk_<staffId>` | `dingtalk_01455368144039922107` |
| 白名单记录 | `ALLOW#dingtalk:<staffId>` | DynamoDB PK |

## 架构说明

钉钉使用 **Stream 模式**（长连接 WebSocket），与 Telegram/Slack/飞书的 Webhook 回调模式不同。
因此钉钉桥接服务运行在 **ECS Fargate** 上（而非 Lambda），作为常驻进程维持 WebSocket 连接。

```
钉钉云 <==WebSocket==> ECS Fargate (dingtalk-bridge)
                              |
                              +-- DynamoDB (用户解析)
                              +-- AgentCore Runtime (AI 对话)
                              +-- S3 (图片上传)
                              +-- 钉钉 REST API (发送回复)
```

## 常见问题

**Q：机器人没有回复消息？**
检查 ECS 服务日志：
```bash
aws logs tail /openclaw/dingtalk-bridge --follow --region us-west-2
```

**Q：如何更新凭证？**
```bash
aws secretsmanager update-secret \
  --secret-id openclaw/channels/dingtalk \
  --secret-string '{"clientId":"新AppKey","clientSecret":"新AppSecret"}' \
  --region us-west-2
```
然后重启 ECS 服务。

**Q：如何从开放注册切回白名单模式？**
将 `cdk.json` 中 `registration_open` 改回 `false`，然后执行 `./scripts/deploy.sh --phase3`。已注册的用户不受影响。
