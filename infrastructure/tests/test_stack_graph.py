"""How the stacks depend on each other, and what each one holds.

On a real synth of every environment (full profile, so the module stacks are
in the graph):

* the compute stack's cluster is imported by the Fargate stack alone, the
  analytics stack imports nothing from compute, and only the migrations stack
  depends on the Fargate stack;
* the Fargate stack holds the API's and the dashboard's services and no data
  store, so destroying it stops both and leaves the data;
* the destroy order a person follows by hand: monitoring and the Glue stack
  depend on analytics, the Fargate stack on compute and monitoring;
* the load balancer is named ``experimentation-<env>`` and its DNS name is the
  Fargate stack's ``ALBDnsName`` output; the API's target groups check
  ``GET /health`` with a 5-second timeout, 2 to be healthy and 3 to be not;
* the API task is 1 vCPU and 2 GB, logs to ``/ecs/experimentation-backend-
  <env>`` and does not set ``REDIS_REQUIRED``;
* Aurora's instances (a writer and a reader on ``db.r5.large`` in prod, one
  ``db.t3.medium`` elsewhere), the database stack's subnet group and both
  parameter groups, the Redis stack's subnet group;
* the cluster and the CodeDeploy application are named for the environment,
  and the old API tasks are kept an hour after a shift;
* the three core tables, and the full profile's on-demand counters table with
  its index by experiment;
* what prod keeps, by name, when its stacks are destroyed;
* the monitoring stack's two dashboards, one of whose widgets graphs API
  Gateway metrics in an app with no API Gateway (#424);
* the split-URL CloudFront construct ships with the modules and nothing
  builds it.
"""

from __future__ import annotations

import ast
import json

import pytest

from .test_app_profiles import REPO_ROOT
from .test_environments_do_not_collide import (
    ENVIRONMENTS,
    MODULES_PRESENT,
    Synth,
    _exports,
    _imports,
    cluster_export_importers,
)

pytestmark = pytest.mark.skipif(
    not MODULES_PRESENT, reason="core checkout: the module stacks are not in the graph"
)

DATA_STORES = (
    "AWS::RDS::",
    "AWS::ElastiCache::",
    "AWS::DynamoDB::",
    "AWS::S3::",
    "AWS::Kinesis::",
    "AWS::OpenSearchService::",
    "AWS::SecretsManager::Secret",
)
SPLIT_URL = (
    REPO_ROOT
    / "modules"
    / "infrastructure"
    / "constructs"
    / "split_url_distribution.py"
)


@pytest.fixture(scope="module")
def synths() -> dict[str, Synth]:
    return {env: Synth(env) for env in ENVIRONMENTS}


def _resources(template: dict, kind: str) -> dict[str, dict]:
    return {
        lid: r for lid, r in template.get("Resources", {}).items() if r["Type"] == kind
    }


# --- the graph ----------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_only_fargate_imports_the_cluster_and_only_migrations_depends_on_fargate(
    synths, env
):
    synth = synths[env]
    assert cluster_export_importers(synth) == {"experimentation-fargate"}
    compute = f"experimentation-compute-{env}"
    from_compute = {name for name, stack in _exports(synth).items() if stack == compute}
    analytics = synth.templates[f"experimentation-analytics-{env}"]
    imported = _imports(analytics.get("Resources", {}), set()) | _imports(
        analytics.get("Outputs", {}), set()
    )
    assert not imported & from_compute, imported & from_compute
    fargate = f"experimentation-fargate-{env}"
    dependants = {
        synth.short(stack)
        for stack, dependencies in synth.dependencies.items()
        if fargate in dependencies
    }
    assert dependants == {"experimentation-migrations"}, dependants


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_fargate_stack_holds_the_two_services_and_no_data_store(synths, env):
    template = synths[env].templates[f"experimentation-fargate-{env}"]
    services = sorted(
        r["Properties"]["ServiceName"]
        for r in _resources(template, "AWS::ECS::Service").values()
    )
    assert services == [
        f"experimentation-backend-{env}",
        f"experimentation-dashboard-{env}",
    ]
    kinds = {r["Type"] for r in template["Resources"].values()}
    stores = sorted(k for k in kinds if k.startswith(DATA_STORES))
    assert not stores, stores


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_what_must_be_destroyed_first(synths, env):
    dependencies = {
        synths[env].short(stack): {synths[env].short(d) for d in found}
        for stack, found in synths[env].dependencies.items()
    }
    assert "experimentation-analytics" in dependencies["experimentation-monitoring"]
    assert "experimentation-analytics" in dependencies["experimentation-glue-etl"]
    assert {"experimentation-compute", "experimentation-monitoring"} <= dependencies[
        "experimentation-fargate"
    ]


# --- the load balancer and the API task -------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_load_balancer_is_named_for_its_environment_and_output(synths, env):
    template = synths[env].templates[f"experimentation-fargate-{env}"]
    ((lid, balancer),) = _resources(
        template, "AWS::ElasticLoadBalancingV2::LoadBalancer"
    ).items()
    assert balancer["Properties"]["Name"] == f"experimentation-{env}"
    assert balancer["Properties"]["Scheme"] == "internet-facing"
    assert template["Outputs"]["ALBDnsName"]["Value"] == {
        "Fn::GetAtt": [lid, "DNSName"]
    }


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_api_target_groups_check_health_on_slash_health(synths, env):
    template = synths[env].templates[f"experimentation-fargate-{env}"]
    api = {
        lid: r["Properties"]
        for lid, r in _resources(
            template, "AWS::ElasticLoadBalancingV2::TargetGroup"
        ).items()
        if r["Properties"]["Port"] == 8000
    }
    assert len(api) == 2, sorted(api)  # blue and green
    for properties in api.values():
        assert properties["HealthCheckPath"] == "/health"
        assert properties["HealthCheckTimeoutSeconds"] == 5
        assert properties["HealthyThresholdCount"] == 2
        assert properties["UnhealthyThresholdCount"] == 3


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_api_task_is_one_vcpu_and_two_gb(synths, env):
    template = synths[env].templates[f"experimentation-fargate-{env}"]
    (api,) = [
        r["Properties"]
        for r in _resources(template, "AWS::ECS::TaskDefinition").values()
        if r["Properties"]["Family"] == f"experimentation-backend-{env}"
    ]
    assert (api["Cpu"], api["Memory"]) == ("1024", "2048")


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_api_logs_to_its_log_group_and_leaves_redis_optional(synths, env):
    """The API container's log is /ecs/experimentation-backend-<env>, and its
    environment does not set REDIS_REQUIRED, so readiness runs at the
    setting's default (backend/tests/unit/core/test_readiness_at_its_defaults.py)."""
    template = synths[env].templates[f"experimentation-fargate-{env}"]
    (api,) = [
        r["Properties"]
        for r in _resources(template, "AWS::ECS::TaskDefinition").values()
        if r["Properties"]["Family"] == f"experimentation-backend-{env}"
    ]
    (container,) = [c for c in api["ContainerDefinitions"] if c["Name"] == "backend"]
    options = container["LogConfiguration"]["Options"]
    assert container["LogConfiguration"]["LogDriver"] == "awslogs"
    group = template["Resources"][options["awslogs-group"]["Ref"]]
    assert group["Properties"]["LogGroupName"] == f"/ecs/experimentation-backend-{env}"
    names = {e["Name"] for e in container.get("Environment", [])}
    names |= {s["Name"] for s in container.get("Secrets", [])}
    assert "REDIS_HOST" in names  # not vacuous
    assert "REDIS_REQUIRED" not in names, sorted(names)


# --- the database and Redis stacks -------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_subnet_and_parameter_groups(synths, env):
    database = synths[env].templates[f"experimentation-database-{env}"]
    redis = synths[env].templates[f"experimentation-redis-{env}"]
    (subnets,) = _resources(database, "AWS::RDS::DBSubnetGroup").values()
    (cluster_parameters,) = _resources(
        database, "AWS::RDS::DBClusterParameterGroup"
    ).items()
    (instance_parameters,) = _resources(database, "AWS::RDS::DBParameterGroup").items()
    ((subnet_group, _),) = _resources(database, "AWS::RDS::DBSubnetGroup").items()
    (cluster,) = _resources(database, "AWS::RDS::DBCluster").values()
    assert cluster["Properties"]["DBSubnetGroupName"] == {"Ref": subnet_group}
    assert cluster["Properties"]["DBClusterParameterGroupName"] == {
        "Ref": cluster_parameters[0]
    }
    for instance in _resources(database, "AWS::RDS::DBInstance").values():
        assert instance["Properties"]["DBParameterGroupName"] == {
            "Ref": instance_parameters[0]
        }
    ((redis_subnets, _),) = _resources(redis, "AWS::ElastiCache::SubnetGroup").items()
    (group,) = _resources(redis, "AWS::ElastiCache::ReplicationGroup").values()
    assert group["Properties"]["CacheSubnetGroupName"] == {"Ref": redis_subnets}
    assert subnets["Properties"]["SubnetIds"], subnets


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_aurora_instances_per_environment(synths, env):
    database = synths[env].templates[f"experimentation-database-{env}"]
    classes = sorted(
        r["Properties"]["DBInstanceClass"]
        for r in _resources(database, "AWS::RDS::DBInstance").values()
    )
    expected = ["db.r5.large"] * 2 if env == "prod" else ["db.t3.medium"]
    assert classes == expected, classes


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_names_the_environment_gives_the_cluster_and_the_application(synths, env):
    compute = synths[env].templates[f"experimentation-compute-{env}"]
    fargate = synths[env].templates[f"experimentation-fargate-{env}"]
    (cluster,) = _resources(compute, "AWS::ECS::Cluster").values()
    (application,) = _resources(fargate, "AWS::CodeDeploy::Application").values()
    assert cluster["Properties"]["ClusterName"] == f"experimentation-{env}"
    assert (
        application["Properties"]["ApplicationName"]
        == f"experimentation-platform-{env}"
    )


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_old_tasks_are_kept_for_an_hour_then_terminated(synths, env):
    fargate = synths[env].templates[f"experimentation-fargate-{env}"]
    (group,) = _resources(fargate, "AWS::CodeDeploy::DeploymentGroup").values()
    blue_green = group["Properties"]["BlueGreenDeploymentConfiguration"]
    assert blue_green["TerminateBlueInstancesOnDeploymentSuccess"] == {
        "Action": "TERMINATE",
        "TerminationWaitTimeInMinutes": 60,
    }


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_core_tables_and_the_full_profiles_counters_table(synths, env):
    core = synths[env].templates[f"experimentation-dynamodb-{env}"]
    names = sorted(
        r["Properties"]["TableName"]
        for r in _resources(core, "AWS::DynamoDB::Table").values()
    )
    assert names == [
        f"experimentation-events-{env}",
        f"experimentation-feature-flags-{env}",
        f"experimentation-overrides-{env}",
    ]
    counters = synths[env].templates[f"experimentation-dynamodb-counters-{env}"]
    (table,) = _resources(counters, "AWS::DynamoDB::Table").values()
    properties = table["Properties"]
    assert properties["TableName"] == f"experiment-counters-{env}"
    assert properties["BillingMode"] == "PAY_PER_REQUEST"
    (index,) = properties["GlobalSecondaryIndexes"]
    assert index["IndexName"] == "experiment-id-index"
    assert [k["AttributeName"] for k in index["KeySchema"]][:1] == ["experiment_id"]


@pytest.mark.regression
def test_what_prod_keeps_when_its_stacks_are_destroyed(synths):
    """By name: what survives a prod teardown, and is billed until removed."""
    prod = synths["prod"].templates
    (pool,) = _resources(
        prod["experimentation-auth-prod"], "AWS::Cognito::UserPool"
    ).values()
    assert pool["Properties"]["UserPoolName"] == "experimentation-platform-users-prod"
    assert pool["DeletionPolicy"] == "Retain"
    tables = {
        r["Properties"]["TableName"]: r
        for stack in (
            "experimentation-dynamodb-prod",
            "experimentation-dynamodb-counters-prod",
        )
        for r in _resources(prod[stack], "AWS::DynamoDB::Table").values()
    }
    assert sorted(tables) == [
        "experiment-counters-prod",
        "experimentation-events-prod",
        "experimentation-feature-flags-prod",
        "experimentation-overrides-prod",
    ]
    for name, table in tables.items():
        assert table["DeletionPolicy"] == "Retain", name
        # The core tables are provisioned in prod; the counters table is not.
        provisioned = "ProvisionedThroughput" in table["Properties"]
        assert provisioned == (name != "experiment-counters-prod"), name
    groups = {
        r["Properties"]["LogGroupName"]: r["DeletionPolicy"]
        for template in prod.values()
        for r in _resources(template, "AWS::Logs::LogGroup").values()
        if isinstance(r["Properties"].get("LogGroupName"), str)
    }
    assert {
        f"/ecs/experimentation-{name}-prod": "Retain"
        for name in ("backend", "dashboard", "migrate")
    }.items() <= groups.items(), groups
    buckets = {
        lid.rstrip("0123456789ABCDEF"): r["DeletionPolicy"]
        for template in prod.values()
        for lid, r in _resources(template, "AWS::S3::Bucket").items()
    }
    assert buckets == {
        "DataLakeBucket": "Retain",
        "AthenaResultsBucket": "Retain",
        "GlueScriptsBucket": "Retain",
    }, buckets
    analytics = prod["experimentation-analytics-prod"]
    for kind in ("AWS::Kinesis::Stream", "AWS::OpenSearchService::Domain"):
        (resource,) = _resources(analytics, kind).values()
        assert resource["DeletionPolicy"] == "Retain", kind
    database = prod["experimentation-database-prod"]
    (key,) = _resources(database, "AWS::KMS::Key").values()
    (alias,) = _resources(database, "AWS::KMS::Alias").values()
    assert key["DeletionPolicy"] == "Retain"
    assert alias.get("DeletionPolicy", "Delete") == "Delete"
    (cluster,) = _resources(database, "AWS::RDS::DBCluster").values()
    assert cluster["DeletionPolicy"] == "Snapshot"
    assert cluster["Properties"]["KmsKeyId"] == {
        "Fn::GetAtt": [next(iter(_resources(database, "AWS::KMS::Key"))), "Arn"]
    }


# --- monitoring -------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_two_dashboards_and_a_widget_nothing_publishes_to(synths, env):
    monitoring = synths[env].templates[f"experimentation-monitoring-{env}"]
    dashboards = {
        r["Properties"]["DashboardName"]: json.dumps(r["Properties"]["DashboardBody"])
        for r in _resources(monitoring, "AWS::CloudWatch::Dashboard").values()
    }
    assert sorted(dashboards) == [
        f"experimentation-application-metrics-{env}",
        f"experimentation-platform-{env}",
    ]
    # #424: the platform dashboard graphs API Gateway's metrics, and no stack
    # creates an API Gateway, so nothing publishes them.
    assert "AWS/ApiGateway" in dashboards[f"experimentation-platform-{env}"]
    kinds = {
        r["Type"]
        for template in synths[env].templates.values()
        for r in template.get("Resources", {}).values()
    }
    assert not {k for k in kinds if k.startswith("AWS::ApiGateway")}, kinds


# --- CloudFront ---------------------------------------------------------------------


@pytest.mark.regression
def test_the_split_url_construct_ships_and_nothing_builds_it(synths):
    tree = ast.parse(SPLIT_URL.read_text())
    classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    assert classes == ["SplitUrlDistribution"], classes
    calls = {
        f"{n.func.value.id}.{n.func.attr}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
    }
    assert "cloudfront.Distribution" in calls, sorted(calls)
    # Nothing the app runs names it ...
    app_sources = [
        *sorted((REPO_ROOT / "infrastructure" / "cdk").rglob("*.py")),
        *sorted((REPO_ROOT / "modules" / "infrastructure" / "cdk").rglob("*.py")),
    ]
    assert app_sources
    for path in app_sources:
        text = path.read_text()
        assert "split_url" not in text and "SplitUrlDistribution" not in text, path
    # ... and no environment has a distribution.
    for synth in synths.values():
        for template in synth.templates.values():
            for resource in template.get("Resources", {}).values():
                assert not resource["Type"].startswith("AWS::CloudFront::"), resource
