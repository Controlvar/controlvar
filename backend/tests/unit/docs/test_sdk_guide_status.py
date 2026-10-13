"""The SDK guide's status table says what CI runs for each SDK (#1142).

``docs/sdk-guide.md`` has one row per SDK: its directory, the framework its
unit tests use, whether it is "verified live" and its page. It used to carry
hand-written test counts that had all gone stale. This reads the table and the
two sentences around it and holds each to the files CI reads:

* the directory exists and its tests use the framework the row names (the
  manifest or build file that declares it, or the command CI runs);
* "yes" under "Verified live" exactly when the SDK is in the ``sdks:`` list of
  the SDK Live Contract job in pr-qa-gate.yml; "including local evaluation"
  exactly for the SDKs with a local-evaluation smoke in the live runner; "not
  yet" for an SDK with a ``contract_smoke`` entry point in the runner that the
  job does not run, and whose unit tests are in the nightly tier;
* every SDK in the job's list has a row, and the sentence that lists the job's
  SDKs lists exactly them, in its order;
* each page a row links to exists.

Reads files only (YAML and ``ast``); no git, so it runs the same in
``scripts/core_build.sh``'s copy.
"""

from __future__ import annotations

import ast
import json
import re
import textwrap
from pathlib import Path
from typing import Callable, Dict, List

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "sdk-guide.md"
SDK = REPO_ROOT / "sdk"
GATE = REPO_ROOT / ".github" / "workflows" / "pr-qa-gate.yml"
UNIT_TESTS = REPO_ROOT / ".github" / "workflows" / "sdk-unit-tests.yml"
RUNNER = REPO_ROOT / "tests" / "sdk-contract" / "live" / "run_live_contract.py"

ROW = re.compile(
    r"^\| (?P<name>[^|]+?) \| `sdk/(?P<dir>[a-z-]+)` \| (?P<tests>[^|]+?) \|"
    r" (?P<live>[^|]+?) \| (?P<docs>[^|]+?) \|$",
    re.M,
)
LISTED = re.compile(
    r"repeats this on every pull request, with `--strict`, for (?P<list>[a-z, -]+?) \(the"
    r" `sdks:` list of the job"
)
NOT_YET = (
    'Rows marked "not yet" have a `contract_smoke` entry point that no CI job runs against'
    " a live backend; their unit tests run in the nightly tier and on pull requests that"
    " touch them."
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _files(directory: str, pattern: str) -> List[Path]:
    return [
        p for p in (SDK / directory).rglob(pattern) if "node_modules" not in p.parts
    ]


def _npm_dev(directory: str, package: str) -> bool:
    data = json.loads(_text(SDK / directory / "package.json"))
    return package in data.get("devDependencies", {})


def _unit_test_commands() -> str:
    return _text(UNIT_TESTS)


#: The framework a row names -> how to tell an SDK's tests use it.
FRAMEWORKS: Dict[str, Callable[[str], bool]] = {
    "jest": lambda d: _npm_dev(d, "jest"),
    "pytest": lambda d: f"python -m pytest sdk/{d}/tests" in _unit_test_commands(),
    "`go test`": lambda d: bool(_files(d, "*_test.go")),
    "JUnit 5": lambda d: any(
        "junit-jupiter" in _text(p)
        for p in _files(d, "pom.xml") + _files(d, "*.gradle.kts")
    ),
    "XCTest": lambda d: ".testTarget(" in _text(SDK / d / "Package.swift"),
    "RSpec": lambda d: (
        re.search(r'gem "rspec"', _text(SDK / d / "Gemfile")) is not None
    ),
    "PHPUnit": lambda d: (
        "phpunit/phpunit"
        in json.loads(_text(SDK / d / "composer.json")).get("require-dev", {})
    ),
    "xUnit": lambda d: any("xunit" in _text(p).lower() for p in _files(d, "*.csproj")),
    "`flutter test`": lambda d: (
        "flutter_test"
        in yaml.safe_load(_text(SDK / d / "pubspec.yaml"))["dev_dependencies"]
        and f"cd sdk/{d} && flutter pub get && flutter test" in _unit_test_commands()
    ),
    "ExUnit": lambda d: "ExUnit.start()" in _text(SDK / d / "test" / "test_helper.exs"),
}


def gate_sdks() -> List[str]:
    return yaml.safe_load(_text(GATE))["jobs"]["sdk-live-contract"]["with"][
        "sdks"
    ].split()


def runner_manifests() -> Dict[str, set]:
    found = {}
    for node in ast.parse(_text(RUNNER)).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in ("MANIFEST", "LOCAL_MANIFEST"):
                found[node.target.id] = {ast.literal_eval(k) for k in node.value.keys}
    assert set(found) == {"MANIFEST", "LOCAL_MANIFEST"}, found
    return found


def heavy_tier() -> List[str]:
    steps = {
        s.get("name"): s
        for s in yaml.safe_load(_text(UNIT_TESTS))["jobs"]["changes"]["steps"]
    }
    body = re.search(
        r"<<'PY'[^\n]*\n(?P<b>.*?)\n\s*PY\s*$", steps["Build the matrix"]["run"], re.S
    )
    for node in ast.parse(textwrap.dedent(body["b"])).body:
        if (
            isinstance(node, ast.Assign)
            and getattr(node.targets[0], "id", "") == "HEAVY"
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("sdk-unit-tests.yml builds no HEAVY list")


def problems(page: str) -> List[str]:
    found: List[str] = []
    rows = list(ROW.finditer(page))
    if len(rows) < 10:
        return [f"found {len(rows)} rows in the status table: the scan is broken"]
    gate, manifests, heavy = gate_sdks(), runner_manifests(), heavy_tier()
    for row in rows:
        name, directory, tests, live = (
            row["name"],
            row["dir"],
            row["tests"],
            row["live"],
        )
        where = f"row {name!r}"
        if not (SDK / directory).is_dir():
            found.append(f"{where}: sdk/{directory} does not exist")
            continue
        check = FRAMEWORKS.get(tests)
        if check is None:
            found.append(f"{where}: no check knows the framework {tests!r}")
        elif not check(directory):
            found.append(f"{where}: sdk/{directory}'s tests do not use {tests}")
        if live.startswith("yes"):
            if directory not in gate:
                found.append(
                    f"{where}: says verified live; {directory!r} is not in the job's sdks: list"
                )
            if ("including local evaluation" in live) != (
                directory in manifests["LOCAL_MANIFEST"]
            ):
                found.append(
                    f"{where}: 'including local evaluation' disagrees with the runner's LOCAL_MANIFEST"
                )
            if (
                "runs under jest in Node" in live
                and "jest"
                not in _text(RUNNER).split(f'"{directory}":')[1].split("),")[0]
            ):
                found.append(f"{where}: the runner does not run this smoke under jest")
            if (
                "Android library tests run in the nightly tier" in live
                and directory not in heavy
            ):
                found.append(f"{where}: {directory!r} is not in the nightly tier")
        elif live.startswith("not yet"):
            if directory in gate:
                found.append(
                    f"{where}: says not yet; {directory!r} is in the job's sdks: list"
                )
            if directory not in manifests["MANIFEST"]:
                found.append(
                    f"{where}: the live runner has no contract_smoke entry for {directory!r}"
                )
            if directory not in heavy:
                found.append(
                    f"{where}: says its unit tests run nightly; {directory!r} is not in the nightly tier"
                )
        else:
            found.append(
                f"{where}: 'Verified live' is neither yes nor not yet: {live!r}"
            )
        for link in re.findall(r"\]\((sdk/[a-z-]+\.md)\)", row["docs"]):
            if not (REPO_ROOT / "docs" / link).is_file():
                found.append(f"{where}: links to docs/{link}, which does not exist")
    in_table = {row["dir"] for row in rows}
    found += [
        f"{sdk!r} is in the job's sdks: list and has no row"
        for sdk in gate
        if sdk not in in_table
    ]
    listed = LISTED.search(" ".join(page.split()))
    if listed is None:
        found.append("the sentence listing the job's SDKs is gone")
    else:
        names = re.split(r", | and ", listed["list"])
        if names != gate:
            found.append(f"the sentence lists {names}; the job's sdks: list is {gate}")
    if NOT_YET not in " ".join(page.split()):
        found.append("the sentence about the 'not yet' rows is gone")
    return found


def test_the_status_table_agrees_with_ci():
    found = problems(_text(PAGE))
    assert not found, "\n".join(found)


@pytest.mark.parametrize(
    "old, new, expected",
    [
        (
            "| Go | `sdk/go` | `go test` | yes |",
            "| Go | `sdk/go` | `go test` | not yet |",
            "row 'Go': says not yet; 'go' is in the job's sdks: list",
        ),
        (
            "| `flutter test` |",
            "| `dart test` |",
            "row 'Flutter / Dart': no check knows the framework '`dart test`'",
        ),
        (
            "| Ruby | `sdk/ruby` | RSpec |",
            "| Ruby | `sdk/ruby` | Minitest |",
            "row 'Ruby': no check knows the framework 'Minitest'",
        ),
        (
            "| Python | `sdk/python` | pytest | yes, including local evaluation |",
            "| Python | `sdk/python` | pytest | yes |",
            "row 'Python': 'including local evaluation' disagrees with the runner's LOCAL_MANIFEST",
        ),
        ("ruby, php and dotnet", "ruby and php", "the sentence lists"),
    ],
)
def test_a_planted_defect_is_refused(old, new, expected):
    page = _text(PAGE)
    assert page.count(old) == 1, old
    found = problems(page.replace(old, new))
    assert any(problem.startswith(expected) for problem in found), found
