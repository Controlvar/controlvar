"""The deployed backend tasks can reach this environment's Aurora (#78).

Three separate things stood between a task and its database, each fatal:

* **No host.** Neither task definition set ``POSTGRES_SERVER`` (the migration
  task only when ``db_host`` was passed), so the application connected to
  ``localhost``: the entry point waited ``DB_WAIT_TIMEOUT`` for a database
  inside its own container and exited 1, and ``/health`` -- the ALB's health
  check, which runs the database check -- could never pass.
* **The wrong password.** ``POSTGRES_PASSWORD`` came from
  ``/<env>/experimentation/db-password``, a secret made by hand that nothing
  ever set on the cluster; Aurora's master password is the one the database
  stack generated into its own secret.
* **No route.** Aurora's security group was created with no ingress at all.

Asserted on the synthesised **staging and prod** templates, by resolving each
``Fn::ImportValue`` back to the export and the resource behind it: a host that
is the read endpoint, a literal, the hand-made secret, or a missing ingress
rule each fails here with a message naming it. This replaces the AST check in
``backend/tests/unit/infrastructure/test_migration_task_command.py``, which
passed with ``db_host=None``.
"""

from __future__ import annotations

import runpy
import sys

import pytest

from .test_app_profiles import CDK_DIR, _app_environment

ENVIRONMENTS = ["staging", "prod"]
BACKEND_STACKS = ["fargate", "migrations"]


class _Synth:
    """One environment's synthesised app, with the export graph resolved."""

    def __init__(self, env: str) -> None:
        self.env = env
        with _app_environment(CDK_DIR, ENVIRONMENT=env):
            namespace = runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")
        assembly = namespace["app"].synth()
        self.templates = {s.stack_name: s.template for s in assembly.stacks}
        self.exports: dict[str, tuple[str, object]] = {}
        for name, template in self.templates.items():
            for output in template.get("Outputs", {}).values():
                export = output.get("Export", {}).get("Name")
                if export:
                    self.exports[export] = (name, output["Value"])

    def stack(self, kind: str) -> dict:
        return self.templates[f"experimentation-{kind}-{self.env}"]

    def resources(self, kind: str) -> dict:
        return self.stack(kind)["Resources"]

    def resolve_import(self, value: object, where: str) -> tuple[str, object]:
        """``{"Fn::ImportValue": name}`` -> (exporting stack, exported value)."""
        assert isinstance(value, dict) and set(value) == {"Fn::ImportValue"}, (
            f"{where} is not imported from another stack: {value!r}"
        )
        name = value["Fn::ImportValue"]
        assert name in self.exports, f"{where} imports {name!r}, which nothing exports"
        return self.exports[name]

    def cluster(self) -> tuple[str, dict]:
        clusters = [
            (lid, r)
            for lid, r in self.resources("database").items()
            if r["Type"] == "AWS::RDS::DBCluster"
        ]
        assert len(clusters) == 1, clusters
        return clusters[0]

    def container(self, kind: str) -> dict:
        # The fargate stack also holds the dashboard's task definition, which
        # has no database; select the backend's by family.
        family = {
            "fargate": f"experimentation-backend-{self.env}",
            "migrations": f"experimentation-migrate-{self.env}",
        }[kind]
        task_defs = [
            r
            for r in self.resources(kind).values()
            if r["Type"] == "AWS::ECS::TaskDefinition"
            and r["Properties"].get("Family") == family
        ]
        assert len(task_defs) == 1, (
            f"{kind}: {len(task_defs)} task definitions of {family}"
        )
        (container,) = [
            c
            for c in task_defs[0]["Properties"]["ContainerDefinitions"]
            if c["Name"] == "backend"
        ]
        return container


@pytest.fixture(scope="module", params=ENVIRONMENTS)
def synth(request) -> _Synth:
    return _Synth(request.param)


def _env_value(container: dict, name: str):
    found = [e["Value"] for e in container.get("Environment", []) if e["Name"] == name]
    assert len(found) == 1, f"{name} appears {len(found)} times in the environment"
    return found[0]


def _secret_value_from(container: dict, name: str):
    found = [s["ValueFrom"] for s in container.get("Secrets", []) if s["Name"] == name]
    assert len(found) == 1, f"{name} appears {len(found)} times in the secrets"
    return found[0]


@pytest.mark.regression
@pytest.mark.parametrize("kind", BACKEND_STACKS)
def test_postgres_server_is_this_environments_writer_endpoint(synth: _Synth, kind: str):
    where = f"experimentation-{kind}-{synth.env}/backend POSTGRES_SERVER"
    value = _env_value(synth.container(kind), "POSTGRES_SERVER")
    exporter, exported = synth.resolve_import(value, where)

    assert exporter == f"experimentation-database-{synth.env}", (
        f"{where} comes from {exporter}, not this environment's database stack"
    )
    cluster_id, _ = synth.cluster()
    assert exported == {"Fn::GetAtt": [cluster_id, "Endpoint.Address"]}, (
        f"{where} is {exported!r}, not the cluster's writer endpoint "
        f"({cluster_id}.Endpoint.Address); the read endpoint refuses writes"
    )


@pytest.mark.regression
@pytest.mark.parametrize("kind", BACKEND_STACKS)
@pytest.mark.parametrize(
    ("variable", "field"),
    [("POSTGRES_PASSWORD", "password"), ("POSTGRES_USER", "username")],
)
def test_credentials_are_the_secret_aurora_was_created_with(
    synth: _Synth, kind: str, variable: str, field: str
):
    where = f"experimentation-{kind}-{synth.env}/backend {variable}"
    value_from = _secret_value_from(synth.container(kind), variable)

    # ECS spells "field F of secret S" as "<S's ARN>:F::".
    assert isinstance(value_from, dict) and "Fn::Join" in value_from, (
        f"{where} is {value_from!r}: not a field of the database stack's secret"
    )
    separator, parts = value_from["Fn::Join"]
    assert separator == "" and len(parts) == 2 and parts[1] == f":{field}::", (
        f"{where} does not read the `{field}` field: {value_from!r}"
    )
    exporter, exported = synth.resolve_import(parts[0], where)
    assert exporter == f"experimentation-database-{synth.env}", (
        f"{where} reads a secret exported by {exporter}"
    )
    assert isinstance(exported, dict) and set(exported) == {"Ref"}, exported
    secret_id = exported["Ref"]
    secret = synth.resources("database")[secret_id]
    assert secret["Type"] == "AWS::SecretsManager::Secret", secret["Type"]
    assert (
        secret["Properties"]["GenerateSecretString"]["GenerateStringKey"] == "password"
    )

    # ...and it is the secret the cluster's master credentials resolve from.
    _, cluster = synth.cluster()
    master = cluster["Properties"]["MasterUserPassword"]
    assert {"Ref": secret_id} in master["Fn::Join"][1], (
        f"the cluster's master password is not read from {secret_id}: {master!r}"
    )


@pytest.mark.regression
def test_the_hand_made_db_password_secret_is_gone(synth: _Synth):
    """Nothing may still name `/<env>/experimentation/db-password`."""
    for kind in BACKEND_STACKS:
        text = str(synth.stack(kind))
        assert "db-password" not in text, (
            f"experimentation-{kind}-{synth.env} still references the hand-made "
            "db-password secret, which Aurora never used"
        )


@pytest.mark.regression
def test_the_ecs_tasks_are_admitted_to_aurora_on_5432(synth: _Synth):
    where = f"experimentation-fargate-{synth.env}"
    rules = [
        (lid, r["Properties"])
        for lid, r in synth.resources("fargate").items()
        if r["Type"] == "AWS::EC2::SecurityGroupIngress"
        and r["Properties"].get("FromPort") == 5432
    ]
    assert len(rules) == 1, f"{where}: expected one ingress rule on 5432, found {rules}"
    _, rule = rules[0]
    assert rule["ToPort"] == 5432 and rule["IpProtocol"] == "tcp", rule
    assert "CidrIp" not in rule and "CidrIpv6" not in rule, rule

    # To Aurora's own security group...
    exporter, target = synth.resolve_import(rule["GroupId"], f"{where} ingress GroupId")
    assert exporter == f"experimentation-database-{synth.env}", exporter
    _, cluster = synth.cluster()
    assert target["Fn::GetAtt"][1] == "GroupId", target
    assert {"Fn::GetAtt": [target["Fn::GetAtt"][0], "GroupId"]} in cluster[
        "Properties"
    ]["VpcSecurityGroupIds"], (
        f"the ingress opens {target!r}, which is not a group the cluster is in"
    )

    # ...from the group the API service's tasks run in (the dashboard service
    # beside it has its own group and no business with the database).
    services = [
        r
        for r in synth.resources("fargate").values()
        if r["Type"] == "AWS::ECS::Service"
        and r["Properties"].get("ServiceName") == f"experimentation-backend-{synth.env}"
    ]
    assert len(services) == 1, services
    service_groups = services[0]["Properties"]["NetworkConfiguration"][
        "AwsvpcConfiguration"
    ]["SecurityGroups"]
    assert rule["SourceSecurityGroupId"] in service_groups, (
        f"the ingress admits {rule['SourceSecurityGroupId']!r}; the service runs "
        f"in {service_groups!r}"
    )

    # The migration task is told to run in that same group.
    output = synth.stack("migrations")["Outputs"]["MigrationSecurityGroupId"]["Value"]
    assert output == rule["SourceSecurityGroupId"], (
        f"the migration task's security group {output!r} is not the one "
        "admitted to Aurora"
    )


class TestSynthRefusesAMissingDatabase:
    """A stack built without a host would silently mean localhost."""

    @staticmethod
    def _require():
        sys.path.insert(0, str(CDK_DIR))
        try:
            from stacks.database_access import require_database
        finally:
            sys.path.remove(str(CDK_DIR))
        return require_database

    @pytest.mark.regression
    @pytest.mark.parametrize(
        "host", [None, "", "  ", "localhost", "127.0.0.1", "LOCALHOST"]
    )
    def test_no_host_or_a_loopback_host_is_refused(self, host):
        with pytest.raises(ValueError, match="db_host"):
            self._require()("SomeStack", host, object())

    def test_missing_credentials_are_refused(self):
        with pytest.raises(ValueError, match="db_credentials"):
            self._require()("SomeStack", "aurora.example.internal", None)

    def test_a_real_host_with_credentials_passes(self):
        self._require()("SomeStack", "aurora.example.internal", object())


@pytest.mark.regression
def test_backups_and_instances_per_environment(synth: _Synth):
    """The disaster recovery plan, the runbook and the backup page: 35 days of
    point-in-time backups in staging and prod, and a writer plus a reader in
    prod only (one writer in staging)."""
    _, cluster = synth.cluster()
    assert cluster["Properties"]["BackupRetentionPeriod"] == 35
    # Each snapshot gets the cluster's tags: the IAM page's rds:AddTagsToResource.
    assert cluster["Properties"]["CopyTagsToSnapshot"] is True
    instances = [
        r
        for r in synth.resources("database").values()
        if r["Type"] == "AWS::RDS::DBInstance"
    ]
    assert len(instances) == {"staging": 1, "prod": 2}[synth.env], len(instances)


@pytest.mark.regression
def test_dev_keeps_one_day_of_backups():
    """ "1 day elsewhere": the environments outside staging and prod."""
    _, cluster = _Synth("dev").cluster()
    assert cluster["Properties"]["BackupRetentionPeriod"] == 1


@pytest.mark.regression
def test_the_database_key_is_kept_in_prod_only(synth: _Synth):
    """The CDK page: outside prod the database's KMS key goes with the stack
    (scheduled for deletion); in prod it is retained with the final snapshot."""
    (key,) = [
        r for r in synth.resources("database").values() if r["Type"] == "AWS::KMS::Key"
    ]
    expected = {"staging": "Delete", "prod": "Retain"}[synth.env]
    assert key.get("DeletionPolicy", "Delete") == expected, key.get("DeletionPolicy")
