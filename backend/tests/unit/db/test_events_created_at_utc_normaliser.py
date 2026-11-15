"""The frozen normaliser in ``1ab99332f0ba`` agrees with the live one (#579).

The revision rewrites stored ``events.created_at`` values with ``_normalise``, a
copy of the string branch of
``backend.app.models.event.normalize_event_timestamp`` frozen into the revision
file (a migration imports nothing from the application, so that it does today
what it did the day it shipped).  The two must agree, value for value, or the
rewrite stores something the write path would never write.

**If a later change to ``normalize_event_timestamp`` breaks this test, update
this test's expectations -- never the shipped revision.**  The revision has
already run on every database that took it; editing it changes nothing there
and makes a fresh replay disagree with every existing install.  A normaliser
that really must write a different format needs its own new revision.

No database.  ``CORPUS`` is also what
``backend/tests/integration/database/test_events_created_at_utc_migration.py``
plants, so its expectations -- written out by hand from the offsets, not
computed by the code under test -- are checked against PostgreSQL there.
"""

from __future__ import annotations

import importlib.util
import pathlib
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from backend.app.models.event import normalize_event_timestamp

pytestmark = [pytest.mark.unit]

REVISION = "1ab99332f0ba"
VERSIONS = (
    pathlib.Path(__file__).resolve().parents[3]
    / "app"
    / "db"
    / "migrations"
    / "versions"
)

#: The shapes a stored ``created_at`` can have: ``(stored, after, candidate)``.
#: ``after`` is what the upgrade leaves in the row (``None``: the value cannot
#: be read and is left exactly as stored); ``candidate`` is whether the
#: migration's scan selects it, which the documented SQL must agree with.
CORPUS: list[tuple[str, Optional[str], bool]] = [
    ("2026-10-01T01:00:00+00:00", "2026-10-01T01:00:00+00:00", False),
    ("2026-10-01T01:00:00.250000+00:00", "2026-10-01T01:00:00.250000+00:00", False),
    ("2026-10-01T01:00:00.000000+00:00", "2026-10-01T01:00:00+00:00", True),
    ("2026-10-01T01:00:00", "2026-10-01T01:00:00+00:00", True),
    ("2026-10-01T01:00:00+02:00", "2026-09-30T23:00:00+00:00", True),
    ("2026-10-01T01:00:00-03:00", "2026-10-01T04:00:00+00:00", True),
    ("2026-10-01T01:00:00Z", "2026-10-01T01:00:00+00:00", True),
    ("2026-10-01T01:00:00-00:00", "2026-10-01T01:00:00+00:00", True),
    ("2026-10-01T01:00:00+0200", "2026-09-30T23:00:00+00:00", True),
    ("2026-10-01 01:00:00+02", "2026-09-30T23:00:00+00:00", True),
    ("2026-10-01T01:00:00+02:00:30", "2026-09-30T22:59:30+00:00", True),
    # Seven digits: truncated to six, as Python does (PostgreSQL would round).
    ("2026-10-01T01:00:00.1234567Z", "2026-10-01T01:00:00.123456+00:00", True),
    ("2026-10-01T01:00:00.5+00:00", "2026-10-01T01:00:00.500000+00:00", True),
    (" 2026-10-01T01:00:00+00:00", "2026-10-01T01:00:00+00:00", True),
    ("2026-10-01T01:00:00+00:00\n", "2026-10-01T01:00:00+00:00", True),
    ("garbage+05:00", None, True),
    # Canonical in shape, so not selected; unreadable either way.
    ("2026-13-01T01:00:00+00:00", None, False),
    # Out of range once moved to UTC: the frozen copy raises OverflowError; the
    # live normaliser turns that into a ValueError (#1684). Unreadable either way.
    ("0001-01-01T00:00:00+05:00", None, True),
    ("9999-12-31T23:00:00-05:00", None, True),
    ("", None, True),
]

#: Unreadable values PostgreSQL's own timestamp parser would accept or reject
#: differently; the migration must leave every one as stored.
NOT_INSTANTS = ["yesterday", "epoch", "infinity", "1696000000", "now"]
OUT_OF_RANGE = ["0001-01-01T00:00:00+05:00", "9999-12-31T23:00:00-05:00"]


def load_revision():
    """The revision module, loaded from its file under a private name."""
    (path,) = VERSIONS.glob(f"{REVISION}_*.py")
    spec = importlib.util.spec_from_file_location(
        f"_rev_{REVISION}_{uuid.uuid4().hex[:6]}", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def frozen():
    return load_revision()._normalise


def _outcome(function, value: str):
    try:
        return ("ok", function(value))
    except (ValueError, OverflowError) as exc:
        return ("raises", type(exc).__name__)


def _random_values(seed: int, count: int) -> list[str]:
    """Offsets from -14:00 to +14:00 (+05:45 and +00:00 included), Z, naive;
    microseconds 0, 1, 999999 and random; T and space separators."""
    rng = random.Random(seed)
    offsets = [
        timedelta(hours=-14),
        timedelta(hours=14),
        timedelta(hours=5, minutes=45),
        timedelta(hours=5, minutes=30),
        timedelta(0),
    ] + [timedelta(minutes=15 * rng.randint(-56, 56)) for _ in range(20)]
    values = []
    for _ in range(count):
        moment = datetime(2000, 1, 1) + timedelta(
            seconds=rng.randint(0, 40 * 365 * 86400)
        )
        moment = moment.replace(
            microsecond=rng.choice([0, 1, 999999, rng.randint(0, 999999)])
        )
        separator = rng.choice(["T", " "])
        kind = rng.choice(["offset", "Z", "naive"])
        if kind == "naive":
            values.append(moment.isoformat(sep=separator))
        elif kind == "Z":
            values.append(moment.isoformat(sep=separator) + "Z")
        else:
            aware = moment.replace(tzinfo=timezone(rng.choice(offsets)))
            values.append(aware.isoformat(sep=separator))
    return values


@pytest.mark.regression
def test_the_frozen_copy_agrees_with_the_normaliser_on_random_values(frozen):
    values = _random_values(seed=579, count=5000)

    mismatches = [
        (v, _outcome(frozen, v), _outcome(normalize_event_timestamp, v))
        for v in values
        if _outcome(frozen, v) != _outcome(normalize_event_timestamp, v)
    ]

    assert mismatches == [], mismatches[:5]


@pytest.mark.regression
@pytest.mark.parametrize("stored, after, candidate", CORPUS)
def test_each_shape_becomes_what_the_corpus_says(frozen, stored, after, candidate):
    """The hand-written expectation, then the live normaliser: all three agree,
    except that an out-of-range date is an OverflowError in the frozen copy and
    a ValueError in the live one (#1684)."""
    if stored in OUT_OF_RANGE:
        assert _outcome(frozen, stored) == ("raises", "OverflowError")
        assert _outcome(normalize_event_timestamp, stored) == ("raises", "ValueError")
    else:
        assert _outcome(frozen, stored) == _outcome(normalize_event_timestamp, stored)
    if after is None:
        assert _outcome(frozen, stored)[0] == "raises"
    else:
        assert frozen(stored) == after


@pytest.mark.regression
@pytest.mark.parametrize("value", NOT_INSTANTS + OUT_OF_RANGE)
def test_values_that_are_not_instants_raise_what_the_migration_catches(frozen, value):
    """``ValueError`` or ``OverflowError``, and nothing else: the two the
    migration counts as unreadable.  An out-of-range date raises the second in
    the frozen copy; the live normaliser raises ``ValueError`` for it (#1684),
    which the migration's ``except (ValueError, OverflowError)`` also covers."""
    with pytest.raises((ValueError, OverflowError)) as frozen_raised:
        frozen(value)
    with pytest.raises((ValueError, OverflowError)) as live_raised:
        normalize_event_timestamp(value)
    if value in OUT_OF_RANGE:
        assert frozen_raised.type is OverflowError
        assert live_raised.type is ValueError
    else:
        assert frozen_raised.type is live_raised.type


def test_the_canonical_output_is_a_fixed_point(frozen):
    for value in _random_values(seed=580, count=500):
        once = frozen(value)
        assert frozen(once) == once


def test_the_scan_selects_with_the_candidate_predicate_exactly():
    """``_SCAN`` spells ``_CANDIDATES`` again (each statement is one fixed
    string); a drift between the two would make the documented count lie."""
    revision = load_revision()
    assert revision._SCAN.endswith("WHERE " + revision._CANDIDATES)
    assert revision._SCAN.count("WHERE ") == 1
