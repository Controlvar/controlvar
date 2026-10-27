"""Gate H6's wiring: scripts/check_installer_cli.py in CDK Stack Tests.

The real AWS CLI is blocked in every pytest session (backend/tests/no_real_aws.py),
so the comparison of the fake ``aws`` with the runner's CLI is a step outside pytest
in infrastructure-tests.yml's CDK Stack Tests job, in check_create_deployment_cli.py's
pattern: no credential files, instance metadata off, the stub on 127.0.0.1 the only
endpoint, dummy keys only in the CLI's own children, and a refusal to start when a
key is set. Pinned here, with the script's refusals and its helpers.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "infrastructure-tests.yml"
SCRIPT = REPO_ROOT / "scripts" / "check_installer_cli.py"


def _script():
    spec = importlib.util.spec_from_file_location("check_installer_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _steps() -> list[dict]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert (
        "paths" not in workflow[True]["pull_request"]
    )  # it runs on every pull request
    job = workflow["jobs"]["cdk-python-tests"]
    assert job["permissions"] == {"contents": "read"}  # nothing can assume a role
    return job["steps"]


def test_the_check_runs_in_the_cdk_job_with_no_credential():
    steps = _steps()
    (step,) = [s for s in steps if "scripts/check_installer_cli.py" in s.get("run", "")]
    assert step["env"] == {
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    assert "--list" not in step["run"]
    # After the CDK tests, whatever their result: one failure must not hide the other.
    assert step["if"] == "${{ !cancelled() }}"
    names = [s.get("name") for s in steps]
    assert names.index(step["name"]) > names.index("Run CDK stack tests")


def test_the_check_refuses_to_start_with_a_key(monkeypatch):
    cli = _script()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "no-real-aws-dummy")
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(cli, "build_cases", lambda *a: ran.append("cases"))
    assert cli.main([]) == 1
    assert ran == []


def test_the_check_refuses_without_a_cli(monkeypatch):
    cli = _script()
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        cli, "build_cases", lambda *a: pytest.fail("no case may be built")
    )
    assert cli.main([]) == 1


def test_the_clis_children_get_dummy_keys_and_no_files():
    env = _script().cli_env(Path("/nowhere"), "UTC")
    assert (
        env["AWS_ACCESS_KEY_ID"] == env["AWS_SECRET_ACCESS_KEY"] == "h6-no-credential"
    )
    assert env["AWS_CONFIG_FILE"] == env["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert not {"AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL"} & set(env)


def test_a_shape_is_a_call_without_its_region():
    cli = _script()
    west = [
        "ec2",
        "describe-vpcs",
        "--region",
        "us-west-2",
        "--query",
        "Vpcs",
        "--output",
        "json",
    ]
    east = [
        "ec2",
        "describe-vpcs",
        "--region",
        "us-east-1",
        "--query",
        "Vpcs",
        "--output",
        "json",
    ]
    assert cli.shape_of(west) == cli.shape_of(east)
    assert cli.with_output(west, "text")[-2:] == ["--output", "text"]


@pytest.mark.parametrize(
    "headers, body, modelled, protocol",
    [
        (
            {"smithy-protocol": "rpc-v2-cbor", "Content-Type": "application/cbor"},
            b"",
            "query",
            "smithy-rpc-v2-cbor",
        ),
        (
            {
                "Content-Type": "application/x-amz-json-1.0",
                "X-Amz-Target": "X.DescribeAlarms",
            },
            b"{}",
            "query",
            "json",
        ),
        (
            {"Content-Type": "application/x-www-form-urlencoded"},
            b"Action=DescribeAlarms",
            "query",
            "query",
        ),
        (
            {"Content-Type": "application/x-www-form-urlencoded"},
            b"Action=DescribeVpcs",
            "ec2",
            "ec2",
        ),
        ({}, b"", "rest-xml", "rest-xml"),
    ],
)
def test_the_stub_answers_in_the_protocol_the_request_used(
    headers, body, modelled, protocol
):
    assert _script().detect(headers, body, modelled) == protocol
