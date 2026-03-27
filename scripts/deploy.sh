#!/usr/bin/env bash
# deploy.sh — Hybrid deployment: CDK + AgentCore Starter Toolkit.
#
# Three-phase deployment:
#   Phase 1: CDK deploys foundation (VPC, Security, AgentCore base, Observability)
#   Phase 2: Starter Toolkit deploys Runtime (ECR, Docker build via CodeBuild, Runtime, Endpoint)
#   Phase 3: CDK deploys dependent stacks (Router, Cron, DingTalk, TokenMonitoring)
#
# Usage:
#   ./scripts/deploy.sh                  # full 3-phase deploy
#   ./scripts/deploy.sh --cdk-only       # CDK stacks only (skip toolkit)
#   ./scripts/deploy.sh --runtime-only   # toolkit deploy only (Phase 2)
#   ./scripts/deploy.sh --phase1         # Phase 1 only
#   ./scripts/deploy.sh --phase3         # Phase 3 only (assumes runtime already deployed)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Resolve account and region
ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)}"
REGION="${CDK_DEFAULT_REGION:-$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('region','us-west-2'))")}"

if [ -z "$ACCOUNT" ]; then
  echo "ERROR: Could not determine AWS account. Set CDK_DEFAULT_ACCOUNT or configure AWS CLI."
  exit 1
fi

export CDK_DEFAULT_ACCOUNT="$ACCOUNT"
export CDK_DEFAULT_REGION="$REGION"

# Agentcore CLI path — check PATH, .venv, ~/.local/bin in order
AGENTCORE_CLI="${AGENTCORE_CLI:-}"
if [ -z "$AGENTCORE_CLI" ]; then
  if command -v agentcore &>/dev/null; then
    AGENTCORE_CLI="agentcore"
  elif [ -x "$PROJECT_DIR/.venv/bin/agentcore" ]; then
    AGENTCORE_CLI="$PROJECT_DIR/.venv/bin/agentcore"
  elif [ -x "$HOME/.local/bin/agentcore" ]; then
    AGENTCORE_CLI="$HOME/.local/bin/agentcore"
  else
    AGENTCORE_CLI="agentcore"  # fall through — will error at phase 2 with helpful message
  fi
fi

echo "=== OpenClaw Hybrid Deploy ==="
echo "  Account: $ACCOUNT"
echo "  Region:  $REGION"
echo ""

MODE="${1:-full}"

activate_venv() {
  if [ -f "$PROJECT_DIR/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$PROJECT_DIR/.venv/bin/activate"
  fi
}

# --- Pre-deploy: clean up orphaned resources that block CloudFormation ---
cleanup_orphaned_resources() {
  echo "--- Checking for orphaned resources ---"
  # CloudWatch dashboards are not deleted by CloudFormation stack deletion,
  # causing "already exists" early validation errors on fresh deploys.
  for dashboard in "OpenClaw-Operations" "OpenClaw-Token-Analytics"; do
    if aws cloudwatch get-dashboard --dashboard-name "$dashboard" --region "$REGION" &>/dev/null; then
      echo "  Deleting orphaned dashboard: $dashboard"
      aws cloudwatch delete-dashboards --dashboard-names "$dashboard" --region "$REGION"
    fi
  done

  # Log groups with explicit names survive stack deletion (RemovalPolicy.RETAIN or DELETE_SKIPPED).
  # Only delete if the owning stack doesn't exist — otherwise they contain live logs.
  if ! aws cloudformation describe-stacks --stack-name OpenClawRouter --region "$REGION" &>/dev/null; then
    for loggroup in "/openclaw/api-access" "/openclaw/lambda/router" "/openclaw/lambda/cron"; do
      if aws logs describe-log-groups --log-group-name-prefix "$loggroup" --region "$REGION" \
         --query "logGroups[?logGroupName=='$loggroup'].logGroupName" --output text 2>/dev/null | grep -q .; then
        echo "  Deleting orphaned log group: $loggroup"
        aws logs delete-log-group --log-group-name "$loggroup" --region "$REGION"
      fi
    done
  fi
  if ! aws cloudformation describe-stacks --stack-name OpenClawDingTalk --region "$REGION" &>/dev/null; then
    local DT_LOG="/openclaw/dingtalk-bridge"
    if aws logs describe-log-groups --log-group-name-prefix "$DT_LOG" --region "$REGION" \
       --query "logGroups[?logGroupName=='$DT_LOG'].logGroupName" --output text 2>/dev/null | grep -q .; then
      echo "  Deleting orphaned log group: $DT_LOG"
      aws logs delete-log-group --log-group-name "$DT_LOG" --region "$REGION"
    fi
  fi

  # DynamoDB table with explicit name survives stack deletion.
  # Only delete if the owning stack (OpenClawRouter) doesn't exist — otherwise it's live data.
  if ! aws cloudformation describe-stacks --stack-name OpenClawRouter --region "$REGION" &>/dev/null; then
    if aws dynamodb describe-table --table-name openclaw-identity --region "$REGION" &>/dev/null; then
      echo "  Deleting orphaned DynamoDB table: openclaw-identity"
      aws dynamodb delete-table --table-name openclaw-identity --region "$REGION" > /dev/null
      aws dynamodb wait table-not-exists --table-name openclaw-identity --region "$REGION"
    fi
  fi

  # S3 bucket with explicit name may survive (non-empty buckets are retained).
  # Only delete if the owning stack (OpenClawAgentCore) doesn't exist — otherwise it's live data.
  local BUCKET="openclaw-user-files-${ACCOUNT}-${REGION}"
  if ! aws cloudformation describe-stacks --stack-name OpenClawAgentCore --region "$REGION" &>/dev/null; then
    if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" &>/dev/null; then
      echo "  Deleting orphaned S3 bucket: $BUCKET"
      python3 -c "
import boto3
s3 = boto3.resource('s3', region_name='$REGION')
bucket = s3.Bucket('$BUCKET')
bucket.object_versions.all().delete()
bucket.delete()
"
    fi
  fi
}

# --- Ensure CDK bootstrap is up to date ---
ensure_bootstrap() {
  BOOTSTRAP_VERSION=$(aws ssm get-parameter \
    --name /cdk-bootstrap/hnb659fds/version \
    --region "$REGION" \
    --query 'Parameter.Value' --output text 2>/dev/null || echo "0")
  if [ "$BOOTSTRAP_VERSION" -lt 30 ] 2>/dev/null; then
    echo "--- CDK bootstrap version $BOOTSTRAP_VERSION < 30, upgrading ---"
    cdk bootstrap "aws://$ACCOUNT/$REGION"
  fi
}

# --- Verify S3 user-files bucket exists with correct config ---
verify_s3_bucket() {
  local BUCKET="openclaw-user-files-${ACCOUNT}-${REGION}"
  local CMK_ARN
  CMK_ARN=$(aws cloudformation describe-stacks --stack-name OpenClawSecurity --region "$REGION" \
    --query "Stacks[0].Outputs[?contains(OutputKey,'SecretsCmk')].OutputValue" --output text 2>/dev/null || true)
  local TTL_DAYS
  TTL_DAYS=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('user_files_ttl_days','365'))" 2>/dev/null || echo "365")

  local CREATED=false
  if ! aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" &>/dev/null; then
    echo "  [WARNING] S3 bucket $BUCKET missing — recreating"
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION" > /dev/null
    CREATED=true
  fi

  # Always verify/repair config — bucket may exist but with wrong settings
  local NEEDS_FIX=false

  # 1. KMS encryption (CDK: aws:kms with CMK)
  if [ -n "$CMK_ARN" ]; then
    local CURRENT_ENC
    CURRENT_ENC=$(aws s3api get-bucket-encryption --bucket "$BUCKET" --region "$REGION" \
      --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' \
      --output text 2>/dev/null || echo "NONE")
    if [ "$CURRENT_ENC" != "aws:kms" ]; then
      echo "  Fixing bucket encryption: $CURRENT_ENC → aws:kms"
      aws s3api put-bucket-encryption --bucket "$BUCKET" --region "$REGION" \
        --server-side-encryption-configuration \
        "{\"Rules\":[{\"ApplyServerSideEncryptionByDefault\":{\"SSEAlgorithm\":\"aws:kms\",\"KMSMasterKeyID\":\"$CMK_ARN\"},\"BucketKeyEnabled\":true}]}"
      NEEDS_FIX=true
    fi
  fi

  # 2. Versioning (CDK: enabled)
  local CURRENT_VER
  CURRENT_VER=$(aws s3api get-bucket-versioning --bucket "$BUCKET" --region "$REGION" \
    --query 'Status' --output text 2>/dev/null || echo "NONE")
  if [ "$CURRENT_VER" != "Enabled" ]; then
    echo "  Fixing bucket versioning: $CURRENT_VER → Enabled"
    aws s3api put-bucket-versioning --bucket "$BUCKET" --region "$REGION" \
      --versioning-configuration Status=Enabled
    NEEDS_FIX=true
  fi

  # 3. Block public access (CDK: all blocked)
  local PUBLIC_BLOCK
  PUBLIC_BLOCK=$(aws s3api get-public-access-block --bucket "$BUCKET" --region "$REGION" \
    --query 'PublicAccessBlockConfiguration.BlockPublicAcls' --output text 2>/dev/null || echo "false")
  if [ "$PUBLIC_BLOCK" != "True" ]; then
    echo "  Fixing bucket public access block"
    aws s3api put-public-access-block --bucket "$BUCKET" --region "$REGION" \
      --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
    NEEDS_FIX=true
  fi

  # 4. Enforce SSL bucket policy (CDK: enforce_ssl=True)
  local HAS_POLICY
  HAS_POLICY=$(aws s3api get-bucket-policy --bucket "$BUCKET" --region "$REGION" \
    --query 'Policy' --output text 2>/dev/null || echo "")
  if [ -z "$HAS_POLICY" ] || ! echo "$HAS_POLICY" | grep -q "aws:SecureTransport"; then
    echo "  Fixing bucket SSL enforcement policy"
    local POLICY
    POLICY=$(cat <<POLICYEOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "EnforceSSL",
    "Effect": "Deny",
    "Principal": "*",
    "Action": "s3:*",
    "Resource": ["arn:aws:s3:::${BUCKET}", "arn:aws:s3:::${BUCKET}/*"],
    "Condition": {"Bool": {"aws:SecureTransport": "false"}}
  }]
}
POLICYEOF
    )
    aws s3api put-bucket-policy --bucket "$BUCKET" --region "$REGION" --policy "$POLICY"
    NEEDS_FIX=true
  fi

  # 5. Lifecycle rule (CDK: expire after TTL_DAYS)
  local HAS_LIFECYCLE
  HAS_LIFECYCLE=$(aws s3api get-bucket-lifecycle-configuration --bucket "$BUCKET" --region "$REGION" \
    --query 'Rules[0].ID' --output text 2>/dev/null || echo "")
  if [ -z "$HAS_LIFECYCLE" ] || [ "$HAS_LIFECYCLE" = "None" ]; then
    echo "  Fixing bucket lifecycle rule (expire after ${TTL_DAYS} days)"
    aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --region "$REGION" \
      --lifecycle-configuration \
      "{\"Rules\":[{\"ID\":\"expire-old-user-files\",\"Status\":\"Enabled\",\"Expiration\":{\"Days\":$TTL_DAYS},\"Filter\":{\"Prefix\":\"\"}}]}"
    NEEDS_FIX=true
  fi

  if [ "$CREATED" = true ]; then
    echo "  Bucket created and configured: $BUCKET"
  elif [ "$NEEDS_FIX" = true ]; then
    echo "  Bucket config repaired: $BUCKET"
  else
    echo "  Bucket OK: $BUCKET"
  fi
}

# --- Phase 1: CDK foundation stacks ---
phase1_cdk() {
  echo "=== Phase 1: CDK foundation stacks ==="
  cd "$PROJECT_DIR"
  activate_venv

  ensure_bootstrap
  cleanup_orphaned_resources

  cdk deploy \
    OpenClawVpc \
    OpenClawSecurity \
    OpenClawAgentCore \
    OpenClawObservability \
    --require-approval never

  # Verify S3 user-files bucket exists and has correct config.
  # RETAIN policy means CDK won't recreate if deleted externally, and manual recreation
  # can leave the bucket without KMS/versioning/policies — causing InvalidToken errors
  # on presigned URLs and breaking file delivery.
  verify_s3_bucket

  echo "  Phase 1 complete."
  echo ""
}

# --- Read CDK outputs for toolkit config ---
read_cdk_outputs() {
  echo "--- Reading CDK outputs ---"

  EXECUTION_ROLE_ARN=$(aws cloudformation describe-stacks \
    --stack-name OpenClawAgentCore --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='ExecutionRoleArn'].OutputValue" \
    --output text)

  SECURITY_GROUP_ID=$(aws cloudformation describe-stacks \
    --stack-name OpenClawAgentCore --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='SecurityGroupId'].OutputValue" \
    --output text)

  PRIVATE_SUBNET_IDS=$(aws cloudformation describe-stacks \
    --stack-name OpenClawAgentCore --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='PrivateSubnetIds'].OutputValue" \
    --output text)

  USER_FILES_BUCKET=$(aws cloudformation describe-stacks \
    --stack-name OpenClawAgentCore --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='UserFilesBucketName'].OutputValue" \
    --output text)

  GATEWAY_TOKEN_SECRET=$(aws cloudformation describe-stacks \
    --stack-name OpenClawSecurity --region "$REGION" \
    --query "Stacks[0].Outputs[?contains(OutputKey,'GatewayTokenSecret')].OutputValue" \
    --output text)
  # Extract secret name from ARN (last segment after last colon, strip random suffix)
  GATEWAY_TOKEN_SECRET_ID="openclaw/gateway-token"

  COGNITO_USER_POOL_ID=$(aws cloudformation describe-stacks \
    --stack-name OpenClawSecurity --region "$REGION" \
    --query "Stacks[0].Outputs[?contains(OutputKey,'IdentityPoolEC8A1A0D')].OutputValue" \
    --output text)

  COGNITO_CLIENT_ID=$(aws cloudformation describe-stacks \
    --stack-name OpenClawSecurity --region "$REGION" \
    --query "Stacks[0].Outputs[?contains(OutputKey,'IdentityPoolProxyClient')].OutputValue" \
    --output text)

  COGNITO_PASSWORD_SECRET_ID="openclaw/cognito-password-secret"

  CMK_ARN=$(aws cloudformation describe-stacks \
    --stack-name OpenClawSecurity --region "$REGION" \
    --query "Stacks[0].Outputs[?contains(OutputKey,'SecretsCmk')].OutputValue" \
    --output text)

  # Read config values from cdk.json
  DEFAULT_MODEL_ID=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('default_model_id','global.anthropic.claude-opus-4-6-v1'))")
  SUBAGENT_MODEL_ID=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('subagent_model_id',''))")
  IMAGE_VERSION=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('image_version','1'))")
  WORKSPACE_SYNC_MS=$(python3 -c "import json; print(int(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('workspace_sync_interval_seconds',300))*1000)")
  CRON_LEAD_TIME=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('cron_lead_time_minutes',5))")
  SESSION_IDLE=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('session_idle_timeout',1800))")
  SESSION_MAX=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('session_max_lifetime',28800))")

  echo "  Execution Role: $EXECUTION_ROLE_ARN"
  echo "  Security Group: $SECURITY_GROUP_ID"
  echo "  Subnets:        $PRIVATE_SUBNET_IDS"
  echo "  S3 Bucket:      $USER_FILES_BUCKET"
}

# --- Phase 2: Starter Toolkit deploy ---
phase2_toolkit() {
  echo "=== Phase 2: Starter Toolkit deploy ==="
  cd "$PROJECT_DIR"
  activate_venv

  # Re-resolve agentcore CLI after venv activation (may now be in PATH)
  if ! command -v "$AGENTCORE_CLI" &>/dev/null; then
    if command -v agentcore &>/dev/null; then
      AGENTCORE_CLI="agentcore"
    else
      echo "ERROR: agentcore CLI not found. Install with: pip install bedrock-agentcore-starter-toolkit"
      exit 1
    fi
  fi

  read_cdk_outputs

  # Configure the agent (creates/updates .bedrock_agentcore.yaml)
  echo "--- Configuring agent ---"
  "$AGENTCORE_CLI" configure \
    --name openclaw_agent \
    --entrypoint bridge/agentcore-contract.js \
    --execution-role "$EXECUTION_ROLE_ARN" \
    --region "$REGION" \
    --vpc \
    --subnets "$PRIVATE_SUBNET_IDS" \
    --security-groups "$SECURITY_GROUP_ID" \
    --idle-timeout "$SESSION_IDLE" \
    --max-lifetime "$SESSION_MAX" \
    --deployment-type container \
    --non-interactive

  # Fix: agentcore configure expands source_path to project root, but the
  # bridge/Dockerfile COPY commands are relative to bridge/. Generate a modified
  # Dockerfile with bridge/-prefixed paths for CodeBuild (project root context).
  echo "--- Generating CodeBuild-compatible Dockerfile ---"
  TOOLKIT_DOCKERFILE="$PROJECT_DIR/.bedrock_agentcore/openclaw_agent/Dockerfile"
  sed -e 's|^COPY agentcore-|COPY bridge/agentcore-|' \
      -e 's|^COPY lightweight-|COPY bridge/lightweight-|' \
      -e 's|^COPY workspace-sync|COPY bridge/workspace-sync|' \
      -e 's|^COPY cloudwatch-logger|COPY bridge/cloudwatch-logger|' \
      -e 's|^COPY scoped-credentials|COPY bridge/scoped-credentials|' \
      -e 's|^COPY force-ipv4|COPY bridge/force-ipv4|' \
      -e 's|^COPY skills/|COPY bridge/skills/|' \
      -e 's|^COPY CLAUDE\.md|COPY bridge/CLAUDE.md|' \
      -e 's|^COPY entrypoint|COPY bridge/entrypoint|' \
      "$PROJECT_DIR/bridge/Dockerfile" > "$TOOLKIT_DOCKERFILE"
  echo "  Dockerfile written to $TOOLKIT_DOCKERFILE"

  # Deploy with environment variables (CodeBuild, default mode)
  echo "--- Deploying runtime ---"
  "$AGENTCORE_CLI" deploy \
    --agent openclaw_agent \
    --auto-update-on-conflict \
    --env "AWS_REGION=$REGION" \
    --env "BEDROCK_MODEL_ID=$DEFAULT_MODEL_ID" \
    --env "GATEWAY_TOKEN_SECRET_ID=$GATEWAY_TOKEN_SECRET_ID" \
    --env "COGNITO_USER_POOL_ID=$COGNITO_USER_POOL_ID" \
    --env "COGNITO_CLIENT_ID=$COGNITO_CLIENT_ID" \
    --env "COGNITO_PASSWORD_SECRET_ID=$COGNITO_PASSWORD_SECRET_ID" \
    --env "S3_USER_FILES_BUCKET=$USER_FILES_BUCKET" \
    --env "WORKSPACE_SYNC_INTERVAL_MS=$WORKSPACE_SYNC_MS" \
    --env "IMAGE_VERSION=$IMAGE_VERSION" \
    --env "EXECUTION_ROLE_ARN=$EXECUTION_ROLE_ARN" \
    --env "CMK_ARN=$CMK_ARN" \
    --env "EVENTBRIDGE_SCHEDULE_GROUP=openclaw-cron" \
    --env "CRON_LAMBDA_ARN=arn:aws:lambda:${REGION}:${ACCOUNT}:function:openclaw-cron-executor" \
    --env "EVENTBRIDGE_ROLE_ARN=arn:aws:iam::${ACCOUNT}:role/openclaw-cron-scheduler-role-${REGION}" \
    --env "IDENTITY_TABLE_NAME=openclaw-identity" \
    --env "CRON_LEAD_TIME_MINUTES=$CRON_LEAD_TIME" \
    --env "SUBAGENT_BEDROCK_MODEL_ID=$SUBAGENT_MODEL_ID"

  # Read runtime ID from .bedrock_agentcore.yaml (most reliable source)
  echo "--- Reading runtime info ---"
  RUNTIME_ID=$(python3 -c "
import yaml
with open('$PROJECT_DIR/.bedrock_agentcore.yaml') as f:
    cfg = yaml.safe_load(f)
agent = cfg.get('agents', {}).get('openclaw_agent', {})
ba = agent.get('bedrock_agentcore', {})
rid = ba.get('agent_id', '') or ''
print(rid)
" 2>/dev/null || echo "")

  if [ -z "$RUNTIME_ID" ]; then
    echo "WARNING: Could not extract runtime_id from toolkit config. You may need to set it manually in cdk.json."
  else
    echo "  Runtime ID: $RUNTIME_ID"
  fi

  # Get endpoint ID via control plane API
  ENDPOINT_ID=""
  if [ -n "$RUNTIME_ID" ]; then
    ENDPOINT_ID=$(aws bedrock-agentcore-control list-agent-runtime-endpoints \
      --agent-runtime-id "$RUNTIME_ID" \
      --region "$REGION" \
      --query 'runtimeEndpoints[0].id' \
      --output text 2>/dev/null || echo "")
    # AWS CLI returns "None" for null values — treat as empty
    if [ "$ENDPOINT_ID" = "None" ] || [ -z "$ENDPOINT_ID" ]; then
      ENDPOINT_ID="DEFAULT"
      echo "  Endpoint ID: $ENDPOINT_ID (assumed default)"
    else
      echo "  Endpoint ID: $ENDPOINT_ID"
    fi
  fi

  # Update cdk.json with runtime info
  if [ -n "$RUNTIME_ID" ] && [ -n "$ENDPOINT_ID" ]; then
    echo "--- Updating cdk.json with runtime info ---"
    python3 -c "
import json
with open('$PROJECT_DIR/cdk.json') as f:
    cfg = json.load(f)
cfg['context']['runtime_id'] = '$RUNTIME_ID'
cfg['context']['runtime_endpoint_id'] = '$ENDPOINT_ID'
with open('$PROJECT_DIR/cdk.json', 'w') as f:
    json.dump(cfg, f, indent=2)
    f.write('\n')
"
    echo "  cdk.json updated."
  fi

  echo "  Phase 2 complete."
  echo ""
}

# --- Phase 3: CDK dependent stacks ---
phase3_cdk() {
  echo "=== Phase 3: CDK dependent stacks ==="
  cd "$PROJECT_DIR"
  activate_venv

  cleanup_orphaned_resources

  # Verify runtime_id is set
  RUNTIME_ID=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('runtime_id',''))")
  if [ -z "$RUNTIME_ID" ] || [ "$RUNTIME_ID" = "PLACEHOLDER" ]; then
    echo "ERROR: runtime_id not set in cdk.json. Run Phase 2 first."
    exit 1
  fi

  cdk deploy \
    OpenClawRouter \
    OpenClawCron \
    OpenClawDingTalk \
    OpenClawTokenMonitoring \
    --require-approval never

  echo "  Phase 3 complete."
  echo ""
}

case "$MODE" in
  --phase1)
    phase1_cdk
    ;;
  --runtime-only)
    phase2_toolkit
    ;;
  --phase3)
    phase3_cdk
    ;;
  --cdk-only)
    phase1_cdk
    phase3_cdk
    ;;
  *)
    phase1_cdk
    phase2_toolkit
    phase3_cdk
    ;;
esac

echo "=== Deploy complete ==="
echo ""
echo "Next steps:"
echo "  1. Set up Telegram:  ./scripts/setup-telegram.sh"
echo "  2. Set up Slack:     ./scripts/setup-slack.sh"
echo "  3. Set up Feishu:    ./scripts/setup-feishu.sh"
echo "  4. Set up DingTalk:  ./scripts/setup-dingtalk.sh"
