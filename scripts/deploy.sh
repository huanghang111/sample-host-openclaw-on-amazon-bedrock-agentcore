#!/usr/bin/env bash
# deploy.sh — Hybrid deployment: CDK + AgentCore Starter Toolkit.
#
# Three-phase deployment:
#   Phase 1: CDK deploys foundation (VPC, Security, AgentCore base, Observability)
#   Phase 2: Starter Toolkit deploys Runtime (ECR, Docker build, Runtime, Endpoint)
#   Phase 3: CDK deploys dependent stacks (Router, Cron, WsBridge, TokenMonitoring)
#
# Usage:
#   ./scripts/deploy.sh                  # full 3-phase deploy
#   ./scripts/deploy.sh --cdk-only       # CDK stacks only (skip toolkit)
#   ./scripts/deploy.sh --runtime-only   # toolkit deploy only (Phase 2)
#   ./scripts/deploy.sh --phase1         # Phase 1 only
#   ./scripts/deploy.sh --phase3         # Phase 3 only (assumes runtime already deployed)
#   ./scripts/deploy.sh --ws-bridge-only # build WS Bridge image only (via CodeBuild)
#
# Environment variables:
#   BUILD_MODE          codebuild (default) or local-build
#                       codebuild: builds in AWS CodeBuild (no local Docker required)
#                       local-build: builds ARM64 container locally with Docker
#   CDK_DEFAULT_ACCOUNT AWS account ID (auto-detected if not set)
#   CDK_DEFAULT_REGION  AWS region (falls back to cdk.json, then aws configure)
#   AGENTCORE_CLI       Path to agentcore CLI (auto-detected)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Build mode ---
BUILD_MODE="${BUILD_MODE:-codebuild}"

# --- Pre-flight checks ---
preflight() {
  local errors=0

  # AWS credentials
  if ! aws sts get-caller-identity &>/dev/null; then
    echo "ERROR: AWS credentials not configured. Run 'aws configure' or set AWS_PROFILE."
    errors=$((errors + 1))
  fi

  # CDK CLI
  if ! command -v cdk &>/dev/null; then
    echo "ERROR: AWS CDK CLI not found. Install with: npm install -g aws-cdk"
    errors=$((errors + 1))
  fi

  # Python venv
  if [ ! -f "$PROJECT_DIR/.venv/bin/activate" ]; then
    echo "ERROR: Python venv not found. Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    errors=$((errors + 1))
  fi

  # Docker (only for local-build mode)
  if [ "$BUILD_MODE" = "local-build" ]; then
    if ! command -v docker &>/dev/null; then
      echo "ERROR: Docker not found (required for BUILD_MODE=local-build). Install Docker or use default BUILD_MODE=codebuild."
      errors=$((errors + 1))
    elif ! docker info &>/dev/null 2>&1; then
      echo "ERROR: Docker daemon not running. Start Docker or use default BUILD_MODE=codebuild."
      errors=$((errors + 1))
    fi
  fi

  # Agentcore CLI
  if ! command -v "${AGENTCORE_CLI:-agentcore}" &>/dev/null && [ ! -x "$HOME/.local/bin/agentcore" ]; then
    echo "ERROR: agentcore CLI not found. Install with: pip install bedrock-agentcore-starter-toolkit==0.3.3"
    errors=$((errors + 1))
  fi

  if [ "$errors" -gt 0 ]; then
    echo ""
    echo "Fix the above errors and re-run."
    exit 1
  fi
}

# Resolve account and region
ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)}"
REGION="${CDK_DEFAULT_REGION:-}"
if [ -z "$REGION" ]; then
  REGION=$(python3 -c "import json; r=json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('region',''); print(r)" 2>/dev/null || echo "")
fi
if [ -z "$REGION" ]; then
  REGION=$(aws configure get region 2>/dev/null || echo "")
fi
if [ -z "$REGION" ]; then
  echo "ERROR: Could not determine AWS region. Set CDK_DEFAULT_REGION, configure region in cdk.json, or run 'aws configure'."
  exit 1
fi

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

# Run pre-flight checks
preflight

echo "=== OpenClaw Hybrid Deploy ==="
echo "  Account:    $ACCOUNT"
echo "  Region:     $REGION"
echo "  Build mode: $BUILD_MODE"
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
  # Clean up orphaned DingTalk bridge log group (replaced by WS Bridge)
  if ! aws cloudformation describe-stacks --stack-name OpenClawDingTalk --region "$REGION" &>/dev/null; then
    local DT_LOG="/openclaw/dingtalk-bridge"
    if aws logs describe-log-groups --log-group-name-prefix "$DT_LOG" --region "$REGION" \
       --query "logGroups[?logGroupName=='$DT_LOG'].logGroupName" --output text 2>/dev/null | grep -q .; then
      echo "  Deleting orphaned log group: $DT_LOG"
      aws logs delete-log-group --log-group-name "$DT_LOG" --region "$REGION"
    fi
  fi
  # Clean up orphaned WS Bridge log group
  if ! aws cloudformation describe-stacks --stack-name OpenClawWsBridge --region "$REGION" &>/dev/null; then
    local WS_LOG="/openclaw/ws-bridge"
    if aws logs describe-log-groups --log-group-name-prefix "$WS_LOG" --region "$REGION" \
       --query "logGroups[?logGroupName=='$WS_LOG'].logGroupName" --output text 2>/dev/null | grep -q .; then
      echo "  Deleting orphaned log group: $WS_LOG"
      aws logs delete-log-group --log-group-name "$WS_LOG" --region "$REGION"
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
    OpenClawGuardrails \
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

# --- Check ARM64 build capability (for local-build mode) ---
check_arm64_build() {
  local arch
  arch=$(uname -m)
  if [ "$arch" = "aarch64" ] || [ "$arch" = "arm64" ]; then
    return 0  # native ARM64, no QEMU needed
  fi
  # x86 host — check for ARM64 emulation via buildx/QEMU
  if docker buildx ls 2>/dev/null | grep -q "linux/arm64"; then
    return 0
  fi
  echo "WARNING: ARM64 emulation not available. Attempting to register QEMU..."
  docker run --rm --privileged tonistiigi/binfmt --install arm64 || {
    echo "ERROR: Could not set up ARM64 emulation. Install QEMU or use BUILD_MODE=codebuild."
    exit 1
  }
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
      echo "ERROR: agentcore CLI not found. Install with: pip install bedrock-agentcore-starter-toolkit==0.3.3"
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
    --language typescript \
    --non-interactive

  # Fix: agentcore configure expands source_path to project root, but our
  # Dockerfile COPY commands expect paths relative to bridge/. Patch it back.
  local yaml_file="$PROJECT_DIR/.bedrock_agentcore.yaml"
  if grep -q "source_path:.*$PROJECT_DIR$" "$yaml_file" 2>/dev/null; then
    sed -i "s|source_path: $PROJECT_DIR$|source_path: $PROJECT_DIR/bridge|" "$yaml_file"
    echo "  (patched source_path -> bridge/)"
  fi

  # Ensure the generated Dockerfile matches our actual Dockerfile
  local gen_dockerfile="$PROJECT_DIR/.bedrock_agentcore/openclaw_agent/Dockerfile"
  if [ -f "$gen_dockerfile" ] && [ -f "$PROJECT_DIR/bridge/Dockerfile" ]; then
    cp "$PROJECT_DIR/bridge/Dockerfile" "$gen_dockerfile"
    echo "  (synced Dockerfile from bridge/)"
  fi

  # Build deploy command based on BUILD_MODE
  echo "--- Deploying runtime (mode: $BUILD_MODE) ---"
  local deploy_flags=()
  if [ "$BUILD_MODE" = "local-build" ]; then
    check_arm64_build
    deploy_flags+=("--local-build")
  fi
  # codebuild mode: no extra flags (default behavior)

  "$AGENTCORE_CLI" deploy \
    --agent openclaw_agent \
    --auto-update-on-conflict \
    "${deploy_flags[@]}" \
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
  TOOLKIT_STATUS=$("$AGENTCORE_CLI" status --agent openclaw_agent --verbose 2>&1 || true)

  # Extract runtime_id from status output (handles non-JSON prefix lines from warnings)
  RUNTIME_ID=$(echo "$TOOLKIT_STATUS" | python3 -c "
import sys, re, json
text = sys.stdin.read()
# Try to find JSON object in the output
m = re.search(r'\{.*\}', text, re.DOTALL)
if m:
    try:
        data = json.loads(m.group())
        # Navigate nested structure: {config: {agent_id: ...}} or flat {agent_id: ...}
        cfg = data.get('config', data)
        rid = cfg.get('agent_id', cfg.get('runtime_id', ''))
        if rid:
            print(rid)
            sys.exit(0)
    except json.JSONDecodeError:
        pass
# Regex fallback
m = re.search(r'\"agent_id\"\s*:\s*\"([a-zA-Z0-9_-]+)\"', text)
print(m.group(1) if m else '')
" 2>/dev/null || echo "")

  # Fallback: read from .bedrock_agentcore.yaml (uses simple text parsing, no yaml dep)
  if [ -z "$RUNTIME_ID" ]; then
    RUNTIME_ID=$(python3 -c "
import re
with open('$PROJECT_DIR/.bedrock_agentcore.yaml') as f:
    text = f.read()
m = re.search(r'agent_id:\s*(\S+)', text)
print(m.group(1) if m else '')
" 2>/dev/null || echo "")
  fi

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
      --query "runtimeEndpoints[?name=='DEFAULT'].id | [0]" \
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

# --- Build WS Bridge container image via CodeBuild ---
build_ws_bridge_image() {
  local WS_ENABLED
  WS_ENABLED=$(python3 -c "import json; print(str(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('ws_bridge_enabled', False)).lower())")
  if [ "$WS_ENABLED" != "true" ]; then
    echo "--- WS Bridge disabled, skipping image build ---"
    return 0
  fi

  echo "=== Building WS Bridge image via CodeBuild ==="
  cd "$PROJECT_DIR"
  activate_venv

  local ECR_REPO="openclaw-ws-bridge"
  local ECR_URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO}"
  local CB_PROJECT="openclaw-ws-bridge-build"
  local CB_ROLE_NAME="openclaw-ws-bridge-codebuild-${REGION}"
  local CB_ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${CB_ROLE_NAME}"
  local S3_BUCKET="openclaw-user-files-${ACCOUNT}-${REGION}"
  local S3_SOURCE_KEY="_build/ws-bridge-source.zip"

  # Read CMK ARN (S3 bucket is KMS-encrypted)
  local CMK_ARN
  CMK_ARN=$(aws cloudformation describe-stacks --stack-name OpenClawSecurity --region "$REGION" \
    --query "Stacks[0].Outputs[?contains(OutputKey,'SecretsCmk')].OutputValue" --output text 2>/dev/null || true)

  # 1. Create ECR repo if not exists
  if ! aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$REGION" &>/dev/null; then
    echo "  Creating ECR repository: $ECR_REPO"
    aws ecr create-repository \
      --repository-name "$ECR_REPO" \
      --region "$REGION" \
      --image-scanning-configuration scanOnPush=true > /dev/null
  fi

  # 2. Create CodeBuild service role if not exists
  if ! aws iam get-role --role-name "$CB_ROLE_NAME" &>/dev/null 2>&1; then
    echo "  Creating CodeBuild service role: $CB_ROLE_NAME"
    aws iam create-role \
      --role-name "$CB_ROLE_NAME" \
      --assume-role-policy-document '{
        "Version": "2012-10-17",
        "Statement": [{
          "Effect": "Allow",
          "Principal": {"Service": "codebuild.amazonaws.com"},
          "Action": "sts:AssumeRole"
        }]
      }' > /dev/null

    # Build the KMS statement only if CMK_ARN is available
    local KMS_STATEMENT=""
    if [ -n "$CMK_ARN" ]; then
      KMS_STATEMENT=",{
            \"Effect\": \"Allow\",
            \"Action\": [\"kms:Decrypt\", \"kms:GenerateDataKey\"],
            \"Resource\": \"${CMK_ARN}\"
          }"
    fi

    aws iam put-role-policy \
      --role-name "$CB_ROLE_NAME" \
      --policy-name "ws-bridge-build" \
      --policy-document "{
        \"Version\": \"2012-10-17\",
        \"Statement\": [
          {
            \"Effect\": \"Allow\",
            \"Action\": [\"ecr:GetAuthorizationToken\"],
            \"Resource\": \"*\"
          },
          {
            \"Effect\": \"Allow\",
            \"Action\": [
              \"ecr:BatchCheckLayerAvailability\",
              \"ecr:GetDownloadUrlForLayer\",
              \"ecr:BatchGetImage\",
              \"ecr:PutImage\",
              \"ecr:InitiateLayerUpload\",
              \"ecr:UploadLayerPart\",
              \"ecr:CompleteLayerUpload\"
            ],
            \"Resource\": \"arn:aws:ecr:${REGION}:${ACCOUNT}:repository/${ECR_REPO}\"
          },
          {
            \"Effect\": \"Allow\",
            \"Action\": [\"s3:GetObject\", \"s3:GetBucketLocation\"],
            \"Resource\": [
              \"arn:aws:s3:::${S3_BUCKET}\",
              \"arn:aws:s3:::${S3_BUCKET}/${S3_SOURCE_KEY}\"
            ]
          },
          {
            \"Effect\": \"Allow\",
            \"Action\": [\"logs:CreateLogGroup\", \"logs:CreateLogStream\", \"logs:PutLogEvents\"],
            \"Resource\": \"arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/aws/codebuild/${CB_PROJECT}:*\"
          }${KMS_STATEMENT}
        ]
      }"

    echo "  Waiting for IAM role propagation..."
    sleep 10
  fi

  # 3. Create CodeBuild project if not exists
  local EXISTING_PROJECT
  EXISTING_PROJECT=$(aws codebuild batch-get-projects --names "$CB_PROJECT" --region "$REGION" \
    --query "projects[0].name" --output text 2>/dev/null || echo "")
  if [ "$EXISTING_PROJECT" != "$CB_PROJECT" ]; then
    echo "  Creating CodeBuild project: $CB_PROJECT"
    aws codebuild create-project \
      --name "$CB_PROJECT" \
      --region "$REGION" \
      --source "{
        \"type\": \"S3\",
        \"location\": \"${S3_BUCKET}/${S3_SOURCE_KEY}\"
      }" \
      --environment "{
        \"type\": \"ARM_CONTAINER\",
        \"image\": \"aws/codebuild/amazonlinux-aarch64-standard:3.0\",
        \"computeType\": \"BUILD_GENERAL1_SMALL\",
        \"privilegedMode\": true,
        \"environmentVariables\": [
          {\"name\": \"ECR_URI\", \"value\": \"${ECR_URI}\"},
          {\"name\": \"IMAGE_TAG\", \"value\": \"latest\"}
        ]
      }" \
      --artifacts '{"type": "NO_ARTIFACTS"}' \
      --service-role "$CB_ROLE_ARN" > /dev/null
  fi

  # 4. Zip ws-bridge/ source and upload to S3
  echo "  Packaging ws-bridge source..."
  local TMPZIP
  TMPZIP=$(mktemp /tmp/ws-bridge-source-XXXXX.zip)
  python3 -c "
import zipfile, os
with zipfile.ZipFile('$TMPZIP', 'w', zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk('$PROJECT_DIR/ws-bridge'):
        dirs[:] = [d for d in dirs if d not in ('__pycache__', '.pytest_cache', '*.egg-info')]
        for f in files:
            if not f.endswith('.pyc'):
                filepath = os.path.join(root, f)
                arcname = os.path.relpath(filepath, '$PROJECT_DIR/ws-bridge')
                z.write(filepath, arcname)
"

  # Compute content-based image tag for CDK change detection
  local IMAGE_TAG
  IMAGE_TAG="build-$(sha256sum "$TMPZIP" | cut -c1-8)"

  # Check if this exact image already exists in ECR — skip build if so
  if aws ecr describe-images --repository-name "$ECR_REPO" --image-ids "imageTag=$IMAGE_TAG" \
       --region "$REGION" &>/dev/null; then
    echo "  Image $ECR_URI:$IMAGE_TAG already exists, skipping build."
    rm -f "$TMPZIP"
  else
    echo "  Uploading source to s3://${S3_BUCKET}/${S3_SOURCE_KEY}..."
    aws s3 cp "$TMPZIP" "s3://${S3_BUCKET}/${S3_SOURCE_KEY}" --region "$REGION" > /dev/null
    rm -f "$TMPZIP"

    # 5. Start CodeBuild
    echo "  Starting CodeBuild (ARM64)..."
    local BUILD_ID
    BUILD_ID=$(aws codebuild start-build \
      --project-name "$CB_PROJECT" \
      --region "$REGION" \
      --buildspec-override "$(cat <<'BUILDSPEC'
version: 0.2
phases:
  pre_build:
    commands:
      - REGISTRY=$(echo $ECR_URI | cut -d/ -f1)
      - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $REGISTRY
  build:
    commands:
      - docker build --platform linux/arm64 -t $ECR_URI:$IMAGE_TAG .
      - docker tag $ECR_URI:$IMAGE_TAG $ECR_URI:latest
  post_build:
    commands:
      - docker push $ECR_URI:$IMAGE_TAG
      - docker push $ECR_URI:latest
BUILDSPEC
)" \
      --environment-variables-override "[
        {\"name\":\"ECR_URI\",\"value\":\"${ECR_URI}\",\"type\":\"PLAINTEXT\"},
        {\"name\":\"IMAGE_TAG\",\"value\":\"${IMAGE_TAG}\",\"type\":\"PLAINTEXT\"}
      ]" \
      --query 'build.id' --output text)
    echo "  Build ID: $BUILD_ID"

    # 6. Wait for build completion
    echo "  Waiting for CodeBuild to finish..."
    local STATUS=""
    while true; do
      STATUS=$(aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$REGION" \
        --query 'builds[0].buildStatus' --output text)
      case "$STATUS" in
        SUCCEEDED)
          echo "  Build succeeded."
          break
          ;;
        FAILED|FAULT|TIMED_OUT|STOPPED)
          echo "  ERROR: Build $STATUS. Check logs: /aws/codebuild/$CB_PROJECT"
          exit 1
          ;;
        *)
          sleep 15
          ;;
      esac
    done
  fi

  # 7. Update cdk.json with image tag for CDK change detection
  python3 -c "
import json
with open('$PROJECT_DIR/cdk.json') as f:
    cfg = json.load(f)
cfg['context']['ws_bridge_image_tag'] = '$IMAGE_TAG'
with open('$PROJECT_DIR/cdk.json', 'w') as f:
    json.dump(cfg, f, indent=2)
    f.write('\n')
"
  echo "  Image: $ECR_URI:$IMAGE_TAG"
  echo "  WS Bridge image build complete."
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
    OpenClawWsBridge \
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
  --ws-bridge-only)
    build_ws_bridge_image
    ;;
  --phase3)
    build_ws_bridge_image
    phase3_cdk
    ;;
  --cdk-only)
    phase1_cdk
    build_ws_bridge_image
    phase3_cdk
    ;;
  *)
    phase1_cdk
    phase2_toolkit
    build_ws_bridge_image
    phase3_cdk
    ;;
esac

echo "=== Deploy complete ==="
echo ""
echo "Next steps:"
echo "  1. Set up Telegram:          ./scripts/setup-telegram.sh"
echo "  2. Set up Slack:             ./scripts/setup-slack.sh"
echo "  3. Set up Feishu (webhook):  ./scripts/setup-feishu.sh"
echo "  4. Set up DingTalk/Feishu (WS multi-bot): ./scripts/setup-multi-bot.sh"
