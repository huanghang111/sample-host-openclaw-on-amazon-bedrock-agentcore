#!/usr/bin/env python3
"""OpenClaw on AgentCore Runtime — CDK Application entry point.

Architecture: Per-user AgentCore Runtime sessions with webhook-based
channel ingestion via Router Lambda. No keepalive needed — sessions
idle-terminate naturally.

Hybrid deployment model:
  Phase 1 (CDK): VPC, Security, AgentCore-base (Role/SG/S3), Observability
  Phase 2 (Starter Toolkit): Runtime, Endpoint, ECR, Docker build
  Phase 3 (CDK): Router, Cron, TokenMonitoring (needs runtime_id/endpoint_id)
"""

import os

import aws_cdk as cdk
import cdk_nag

from stacks.vpc_stack import VpcStack
from stacks.security_stack import SecurityStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.router_stack import RouterStack
from stacks.guardrails_stack import GuardrailsStack
from stacks.cron_stack import CronStack
from stacks.ws_bridge_stack import WsBridgeStack
from stacks.observability_stack import ObservabilityStack
from stacks.token_monitoring_stack import TokenMonitoringStack

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION"),
)

# --- Foundation ---
vpc_stack = VpcStack(app, "OpenClawVpc", env=env)

security_stack = SecurityStack(app, "OpenClawSecurity", env=env)

# --- Guardrails (Bedrock content filtering — opt-in via enable_guardrails) ---
guardrails_stack = GuardrailsStack(
    app,
    "OpenClawGuardrails",
    cmk_arn=security_stack.cmk.key_arn,
    env=env,
)

# --- AgentCore base resources (Role, SG, S3) ---
# Runtime/Endpoint created by Starter Toolkit; runtime_id/endpoint_id
# injected via cdk.json context after `agentcore deploy`.
agentcore_stack = AgentCoreStack(
    app,
    "OpenClawAgentCore",
    cmk_arn=security_stack.cmk.key_arn,
    vpc=vpc_stack.vpc,
    private_subnet_ids=[s.subnet_id for s in vpc_stack.vpc.private_subnets],
    cognito_issuer_url=security_stack.cognito_issuer_url,
    cognito_client_id=security_stack.user_pool_client_id,
    cognito_user_pool_id=security_stack.user_pool_id,
    cognito_password_secret_name=security_stack.cognito_password_secret.secret_name,
    gateway_token_secret_name=security_stack.gateway_token_secret.secret_name,
    guardrail_id=guardrails_stack.guardrail_id or "",
    guardrail_version=guardrails_stack.guardrail_version or "",
    env=env,
)

# --- Router (Lambda + API Gateway HTTP API for Telegram/Slack webhooks) ---
router_stack = RouterStack(
    app,
    "OpenClawRouter",
    runtime_arn=agentcore_stack.runtime_arn,
    runtime_endpoint_id=agentcore_stack.runtime_endpoint_id,
    gateway_token_secret_name=security_stack.gateway_token_secret.secret_name,
    telegram_token_secret_name=security_stack.channel_secrets["telegram"].secret_name,
    slack_token_secret_name=security_stack.channel_secrets["slack"].secret_name,
    feishu_token_secret_name=security_stack.channel_secrets["feishu"].secret_name,
    webhook_secret_name=security_stack.webhook_secret.secret_name,
    cmk_arn=security_stack.cmk.key_arn,
    user_files_bucket_name=agentcore_stack.user_files_bucket.bucket_name,
    user_files_bucket_arn=agentcore_stack.user_files_bucket.bucket_arn,
    env=env,
)

# --- Cron (EventBridge Scheduler + Lambda executor) ---
# Use deterministic string ARNs for identity table to avoid cyclic dependency
_region = env.region or os.environ.get("CDK_DEFAULT_REGION", "us-west-2")
_account = env.account or os.environ.get("CDK_DEFAULT_ACCOUNT", "")
_identity_table_name = "openclaw-identity"
_identity_table_arn = f"arn:aws:dynamodb:{_region}:{_account}:table/{_identity_table_name}"

cron_stack = CronStack(
    app,
    "OpenClawCron",
    runtime_arn=agentcore_stack.runtime_arn,
    runtime_endpoint_id=agentcore_stack.runtime_endpoint_id,
    identity_table_name=_identity_table_name,
    identity_table_arn=_identity_table_arn,
    telegram_token_secret_name=security_stack.channel_secrets["telegram"].secret_name,
    slack_token_secret_name=security_stack.channel_secrets["slack"].secret_name,
    feishu_token_secret_name=security_stack.channel_secrets["feishu"].secret_name,
    dingtalk_token_secret_name=security_stack.channel_secrets["dingtalk"].secret_name,
    ws_bridge_bots_secret_name=security_stack.ws_bridge_bots_secret.secret_name,
    cmk_arn=security_stack.cmk.key_arn,
    agentcore_execution_role=agentcore_stack.execution_role,
    env=env,
)

# --- WS Bridge (ECS Fargate — multi-bot DingTalk + Feishu WebSocket) ---
# Replaces the legacy single-bot DingTalkStack.
# Opt-in via ws_bridge_enabled in cdk.json context.
_ws_bridge_enabled = str(app.node.try_get_context("ws_bridge_enabled") or "false").lower() == "true"
ws_bridge_stack = None
if _ws_bridge_enabled:
    ws_bridge_stack = WsBridgeStack(
        app,
        "OpenClawWsBridge",
        vpc=vpc_stack.vpc,
        private_subnet_ids=[s.subnet_id for s in vpc_stack.vpc.private_subnets],
        vpce_security_group_id=vpc_stack.vpce_sg.security_group_id,
        runtime_arn=agentcore_stack.runtime_arn,
        runtime_endpoint_id=agentcore_stack.runtime_endpoint_id,
        identity_table_name=_identity_table_name,
        identity_table_arn=_identity_table_arn,
        ws_bridge_bots_secret_name=security_stack.ws_bridge_bots_secret.secret_name,
        cmk_arn=security_stack.cmk.key_arn,
        user_files_bucket_name=agentcore_stack.user_files_bucket.bucket_name,
        user_files_bucket_arn=agentcore_stack.user_files_bucket.bucket_arn,
        env=env,
    )

# --- Observability (dashboards + alarms) ---
observability_stack = ObservabilityStack(
    app,
    "OpenClawObservability",
    cmk_arn=security_stack.cmk.key_arn,
    env=env,
)

# --- Token Monitoring ---
token_monitoring_stack = TokenMonitoringStack(
    app,
    "OpenClawTokenMonitoring",
    invocation_log_group=observability_stack.invocation_log_group,
    alarm_topic=observability_stack.alarm_topic,
    cmk_arn=security_stack.cmk.key_arn,
    env=env,
)

# --- cdk-nag security checks ---
cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks(verbose=True))

app.synth()
