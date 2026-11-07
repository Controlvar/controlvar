"""Which flags the list marks stale, and why (#1808): the pure half.

``stale_flags.classify`` is given the flags, the time, N (``STALE_FLAG_DAYS``),
whether the installation recorded any evaluation in the last N days, which
flags were evaluated in them and which have an ACTIVE rollout schedule. The
database half (what counts as an evaluation, the install-wide guard, the
route) is ``backend/tests/integration/api/test_stale_flags_api.py``.

Every time is a fixed instant; nothing reads the clock.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.services import stale_flags

pytestmark = [pytest.mark.unit, pytest.mark.regression]

NOW = datetime(2026, 11, 7, 12, 0, 0)
DAYS = 30
CUTOFF = NOW - timedelta(days=DAYS)
OLD = CUTOFF - timedelta(days=5)
SECOND = timedelta(seconds=1)

NO_EVAL = "not_evaluated"
ON = "on_for_everyone"
OFF = "off_for_everyone"

DASHBOARD_CONDITION = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "country", "operator": "equals", "value": "DE"}
            ],
        }
    ],
}


def flag(
    status=FeatureFlagStatus.ACTIVE,
    rollout_percentage=50,
    targeting_rules=None,
    tags=None,
    created_at=OLD,
    updated_at=OLD,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        key=f"flag-{uuid.uuid4().hex[:8]}",
        status=status,
        rollout_percentage=rollout_percentage,
        targeting_rules=targeting_rules,
        tags=tags,
        created_at=created_at,
        updated_at=updated_at,
    )


def reasons(
    item,
    *,
    evaluated=False,
    evaluations_seen=True,
    scheduled=False,
    now=NOW,
    days=DAYS,
):
    answer = stale_flags.classify(
        [item],
        now=now,
        days=days,
        evaluations_seen=evaluations_seen,
        evaluated_ids={item.id} if evaluated else set(),
        active_schedule_ids={item.id} if scheduled else set(),
    )
    assert set(answer) == {item.id}
    return answer[item.id]


# ---------------------------------------------------------------------------
# V1: the created_at and updated_at boundaries (inclusive)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "created_at, expected",
    [
        (CUTOFF - SECOND, [OFF]),
        (CUTOFF, [OFF]),
        (CUTOFF + SECOND, []),
    ],
    ids=["a second older than N days", "exactly N days", "a second younger"],
)
def test_v1_a_flag_created_exactly_n_days_ago_is_old_enough(created_at, expected):
    item = flag(
        status=FeatureFlagStatus.INACTIVE,
        created_at=created_at,
        updated_at=created_at,
    )
    assert reasons(item, evaluated=True) == expected


@pytest.mark.parametrize(
    "updated_at, expected",
    [
        (CUTOFF - SECOND, [ON]),
        (CUTOFF, [ON]),
        (CUTOFF + SECOND, []),
    ],
    ids=["a second older than N days", "exactly N days", "a second younger"],
)
def test_v1_a_flag_unchanged_for_exactly_n_days_is_unchanged(updated_at, expected):
    item = flag(rollout_percentage=100, updated_at=updated_at)
    assert reasons(item, evaluated=True) == expected


def test_v1_a_different_n_moves_the_cutoff():
    item = flag(status=FeatureFlagStatus.INACTIVE, created_at=NOW - timedelta(days=8))
    item.updated_at = item.created_at
    assert reasons(item, evaluated=True, days=7) == [OFF]
    assert reasons(item, evaluated=True, days=9) == []


# ---------------------------------------------------------------------------
# V2: a flag younger than N days is never stale
# ---------------------------------------------------------------------------


def test_v2_a_flag_younger_than_n_days_is_never_stale():
    young = NOW - timedelta(days=1)
    item = flag(status=FeatureFlagStatus.INACTIVE, created_at=young, updated_at=OLD)
    assert reasons(item, evaluated=False) == []


# ---------------------------------------------------------------------------
# V3: only 0 and 100 percent are one answer for everyone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "percentage, expected",
    [(0, [OFF]), (1, []), (50, []), (99, []), (100, [ON])],
)
def test_v3_only_0_and_100_percent_serve_everyone_one_answer(percentage, expected):
    item = flag(rollout_percentage=percentage)
    assert reasons(item, evaluated=True) == expected


# ---------------------------------------------------------------------------
# V4: what counts as targeting rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rules",
    [
        None,
        {},
        [],
        {"logical_operator": "AND", "groups": []},
        {
            "logical_operator": "AND",
            "groups": [{"logical_operator": "AND", "conditions": []}],
        },
        {"rules": []},
    ],
    ids=[
        "none",
        "empty object",
        "empty list",
        "dashboard, no groups",
        "dashboard, one empty group",
        "native, no rules",
    ],
)
def test_v4_rules_that_select_nobody_are_no_rules(rules):
    assert reasons(
        flag(rollout_percentage=100, targeting_rules=rules), evaluated=True
    ) == [ON]
    assert reasons(
        flag(rollout_percentage=0, targeting_rules=rules), evaluated=True
    ) == [OFF]


@pytest.mark.parametrize(
    "rules",
    [
        DASHBOARD_CONDITION,
        {
            "rules": [],
            "default_rule": {
                "id": "default",
                "rule": {
                    "operator": "and",
                    "conditions": [
                        {"attribute": "country", "operator": "eq", "value": "DE"}
                    ],
                },
                "rollout_percentage": 100,
            },
        },
        [{"type": "context", "conditions": [{"key": "plan", "value": "pro"}]}],
        {"groups": "not a list of groups"},
        {"something": "else"},
        "unreadable",
    ],
    ids=[
        "a dashboard condition",
        "a native default rule",
        "a legacy list",
        "unreadable groups",
        "an unknown object",
        "not an object",
    ],
)
def test_v4_any_rule_or_unreadable_rules_count_as_rules(rules):
    assert (
        reasons(flag(rollout_percentage=100, targeting_rules=rules), evaluated=True)
        == []
    )
    assert (
        reasons(flag(rollout_percentage=0, targeting_rules=rules), evaluated=True) == []
    )


def test_v4_an_inactive_flag_is_off_for_everyone_whatever_its_rules():
    item = flag(
        status=FeatureFlagStatus.INACTIVE,
        rollout_percentage=100,
        targeting_rules=DASHBOARD_CONDITION,
    )
    assert reasons(item, evaluated=True) == [OFF]


# ---------------------------------------------------------------------------
# V5: an archived flag is never stale
# ---------------------------------------------------------------------------


def test_v5_an_archived_flag_is_never_stale():
    archived = flag(status=FeatureFlagStatus.ARCHIVED, rollout_percentage=0)
    inactive = flag(status=FeatureFlagStatus.INACTIVE, rollout_percentage=0)
    assert reasons(archived, evaluated=False) == []
    # The control: the same flag, not archived, has both reasons.
    assert reasons(inactive, evaluated=False) == [NO_EVAL, OFF]


def test_v5_the_status_may_be_stored_as_its_string():
    assert reasons(flag(status="ARCHIVED"), evaluated=False) == []
    assert reasons(flag(status="INACTIVE"), evaluated=True) == [OFF]


# ---------------------------------------------------------------------------
# V6: the permanent tag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tags",
    [["permanent"], [" Permanent "], ["checkout", "PERMANENT"]],
    ids=["exact", "case and spaces", "among others"],
)
def test_v6_a_flag_tagged_permanent_is_never_stale(tags):
    item = flag(status=FeatureFlagStatus.INACTIVE, tags=tags)
    assert reasons(item, evaluated=False) == []


@pytest.mark.parametrize(
    "tags",
    [
        ["permanent-ish"],
        ["permenant"],
        ["not permanent"],
        "permanent",
        {"permanent": True},
        [1, None],
        None,
        [],
    ],
    ids=[
        "a longer word",
        "a misspelling",
        "a phrase",
        "a string, not a list",
        "an object",
        "not strings",
        "none",
        "no tags",
    ],
)
def test_v6_anything_else_is_not_the_permanent_tag_and_never_raises(tags):
    item = flag(status=FeatureFlagStatus.INACTIVE, tags=tags)
    assert reasons(item, evaluated=False) == [NO_EVAL, OFF]


# ---------------------------------------------------------------------------
# V7: unchanged is measured from updated_at, so a rename restarts the count
# ---------------------------------------------------------------------------


def test_v7_an_edit_restarts_the_count():
    renamed = flag(
        status=FeatureFlagStatus.INACTIVE,
        created_at=OLD,
        updated_at=NOW - timedelta(days=1),
    )
    assert reasons(renamed, evaluated=True) == []


# ---------------------------------------------------------------------------
# V8: an aware time gives the naive answer
# ---------------------------------------------------------------------------


def test_v8_aware_times_give_the_same_answer_as_naive_ones():
    aware_now = NOW.replace(tzinfo=timezone.utc)
    paris = timezone(timedelta(hours=1))
    item = flag(
        rollout_percentage=100,
        # The status writers store an aware time; this one is the cutoff.
        updated_at=(CUTOFF + timedelta(hours=1)).replace(tzinfo=paris),
    )
    assert reasons(item, evaluated=True, now=aware_now) == [ON]
    item.updated_at = (CUTOFF + timedelta(hours=1, seconds=1)).replace(tzinfo=paris)
    assert reasons(item, evaluated=True, now=aware_now) == []


# ---------------------------------------------------------------------------
# The reasons, the guard and the schedule exemption
# ---------------------------------------------------------------------------


def test_not_evaluated_is_reported_only_when_evaluations_were_seen():
    item = flag(rollout_percentage=50)
    assert reasons(item, evaluated=False, evaluations_seen=True) == [NO_EVAL]
    assert reasons(item, evaluated=False, evaluations_seen=False) == []
    assert reasons(item, evaluated=True, evaluations_seen=True) == []


def test_the_reasons_come_in_their_fixed_order():
    item = flag(rollout_percentage=100)
    assert reasons(item, evaluated=False) == [NO_EVAL, ON]
    item = flag(status=FeatureFlagStatus.INACTIVE)
    assert reasons(item, evaluated=False) == [NO_EVAL, OFF]
    assert stale_flags.REASONS == (NO_EVAL, ON, OFF)


def test_a_flag_with_an_active_rollout_schedule_is_never_stale():
    item = flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=0)
    assert reasons(item, evaluated=False, scheduled=True) == []
    assert reasons(item, evaluated=False, scheduled=False) == [NO_EVAL, OFF]


def test_every_flag_given_is_answered():
    items = [flag(), flag(status=FeatureFlagStatus.ARCHIVED), flag(created_at=NOW)]
    answer = stale_flags.classify(
        items,
        now=NOW,
        days=DAYS,
        evaluations_seen=True,
        evaluated_ids=set(),
        active_schedule_ids=set(),
    )
    assert answer == {items[0].id: [NO_EVAL], items[1].id: [], items[2].id: []}


def test_the_candidates_are_the_flags_whose_evaluations_matter():
    """The database is asked about these flags only."""
    keep = flag()
    items = [
        keep,
        flag(status=FeatureFlagStatus.ARCHIVED),
        flag(created_at=NOW - timedelta(days=1)),
        flag(tags=["permanent"]),
    ]
    assert stale_flags.candidate_ids(items, now=NOW, days=DAYS) == [keep.id]


# ---------------------------------------------------------------------------
# The two raw_metrics statements read the newest rows first
#
# raw_metrics is never pruned, so a statement that scans the window, or every
# row older than it, grows with the table. On 5M rows written in time order a
# plain EXISTS was planned on the flag-only index and read every older row
# first (1,250 per flag); each statement below reads an index backwards from
# the newest row and stops at the first evaluation. These pin the shape; the
# plans are in the pull request.
# ---------------------------------------------------------------------------

NEWEST_FIRST = re.compile(r"ORDER BY \S*raw_metrics\.timestamp DESC\s+LIMIT")


def _statement(call, *args):
    db = MagicMock()
    db.execute.return_value.first.return_value = None
    db.execute.return_value.__iter__.return_value = iter([])
    call(db, *args)
    (statement,) = [c.args[0] for c in db.execute.call_args_list]
    return str(statement.compile(dialect=postgresql.dialect()))


def test_the_install_guard_reads_the_timestamp_index_from_the_newest_row():
    sql = _statement(stale_flags.any_evaluation_since, CUTOFF)
    assert NEWEST_FIRST.search(sql), sql
    assert "flag_evaluation" not in sql  # a bound parameter, not inlined
    assert "metric_type" in sql


def test_each_flags_evaluations_are_read_newest_first_in_one_statement():
    sql = _statement(stale_flags.evaluated_flag_ids, [uuid.uuid4()] * 3, CUTOFF)
    assert "JOIN LATERAL" in sql, sql
    assert NEWEST_FIRST.search(sql), sql
    assert "EXISTS" not in sql


async def test_the_clock_the_list_classifies_at_is_an_aware_utc_time():
    before = datetime.now(timezone.utc)
    moment = await stale_flags.stale_clock()
    after = datetime.now(timezone.utc)
    assert moment.tzinfo is not None
    assert moment.utcoffset() == timedelta(0)
    # Between two readings of the real UTC clock, so a local-time clock on a
    # host that is not on UTC lands hours outside.
    assert before <= moment <= after
