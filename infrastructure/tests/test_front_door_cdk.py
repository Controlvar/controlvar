"""What the README, the FAQ and the architecture overview say the CDK deploys.

Each test reads a real ``app.synth()``, never the pages: the pages' claims are
tied to these tests by their registries under ``docs/_claims/``.

* the load balancer answers HTTPS, and its HTTP listener only redirects there;
* no stack creates a WAF, a CloudFront distribution or an API Gateway;
* Aurora's storage is encrypted with a KMS key the database stack creates, and
  only prod has a reader beside the writer;
* the API's task reads its secrets from Secrets Manager;
* the full profile's counters table is keyed by ``pk`` and ``sk``;
* no Lambda function is behind the load balancer, and the one fed by an event
  source is a placeholder.
"""

from __future__ import annotations

import re

import pytest

from .test_dashboard_service import _synth

pytestmark = pytest.mark.regression

#: CDK's own singleton providers, which are not application functions.
CDK_PROVIDER = re.compile(r"^(AWS[0-9a-f]{32}|CustomS3AutoDeleteObjects|LogRetention)")


@pytest.fixture(scope="module")
def dev():
    return _synth("dev")


@pytest.fixture(scope="module")
def prod():
    return _synth("prod")


def _resources(assembly, kind: str):
    """(stack name, logical id, properties) of every resource of *kind*."""
    for stack in assembly.stacks:
        for lid, resource in stack.template.get("Resources", {}).items():
            if resource["Type"] == kind:
                yield stack.stack_name, lid, resource.get("Properties", {})


def _types(assembly) -> set:
    return {
        resource["Type"]
        for stack in assembly.stacks
        for resource in stack.template.get("Resources", {}).values()
    }


def test_http_only_redirects_to_https(dev):
    listeners = {
        p["Port"]: p
        for _, _, p in _resources(dev, "AWS::ElasticLoadBalancingV2::Listener")
    }
    assert listeners[80]["Protocol"] == "HTTP"
    assert listeners[80]["DefaultActions"] == [
        {
            "RedirectConfig": {
                "Port": "443",
                "Protocol": "HTTPS",
                "StatusCode": "HTTP_301",
            },
            "Type": "redirect",
        }
    ]
    assert listeners[443]["Protocol"] == "HTTPS"
    assert listeners[443]["Certificates"], "the HTTPS listener has no certificate"
    # Every listener but the redirect speaks HTTPS (8443 is the blue/green test one).
    assert {port: p["Protocol"] for port, p in listeners.items() if port != 80} == {
        443: "HTTPS",
        8443: "HTTPS",
    }


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_no_stack_creates_a_waf_cloudfront_or_api_gateway(environment, dev, prod):
    assembly = {"dev": dev, "prod": prod}[environment]
    types = _types(assembly)
    assert "AWS::ElasticLoadBalancingV2::LoadBalancer" in types, "the scan found no ALB"
    offenders = sorted(
        t
        for t in types
        if t.startswith(
            ("AWS::WAF", "AWS::CloudFront::", "AWS::ApiGateway", "AWS::ApiGatewayV2")
        )
    )
    assert offenders == []


def test_aurora_storage_is_encrypted_with_the_stacks_kms_key(dev):
    (cluster,) = [p for _, _, p in _resources(dev, "AWS::RDS::DBCluster")]
    assert cluster["StorageEncrypted"] is True
    key_ids = {lid for _, lid, _ in _resources(dev, "AWS::KMS::Key")}
    assert cluster["KmsKeyId"]["Fn::GetAtt"][0] in key_ids
    (key,) = [p for _, _, p in _resources(dev, "AWS::KMS::Key")]
    # No KeySpec means KMS's SYMMETRIC_DEFAULT, an AES-256 key.
    assert key.get("KeySpec", "SYMMETRIC_DEFAULT") == "SYMMETRIC_DEFAULT"


@pytest.mark.parametrize(("environment", "instances"), [("dev", 1), ("prod", 2)])
def test_only_prod_has_an_aurora_reader(environment, instances, dev, prod):
    assembly = {"dev": dev, "prod": prod}[environment]
    assert len(list(_resources(assembly, "AWS::RDS::DBInstance"))) == instances


def test_the_api_reads_its_secrets_from_secrets_manager(dev):
    secrets = {}
    for _, _, props in _resources(dev, "AWS::ECS::TaskDefinition"):
        for container in props.get("ContainerDefinitions", []):
            if container["Name"] == "backend":
                secrets.update(
                    {s["Name"]: s["ValueFrom"] for s in container.get("Secrets", [])}
                )
    assert sorted(secrets) == [
        "AUDIT_HMAC_KEY",
        "FIRST_SUPERUSER_PASSWORD",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        "SECRET_KEY",
    ]
    for name, value in secrets.items():
        assert "secretsmanager" in str(value) or "Ref" in str(value), (name, value)


def test_the_full_profiles_counters_table(dev):
    tables = {
        p["TableName"]: p
        for _, _, p in _resources(dev, "AWS::DynamoDB::Table")
        if isinstance(p.get("TableName"), str)
    }
    table = tables["experiment-counters-dev"]
    assert table["KeySchema"] == [
        {"AttributeName": "pk", "KeyType": "HASH"},
        {"AttributeName": "sk", "KeyType": "RANGE"},
    ]
    assert [g["IndexName"] for g in table["GlobalSecondaryIndexes"]] == [
        "experiment-id-index"
    ]


def test_no_lambda_serves_requests_and_the_one_fed_events_does_nothing(dev):
    functions = {
        lid: props
        for _, lid, props in _resources(dev, "AWS::Lambda::Function")
        if not CDK_PROVIDER.match(lid)
    }
    assert functions, "the scan found no application function"
    # Nothing behind the load balancer is a Lambda function.
    for _, _, props in _resources(dev, "AWS::ElasticLoadBalancingV2::TargetGroup"):
        assert props.get("TargetType") != "lambda", props
    for _, _, props in _resources(dev, "AWS::Lambda::Permission"):
        assert "elasticloadbalancing" not in str(props.get("Principal")), props
        assert "apigateway" not in str(props.get("Principal")), props
    # The event source feeds a placeholder whose inline code only returns 200.
    mappings = list(_resources(dev, "AWS::Lambda::EventSourceMapping"))
    assert mappings, "the scan found no event source mapping"
    for _, _, mapping in mappings:
        target = mapping["FunctionName"]["Ref"]
        inline = functions[target]["Code"].get("ZipFile", "")
        assert "'statusCode': 200" in inline and "import" not in inline, target


#: Each stack the architecture overview lists, by id, and resource types it
#: must hold. Full profile (this checkout has modules/), environment dev.
STACK_CONTENTS = {
    "experimentation-vpc-dev": {
        "AWS::EC2::VPC",
        "AWS::EC2::Subnet",
        "AWS::EC2::NatGateway",
        "AWS::EC2::RouteTable",
        "AWS::EC2::NetworkAcl",
        "AWS::EC2::VPCEndpoint",
        "AWS::EC2::SecurityGroup",
    },
    "experimentation-auth-dev": {
        "AWS::Cognito::UserPool",
        "AWS::Cognito::UserPoolClient",
    },
    "experimentation-database-dev": {
        "AWS::RDS::DBCluster",
        "AWS::RDS::DBClusterParameterGroup",
        "AWS::KMS::Key",
        "AWS::EC2::SecurityGroup",
    },
    "experimentation-redis-dev": {
        "AWS::ElastiCache::ReplicationGroup",
        "AWS::ElastiCache::SubnetGroup",
        "AWS::EC2::SecurityGroup",
    },
    "experimentation-dynamodb-dev": {"AWS::DynamoDB::Table"},
    "experimentation-compute-dev": {
        "AWS::ECS::Cluster",
        "AWS::EC2::SecurityGroup",
        "AWS::Lambda::Function",
    },
    "experimentation-fargate-dev": {
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "AWS::ElasticLoadBalancingV2::Listener",
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        "AWS::ECS::Service",
        "AWS::CodeDeploy::Application",
        "AWS::CodeDeploy::DeploymentGroup",
        "AWS::ApplicationAutoScaling::ScalableTarget",
    },
    "experimentation-migrations-dev": {"AWS::ECS::TaskDefinition"},
    "experimentation-monitoring-dev": {
        "AWS::CloudWatch::Dashboard",
        "AWS::CloudWatch::Alarm",
        "AWS::SNS::Topic",
    },
    "experimentation-dynamodb-counters-dev": {"AWS::DynamoDB::Table"},
    "experimentation-analytics-dev": {
        "AWS::Kinesis::Stream",
        "AWS::KinesisFirehose::DeliveryStream",
        "AWS::S3::Bucket",
        "AWS::OpenSearchService::Domain",
        "AWS::Lambda::Function",
    },
    "experimentation-glue-etl-dev": {
        "AWS::Glue::Database",
        "AWS::Glue::Crawler",
        "AWS::Glue::Job",
        "AWS::S3::Bucket",
        "AWS::Events::Rule",
    },
}


def test_each_stack_holds_what_the_overview_lists(dev):
    found = {
        stack.stack_name: [
            r["Type"] for r in stack.template.get("Resources", {}).values()
        ]
        for stack in dev.stacks
    }
    assert set(found) == set(STACK_CONTENTS)
    for name, kinds in STACK_CONTENTS.items():
        assert kinds <= set(found[name]), (name, sorted(kinds - set(found[name])))
    assert found["experimentation-dynamodb-dev"].count("AWS::DynamoDB::Table") == 3
    assert found["experimentation-glue-etl-dev"].count("AWS::Glue::Job") == 2


def test_three_application_functions_and_none_from_an_asset(dev):
    functions = {
        re.sub(r"[0-9A-F]{8}$", "", lid): props
        for _, lid, props in _resources(dev, "AWS::Lambda::Function")
        if not CDK_PROVIDER.match(lid)
    }
    assert set(functions) == {
        "DatabaseAccessLambda",
        "AnalyticsLambda",
        "ETLTriggerLambda",
    }
    for name in ("DatabaseAccessLambda", "AnalyticsLambda"):
        inline = functions[name]["Code"].get("ZipFile", "")
        assert "'statusCode': 200" in inline and "import" not in inline, name
    assert "glue.start_job_run" in functions["ETLTriggerLambda"]["Code"].get(
        "ZipFile", ""
    )
    # Nothing is deployed from an asset, so no code under backend/lambda/ is.
    assert not [n for n, p in functions.items() if "S3Key" in p["Code"]]
