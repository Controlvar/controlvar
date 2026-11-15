"""Stored targeting rules the server cannot read affect only their owner (#1674).

Only a direct database write leaves such rules: the write validators refuse an
integer too large for a float in a geo value and a version part ``int()``
cannot read. Reading one raises (``OverflowError``, ``ValueError``), and on
main that escaped every reader that reads more than one owner's rules:

* ``get_user_flags`` (``GET /feature-flags/user/{id}``) raised for every flag;
* ``build_ruleset`` (``GET /sdk/ruleset``) raised, so no SDK could load one;
* a rules segment holding such rules made ``resolve_segment_memberships``
  raise, so a flag naming it killed the bulk call too; saving a flag that
  names it (``segment_reference_problem``) and ``POST /segments/{id}/evaluate``
  (``stored_segment_rules``) raised.

Now each reader guards the pure read only. The flag answers ``false``
(``reason: "error"`` on ``/evaluate``), the ruleset marks it ``remote``, the
segment is unavailable or "not valid", and every other flag and segment is
answered as before. Never fail open: the flag's global rollout is not served.

DB-free: ``FeatureFlag`` objects and stub sessions. The routes, on a real
database: ``backend/tests/integration/api/test_unreadable_stored_rules_api.py``.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from backend.app.core.log_once import EVALUATION_NOTES
from backend.app.models import register_core_models
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.segment import SegmentStatus
from backend.app.schemas.experiment import ExperimentUpdate
from backend.app.schemas.feature_flag import FeatureFlagCreate, FeatureFlagUpdate
from backend.app.schemas.segment import SegmentCreate
from backend.app.services import audience_service, sdk_ruleset, segment_membership
from backend.app.services import feature_flag_service as ffs
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.audience_service import (
    AudienceService,
    SegmentRulesNotValid,
    stored_segment_rules,
)
from backend.app.services.sdk_ruleset import (
    build_flag_entry,
    build_ruleset,
    canonical_json,
    ruleset_version,
)
from backend.app.services.segment_membership import (
    SegmentMemberships,
    resolve_segment_memberships,
    segment_reference_problem,
)

pytestmark = pytest.mark.unit

register_core_models()


def cond(attribute: str, operator: str, value: Any) -> Dict[str, Any]:
    return {"attribute": attribute, "operator": operator, "value": value}


def dashboard(*conditions: Dict[str, Any]) -> Dict[str, Any]:
    """Rules in the shape the dashboard stores."""
    return {
        "logical_operator": "AND",
        "groups": [{"logical_operator": "AND", "conditions": list(conditions)}],
    }


#: An integer too large for a float: ``float()`` raises OverflowError.
GEO = dashboard(
    cond("location", "geo_within_radius", {"lat": 1, "lon": 2, "radius": 10**400})
)
#: A superscript two is a digit to ``str.isdigit`` but not to ``int()``.
SEMVER = dashboard(cond("app_version", "semver_gte", "1.²"))
US = dashboard(cond("country", "equals", "US"))

BAD_SEGMENT = "5e9a1c00-0000-4000-8000-000000001674"
GOOD_SEGMENT = "5e9a1c00-0000-4000-8000-000000000440"
NAMES_BAD_SEGMENT = dashboard(cond("segment", "in_segment", BAD_SEGMENT))
NAMES_GOOD_SEGMENT = dashboard(cond("segment", "in_segment", GOOD_SEGMENT))

USER = "user-1674"
CONTEXT = {"country": "US"}

REMOTE = "remote"
DISABLED_BY_ERROR = {"enabled": False, "reason": "error", "rule_id": None}
NOT_VALID = "groups[0].conditions[0].value: segment rules not valid"


def flag(key: str, rules: Any, rollout: int = 100, status=FeatureFlagStatus.ACTIVE):
    """A stored flag. 100% by default: an unreadable one must still be off."""
    return FeatureFlag(
        id=uuid.uuid4(),
        key=key,
        name=key,
        status=status,
        rollout_percentage=rollout,
        targeting_rules=rules,
    )


def segment_row(segment_id: str, rules: Any) -> SimpleNamespace:
    """A row of the membership query (column tuples, never an ORM object)."""
    return SimpleNamespace(
        id=uuid.UUID(segment_id), kind="rules", status=SegmentStatus.ACTIVE, rules=rules
    )


def stub_db(flags=(), segments=(), segment_error: Exception = None) -> MagicMock:
    """A session answering the flag query with ``flags`` and the segment query
    with ``segments`` (or raising ``segment_error`` from it)."""
    db = MagicMock()

    def query(*entities):
        q = MagicMock()
        result = q.filter.return_value.all
        if entities and entities[0] is FeatureFlag:
            result.return_value = list(flags)
        elif segment_error is not None:
            result.side_effect = segment_error
        else:
            result.return_value = list(segments)
        return q

    db.query.side_effect = query
    return db


def db_error() -> OperationalError:
    return OperationalError("SELECT", {}, Exception("server closed the connection"))


class RulesReadFails:
    """``inner`` whose stored rules cannot be loaded: a database error, as an
    expired attribute's refresh would raise. Never stored rules that raise."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def targeting_rules(self) -> Any:
        raise db_error()

    @property
    def rules(self) -> Any:
        raise db_error()


@pytest.fixture(autouse=True)
def metrics(monkeypatch):
    """Evaluation writes metrics and error-log rows; recorded here instead."""
    fake = MagicMock()
    monkeypatch.setattr(ffs, "MetricsService", fake)
    return fake


@pytest.fixture(autouse=True)
def fresh_notes():
    """The log-once store is process-global (PE 8): every test starts empty."""
    EVALUATION_NOTES.clear()
    yield
    EVALUATION_NOTES.clear()


def bulk(flags, segments=()) -> Dict[str, bool]:
    db = stub_db(flags, segments)
    return ffs.FeatureFlagService(db=db).get_user_flags(USER, CONTEXT)


# ---------------------------------------------------------------------------
# The bulk call: GET /feature-flags/user/{user_id}
# ---------------------------------------------------------------------------

UNREADABLE = {
    "geo_overflow": ([GEO], []),
    "semver_digit": ([SEMVER], []),
    "segment_overflow": ([NAMES_BAD_SEGMENT], [segment_row(BAD_SEGMENT, GEO)]),
}


@pytest.mark.regression
@pytest.mark.parametrize("case", list(UNREADABLE))
def test_unreadable_stored_rules_affect_only_their_flag(case):
    """On main the bulk call raised (OverflowError, ValueError) for every
    flag. Now the bad flag, at 100%, answers false and the others as before."""
    rules, segments = UNREADABLE[case]
    flags = [
        flag("good-rule", US, rollout=0),
        flag("bad", rules[0]),
        flag("good-all", None),
        flag("good-none", None, rollout=0),
    ]
    assert bulk(flags, segments) == {
        "good-rule": True,
        "bad": False,
        "good-all": True,
        "good-none": False,
    }


@pytest.mark.parametrize("rules", [GEO, SEMVER], ids=["geo", "semver"])
def test_evaluate_answers_error_and_records_it_as_before(rules, metrics):
    """``/evaluate/{key}`` already failed closed on main, with an error-log
    row (the safety monitor counts it). Unchanged; pinned."""
    bad = flag("bad", rules)
    service = ffs.FeatureFlagService(db=MagicMock())
    assert service.evaluate_flag_detailed(bad, USER, CONTEXT) == DISABLED_BY_ERROR
    [call] = metrics.log_error.call_args_list
    assert call.kwargs["data"].feature_flag_id == bad.id


def test_the_bulk_call_records_the_error_as_evaluate_does(metrics):
    """The bad flag is evaluated in the bulk call (no longer abandoned before
    it), so it writes the same error-log row ``/evaluate`` writes."""
    bad = flag("bad", GEO)
    assert bulk([flag("good", None), bad]) == {"good": True, "bad": False}
    [call] = metrics.log_error.call_args_list
    assert call.kwargs["data"].feature_flag_id == bad.id


# ---------------------------------------------------------------------------
# The ruleset: GET /api/v1/sdk/ruleset
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_ruleset_marks_only_the_unreadable_flags_remote():
    """On main build_ruleset raised OverflowError: no SDK could load one."""
    good = flag("good", US, rollout=25)
    flags = [
        flag("geo", GEO),
        good,
        flag("semver", SEMVER),
        flag("geo-off", GEO, status=FeatureFlagStatus.INACTIVE),
    ]
    doc = build_ruleset(flags)
    entries = {entry["key"]: entry for entry in doc["flags"]}
    assert [entry["key"] for entry in doc["flags"]] == [
        "geo",
        "geo-off",
        "good",
        "semver",
    ]
    for key in ("geo", "semver"):
        assert entries[key] == {"key": key, "active": True, "evaluation": REMOTE}
    # An inactive flag's rules are never read.
    assert entries["geo-off"] == {"key": "geo-off", "active": False}
    # The good flag is byte for byte the entry it has on its own.
    assert entries["good"]["evaluation"] == "local"
    assert canonical_json(entries["good"]) == canonical_json(build_flag_entry(good))
    content = {key: value for key, value in doc.items() if key != "version"}
    assert doc["version"] == ruleset_version(content)


def test_an_unreadable_flag_fails_closed_not_open():
    """At 100%, the bad flag is off in the bulk call, off with ``error`` on
    ``/evaluate`` and ``remote`` in the ruleset. A reader that returned None
    instead (the rejected design) would serve its 100% rollout: True,
    ``rollout`` and a local entry with no rules."""
    bad = flag("bad", GEO, rollout=100)
    assert bulk([bad]) == {"bad": False}
    service = ffs.FeatureFlagService(db=MagicMock())
    assert service.evaluate_flag_detailed(bad, USER, CONTEXT) == DISABLED_BY_ERROR
    assert build_flag_entry(bad) == {"key": "bad", "active": True, "evaluation": REMOTE}


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_segment_whose_stored_rules_raise_is_unavailable_and_only_it():
    """On main resolve_segment_memberships raised OverflowError."""
    db = stub_db(
        segments=[segment_row(BAD_SEGMENT, GEO), segment_row(GOOD_SEGMENT, US)]
    )
    memberships = resolve_segment_memberships(
        db, USER, CONTEXT, [BAD_SEGMENT, GOOD_SEGMENT]
    )
    assert memberships == SegmentMemberships(
        members=frozenset({GOOD_SEGMENT}),
        evaluated=frozenset({BAD_SEGMENT, GOOD_SEGMENT}),
        unavailable=frozenset({BAD_SEGMENT}),
    )
    db.rollback.assert_not_called()


@pytest.mark.regression
def test_naming_that_segment_is_refused_as_not_valid():
    """On main segment_reference_problem raised OverflowError: saving a flag
    or experiment that names the segment answered 500, now 422."""
    db = stub_db(segments=[segment_row(BAD_SEGMENT, GEO)])
    assert segment_reference_problem(db, NAMES_BAD_SEGMENT) == NOT_VALID


@pytest.mark.regression
def test_stored_segment_rules_that_raise_are_not_valid(monkeypatch):
    """EMG5-6: on main stored_segment_rules raised OverflowError, so
    ``POST /segments/{id}/evaluate`` answered 500, not its documented 409."""
    segment = SimpleNamespace(id=BAD_SEGMENT, name="bad", kind="rules", rules=GEO)
    with pytest.raises(SegmentRulesNotValid):
        stored_segment_rules(segment)
    monkeypatch.setattr(
        AudienceService, "get_segment", staticmethod(lambda db, sid: segment)
    )
    with pytest.raises(SegmentRulesNotValid):
        AudienceService.evaluate_membership(MagicMock(), BAD_SEGMENT, CONTEXT)


# ---------------------------------------------------------------------------
# Experiments (the issue's experiment clause)
# ---------------------------------------------------------------------------


def experiment_targeting(rules: Any, db=None) -> Dict[str, Any]:
    outcome = AssignmentService(db or MagicMock())._evaluate_experiment_targeting(
        SimpleNamespace(id="exp-1674", targeting_rules=rules),
        AssignmentService._build_targeting_context(USER, CONTEXT),
        user_id=USER,
    )
    outcome.pop("metrics", None)
    return outcome


@pytest.mark.parametrize("rules", [GEO, SEMVER], ids=["geo", "semver"])
def test_an_experiment_whose_stored_rules_raise_refuses_the_user(rules):
    """Already true on main (assignment_service's own catch); pinned."""
    assert experiment_targeting(rules) == {
        "eligible": False,
        "rule_id": None,
        "reason": "Targeting evaluation error",
        "validation_passed": False,
    }


@pytest.mark.regression
def test_an_experiment_naming_a_segment_whose_rules_raise_refuses_the_user():
    """Refused on main too, but as "Targeting evaluation error" (the
    OverflowError escaped membership); now the segment is unavailable."""
    db = stub_db(segments=[segment_row(BAD_SEGMENT, GEO)])
    assert experiment_targeting(NAMES_BAD_SEGMENT, db) == {
        "eligible": False,
        "rule_id": None,
        "reason": "Segment membership could not be decided",
        "validation_passed": False,
    }


# ---------------------------------------------------------------------------
# Only the pure read is guarded (PE 8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["query", "rules"])
def test_a_database_error_still_takes_the_database_error_path(where):
    """Rolled back, and every requested segment unavailable: never one
    segment's "unreadable rules"."""
    requested = frozenset({BAD_SEGMENT, GOOD_SEGMENT})
    if where == "query":
        db = stub_db(segment_error=db_error())
    else:
        db = stub_db(
            segments=[
                segment_row(GOOD_SEGMENT, US),
                RulesReadFails(segment_row(BAD_SEGMENT, US)),
            ]
        )
    memberships = resolve_segment_memberships(db, USER, CONTEXT, requested)
    assert memberships == SegmentMemberships(frozenset(), requested, requested)
    db.rollback.assert_called_once_with()


def _bulk_read_fails():
    bulk([flag("good", None), RulesReadFails(flag("bad", US))])


def _ruleset_read_fails():
    build_flag_entry(RulesReadFails(flag("bad", US)))


def _reference_read_fails():
    db = stub_db(segments=[RulesReadFails(segment_row(BAD_SEGMENT, US))])
    segment_reference_problem(db, NAMES_BAD_SEGMENT)


def _stored_segment_read_fails():
    stored_segment_rules(
        RulesReadFails(SimpleNamespace(id=BAD_SEGMENT, kind="rules", rules=US))
    )


READ_FAILS = {
    "get_user_flags": _bulk_read_fails,
    "build_flag_entry": _ruleset_read_fails,
    "segment_reference_problem": _reference_read_fails,
    "stored_segment_rules": _stored_segment_read_fails,
}


@pytest.mark.parametrize("site", list(READ_FAILS))
def test_a_database_error_reading_the_rules_is_not_unreadable_rules(site):
    """The stored rules are read outside each new try, so a database error
    reaches the caller as one."""
    with pytest.raises(OperationalError):
        READ_FAILS[site]()


def _raise_once(exc_type, then: Any = None):
    calls: List[int] = []

    def stub(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise exc_type()
        return then

    return stub


def _interrupt_bulk(monkeypatch):
    # Raises at the bulk call's own read; a later read (inside evaluate)
    # answers None, so only that first catch decides.
    monkeypatch.setattr(
        ffs, "normalise_targeting_rules", _raise_once(KeyboardInterrupt)
    )
    bulk([flag("bad", US)])


def _interrupt_ruleset(monkeypatch):
    monkeypatch.setattr(
        sdk_ruleset, "normalise_targeting_rules", _raise_once(KeyboardInterrupt)
    )
    build_flag_entry(flag("bad", US))


def _interrupt_membership(monkeypatch):
    monkeypatch.setattr(
        segment_membership, "validate_segment_rules", _raise_once(KeyboardInterrupt)
    )
    db = stub_db(segments=[segment_row(BAD_SEGMENT, US)])
    resolve_segment_memberships(db, USER, CONTEXT, [BAD_SEGMENT])


def _interrupt_reference(monkeypatch):
    monkeypatch.setattr(
        segment_membership, "validate_segment_rules", _raise_once(KeyboardInterrupt)
    )
    db = stub_db(segments=[segment_row(BAD_SEGMENT, US)])
    segment_reference_problem(db, NAMES_BAD_SEGMENT)


def _interrupt_stored_segment(monkeypatch):
    monkeypatch.setattr(
        audience_service, "validate_segment_rules", _raise_once(KeyboardInterrupt)
    )
    stored_segment_rules(SimpleNamespace(id=BAD_SEGMENT, kind="rules", rules=US))


INTERRUPTS = {
    "get_user_flags": _interrupt_bulk,
    "build_flag_entry": _interrupt_ruleset,
    "resolve_segment_memberships": _interrupt_membership,
    "segment_reference_problem": _interrupt_reference,
    "stored_segment_rules": _interrupt_stored_segment,
}


@pytest.mark.parametrize("site", list(INTERRUPTS))
def test_a_keyboard_interrupt_is_never_swallowed(site, monkeypatch):
    """``except Exception``, never ``BaseException``."""
    with pytest.raises(KeyboardInterrupt):
        INTERRUPTS[site](monkeypatch)


# ---------------------------------------------------------------------------
# Logged once per owner, the exception type only
# ---------------------------------------------------------------------------

UNREADABLE_LOG = "Cannot read the stored targeting rules for"


def _unreadable_records(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records if UNREADABLE_LOG in r.getMessage()]


def test_logged_once_per_flag_with_the_type_only(caplog):
    flags = [flag("geo", GEO), flag("semver", SEMVER)]
    with caplog.at_level(logging.WARNING):
        build_ruleset(flags)
        build_ruleset(flags)
    # Two builds, two owners: one record each, and nothing else logged.
    assert len(caplog.records) == 2, [r.getMessage() for r in caplog.records]
    geo, semver = sorted(r.getMessage() for r in caplog.records)
    assert geo.startswith(f"{UNREADABLE_LOG} flag:geo (OverflowError);")
    assert semver.startswith(f"{UNREADABLE_LOG} flag:semver (ValueError);")
    for message in (geo, semver):
        assert message.endswith("Logged once.")
        # Never the exception's text: it can repeat the rule's content.
        assert "0000" not in message
        assert "²" not in message
        assert "int()" not in message
    # The bulk call reads the same owners: still one record each.
    with caplog.at_level(logging.WARNING):
        bulk(flags)
    assert len(_unreadable_records(caplog)) == 2


def test_logged_once_per_segment_with_the_type_only(caplog):
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            db = stub_db(segments=[segment_row(BAD_SEGMENT, GEO)])
            resolve_segment_memberships(db, USER, CONTEXT, [BAD_SEGMENT])
            segment_reference_problem(db, NAMES_BAD_SEGMENT)
    [message] = _unreadable_records(caplog)
    assert message.startswith(
        f"{UNREADABLE_LOG} segment:{BAD_SEGMENT} (OverflowError);"
    )
    assert "0000" not in message.replace(BAD_SEGMENT, "")


WRITE_SCHEMAS = {
    "flag create": lambda rules: FeatureFlagCreate(
        key="k", name="n", targeting_rules=rules
    ),
    "flag update": lambda rules: FeatureFlagUpdate(targeting_rules=rules),
    "experiment update": lambda rules: ExperimentUpdate(targeting_rules=rules),
    "segment create": lambda rules: SegmentCreate(name="segment", rules=rules),
}


@pytest.mark.parametrize("rules", [GEO, SEMVER], ids=["geo", "semver"])
@pytest.mark.parametrize("schema", list(WRITE_SCHEMAS))
def test_only_a_direct_database_write_leaves_such_rules(schema, rules):
    """Every write path refuses both values (422), so only a direct database
    write stores them. Already so on main; pinned."""
    with pytest.raises(ValidationError) as refused:
        WRITE_SCHEMAS[schema](rules)
    [error] = refused.value.errors()
    assert error["msg"] == (
        "Value error, groups[0].conditions[0].value: value is not valid for the "
        "operator"
    )


def test_a_good_flag_naming_a_good_segment_is_unaffected():
    """The bad segment is unavailable only for the flag that names it."""
    flags = [
        flag("names-bad", NAMES_BAD_SEGMENT),
        flag("names-good", NAMES_GOOD_SEGMENT, rollout=0),
    ]
    segments = [segment_row(BAD_SEGMENT, GEO), segment_row(GOOD_SEGMENT, US)]
    assert bulk(flags, segments) == {"names-bad": False, "names-good": True}
