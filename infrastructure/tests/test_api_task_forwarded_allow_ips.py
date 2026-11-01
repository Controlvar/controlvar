"""The API task believes X-Forwarded-For from the VPC its load balancer is in.

The API image believes loopback and the networks its container is directly
attached to, plus ``FORWARDED_ALLOW_IPS``. On Fargate the task's own network
is its private subnet, and the load balancer sits in the public subnets, so
the API task definition sets ``FORWARDED_ALLOW_IPS`` to the VPC's range; the
task admits connections from the load balancer alone. Without it every request
would resolve to a load balancer address.

The value is compared EXACTLY with the VPC's CidrBlock in the same synthesised
assembly, for staging and prod, core and full (the construction
``test_api_task_does_not_migrate.py`` uses). The migration and dashboard tasks
serve no traffic through the load balancer to the API and do not carry it.
"""

from __future__ import annotations

import pytest

from .test_api_task_does_not_migrate import CASES, _synth, core_checkout  # noqa: F401
from .test_app_profiles import CDK_DIR, _app_environment

SETTING = "FORWARDED_ALLOW_IPS"


def migration_task_family(cdk_dir, environment: str) -> str:
    """The migration task's family, from the app's own ``stacks/names.py``."""
    with _app_environment(cdk_dir, ENVIRONMENT=environment):
        from stacks.names import migration_task_family as family

        return family(environment)


def _resources(assembly, kind: str) -> list:
    return [
        resource
        for stack in assembly.stacks
        for resource in stack.template.get("Resources", {}).values()
        if resource["Type"] == kind
    ]


def _containers(assembly, family: str) -> list:
    return [
        container
        for resource in _resources(assembly, "AWS::ECS::TaskDefinition")
        if resource["Properties"].get("Family") == family
        for container in resource["Properties"].get("ContainerDefinitions", [])
    ]


def _environment(container: dict) -> dict:
    return {e["Name"]: e["Value"] for e in container.get("Environment", [])}


@pytest.fixture(scope="module", params=CASES)
def synthesised(request, core_checkout):  # noqa: F811
    environment, profile = request.param
    cdk_dir = core_checkout if profile == "core" else CDK_DIR
    assembly, built = _synth(cdk_dir, environment)
    assert built == profile, f"asked for the {profile} profile, app.py built {built}"
    vpcs = _resources(assembly, "AWS::EC2::VPC")
    assert len(vpcs) == 1, f"{environment}/{profile}: {len(vpcs)} VPCs"
    api = [
        c
        for c in _containers(assembly, f"experimentation-backend-{environment}")
        if c["Name"] == "backend"
    ]
    assert len(api) == 1, f"{environment}/{profile}: {len(api)} API backend containers"
    others = {
        "migration": _containers(assembly, migration_task_family(cdk_dir, environment)),
        "dashboard": _containers(assembly, f"experimentation-dashboard-{environment}"),
    }
    for name, containers in others.items():
        assert containers, f"{environment}/{profile}: no {name} task found"
    return {
        "label": f"{environment}/{profile}",
        "cidr": vpcs[0]["Properties"]["CidrBlock"],
        "api": _environment(api[0]),
        "others": {
            name: [_environment(c) for c in containers]
            for name, containers in others.items()
        },
    }


@pytest.mark.unit
@pytest.mark.regression
def test_the_api_task_believes_exactly_the_vpc(synthesised):
    env = synthesised["api"]
    assert SETTING in env, f"{synthesised['label']}: the API task sets no {SETTING}"
    assert env[SETTING] == synthesised["cidr"], (
        f"{synthesised['label']}: {SETTING} is {env[SETTING]!r}, the VPC is "
        f"{synthesised['cidr']!r}"
    )
    assert env[SETTING] == "10.0.0.0/16"


@pytest.mark.unit
def test_the_migration_and_dashboard_tasks_do_not_carry_it(synthesised):
    for name, environments in synthesised["others"].items():
        for env in environments:
            assert SETTING not in env, (
                f"{synthesised['label']}: the {name} task must not carry {SETTING}"
            )
