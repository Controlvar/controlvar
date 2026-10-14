"""What the CDK app gives the API and which data services it creates (#1142).

On a real synth of every environment: the API service is deployed blue/green
by CodeDeploy; the API task role holds exactly the listed actions and one
managed policy; there is no CloudFront distribution and no bucket for the
dashboard's assets; and the full profile's analytics stack creates a Kinesis
stream that Firehose delivers to S3, and an OpenSearch domain.
"""

from __future__ import annotations

import json
import runpy

import pytest

from .test_app_profiles import CDK_DIR, _app_environment
from .test_environments_do_not_collide import (
    ENVIRONMENTS,
    MODULES_PRESENT,
    Synth,
    _as_list,
    _backend_task_definition,
    _task_role_statements,
)

#: The API task role's actions on the full profile (ECS Exec, the secrets the
#: task reads, the counters table and the etl module's Glue calls).
CORE_ACTIONS = {
    "logs:CreateLogStream",
    "logs:DescribeLogGroups",
    "logs:DescribeLogStreams",
    "logs:PutLogEvents",
    "secretsmanager:GetSecretValue",
    "ssmmessages:CreateControlChannel",
    "ssmmessages:CreateDataChannel",
    "ssmmessages:OpenControlChannel",
    "ssmmessages:OpenDataChannel",
}
MODULE_ACTIONS = {
    "dynamodb:Query",
    "dynamodb:UpdateItem",
    "glue:StartJobRun",
    "glue:GetJobRun",
    "glue:StartCrawler",
    "glue:GetCrawler",
    "glue:GetTable",
    "glue:BatchCreatePartition",
}


def _synth(env: str, profile: str | None = None) -> Synth:
    overrides = {"EXPERIMENTLY_PROFILE": profile} if profile else {}
    with _app_environment(CDK_DIR, ENVIRONMENT=env, **overrides):
        namespace = runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")
    return Synth(env, namespace["app"].synth())


@pytest.fixture(scope="module")
def synths() -> dict[str, Synth]:
    return {env: _synth(env) for env in ENVIRONMENTS}


@pytest.fixture(scope="module")
def core() -> Synth:
    return _synth("prod", "core")


def _of_type(synth: Synth, rtype: str) -> list[tuple[str, str, dict]]:
    return [(s, lid, r) for s, lid, r in synth.resources() if r["Type"] == rtype]


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_api_is_deployed_blue_green_by_codedeploy(synths, env):
    services = {
        r["Properties"]["ServiceName"]: r["Properties"]
        for _, _, r in _of_type(synths[env], "AWS::ECS::Service")
    }
    assert services[f"experimentation-backend-{env}"]["DeploymentController"] == {
        "Type": "CODE_DEPLOY"
    }
    ((_, _, group),) = _of_type(synths[env], "AWS::CodeDeploy::DeploymentGroup")
    props = group["Properties"]
    assert props["DeploymentStyle"] == {
        "DeploymentOption": "WITH_TRAFFIC_CONTROL",
        "DeploymentType": "BLUE_GREEN",
    }
    (pair,) = props["LoadBalancerInfo"]["TargetGroupPairInfoList"]
    assert len(pair["TargetGroups"]) == 2


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_api_task_role_holds_exactly_these_permissions(synths, env, core):
    for synth, expected in (
        (synths[env], CORE_ACTIONS | (MODULE_ACTIONS if MODULES_PRESENT else set())),
        (core, CORE_ACTIONS),
    ):
        statements = _task_role_statements(synth)
        actions = {a for s in statements for a in _as_list(s.get("Action", []))}
        assert actions == expected, (synth.env, sorted(actions ^ expected))
        assert not [
            a
            for a in actions
            if a.split(":")[0] in {"kinesis", "cognito-idp", "athena", "s3"}
        ]

        role_id = _backend_task_definition(synth)["Properties"]["TaskRoleArn"][
            "Fn::GetAtt"
        ][0]
        role = synth.templates[f"experimentation-fargate-{synth.env}"]["Resources"][
            role_id
        ]
        (managed,) = role["Properties"]["ManagedPolicyArns"]
        assert json.dumps(managed).endswith(
            ':iam::aws:policy/CloudWatchLogsFullAccess"]]}'
        )


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_no_cloudfront_distribution_and_no_bucket_for_the_dashboard(synths, env):
    assert not _of_type(synths[env], "AWS::CloudFront::Distribution")
    stacks = {s for s, _, _ in _of_type(synths[env], "AWS::S3::Bucket")}
    assert stacks <= {
        f"experimentation-analytics-{env}",
        f"experimentation-glue-etl-{env}",
    }, stacks
    dashboard = {
        r["Properties"]["ServiceName"]: r["Properties"]
        for _, _, r in _of_type(synths[env], "AWS::ECS::Service")
    }[f"experimentation-dashboard-{env}"]
    assert dashboard["DeploymentController"] == {"Type": "ECS"}


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_analytics_stack_streams_to_s3_and_has_an_opensearch_domain(
    synths, env, core
):
    if not MODULES_PRESENT:
        pytest.skip("core checkout: no analytics module")
    synth = synths[env]
    analytics = f"experimentation-analytics-{env}"
    found = {r["Type"] for s, _, r in synth.resources() if s == analytics}
    assert {
        "AWS::Kinesis::Stream",
        "AWS::KinesisFirehose::DeliveryStream",
        "AWS::OpenSearchService::Domain",
        "AWS::S3::Bucket",
    } <= found
    ((_, _, firehose),) = _of_type(synth, "AWS::KinesisFirehose::DeliveryStream")
    props = firehose["Properties"]
    assert props["DeliveryStreamType"] == "KinesisStreamAsSource"
    assert (
        "ExtendedS3DestinationConfiguration" in props
        or "S3DestinationConfiguration" in props
    )
    # The core profile creates none of them.
    assert not [s for s in core.templates if "analytics" in s]


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_nothing_collects_the_apis_prometheus_metrics(synths, env):
    synth = synths[env]
    (container,) = _backend_task_definition(synth)["Properties"]["ContainerDefinitions"]
    assert container["Name"] == "backend"
    rendered = json.dumps(synth.templates).lower()
    for collector in (
        "cloudwatch-agent",
        "aws-otel",
        "adot",
        "remote_write",
        "remote-write",
    ):
        assert collector not in rendered, collector
    assert not [
        r for _, _, r in synth.resources() if r["Type"].startswith("AWS::APS::")
    ]
