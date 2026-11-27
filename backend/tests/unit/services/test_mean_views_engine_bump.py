"""
No results view's answer on a mean metric moves without an engine bump.

Six views read a metric beside ``/results`` itself: sequential, Bayesian,
sample size, daily, live and the breakdown.  Each reads the share of users
who sent a mean metric's event today, and each is to read the metric's values
per user instead, one change at a time.  Each such change moves that view's
numbers, so it bumps ``ENGINE_VERSION``.

This test runs each view on ``mean_metrics_harness.SHARE_IS_NOT_THE_MEAN`` (a
revenue primary whose two arms have the same share of buyers and different
means), hashes its answer, and requires the hash to be one of two:

* ``PRE_MEAN_DIGESTS[view]``: the view's answer before any view read a mean
  metric's values, recorded by running this module on main's code before
  ``mean_arms.py`` existed;
* ``MEAN_VIEW_PINS[ENGINE_VERSION][view]``: the answer pinned for the running
  engine version.

So a change that moves a view's answer fails here until it bumps the engine
and pins the new answer under the new version.  ``MEAN_VIEW_PINS`` is empty
until the first view moves; the change that adds its first entry registers it
in ``test_engine_versions_frozen.PIN_TABLES``, which from then on refuses an
edit to a frozen version's entry.  A view that moved keeps its pin in every
later version's entry.

The hash is of the answer's canonical JSON with floats at 10 significant
digits (``test_engine_fingerprint._canonical``), without the fields that are
text rather than a computed answer (``run_view``).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Tuple

import pytest

from backend.app.core.stats_engine import ENGINE_VERSION
from backend.tests.unit.services import mean_metrics_harness as harness
from backend.tests.unit.services import test_sufficient_stats_fingerprint as sufficient
from backend.tests.unit.services.test_engine_fingerprint import _canonical

pytestmark = [pytest.mark.unit]

#: Each view's answer before any view read a mean metric's values (every one
#: reads the share of users: "no difference"), recorded on main's code before
#: ``mean_arms.py`` existed.  Never edited.
PRE_MEAN_DIGESTS: Dict[str, str] = {
    "sequential": "46a820726d70c436957872e734df2e1723e79100ba25d944f85b0e5f61329da7",
    "bayesian": "2fdf7e6d03cc2e665e5fe030a1395f1ab57d26fbd6e3aea898bc52c3396ff8ab",
    "sample_size": "b8a9abd1209ceb876e4d2c08d426c599b0f4d7506aa45b4318604d299836e745",
    "daily": "970e795476740dbeb266dc48896ccdeda31447325af3949b55fcda79dd15d71d",
    "live": "478190aac00a035a6a96f733eef37074ebdc0bdf76f4d11bc81904b25b7dbadd",
    "breakdown": "24c44e68cc1ca8d45120ae0ca4ca61217e2e6e007485f8564c6543438e9f8d13",
}

#: ``{engine version: {view: digest}}`` for the views whose answer moved.
MEAN_VIEW_PINS: Dict[str, Dict[str, str]] = {}


def view_digest(view: str, monkeypatch: pytest.MonkeyPatch) -> Tuple[str, str]:
    """sha256 of ``view``'s canonical answer, and the payload hashed."""
    output: Any = harness.run_view(view, monkeypatch)
    payload = json.dumps(_canonical(output), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), payload


@pytest.mark.parametrize("view", harness.VIEWS)
def test_a_views_answer_moves_only_with_an_engine_bump(view, monkeypatch):
    digest, payload = view_digest(view, monkeypatch)
    allowed = {PRE_MEAN_DIGESTS.get(view)}
    pinned = MEAN_VIEW_PINS.get(ENGINE_VERSION, {}).get(view)
    if pinned is not None:
        allowed.add(pinned)
    assert digest in allowed, (
        f"{view}: digest {digest} is neither the answer before the mean views "
        f"nor pinned under {ENGINE_VERSION}. A change that moves a view's "
        "answer bumps ENGINE_VERSION and pins it in MEAN_VIEW_PINS under the "
        f"new version (see this module's docstring).\n{payload}"
    )


def test_every_view_has_its_answer_before_the_mean_views():
    assert set(PRE_MEAN_DIGESTS) == set(harness.VIEWS)


def test_every_pin_is_under_a_frozen_version_after_1_14_0():
    """Every view read the share of users up to 1.14.0, so a pin under it or
    an earlier version would claim an answer it never gave; a pin under a
    version that is not frozen is never read."""
    for version, pins in MEAN_VIEW_PINS.items():
        assert version in sufficient.RELEASED_ENGINE_VERSIONS, version
        assert sufficient.RELEASED_ENGINE_VERSIONS.index(
            version
        ) > sufficient.RELEASED_ENGINE_VERSIONS.index("1.14.0"), version
        assert set(pins) <= set(harness.VIEWS), (version, sorted(pins))
