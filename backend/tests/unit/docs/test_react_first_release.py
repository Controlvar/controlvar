"""The React SDK's first release is 0.1.0, and nothing names the old number.

Before its first publish, ``sdk/react`` carried 1.1.0, a version never
released, and its pages and the demo READMEs followed it. Plan P (#1498)
moves it to 0.1.0. This holds:

* the manifest and the lock's own two version fields (R1a: edit this pin in
  the pull request that bumps the version after 0.1.0 is on npm);
* for every npm SDK, the lock's own version fields agree with package.json
  (V1), which neither ``npm ci`` nor any other test compares;
* the lines the widened version grep (EMP-9, the plan's C9) found for React,
  which named 1.1.

Reads files only: no git and no npm, so it runs the same in
``scripts/core_build.sh``'s copy.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
SDK = REPO_ROOT / "sdk"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def lock_versions(sdk: str) -> tuple:
    """The lock's own version fields: its root ``version`` and
    ``packages[""].version``."""
    lock = _json(SDK / sdk / "package-lock.json")
    return lock.get("version"), (lock.get("packages") or {}).get("", {}).get("version")


@pytest.mark.regression
def test_the_react_package_is_first_published_at_0_1_0():
    """R1a. The manifest said 1.1.0, a version never published; the first
    release is 0.1.0. Edit this pin in the pull request that bumps the
    version after 0.1.0 is on npm."""
    manifest = _json(SDK / "react" / "package.json")
    assert manifest["name"] == "@controlvar/react-sdk"
    assert manifest["version"] == "0.1.0"
    assert lock_versions("react") == ("0.1.0", "0.1.0")


def _npm_sdks() -> list:
    return sorted(p.parent.name for p in SDK.glob("*/package-lock.json"))


def test_the_npm_sdks_are_read():
    assert {"js", "react", "openfeature"} <= set(_npm_sdks())


@pytest.mark.parametrize("sdk", _npm_sdks())
def test_each_npm_lock_carries_its_manifests_version(sdk: str):
    """V1. A bump that moves package.json and leaves the lock's own fields
    behind is refused."""
    version = _json(SDK / sdk / "package.json")["version"]
    assert lock_versions(sdk) == (version, version), (
        f"sdk/{sdk}/package-lock.json says {lock_versions(sdk)}; "
        f"package.json says {version}"
    )


#: Every file in which the widened version grep of the first-publish plan
#: found a React line (11 at main ac8c0159, with the lock excluded): the
#: manifest, the README, the SDK page, the two demo READMEs and their claims
#: registries, and a test fixture.
NEVER_RELEASED_REACT_SITES = (
    "sdk/react/package.json",
    "sdk/react/README.md",
    "docs/sdk/react.md",
    "demo/shoplab/README.md",
    "demo/streampulse/README.md",
    "docs/_claims/demo/shoplab/README.md.toml",
    "docs/_claims/demo/streampulse/README.md.toml",
    "backend/tests/unit/scripts/test_pin_npm_local_deps.py",
)
#: The React alternatives of that grep, as it ran in bash. They are built from
#: OLD so that this file is not itself a hit for the grep.
OLD = ".".join(("1", "1", "0"))
NEVER_RELEASED_REACT = re.compile(
    r"react-sdk-"
    + re.escape(OLD[:3])
    + r"|React SDK (?:v|≥ ?|>= ?)"
    + re.escape(OLD[:2])
    + r"|`@controlvar/react-sdk` \(v"
    + re.escape(OLD[:1])
    + r'|"version": "'
    + re.escape(OLD)
    + '"'
)


@pytest.mark.parametrize(
    "line",
    [
        f"`controlvar-react-sdk-{OLD}.tgz`:",
        f"The app is written against React SDK v{OLD}'s surface",
        f"(React SDK ≥ {OLD} with the assigned/reason fields)",
        f"`@controlvar/react-sdk` (v{OLD[:3]}) provides a context provider",
        f'  "version": "{OLD}",',
    ],
)
def test_the_never_released_react_pattern_finds_each_old_site(line):
    assert NEVER_RELEASED_REACT.search(line), line


@pytest.mark.regression
@pytest.mark.parametrize("site", NEVER_RELEASED_REACT_SITES)
def test_no_react_site_names_the_never_released_version(site):
    found = [
        f"{site}:{number}: {line.strip()}"
        for number, line in enumerate(
            (REPO_ROOT / site).read_text(encoding="utf-8").splitlines(), start=1
        )
        if NEVER_RELEASED_REACT.search(line)
    ]
    assert not found, found
