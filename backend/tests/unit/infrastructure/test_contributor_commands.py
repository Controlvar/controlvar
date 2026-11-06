"""Commands a contributor is told to run do what they say (#1287, #288).

* #1287: the development stack's documented teardown (the compose file's header
  comment and ``make down``) left the ``demo``, ``tools`` and ``aws`` profile
  services running, because a plain ``docker compose down`` only acts on the
  services of the profiles that are enabled -- none, by default.
* #288: ``sdk/react-native`` had an ``npm run lint`` script that invokes eslint,
  which the package did not install, so the command failed for everyone.

Read as text and JSON, with no Docker and no npm, so it runs in the unit job.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Set

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPOSE = REPO_ROOT / "docker-compose.yml"
MAKEFILE = REPO_ROOT / "Makefile"


def _declared_profiles() -> Set[str]:
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    return {p for svc in services.values() for p in svc.get("profiles", [])}


def _down_commands(text: str) -> list:
    """Every `docker compose ... down ...` command in `text`, flags included."""
    return re.findall(r"docker compose[^\n#]*?\bdown\b[^\n#]*", text)


def _profiles_named(command: str) -> Set[str]:
    return set(re.findall(r"--profile[ =]([\w*-]+)", command))


def test_the_compose_file_declares_the_profiles_this_test_expects():
    # If a profile is added, the teardown below must learn it.
    assert _declared_profiles() == {"demo", "tools", "aws"}


def test_make_down_names_every_profile():
    recipe = re.search(r"^down:.*\n((?:\t.*\n)+)", MAKEFILE.read_text(), re.M)
    assert recipe, "Makefile has no down target"
    text = recipe.group(1).replace("$(COMPOSE)", "docker compose")
    commands = _down_commands(text)
    assert commands, "make down runs no `docker compose down`"
    for command in commands:
        named = _profiles_named(command)
        assert "*" in named or named >= _declared_profiles(), command


def test_the_compose_header_teardown_names_every_profile():
    header = []
    for line in COMPOSE.read_text().splitlines():
        if not line.startswith("#"):
            break
        header.append(line.lstrip("# ").rstrip())
    commands = _down_commands("\n".join(header))
    assert commands, "the compose header documents no teardown"
    for command in commands:
        named = _profiles_named(command)
        assert "*" in named or named >= _declared_profiles(), command


def _package_jsons():
    found = []
    for top in ("sdk", "frontend", "demo"):
        for path in sorted((REPO_ROOT / top).rglob("package.json")):
            if "node_modules" not in path.parts:
                found.append(path)
    return found


def test_every_script_that_runs_eslint_declares_eslint():
    # #288 (react-native) and the same defect in sdk/openfeature: a `lint`
    # script that runs eslint with no eslint dependency fails for everyone.
    paths = _package_jsons()
    assert REPO_ROOT / "sdk" / "react-native" / "package.json" in paths
    assert REPO_ROOT / "sdk" / "openfeature" / "package.json" in paths
    running = 0
    offenders = []
    for path in paths:
        pkg = json.loads(path.read_text())
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        for name, command in pkg.get("scripts", {}).items():
            if re.search(r"\beslint\b", command):
                running += 1
                if "eslint" not in deps:
                    offenders.append(f"{path.parent.name}: `npm run {name}`")
    assert not offenders, f"these run eslint without declaring it: {offenders}"
    # positive control: frontend and the demos really do run eslint, declared
    assert running >= 3
