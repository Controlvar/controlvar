"""Gate G2: the AWS installer beta's sweep covers what the stacks make.

deploy/aws-installer/lib/sweep.sh records an account before an install and after its
uninstall; anything the stacks make that no sweep file lists would be left behind
unseen. So every CloudFormation resource type in the real staging synth of the core
profile, the shape the beta installs, either has a sweep file (``SWEPT``) or exists
only inside a parent that has one and goes with it (``COVERED_BY_PARENT``). Both
lists are exact for this synth: a type the stacks gain that neither names fails as
"not swept", and an entry for a type the synth no longer makes fails as stale.

Synthesised without cdk.json's context, as test_teardown_instructions.py does; a
synth with that context, the mode a deploy runs in, is not checked here.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from .test_app_profiles import REPO_ROOT
from .test_secret_arns import _assembly

SWEEP = Path(REPO_ROOT) / "deploy" / "aws-installer" / "lib" / "sweep.sh"

#: Each type with a sweep file of its own: the type, the file (lib/sweep.sh's
#: name for it).
SWEPT = {
    "AWS::ApplicationAutoScaling::ScalableTarget": "autoscaling-ecs",
    "AWS::CloudWatch::Alarm": "cloudwatch-alarms",
    "AWS::CloudWatch::CompositeAlarm": "cloudwatch-alarms",
    "AWS::CloudWatch::Dashboard": "cloudwatch-dashboards",
    "AWS::CodeDeploy::Application": "codedeploy-applications",
    "AWS::Cognito::UserPool": "cognito-user-pools",
    "AWS::DynamoDB::Table": "dynamodb-tables",
    "AWS::EC2::EIP": "ec2-addresses",
    "AWS::EC2::InternetGateway": "ec2-internet-gateways",
    "AWS::EC2::NatGateway": "ec2-nat-gateways",
    "AWS::EC2::NetworkAcl": "ec2-network-acls",
    "AWS::EC2::RouteTable": "ec2-route-tables",
    "AWS::EC2::SecurityGroup": "ec2-security-groups",
    "AWS::EC2::Subnet": "ec2-subnets",
    "AWS::EC2::VPC": "ec2-vpcs",
    "AWS::EC2::VPCEndpoint": "ec2-vpc-endpoints",
    "AWS::ECS::Cluster": "ecs-clusters",
    "AWS::ECS::Service": "ecs-services",
    "AWS::ECS::TaskDefinition": "ecs-td-families-active",
    "AWS::ElastiCache::ReplicationGroup": "elasticache-replication-groups",
    "AWS::ElastiCache::SubnetGroup": "elasticache-subnet-groups",
    "AWS::ElasticLoadBalancingV2::LoadBalancer": "elbv2-load-balancers",
    "AWS::ElasticLoadBalancingV2::TargetGroup": "elbv2-target-groups",
    "AWS::IAM::Role": "iam-roles",
    "AWS::KMS::Alias": "kms-aliases",
    "AWS::KMS::Key": "kms-keys",
    "AWS::Lambda::Function": "lambda-functions",
    "AWS::Logs::LogGroup": "logs-groups",
    "AWS::RDS::DBCluster": "rds-clusters",
    "AWS::RDS::DBClusterParameterGroup": "rds-cluster-parameter-groups",
    "AWS::RDS::DBInstance": "rds-instances",
    "AWS::RDS::DBParameterGroup": "rds-parameter-groups",
    "AWS::RDS::DBSubnetGroup": "rds-subnet-groups",
    "AWS::SNS::Subscription": "sns-subscriptions",
    "AWS::SNS::Topic": "sns-topics",
    "AWS::SSM::Parameter": "ssm-parameters",
    "AWS::SecretsManager::Secret": "secrets",
}

#: Each type that exists only inside a swept parent and is deleted with it: the
#: type, its parent's type, and why.
COVERED_BY_PARENT = {
    "AWS::ApplicationAutoScaling::ScalingPolicy": (
        "AWS::ApplicationAutoScaling::ScalableTarget",
        "a policy belongs to its scalable target",
    ),
    "AWS::CodeDeploy::DeploymentGroup": (
        "AWS::CodeDeploy::Application",
        "a deployment group belongs to its application",
    ),
    "AWS::Cognito::UserPoolClient": (
        "AWS::Cognito::UserPool",
        "a client belongs to its pool",
    ),
    "AWS::EC2::NetworkAclEntry": (
        "AWS::EC2::NetworkAcl",
        "an entry belongs to its ACL",
    ),
    "AWS::EC2::Route": ("AWS::EC2::RouteTable", "a route belongs to its table"),
    "AWS::EC2::SecurityGroupEgress": (
        "AWS::EC2::SecurityGroup",
        "a rule belongs to its group",
    ),
    "AWS::EC2::SecurityGroupIngress": (
        "AWS::EC2::SecurityGroup",
        "a rule belongs to its group",
    ),
    "AWS::EC2::SubnetNetworkAclAssociation": (
        "AWS::EC2::Subnet",
        "an association belongs to its subnet",
    ),
    "AWS::EC2::SubnetRouteTableAssociation": (
        "AWS::EC2::Subnet",
        "an association belongs to its subnet",
    ),
    "AWS::EC2::VPCGatewayAttachment": (
        "AWS::EC2::InternetGateway",
        "ec2-internet-gateways prints the VPC each gateway is attached to",
    ),
    "AWS::ElasticLoadBalancingV2::Listener": (
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "a listener belongs to its load balancer",
    ),
    "AWS::ElasticLoadBalancingV2::ListenerRule": (
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "a rule belongs to a listener of the load balancer",
    ),
    "AWS::IAM::Policy": ("AWS::IAM::Role", "an inline policy belongs to its role"),
    "AWS::Logs::MetricFilter": (
        "AWS::Logs::LogGroup",
        "a filter belongs to its log group",
    ),
    "AWS::SecretsManager::SecretTargetAttachment": (
        "AWS::SecretsManager::Secret",
        "an attachment belongs to its secret",
    ),
}


def sweep_types() -> set[str]:
    """The type names lib/sweep.sh writes a file for."""
    found = set()
    for line in SWEEP.read_text().splitlines():
        words = line.split(None, 1)
        if words and words[0] in ("_sweep_q", "_sweep_each"):
            found.add(shlex.split(line)[2])
    return found


def uncovered(types: set[str]) -> list[str]:
    """What the gate reports for a synth with ``types``: each type no list names,
    and each list entry for a type the synth does not make."""
    named = set(SWEPT) | set(COVERED_BY_PARENT)
    return [f"not swept: {t}" for t in sorted(types - named)] + [
        f"stale entry, the synth makes no {t}" for t in sorted(named - types)
    ]


def _types() -> set[str]:
    return {
        resource["Type"]
        for stack in _assembly("staging", "core").stacks
        for resource in stack.template.get("Resources", {}).values()
    }


def test_every_type_the_stacks_make_is_swept_or_goes_with_a_swept_parent():
    problems = uncovered(_types())
    assert problems == [], "\n".join(problems)


def test_every_sweep_file_named_is_one_the_library_writes():
    files = sweep_types()
    assert len(files) == 68, sorted(files)
    assert {f for f in SWEPT.values() if f not in files} == set()
    for child, (parent, why) in COVERED_BY_PARENT.items():
        assert parent in SWEPT, (child, parent)
        assert why


def test_a_type_the_stacks_gain_reads_as_not_swept():
    """The gate's own failure, on a test synth with one type more."""
    assert uncovered(_types() | {"AWS::SQS::Queue"}) == ["not swept: AWS::SQS::Queue"]
