"""A metric's conversion window: ``/results`` against the warehouse analysis (#1934).

The same rows (``backend/tests/integration/metric_window_world.py``'s first
twenty users and a 24-hour window) are counted twice: by ``/results`` on
PostgreSQL, and by the warehouse analysis's own statement
(``build_metric_query``) on DuckDB, over tables made from those rows: each
assigned user's exposures (the first is the assignment) and each user's own
events of the metric, at their own times. A warehouse table records no
variant, so a user's own events are the ones tagged with the user's variant,
or with none; an event tagged with the other variant is the tag case below.

The two rules differ in three ways, and each has a case here:

* ``/results`` starts a user's window at the assignment's whole second, the
  warehouse at the exposure itself: u07, assigned at .7 s with an event at
  .2 s, counts only in ``/results``;
* the warehouse also stops at the analysis window's end, which defaults to the
  experiment's end date: u03 and u16, inside their windows but after the end
  date, count there only with a later end;
* ``/results`` counts an event only for the variant it is tagged with, which
  a warehouse table does not record: u18's event, tagged with the other
  variant, counts only in the warehouse.

So ``/results`` counts 9, the warehouse 8 with a far end and 6 at the end
date, and without u07 both count 8.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

import pytest

from backend.app.models.user import User, UserRole
from backend.app.services.analysis_service import AnalysisService
from backend.tests.integration import metric_window_world as mw
from backend.tests.integration.conftest import HASHED_PASSWORD
from modules.backend.app.services.warehouse_query_builder import (
    AnalysisWindow,
    AssignmentMapping,
    MetricMapping,
    build_metric_query,
)
from modules.backend.app.services.warehouse_sufficient_stats import parse_metric_rows
from modules.backend.tests.unit.warehouse.duckdb_adapter import (
    DUCKDB_SQL,
    DuckDBWarehouse,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

UTC = timezone.utc
KEY = "conversion-window-parity"
START = (mw.S - mw.DAY).replace(tzinfo=UTC)
FAR_END = (mw.S + 30 * mw.DAY).replace(tzinfo=UTC)
AT_THE_END_DATE = mw.END_DATE.replace(tzinfo=UTC)
ASSIGNMENT = AssignmentMapping(
    ("main", "exposures"), "user_id", "experiment_key", "variant", "exposed_at"
)
METRIC = MetricMapping(
    ("main", "events"),
    "user_id",
    "event_at",
    "proportion",
    conversion_window_hours=mw.WINDOW_HOURS,
)


def _instant(row: mw.Row, sent: mw.Sent) -> datetime:
    if isinstance(sent.at, str):
        return datetime.fromisoformat(sent.at).astimezone(UTC)
    return (mw.S + sent.at).replace(tzinfo=UTC)


def warehouse_count(
    rows: Iterable[mw.Row], end: datetime, other_tags: bool = False
) -> int:
    """Converting units of the warehouse statement on DuckDB. With
    ``other_tags`` the events table also holds the events tagged with another
    variant than the user's."""
    rows = list(rows)
    wh = DuckDBWarehouse()
    wh.create(
        "exposures",
        [
            ("user_id", "VARCHAR"),
            ("experiment_key", "VARCHAR"),
            ("variant", "VARCHAR"),
            ("exposed_at", "TIMESTAMPTZ"),
        ],
        [
            (
                row.label,
                KEY,
                row.variant,
                (mw.S + row.assigned + at).replace(tzinfo=UTC),
            )
            for row in rows
            if row.variant is not None
            for at in (timedelta(0),) + row.exposures
        ],
    )
    wh.create(
        "events",
        [("user_id", "VARCHAR"), ("event_at", "TIMESTAMPTZ")],
        [
            (row.label, _instant(row, sent))
            for row in rows
            for sent in row.sent
            if sent.event_name == "purchase"
            and sent.event_type == "purchase"
            and (other_tags or sent.tag in (None, row.variant))
        ],
    )
    stats = parse_metric_rows(
        wh.fetch(
            build_metric_query(
                DUCKDB_SQL, ASSIGNMENT, METRIC, KEY, AnalysisWindow(START, end)
            )
        )
    )
    return sum(v.n_converted for v in stats.variants)


@pytest.fixture
def owner(db_session) -> Iterable[User]:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"window_parity_{suffix}",
        email=f"window_parity_{suffix}@int.test",
        hashed_password=HASHED_PASSWORD,
        is_active=True,
        is_superuser=True,
        role=UserRole.ADMIN,
    )
    db_session.add(user)
    db_session.commit()
    made = []
    yield user, made
    mw.remove(db_session, made)
    db_session.query(User).filter(User.id == user.id).delete()
    db_session.commit()


def results_count(db_session, owner, rows) -> int:
    """Converting users of ``/results`` on PostgreSQL."""
    user, made = owner
    world = mw.build(db_session, user.id, rows=tuple(rows), end_date=mw.END_DATE)
    made.append(world.experiment.id)
    results = AnalysisService(db_session).get_experiment_results(
        world.experiment.id, include_bayesian=False
    )
    (metric,) = results["metrics"]
    return sum(v["conversions"] for v in metric["variants"])


def _only(*labels: str, rows=mw.TWENTY_ROWS) -> tuple:
    return tuple(row for row in rows if row.label in labels)


def _without(*labels: str, rows=mw.TWENTY_ROWS) -> tuple:
    return tuple(row for row in rows if row.label not in labels)


def test_results_9_warehouse_8_and_6(db_session, owner):
    assert results_count(db_session, owner, mw.TWENTY_ROWS) == 9
    assert warehouse_count(mw.TWENTY_ROWS, FAR_END) == 8
    assert warehouse_count(mw.TWENTY_ROWS, AT_THE_END_DATE) == 6


def test_without_u07_both_count_8(db_session, owner):
    rows = _without("u07")
    assert results_count(db_session, owner, rows) == 8
    assert warehouse_count(rows, FAR_END) == 8


@pytest.mark.parametrize(
    "case, labels, results, far, at_the_end",
    [
        # The start: /results floors the assignment to its second.
        ("floor", ("u07",), 1, 0, 0),
        ("floor, added rows", ("u21",), 1, 0, 0),
        # The end: the warehouse stops at the analysis window's end.
        ("end", ("u03", "u16"), 2, 2, 0),
        # The tag: a warehouse table has none, so u18's event counts there.
        ("tag", ("u18",), 0, 1, 1),
        # Both read the event's own time, not when it was stored.
        ("own time", ("u12", "u13"), 1, 1, 1),
    ],
)
def test_each_stated_difference(
    db_session, owner, case, labels, results, far, at_the_end
):
    rows = _only(*labels, rows=mw.ALL_ROWS)
    assert len(rows) == len(labels), case
    assert results_count(db_session, owner, rows) == results, case
    assert warehouse_count(rows, FAR_END, other_tags=True) == far, case
    assert warehouse_count(rows, AT_THE_END_DATE, other_tags=True) == at_the_end, case


def test_the_tag_is_the_whole_difference_of_the_other_tags(db_session, owner):
    """With every event in the warehouse's table, u18's too, it counts one
    more user than with each user's own: 9 and 7."""
    assert warehouse_count(mw.TWENTY_ROWS, FAR_END, other_tags=True) == 9
    assert warehouse_count(mw.TWENTY_ROWS, AT_THE_END_DATE, other_tags=True) == 7
