"""Only the JavaScript and Python SDKs can answer a flag without asking the server.

Local evaluation starts from the ruleset, ``GET /api/v1/sdk/ruleset``: an SDK
whose source never requests it can only send each evaluation to the server.
This reads every SDK's source files (not its tests, examples or build output)
and requires that the ruleset route appears in exactly ``sdk/js`` and
``sdk/python``. Reads files only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[3]
SDK = REPO_ROOT / "sdk"
SOURCE = {
    ".ts",
    ".tsx",
    ".js",
    ".mjs",
    ".py",
    ".go",
    ".java",
    ".kt",
    ".swift",
    ".rb",
    ".php",
    ".cs",
    ".ex",
    ".exs",
    ".dart",
}
#: Directories that hold tests, examples, dependencies or build output.
NOT_SOURCE = {
    "__tests__",
    "__mocks__",
    "tests",
    "test",
    "spec",
    "Tests",
    "examples",
    "example",
    "Examples",
    "node_modules",
    "dist",
    "build",
    "target",
    "bin",
    "obj",
    "vendor",
    "deps",
    "_build",
}
ROUTE = "/api/v1/sdk/ruleset"


def sources_naming_the_ruleset() -> Dict[str, List[str]]:
    found: Dict[str, List[str]] = {}
    for path in sorted(SDK.rglob("*")):
        rel = path.relative_to(SDK)
        if (
            not path.is_file()
            or path.suffix not in SOURCE
            or NOT_SOURCE & set(rel.parts[1:-1])
        ):
            continue
        if path.name.endswith(
            ("_test.go", ".test.ts", ".test.tsx", ".spec.ts")
        ) or path.name.startswith("test_"):
            continue
        if ROUTE in path.read_text(encoding="utf-8", errors="replace"):
            found.setdefault(rel.parts[0], []).append(rel.as_posix())
    return found


def test_only_the_javascript_and_python_sdks_request_the_ruleset():
    found = sources_naming_the_ruleset()
    assert sorted(found) == ["js", "python"], found


def test_the_scan_reads_every_sdk():
    scanned = {
        p.relative_to(SDK).parts[0]
        for p in SDK.rglob("*")
        if p.is_file() and p.suffix in SOURCE
    }
    assert {
        "go",
        "java",
        "ruby",
        "php",
        "dotnet",
        "elixir",
        "flutter",
        "ios",
        "android",
        "react",
        "edge",
    } <= scanned
