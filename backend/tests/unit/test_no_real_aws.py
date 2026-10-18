"""No test can reach real AWS credentials or the real ``aws`` binary.

The gate is backend/tests/no_real_aws.py, loaded by the repository-root
conftest.py, plus a copy in each SDK's conftest. This file proves:

* in this session, ``aws sts get-caller-identity`` runs the blocking fake and
  boto3's STS client signs with the dummy key -- neither can return an account;
* a test that puts its own fake ``aws`` first on PATH still wins;
* every other conftest root CI runs actually loads the fixture;
* the SDK copies blank exactly the same variables as the plugin.

The boto3 call is stopped at ``before-send``: it never leaves the process,
so even with the gate broken on a machine that holds credentials, this test
sends nothing to AWS.
"""

from __future__ import annotations

import ast
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import boto3
import pytest

from backend.tests import no_real_aws

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_aws_cli_runs_the_blocking_fake(_no_real_aws: Path) -> None:
    fake = _no_real_aws / "aws"
    # Checked before the call, so a broken gate fails here instead of
    # running the real CLI.
    assert shutil.which("aws") == str(fake)

    result = subprocess.run(
        ["aws", "sts", "get-caller-identity"], capture_output=True, text=True
    )

    assert result.returncode == no_real_aws.FAKE_EXIT
    assert no_real_aws.MARKER in result.stderr
    assert "Account" not in result.stdout
    assert "sts get-caller-identity" in (_no_real_aws / "calls.log").read_text()


class _Blocked(Exception):
    """Raised at before-send: the request never leaves the process."""


def test_boto3_sts_signs_with_the_dummy_and_returns_no_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # boto3.client() reuses boto3.DEFAULT_SESSION, whose credentials are cached
    # from whenever an earlier test first created it. Start from no session so
    # this checks the environment the gate sets, not what ran before (#136).
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
    sent: list[str] = []

    def block(request, **kwargs):
        sent.append(request.headers.get("Authorization", b"").decode())
        raise _Blocked

    client = boto3.client("sts", region_name="us-east-1")
    client.meta.events.register("before-send.sts.GetCallerIdentity", block)

    with pytest.raises(_Blocked):
        client.get_caller_identity()

    assert len(sent) == 1
    # A bool, not the header: a failure must not print a real access key id.
    signed_with_dummy = f"Credential={no_real_aws.DUMMY_CREDENTIAL}/" in sent[0]
    assert signed_with_dummy, "STS request was signed with a key that is not the dummy"


def test_the_credential_chain_holds_only_dummies() -> None:
    session = boto3.Session()
    credentials = session.get_credentials()
    # Bools, not values: a failure must not print a real access key id.
    method, is_dummy = (
        credentials.method,
        credentials.access_key == no_real_aws.DUMMY_CREDENTIAL,
    )
    has_token = credentials.token is not None
    assert (method, is_dummy, has_token) == ("env", True, False)
    assert session.available_profiles == []  # ~/.aws/config is not read
    for name in no_real_aws.UNSET:
        assert name not in os.environ, name
    for name in no_real_aws.MISSING_FILES:
        assert not Path(os.environ[name]).exists(), name
    assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"


def test_a_test_that_prepends_its_own_fake_aws_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = tmp_path / "aws"
    mine.write_text("#!/bin/sh\necho mine\n")
    mine.chmod(mine.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    result = subprocess.run(["aws", "--version"], capture_output=True, text=True)

    assert (result.returncode, result.stdout) == (0, "mine\n")


# Every other conftest root CI runs, with the arguments its workflow passes.
# backend/tests is this session. modules/ is absent from a core build.
_ROOTS = [
    ("modules/backend/tests", []),
    ("backend/lambda/event_processor", []),
    ("backend/lambda/feature_flag_evaluation", []),
    ("backend/lambda/shared", []),
    ("infrastructure/tests", ["-o", "addopts=", "--import-mode=importlib"]),
    ("tests/sdk-contract", ["-o", "addopts="]),
    ("sdk/python/tests", ["-o", "addopts="]),
    ("sdk/openfeature-python/tests", ["-o", "addopts="]),
    # The docs-journey runner: its own pytest root (tests/acceptance/docs/
    # pytest.ini), loading backend/tests/no_real_aws.py from its conftest.
    ("tests/acceptance/docs", []),
    # The showcase tool's browser checks: their own pytest root, the same way.
    # Run by hand (no CI job runs them); loading their conftest, as here,
    # needs no browser.
    ("tests/acceptance/showcase_browser", []),
]


@pytest.mark.parametrize(
    ("root", "args"),
    # Only modules/ may be absent; a renamed root must fail, not drop out.
    [r for r in _ROOTS if (REPO_ROOT / "modules").is_dir() or r[0][:8] != "modules/"],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_every_ci_conftest_root_loads_the_fixture(root: str, args: list[str]) -> None:
    # --fixtures loads the conftests without importing a test module.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            root,
            "-p",
            "no:cacheprovider",
            "--fixtures",
            "-v",
            "--ignore-glob=*/test_*.py",
            *args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "_no_real_aws" in result.stdout, result.stdout


_SDK_COPIES = [
    "sdk/python/tests/conftest.py",
    "sdk/openfeature-python/tests/conftest.py",
]


def _literals(path: Path) -> dict[str, object]:
    found: dict[str, object] = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    found[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    return found


@pytest.mark.parametrize("copy", _SDK_COPIES)
def test_the_sdk_copies_blank_the_same_variables(copy: str) -> None:
    found = _literals(REPO_ROOT / copy)
    assert found["DUMMY"] == no_real_aws.DUMMY
    assert found["MISSING_FILES"] == no_real_aws.MISSING_FILES
    assert found["UNSET"] == no_real_aws.UNSET
    assert found["NO_REAL_AWS_MARKER"] == no_real_aws.MARKER
    assert found["NO_REAL_AWS_FAKE_EXIT"] == no_real_aws.FAKE_EXIT
