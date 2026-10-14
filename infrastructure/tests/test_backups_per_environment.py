"""The backups each data store gets, per environment, on a real synth.

* Aurora: the retention ``aurora_backup_retention_days()`` returns (35 days in
  prod and staging, 1 elsewhere), no preferred backup or maintenance window,
  a final snapshot when the prod cluster is deleted, and a generated master
  password with no ``"``, ``@``, ``/`` or ``\\``;
* DynamoDB: point-in-time recovery on every table, in both profiles;
* Redis: a daily snapshot in 02:00-03:00 UTC, maintenance on Sunday
  04:00-05:00 UTC, snapshots kept 7 days in prod, 3 in staging, 1 elsewhere;
* ``retains_data(env)`` is ``env == "prod"``, and only prod's data outlives
  its stacks; outside prod each bucket is emptied by the CDK's own provider
  before it is deleted.
"""

from __future__ import annotations

import runpy
import sys

import pytest

from .test_app_profiles import CDK_DIR, _app_environment

ENVIRONMENTS = ("dev", "staging", "prod", "demo")
AURORA_RETENTION = {"dev": 1, "staging": 35, "prod": 35, "demo": 1}
REDIS_RETENTION = {"dev": 1, "staging": 3, "prod": 7, "demo": 1}


def _templates(environment: str, profile: str = "full") -> dict[str, dict]:
    with _app_environment(
        CDK_DIR, ENVIRONMENT=environment, EXPERIMENTLY_PROFILE=profile
    ):
        namespace = runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")
    return {s.stack_name: s.template for s in namespace["app"].synth().stacks}


@pytest.fixture(scope="module")
def synths() -> dict[str, dict[str, dict]]:
    return {environment: _templates(environment) for environment in ENVIRONMENTS}


def _of_type(templates: dict[str, dict], kind: str) -> list[dict]:
    return [
        resource
        for template in templates.values()
        for resource in template.get("Resources", {}).values()
        if resource["Type"] == kind
    ]


def _environments_module():
    sys.path.insert(0, str(CDK_DIR))
    try:
        for name in [m for m in sys.modules if m.split(".")[0] == "stacks"]:
            del sys.modules[name]
        from stacks import environments
    finally:
        sys.path.remove(str(CDK_DIR))
    return environments


# --- Aurora -------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_aurora_keeps_the_retention_the_environment_function_returns(
    synths, environment
):
    environments = _environments_module()
    assert (
        environments.aurora_backup_retention_days(environment)
        == (AURORA_RETENTION[environment])
    )
    (cluster,) = _of_type(synths[environment], "AWS::RDS::DBCluster")
    assert cluster["Properties"]["BackupRetentionPeriod"] == (
        environments.aurora_backup_retention_days(environment)
    )


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_aurora_sets_no_backup_or_maintenance_window(synths, environment):
    (cluster,) = _of_type(synths[environment], "AWS::RDS::DBCluster")
    instances = _of_type(synths[environment], "AWS::RDS::DBInstance")
    assert instances, "the scan found no Aurora instance"
    for resource in (cluster, *instances):
        properties = resource["Properties"]
        assert "PreferredBackupWindow" not in properties, properties
        assert "PreferredMaintenanceWindow" not in properties, properties


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_only_prod_takes_a_final_snapshot_of_its_cluster(synths, environment):
    (cluster,) = _of_type(synths[environment], "AWS::RDS::DBCluster")
    expected = "Snapshot" if environment == "prod" else "Delete"
    assert cluster["DeletionPolicy"] == expected
    assert cluster["UpdateReplacePolicy"] == expected


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_the_generated_master_password_leaves_out_four_characters(synths, environment):
    (secret,) = _of_type(synths[environment], "AWS::SecretsManager::Secret")
    generate = secret["Properties"]["GenerateSecretString"]
    assert generate["GenerateStringKey"] == "password"
    assert sorted(generate["ExcludeCharacters"]) == sorted('"@/\\')
    # Nothing else is left out: punctuation is allowed.
    assert generate.get("ExcludePunctuation") in (None, False), generate
    for flag in ("ExcludeLowercase", "ExcludeUppercase", "ExcludeNumbers"):
        assert generate.get(flag) in (None, False), (flag, generate)


# --- DynamoDB -----------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("profile", ["core", "full"])
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_every_dynamodb_table_has_point_in_time_recovery(environment, profile):
    tables = _of_type(_templates(environment, profile), "AWS::DynamoDB::Table")
    assert len(tables) == (3 if profile == "core" else 4), len(tables)
    for table in tables:
        assert table["Properties"]["PointInTimeRecoverySpecification"] == {
            "PointInTimeRecoveryEnabled": True
        }, table["Properties"].get("TableName")


# --- Redis --------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_redis_snapshots_daily_in_its_window_and_keeps_them_per_environment(
    synths, environment
):
    (group,) = _of_type(synths[environment], "AWS::ElastiCache::ReplicationGroup")
    properties = group["Properties"]
    assert properties["SnapshotWindow"] == "02:00-03:00"
    assert properties["PreferredMaintenanceWindow"] == "sun:04:00-sun:05:00"
    assert properties["SnapshotRetentionLimit"] == REDIS_RETENTION[environment]


# --- what outlives the stacks ---------------------------------------------------


@pytest.mark.regression
def test_only_prod_retains_its_data():
    environments = _environments_module()
    for environment in (*ENVIRONMENTS, "qa", "production", "Prod", ""):
        assert environments.retains_data(environment) == (environment == "prod")


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_a_bucket_outside_prod_is_emptied_by_the_cdks_provider(synths, environment):
    """``auto_delete_objects=True``: a custom resource per bucket, backed by
    the CDK's own provider function with its own role, which the bucket's
    policy lets change the policy (to refuse new writes), list, and delete
    every object version."""
    provider = "CustomS3AutoDeleteObjectsCustomResourceProvider"
    found = 0
    for stack, template in synths[environment].items():
        resources = template.get("Resources", {})
        buckets = [
            lid for lid, r in resources.items() if r["Type"] == "AWS::S3::Bucket"
        ]
        emptied = {
            r["Properties"]["BucketName"]["Ref"]: r["Properties"]["ServiceToken"]
            for r in resources.values()
            if r["Type"] == "Custom::S3AutoDeleteObjects"
        }
        if environment == "prod":
            assert not emptied, (stack, emptied)
            assert not [lid for lid in resources if lid.startswith(provider)], stack
            continue
        assert sorted(emptied) == sorted(buckets), stack
        for bucket, token in emptied.items():
            handler = token["Fn::GetAtt"][0]
            assert handler.startswith(f"{provider}Handler"), handler
            role = resources[handler]["Properties"]["Role"]["Fn::GetAtt"][0]
            assert role.startswith(f"{provider}Role"), role
            (policy,) = [
                r["Properties"]["PolicyDocument"]["Statement"]
                for r in resources.values()
                if r["Type"] == "AWS::S3::BucketPolicy"
                and r["Properties"]["Bucket"] == {"Ref": bucket}
            ]
            grants = [
                set(s["Action"])
                for s in policy
                if s["Principal"] == {"AWS": {"Fn::GetAtt": [role, "Arn"]}}
            ]
            assert grants == [
                {"s3:PutBucketPolicy", "s3:GetBucket*", "s3:List*", "s3:DeleteObject*"}
            ], grants
            found += 1
    assert found == (0 if environment == "prod" else 3)
