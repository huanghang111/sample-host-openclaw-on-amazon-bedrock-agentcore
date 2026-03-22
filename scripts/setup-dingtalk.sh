#!/bin/bash
# Set up DingTalk Robot and add the deployer to the user allowlist.
#
# Usage:
#   ./scripts/setup-dingtalk.sh
#
# This script:
#   1. Guides you through creating a DingTalk Robot on the developer platform
#   2. Prompts for DingTalk app credentials and stores them in Secrets Manager
#   3. Prompts for your DingTalk staffId and adds you to the allowlist
#
# Prerequisites:
#   - CDK stacks deployed (OpenClawSecurity, OpenClawDingTalk)
#   - DingTalk enterprise internal app created at https://open.dingtalk.com/
#   - aws cli configured with appropriate permissions
#
# Environment:
#   CDK_DEFAULT_REGION -- AWS region (default: us-west-2)
#   AWS_PROFILE        -- AWS CLI profile (optional)

set -euo pipefail

REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-us-west-2}}"
TABLE_NAME="${IDENTITY_TABLE_NAME:-openclaw-identity}"
PROFILE_ARG=""
if [ -n "${AWS_PROFILE:-}" ]; then
    PROFILE_ARG="--profile $AWS_PROFILE"
fi

echo "=== OpenClaw DingTalk Setup ==="
echo ""

# Support non-interactive mode via env vars:
#   DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET, DINGTALK_STAFF_ID
NON_INTERACTIVE="${NON_INTERACTIVE:-false}"
if [ -n "${DINGTALK_CLIENT_ID:-}" ] && [ -n "${DINGTALK_CLIENT_SECRET:-}" ]; then
    NON_INTERACTIVE=true
fi

# --- Step 1: Instructions ---
echo "Step 1: Create DingTalk Robot"
echo ""
echo "  If you haven't already, create a DingTalk Robot:"
echo ""
echo "  1. Go to https://open.dingtalk.com/"
echo "  2. Create an enterprise internal application"
echo "  3. Add Robot capability"
echo "  4. Set message receiving mode to Stream mode"
echo "  5. Save the generated ClientId (AppKey) and ClientSecret (AppSecret)"
echo "  6. Permissions -> add these scopes:"
echo "       - qyapi_robot_sendmsg    (send robot messages)"
echo "       - qyapi_chat_manage      (manage group chats, for group messaging)"
echo "  7. Publish the application"
echo ""
echo "  IMPORTANT: The Robot must use Stream mode, not HTTP mode."
echo "  IMPORTANT: The app must be published before it can receive messages."
echo ""
if [ "$NON_INTERACTIVE" != "true" ]; then
    read -rp "Press Enter once you've completed the above steps..."
fi
echo ""

# --- Step 2: Store credentials ---
echo "Step 2: Store DingTalk credentials in Secrets Manager"
echo ""
CLIENT_ID="${DINGTALK_CLIENT_ID:-}"
CLIENT_SECRET="${DINGTALK_CLIENT_SECRET:-}"
if [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
    echo "Find these in your DingTalk app's 'Credentials & Basic Info' page."
    echo ""
    read -rp "Enter your DingTalk ClientId (AppKey): " CLIENT_ID
    read -rp "Enter your DingTalk ClientSecret (AppSecret): " CLIENT_SECRET
fi

echo "Storing credentials in Secrets Manager..."
aws secretsmanager update-secret \
    --secret-id openclaw/channels/dingtalk \
    --secret-string "{\"clientId\":\"${CLIENT_ID}\",\"clientSecret\":\"${CLIENT_SECRET}\"}" \
    --region "$REGION" $PROFILE_ARG

echo "Credentials stored."
echo ""

# --- Step 3: Verify credentials ---
echo "Step 3: Verify credentials"
echo ""
echo "Testing DingTalk API access..."
VERIFY_RESULT=$(curl -s -X POST "https://api.dingtalk.com/v1.0/oauth2/accessToken" \
    -H "Content-Type: application/json" \
    -d "{\"appKey\":\"${CLIENT_ID}\",\"appSecret\":\"${CLIENT_SECRET}\"}" 2>&1)

if echo "$VERIFY_RESULT" | grep -q "accessToken"; then
    echo "Credentials verified successfully."
else
    echo "WARNING: Credential verification failed. Response:"
    echo "  $VERIFY_RESULT"
    echo ""
    echo "Common causes:"
    echo "  - App not published (版本管理 -> 发布)"
    echo "  - ClientId/ClientSecret incorrect"
    echo "  - IP whitelist restrictions"
    echo ""
    if [ "$NON_INTERACTIVE" != "true" ]; then
        read -rp "Continue anyway? (y/N): " CONFIRM
        if [[ "${CONFIRM:-n}" != "y" && "${CONFIRM:-n}" != "Y" ]]; then
            echo "Aborted."
            exit 1
        fi
    else
        echo "Non-interactive mode: continuing despite verification failure."
    fi
fi
echo ""

# --- Step 4: Add to allowlist ---
echo "Step 4: Add yourself to the allowlist"
echo ""
echo "To find your DingTalk staffId:"
echo "  - Message the bot from DingTalk"
echo "  - The rejection reply will show your ID (e.g. dingtalk:manager1234)"
echo ""
echo "If you don't know your staffId yet, you can skip this step and run:"
echo "  ./scripts/manage-allowlist.sh add dingtalk:<your_staff_id>"
echo ""
STAFF_ID="${DINGTALK_STAFF_ID:-}"
if [ -z "$STAFF_ID" ] && [ "$NON_INTERACTIVE" != "true" ]; then
    read -rp "Enter your DingTalk staffId (or press Enter to skip): " STAFF_ID
fi

if [ -n "$STAFF_ID" ]; then
    CHANNEL_KEY="dingtalk:${STAFF_ID}"
    NOW_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    echo "Adding $CHANNEL_KEY to allowlist..."
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
    echo "Added $CHANNEL_KEY to allowlist."
else
    echo "Skipped allowlist setup."
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "  Credentials: stored in Secrets Manager (openclaw/channels/dingtalk)"
if [ -n "$STAFF_ID" ]; then
    echo "  Allowlisted: dingtalk:${STAFF_ID}"
fi
echo ""
echo "The DingTalk Bridge ECS service will pick up the credentials automatically."
echo "If the service is already running, it will use the new credentials"
echo "within 15 minutes (secret cache TTL) or after the next task restart."
echo ""
echo "To force restart the ECS service:"
echo "  aws ecs update-service --cluster openclaw-dingtalk \\"
echo "    --service openclaw-dingtalk-bridge --force-new-deployment \\"
echo "    --region $REGION"
echo ""
echo "To add more users later:"
echo "  ./scripts/manage-allowlist.sh add dingtalk:<staff_id>"
