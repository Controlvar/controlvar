"""The AWS installer beta against the real synth (gates A7 and A6's CDK half).

deploy/aws-installer/lib/names.sh holds every infrastructure/cdk name the beta uses:
the nine stacks, the cluster, the services, the families, the log groups, the
secrets, the repositories, the topic, the fargate stack's outputs and the line app.py
prints for the profile. Each is compared here with a real staging synth of the core
profile, made the way install makes it: from a copy of infrastructure/cdk with no
modules/ beside it (the record's src/, a git archive of the release's tag). A name a
later change renames fails here, named, rather than in a reader's account.

A6: lib/env.sh's assert_cdk_target, run by bash on that synth's own cloud assembly,
passes it, and refuses the same assembly with a stack moved to us-east-1, with a tenth
stack, and the full profile's.

Synthesised without cdk.json's context, as test_teardown_instructions.py does; the
deployable mode's first synth is the real run's `cdk list`.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

from .test_app_profiles import CDK_DIR, FAKE_ACCOUNT, REPO_ROOT, _app_environment
from .test_secret_arns import _assembly

LIB = Path(REPO_ROOT) / "deploy" / "aws-installer" / "lib"
NAMES = LIB / "names.sh"


def names() -> dict[str, list[str]]:
    """Every NAMES_ variable lib/names.sh defines, read by bash itself."""
    script = (
        f'. "{NAMES}"; for v in $(compgen -v NAMES_); do '
        'eval "set -- \\"\\${$v[@]}\\""; for x in "$@"; do printf "%s=%s\\n" "$v" "$x"; done; done'
    )
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    out: dict[str, list[str]] = {}
    for line in done.stdout.splitlines():
        name, value = line.split("=", 1)
        out.setdefault(name, []).append(value)
    return out


class Synth:
    def __init__(self, printed: str, assembly) -> None:
        self.printed = printed.splitlines()
        self.assembly = assembly
        self.templates = {s.stack_name: s.template for s in assembly.stacks}
        self.text = json.dumps(self.templates)

    def values(self, kind: str, prop: str) -> set[str]:
        return {
            r["Properties"][prop]
            for t in self.templates.values()
            for r in t.get("Resources", {}).values()
            if r["Type"] == kind and isinstance(r.get("Properties", {}).get(prop), str)
        }

    def container_insights(self, cluster: str) -> bool:
        """The synth's cluster of that name has Container Insights on."""
        for t in self.templates.values():
            for r in t.get("Resources", {}).values():
                props = r.get("Properties", {})
                if (
                    r["Type"] == "AWS::ECS::Cluster"
                    and props.get("ClusterName") == cluster
                ):
                    return any(
                        s.get("Name") == "containerInsights"
                        and s.get("Value") == "enabled"
                        for s in props.get("ClusterSettings", [])
                    )
        return False

    def containers(self, family: str) -> set[str]:
        return {
            c["Name"]
            for t in self.templates.values()
            for r in t.get("Resources", {}).values()
            if r["Type"] == "AWS::ECS::TaskDefinition"
            and r["Properties"].get("Family") == family
            for c in r["Properties"].get("ContainerDefinitions", [])
        }


@pytest.fixture(scope="module")
def synth(tmp_path_factory) -> Synth:
    """staging, core, from a copy of infrastructure/cdk with no modules/ beside it."""
    src = tmp_path_factory.mktemp("installer-src") / "src"
    copy = src / "infrastructure" / "cdk"
    shutil.copytree(
        CDK_DIR,
        copy,
        ignore=shutil.ignore_patterns(
            "cdk.out", "__pycache__", "node_modules", ".pytest_cache"
        ),
    )
    printed = io.StringIO()
    with _app_environment(copy, ENVIRONMENT="staging", EXPERIMENTLY_PROFILE="core"):
        with contextlib.redirect_stdout(printed):
            namespace = runpy.run_path(str(copy / "app.py"), run_name="__main__")
            assembly = namespace["app"].synth()
    return Synth(printed.getvalue(), assembly)


#: How each name is found in the synth: (resource type, property) for a name a resource
#: carries; "stacks", "outputs" or "text" (anywhere in the templates) for the rest.
WHERE = {
    "NAMES_STACKS": "stacks",
    "NAMES_DESTROY_ORDER": "stacks",
    "NAMES_VPC_STACK": "stacks",
    "NAMES_STACK_PREFIX": "stacks",
    "NAMES_FARGATE_STACK": "stacks",
    "NAMES_FARGATE_OUTPUTS": "outputs",
    "NAMES_PROFILE_LINE": "printed",
    "NAMES_PROFILE_VARIABLE": "printed",
    "NAMES_CLUSTER": ("AWS::ECS::Cluster", "ClusterName"),
    "NAMES_LOAD_BALANCER": ("AWS::ElasticLoadBalancingV2::LoadBalancer", "Name"),
    "NAMES_API_SERVICE": ("AWS::ECS::Service", "ServiceName"),
    "NAMES_CODEDEPLOY_APPLICATION": ("AWS::CodeDeploy::Application", "ApplicationName"),
    "NAMES_DEPLOYMENT_GROUP": (
        "AWS::CodeDeploy::DeploymentGroup",
        "DeploymentGroupName",
    ),
    "NAMES_DB_KEY_DESCRIPTION": ("AWS::KMS::Key", "Description"),
    "NAMES_TABLES": ("AWS::DynamoDB::Table", "TableName"),
    "NAMES_FAMILIES": ("AWS::ECS::TaskDefinition", "Family"),
    "NAMES_MIGRATE_FAMILY": ("AWS::ECS::TaskDefinition", "Family"),
    "NAMES_LOG_GROUPS": ("AWS::Logs::LogGroup", "LogGroupName"),
    "NAMES_MIGRATE_LOG_GROUP": ("AWS::Logs::LogGroup", "LogGroupName"),
    "NAMES_ALARM_TOPIC": ("AWS::SNS::Topic", "TopicName"),
    "NAMES_DB_SECRET": ("AWS::SecretsManager::Secret", "Name"),
    "NAMES_CONTAINER_INSIGHTS_LOG_GROUP": "insights",
    "NAMES_BACKEND_CONTAINER": "containers",
    "NAMES_JWT_SECRET": "text",
    "NAMES_PASSWORD_SECRET": "text",
    "NAMES_BACKEND_REPOSITORY": "text",
    "NAMES_WEB_REPOSITORY": "text",
}


def missing(defined: dict[str, list[str]], synth: Synth) -> list[str]:
    """Each value lib/names.sh holds that the synth does not make, named."""
    stacks = sorted(synth.templates)
    problems = []
    for name, values in sorted(defined.items()):
        where = WHERE.get(name)
        if where is None:
            problems.append(f"{name}: no rule says where the synth makes it")
            continue
        for value in values:
            if where == "stacks":
                ok = (
                    (
                        name in ("NAMES_STACKS", "NAMES_DESTROY_ORDER")
                        and value in stacks
                    )
                    or (name == "NAMES_VPC_STACK" and value in stacks)
                    or (
                        name == "NAMES_STACK_PREFIX"
                        and all(s.startswith(value) for s in stacks)
                    )
                    or (name == "NAMES_FARGATE_STACK" and value in stacks)
                )
            elif where == "outputs":
                fargate = synth.templates.get(defined["NAMES_FARGATE_STACK"][0], {})
                ok = value in fargate.get("Outputs", {})
            elif where == "printed":
                line = "[cdk] " + defined["NAMES_PROFILE_LINE"][0]
                ok = line in synth.printed and (
                    name != "NAMES_PROFILE_VARIABLE" or f"{value}=core" in line
                )
            elif where == "containers":
                ok = all(
                    value in synth.containers(f)
                    for f in (
                        defined["NAMES_API_SERVICE"][0],
                        defined["NAMES_MIGRATE_FAMILY"][0],
                    )
                )
            elif where == "text":
                ok = value in synth.text
            elif where == "insights":
                # ECS makes /aws/ecs/containerinsights/<cluster>/performance for a cluster
                # with Container Insights on.
                ok = value == (
                    f"/aws/ecs/containerinsights/{defined['NAMES_CLUSTER'][0]}/performance"
                ) and synth.container_insights(defined["NAMES_CLUSTER"][0])
            else:
                ok = value in synth.values(*where)
            if not ok:
                problems.append(f"{name}: the synth makes no {value!r}")
    if sorted(defined.get("NAMES_STACKS", [])) != stacks:
        problems.append(f"NAMES_STACKS is not the synth's nine: {stacks}")
    if sorted(defined.get("NAMES_DESTROY_ORDER", [])) != stacks:
        problems.append(
            f"NAMES_DESTROY_ORDER is not the synth's nine, each once: {stacks}"
        )
    return problems


def test_every_name_the_beta_uses_is_the_synths(synth):
    assert len(synth.templates) == 9, sorted(synth.templates)
    problems = missing(names(), synth)
    assert problems == [], "\n".join(problems)


def test_uninstall_destroys_the_nine_in_an_order_the_synth_allows(synth):
    """F5: lib/names.sh's NAMES_DESTROY_ORDER, the order uninstall runs cdk destroy in,
    puts each stack before every stack it imports an export from (CloudFormation will not
    delete an export another stack imports) and before every stack it depends on, in the
    real staging-core synth; and the network's stack is the one holding the VPC (the stack
    uninstall's GuardDuty check reads)."""
    from .test_environments_do_not_collide import _exports, import_graph

    defined = names()
    order = defined["NAMES_DESTROY_ORDER"]
    position = {stack: n for n, stack in enumerate(order)}
    exports = _exports(synth)
    problems = []
    for export, importers in sorted(import_graph(synth).items()):
        for importer in sorted(importers):
            if position[importer] > position[exports[export]]:
                problems.append(
                    f"{importer} imports {export} from {exports[export]} and must be destroyed first"
                )
    for stack in synth.assembly.stacks:
        for dependency in stack.dependencies:
            name = getattr(dependency, "stack_name", None)
            if name is not None and position[stack.stack_name] > position[name]:
                problems.append(
                    f"{stack.stack_name} depends on {name} and must be destroyed first"
                )
    assert problems == [], "\n".join(problems)
    vpc = defined["NAMES_VPC_STACK"][0]
    assert any(
        r["Type"] == "AWS::EC2::VPC" for r in synth.templates[vpc]["Resources"].values()
    ), vpc


def test_app_py_reads_the_profile_variable_names_sh_names():
    variable = names()["NAMES_PROFILE_VARIABLE"][0]
    assert f'os.environ.get("{variable}"' in (CDK_DIR / "app.py").read_text()


def test_a_renamed_family_is_named(synth):
    """The check's own failure, on a test synth whose migration family is renamed."""
    renamed = Synth("\n".join(synth.printed), synth.assembly)
    for template in renamed.templates.values():
        for resource in template.get("Resources", {}).values():
            if resource["Type"] == "AWS::ECS::TaskDefinition" and resource[
                "Properties"
            ].get("Family", "").endswith("-migrate-staging"):
                resource["Properties"]["Family"] = "experimentation-migration-staging"
    renamed.text = json.dumps(renamed.templates)
    problems = missing(names(), renamed)
    assert (
        "NAMES_FAMILIES: the synth makes no 'experimentation-migrate-staging'"
        in problems
    ), problems
    assert (
        "NAMES_MIGRATE_FAMILY: the synth makes no 'experimentation-migrate-staging'"
        in problems
    ), problems


# --- A6: the target check on the real assembly ----------------------------------------------


def _target(
    listed: list[str], manifest: dict, folder: Path, account: str = FAKE_ACCOUNT
) -> subprocess.CompletedProcess:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps(manifest))
    listing = folder / "list.txt"
    listing.write_text("".join(f"{s}\n" for s in listed))
    script = (
        f'. "{LIB}/names.sh"; . "{LIB}/env.sh"; . "{LIB}/messages.sh"; '
        f'assert_cdk_target "{listing}" "{folder}" "{account}"'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def _manifest(assembly) -> dict:
    return json.loads((Path(assembly.directory) / "manifest.json").read_text())


def _vpc_stack(manifest: dict) -> str:
    """The VPC stack's artifact (an assembly also holds each stack's asset manifest)."""
    return next(
        k
        for k, v in manifest["artifacts"].items()
        if k.startswith("experimentation-vpc-")
        and v["type"] == "aws:cloudformation:stack"
    )


def test_the_target_check_passes_the_real_assembly(synth, tmp_path):
    done = _target(
        sorted(synth.templates), _manifest(synth.assembly), tmp_path / "real"
    )
    assert done.returncode == 0, done.stderr


def test_the_target_check_refuses_a_stack_in_another_region(synth, tmp_path):
    manifest = _manifest(synth.assembly)
    manifest["artifacts"][_vpc_stack(manifest)]["environment"] = (
        f"aws://{FAKE_ACCOUNT}/us-east-1"
    )
    done = _target(sorted(synth.templates), manifest, tmp_path / "region")
    assert (
        done.returncode == 1
        and "not every stack in the manifest is one of the nine" in done.stderr
    )


def test_the_target_check_refuses_a_tenth_stack(synth, tmp_path):
    manifest = _manifest(synth.assembly)
    manifest["artifacts"]["experimentation-extra-staging"] = dict(
        manifest["artifacts"][_vpc_stack(manifest)]
    )
    done = _target(
        sorted(synth.templates) + ["experimentation-extra-staging"],
        manifest,
        tmp_path / "tenth",
    )
    assert (
        done.returncode == 1
        and "cdk list is not exactly the nine staging stacks" in done.stderr
    )


def test_the_target_check_refuses_the_full_profile(tmp_path):
    full = _assembly("staging", "full")
    stacks = sorted(s.stack_name for s in full.stacks)
    assert len(stacks) > 9, stacks
    done = _target(stacks, _manifest(full), tmp_path / "full")
    assert (
        done.returncode == 1
        and "cdk list is not exactly the nine staging stacks" in done.stderr
    )


def test_the_target_check_refuses_another_account(synth, tmp_path):
    done = _target(
        sorted(synth.templates),
        _manifest(synth.assembly),
        tmp_path / "account",
        account="123456789012",
    )
    assert done.returncode == 1 and re.search(
        r"not every stack in the manifest is one of the nine in aws://<acct>/us-west-2",
        done.stderr,
    )
