"""An SDK that depends on a sibling SDK admits the sibling's version (#668).

``sdk/openfeature-python`` required ``controlvar>=1.0.0`` while
``sdk/python`` -- the package that publishes ``controlvar`` -- was versioned
0.1.0. Every test passed, because each package's tests install the sibling from
its local path; the first place the mismatch shows is a user's ``pip install``
after both are published, and by then the version is burned.

So, for every top-level ``sdk/<name>/`` manifest (``package.json`` or
``pyproject.toml``), every dependency on a package another ``sdk/`` directory
publishes must admit that directory's current manifest version:

* Python: the PEP 508 specifier, checked with ``packaging``.
* npm: a semver range, checked by the small evaluator below. The ``semver``
  package pinned for ``core/rules_engine.py`` understands plain comparators
  only (``>=0.1.0``), not npm's caret, tilde and range grammar: its
  ``match('^0.1.0')`` raises ``ValueError``. A ``file:`` spec must point at
  the sibling's own directory.

The npm evaluator refuses any range shape it does not understand rather than
passing it: an unknown shape fails the test until the evaluator is taught it.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[3]
SDK_DIR = ROOT / "sdk"

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

NPM_SECTIONS = (
    "dependencies",
    "peerDependencies",
    "optionalDependencies",
    "devDependencies",
)


# --------------------------------------------------------------------------
# Manifests
# --------------------------------------------------------------------------


def _manifests() -> dict[str, dict]:
    """``sdk directory -> {ecosystem, name, version, deps}`` for every SDK
    with an npm or Python manifest. ``deps`` is a list of ``(name, spec)``."""
    found: dict[str, dict] = {}
    for directory in sorted(p for p in SDK_DIR.iterdir() if p.is_dir()):
        package_json = directory / "package.json"
        pyproject = directory / "pyproject.toml"
        if package_json.is_file():
            data = json.loads(package_json.read_text(encoding="utf-8"))
            deps = [
                (name, spec)
                for section in NPM_SECTIONS
                for name, spec in (data.get(section) or {}).items()
            ]
            found[directory.name] = {
                "ecosystem": "npm",
                "name": data["name"],
                "version": data["version"],
                "deps": deps,
            }
        elif pyproject.is_file():
            project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
            raw = list(project.get("dependencies") or [])
            for extra in (project.get("optional-dependencies") or {}).values():
                raw.extend(extra)
            deps = []
            for line in raw:
                requirement = Requirement(line)
                deps.append((canonicalize_name(requirement.name), requirement))
            found[directory.name] = {
                "ecosystem": "pypi",
                "name": canonicalize_name(project["name"]),
                "version": project["version"],
                "deps": deps,
            }
    return found


MANIFESTS = _manifests()


def _sibling_edges() -> list[tuple[str, str, object]]:
    """``(dependent sdk, sibling sdk, spec)`` for every sibling dependency."""
    edges = []
    for sdk, manifest in MANIFESTS.items():
        publishers = {
            other["name"]: other_sdk
            for other_sdk, other in MANIFESTS.items()
            if other["ecosystem"] == manifest["ecosystem"] and other_sdk != sdk
        }
        for name, spec in manifest["deps"]:
            if name in publishers:
                edges.append((sdk, publishers[name], spec))
    return edges


EDGES = _sibling_edges()


# --------------------------------------------------------------------------
# A deliberately small npm semver range evaluator
# --------------------------------------------------------------------------

_VERSION = re.compile(r"^v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?$")
_COMPARATOR = re.compile(r"^(\^|~|>=|<=|>|<|=)?\s*(\S+)$")


def _triple(text: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        raise ValueError(f"not a plain X.Y.Z version: {text!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _bounds(operator: str, text: str) -> list[tuple[str, tuple[int, int, int]]]:
    """One npm comparator as a list of ``(op, X.Y.Z)`` bounds, all of which
    must hold. Raises ``ValueError`` for anything outside the supported set."""
    match = _VERSION.fullmatch(text)
    if not match:
        raise ValueError(f"unsupported version in range: {text!r}")
    parts = [None if p is None or p in "xX*" else int(p) for p in match.groups()]
    major, minor, patch = parts
    if major is None:
        if operator not in ("", "="):
            raise ValueError(f"unsupported range: {operator}{text}")
        return []
    full = minor is not None and patch is not None
    low = (major, minor or 0, patch or 0)

    if operator in ("", "=") and full:
        return [("==", low)]
    if operator == "^" or (operator in ("", "=") and not full):
        if operator == "^":
            if major > 0 or minor is None:
                high = (major + 1, 0, 0)
            elif minor > 0 or patch is None:
                high = (0, minor + 1, 0)
            else:
                high = (0, 0, patch + 1)
        else:  # x-range such as 1.x or 1.2
            high = (major + 1, 0, 0) if minor is None else (major, minor + 1, 0)
        return [(">=", low), ("<", high)]
    if operator == "~":
        high = (major + 1, 0, 0) if minor is None else (major, minor + 1, 0)
        return [(">=", low), ("<", high)]
    if operator in (">=", ">", "<", "<=") and full:
        return [(operator, low)]
    raise ValueError(f"unsupported range: {operator}{text}")


def npm_range_admits(spec: str, version: str) -> bool:
    """True when the npm range ``spec`` admits the plain ``version``."""
    target = _triple(version)
    checks = {
        "==": lambda b: target == b,
        ">=": lambda b: target >= b,
        ">": lambda b: target > b,
        "<": lambda b: target < b,
        "<=": lambda b: target <= b,
    }
    for alternative in spec.split("||"):
        tokens = re.findall(r"(?:\^|~|>=|<=|>|<|=)?\s*[^\s<>=^~]+", alternative.strip())
        if not tokens:
            raise ValueError(f"empty range alternative in {spec!r}")
        satisfied = True
        for token in tokens:
            match = _COMPARATOR.fullmatch(token.strip())
            if not match:
                raise ValueError(f"unsupported range token {token!r} in {spec!r}")
            for op, bound in _bounds(match.group(1) or "", match.group(2)):
                if not checks[op](bound):
                    satisfied = False
        if satisfied:
            return True
    return False


@pytest.mark.parametrize(
    ("spec", "version", "expected"),
    [
        ("^0.1.0", "0.1.0", True),
        ("^0.1.0", "0.1.9", True),
        ("^0.1.0", "0.2.0", False),
        ("^1.0.0", "0.1.0", False),
        ("^1.0.0", "1.4.2", True),
        ("~0.1.2", "0.1.5", True),
        ("~0.1.2", "0.2.0", False),
        (">=1.0.0", "0.1.0", False),
        (">=0.1.0 <0.2.0", "0.1.3", True),
        (">=0.1.0 <0.2.0", "0.2.0", False),
        ("0.1.x", "0.1.7", True),
        ("1.0.0 || ^0.1.0", "0.1.0", True),
        ("*", "0.1.0", True),
        ("0.1.0", "0.1.0", True),
        ("0.1.0", "0.1.1", False),
    ],
)
def test_the_npm_range_evaluator(spec: str, version: str, expected: bool) -> None:
    assert npm_range_admits(spec, version) is expected


def test_the_npm_range_evaluator_refuses_what_it_does_not_understand() -> None:
    with pytest.raises(ValueError):
        npm_range_admits("1.0.0 - 2.0.0", "1.5.0")
    with pytest.raises(ValueError):
        npm_range_admits("^1.0.0-beta.1", "1.0.0")


# --------------------------------------------------------------------------
# The property
# --------------------------------------------------------------------------


def test_the_sibling_edges_are_found() -> None:
    """Guards against a vacuous pass: the two known sibling dependencies are
    discovered, so a broken discovery cannot report "no edges, all fine"."""
    pairs = {(sdk, sibling) for sdk, sibling, _ in EDGES}
    assert ("openfeature-python", "python") in pairs, EDGES
    assert ("openfeature", "js") in pairs, EDGES


@pytest.mark.parametrize(
    ("sdk", "sibling", "spec"),
    EDGES,
    ids=[f"{sdk}->{sibling}" for sdk, sibling, _ in EDGES],
)
def test_sibling_range_admits_the_sibling_version(
    sdk: str, sibling: str, spec: object
) -> None:
    dependent = MANIFESTS[sdk]
    target = MANIFESTS[sibling]
    version = target["version"]
    where = f"sdk/{sdk} depends on {target['name']} (sdk/{sibling}, version {version})"

    if dependent["ecosystem"] == "pypi":
        assert isinstance(spec, Requirement)
        assert spec.url is None, f"{where} by URL ({spec.url}); declare a version range"
        assert spec.specifier.contains(Version(version), prereleases=True), (
            f"{where} with {str(spec.specifier)!r}, which does not admit {version}. "
            "Once both are published the two cannot be installed together: "
            "align the range with the sibling's manifest version."
        )
        return

    assert isinstance(spec, str)
    if spec.startswith("file:"):
        path = (SDK_DIR / sdk / spec.removeprefix("file:")).resolve()
        assert path == (SDK_DIR / sibling).resolve(), (
            f"{where} through {spec!r}, which is not that sibling's directory"
        )
        return
    assert npm_range_admits(spec, version), (
        f"{where} with {spec!r}, which does not admit {version}. Align the "
        "range with the sibling's manifest version."
    )
