"""What the stacks leave to a person, and what no stack schedules.

Asserted on a real synth of every environment, in both profiles:

* no stack creates an identity that could deploy: no IAM user, no OIDC
  provider, and no role any principal but an AWS service may assume. The
  bootstrap identity, the ``cdk deploy`` identity and the GitHub workflow role
  are all set up by a person;
* no stack creates the ECR repositories, the ``/<env>/experimentation/``
  secrets, the certificate or a DNS record, while the stacks use all four: a
  person creates them before the first deploy;
* each environment reads its own secrets, ``/<env>/experimentation/<name>``,
  the same three names in each;
* no stack schedules a secret's rotation or a backup.
"""

from __future__ import annotations

import json
import runpy

import pytest

from .test_app_profiles import (
    CDK_DIR,
    FAKE_ACCOUNT,
    FAKE_REGION,
    SECRET_ARN_INPUTS,
    _app_environment,
    fake_secret_arn,
)

ENVIRONMENTS = ("dev", "staging", "prod", "demo")
PROFILES = ("core", "full")
CERTIFICATE = (
    f"arn:aws:acm:{FAKE_REGION}:{FAKE_ACCOUNT}:certificate/"
    "00000000-0000-0000-0000-000000000000"
)


def _templates(environment: str, profile: str) -> dict[str, dict]:
    with _app_environment(
        CDK_DIR, ENVIRONMENT=environment, EXPERIMENTLY_PROFILE=profile
    ):
        namespace = runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")
    return {s.stack_name: s.template for s in namespace["app"].synth().stacks}


@pytest.fixture(scope="module")
def synths() -> dict[tuple[str, str], dict[str, dict]]:
    return {(e, p): _templates(e, p) for e in ENVIRONMENTS for p in PROFILES}


def _resources(templates: dict[str, dict]):
    for stack, template in sorted(templates.items()):
        for logical_id, resource in sorted(template.get("Resources", {}).items()):
            yield stack, logical_id, resource


def _principals(document: dict) -> list:
    return [s.get("Principal") for s in document.get("Statement", [])]


@pytest.mark.regression
def test_no_stack_creates_an_identity_that_could_deploy(synths):
    roles = 0
    for templates in synths.values():
        for stack, lid, resource in _resources(templates):
            kind = resource["Type"]
            assert kind not in (
                "AWS::IAM::User",
                "AWS::IAM::Group",
                "AWS::IAM::AccessKey",
                "AWS::IAM::OIDCProvider",
                "AWS::IAM::SAMLProvider",
                "Custom::AWSCDKOpenIdConnectProvider",
            ), (stack, lid, kind)
            if kind == "AWS::IAM::Role":
                roles += 1
                trust = resource["Properties"]["AssumeRolePolicyDocument"]
                for principal in _principals(trust):
                    assert isinstance(principal, dict) and set(principal) == {
                        "Service"
                    }, (stack, lid, principal)
    # Not vacuous: the task roles, the CodeDeploy role and the Lambda roles.
    assert roles > 50, roles


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_what_a_person_creates_by_hand_no_stack_creates(synths, environment):
    templates = synths[(environment, "full")]
    kinds = {r["Type"] for _, _, r in _resources(templates)}
    for made_by_hand in (
        "AWS::ECR::Repository",
        "AWS::CertificateManager::Certificate",
        "AWS::Route53::RecordSet",
        "AWS::Route53::RecordSetGroup",
        "AWS::Route53::HostedZone",
    ):
        assert made_by_hand not in kinds, made_by_hand
    prefix = f"/{environment}/experimentation/"
    for stack, lid, resource in _resources(templates):
        if resource["Type"] == "AWS::SecretsManager::Secret":
            name = str(resource["Properties"].get("Name"))
            assert not name.startswith(prefix), (stack, lid, name)
    text = json.dumps(templates)
    # ... while the stacks use every one of them.
    for repository in (
        "experimentation-platform/backend",
        "experimentation-platform/web",
    ):
        assert f"/{repository}" in text, repository
    assert CERTIFICATE in text
    for variable in SECRET_ARN_INPUTS:
        assert fake_secret_arn(variable, environment) in text, variable


@pytest.mark.regression
def test_each_environment_reads_its_own_secrets_by_the_same_names(synths):
    names: dict[str, dict[str, str]] = {}
    for environment in ENVIRONMENTS:
        found = {}
        for _, _, resource in _resources(synths[(environment, "full")]):
            if resource["Type"] != "AWS::ECS::TaskDefinition":
                continue
            for container in resource["Properties"]["ContainerDefinitions"]:
                for secret in container.get("Secrets", []):
                    value = secret["ValueFrom"]
                    if isinstance(value, str) and ":secret:/" in value:
                        found[secret["Name"]] = value.split(":secret:", 1)[1]
        assert set(found) == {
            "SECRET_KEY",
            "FIRST_SUPERUSER_PASSWORD",
            "AUDIT_HMAC_KEY",
        }
        prefix = f"/{environment}/experimentation/"
        assert all(v.startswith(prefix) for v in found.values()), found
        names[environment] = {k: v[len(prefix) :] for k, v in found.items()}
    assert names["staging"] == names["prod"] == names["dev"] == names["demo"], names


@pytest.mark.regression
@pytest.mark.parametrize("variable", sorted(SECRET_ARN_INPUTS))
def test_a_staging_synth_refuses_a_production_secret(variable):
    with pytest.raises(ValueError) as refused:
        with _app_environment(
            CDK_DIR,
            ENVIRONMENT="staging",
            **{variable: fake_secret_arn(variable, "prod")},
        ):
            runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")
    assert variable in str(refused.value)


#: What a scheduled rotation or backup would have to name somewhere: the
#: resource that schedules it, or the call (or its IAM action) a scheduled
#: function would make.
SCHEDULING = (
    "RotationRules",
    "RotationSchedule",
    "rotate_secret",
    "RotateSecret",
    "CreateBackup",
    "create_backup",
    "StartBackupJob",
    "CreateDBClusterSnapshot",
    "create_db_cluster_snapshot",
    "CreateSnapshot",
    "create_snapshot",
)


@pytest.mark.regression
def test_no_stack_schedules_a_rotation_or_a_backup(synths):
    seen = set()
    for templates in synths.values():
        for stack, lid, resource in _resources(templates):
            kind = resource["Type"]
            seen.add(kind)
            assert not kind.startswith("AWS::Backup::"), (stack, lid, kind)
            text = json.dumps(resource)
            for word in SCHEDULING:
                assert word not in text, (stack, lid, word)
    # Not vacuous: the secrets and tables these would schedule are there.
    assert {"AWS::SecretsManager::Secret", "AWS::DynamoDB::Table"} <= seen
