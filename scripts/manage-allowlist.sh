#!/bin/bash
# Manage the user allowlist in DynamoDB.
#
# Usage:
#   ./scripts/manage-allowlist.sh add telegram:123456
#   ./scripts/manage-allowlist.sh add slack:U789ABC
#   ./scripts/manage-allowlist.sh remove telegram:123456
#   ./scripts/manage-allowlist.sh list
#
# Uses the openclaw-identity DynamoDB table.
# Requires: aws cli, appropriate IAM permissions.
# Set AWS_PROFILE and AWS_REGION as needed.

set -euo pipefail

TABLE_NAME="${IDENTITY_TABLE_NAME:-openclaw-identity}"
REGION="${AWS_REGION:-us-west-2}"
PROFILE_ARG=""
if [ -n "${AWS_PROFILE:-}" ]; then
    PROFILE_ARG="--profile $AWS_PROFILE"
fi

usage() {
    echo "Usage: $0 {add|remove|list|add-bot|remove-bot|list-bot} [args...]"
    echo ""
    echo "Global allowlist commands:"
    echo "  add    <channel:user_id>  — Allow a user (e.g. telegram:123456)"
    echo "  remove <channel:user_id>  — Revoke a user's access"
    echo "  list                      — List all allowed users"
    echo ""
    echo "Per-bot allowlist commands:"
    echo "  add-bot    <bot_id> <channel:user_id> — Allow a user for a specific bot"
    echo "  remove-bot <bot_id> <channel:user_id> — Revoke user's access to a bot"
    echo "  list-bot   <bot_id>                   — List allowed users for a bot"
    echo ""
    echo "Environment:"
    echo "  AWS_REGION   — AWS region (default: us-west-2)"
    echo "  AWS_PROFILE  — AWS CLI profile (optional)"
    echo "  IDENTITY_TABLE_NAME — DynamoDB table (default: openclaw-identity)"
    exit 1
}

cmd_add() {
    local channel_key="$1"
    local now_iso
    now_iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    echo "Adding $channel_key to allowlist..."
    aws dynamodb put-item \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        $PROFILE_ARG \
        --item "{
            \"PK\": {\"S\": \"ALLOW#${channel_key}\"},
            \"SK\": {\"S\": \"ALLOW\"},
            \"channelKey\": {\"S\": \"${channel_key}\"},
            \"addedAt\": {\"S\": \"${now_iso}\"}
        }"
    echo "Done. $channel_key is now allowed."
}

cmd_remove() {
    local channel_key="$1"

    echo "Removing $channel_key from allowlist..."
    aws dynamodb delete-item \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        $PROFILE_ARG \
        --key "{
            \"PK\": {\"S\": \"ALLOW#${channel_key}\"},
            \"SK\": {\"S\": \"ALLOW\"}
        }"
    echo "Done. $channel_key is no longer allowed."
    echo "Note: If the user already has a session, they can still use it until it expires."
}

cmd_list() {
    echo "Allowed users in $TABLE_NAME ($REGION):"
    echo ""
    aws dynamodb scan \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        $PROFILE_ARG \
        --filter-expression "begins_with(PK, :prefix)" \
        --expression-attribute-values '{":prefix": {"S": "ALLOW#"}}' \
        --query 'Items[].{User: channelKey.S, AddedAt: addedAt.S}' \
        --output table
}

# ---------------------------------------------------------------------------
# Per-bot allowlist commands (BOT_ALLOW# / BOT_META# records)
# ---------------------------------------------------------------------------

cmd_add_bot() {
    local bot_id="$1"
    local channel_key="$2"
    local now_iso
    now_iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    echo "Adding $channel_key to bot '$bot_id' allowlist..."

    # Add the user entry
    aws dynamodb put-item \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        $PROFILE_ARG \
        --item "{
            \"PK\": {\"S\": \"BOT_ALLOW#${bot_id}#${channel_key}\"},
            \"SK\": {\"S\": \"BOT_ALLOW\"},
            \"botId\": {\"S\": \"${bot_id}\"},
            \"channelKey\": {\"S\": \"${channel_key}\"},
            \"addedAt\": {\"S\": \"${now_iso}\"}
        }"

    # Mark the bot as restricted (BOT_META record)
    aws dynamodb put-item \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        $PROFILE_ARG \
        --item "{
            \"PK\": {\"S\": \"BOT_META#${bot_id}\"},
            \"SK\": {\"S\": \"BOT_META\"},
            \"botId\": {\"S\": \"${bot_id}\"},
            \"restricted\": {\"BOOL\": true},
            \"updatedAt\": {\"S\": \"${now_iso}\"}
        }"

    echo "Done. $channel_key is now allowed for bot '$bot_id'."
}

cmd_remove_bot() {
    local bot_id="$1"
    local channel_key="$2"

    echo "Removing $channel_key from bot '$bot_id' allowlist..."
    aws dynamodb delete-item \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        $PROFILE_ARG \
        --key "{
            \"PK\": {\"S\": \"BOT_ALLOW#${bot_id}#${channel_key}\"},
            \"SK\": {\"S\": \"BOT_ALLOW\"}
        }"

    # Check if any entries remain — if not, remove the restricted flag
    local remaining
    remaining=$(aws dynamodb scan \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        $PROFILE_ARG \
        --filter-expression "begins_with(PK, :prefix) AND SK = :sk" \
        --expression-attribute-values "{\":prefix\": {\"S\": \"BOT_ALLOW#${bot_id}#\"}, \":sk\": {\"S\": \"BOT_ALLOW\"}}" \
        --select COUNT \
        --query 'Count' \
        --output text 2>/dev/null || echo "0")

    if [ "$remaining" = "0" ]; then
        echo "No remaining entries — removing restricted flag for bot '$bot_id'."
        aws dynamodb delete-item \
            --table-name "$TABLE_NAME" \
            --region "$REGION" \
            $PROFILE_ARG \
            --key "{
                \"PK\": {\"S\": \"BOT_META#${bot_id}\"},
                \"SK\": {\"S\": \"BOT_META\"}
            }"
    fi

    echo "Done. $channel_key removed from bot '$bot_id' allowlist."
}

cmd_list_bot() {
    local bot_id="$1"
    echo "Allowed users for bot '$bot_id' in $TABLE_NAME ($REGION):"
    echo ""
    aws dynamodb scan \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        $PROFILE_ARG \
        --filter-expression "begins_with(PK, :prefix) AND SK = :sk" \
        --expression-attribute-values "{\":prefix\": {\"S\": \"BOT_ALLOW#${bot_id}#\"}, \":sk\": {\"S\": \"BOT_ALLOW\"}}" \
        --query 'Items[].{User: channelKey.S, AddedAt: addedAt.S}' \
        --output table
}

# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------

if [ $# -lt 1 ]; then
    usage
fi

case "$1" in
    add)
        [ $# -lt 2 ] && usage
        cmd_add "$2"
        ;;
    remove)
        [ $# -lt 2 ] && usage
        cmd_remove "$2"
        ;;
    list)
        cmd_list
        ;;
    add-bot)
        [ $# -lt 3 ] && usage
        cmd_add_bot "$2" "$3"
        ;;
    remove-bot)
        [ $# -lt 3 ] && usage
        cmd_remove_bot "$2" "$3"
        ;;
    list-bot)
        [ $# -lt 2 ] && usage
        cmd_list_bot "$2"
        ;;
    *)
        usage
        ;;
esac
