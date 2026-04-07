#!/bin/bash
# Configure the Multi-Bot WebSocket Bridge (DingTalk + Feishu).
#
# Usage:
#   ./scripts/setup-multi-bot.sh              # Interactive: add/list/remove bots
#   ./scripts/setup-multi-bot.sh add          # Add a new bot
#   ./scripts/setup-multi-bot.sh list         # List configured bots
#   ./scripts/setup-multi-bot.sh remove       # Remove a bot
#   ./scripts/setup-multi-bot.sh restart      # Force ECS redeployment
#
# This script manages the openclaw/ws-bridge/bots secret in Secrets Manager
# and the openclaw-ws-bridge ECS Fargate service.
#
# Prerequisites:
#   - CDK deployed with ws_bridge_enabled=true (OpenClawWsBridge stack)
#   - aws cli configured with appropriate permissions
#
# Environment:
#   CDK_DEFAULT_REGION -- AWS region (default: us-west-2)
#   AWS_PROFILE        -- AWS CLI profile (optional)

set -euo pipefail

REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-us-west-2}}"
TABLE_NAME="${IDENTITY_TABLE_NAME:-openclaw-identity}"
SECRET_ID="openclaw/ws-bridge/bots"
CLUSTER_NAME="openclaw-ws-bridge"
SERVICE_NAME="openclaw-ws-bridge"
PROFILE_ARG=""
if [ -n "${AWS_PROFILE:-}" ]; then
    PROFILE_ARG="--profile $AWS_PROFILE"
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_get_current_config() {
    local raw
    raw=$(aws secretsmanager get-secret-value \
        --secret-id "$SECRET_ID" \
        --region "$REGION" $PROFILE_ARG \
        --query SecretString --output text 2>/dev/null || echo "")

    # Check if the secret contains valid bot config JSON
    if echo "$raw" | python3 -c "import sys,json; json.load(sys.stdin).get('bots')" 2>/dev/null; then
        echo "$raw"
    else
        echo '{"bots":[]}'
    fi
}

_save_config() {
    local config="$1"
    aws secretsmanager update-secret \
        --secret-id "$SECRET_ID" \
        --secret-string "$config" \
        --region "$REGION" $PROFILE_ARG > /dev/null
}

_verify_dingtalk_credentials() {
    local client_id="$1"
    local client_secret="$2"
    local result
    result=$(curl -s -X POST "https://api.dingtalk.com/v1.0/oauth2/accessToken" \
        -H "Content-Type: application/json" \
        -d "{\"appKey\":\"${client_id}\",\"appSecret\":\"${client_secret}\"}" 2>&1)
    if echo "$result" | grep -q "accessToken"; then
        return 0
    else
        echo "$result"
        return 1
    fi
}

_verify_feishu_credentials() {
    local app_id="$1"
    local app_secret="$2"
    local result
    result=$(curl -s -X POST "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal" \
        -H "Content-Type: application/json" \
        -d "{\"app_id\":\"${app_id}\",\"app_secret\":\"${app_secret}\"}" 2>&1)
    if echo "$result" | grep -q "tenant_access_token"; then
        return 0
    else
        echo "$result"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_list() {
    echo "=== Configured WS Bridge Bots ==="
    echo ""
    local config
    config=$(_get_current_config)
    local count
    count=$(echo "$config" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('bots',[])))")

    if [ "$count" = "0" ]; then
        echo "  No bots configured."
        echo ""
        echo "  Run: ./scripts/setup-multi-bot.sh add"
        return
    fi

    echo "$config" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for b in data.get('bots', []):
    status = 'enabled' if b.get('enabled', True) else 'DISABLED'
    channel = b['channel']
    bot_id = b['id']
    if channel == 'dingtalk':
        cred_hint = b.get('credentials',{}).get('clientId','?')[:12] + '...'
    elif channel == 'feishu':
        cred_hint = b.get('credentials',{}).get('appId','?')[:12] + '...'
    else:
        cred_hint = '?'
    print(f'  {bot_id:<24} {channel:<10} {status:<10} ({cred_hint})')
"
    echo ""
    echo "  Total: $count bot(s)"
}

cmd_add() {
    echo "=== Add a Bot to WS Bridge ==="
    echo ""

    # Choose channel
    echo "Select channel:"
    echo "  1) DingTalk"
    echo "  2) Feishu"
    echo ""
    read -rp "Choice (1/2): " CHANNEL_CHOICE

    local CHANNEL=""
    case "$CHANNEL_CHOICE" in
        1) CHANNEL="dingtalk" ;;
        2) CHANNEL="feishu" ;;
        *) echo "Invalid choice."; exit 1 ;;
    esac
    echo ""

    # Bot ID
    local DEFAULT_ID="${CHANNEL}-main"
    read -rp "Bot ID [$DEFAULT_ID]: " BOT_ID
    BOT_ID="${BOT_ID:-$DEFAULT_ID}"

    # Validate bot ID
    if ! echo "$BOT_ID" | grep -qE '^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$'; then
        echo "ERROR: Invalid bot ID. Must be alphanumeric/hyphens/underscores, 1-48 chars."
        exit 1
    fi

    # Check for duplicate
    local config
    config=$(_get_current_config)
    local exists
    exists=$(echo "$config" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print('yes' if any(b['id'] == '$BOT_ID' for b in data.get('bots',[])) else 'no')
")
    if [ "$exists" = "yes" ]; then
        echo "ERROR: Bot '$BOT_ID' already exists. Remove it first or choose a different ID."
        exit 1
    fi

    echo ""

    # Credentials
    if [ "$CHANNEL" = "dingtalk" ]; then
        echo "--- DingTalk Credentials ---"
        echo ""
        echo "  Get these from https://open-dev.dingtalk.com/"
        echo "  App > Credentials & Basic Info > ClientId (AppKey) / ClientSecret (AppSecret)"
        echo ""
        read -rp "ClientId (AppKey): " CLIENT_ID
        read -rp "ClientSecret (AppSecret): " CLIENT_SECRET

        echo ""
        echo "Verifying credentials..."
        local verify_out
        if verify_out=$(_verify_dingtalk_credentials "$CLIENT_ID" "$CLIENT_SECRET"); then
            echo "  Credentials verified successfully."
        else
            echo "  WARNING: Verification failed: $verify_out"
            echo ""
            echo "  Common causes:"
            echo "    - App not published yet (Version Management > Publish)"
            echo "    - ClientId or ClientSecret incorrect"
            echo ""
            read -rp "Continue anyway? (y/N): " CONFIRM
            if [[ "${CONFIRM:-n}" != "y" && "${CONFIRM:-n}" != "Y" ]]; then
                echo "Cancelled."
                exit 1
            fi
        fi

        # Build new bot entry
        NEW_BOT="{\"id\":\"${BOT_ID}\",\"channel\":\"dingtalk\",\"enabled\":true,\"credentials\":{\"clientId\":\"${CLIENT_ID}\",\"clientSecret\":\"${CLIENT_SECRET}\"}}"

    elif [ "$CHANNEL" = "feishu" ]; then
        echo "--- Feishu Credentials ---"
        echo ""
        echo "  Get these from https://open.feishu.cn/app"
        echo "  App > Credentials & Basic Info > App ID / App Secret"
        echo ""
        echo "  Required permissions:"
        echo "    - im:message (Send messages)"
        echo "    - im:message.group_at_msg (Receive group @mentions)"
        echo "    - im:resource (Download images/files)"
        echo ""
        echo "  Required events (Event Subscriptions > Add Event):"
        echo "    - im.message.receive_v1 (Receive messages)"
        echo ""
        echo "  Enable WebSocket mode:"
        echo "    - Event Subscriptions > Request URL: select 'Use Long Connection (WebSocket)'"
        echo ""
        read -rp "App ID: " APP_ID
        read -rp "App Secret: " APP_SECRET

        echo ""
        echo "Verifying credentials..."
        local verify_out
        if verify_out=$(_verify_feishu_credentials "$APP_ID" "$APP_SECRET"); then
            echo "  Credentials verified successfully."
        else
            echo "  WARNING: Verification failed: $verify_out"
            echo ""
            echo "  Common causes:"
            echo "    - App not published / approved yet"
            echo "    - App ID or App Secret incorrect"
            echo ""
            read -rp "Continue anyway? (y/N): " CONFIRM
            if [[ "${CONFIRM:-n}" != "y" && "${CONFIRM:-n}" != "Y" ]]; then
                echo "Cancelled."
                exit 1
            fi
        fi

        NEW_BOT="{\"id\":\"${BOT_ID}\",\"channel\":\"feishu\",\"enabled\":true,\"credentials\":{\"appId\":\"${APP_ID}\",\"appSecret\":\"${APP_SECRET}\"}}"
    fi

    echo ""

    # Append to config
    local updated
    updated=$(echo "$config" | python3 -c "
import sys, json
data = json.load(sys.stdin)
new_bot = json.loads('$NEW_BOT')
data.setdefault('bots', []).append(new_bot)
print(json.dumps(data, ensure_ascii=False))
")

    echo "Saving to Secrets Manager..."
    _save_config "$updated"
    echo "  Bot '$BOT_ID' ($CHANNEL) added."

    echo ""

    # Allowlist
    echo "--- Allowlist ---"
    echo ""
    if [ "$CHANNEL" = "dingtalk" ]; then
        echo "  To add yourself to the allowlist, enter your DingTalk staffId."
        echo "  If you don't know it yet, skip this step — message the bot and"
        echo "  it will reply with your ID (e.g. dingtalk:manager1234)."
        echo ""
        read -rp "DingTalk staffId (press Enter to skip): " STAFF_ID
        if [ -n "$STAFF_ID" ]; then
            STAFF_ID="${STAFF_ID#dingtalk:}"
            _add_to_allowlist "dingtalk:${STAFF_ID}"
        else
            echo "  Skipped."
            echo ""
            echo "  After restart, message the bot to get your ID, then run:"
            echo "    ./scripts/manage-allowlist.sh add dingtalk:<staffId>"
        fi
    elif [ "$CHANNEL" = "feishu" ]; then
        echo "  Note: Feishu open_id is app-scoped — each bot sees a different"
        echo "  open_id for the same user. You'll need to allowlist separately"
        echo "  for each Feishu bot you add."
        echo ""
        echo "  To find your open_id: restart the service, message the bot,"
        echo "  and it will reply with your ID (e.g. feishu:ou_xxxxx)."
        echo ""
        read -rp "Feishu open_id (press Enter to skip): " OPEN_ID
        if [ -n "$OPEN_ID" ]; then
            OPEN_ID="${OPEN_ID#feishu:}"
            _add_to_allowlist "feishu:${OPEN_ID}"
        else
            echo "  Skipped."
            echo ""
            echo "  After restart, message the bot to get your ID, then run:"
            echo "    ./scripts/manage-allowlist.sh add feishu:<open_id>"
        fi
    fi

    echo ""
    _prompt_restart
}

cmd_remove() {
    echo "=== Remove a Bot from WS Bridge ==="
    echo ""

    local config
    config=$(_get_current_config)

    # Show current bots
    local count
    count=$(echo "$config" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('bots',[])))")
    if [ "$count" = "0" ]; then
        echo "  No bots configured."
        return
    fi

    echo "Current bots:"
    echo "$config" | python3 -c "
import sys, json
for i, b in enumerate(json.load(sys.stdin).get('bots', []), 1):
    print(f'  {i}) {b[\"id\"]} ({b[\"channel\"]})')
"
    echo ""
    read -rp "Enter bot ID to remove: " REMOVE_ID

    local exists
    exists=$(echo "$config" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print('yes' if any(b['id'] == '$REMOVE_ID' for b in data.get('bots',[])) else 'no')
")
    if [ "$exists" != "yes" ]; then
        echo "  Bot '$REMOVE_ID' not found."
        return
    fi

    local updated
    updated=$(echo "$config" | python3 -c "
import sys, json
data = json.load(sys.stdin)
data['bots'] = [b for b in data.get('bots',[]) if b['id'] != '$REMOVE_ID']
print(json.dumps(data, ensure_ascii=False))
")

    echo "Saving to Secrets Manager..."
    _save_config "$updated"
    echo "  Bot '$REMOVE_ID' removed."
    echo ""
    _prompt_restart
}

cmd_restart() {
    echo "Forcing ECS redeployment..."
    aws ecs update-service \
        --cluster "$CLUSTER_NAME" \
        --service "$SERVICE_NAME" \
        --force-new-deployment \
        --region "$REGION" $PROFILE_ARG \
        --query 'service.{status:status,running:runningCount,desired:desiredCount}' \
        --output table
    echo ""
    echo "New task will start with updated bot config."
    echo "Monitor with:"
    echo "  aws ecs describe-services --cluster $CLUSTER_NAME --services $SERVICE_NAME \\"
    echo "    --region $REGION --query 'services[0].{status:status,running:runningCount,deployments:deployments[*].{status:status,running:runningCount,desired:desiredCount}}'"
}

_add_to_allowlist() {
    local channel_key="$1"
    local now_iso
    now_iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    echo "  Adding $channel_key to allowlist..."
    aws dynamodb put-item \
        --table-name "$TABLE_NAME" \
        --region "$REGION" $PROFILE_ARG \
        --item "{
            \"PK\": {\"S\": \"ALLOW#${channel_key}\"},
            \"SK\": {\"S\": \"ALLOW\"},
            \"channelKey\": {\"S\": \"${channel_key}\"},
            \"addedAt\": {\"S\": \"${now_iso}\"}
        }"
    echo "  $channel_key added to allowlist."
}

_prompt_restart() {
    echo "The WS Bridge ECS service needs to restart to pick up the change."
    read -rp "Restart now? (Y/n): " DO_RESTART
    if [[ "${DO_RESTART:-y}" == "n" || "${DO_RESTART:-y}" == "N" ]]; then
        echo ""
        echo "  Restart later with: ./scripts/setup-multi-bot.sh restart"
        echo "  Or credentials will refresh within 15 min (cache TTL)."
    else
        echo ""
        cmd_restart
    fi
}

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

usage() {
    echo "Usage: $0 {add|list|remove|restart}"
    echo ""
    echo "Commands:"
    echo "  add      — Add a new DingTalk or Feishu bot"
    echo "  list     — List all configured bots"
    echo "  remove   — Remove a bot"
    echo "  restart  — Force ECS redeployment to pick up config changes"
    echo ""
    echo "Environment:"
    echo "  CDK_DEFAULT_REGION  — AWS region (default: us-west-2)"
    echo "  AWS_PROFILE         — AWS CLI profile (optional)"
    exit 1
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Default to interactive menu if no command given
if [ $# -lt 1 ]; then
    echo "=== OpenClaw Multi-Bot WS Bridge Setup ==="
    echo ""
    echo "  1) Add a bot"
    echo "  2) List bots"
    echo "  3) Remove a bot"
    echo "  4) Restart service"
    echo ""
    read -rp "Choice (1-4): " CHOICE
    case "$CHOICE" in
        1) cmd_add ;;
        2) cmd_list ;;
        3) cmd_remove ;;
        4) cmd_restart ;;
        *) usage ;;
    esac
    exit 0
fi

case "$1" in
    add)     cmd_add ;;
    list)    cmd_list ;;
    remove)  cmd_remove ;;
    restart) cmd_restart ;;
    *)       usage ;;
esac
