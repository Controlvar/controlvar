"""What ``infrastructure/cdk/app.py`` needs before it synthesises anything.

Every ``cdk`` command an operator runs -- ``synth``, ``diff``, ``deploy``,
``destroy`` -- runs ``app.py`` first, so a refusal here is a refusal of all of
them. Pinned here, each on the app itself (``runpy`` of ``app.py``, as the CLI
runs it):

* the app is Python, run by ``python3 app.py``, on the CDK v2 library, and CI
  synthesises it on Python 3.11;
* ``ENVIRONMENT`` defaults to ``dev`` and takes exactly ``dev``, ``staging``,
  ``prod`` or ``demo``;
* a missing ``CERTIFICATE_ARN`` or ``PUBLIC_BASE_URL`` stops the app, naming
  the variable, in every environment, and the HTTPS listeners carry the
  certificate given;
* the stacks deploy through the default bootstrap (qualifier ``hnb659fds``):
  its deploy, CloudFormation-execution, lookup and file-publishing roles, its
  assets bucket and its version parameter. The context in ``cdk.json`` is
  applied, as the CLI applies it, so a qualifier set there is seen.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import runpy

import aws_cdk as cdk
import pytest
import yaml

from .test_app_profiles import (
    CDK_DIR,
    FAKE_ACCOUNT,
    FAKE_REGION,
    REPO_ROOT,
    SECRET_ARN_INPUTS,
    _app_environment,
    fake_secret_arn,
)
from .test_dashboard_service import _synth

ENVIRONMENTS = ("dev", "staging", "prod", "demo")
QUALIFIER = "hnb659fds"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "infrastructure-tests.yml"


def _run_app(**overrides):
    """``app.py`` run as the CLI runs it, without the final synthesis."""
    synth = cdk.App.synth
    cdk.App.synth = lambda self, **kwargs: None  # type: ignore[method-assign]
    try:
        with _app_environment(CDK_DIR, **overrides):
            return runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")
    finally:
        cdk.App.synth = synth  # type: ignore[method-assign]


# --- the app ------------------------------------------------------------------


def test_the_cdk_app_is_python_run_by_python3_app_py_on_cdk_v2():
    cdk_json = json.loads((CDK_DIR / "cdk.json").read_text())
    assert cdk_json["app"] == "python3 app.py"
    tree = ast.parse((CDK_DIR / "app.py").read_text())
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    # CDK v2 is the single `aws_cdk` package (v1 was `aws_cdk.core` and one
    # package per service).
    assert "aws_cdk" in imported and "aws_cdk.core" not in imported, imported
    pins = re.findall(
        r"^aws-cdk-lib==([0-9]+)\.", (CDK_DIR / "requirements.txt").read_text(), re.M
    )
    assert pins == ["2"], pins


def test_ci_synthesises_the_app_on_python_3_11():
    """The CDK Stack Tests job runs the app (every synth below) on 3.11, and
    nothing in the app is syntax a 3.11 interpreter refuses."""
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["cdk-python-tests"]
    versions = [
        str(step["with"]["python-version"])
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/setup-python")
    ]
    assert versions == ["3.11"], versions
    runs = " ".join(str(step.get("run", "")) for step in job["steps"])
    assert "python -m pytest infrastructure/tests" in runs, runs
    sources = sorted(CDK_DIR.rglob("*.py"))
    assert CDK_DIR / "app.py" in sources and len(sources) > 10, sources
    for path in sources:
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 11))


@pytest.mark.regression
def test_environment_defaults_to_dev_and_takes_four_names():
    # Unset: the dev secrets are the ones a dev synth must be given.
    dev_arns = {name: fake_secret_arn(name, "dev") for name in SECRET_ARN_INPUTS}
    assert _run_app(ENVIRONMENT=None, **dev_arns)["env_name"] == "dev"
    for name in ENVIRONMENTS:
        assert _run_app(ENVIRONMENT=name)["env_name"] == name
    for name in ("qa", "production", "Prod", ""):
        with pytest.raises(ValueError) as refused:
            _run_app(ENVIRONMENT=name)
        assert "Must be one of: dev, staging, prod, demo" in str(refused.value)


# --- required inputs ----------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
@pytest.mark.parametrize("variable", ["CERTIFICATE_ARN", "PUBLIC_BASE_URL"])
def test_a_missing_certificate_or_public_url_stops_the_app(environment, variable):
    for value in (None, ""):
        with pytest.raises(ValueError) as refused:
            _run_app(ENVIRONMENT=environment, **{variable: value})
        assert f"set the {variable} environment variable" in str(refused.value)


@pytest.mark.regression
def test_the_https_listeners_carry_the_certificate_given():
    certificate = (
        f"arn:aws:acm:{FAKE_REGION}:{FAKE_ACCOUNT}:certificate/"
        "12345678-1234-1234-1234-123456789012"
    )
    with _app_environment(CDK_DIR, ENVIRONMENT="staging", CERTIFICATE_ARN=certificate):
        assembly = runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")[
            "app"
        ].synth()
    (fargate,) = [s for s in assembly.stacks if "-fargate-" in s.stack_name]
    listeners = [
        r["Properties"]
        for r in fargate.template["Resources"].values()
        if r["Type"] == "AWS::ElasticLoadBalancingV2::Listener"
    ]
    https = [p for p in listeners if p["Protocol"] == "HTTPS"]
    assert len(https) == 2, listeners
    for listener in https:
        assert listener["Certificates"] == [{"CertificateArn": certificate}]
    # The third listener is the port-80 redirect, with no certificate.
    (plain,) = [p for p in listeners if p["Protocol"] == "HTTP"]
    assert "Certificates" not in plain


# --- the bootstrap ------------------------------------------------------------


@pytest.mark.regression
def test_every_stack_deploys_through_the_default_bootstrap():
    """The names `cdk bootstrap` gives its roles, bucket and parameter, with
    the default qualifier: the app overrides none of them, here or in
    cdk.json's context."""
    context = json.loads((CDK_DIR / "cdk.json").read_text()).get("context", {})
    assembly = _synth("staging", context=context)

    def role(kind: str) -> str:
        return (
            f"arn:${{AWS::Partition}}:iam::{FAKE_ACCOUNT}:role/"
            f"cdk-{QUALIFIER}-{kind}-role-{FAKE_ACCOUNT}-{FAKE_REGION}"
        )

    stacks = list(assembly.stacks)
    assert len(stacks) >= 10, [s.stack_name for s in stacks]
    for stack in stacks:
        assert stack.assume_role_arn == role("deploy"), stack.stack_name
        assert stack.cloud_formation_execution_role_arn == role("cfn-exec")
        assert stack.lookup_role.arn == role("lookup")
        assert stack.bootstrap_stack_version_ssm_parameter == (
            f"/cdk-bootstrap/{QUALIFIER}/version"
        )
    destinations = [
        destination
        for manifest in pathlib.Path(assembly.directory).glob("*.assets.json")
        for asset in json.loads(manifest.read_text()).get("files", {}).values()
        for destination in asset["destinations"].values()
    ]
    assert destinations, "the scan found no file asset"
    for destination in destinations:
        assert destination["bucketName"] == (
            f"cdk-{QUALIFIER}-assets-{FAKE_ACCOUNT}-{FAKE_REGION}"
        )
        assert destination["assumeRoleArn"] == role("file-publishing")
