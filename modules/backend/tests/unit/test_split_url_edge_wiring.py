"""The split-URL edge router splits nothing in this release (#393).

``docs/api/split-url.md`` warns that the Lambda@Edge router reads its
configuration only from an ``X-Split-URL-Config`` request header, and that
nothing sets that header: no stack creates the CloudFront distribution or adds
the header, and the platform never sends an experiment's ``split_url_config``
to the edge. These hold that:

* the router, run on viewer-request events, answers a redirect only for a
  configuration in that header; the same configuration anywhere else in the
  request (another header, a cookie, the query string, the body) is passed
  through untouched;
* no infrastructure code outside the construct's own file refers to the
  construct, its module or a CloudFront distribution, in any form (an import,
  a name, an attribute, a resource type string);
* no code outside the router, in any of the tree's languages, names the header,
  in any case, so no code path can send it.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

ROOT = Path(__file__).resolve().parents[4]
ROUTER = ROOT / "modules" / "lambda" / "split_url_router" / "handler.py"
CONSTRUCT = (
    ROOT / "modules" / "infrastructure" / "constructs" / "split_url_distribution.py"
)
INFRASTRUCTURE = (ROOT / "infrastructure", ROOT / "modules" / "infrastructure")
HEADER = "x-split-url-config"

CONFIG = {
    "experiment_key": "edge_wiring",
    "variants": [
        {"name": "control", "url": "https://example.com/a", "traffic_allocation": 50},
        {"name": "treatment", "url": "https://example.com/b", "traffic_allocation": 50},
    ],
}


def _router():
    spec = importlib.util.spec_from_file_location("split_url_edge_router", ROUTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.handler


def _event(headers=None, querystring="", body=None):
    request = {
        "uri": "/landing",
        "querystring": querystring,
        "headers": {
            "x-forwarded-for": [{"key": "X-Forwarded-For", "value": "10.1.2.3"}],
            "user-agent": [{"key": "User-Agent", "value": "Edge/1.0"}],
            **(headers or {}),
        },
    }
    if body is not None:
        request["body"] = {"data": body, "encoding": "text"}
    return {"Records": [{"cf": {"request": request}}]}


def _header(name, value):
    return {name.lower(): [{"key": name, "value": value}]}


def test_the_router_redirects_only_on_the_header():
    handler = _router()
    config = json.dumps(CONFIG)
    redirected = handler(_event(_header("X-Split-URL-Config", config)), None)
    assert redirected["status"] == "302"

    elsewhere = [
        _event(_header("X-Split-Url-Configuration", config)),
        _event(_header("X-Experiment-Config", config)),
        _event(_header("Cookie", f"x-split-url-config={config}")),
        _event(querystring=f"x-split-url-config={config}"),
        _event(body=config),
    ]
    for event in elsewhere:
        request = event["Records"][0]["cf"]["request"]
        answer = handler(event, None)
        assert answer is request, answer
        assert "status" not in answer


def _python_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        parts = set(path.relative_to(ROOT).parts)
        if "tests" in parts or "node_modules" in parts or "cdk.out" in parts:
            continue
        yield path


#: Every way a file can refer to the construct or to a CloudFront distribution:
#: the construct and its module, and each distribution class aws_cloudfront has.
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


def test_no_stack_creates_the_distribution():
    checked = 0
    found = {}
    for root in INFRASTRUCTURE:
        for path in _python_files(root):
            checked += 1
            if path == CONSTRUCT:
                continue
            refs = sorted(set(_references(path)))
            if refs:
                found[str(path.relative_to(ROOT))] = refs
    assert checked > 20, "the scan read no infrastructure code"
    # The scan sees the construct itself: it is the file that does create one.
    assert set(_references(CONSTRUCT)) & {"Distribution"}
    assert found == {}


#: The source files of every language in the tree: the API and the scripts
#: (Python, shell), the dashboards (TypeScript, JavaScript), each SDK under sdk/
#: (Go, Java, Kotlin, Swift, C#, PHP, Ruby, Elixir, Dart), and configuration.
_SOURCE_SUFFIXES = {
    ".py",
    ".sh",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".go",
    ".java",
    ".kt",
    ".kts",
    ".swift",
    ".cs",
    ".php",
    ".rb",
    ".ex",
    ".exs",
    ".dart",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".tpl",
    ".xml",
}
#: Everywhere code that could send the header lives: the API and the modules,
#: the Lambdas, the infrastructure, the dashboards, the SDKs, the demo apps,
#: the deployment files, the workflows and the scripts.
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


def test_nothing_but_the_router_names_the_header():
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
            if path == ROUTER:
                continue
            if HEADER in path.read_text(encoding="utf-8", errors="replace").lower():
                found.append(str(relative))
    assert checked > 500, "the scan read too few files"
    assert HEADER in ROUTER.read_text(encoding="utf-8").lower()
    assert found == []
