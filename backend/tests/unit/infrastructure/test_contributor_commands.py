"""Commands a contributor is told to run do what they say (#1287).

* #1287: the development stack's documented teardown (the compose file's header
  comment and ``make down``) left the ``demo``, ``tools`` and ``aws`` profile
  services running, because a plain ``docker compose down`` only acts on the
  services of the profiles that are enabled -- none, by default.

Read as text, with no Docker and no npm, so it runs in the unit job.
"""

from __future__ import annotations

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
