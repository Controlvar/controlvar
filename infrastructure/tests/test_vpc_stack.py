import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from infrastructure.cdk.stacks.vpc_stack import VpcStack

@pytest.fixture
def app():
    return cdk.App()

@pytest.fixture
def stack(app):
    return VpcStack(app, "TestVpcStack")

@pytest.fixture
def template(stack):
    return Template.from_stack(stack)

def test_vpc_created(template):
    # Verify that exactly one VPC is created
    template.resource_count_is("AWS::EC2::VPC", 1)
    # Optionally, check that the VPC uses the expected CIDR/IP configuration
    template.has_resource_properties("AWS::EC2::VPC", {
        "CidrBlock": "10.0.0.0/16"  # This may appear if using ip_addresses as cidr
    })

def test_subnet_outputs(template):
    # Check that outputs for public, private, and isolated subnets exist
    template.has_output("PublicSubnets", {})
    template.has_output("PrivateSubnets", {})
    template.has_output("IsolatedSubnets", {})

def test_security_group_outputs(template):
    # Check that outputs for App, Db, and Bastion security groups exist.
    template.has_output("AppSecurityGroup", {})
    template.has_output("DbSecurityGroup", {})
    # Use the updated output ID for Bastion Security Group to avoid duplication.
    template.has_output("BastionSecurityGroupOutput", {})

def test_gateway_endpoints(template):
    # Check that a VPC endpoint for S3 exists
    template.has_resource_properties("AWS::EC2::VPCEndpoint", {
        "ServiceName": {
            "Fn::Join": [
                "",
                [
                    "com.amazonaws.",
                    { "Ref": "AWS::Region" },
                    ".s3"
                ]
            ]
        }
    })
    # Check that a VPC endpoint for DynamoDB exists
    template.has_resource_properties("AWS::EC2::VPCEndpoint", {
        "ServiceName": {
            "Fn::Join": [
                "",
                [
                    "com.amazonaws.",
                    { "Ref": "AWS::Region" },
                    ".dynamodb"
                ]
            ]
        }
    })


def _service(endpoint: dict) -> str:
    """``com.amazonaws.<region>.<service>`` with the region written REGION."""
    name = endpoint["Properties"]["ServiceName"]
    if isinstance(name, str):
        return name
    parts = name["Fn::Join"][1]
    return "".join(p if isinstance(p, str) else "REGION" for p in parts)


def test_interface_endpoints(template):
    """The AWS CDK Deployment page: Secrets Manager, ECR (API and Docker) and
    CloudWatch Logs are reached through interface endpoints with private DNS."""
    endpoints = template.find_resources("AWS::EC2::VPCEndpoint")
    interface = {
        _service(e): e["Properties"]
        for e in endpoints.values()
        if e["Properties"].get("VpcEndpointType") == "Interface"
    }
    assert sorted(interface) == [
        "com.amazonaws.REGION.ecr.api",
        "com.amazonaws.REGION.ecr.dkr",
        "com.amazonaws.REGION.logs",
        "com.amazonaws.REGION.secretsmanager",
    ], sorted(interface)
    assert all(p.get("PrivateDnsEnabled") is True for p in interface.values())


def test_subnet_layout(template):
    """The AWS CDK Deployment page: two availability zones, each with a public,
    a private and an isolated /24, numbered in that order."""
    subnets = template.find_resources("AWS::EC2::Subnet")
    by_type = {}
    for subnet in subnets.values():
        props = subnet["Properties"]
        tags = {t["Key"]: t["Value"] for t in props.get("Tags", [])}
        by_type.setdefault(tags["aws-cdk:subnet-type"], []).append(props["CidrBlock"])
    assert {k: sorted(v) for k, v in by_type.items()} == {
        "Public": ["10.0.0.0/24", "10.0.1.0/24"],
        "Private": ["10.0.2.0/24", "10.0.3.0/24"],
        "Isolated": ["10.0.4.0/24", "10.0.5.0/24"],
    }, by_type
