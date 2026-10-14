"""What an operator reads off the stacks: names, outputs, backups, logs and
what is not built (#1142, D73 TR4).

Each test synthesises ``app.py`` for the environments the deployment pages
cover and asserts one thing an operator types or relies on mid-incident:
the physical names the workflows and runbooks address and the stack each is
in, the outputs that carry the names the CDK generates, the backups and log
retention per environment, the buckets of the full profile, and that nothing
is built in a second region. Reads the synthesised templates and the three
deployment workflows only.
"""

from __future__ import annotations

import json
import re

import pytest
import yaml

from .test_app_profiles import REPO_ROOT
from .test_dashboard_service import _synth

pytestmark = pytest.mark.regression

ENVIRONMENTS = ("dev", "staging", "prod", "demo")
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
MODULES_PRESENT = (REPO_ROOT / "modules" / "infrastructure" / "cdk" / "stacks").is_dir()


@pytest.fixture(scope="module")
def synths() -> dict:
    """{env: {stack name: template}} for every environment app.py accepts."""
    return {
        env: {s.stack_name: s.template for s in _synth(env).stacks}
        for env in ENVIRONMENTS
    }


def _resources(templates: dict, rtype: str, stack: str | None = None):
    for name, template in templates.items():
        if stack is not None and name != stack:
            continue
        for lid, resource in template.get("Resources", {}).items():
            if resource["Type"] == rtype:
                yield name, lid, resource


def _props(resource: dict) -> dict:
    return resource.get("Properties", {})


# --- the names -------------------------------------------------------------------------

#: (what, resource type, name property, name, the stack it is in)
NAMES = (
    (
        "ECS cluster",
        "AWS::ECS::Cluster",
        "ClusterName",
        "experimentation-{env}",
        "compute",
    ),
    (
        "API service",
        "AWS::ECS::Service",
        "ServiceName",
        "experimentation-backend-{env}",
        "fargate",
    ),
    (
        "API family",
        "AWS::ECS::TaskDefinition",
        "Family",
        "experimentation-backend-{env}",
        "fargate",
    ),
    (
        "migration family",
        "AWS::ECS::TaskDefinition",
        "Family",
        "experimentation-migrate-{env}",
        "migrations",
    ),
    (
        "dashboard service",
        "AWS::ECS::Service",
        "ServiceName",
        "experimentation-dashboard-{env}",
        "fargate",
    ),
    (
        "dashboard family",
        "AWS::ECS::TaskDefinition",
        "Family",
        "experimentation-dashboard-{env}",
        "fargate",
    ),
    (
        "API log group",
        "AWS::Logs::LogGroup",
        "LogGroupName",
        "/ecs/experimentation-backend-{env}",
        "fargate",
    ),
    (
        "migration log group",
        "AWS::Logs::LogGroup",
        "LogGroupName",
        "/ecs/experimentation-migrate-{env}",
        "migrations",
    ),
    (
        "dashboard log group",
        "AWS::Logs::LogGroup",
        "LogGroupName",
        "/ecs/experimentation-dashboard-{env}",
        "fargate",
    ),
    (
        "CodeDeploy application",
        "AWS::CodeDeploy::Application",
        "ApplicationName",
        "experimentation-platform-{env}",
        "fargate",
    ),
    (
        "CodeDeploy group",
        "AWS::CodeDeploy::DeploymentGroup",
        "DeploymentGroupName",
        "experimentation-{env}",
        "fargate",
    ),
    (
        "Redis replication group",
        "AWS::ElastiCache::ReplicationGroup",
        "ReplicationGroupId",
        "experimentation-redis-{env}-redis",
        "redis",
    ),
    (
        "Aurora credentials",
        "AWS::SecretsManager::Secret",
        "Name",
        "experimentation-database-{env}-aurora-credentials",
        "database",
    ),
)


@pytest.mark.parametrize("env", ("staging", "prod"))
@pytest.mark.parametrize("row", NAMES, ids=[row[0] for row in NAMES])
def test_each_name_an_operator_types_is_one_resource_of_its_type(synths, env, row):
    _, rtype, prop, name, stack = row
    name = name.format(env=env)
    found = [
        stack_name
        for stack_name, _, resource in _resources(synths[env], rtype)
        if _props(resource).get(prop) == name
    ]
    assert found == [f"experimentation-{stack}-{env}"], (name, found)


@pytest.mark.parametrize("env", ("staging", "prod"))
def test_the_three_stacks_the_workflows_address_exist(synths, env):
    for stack in ("fargate", "database", "migrations"):
        assert f"experimentation-{stack}-{env}" in synths[env]


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_aurora_cluster_identifier_is_generated_and_published(synths, env):
    database = synths[env][f"experimentation-database-{env}"]
    ((_, lid, cluster),) = list(_resources({"db": database}, "AWS::RDS::DBCluster"))
    assert "DBClusterIdentifier" not in _props(cluster)
    assert database["Outputs"]["ClusterIdentifier"]["Value"] == {"Ref": lid}


def _output_reads() -> list[tuple[str, str, list[str]]]:
    """(workflow, stack name template, keys) for every stack-outputs read."""
    reads = []
    for name in ("deploy.yml", "rollback.yml", "db-migrate.yml"):
        document = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
        for job in document["jobs"].values():
            env = job.get("env", {})
            for step in job.get("steps", []):
                if step.get("uses") != "./.github/actions/stack-outputs":
                    continue
                stack = step["with"]["stack"]
                variable = re.fullmatch(r"\$\{\{ env\.(\w+) \}\}", stack)
                assert variable, stack
                reads.append(
                    (name, env[variable.group(1)], step["with"]["keys"].split())
                )
    return reads


@pytest.mark.parametrize("env", ("staging", "prod"))
def test_every_generated_name_a_workflow_reads_is_a_stack_output(synths, env):
    """The cluster identifier and the task network are generated; the stacks
    publish them as outputs, and that is where the workflows read them."""
    reads = _output_reads()
    keys = {key for _, _, ks in reads for key in ks}
    assert {"ClusterIdentifier", "TaskSubnets", "TaskSecurityGroup"} <= keys, reads
    for workflow, stack, ks in reads:
        stack = stack.replace("${{ inputs.environment }}", env)
        outputs = synths[env][stack]["Outputs"]
        for key in ks:
            assert key in outputs, (workflow, stack, key)
            # Generated: an intrinsic, not a literal name typed in the stack.
            assert isinstance(outputs[key]["Value"], dict), (workflow, key)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_every_parameter_the_stacks_write_is_under_experimentation_env(synths, env):
    names = [
        _props(r)["Name"] for _, _, r in _resources(synths[env], "AWS::SSM::Parameter")
    ]
    assert names, "no parameter: the check is vacuous"
    for name in names:
        assert isinstance(name, str) and name.startswith(f"/experimentation/{env}/"), (
            name
        )


@pytest.mark.parametrize("env", ("staging", "prod"))
def test_the_migration_task_logs_to_its_group_under_migrate_backend(synths, env):
    """Its stream is migrate/backend/<task id>: the awslogs prefix, then the
    container's name (ECS adds the task id)."""
    migrations = synths[env][f"experimentation-migrations-{env}"]
    groups = {
        lid: _props(r)["LogGroupName"]
        for _, lid, r in _resources({"m": migrations}, "AWS::Logs::LogGroup")
    }
    ((_, _, definition),) = list(
        _resources({"m": migrations}, "AWS::ECS::TaskDefinition")
    )
    (container,) = _props(definition)["ContainerDefinitions"]
    options = container["LogConfiguration"]["Options"]
    assert container["Name"] == "backend"
    assert options["awslogs-stream-prefix"] == "migrate"
    assert (
        groups[options["awslogs-group"]["Ref"]] == f"/ecs/experimentation-migrate-{env}"
    )


@pytest.mark.parametrize("env", ("staging", "prod"))
def test_the_load_balancer_probes_the_api_on_health(synths, env):
    fargate = synths[env][f"experimentation-fargate-{env}"]
    api_groups = [
        _props(r)
        for _, lid, r in _resources(
            {"f": fargate}, "AWS::ElasticLoadBalancingV2::TargetGroup"
        )
        if lid.startswith(("BlueTargetGroup", "GreenTargetGroup"))
    ]
    assert len(api_groups) == 2
    for group in api_groups:
        assert group["HealthCheckPath"] == "/health"
        assert group["Port"] == 8000


# --- what each stack holds --------------------------------------------------------------


@pytest.mark.parametrize("env", ("staging", "prod"))
def test_the_dynamodb_compute_and_migrations_stacks_hold_what_the_list_says(
    synths, env
):
    tables = sorted(
        _props(r)["TableName"]
        for _, _, r in _resources(
            synths[env], "AWS::DynamoDB::Table", f"experimentation-dynamodb-{env}"
        )
    )
    assert tables == sorted(
        f"experimentation-{t}-{env}" for t in ("events", "feature-flags", "overrides")
    )
    compute = f"experimentation-compute-{env}"
    assert [
        _props(r)["ClusterName"]
        for _, _, r in _resources(synths[env], "AWS::ECS::Cluster", compute)
    ] == [f"experimentation-{env}"]
    groups = [
        lid for _, lid, _ in _resources(synths[env], "AWS::EC2::SecurityGroup", compute)
    ]
    assert len(groups) == 1 and groups[0].startswith("ECSSecurityGroup"), groups
    functions = [
        lid for _, lid, _ in _resources(synths[env], "AWS::Lambda::Function", compute)
    ]
    assert len(functions) == 1 and functions[0].startswith("DatabaseAccessLambda"), (
        functions
    )
    migrations = list(
        _resources(
            synths[env], "AWS::ECS::TaskDefinition", f"experimentation-migrations-{env}"
        )
    )
    assert len(migrations) == 1
    ((_, _, definition),) = migrations
    (container,) = _props(definition)["ContainerDefinitions"]
    assert container["Command"] == ["python", "-m", "backend.app.db.bootstrap"]


# --- backups and logs ----------------------------------------------------------------------

AURORA_RETENTION = {"dev": 1, "staging": 35, "prod": 35, "demo": 1}


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_aurora_keeps_35_days_in_prod_and_staging_and_1_elsewhere(synths, env):
    ((_, _, cluster),) = list(_resources(synths[env], "AWS::RDS::DBCluster"))
    assert _props(cluster)["BackupRetentionPeriod"] == AURORA_RETENTION[env]
    # Deleting the prod stack leaves a final snapshot; elsewhere it does not.
    assert cluster.get("DeletionPolicy") == ("Snapshot" if env == "prod" else "Delete")


REDIS = {
    # env: (snapshot retention days, nodes, automatic failover)
    "dev": (1, 1, False),
    "staging": (3, 1, False),
    "prod": (7, 3, True),
}


@pytest.mark.parametrize("env", sorted(REDIS))
def test_redis_snapshots_daily_at_two_utc_and_runs_these_nodes(synths, env):
    ((_, _, group),) = list(
        _resources(synths[env], "AWS::ElastiCache::ReplicationGroup")
    )
    props = _props(group)
    retention, nodes, failover = REDIS[env]
    assert props["SnapshotWindow"] == "02:00-03:00"
    assert props["SnapshotRetentionLimit"] == retention
    assert props["NumCacheClusters"] == nodes
    assert bool(props.get("AutomaticFailoverEnabled")) is failover
    # No parameter group of its own: the engine's default, which keeps no
    # append-only file.
    assert "CacheParameterGroupName" not in props
    assert not list(_resources(synths[env], "AWS::ElastiCache::ParameterGroup"))


@pytest.mark.parametrize("env", ("staging", "prod"))
def test_the_database_login_is_one_generated_secret_in_this_region_only(synths, env):
    secrets = list(_resources(synths[env], "AWS::SecretsManager::Secret"))
    assert [_props(r)["Name"] for _, _, r in secrets] == [
        f"experimentation-database-{env}-aurora-credentials"
    ]
    ((_, _, secret),) = secrets
    assert "GenerateSecretString" in _props(secret)
    assert "ReplicaRegions" not in _props(secret)


@pytest.mark.parametrize("env", ("staging", "prod"))
def test_the_ecs_logs_are_kept_90_and_30_days_and_nowhere_else(synths, env):
    retention = {
        _props(r)["LogGroupName"]: _props(r).get("RetentionInDays")
        for _, _, r in _resources(synths[env], "AWS::Logs::LogGroup")
        if isinstance(_props(r).get("LogGroupName"), str)
    }
    assert retention[f"/ecs/experimentation-backend-{env}"] == 90
    assert retention[f"/ecs/experimentation-dashboard-{env}"] == 90
    assert retention[f"/ecs/experimentation-migrate-{env}"] == 30
    assert not list(_resources(synths[env], "AWS::Logs::SubscriptionFilter"))
    assert not list(_resources(synths[env], "AWS::Logs::Destination"))


@pytest.mark.parametrize("env", ("staging", "prod"))
def test_the_full_profiles_three_buckets(synths, env):
    buckets = {
        (stack, lid): r for stack, lid, r in _resources(synths[env], "AWS::S3::Bucket")
    }
    if not MODULES_PRESENT:
        assert buckets == {}, sorted(buckets)
        return
    by_id = {
        re.sub(r"[0-9A-F]{8}$", "", lid): (stack, r)
        for (stack, lid), r in buckets.items()
    }
    assert set(by_id) == {"DataLakeBucket", "AthenaResultsBucket", "GlueScriptsBucket"}
    lake_stack, lake = by_id["DataLakeBucket"]
    assert lake_stack == f"experimentation-analytics-{env}"
    assert _props(lake)["VersioningConfiguration"] == {"Status": "Enabled"}
    # Only the data lake: nothing restores an object in the other two.
    for name in ("AthenaResultsBucket", "GlueScriptsBucket"):
        versioning = _props(by_id[name][1]).get("VersioningConfiguration")
        assert versioning in (None, {"Status": "Suspended"}), (name, versioning)
    results_stack, results = by_id["AthenaResultsBucket"]
    assert results_stack == f"experimentation-glue-etl-{env}"
    rules = _props(results)["LifecycleConfiguration"]["Rules"]
    assert [rule["ExpirationInDays"] for rule in rules] == [30]
    assert by_id["GlueScriptsBucket"][0] == f"experimentation-glue-etl-{env}"
    for _, bucket in by_id.values():
        assert "ReplicationConfiguration" not in _props(bucket)
        assert bucket["DeletionPolicy"] == ("Retain" if env == "prod" else "Delete")


# --- the deployment group's two waits ----------------------------------------------------


@pytest.mark.parametrize("env", ("staging", "prod"))
def test_codedeploy_keeps_the_previous_task_set_an_hour_after_the_shift(synths, env):
    ((_, _, group),) = list(_resources(synths[env], "AWS::CodeDeploy::DeploymentGroup"))
    config = _props(group)["BlueGreenDeploymentConfiguration"]
    assert config["TerminateBlueInstancesOnDeploymentSuccess"] == {
        "Action": "TERMINATE",
        "TerminationWaitTimeInMinutes": 60,
    }


# --- a second region -------------------------------------------------------------------------


@pytest.mark.parametrize("env", ("staging", "prod"))
def test_nothing_is_built_for_a_second_region(synths, env):
    """No Aurora global database (so a cdk deploy in a new region creates a
    new, empty cluster), no secret replica, no ECR repository, no bucket
    replication."""
    templates = synths[env]
    assert not list(_resources(templates, "AWS::RDS::GlobalCluster"))
    ((_, _, cluster),) = list(_resources(templates, "AWS::RDS::DBCluster"))
    for restored_from in (
        "GlobalClusterIdentifier",
        "SnapshotIdentifier",
        "SourceDBClusterIdentifier",
        "RestoreType",
        "SourceRegion",
    ):
        assert restored_from not in _props(cluster), restored_from
    assert not list(_resources(templates, "AWS::ECR::Repository"))
    for _, _, secret in _resources(templates, "AWS::SecretsManager::Secret"):
        assert "ReplicaRegions" not in _props(secret)
    for _, _, bucket in _resources(templates, "AWS::S3::Bucket"):
        assert "ReplicationConfiguration" not in _props(bucket)
    blob = json.dumps(templates)
    assert "AWS::DynamoDB::GlobalTable" not in blob


# --- the secrets the task definitions carry -------------------------------------------------


@pytest.mark.parametrize("env", ("staging", "prod"))
def test_both_backend_task_definitions_carry_the_secrets_you_create(synths, env):
    """The API's and the migration's: SECRET_KEY and FIRST_SUPERUSER_PASSWORD
    from /<env>/experimentation/jwt-secret and first-superuser-password, and on
    the full profile AUDIT_HMAC_KEY from audit-hmac-key, each by complete ARN."""
    expected = {
        "SECRET_KEY": "jwt-secret",
        "FIRST_SUPERUSER_PASSWORD": "first-superuser-password",
    }
    if MODULES_PRESENT:
        expected["AUDIT_HMAC_KEY"] = "audit-hmac-key"
    families = {}
    for _, _, definition in _resources(synths[env], "AWS::ECS::TaskDefinition"):
        family = _props(definition)["Family"]
        (container, *_) = _props(definition)["ContainerDefinitions"]
        families[family] = {
            s["Name"]: s["ValueFrom"] for s in container.get("Secrets", [])
        }
    for family in (f"experimentation-backend-{env}", f"experimentation-migrate-{env}"):
        literal = {k: v for k, v in families[family].items() if isinstance(v, str)}
        assert set(literal) == set(expected), (family, sorted(literal))
        for variable, name in expected.items():
            assert re.fullmatch(
                rf"arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{{12}}:secret:/{env}/experimentation/{name}-\w{{6}}",
                literal[variable],
            ), (family, variable)


# --- the revisions a cdk deploy registers ----------------------------------------------------


@pytest.mark.parametrize("env", ("staging", "prod"))
def test_a_cdk_deploy_registers_revisions_naming_the_bootstrap_tag(synths, env):
    """Without -c backend_image_tag / dashboard_image_tag, every task
    definition the stacks register names the `bootstrap` tag of the two
    repositories, never a released build; the stacks create neither
    repository."""
    images = {}
    for _, _, definition in _resources(synths[env], "AWS::ECS::TaskDefinition"):
        for container in _props(definition)["ContainerDefinitions"]:
            images[(_props(definition)["Family"], container["Name"])] = json.dumps(
                container["Image"]
            )
    assert set(images) == {
        (f"experimentation-backend-{env}", "backend"),
        (f"experimentation-migrate-{env}", "backend"),
        (f"experimentation-dashboard-{env}", "dashboard"),
    }, sorted(images)
    for (family, container), image in images.items():
        repository = "web" if container == "dashboard" else "backend"
        assert f"experimentation-platform/{repository}:bootstrap" in image, (
            family,
            image,
        )
    assert not list(_resources(synths[env], "AWS::ECR::Repository"))
