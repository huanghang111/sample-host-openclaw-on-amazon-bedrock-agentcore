"""AgentCore Gateway Stack — MCP Gateway with IAM authorization.

Creates an empty AgentCore Gateway. Targets are added out-of-band via CLI.
"""

from aws_cdk import (
    CfnOutput,
    Stack,
    aws_bedrockagentcore as agentcore,
    aws_iam as iam,
)
import cdk_nag
from constructs import Construct


class GatewayStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region

        self.gateway_role = iam.Role(
            self,
            "GatewayRole",
            role_name=f"openclaw-gateway-role-{region}",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        self.gateway_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetResourceApiKey",
                    "secretsmanager:GetSecretValue",
                ],
                resources=["*"],
            )
        )

        self.gateway = agentcore.CfnGateway(
            self,
            "Gateway",
            name="gateway-for-openclaw",
            authorizer_type="AWS_IAM",
            protocol_type="MCP",
            role_arn=self.gateway_role.role_arn,
            description="OpenClaw AgentCore Gateway",
        )

        CfnOutput(self, "GatewayId", value=self.gateway.attr_gateway_identifier)
        CfnOutput(self, "GatewayUrl", value=self.gateway.attr_gateway_url)

        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.gateway_role,
            [cdk_nag.NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason="GetWorkloadAccessToken does not support resource-level permissions.",
                applies_to=["Resource::*"],
            )],
            apply_to_children=True,
        )
