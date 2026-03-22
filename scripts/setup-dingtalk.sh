#!/bin/bash
# 配置钉钉机器人并将部署者添加到用户白名单。
#
# 用法：
#   ./scripts/setup-dingtalk.sh
#
# 本脚本将：
#   1. 指导你在钉钉开放平台创建机器人应用
#   2. 提示输入钉钉应用凭证并存入 Secrets Manager
#   3. 提示输入你的钉钉 staffId 并添加到白名单
#
# 前置条件：
#   - 已完成 CDK 部署（OpenClawSecurity、OpenClawDingTalk）
#   - 已在 https://open-dev.dingtalk.com/ 创建企业内部应用
#   - aws cli 已配置且具有相应权限
#
# 环境变量：
#   CDK_DEFAULT_REGION -- AWS 区域（默认：us-west-2）
#   AWS_PROFILE        -- AWS CLI 配置文件（可选）

set -euo pipefail

REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-us-west-2}}"
TABLE_NAME="${IDENTITY_TABLE_NAME:-openclaw-identity}"
PROFILE_ARG=""
if [ -n "${AWS_PROFILE:-}" ]; then
    PROFILE_ARG="--profile $AWS_PROFILE"
fi

echo "=== OpenClaw 钉钉机器人配置 ==="
echo ""

# 支持非交互模式（通过环境变量传入）：
#   DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET, DINGTALK_STAFF_ID
NON_INTERACTIVE="${NON_INTERACTIVE:-false}"
if [ -n "${DINGTALK_CLIENT_ID:-}" ] && [ -n "${DINGTALK_CLIENT_SECRET:-}" ]; then
    NON_INTERACTIVE=true
fi

# --- 步骤 1：创建钉钉机器人 ---
echo "步骤 1：创建钉钉机器人"
echo ""
echo "  如果你尚未创建机器人，请按以下步骤操作："
echo ""
echo "  1. 访问 https://open-dev.dingtalk.com/"
echo "  2. 点击「应用开发」"
echo "  3. 点击「一键创建 OpenClaw 机器人应用」"
echo "  4. 确认消息接收模式为 Stream 模式"
echo "  5. 进入「凭证与基础信息」，复制 ClientId（AppKey）和 ClientSecret（AppSecret）"
echo "  6. 进入「权限管理」，添加以下权限："
echo "       - qyapi_robot_sendmsg    （发送机器人消息）"
echo "       - qyapi_chat_manage      （管理群聊，用于群消息发送）"
echo "  7. 进入「版本管理与发布」，发布应用"
echo ""
echo "  注意：机器人必须使用 Stream 模式，不支持 HTTP 模式。"
echo "  注意：应用必须发布后才能接收消息。"
echo ""
if [ "$NON_INTERACTIVE" != "true" ]; then
    read -rp "完成以上步骤后按回车继续..."
fi
echo ""

# --- 步骤 2：存储凭证 ---
echo "步骤 2：将钉钉凭证存入 Secrets Manager"
echo ""
CLIENT_ID="${DINGTALK_CLIENT_ID:-}"
CLIENT_SECRET="${DINGTALK_CLIENT_SECRET:-}"
if [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
    echo "请在钉钉应用的「凭证与基础信息」页面获取以下信息。"
    echo ""
    read -rp "请输入 ClientId（AppKey）：" CLIENT_ID
    read -rp "请输入 ClientSecret（AppSecret）：" CLIENT_SECRET
fi

echo "正在将凭证存入 Secrets Manager..."
aws secretsmanager update-secret \
    --secret-id openclaw/channels/dingtalk \
    --secret-string "{\"clientId\":\"${CLIENT_ID}\",\"clientSecret\":\"${CLIENT_SECRET}\"}" \
    --region "$REGION" $PROFILE_ARG

echo "凭证已存储。"
echo ""

# --- 步骤 3：验证凭证 ---
echo "步骤 3：验证凭证"
echo ""
echo "正在测试钉钉 API 连接..."
VERIFY_RESULT=$(curl -s -X POST "https://api.dingtalk.com/v1.0/oauth2/accessToken" \
    -H "Content-Type: application/json" \
    -d "{\"appKey\":\"${CLIENT_ID}\",\"appSecret\":\"${CLIENT_SECRET}\"}" 2>&1)

if echo "$VERIFY_RESULT" | grep -q "accessToken"; then
    echo "凭证验证成功。"
else
    echo "警告：凭证验证失败。返回结果："
    echo "  $VERIFY_RESULT"
    echo ""
    echo "常见原因："
    echo "  - 应用尚未发布（版本管理 -> 发布）"
    echo "  - ClientId 或 ClientSecret 输入错误"
    echo "  - IP 白名单限制"
    echo ""
    if [ "$NON_INTERACTIVE" != "true" ]; then
        read -rp "是否继续？(y/N)：" CONFIRM
        if [[ "${CONFIRM:-n}" != "y" && "${CONFIRM:-n}" != "Y" ]]; then
            echo "已取消。"
            exit 1
        fi
    else
        echo "非交互模式：忽略验证失败，继续执行。"
    fi
fi
echo ""

# --- 步骤 4：添加白名单 ---
echo "步骤 4：将你添加到白名单"
echo ""
echo "如何获取你的钉钉 staffId："
echo "  - 在钉钉中给机器人发送任意消息"
echo "  - 机器人会回复拒绝消息，其中包含你的 ID（例如 dingtalk:manager1234）"
echo ""
echo "如果你暂时不知道 staffId，可以跳过此步骤，稍后运行："
echo "  ./scripts/manage-allowlist.sh add dingtalk:<你的staffId>"
echo ""
STAFF_ID="${DINGTALK_STAFF_ID:-}"
if [ -z "$STAFF_ID" ] && [ "$NON_INTERACTIVE" != "true" ]; then
    read -rp "请输入你的钉钉 staffId（按回车跳过）：" STAFF_ID
fi

if [ -n "$STAFF_ID" ]; then
    # Strip "dingtalk:" prefix if user included it
    STAFF_ID="${STAFF_ID#dingtalk:}"
    CHANNEL_KEY="dingtalk:${STAFF_ID}"
    NOW_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    echo "正在将 $CHANNEL_KEY 添加到白名单..."
    aws dynamodb put-item \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        $PROFILE_ARG \
        --item "{
            \"PK\": {\"S\": \"ALLOW#${CHANNEL_KEY}\"},
            \"SK\": {\"S\": \"ALLOW\"},
            \"channelKey\": {\"S\": \"${CHANNEL_KEY}\"},
            \"addedAt\": {\"S\": \"${NOW_ISO}\"}
        }"
    echo "已将 $CHANNEL_KEY 添加到白名单。"
else
    echo "已跳过白名单配置。"
fi

echo ""
echo "=== 配置完成 ==="
echo ""
echo "  凭证：已存入 Secrets Manager（openclaw/channels/dingtalk）"
if [ -n "$STAFF_ID" ]; then
    echo "  白名单：已添加 dingtalk:${STAFF_ID}"
fi
echo ""
echo "钉钉桥接 ECS 服务会自动加载新凭证。"
echo "如果服务已在运行，凭证将在 15 分钟内生效（缓存 TTL），"
echo "或在下次任务重启后立即生效。"
echo ""
echo "如需立即重启 ECS 服务："
echo "  aws ecs update-service --cluster openclaw-dingtalk \\"
echo "    --service openclaw-dingtalk-bridge --force-new-deployment \\"
echo "    --region $REGION"
echo ""
echo "如需添加更多用户："
echo "  ./scripts/manage-allowlist.sh add dingtalk:<staffId>"
