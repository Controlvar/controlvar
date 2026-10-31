"""
The engine versions frozen on main, and the proof that a frozen version's pins
cannot change.

The rule: an engine-changing PR bumps ``ENGINE_VERSION`` to the next minor and
appends it to ``RELEASED_ENGINE_VERSIONS`` ("frozen on main") in the same
change.  A frozen version's entries are never edited, and no key is ever added
under one.  A PR is engine-changing when, for the same database rows and
request, it adds, removes or changes any number, decision (winner,
recommendation, Bayesian decision, ``can_stop``) or allocation weight that a
path stamping ``engine_version`` returns, caches or persists: results and its
snapshots, Bayesian, CUPED, sequential, and the bandit's state, history and
status.  A revert of an engine-changing PR bumps again.

Every table that pins an output per engine version is registered in
``PIN_TABLES``.  ``FROZEN_DIGESTS`` holds one digest per frozen version: the
sha256 of the canonical JSON (sorted keys, no whitespace) of that version's
entry in each registered table.  A table with no entry for a version
contributes nothing to that version's digest, so a new table whose first entry
is a new version changes no existing digest.  The dict only ever grows by
appending a line; so does ``FROZEN_VERSIONS``, the literal of the frozen
versions in order.  The engine-changing PR appends to all three: the tuple
``RELEASED_ENGINE_VERSIONS``, ``FROZEN_DIGESTS`` and ``FROZEN_VERSIONS``.
Replacing a hash or adding a key under a frozen version
changes that version's digest and fails here; the failing line names the
version.

A source scan reads every module under ``backend/tests/unit`` (files only, not
git: core-build has no ``.git``) and fails on a version-keyed table that is not
registered, so a new table cannot escape the freeze.

To freeze a new version: append it to ``RELEASED_ENGINE_VERSIONS`` in
``test_sufficient_stats_fingerprint.py``, bump ``ENGINE_VERSION``, add the
version's entries to every pin table, append its digest to ``FROZEN_DIGESTS``
(run this module; the failure prints the value) and its version to
``FROZEN_VERSIONS``.
"""

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from backend.app.core.stats_engine import ENGINE_VERSION
from backend.tests.unit.services import (
    test_binomial_metric_result_characterisation as characterisation,
)
from backend.tests.unit.services import test_engine_fingerprint as engine_fingerprint
from backend.tests.unit.services import (
    test_lower_is_better_fingerprint as lower_is_better,
)
from backend.tests.unit.services import test_results_correction_pins as correction_pins
from backend.tests.unit.services import test_revenue_per_user_routing as revenue
from backend.tests.unit.services import test_sufficient_stats_fingerprint as sufficient

pytestmark = [pytest.mark.unit, pytest.mark.regression]

_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_UNIT_DIR = Path(__file__).resolve().parents[1]
_THIS = "services/test_engine_versions_frozen.py"

RELEASED_ENGINE_VERSIONS = sufficient.RELEASED_ENGINE_VERSIONS

#: Every table that pins an output per engine version: (path under
#: ``backend/tests/unit``, name) -> the table.
PIN_TABLES: Dict[Tuple[str, str], Dict[str, Any]] = {
    (
        "services/test_sufficient_stats_fingerprint.py",
        "SUFFICIENT_STATS_FINGERPRINTS",
    ): sufficient.SUFFICIENT_STATS_FINGERPRINTS,
    (
        "services/test_revenue_per_user_routing.py",
        "REVENUE_RESULTS_FINGERPRINTS",
    ): revenue.REVENUE_RESULTS_FINGERPRINTS,
    (
        "services/test_engine_fingerprint.py",
        "ENGINE_FINGERPRINTS",
    ): engine_fingerprint.ENGINE_FINGERPRINTS,
    (
        "services/test_lower_is_better_fingerprint.py",
        "LOWER_IS_BETTER_FINGERPRINTS",
    ): lower_is_better.LOWER_IS_BETTER_FINGERPRINTS,
    (
        "services/test_results_correction_pins.py",
        "ONE_TREATMENT_PINS",
    ): correction_pins.ONE_TREATMENT_PINS,
    (
        "services/test_binomial_metric_result_characterisation.py",
        "CHARACTERISATIONS",
    ): characterisation.CHARACTERISATIONS,
}

#: One digest per frozen version.  Append a line; never edit one.
FROZEN_DIGESTS: Dict[str, str] = {
    "1.1.0": "5a4a6ab77102d4c5fbb2a859460c841fb1db7189dafd8469cba5fcd327d72a10",
    "1.2.0": "5deca8a606e4774105fb6fe87aef0f04534c08f3707386933e2ba6ce33992477",
    "1.3.0": "85047995d86198ceec4a21eddd772c6df22b17b911017310ffc1dcf3f5b70eac",
    "1.4.0": "863715ed587943b3b45b8940c8037461e316c9154400d70d3ff817943ae69537",
    "1.5.0": "428844d519af491070788ffdead8493147ae1731cc0bc11d0ef07ba92d6da64f",
    # 1.6.0 (#1385) changes a lower-is-better bandit, which no pin table
    # runs: its entries are 1.5.0's, so its digest is too.
    "1.6.0": "428844d519af491070788ffdead8493147ae1731cc0bc11d0ef07ba92d6da64f",
}


#: The frozen versions, oldest first, as a literal that only ever grows by
#: appending.  The engine-changing PR appends its version to all three of
#: ``RELEASED_ENGINE_VERSIONS``, ``FROZEN_DIGESTS`` and this tuple.  It is what
#: stops a frozen middle version being dropped from the first two together.
FROZEN_VERSIONS: Tuple[str, ...] = (
    "1.1.0",
    "1.2.0",
    "1.3.0",
    "1.4.0",
    "1.5.0",
    "1.6.0",
)


def _key(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def version_digest(version: str, tables=None) -> str:
    """sha256 of the canonical JSON of ``version``'s entry in each table."""
    tables = PIN_TABLES if tables is None else tables
    entries = {
        f"{path}::{name}": table[version]
        for (path, name), table in tables.items()
        if version in table
    }
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_engine_versions_that_shipped_are_recorded():
    """1.5.0 shipped in v0.38.0 and v0.39.0 (#1383, #1400) while the tuple
    still ended at 1.4.0, so its hashes could be replaced with no test failing.
    Every frozen version has a digest, and every digest has a frozen version."""
    assert set(FROZEN_DIGESTS) == set(RELEASED_ENGINE_VERSIONS)


def test_a_frozen_version_is_never_dropped():
    """``FROZEN_VERSIONS`` is an append-only literal.  The tuple must start with
    it, and it must be the digest keys in order, so dropping a frozen version
    (the first as much as a middle one) from the tuple and its digest line
    together fails here."""
    assert RELEASED_ENGINE_VERSIONS[: len(FROZEN_VERSIONS)] == FROZEN_VERSIONS, (
        f"RELEASED_ENGINE_VERSIONS {RELEASED_ENGINE_VERSIONS} no longer starts "
        f"with the frozen versions {FROZEN_VERSIONS}: a frozen version is "
        "never removed or reordered"
    )
    assert tuple(FROZEN_DIGESTS) == FROZEN_VERSIONS


def test_the_running_engine_is_the_last_frozen_version():
    """A bump without the append, or an append without the bump, fails here."""
    assert ENGINE_VERSION == RELEASED_ENGINE_VERSIONS[-1], (
        f"ENGINE_VERSION is {ENGINE_VERSION} but the last version frozen on "
        f"main is {RELEASED_ENGINE_VERSIONS[-1]}: an engine-changing PR bumps "
        "ENGINE_VERSION and appends it to RELEASED_ENGINE_VERSIONS together"
    )


def test_frozen_versions_strictly_increase():
    keys = [_key(v) for v in RELEASED_ENGINE_VERSIONS]
    assert keys == sorted(set(keys)), RELEASED_ENGINE_VERSIONS


@pytest.mark.parametrize("version", sorted(FROZEN_DIGESTS, key=_key))
def test_a_frozen_versions_pins_are_unchanged(version):
    actual = version_digest(version)
    assert actual == FROZEN_DIGESTS[version], (
        f"the pinned output of frozen engine version {version} changed "
        f"(digest {actual}); bump ENGINE_VERSION instead"
    )


def _version_keyed_tables() -> List[Tuple[str, str]]:
    """Top-level dict assignments under backend/tests/unit with a version key."""
    found = []
    for path in sorted(_UNIT_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            else:
                continue
            value = node.value
            if not isinstance(value, ast.Dict):
                continue
            if any(
                isinstance(k, ast.Constant)
                and isinstance(k.value, str)
                and _VERSION.match(k.value)
                for k in value.keys
            ):
                rel = path.relative_to(_UNIT_DIR).as_posix()
                found.extend((rel, name) for name in names)
    return found


def test_every_version_keyed_table_is_registered():
    """A table keyed by engine version that is not in ``PIN_TABLES`` would sit
    outside the freeze.  This module's own ``FROZEN_DIGESTS`` is the one other
    version-keyed table.

    This is a heuristic.  It sees only top-level assignments, under
    ``backend/tests/unit``, of a dict literal with a string-literal key shaped
    ``N.N.N``.  It misses a non-literal key (``{V: ...}``), a nested dict, a
    ``dict([...])`` call, a later ``X["1.5.0"] = ...``, a class attribute, a
    key such as ``"v1.5.0"`` or ``"1.5"``, and any file outside
    ``backend/tests/unit``.  It also does not check that a ``PIN_TABLES`` entry
    is the object its (path, name) names."""
    found = set(_version_keyed_tables())
    registered = set(PIN_TABLES) | {(_THIS, "FROZEN_DIGESTS")}
    assert found == registered, (
        f"version-keyed tables not registered in PIN_TABLES: "
        f"{sorted(found - registered)}; registered but not found: "
        f"{sorted(registered - found)}"
    )
