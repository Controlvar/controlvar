"""The split URL edge app and its construct (#393).

The edge app (``modules/infrastructure/split_url_edge``) is a CDK app of its
own: one stack in us-east-1 with one CloudFront distribution whose only
behaviour runs the router at viewer request and viewer response. These hold,
on the cloud assembly ``python app.py`` writes (``conftest.py``):

* exactly one distribution; one function version, associated exactly twice,
  ``viewer-request`` and ``viewer-response``; python3.12 on x86_64; no
  environment variables; a code asset of exactly ``handler.py`` (the router,
  byte for byte) and ``config.json`` (the settings); nothing cached;
* synth is refused, naming the setting, for each wrong setting, and for a
  stack outside us-east-1 or with no region;
* the API key is in the router's settings file and nowhere else;
* nothing but the edge app builds the construct, and no workflow, Makefile
  target, installer step or script names the edge app;
* nothing names the router's old configuration header.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from modules.backend.tests.unit.split_url_router.conftest import (
    API_KEY,
    EDGE_APP,
    REQUIRED,
    ROOT,
    SETTINGS,
    synth,
)

pytestmark = [pytest.mark.unit]

ROUTER = ROOT / "modules" / "lambda" / "split_url_router" / "handler.py"
CONSTRUCT = (
    ROOT / "modules" / "infrastructure" / "constructs" / "split_url_distribution.py"
)


def _resources(template: dict, kind: str) -> dict:
    return {
        name: resource
        for name, resource in template["Resources"].items()
        if resource["Type"] == kind
    }


@pytest.mark.regression
def test_the_edge_app_runs_the_router_at_both_viewer_events(edge_synth):
    """V19(b): one distribution, one function version associated twice, on
    python3.12 and x86_64, with no environment variables, and nothing
    cached."""
    template = edge_synth.template()
    (distribution,) = _resources(template, "AWS::CloudFront::Distribution").values()
    (function,) = _resources(template, "AWS::Lambda::Function").values()
    (version_name,) = _resources(template, "AWS::Lambda::Version")
    (role,) = _resources(template, "AWS::IAM::Role").values()
    (policy,) = _resources(template, "AWS::CloudFront::CachePolicy").values()
    config = distribution["Properties"]["DistributionConfig"]
    behaviour = config["DefaultCacheBehavior"]
    assert "CacheBehaviors" not in config
    assert behaviour["LambdaFunctionAssociations"] == [
        {
            "EventType": "viewer-request",
            "IncludeBody": False,
            "LambdaFunctionARN": {"Ref": version_name},
        },
        {"EventType": "viewer-response", "LambdaFunctionARN": {"Ref": version_name}},
    ]
    properties = function["Properties"]
    assert properties["Handler"] == "handler.handler"
    assert properties["Runtime"] == "python3.12"
    assert properties["Architectures"] == ["x86_64"]
    assert "Environment" not in properties
    principals = {
        statement["Principal"]["Service"]
        for statement in role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    }
    assert principals == {"lambda.amazonaws.com", "edgelambda.amazonaws.com"}
    ttls = policy["Properties"]["CachePolicyConfig"]
    assert (ttls["DefaultTTL"], ttls["MinTTL"], ttls["MaxTTL"]) == (0, 0, 0)
    assert behaviour["CachePolicyId"] == {
        "Ref": next(iter(_resources(template, "AWS::CloudFront::CachePolicy")))
    }
    assert behaviour["ViewerProtocolPolicy"] == "redirect-to-https"
    assert [o["DomainName"] for o in config["Origins"]] == ["origin.example.com"]
    assert config["Aliases"] == ["shop.example.com", "www.example.com"]
    assert (
        config["ViewerCertificate"]["AcmCertificateArn"]
        == SETTINGS["SPLIT_URL_CERTIFICATE_ARN"]
    )


def test_the_edge_app_is_one_stack_in_us_east_1(edge_synth):
    stacks = {
        name: artifact["environment"]
        for name, artifact in edge_synth.manifest()["artifacts"].items()
        if artifact["type"] == "aws:cloudformation:stack"
    }
    assert stacks == {"controlvar-split-url-edge": "aws://unknown-account/us-east-1"}


@pytest.mark.regression
def test_the_router_is_bundled_with_its_settings(edge_synth, edge_synth_defaults):
    """V19(b): the code asset is the router byte for byte and config.json with
    the settings, defaults where none was given; the temporary copy it was
    made from is gone once synth has finished."""
    asset = edge_synth.function_asset()
    assert sorted(p.name for p in asset.iterdir()) == ["config.json", "handler.py"]
    assert (asset / "handler.py").read_bytes() == ROUTER.read_bytes()
    assert json.loads((asset / "config.json").read_text(encoding="utf-8")) == {
        "api_url": "https://api.example.com",
        "api_key": API_KEY,
        "cookie_name": "site_uid",
        "cookie_max_age_days": 7,
        "cookie_domain": "example.com",
    }
    defaults = edge_synth_defaults.function_asset() / "config.json"
    assert json.loads(defaults.read_text(encoding="utf-8")) == {
        "api_url": "https://api.example.com",
        "api_key": API_KEY,
        "cookie_name": "controlvar_uid",
        "cookie_max_age_days": 30,
        "cookie_domain": None,
    }
    for result in (edge_synth, edge_synth_defaults):
        assert list(result.tmpdir.iterdir()) == []


_KEY_SHAPE = re.compile(r"eptk_[0-9a-f]{32}")


@pytest.mark.regression
def test_the_api_key_is_only_in_the_routers_settings_file(edge_synth):
    """The cloud assembly holds the key once: in the router's config.json.
    Not in a template, a manifest or the router's code, and no other string
    shaped like a key is anywhere in it."""
    found = {}
    for path in sorted(edge_synth.outdir.rglob("*")):
        if path.is_file():
            hits = _KEY_SHAPE.findall(
                path.read_text(encoding="utf-8", errors="replace")
            )
            if hits:
                found[str(path.relative_to(edge_synth.outdir))] = hits
    asset = edge_synth.function_asset().relative_to(edge_synth.outdir)
    assert found == {str(asset / "config.json"): [API_KEY]}


#: Each wrong setting, and the setting the refusal must name.
WRONG = {
    "no-api-url": ({"SPLIT_URL_API_URL": None}, "SPLIT_URL_API_URL"),
    "http-api-url": (
        {"SPLIT_URL_API_URL": "http://api.example.com"},
        "SPLIT_URL_API_URL",
    ),
    "api-url-with-a-path": (
        {"SPLIT_URL_API_URL": "https://api.example.com/v1"},
        "SPLIT_URL_API_URL",
    ),
    "api-url-with-a-query": (
        {"SPLIT_URL_API_URL": "https://api.example.com/?a=1"},
        "SPLIT_URL_API_URL",
    ),
    "api-url-with-a-user": (
        {"SPLIT_URL_API_URL": "https://user@api.example.com"},
        "SPLIT_URL_API_URL",
    ),
    "no-api-key": ({"SPLIT_URL_API_KEY": None}, "SPLIT_URL_API_KEY"),
    "api-key-with-a-space": ({"SPLIT_URL_API_KEY": "a key"}, "SPLIT_URL_API_KEY"),
    "no-origin": ({"SPLIT_URL_ORIGIN": None}, "SPLIT_URL_ORIGIN"),
    "origin-with-a-scheme": (
        {"SPLIT_URL_ORIGIN": "https://origin.example.com"},
        "SPLIT_URL_ORIGIN",
    ),
    "origin-with-a-port": (
        {"SPLIT_URL_ORIGIN": "origin.example.com:8443"},
        "SPLIT_URL_ORIGIN",
    ),
    "origin-an-address": ({"SPLIT_URL_ORIGIN": "10.0.0.1"}, "SPLIT_URL_ORIGIN"),
    "cookie-name": ({"SPLIT_URL_COOKIE_NAME": "a;b"}, "SPLIT_URL_COOKIE_NAME"),
    "max-age-0": (
        {"SPLIT_URL_COOKIE_MAX_AGE_DAYS": "0"},
        "SPLIT_URL_COOKIE_MAX_AGE_DAYS",
    ),
    "max-age-401": (
        {"SPLIT_URL_COOKIE_MAX_AGE_DAYS": "401"},
        "SPLIT_URL_COOKIE_MAX_AGE_DAYS",
    ),
    "max-age-not-a-number": (
        {"SPLIT_URL_COOKIE_MAX_AGE_DAYS": "30d"},
        "SPLIT_URL_COOKIE_MAX_AGE_DAYS",
    ),
    "cookie-domain": (
        {"SPLIT_URL_COOKIE_DOMAIN": "example.com/x"},
        "SPLIT_URL_COOKIE_DOMAIN",
    ),
    "domain-names-without-a-certificate": (
        {"SPLIT_URL_CERTIFICATE_ARN": None},
        "SPLIT_URL_DOMAIN_NAMES",
    ),
    "certificate-without-domain-names": (
        {"SPLIT_URL_DOMAIN_NAMES": None},
        "SPLIT_URL_CERTIFICATE_ARN",
    ),
    "certificate-in-another-region": (
        {
            "SPLIT_URL_CERTIFICATE_ARN": (
                "arn:aws:acm:eu-west-1:123456789012:certificate/1234abcd"
            )
        },
        "SPLIT_URL_CERTIFICATE_ARN",
    ),
    "domain-name": (
        {"SPLIT_URL_DOMAIN_NAMES": "shop.example.com,not a host"},
        "SPLIT_URL_DOMAIN_NAMES",
    ),
}


@pytest.mark.parametrize("kind", sorted(WRONG))
def test_synth_is_refused_for_each_wrong_setting(kind, tmp_path):
    """V19(c): refused at synth, naming the setting, with nothing written."""
    changes, setting = WRONG[kind]
    result = synth(tmp_path, {**SETTINGS, **changes})
    assert result.returncode != 0, kind
    assert result.stderr.startswith("split URL edge app: "), result.stderr
    assert setting in result.stderr, result.stderr
    assert not list(result.outdir.glob("*.template.json"))


REGION_APP = """\
import importlib.util
import sys

import aws_cdk as cdk

spec = importlib.util.spec_from_file_location("construct", sys.argv[1])
construct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(construct)
region = sys.argv[2]
app = cdk.App()
env = None if region == "none" else cdk.Environment(region=region)
stack = cdk.Stack(app, "Edge", env=env)
try:
    construct.SplitUrlDistribution(
        stack,
        "SplitUrl",
        origin_domain="origin.example.com",
        api_url="https://api.example.com",
        api_key="key-1",
    )
except ValueError as error:
    sys.exit(f"refused: {error}")
app.synth()
"""


@pytest.mark.parametrize(
    "region, refused",
    [("eu-west-1", True), ("none", True), ("us-east-1", False)],
    ids=["eu-west-1", "no-region", "us-east-1"],
)
def test_the_construct_is_refused_outside_us_east_1(region, refused, tmp_path):
    """V19(c): CDK itself synthesises a Lambda@Edge function in any region;
    the construct refuses every stack but one whose region is literally
    us-east-1."""
    app = tmp_path / "app" / "region_app.py"
    app.parent.mkdir()
    app.write_text(REGION_APP, encoding="utf-8")
    result = _run_region_app(app, region, tmp_path)
    if refused:
        assert result.returncode != 0
        assert "refused: The split URL edge stack must be in us-east-1" in result.stderr
    else:
        assert result.returncode == 0, result.stderr


def _run_region_app(app: Path, region: str, tmp_path: Path):
    outdir = tmp_path / "cdk.out"
    outdir.mkdir()
    return subprocess.run(
        [sys.executable, app.name, str(CONSTRUCT), region],
        cwd=app.parent,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", str(tmp_path)),
            "CDK_OUTDIR": str(outdir),
            "TMPDIR": str(tmp_path),
            "PYTHONDONTWRITEBYTECODE": "1",
            "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_the_required_settings_are_the_three_documented():
    assert REQUIRED == ("SPLIT_URL_API_URL", "SPLIT_URL_API_KEY", "SPLIT_URL_ORIGIN")


# -- who builds and deploys it ----------------------------------------------


def _python_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        parts = set(path.relative_to(ROOT).parts)
        if "tests" in parts or "node_modules" in parts or "cdk.out" in parts:
            continue
        yield path


#: Every way a file can refer to the construct or to a CloudFront distribution.
_NAMES = {
    "SplitUrlDistribution",
    "split_url_distribution",
    "Distribution",
    "CfnDistribution",
    "CloudFrontWebDistribution",
    "CfnStreamingDistribution",
}
_TYPE = re.compile(r"AWS::CloudFront::(?:Distribution|StreamingDistribution)")


def _references(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _NAMES:
            yield node.id
        elif isinstance(node, ast.Attribute) and node.attr in _NAMES:
            yield node.attr
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            for name in names:
                if any(part in _NAMES for part in name.split(".")):
                    yield name
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _TYPE.search(node.value) or "split_url_distribution" in node.value:
                yield node.value


@pytest.mark.regression
def test_the_constructs_only_referrer_is_the_edge_app():
    """V19(d), and the pin it replaces (nothing referred to the construct):
    of all the infrastructure code, only the construct's own file creates a
    distribution, and only the edge app refers to the construct."""
    checked = 0
    found = {}
    for root in (ROOT / "infrastructure", ROOT / "modules" / "infrastructure"):
        for path in _python_files(root):
            checked += 1
            if path == CONSTRUCT:
                continue
            refs = sorted(set(_references(path)))
            if refs:
                found[str(path.relative_to(ROOT))] = refs
    assert checked > 20, "the scan read no infrastructure code"
    assert "Distribution" in set(_references(CONSTRUCT))
    assert list(found) == ["modules/infrastructure/split_url_edge/app.py"], found


#: Where a deploy would be wired: the workflows and actions, every Makefile,
#: the installer and the other deployment files, the scripts, and the
#: platform's CDK app.
DEPLOY_ROOTS = (".github", "deploy", "scripts", "charts", "infrastructure")
_EDGE_APP_NAMES = re.compile(r"split[_-]url[_-]edge", re.IGNORECASE)


@pytest.mark.regression
def test_nothing_deploys_the_edge_app():
    """V19(d): no workflow, Makefile target, installer step or script names
    the edge app, so only an operator's own cdk deploy creates it."""
    paths = [p for p in sorted(ROOT.glob("Makefile*")) if p.is_file()]
    for root in DEPLOY_ROOTS:
        paths += [p for p in sorted((ROOT / root).rglob("*")) if p.is_file()]
    paths = [
        p for p in paths if "node_modules" not in p.parts and "cdk.out" not in p.parts
    ]
    found = [
        str(path.relative_to(ROOT))
        for path in paths
        if _EDGE_APP_NAMES.search(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert len(paths) > 200, "the scan read too few files"
    assert any(p.name == "Makefile" for p in paths)
    assert any(".github/workflows" in str(p) for p in paths)
    assert found == []
    assert _EDGE_APP_NAMES.search(str(EDGE_APP.relative_to(ROOT)))


#: The source files of every language in the tree.
_SOURCE_SUFFIXES = {
    ".py", ".sh", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".java",
    ".kt", ".kts", ".swift", ".cs", ".php", ".rb", ".ex", ".exs", ".dart",
    ".json", ".yml", ".yaml", ".toml", ".tpl", ".xml",
}  # fmt: skip
CODE_ROOTS = (
    "backend/app",
    "backend/lambda",
    "modules",
    "infrastructure",
    "frontend/src",
    "sdk",
    "demo",
    "deploy",
    "charts",
    ".github",
    "scripts",
)
_SKIP_DIRS = {"node_modules", "tests", "__tests__", "__pycache__", "cdk.out", ".next"}
#: The router's old configuration header, which nothing reads or sends now.
OLD_HEADER = "x-split-url-" + "config"


@pytest.mark.regression
def test_nothing_names_the_old_configuration_header():
    """V18, and the pin it replaces (only the router named the header): no
    code in any of the tree's languages names it, the router included."""
    checked = 0
    found = []
    for root in CODE_ROOTS:
        for path in sorted((ROOT / root).rglob("*")):
            if path.suffix not in _SOURCE_SUFFIXES or not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if _SKIP_DIRS & set(relative.parts):
                continue
            if path.name.endswith((".test.ts", ".test.tsx", ".spec.ts")):
                continue
            checked += 1
            if OLD_HEADER in path.read_text(encoding="utf-8", errors="replace").lower():
                found.append(str(relative))
    assert checked > 500, "the scan read too few files"
    assert found == []
