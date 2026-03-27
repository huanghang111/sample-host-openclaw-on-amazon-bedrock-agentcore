"""DingTalk Stack — ECS Fargate service for DingTalk Robot integration.

Deploys a long-running Fargate task that maintains a WebSocket connection
to DingTalk via Stream mode, receives bot messages, resolves users via
DynamoDB, invokes per-user AgentCore sessions, and sends responses back.

DingTalk Robot uses client-initiated WebSocket (Stream mode) — not webhooks —
so this requires a persistent process rather than Lambda.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_logs as logs,
)
import cdk_nag
from constructs import Construct

from stacks import retention_days


class DingTalkStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        private_subnet_ids: list[str],
        vpce_security_group_id: str,
        runtime_arn: str,
        runtime_endpoint_id: str,
        identity_table_name: str,
        identity_table_arn: str,
        dingtalk_token_secret_name: str,
        cmk_arn: str,
        user_files_bucket_name: str,
        user_files_bucket_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30
        registration_open = str(self.node.try_get_context("registration_open") or "false").lower()

        # --- Log Group ---
        log_group = logs.LogGroup(
            self,
            "DingTalkBridgeLogGroup",
            log_group_name="/openclaw/dingtalk-bridge",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- VPC Endpoint for bedrock-agentcore data plane ---
        # Required because the Fargate task runs in VPC private subnets and
        # needs to call bedrock-agentcore:InvokeAgentRuntime. The Router Lambda
        # doesn't need this because it runs outside the VPC.
        vpce_sg = ec2.SecurityGroup.from_security_group_id(
            self, "VpceSG", vpce_security_group_id,
        )
        private_subnets = ec2.SubnetSelection(
            subnets=[
                ec2.Subnet.from_subnet_id(self, f"VpceSubnet{i}", sid)
                for i, sid in enumerate(private_subnet_ids)
            ],
        )
        vpc.add_interface_endpoint(
            "AgentCoreEndpoint",
            service=ec2.InterfaceVpcEndpointService(
                f"com.amazonaws.{region}.bedrock-agentcore",
                port=443,
            ),
            subnets=private_subnets,
            security_groups=[vpce_sg],
            private_dns_enabled=True,
        )

        # --- ECS Cluster ---
        cluster = ecs.Cluster(
            self,
            "DingTalkCluster",
            cluster_name="openclaw-dingtalk",
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )

        # --- Security Group (egress-only: HTTPS for DingTalk + AWS APIs) ---
        sg = ec2.SecurityGroup(
            self,
            "DingTalkBridgeSG",
            vpc=vpc,
            description="DingTalk Bridge - egress-only for DingTalk WebSocket and AWS APIs",
            allow_all_outbound=False,
        )
        sg.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS outbound")

        # --- Task Definition ---
        task_def = ecs.FargateTaskDefinition(
            self,
            "DingTalkBridgeTaskDef",
            family="openclaw-dingtalk-bridge",
            cpu=256,
            memory_limit_mib=512,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )

        # --- Container ---
        container = task_def.add_container(
            "DingTalkBridgeContainer",
            image=ecs.ContainerImage.from_asset(
                "dingtalk-bridge",
                platform=ecr_assets.Platform.LINUX_ARM64,
            ),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="dingtalk",
                log_group=log_group,
            ),
            environment={
                "AGENTCORE_RUNTIME_ARN": runtime_arn,
                "AGENTCORE_QUALIFIER": runtime_endpoint_id,
                "IDENTITY_TABLE_NAME": identity_table_name,
                "DINGTALK_SECRET_ID": dingtalk_token_secret_name,
                "USER_FILES_BUCKET": user_files_bucket_name,
                "AWS_REGION": region,
                "REGISTRATION_OPEN": registration_open,
                "HEALTH_PORT": "8080",
            },
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8080/health')\" || exit 1"],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(30),
            ),
        )
        container.add_port_mappings(ecs.PortMapping(container_port=8080))

        # --- Fargate Service ---
        self.service = ecs.FargateService(
            self,
            "DingTalkBridgeService",
            service_name="openclaw-dingtalk-bridge",
            cluster=cluster,
            task_definition=task_def,
            desired_count=1,
            assign_public_ip=False,
            security_groups=[sg],
            vpc_subnets=ec2.SubnetSelection(
                subnets=[
                    ec2.Subnet.from_subnet_id(self, f"Subnet{i}", sid)
                    for i, sid in enumerate(private_subnet_ids)
                ],
            ),
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            min_healthy_percent=0,  # allow replacing the single task during deploy
            max_healthy_percent=100,
        )

        # --- IAM Permissions (task role) ---
        task_role = task_def.task_role

        # AgentCore invocation
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                resources=[runtime_arn, f"{runtime_arn}/*"],
            )
        )

        # DynamoDB read/write (identity table)
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem", "dynamodb:PutItem",
                    "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query",
                ],
                resources=[identity_table_arn, f"{identity_table_arn}/index/*"],
            )
        )

        # Secrets Manager (DingTalk credentials)
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                resources=[f"arn:aws:secretsmanager:{region}:{account}:secret:openclaw/*"],
            )
        )

        # KMS decrypt for secrets
        task_role.add_to_policy(
            iam.PolicyStatement(actions=["kms:Decrypt"], resources=[cmk_arn])
        )

        # S3 PutObject for image/file uploads, GetObject+HeadObject for outbound file delivery
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[f"{user_files_bucket_arn}/*/_uploads/*"],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:HeadObject"],
                resources=[f"{user_files_bucket_arn}/*"],
            )
        )

        # KMS GenerateDataKey for S3 bucket encryption
        task_role.add_to_policy(
            iam.PolicyStatement(actions=["kms:GenerateDataKey"], resources=[cmk_arn])
        )

        # --- Outputs ---
        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "ServiceName", value=self.service.service_name)

        # --- cdk-nag suppressions ---
        cdk_nag.NagSuppressions.add_resource_suppressions(
            task_def,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="AgentCore InvokeAgentRuntime needs runtime-endpoint sub-resource "
                    "path (runtime/{id}/*). Secrets Manager scoped to openclaw/* prefix. "
                    "DynamoDB index wildcard for queries. S3 PutObject scoped to "
                    "*/_uploads/* for image uploads. ECR pull uses Resource::* by default.",
                    applies_to=[
                        f"Resource::{runtime_arn}/*",
                        f"Resource::arn:aws:secretsmanager:{region}:{account}:secret:openclaw/*",
                        f"Resource::arn:aws:dynamodb:{region}:{account}:table/{identity_table_name}/index/*",
                        f"Resource::{user_files_bucket_arn}/*/_uploads/*",
                        "Resource::<UserFilesBucketCFDFD8C0.Arn>/*/_uploads/*",
                        "Action::kms:GenerateDataKey*",
                        "Resource::*",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-ECS2",
                    reason="Environment variables contain non-sensitive configuration "
                    "(ARNs, table names, region). Secrets are fetched at runtime from "
                    "Secrets Manager, not passed as env vars.",
                ),
            ],
            apply_to_children=True,
        )
