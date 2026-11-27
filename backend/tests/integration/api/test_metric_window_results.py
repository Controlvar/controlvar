"""Every result that reads a metric counts only the events within its window (#1934).

On ``metric_window_world``'s rows and a 24-hour window, each reader below is
held to an exact number: 7 converting users of 12 in control and 4 of 11 in
treatment (9 of the twenty rows' 19 assigned users, 11 of all 23), where with no window
it is 10 and 10. A reader that was handed no window, or that applied the rule
differently, answers another number on these rows; ``metric_window_world``
says which row tells which rule apart.

Each test runs with the database session in UTC and in Los Angeles, which
leaves summer time inside the window: the rule is applied to instants in
Python, so no session setting can move it.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from backend.app.core.bandit_scheduler import BanditScheduler
from backend.app.main import app
from backend.app.models.assignment import Assignment
from backend.app.models.experiment import MetricType
from backend.app.models.segment import Segment, SegmentKind, SegmentMember
from backend.app.services import event_matching
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.event_matching import (
    MetricEvents,
    count_converting_users,
    metric_events,
    windowed_users,
)
from backend.tests.integration import identity_alias_world
from backend.tests.integration import metric_window_world as mw
from backend.tests.integration.api.test_converting_users_consistency import (
    _bandit_counts,
    _bayesian_counts,
    _breakdown_conversions,
    _cumulative_counts,
    _results_counts,
    _segmented_conversions,
    _sequential_inputs,
    _streaming_counts,
)
from backend.tests.integration.conftest import make_client_for_user

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

#: Worked out from the rows' ``counts`` flags, and pinned here as numbers.
EXPECTED = {
    "control": {"n": 12, "converted": 7},
    "treatment": {"n": 11, "converted": 4},
}
EVER = {"control": {"n": 12, "converted": 10}, "treatment": {"n": 11, "converted": 10}}
TWENTY_EXPECTED = {
    "control": {"n": 10, "converted": 5},
    "treatment": {"n": 9, "converted": 4},
}

ZONES = ("UTC", "America/Los_Angeles")
EVER_METRIC = mw.metric("Purchase ever", hours=None)


@pytest.fixture(params=ZONES)
def zoned(request, db_session) -> Iterator[Session]:
    """A session whose connections run in ``TimeZone`` = the parameter."""
    engine = create_engine(
        db_session.get_bind().url,
        poolclass=NullPool,
        connect_args={"options": f"-c TimeZone={request.param}"},
    )

    @sa_event.listens_for(engine, "connect")
    def _schema(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("SET search_path TO test_experimentation")
        cursor.close()

    session = sessionmaker(
        bind=engine, autocommit=False, autoflush=False, expire_on_commit=False
    )()
    assert session.execute(text("SHOW TimeZone")).scalar() == request.param
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture
def client(zoned, admin_user) -> Iterator[TestClient]:
    """The admin's client, its requests on ``zoned``'s engine."""
    yield make_client_for_user(zoned, admin_user)
    app.dependency_overrides.clear()


@pytest.fixture
def worlds(zoned) -> Iterator[list]:
    """Every experiment a test builds, removed afterwards."""
    made: list = []
    yield made
    mw.remove(zoned, [w.experiment.id for w in made])


def _build(zoned, admin_user, worlds, **fields) -> mw.World:
    world = mw.build(zoned, admin_user.id, **fields)
    worlds.append(world)
    return world


def _get(client, path, **params) -> Dict:
    response = client.get(path, params={"use_cache": "false", **params})
    assert response.status_code == 200, response.text
    return response.json()


def _metric(results, name) -> Dict:
    (found,) = [m for m in results["metrics"] if m["metric_name"] == name]
    return found


def _by_variant(metric_result, *fields) -> Dict:
    return {
        v["variant_name"]: tuple(v[f] for f in fields)
        for v in metric_result["variants"]
    }


@pytest.fixture
def segment_of(zoned, admin_user) -> Iterator:
    """``segment_of(user_ids)``: an id-list segment holding them."""
    made = []

    def make(user_ids) -> Segment:
        segment = Segment(
            name=f"Window members {uuid.uuid4().hex[:8]}",
            kind=SegmentKind.ID_LIST.value,
            owner_id=admin_user.id,
        )
        zoned.add(segment)
        zoned.flush()
        for user_id in user_ids:
            zoned.add(SegmentMember(segment_id=segment.id, member_id=user_id))
        zoned.commit()
        made.append(segment.id)
        return segment

    yield make
    zoned.rollback()
    zoned.query(SegmentMember).filter(SegmentMember.segment_id.in_(made)).delete(
        synchronize_session=False
    )
    zoned.query(Segment).filter(Segment.id.in_(made)).delete(synchronize_session=False)
    zoned.commit()


# ---------------------------------------------------------------------------
# The rule, on the rows
# ---------------------------------------------------------------------------


def test_the_rows_say_what_the_rule_counts():
    """The flags the other tests are worked out from, as lists."""
    assert mw.converters(mw.TWENTY_ROWS) == [
        "u01", "u03", "u05", "u06", "u07", "u08", "u09", "u12", "u16",
    ]  # fmt: skip
    assert mw.converters(mw.ADDED_ROWS) == ["u21", "u23"]
    assert len(mw.converters(mw.TWENTY_ROWS, windowed=False)) == 16
    assert len(mw.converters(mw.ALL_ROWS, windowed=False)) == 20
    assert mw.per_variant(mw.ALL_ROWS) == EXPECTED
    assert mw.per_variant(mw.ALL_ROWS, windowed=False) == EVER
    assert mw.per_variant(mw.TWENTY_ROWS) == TWENTY_EXPECTED


@pytest.mark.parametrize("rows", ["twenty", "all"])
def test_the_window_counts_exactly_these_users(zoned, admin_user, worlds, rows):
    """9 of 9 on the twenty rows, 11 with the four added: each converting user has
    exactly one event inside the window."""
    chosen = mw.TWENTY_ROWS if rows == "twenty" else mw.ALL_ROWS
    world = _build(zoned, admin_user, worlds, rows=chosen, metrics=[mw.IN_A_DAY])
    events = metric_events(world.experiment.metric_definitions[0])
    assert events == MetricEvents("purchase", 24)

    found = windowed_users(zoned, world.experiment.id, None, events)
    got = world.labels(user for _, user in found)
    want = mw.converters(chosen)
    assert got == want, {
        "counted, not inside": sorted(set(got) - set(want)),
        "inside, not counted": sorted(set(want) - set(got)),
    }
    assert sum(row.events for row in found.values()) == len(mw.converters(chosen))
    assert len(found) == {"twenty": 9, "all": 11}[rows]

    for name, variant in (("control", world.control), ("treatment", world.treatment)):
        assert (
            count_converting_users(zoned, world.experiment.id, variant.id, events)
            == mw.per_variant(chosen)[name]["converted"]
        )
    # The same users with no window: 16 and 20.
    ever = MetricEvents("purchase", None)
    assert (
        sum(
            count_converting_users(zoned, world.experiment.id, v.id, ever)
            for v in (world.control, world.treatment)
        )
        == {"twenty": 16, "all": 20}[rows]
    )


def test_each_user_s_first_inside_event_is_the_one_inside(zoned, admin_user, worlds):
    """u08 sent one event before the start and one inside; u09 one inside and
    one after the end: the event read is the inside one, and the value."""
    world = _build(
        zoned,
        admin_user,
        worlds,
        metrics=[mw.metric("Revenue in a day", metric_type=MetricType.REVENUE)],
    )
    events = metric_events(world.experiment.metric_definitions[0])
    found = windowed_users(zoned, world.experiment.id, None, events)
    by_label = {world.labels([user])[0]: row for (_, user), row in found.items()}
    assert by_label["u08"].first == "2026-11-01T07:00:00+00:00"
    assert by_label["u09"].first == "2026-11-01T08:00:00+00:00"
    assert {label: row.value for label, row in by_label.items()} == dict.fromkeys(
        mw.converters(mw.ALL_ROWS), 10.0
    )


# ---------------------------------------------------------------------------
# Every reader, one number
# ---------------------------------------------------------------------------


def test_every_view_counts_the_window(zoned, client, admin_user, worlds, segment_of):
    world = _build(zoned, admin_user, worlds)
    exp = world.experiment
    base = f"/api/v1/results/{exp.id}"

    results = _get(client, base, breakdown="country")
    assert _results_counts(results) == EXPECTED
    assert results["summary"]["total_conversions"] == 11
    assert _bayesian_counts(results["bayesian_results"]) == EXPECTED
    assert _breakdown_conversions(results) == EXPECTED

    alias = _get(client, f"/api/v1/experiments/{exp.id}/results")
    assert _results_counts(alias) == EXPECTED

    assert _bayesian_counts(_get(client, f"{base}/bayesian")) == EXPECTED
    assert _sequential_inputs(client, exp) == EXPECTED
    assert _cumulative_counts(_get(client, f"{base}/daily")) == EXPECTED

    sample_size = _get(client, f"{base}/sample-size")
    assert (sample_size["baseline_users"], sample_size["baseline_rate"]) == (
        12,
        pytest.approx(7 / 12),
    )

    (cuped,) = _get(client, f"{base}/cuped")["metrics"]
    assert cuped["method"] == "none"
    assert (cuped["adjusted_control_mean"], cuped["adjusted_treatment_mean"]) == (
        pytest.approx(7 / 12),
        pytest.approx(4 / 11),
    )

    daily = _get(client, f"/api/v1/experiments/{exp.id}/daily-results")
    summed = {"control": 0, "treatment": 0}
    for day in daily:
        (metric,) = day["metrics"]
        for v in metric["variants"]:
            summed[v["variant_name"]] += v["conversions"]
    assert summed == {"control": 7, "treatment": 4}

    assert _segmented_conversions(zoned, exp) == EXPECTED
    segmented = _get(client, f"/api/v1/experiments/{exp.id}/segmented-results/country")
    totals = {"control": 0, "treatment": 0}
    for segment in segmented["segments"]:
        for metric in segment["metrics"]:
            for v in metric["variants"]:
                totals[v["variant_name"]] += v["conversions"]
    assert totals == {"control": 7, "treatment": 4}

    segment = segment_of(world.user.values())
    member_results = _get(client, f"{base}/segments/{segment.id}")
    assert _results_counts(member_results) == EXPECTED
    assert member_results["summary"]["total_conversions"] == 11

    export = client.get(
        "/api/v1/export/variants",
        params={
            "format": "json",
            "start_date": (exp.created_at - timedelta(seconds=1)).isoformat(),
        },
    )
    assert export.status_code == 200, export.text
    exported = {
        r["variant_name"]: {"n": r["assignments"], "converted": r["conversions"]}
        for r in export.json()
        if r["experiment_id"] == str(exp.id)
    }
    assert exported == EXPECTED
    report = _get(client, f"/api/v1/export/reports/experiments/{exp.id}")
    assert {
        r["variant_name"]: {"n": r["assignments"], "converted": r["conversions"]}
        for r in report["variants"]
    } == EXPECTED

    batched = AnalysisService(zoned).get_experiment_results_many([exp.id])
    assert _results_counts(batched[str(exp.id)]) == EXPECTED

    assert _streaming_counts(zoned, exp) == EXPECTED
    assert _bandit_counts(zoned, exp) == EXPECTED


def test_every_view_counts_the_window_for_a_revenue_primary(
    zoned, client, admin_user, worlds
):
    """The same rows with a revenue primary (``mw.REVENUE_IN_A_DAY``).
    ``/results`` reads its values per user inside the window
    (``mw.REVENUE_MEANS``).  Every other view here still reads the share of
    users who sent the event, so it counts the same 7 of 12 and 4 of 11 as for
    a conversion primary; a view that moves to the mean changes its own line
    to ``mw.REVENUE_MEANS``."""
    world = _build(zoned, admin_user, worlds, metrics=[mw.REVENUE_IN_A_DAY])
    exp = world.experiment
    base = f"/api/v1/results/{exp.id}"
    means = {
        name: (pytest.approx(mean, rel=1e-12),)
        for name, mean in mw.REVENUE_MEANS.items()
    }

    results = _get(client, base, breakdown="country")
    assert _by_variant(_metric(results, "Revenue in a day"), "mean") == means
    assert _results_counts(results) == EXPECTED
    assert _bayesian_counts(results["bayesian_results"]) == EXPECTED
    assert _breakdown_conversions(results) == EXPECTED
    assert _bayesian_counts(_get(client, f"{base}/bayesian")) == EXPECTED
    assert _sequential_inputs(client, exp) == EXPECTED
    assert _cumulative_counts(_get(client, f"{base}/daily")) == EXPECTED
    sample_size = _get(client, f"{base}/sample-size")
    assert (sample_size["baseline_users"], sample_size["baseline_rate"]) == (
        12,
        pytest.approx(7 / 12),
    )
    assert _streaming_counts(zoned, exp) == EXPECTED

    batched = AnalysisService(zoned).get_experiment_results_many([exp.id])
    assert _by_variant(_metric(batched[str(exp.id)], "Revenue in a day"), "mean") == (
        means
    )


def test_the_balanced_groups_count_the_window(zoned, client, admin_user, worlds):
    world = _build(zoned, admin_user, worlds, balanced=True)
    body = _get(client, f"/api/v1/results/{world.experiment.id}/balanced-groups")
    totals = {
        "control": {"n": 0, "converted": 0},
        "treatment": {"n": 0, "converted": 0},
    }
    for group in body["groups"]["segments"]:
        for v in group["variants"]:
            totals[v["variant_name"]]["n"] += v["sample_size"]
            totals[v["variant_name"]]["converted"] += v["conversions"]
    assert totals == EXPECTED


def test_the_interaction_counts_the_window(zoned, client, admin_user, worlds):
    """The same 23 users in a second experiment, split by their country: the
    first experiment's converting users among them are its own 7 and 4."""
    first = _build(zoned, admin_user, worlds)
    users = first.user["u01"].split("-")[1]
    swapped = tuple(
        dataclasses.replace(
            row, variant="control" if mw.country_of(row.label) == "US" else "treatment"
        )
        if row.variant is not None
        else row
        for row in mw.ALL_ROWS
    )
    second = _build(zoned, admin_user, worlds, rows=swapped, users=users)
    body = _get(
        client, f"/api/v1/interactions/{first.experiment.id}/{second.experiment.id}"
    )
    assert body["shared_users"] == 23
    (row,) = [
        r
        for r in body["interaction_results"]
        if r["experiment_id"] == str(first.experiment.id)
    ]
    converted = {
        "control": sum(arm["converted_control"] for arm in row["arms"]),
        "treatment": sum(arm["converted_treatment"] for arm in row["arms"]),
    }
    assert converted == {"control": 7, "treatment": 4}


# ---------------------------------------------------------------------------
# Means, two metrics on one event, the daily series, the total
# ---------------------------------------------------------------------------


def test_a_mean_drops_the_events_outside_not_the_users(
    zoned, client, admin_user, worlds, segment_of
):
    """A count metric counts each user's events inside the window (one each),
    and a revenue metric adds their values: u08's 100 before the start and
    u09's 1,000 after the end are not added, and every assigned user is in
    the mean's denominator."""
    world = _build(
        zoned,
        admin_user,
        worlds,
        metrics=[
            mw.metric(
                "Purchases in a day", metric_type=MetricType.COUNT, is_primary=True
            ),
            mw.metric("Revenue in a day", metric_type=MetricType.REVENUE),
        ],
    )
    exp = world.experiment
    single = _get(client, f"/api/v1/results/{exp.id}")
    segment = segment_of(world.user.values())
    members = _get(client, f"/api/v1/results/{exp.id}/segments/{segment.id}")
    batched = AnalysisService(zoned).get_experiment_results_many([exp.id])[str(exp.id)]
    for results in (single, members, batched):
        count = _by_variant(
            _metric(results, "Purchases in a day"), "sample_size", "mean"
        )
        revenue = _by_variant(
            _metric(results, "Revenue in a day"), "sample_size", "mean"
        )
        assert count == {
            "control": (12, pytest.approx(7 / 12)),
            "treatment": (11, pytest.approx(4 / 11)),
        }
        assert revenue == {
            "control": (12, pytest.approx(70 / 12)),
            "treatment": (11, pytest.approx(40 / 11)),
        }


def test_two_metrics_on_one_event_keep_their_own_windows(
    zoned, client, admin_user, worlds, segment_of
):
    """One metric with 24 hours and one with none, on the same event: 7 and 4
    beside 10 and 10 in one answer, and a total of 20, in every view that
    lists metrics, the batch and the CSV (whose conversions are the primary's)."""
    world = _build(zoned, admin_user, worlds, metrics=[mw.IN_A_DAY, EVER_METRIC])
    exp = world.experiment
    base = f"/api/v1/results/{exp.id}"
    segment = segment_of(world.user.values())
    views = {
        "results": _get(client, base),
        "members": _get(client, f"{base}/segments/{segment.id}"),
        "batch": AnalysisService(zoned).get_experiment_results_many([exp.id])[
            str(exp.id)
        ],
    }
    for name, results in views.items():
        day = _metric(results, "Purchase in a day")
        ever = _metric(results, "Purchase ever")
        assert _by_variant(day, "conversions") == {
            "control": (7,),
            "treatment": (4,),
        }, name
        assert _by_variant(ever, "conversions") == {
            "control": (10,),
            "treatment": (10,),
        }, name
        assert (day["conversion_window_hours"], ever["conversion_window_hours"]) == (
            24,
            None,
        )
        assert results["summary"]["total_conversions"] == 20, name

    breakdown = _get(client, base, breakdown="country")["breakdown"]
    assert (
        sum(v["conversions"] for s in breakdown["segments"] for v in s["variants"])
        == 11
    )  # the breakdown is of the primary metric
    segmented = AnalysisService(zoned).get_segmented_results(
        experiment_id=exp.id, segment_by="country"
    )
    per_metric: Dict[str, int] = {}
    for s in segmented["segments"]:
        for metric in s["metrics"]:
            per_metric[metric["metric_name"]] = per_metric.get(
                metric["metric_name"], 0
            ) + sum(v["conversions"] for v in metric["variants"])
    assert per_metric == {"Purchase in a day": 11, "Purchase ever": 20}

    # The CSV's conversions are the primary metric's: the windowed one here,
    # and in a second experiment the one with no window.
    other = _build(
        zoned,
        admin_user,
        worlds,
        metrics=[
            mw.metric("Purchase in a day", hours=24),
            mw.metric("Purchase ever", hours=None, is_primary=True),
        ],
    )
    csv = client.get(
        "/api/v1/export/variants",
        params={
            "format": "csv",
            "start_date": (exp.created_at - timedelta(seconds=1)).isoformat(),
        },
    )
    assert csv.status_code == 200, csv.text
    lines = csv.text.splitlines()
    header = lines[0].split(",")
    rows = [dict(zip(header, line.split(","))) for line in lines[1:]]
    found = {
        (r["experiment_id"], r["variant_name"]): r["conversions"]
        for r in rows
        if r["experiment_id"] in (str(exp.id), str(other.experiment.id))
    }
    assert found == {
        (str(exp.id), "control"): "7",
        (str(exp.id), "treatment"): "4",
        (str(other.experiment.id), "control"): "10",
        (str(other.experiment.id), "treatment"): "10",
    }


def test_the_daily_series_counts_each_user_on_the_day_of_the_inside_event(
    zoned, client, admin_user, worlds
):
    """u08's day is that of its inside event (1 November), not of its first
    event; u03's, u16's and u23's inside events are on 2 November (UTC)."""
    world = _build(zoned, admin_user, worlds)
    daily = _get(client, f"/api/v1/results/{world.experiment.id}/daily")
    new = {
        s["variant_name"]: {
            v["date"]: v["conversions"] for v in s["values"] if v["conversions"]
        }
        for s in daily["series"]
    }
    assert new == {
        "control": {"2026-11-01": 5, "2026-11-02": 2},
        "treatment": {"2026-11-01": 3, "2026-11-02": 1},
    }


def test_the_total_is_the_union_of_the_windowed_converters(
    zoned, client, admin_user, worlds, segment_of
):
    """Every metric has a window: the total is the 11 users with an event
    inside one, never the legacy ``conversion`` events (u17 sent one, under
    another name)."""
    world = _build(
        zoned,
        admin_user,
        worlds,
        metrics=[
            mw.IN_A_DAY,
            mw.metric("Purchases in an hour", hours=1, metric_type=MetricType.COUNT),
        ],
    )
    exp = world.experiment
    segment = segment_of(world.user.values())
    for results in (
        _get(client, f"/api/v1/results/{exp.id}"),
        _get(client, f"/api/v1/results/{exp.id}/segments/{segment.id}"),
        AnalysisService(zoned).get_experiment_results_many([exp.id])[str(exp.id)],
    ):
        assert results["summary"]["total_conversions"] == 11
        hour = _metric(results, "Purchases in an hour")
        # Inside an hour: u01, u05, u06, u07 and u21 (u08's is at 2 h).
        assert _by_variant(hour, "sample_size", "mean") == {
            "control": (12, pytest.approx(4 / 12)),
            "treatment": (11, pytest.approx(1 / 11)),
        }


# ---------------------------------------------------------------------------
# An unreadable time, the bandit, the stream, a linked id
# ---------------------------------------------------------------------------


def test_an_unreadable_time_is_not_counted_and_is_logged_once(
    zoned, client, admin_user, worlds, caplog
):
    """With a window, an event whose stored time cannot be read is not
    counted, the answer is 200, and one line says how many there were; with
    no window it counts, as it always has."""
    rows = (
        mw.Row("u01", "control", (mw.Sent(timedelta(0)),), True, "inside"),
        mw.Row(
            "u03",
            "control",
            (mw.Sent("not a time", raw=True), mw.Sent("2026-13-01T00:00:00", raw=True)),
            False,
            "two unreadable times",
        ),
        mw.Row("u02", "treatment", (mw.Sent(mw.HOUR),), True, "inside"),
    )
    world = _build(
        zoned, admin_user, worlds, rows=rows, metrics=[mw.IN_A_DAY, EVER_METRIC]
    )
    exp = world.experiment
    results = _get(client, f"/api/v1/results/{exp.id}")
    assert _by_variant(_metric(results, "Purchase in a day"), "conversions") == {
        "control": (1,),
        "treatment": (1,),
    }
    assert _by_variant(_metric(results, "Purchase ever"), "conversions") == {
        "control": (2,),
        "treatment": (1,),
    }

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=event_matching.__name__):
        found = windowed_users(
            zoned, exp.id, None, MetricEvents("purchase", mw.WINDOW_HOURS)
        )
    assert world.labels(user for _, user in found) == ["u01", "u02"]
    lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == event_matching.__name__ and "could not be read" in r.getMessage()
    ]
    # Each read logs how many it could not read, once.
    assert lines == [
        f"conversion window: 2 event(s) of experiment {exp.id} not "
        "counted: their created_at could not be read"
    ]


def test_the_bandit_reads_the_window_from_postgres_alone(zoned, admin_user, worlds):
    """Successes 7 and 4; the counter service is never asked while PostgreSQL
    has pulls, and a reader that raised would make the stats None."""
    world = _build(zoned, admin_user, worlds)
    exp = world.experiment
    assert BanditScheduler._primary_metric_events(exp) == MetricEvents("purchase", 24)
    assert _bandit_counts(zoned, exp) == EXPECTED
    counters = MagicMock(name="counter service")
    with patch(
        "backend.app.core.bandit_scheduler.realtime_counter_service",
        return_value=counters,
    ) as provider:
        stats = BanditScheduler(zoned).get_variant_stats_from_counters(
            exp.id, [str(world.control.id), str(world.treatment.id)], exp
        )
    assert provider.call_count == 0 and counters.call_count == 0
    assert {
        name: (stats[str(v.id)].pulls, stats[str(v.id)].successes)
        for name, v in (("control", world.control), ("treatment", world.treatment))
    } == {"control": (12, 7), "treatment": (11, 4)}


@pytest.mark.parametrize("rows, total", [("twenty", (19, 9)), ("all", (23, 11))])
def test_the_live_stream_counts_the_window(zoned, admin_user, worlds, rows, total):
    chosen = mw.TWENTY_ROWS if rows == "twenty" else mw.ALL_ROWS
    world = _build(zoned, admin_user, worlds, rows=chosen)
    streamed = _streaming_counts(zoned, world.experiment)
    assert streamed == mw.per_variant(chosen)
    assert (
        sum(v["n"] for v in streamed.values()),
        sum(v["converted"] for v in streamed.values()),
    ) == total


def test_a_linked_id_counts_from_the_holder_s_earliest_row(
    zoned, client, admin_user, worlds
):
    """a (control, assigned at S) and b (treatment, at S + 20 h) are linked;
    b's events, sent after the link, are stored under a, the holder. The one
    dated S + 1 h is inside a's window; the one at S + 25 h is not, though it
    is inside a window counted from b's row."""
    world = _build(zoned, admin_user, worlds, rows=())
    exp = world.experiment
    suffix = uuid.uuid4().hex[:8]
    first, second = f"mw-link-{suffix}-a", f"mw-link-{suffix}-b"
    for user_id, variant, at in (
        (first, world.control, mw.S),
        (second, world.treatment, mw.S + 20 * mw.HOUR),
    ):
        zoned.add(
            Assignment(
                experiment_id=exp.id,
                variant_id=variant.id,
                user_id=user_id,
                created_at=at,
                updated_at=at,
            )
        )
    zoned.commit()
    ids = identity_alias_world.World(zoned, None)
    try:
        assert ids.link(first, second)
        for hours in (1, 25):
            response = client.post(
                "/api/v1/tracking/track",
                json={
                    "event_type": "purchase",
                    "event_name": "purchase",
                    "user_id": second,
                    "experiment_key": exp.key,
                    "timestamp": (mw.S + hours * mw.HOUR)
                    .replace(tzinfo=timezone.utc)
                    .isoformat(),
                },
            )
            assert response.status_code == 200, response.text
        stored = ids.events(exp, [first, second], "purchase")
        assert [(e.user_id, str(e.variant_id)) for e in stored] == [
            (first, str(world.control.id))
        ] * 2
        found = windowed_users(
            zoned, exp.id, None, metric_events(exp.metric_definitions[0])
        )
        assert sorted(found) == [(str(world.control.id), first)]
        results = _get(client, f"/api/v1/results/{exp.id}")
        assert _results_counts(results) == {
            "control": {"n": 1, "converted": 1},
            "treatment": {"n": 1, "converted": 0},
        }
    finally:
        ids.close()


def test_a_window_is_the_only_thing_that_moves_a_number(zoned, admin_user, worlds):
    """The rows with no window give today's 10 and 10, on every reader the
    windowed experiment answered 7 and 4 on above."""
    world = _build(
        zoned,
        admin_user,
        worlds,
        metrics=[mw.metric("Purchase ever", hours=None, is_primary=True)],
    )
    exp = world.experiment
    assert metric_events(exp.metric_definitions[0]) == MetricEvents("purchase", None)
    results = AnalysisService(zoned).get_experiment_results(exp.id)
    assert _results_counts(results) == EVER
    assert results["summary"]["total_conversions"] == 20
    assert _streaming_counts(zoned, exp) == EVER
    assert _bandit_counts(zoned, exp) == EVER
    (metric,) = results["metrics"]
    assert metric["conversion_window_hours"] is None
    assert {v["open_window_users"] for v in metric["variants"]} == {None}
