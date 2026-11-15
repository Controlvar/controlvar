"""A native ``time_window`` saves without ``start`` and ``end`` (#1527).

The ``Condition`` schema used to require ``start`` and ``end``, two keys the
engine never reads, so a window it can read (``{"days": [0, 1]}``,
``start_time`` and ``end_time``, ``start_date`` and ``end_date``) was refused
with ``...conditions[k]: not valid``. Now the schema asks only for an object,
and the walker accepts exactly what ``rules_engine.check_time_window`` reads.
Pinned, without a database:

* (T1) each readable window is accepted on flags and experiments and is
  evaluated as written, inside and outside its window;
* (T2) a window the engine cannot read is refused with the one reason, and a
  value that is not an object is still ``not valid``;
* (T3) stored windows that already answered ``error`` still do: the read
  gates gained no check;
* (T4) the one change to stored answers: native rules holding a window
  without ``start`` or ``end`` were ignored as a whole and are now read, on
  every reader (flag evaluation, experiment targeting, the SDK ruleset);
* (T5) the dashboard shape is unchanged: its window still needs ``start``
  and ``end`` (or ``start_time`` and ``end_time``).
"""

from __future__ import annotations

import copy
import re
from types import SimpleNamespace
from typing import Any, Dict, Tuple
from unittest.mock import MagicMock

import pytest

from backend.app.core.targeting_adapter import TIME_WINDOW_REASON
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.sdk_ruleset import build_flag_entry
from backend.tests.unit.core.test_targeting_vocabulary import (
    EXPERIMENT,
    FLAG,
    MOMENT,
    NATIVE_KINDS,
    SEGMENT,
    US,
    _matches,
    _validated,
    cond,
    dashboard,
    native,
    refusal,
)
from backend.tests.unit.services.test_experiment_flag_targeting_parity import (
    flag_detailed,
)

pytestmark = pytest.mark.unit

#: PE-B3-11's text, written out here so a changed reason fails.
REASON = (
    "unreadable time window; give days (0 to 6), start_time and end_time "
    "(HH:MM), or start_date and end_date (YYYY-MM-DD); a timezone must be a "
    "name such as Europe/Paris"
)

THURSDAY = "2024-01-11T12:00:00"  # MOMENT is a Wednesday (day 2), 12:00
SATURDAY = "2024-01-13T12:00:00"


def test_the_reason_names_the_readable_keys_and_never_start_or_end():
    assert TIME_WINDOW_REASON == REASON
    assert "\n" not in REASON and len(REASON) <= 200
    assert re.search(r"\b(start|end)\b", REASON) is None


# ---------------------------------------------------------------------------
# (T1) Readable windows are accepted and evaluated as written
# ---------------------------------------------------------------------------

#: label -> (window, a moment inside it, a moment outside it)
READABLE: Dict[str, Tuple[Dict[str, Any], str, str]] = {
    "days only": ({"days": [2]}, MOMENT, THURSDAY),
    "weekdays (the reference page's second example)": (
        {"days": [0, 1, 2, 3, 4]},
        MOMENT,
        SATURDAY,
    ),
    "days, true read as 1": ({"days": [True, 2]}, MOMENT, THURSDAY),
    "times only": (
        {"start_time": "09:00", "end_time": "17:00"},
        MOMENT,
        "2024-01-10T18:00:00",
    ),
    "times spanning midnight": (
        {"start_time": "22:00", "end_time": "13:00"},
        MOMENT,
        "2024-01-10T15:00:00",
    ),
    "dates only": (
        {"start_date": "2024-01-01", "end_date": "2024-01-31"},
        MOMENT,
        "2024-02-07T12:00:00",
    ),
    "days and a timezone": (
        {"days": [2], "timezone": "Europe/Paris"},
        MOMENT,
        THURSDAY,
    ),
    "days and a null timezone": ({"days": [2], "timezone": None}, MOMENT, THURSDAY),
    "days with start and end besides": (
        {"start": "x", "end": "y", "days": [2]},
        MOMENT,
        THURSDAY,
    ),
    "days and an unknown key": ({"days": [2], "note": "q1"}, MOMENT, THURSDAY),
}


@pytest.mark.regression
@pytest.mark.parametrize("kind", NATIVE_KINDS)
@pytest.mark.parametrize("label", sorted(READABLE))
def test_every_readable_window_is_accepted_and_evaluated(label, kind):
    """(T1) On main each of these but the one with ``start`` and ``end`` is
    refused with ``rules[0].rule.conditions[0]: not valid``."""
    window, inside, outside = READABLE[label]
    raw = native(cond("time_window", window))
    assert refusal(kind, raw) is None
    rules = _validated(kind, raw)
    assert _matches(kind, rules, {"a": inside}) is True
    assert _matches(kind, rules, {"a": outside}) is False


@pytest.mark.parametrize("kind", NATIVE_KINDS)
def test_a_window_with_no_day_is_accepted_and_matches_nobody(kind):
    """Readable, and an empty range, like a 0% rollout (accepted by design)."""
    raw = native(cond("time_window", {"days": []}))
    assert refusal(kind, raw) is None
    rules = _validated(kind, raw)
    for moment in (MOMENT, THURSDAY, SATURDAY):
        assert _matches(kind, rules, {"a": moment}) is False


# ---------------------------------------------------------------------------
# (T2) Unreadable windows are refused with the reason
# ---------------------------------------------------------------------------

UNREADABLE = {
    "no key": {},
    "an unknown key only": {"foo": 1},
    "start only": {"start": "09:00"},
    "start and end only": {"start": "09:00", "end": "17:00"},
    "days outside 0-6": {"days": [7]},
    "days as text": {"days": "0,1"},
    "start_time alone": {"start_time": "09:00"},
    "end_time alone": {"end_time": "17:00"},
    "times not HH:MM": {"start_time": "0900", "end_time": "1700"},
    "dates with slashes": {"start_date": "2024/01/01", "end_date": "2024/01/31"},
    "start_date after end_date": {
        "start_date": "2024-02-01",
        "end_date": "2024-01-01",
    },
    "an unknown timezone": {"days": [1], "timezone": "Mars/Olympus"},
}


@pytest.mark.regression
@pytest.mark.parametrize("kind", NATIVE_KINDS)
@pytest.mark.parametrize("label", sorted(UNREADABLE))
def test_an_unreadable_window_is_refused_with_the_reason(label, kind):
    rules = native(US, cond("time_window", UNREADABLE[label]))
    assert refusal(kind, rules) == f"rules[0].rule.conditions[1].value: {REASON}"


@pytest.mark.parametrize("kind", NATIVE_KINDS)
@pytest.mark.parametrize("value", ["09:00-17:00", ["09:00", "17:00"], None])
def test_a_value_that_is_not_an_object_is_still_not_valid(value, kind):
    rules = native(US, cond("time_window", value))
    assert refusal(kind, rules) == "rules[0].rule.conditions[1]: not valid"


# ---------------------------------------------------------------------------
# Stored rules: what each reader answers
# ---------------------------------------------------------------------------


def experiment_answer(raw: Any, context: Dict[str, Any]) -> Tuple[bool, str]:
    """``(eligible, reason)`` from experiment targeting, as assignment asks it."""
    experiment = SimpleNamespace(
        id="00000000-0000-0000-0000-000000001527",
        targeting_rules=copy.deepcopy(raw),
    )
    out = AssignmentService(MagicMock())._evaluate_experiment_targeting(
        experiment, dict(context), user_id="user-1527"
    )
    return out["eligible"], out["reason"]


def sdk_evaluation(raw: Any) -> str:
    """``local`` or ``remote``: how ``GET /api/v1/sdk/ruleset`` serves a flag."""
    flag = FeatureFlag(
        key="tw",
        name="tw",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=0,
        targeting_rules=copy.deepcopy(raw),
    )
    return build_flag_entry(flag)["evaluation"]


OFF_ERROR = {"enabled": False, "reason": "error", "rule_id": None}


@pytest.mark.regression
def test_a_stored_unreadable_window_still_answers_error():
    """(T3) A guard, not a regression: it passes on main too, and fails when a
    readability check is put in a read gate (the ``Condition`` model, for
    one), which would turn these from ``error`` into the global rollout."""
    stored = native(cond("time_window", {"start": "09:00", "end": "17:00"}))
    assert flag_detailed(stored, {"a": MOMENT}, rollout=100) == OFF_ERROR
    iso = dashboard(
        {
            "attribute": "a",
            "operator": "time_window",
            "value": {"start": "2024-01-01T09:00:00", "end": "2024-01-31T17:00:00"},
        }
    )
    assert experiment_answer(iso, {"a": MOMENT}) == (
        False,
        "No targeting rules matched",
    )


DAYS = native(cond("time_window", {"days": [2]}))
EMPTY = native(cond("time_window", {}))
#: ``country eq US`` first, then a days-only window: a healthy rule that was
#: ignored with the window and now applies.
SIBLING = {
    "rules": [
        {"id": "us", "rule": {"operator": "and", "conditions": [US]}, "priority": 0},
        {
            "id": "w",
            "rule": {
                "operator": "and",
                "conditions": [cond("time_window", {"days": [2]})],
            },
            "priority": 1,
        },
    ]
}

#: label -> (stored rules, context, the flag's rollout, the flag's answer, the
#: experiment's answer). Each row differs from main's answer: there every one
#: of these rulesets was ignored, so the flag served its rollout and experiment
#: targeting answered ``(False, "Targeting evaluation error")``.
STORED_ANSWERS = {
    "days only, Wednesday, flag at 0%": (
        DAYS,
        {"a": MOMENT},
        0,
        {"enabled": True, "reason": "targeting_rule", "rule_id": "r"},
        (True, "Matched targeting rule: r"),
    ),
    "days only, Thursday, flag at 100%": (
        DAYS,
        {"a": THURSDAY},
        100,
        {"enabled": True, "reason": "rollout", "rule_id": None},
        (False, "No targeting rules matched"),
    ),
    "no key, flag at 100%: on to off": (
        EMPTY,
        {"a": MOMENT},
        100,
        OFF_ERROR,
        (False, "No targeting rules matched"),
    ),
    "sibling US rule, Thursday, a US user, flag at 0%": (
        SIBLING,
        {"a": THURSDAY, "country": "US"},
        0,
        {"enabled": True, "reason": "targeting_rule", "rule_id": "us"},
        (True, "Matched targeting rule: us"),
    ),
    "sibling US rule, Wednesday, a DE user, flag at 0%": (
        SIBLING,
        {"a": MOMENT, "country": "DE"},
        0,
        {"enabled": True, "reason": "targeting_rule", "rule_id": "w"},
        (True, "Matched targeting rule: w"),
    ),
}


@pytest.mark.regression
@pytest.mark.parametrize("label", list(STORED_ANSWERS))
def test_a_stored_native_window_without_start_or_end_is_now_evaluated(label):
    """(T4) The answers that change on upgrade (PE-B3-2), pinned per reader."""
    stored, context, rollout, flag, experiment = STORED_ANSWERS[label]
    assert flag_detailed(stored, context, rollout=rollout) == flag
    assert experiment_answer(stored, context) == experiment


@pytest.mark.regression
@pytest.mark.parametrize(
    "stored", [DAYS, EMPTY, SIBLING], ids=["days", "{}", "sibling"]
)
def test_the_sdk_ruleset_serves_such_a_flag_for_remote_evaluation(stored):
    """(T4) On main the flag was served ``local`` with no rules (they were
    ignored); now its rules are read, and a ``time_window`` is evaluated by
    the server."""
    assert sdk_evaluation(stored) == "remote"


#: ``country eq US`` first and the window ONLY in ``default_rule``: the rules
#: are otherwise local, so the SDK entry stays local (with its rules and the
#: ``default_rule``), where main served it local with no rules at all.
DEFAULT_ONLY = {
    "rules": [
        {"id": "us", "rule": {"operator": "and", "conditions": [US]}, "priority": 0}
    ],
    "default_rule": {
        "id": "d",
        "rule": {
            "operator": "and",
            "conditions": [cond("time_window", {})],
        },
        "rollout_percentage": 100,
    },
}


@pytest.mark.regression
def test_a_window_only_in_the_default_rule_serves_locally_with_the_default_rule():
    """(N1) The SDK sentence "such a flag is served remotely" is false here:
    the flag stays ``local``, with its rules and its ``default_rule``, where
    main dropped both (the whole ruleset was ignored). And the flag's answer
    moves: ``default_rule`` now applies to a user no rule matches, so a DE user
    at rollout 0 goes from off (``rollout``) to on (``targeting_rule``)."""
    flag = FeatureFlag(
        key="tw",
        name="tw",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=0,
        targeting_rules=copy.deepcopy(DEFAULT_ONLY),
    )
    entry = build_flag_entry(flag)
    assert entry["evaluation"] == "local"
    assert len(entry["rules"]) == 1
    assert entry["default_rule"] == {"id": "d", "rollout_percentage": 100}
    assert flag_detailed(DEFAULT_ONLY, {"a": MOMENT, "country": "DE"}, rollout=0) == {
        "enabled": True,
        "reason": "targeting_rule",
        "rule_id": "d",
    }


# ---------------------------------------------------------------------------
# (T5) The dashboard shape is unchanged
# ---------------------------------------------------------------------------

DASHBOARD_WINDOWS = {
    "days only": {"days": [0, 1]},
    "dates only": {"start_date": "2024-01-01", "end_date": "2024-01-31"},
    "days and a timezone": {"days": [0], "timezone": "Europe/Paris"},
}


@pytest.mark.parametrize("kind", [FLAG, EXPERIMENT, SEGMENT])
@pytest.mark.parametrize("label", sorted(DASHBOARD_WINDOWS))
def test_a_dashboard_window_still_needs_start_and_end(label, kind):
    """(T5) The converter (``_time_window_value``) still asks for ``start``
    and ``end`` or ``start_time`` and ``end_time``; #1527 is native-only."""
    rules = dashboard(
        {"attribute": "t", "operator": "time_window", "value": DASHBOARD_WINDOWS[label]}
    )
    assert refusal(kind, rules) == (
        "groups[0].conditions[0].value: value is not valid for the operator"
    )
