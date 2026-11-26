"""The statements every results view reads ``events`` with, as main ran them.

A metric's events reach each reader as ``event_matching.MetricEvents``, and a
metric with no conversion window must be read exactly as before: the same
statements, not merely the same numbers. A window rule that reached the
statement of a metric without one (``AND created_at IS NOT NULL``, say)
answers the same numbers on every fixture and is visible only here.

On the fixed experiment of ``test_unfiltered_results_pinned`` (four metrics,
Bayesian and sequential on), plus an id-list segment and a second experiment
sharing its users, each view below is requested once and every statement it
runs that reads ``events`` is recorded. ``golden/results_event_reads.json``
holds, for each view, the distinct statement texts in order; they were taken
from main's code in the commit that added this file, before any reader took a
``MetricEvents``.

The statement text carries no bound values, so the texts do not depend on the
ids or the times; they do depend on how many event names a statement binds,
which the fixture fixes. This pin may change only in a pull request that says
which statement moved and why.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List

import pytest
from sqlalchemy import event

from backend.app.core.bandit_scheduler import BanditScheduler
from backend.app.core.database_config import get_schema_name
from backend.app.models.assignment import Assignment
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Variant,
)
from backend.app.models.segment import Segment, SegmentKind, SegmentMember
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.results_streaming_service import ResultsStreamingService
from backend.tests.integration.api.test_unfiltered_results_pinned import (
    ASSIGNED,
    CONTROL_ID,
    EXPERIMENT_ID,
    TREATMENT_ID,
    _build,
    _remove,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

GOLDEN = Path(__file__).parent / "golden" / "results_event_reads.json"

SEGMENT_ID = uuid.UUID("19340000-0000-4000-8000-000000000031")
OTHER_ID = uuid.UUID("19340000-0000-4000-8000-000000000041")
OTHER_A = uuid.UUID("19340000-0000-4000-8000-000000000051")
OTHER_B = uuid.UUID("19340000-0000-4000-8000-000000000052")

READS_EVENTS = re.compile(rf"\b{re.escape(get_schema_name())}\.events\b")


def _remove_extras(db_session) -> None:
    db_session.rollback()
    db_session.query(SegmentMember).filter(
        SegmentMember.segment_id == SEGMENT_ID
    ).delete(synchronize_session=False)
    db_session.query(Segment).filter(Segment.id == SEGMENT_ID).delete(
        synchronize_session=False
    )
    for model in (Assignment, Variant):
        db_session.query(model).filter(model.experiment_id == OTHER_ID).delete(
            synchronize_session=False
        )
    db_session.query(Experiment).filter(Experiment.id == OTHER_ID).delete(
        synchronize_session=False
    )
    db_session.commit()


def _build_extras(db_session, owner_id) -> None:
    db_session.add(
        Segment(
            id=SEGMENT_ID,
            name="Event reads pin",
            kind=SegmentKind.ID_LIST.value,
            owner_id=owner_id,
        )
    )
    db_session.flush()
    for user_id in ("u01", "u02", "u04", "u05", "u09"):
        db_session.add(SegmentMember(segment_id=SEGMENT_ID, member_id=user_id))
    db_session.add(
        Experiment(
            id=OTHER_ID,
            name="Event reads pin, other",
            key="event-reads-pin-other-1934",
            status=ExperimentStatus.ACTIVE,
            experiment_type=ExperimentType.A_B,
            owner_id=owner_id,
            start_date=datetime(2026, 11, 1, 0, 0, 0),
        )
    )
    db_session.flush()
    for variant_id, name, is_control in (
        (OTHER_A, "control", True),
        (OTHER_B, "treatment", False),
    ):
        db_session.add(
            Variant(
                id=variant_id,
                experiment_id=OTHER_ID,
                name=name,
                is_control=is_control,
                traffic_allocation=50,
            )
        )
    db_session.flush()
    for index, user_id in enumerate(sorted(ASSIGNED)):
        db_session.add(
            Assignment(
                experiment_id=OTHER_ID,
                variant_id=OTHER_A if index % 2 else OTHER_B,
                user_id=user_id,
            )
        )
    db_session.commit()


@pytest.fixture
def pinned_reads(db_session, admin_user) -> Iterator[None]:
    _remove_extras(db_session)
    _remove(db_session)
    _build(db_session, admin_user.id)
    _build_extras(db_session, admin_user.id)
    try:
        yield
    finally:
        _remove_extras(db_session)
        _remove(db_session)


class _SharedSession:
    """The test session, for the streaming service; its close() is a no-op."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        pass


def event_reads(client, db_session) -> Dict[str, List[str]]:
    """Each view's distinct statements that read ``events``, in order."""
    engine = db_session.get_bind()
    seen: List[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if READS_EVENTS.search(statement) and statement not in seen:
            seen.append(statement)

    results = f"/api/v1/results/{EXPERIMENT_ID}"
    experiments = f"/api/v1/experiments/{EXPERIMENT_ID}"
    views = {
        "results": lambda: client.get(results, params={"use_cache": "false"}),
        "breakdown": lambda: client.get(
            results, params={"use_cache": "false", "breakdown": "tier"}
        ),
        "daily": lambda: client.get(f"{results}/daily"),
        "sample_size": lambda: client.get(f"{results}/sample-size"),
        "sequential": lambda: client.get(f"{results}/sequential"),
        "cuped": lambda: client.get(f"{results}/cuped"),
        "bayesian": lambda: client.get(f"{results}/bayesian"),
        "segment": lambda: client.get(f"{results}/segments/{SEGMENT_ID}"),
        "experiment_daily": lambda: client.get(f"{experiments}/daily-results"),
        "segmented": lambda: client.get(f"{experiments}/segmented-results/tier"),
        "interaction": lambda: client.get(
            f"/api/v1/interactions/{EXPERIMENT_ID}/{OTHER_ID}"
        ),
        "report": lambda: client.get(
            f"/api/v1/export/reports/experiments/{EXPERIMENT_ID}"
        ),
    }
    found: Dict[str, List[str]] = {}
    # The clock is not frozen: a statement's text carries no time, and the
    # fixture's dates are all in the past.
    event.listen(engine, "before_cursor_execute", record)
    try:
        for name, request in views.items():
            seen.clear()
            response = request()
            assert response.status_code == 200, (name, response.text)
            found[name] = list(seen)
        seen.clear()
        AnalysisService(db_session).get_experiment_results_many([EXPERIMENT_ID])
        found["export_batched"] = list(seen)
        seen.clear()
        service = ResultsStreamingService(lambda: _SharedSession(db_session))
        snapshot = asyncio.run(service.get_live_snapshot(str(EXPERIMENT_ID)))
        assert snapshot.get("event") == "results_update", snapshot
        found["stream"] = list(seen)
        seen.clear()
        stats = BanditScheduler(db_session)._stats_from_postgres(
            EXPERIMENT_ID, [str(CONTROL_ID), str(TREATMENT_ID)]
        )
        assert stats is not None
        found["bandit"] = list(seen)
    finally:
        event.remove(engine, "before_cursor_execute", record)
        db_session.rollback()
    return found


def _golden() -> Dict[str, List[str]]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))["views"]


def test_every_view_reads_events_with_mains_statements(
    pinned_reads, admin_client, db_session
):
    found = event_reads(admin_client, db_session)
    golden = _golden()
    assert sorted(found) == sorted(golden)
    moved = {
        name: {
            "added": [s for s in found[name] if s not in golden[name]],
            "removed": [s for s in golden[name] if s not in found[name]],
        }
        for name in golden
        if found[name] != golden[name]
    }
    assert moved == {}, json.dumps(moved, indent=1)


def test_the_pin_covers_every_reader():
    """A guard on the golden file, so the pin cannot go vacuous: every view
    reads events, and between them they run the conversion, count, value,
    total and batched reads."""
    golden = _golden()
    assert all(golden[name] for name in golden), [n for n in golden if not golden[n]]
    every = "\n".join(s for statements in golden.values() for s in statements)
    for fragment in ("min(", "count(", "sum(", "GROUP BY", "DISTINCT"):
        assert fragment in every, fragment
