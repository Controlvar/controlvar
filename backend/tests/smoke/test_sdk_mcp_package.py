"""The MCP server's package (``sdk/mcp``) is pinned the way its plan says.

* Its runtime dependencies are exactly two, each an exact version, and the
  committed lock resolves each to that version (Q26a). A range would let a
  consumer's install pick a release nobody tested.
* Its Dependabot entry waits at least 14 days before proposing a version
  update (Q26b), and is the only entry that watches the directory, so no
  other entry proposes the same bump without the wait.
* Every npm package the release job publishes has a test script, without
  ``--passWithNoTests`` (Q27): the release runs ``npm test --if-present``,
  which passes a package with no script at all.
* ``engines.node``'s floor is the Node the package is tested and built on in
  CI (V-E1), so the declared floor is one something has run.

Reads files only; no git, no network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = ROOT / "sdk" / "mcp"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
UNIT_TESTS = ROOT / ".github" / "workflows" / "sdk-unit-tests.yml"
RELEASE = ROOT / ".github" / "workflows" / "sdk-release.yml"

EXACT = re.compile(r"\d+\.\d+\.\d+")
COOLDOWN_DAYS = 14


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pin_problems(manifest: dict, lock: dict) -> list[str]:
    problems = []
    dependencies = manifest.get("dependencies") or {}
    if set(dependencies) != {"@modelcontextprotocol/sdk", "zod"}:
        problems.append(
            f"sdk/mcp's runtime dependencies are {sorted(dependencies)}; "
            "expected exactly @modelcontextprotocol/sdk and zod"
        )
    packages = lock.get("packages") or {}
    root = (packages.get("") or {}).get("dependencies") or {}
    for name, spec in sorted(dependencies.items()):
        if not EXACT.fullmatch(spec):
            problems.append(f"{name} is a range, not a pin: {spec!r}")
            continue
        if root.get(name) != spec:
            problems.append(
                f"package-lock.json's root names {name} {root.get(name)!r}, not {spec}"
            )
        resolved = (packages.get(f"node_modules/{name}") or {}).get("version")
        if resolved != spec:
            problems.append(
                f"package-lock.json resolves {name} to {resolved!r}, not {spec}"
            )
    return problems


def test_the_runtime_dependencies_are_exact_pins_the_lock_resolves() -> None:
    problems = pin_problems(
        _json(PACKAGE_DIR / "package.json"), _json(PACKAGE_DIR / "package-lock.json")
    )
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    "spec, expected",
    [
        ("^1.32.1", "@modelcontextprotocol/sdk is a range, not a pin"),
        ("~1.32.1", "@modelcontextprotocol/sdk is a range, not a pin"),
        (">=1.32.1", "@modelcontextprotocol/sdk is a range, not a pin"),
        ("1.32.2", "package-lock.json's root names @modelcontextprotocol/sdk"),
    ],
)
def test_a_range_or_a_pin_the_lock_disagrees_with_is_refused(
    spec: str, expected: str
) -> None:
    manifest = _json(PACKAGE_DIR / "package.json")
    lock = _json(PACKAGE_DIR / "package-lock.json")
    manifest["dependencies"]["@modelcontextprotocol/sdk"] = spec
    assert any(p.startswith(expected) for p in pin_problems(manifest, lock))


def cooldown_problems(config: dict) -> list[str]:
    entries = []
    for update in config.get("updates") or []:
        directories = list(update.get("directories") or [])
        if "directory" in update:
            directories.append(update["directory"])
        if any(d.rstrip("/") == "/sdk/mcp" for d in directories):
            entries.append(update)
    if len(entries) != 1:
        return [
            f"{len(entries)} Dependabot entries watch /sdk/mcp; expected exactly one"
        ]
    days = (entries[0].get("cooldown") or {}).get("default-days")
    problems = []
    if entries[0].get("package-ecosystem") != "npm":
        problems.append("the /sdk/mcp Dependabot entry is not npm")
    if not isinstance(days, int) or days < COOLDOWN_DAYS:
        problems.append(
            f"the /sdk/mcp Dependabot entry has no cooldown of at least {COOLDOWN_DAYS} days"
        )
    return problems


def test_dependabot_waits_two_weeks_before_proposing_an_mcp_update() -> None:
    problems = cooldown_problems(yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8")))
    assert not problems, "\n".join(problems)


def test_a_missing_or_short_cooldown_is_refused() -> None:
    config = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    entry = next(u for u in config["updates"] if u.get("directory") == "/sdk/mcp")
    entry["cooldown"] = {"default-days": 3}
    assert cooldown_problems(config) == [
        "the /sdk/mcp Dependabot entry has no cooldown of at least 14 days"
    ]
    del entry["cooldown"]
    assert cooldown_problems(config) == [
        "the /sdk/mcp Dependabot entry has no cooldown of at least 14 days"
    ]
    config["updates"][0].setdefault("directories", []).append("/sdk/mcp")
    assert cooldown_problems(config)[0].startswith(
        "2 Dependabot entries watch /sdk/mcp"
    )


def _npm_packages() -> list[str]:
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location(
        "check_sdk_version", ROOT / "scripts" / "check_sdk_version.py"
    )
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return sorted(name for name, eco in module.ECOSYSTEMS.items() if eco == "npm")


def test_the_npm_list_holds_the_mcp_server() -> None:
    assert "mcp" in _npm_packages()


@pytest.mark.parametrize("sdk", _npm_packages())
def test_every_npm_package_has_a_test_script(sdk: str) -> None:
    script = (
        (_json(ROOT / "sdk" / sdk / "package.json").get("scripts") or {}).get("test")
    ) or ""
    assert "jest" in script, (
        f"sdk/{sdk} has no test script: the release would publish untested"
    )
    assert "--passWithNoTests" not in script, (
        f"sdk/{sdk}'s test script passes with no tests: the release would publish untested"
    )


def _node_versions() -> dict[str, str]:
    unit = yaml.safe_load(UNIT_TESTS.read_text(encoding="utf-8"))
    release = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))

    def setup_node(steps: list[dict]) -> str:
        found = [
            str(step["with"]["node-version"])
            for step in steps
            if str(step.get("uses", "")).startswith("actions/setup-node@")
        ]
        assert len(found) == 1, found
        return found[0]

    return {
        "sdk-unit-tests.yml": setup_node(unit["jobs"]["linux"]["steps"]),
        "sdk-release.yml npm-build": setup_node(release["jobs"]["npm-build"]["steps"]),
    }


def floor_problems(engines: str, ci: dict[str, str]) -> list[str]:
    match = re.fullmatch(r">=\s*(\d+)", engines.strip())
    if match is None:
        return [f"sdk/mcp declares node {engines!r}, which is not a plain >= floor"]
    return [
        f"sdk/mcp declares node {engines} but CI tests it on {version} only ({where})"
        for where, version in ci.items()
        if version.split(".")[0] != match.group(1)
    ]


def test_the_declared_node_floor_is_the_node_ci_runs() -> None:
    engines = _json(PACKAGE_DIR / "package.json")["engines"]["node"]
    problems = floor_problems(engines, _node_versions())
    assert not problems, "\n".join(problems)


def test_a_floor_ci_does_not_run_is_refused() -> None:
    assert floor_problems(">=20", _node_versions())[0].startswith(
        "sdk/mcp declares node >=20 but CI tests it on 22 only"
    )
