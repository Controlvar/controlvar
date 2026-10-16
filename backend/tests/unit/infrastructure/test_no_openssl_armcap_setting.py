"""No compose service, image or chart sets OPENSSL_armcap (#1265).

``OPENSSL_armcap=0`` turns off OpenSSL's assembly code paths. On the arm64
hardware where cryptography's OpenSSL crashes, ``backend/docker-entrypoint.sh``
detects it and the venv's start-up hook applies the setting to every Python
process; everywhere else it is a large slowdown for nothing (AES-GCM and
SHA-256 measured many times slower on Apple Silicon). An EMPTY value is read by
OpenSSL as 0 too, while the entrypoint treats empty as unset, so a pass-through
like ``OPENSSL_armcap: ${OPENSSL_armcap:-}`` would quietly turn the paths off
on every arm64 host. Setting it is for an operator, by hand; nothing shipped
here does.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Iterator, List, Tuple

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
VARIABLE = "OPENSSL_armcap"
_COMPOSE_NAME = re.compile(r"(^|/)(docker-)?compose[^/]*\.ya?ml$")


def compose_files() -> List[Path]:
    """Every compose file in the tree, found by name, not listed by hand."""
    try:
        listed = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        # core_build.sh's copy has no .git: walk the tree instead.
        listed = [
            str(path.relative_to(REPO_ROOT))
            for path in REPO_ROOT.rglob("*.y*ml")
            if "node_modules" not in path.parts
        ]
    found = []
    for name in sorted(listed):
        if name.startswith(".github/") or not _COMPOSE_NAME.search(name):
            continue
        data = yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("services"), dict):
            found.append(REPO_ROOT / name)
    return found


def environment_names(service: Any) -> Iterator[str]:
    environment = (service or {}).get("environment") or {}
    if isinstance(environment, dict):
        yield from environment
    else:
        for item in environment:
            yield str(item).split("=", 1)[0]


def service_settings() -> Iterator[Tuple[str, str, str]]:
    for path in compose_files():
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, service in data["services"].items():
            for variable in environment_names(service):
                yield str(path.relative_to(REPO_ROOT)), name, variable


def dockerfile_instructions(path: Path) -> Iterator[str]:
    """Each instruction on one line: comments dropped, continuations joined."""
    current = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        current += stripped
        if current.strip():
            yield current.strip()
        current = ""
    if current.strip():
        yield current.strip()


def test_the_compose_files_are_found():
    names = {str(path.relative_to(REPO_ROOT)) for path in compose_files()}
    assert {"docker-compose.yml", "deploy/compose/compose.yml"} <= names, names


@pytest.mark.regression
def test_no_compose_service_sets_openssl_armcap():
    offenders = [
        f"{path}: {service}"
        for path, service, variable in service_settings()
        if variable == VARIABLE
    ]
    assert offenders == [], (
        f"{VARIABLE} is set by {offenders}; the entrypoint decides it per CPU"
    )


@pytest.mark.regression
def test_no_image_or_chart_sets_openssl_armcap():
    dockerfiles = sorted(REPO_ROOT.glob("**/Dockerfile"))
    assert REPO_ROOT / "backend" / "Dockerfile" in dockerfiles
    for dockerfile in dockerfiles:
        if "node_modules" in dockerfile.parts:
            continue
        for instruction in dockerfile_instructions(dockerfile):
            assert not (
                instruction.upper().startswith(("ENV ", "ARG "))
                and VARIABLE in instruction
            ), f"{dockerfile.relative_to(REPO_ROOT)}: {instruction}"
    chart = REPO_ROOT / "charts" / "experimently"
    for path in sorted(chart.rglob("*")):
        if path.is_file() and path.suffix in (".yaml", ".yml", ".tpl", ".json"):
            assert VARIABLE not in path.read_text(encoding="utf-8"), path
