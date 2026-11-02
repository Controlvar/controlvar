# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""``tests/scale/preload.sql`` against the bootstrapped test schema (#1608).

The scale harness pipes this file, then one call, to psql inside its own
stack's postgres container (``scripts/scale_stack.py fixtures``). This test
drives the same bytes through psycopg2, with no psql anywhere, at small
counts, then reads the rows back through the models. So a column the
preload writes that a model renames, or a psql-only line in the file (a
``\\set``, a ``:variable``), fails here before it fails on a runner.

It also pins the rule the harness relies on to check a returning user's
assignment without asking the server: user i is ``<prefix><i, zero-padded>``
and gets variant ``i mod 2`` of scale_ab's two variants in byte order of
their names. The variants here are named so that byte order and a
locale's order disagree ("Treatment" before "control").
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2
import pytest

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event, normalize_event_timestamp
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import RawMetric

pytestmark = pytest.mark.integration

PRELOAD = Path(__file__).resolve().parents[3] / "tests" / "scale" / "preload.sql"
SCHEMA = "test_experimentation"
PREFIX = "scale-user-"
WIDTH = 7


def _preload(engine, users: int, events: int, raw_metrics: int) -> str:
    """The file's bytes, unchanged, then the call: one session, as psql runs."""
    conn = engine.raw_connection()
    try:
        cur = conn.cursor()
        cur.execute(PRELOAD.read_bytes())
        cur.execute(
            "SELECT pg_temp.scale_preload(%s, %s, %s, %s, %s, %s)",
            (SCHEMA, users, events, raw_metrics, PREFIX, WIDTH),
        )
        (answer,) = cur.fetchone()
        conn.commit()
        return answer
    finally:
        conn.close()


def _user(index: int) -> str:
    return f"{PREFIX}{index:0{WIDTH}d}"


def _clean(db) -> None:
    for model in (Assignment, Event, RawMetric):
        db.query(model).filter(model.user_id.like(f"{PREFIX}%")).delete(
            synchronize_session=False
        )
    db.query(FeatureFlag).filter(FeatureFlag.key.like("scale_preload_test_%")).delete(
        synchronize_session=False
    )
    db.query(FeatureFlag).filter(FeatureFlag.key.like("scale\\_flag\\_%")).delete(
        synchronize_session=False
    )
    db.query(Experiment).filter(Experiment.key == "scale_ab").delete(
        synchronize_session=False
    )
    db.commit()


@pytest.fixture
def scale_ab(db_session):
    """scale_ab with two variants, three scale flags and one other flag."""
    _clean(db_session)
    experiment = Experiment(
        name="scale_ab", key="scale_ab", status=ExperimentStatus.ACTIVE
    )
    variants = [
        Variant(name=name, experiment=experiment, traffic_allocation=50)
        for name in ("control", "Treatment")
    ]
    flags = [
        FeatureFlag(
            key=f"scale_flag_{i:03d}",
            name=f"scale_flag_{i:03d}",
            status=FeatureFlagStatus.ACTIVE,
            rollout_percentage=50,
        )
        for i in range(3)
    ]
    other = FeatureFlag(
        key=f"scale_preload_test_{uuid.uuid4().hex[:8]}",
        name="not a scale flag",
        status=FeatureFlagStatus.ACTIVE,
    )
    db_session.add_all([experiment, *variants, *flags, other])
    db_session.commit()
    yield {
        "experiment": experiment.id,
        "variants": [v.id for v in sorted(variants, key=lambda v: v.name)],
        "flags": {f.id for f in flags},
    }
    db_session.rollback()
    _clean(db_session)


def test_the_preload_writes_what_the_harness_expects(test_db, db_session, scale_ab):
    before = datetime.utcnow().replace(microsecond=0)
    answer = _preload(test_db, users=10, events=25, raw_metrics=30)
    assert answer == f"loaded 10 users, 25 events and 30 raw_metrics rows into {SCHEMA}"

    assignments = {
        a.user_id: a.variant_id
        for a in db_session.query(Assignment).filter(
            Assignment.experiment_id == scale_ab["experiment"]
        )
    }
    # The rule: user i gets the variant at (i mod 2) in byte order of names.
    assert assignments == {_user(i): scale_ab["variants"][i % 2] for i in range(10)}, (
        "a returning user's variant is not the one the preload's rule gives"
    )

    events = db_session.query(Event).filter(Event.user_id.like(f"{PREFIX}%")).all()
    assert len(events) == 25
    for event in events:
        assert event.event_type == event.event_name == "scale_conversion"
        assert event.user_id in assignments
        assert event.experiment_id == scale_ab["experiment"]
        assert event.variant_id == assignments[event.user_id]
        # The string column in the one format the server writes and compares.
        assert event.created_at == normalize_event_timestamp(event.updated_at)
        assert (
            before - timedelta(days=30)
            <= event.updated_at
            < before + timedelta(seconds=1)
        )

    metrics = (
        db_session.query(RawMetric).filter(RawMetric.user_id.like(f"{PREFIX}%")).all()
    )
    assert len(metrics) == 30
    assert {m.feature_flag_id for m in metrics} == scale_ab["flags"]
    assert {m.metric_type for m in metrics} == {"flag_evaluation"}
    newest = max(m.timestamp for m in metrics)
    assert newest < before - timedelta(minutes=15), "a row is not 15 minutes old"


def test_a_second_call_loads_nothing(test_db, db_session, scale_ab):
    _preload(test_db, users=4, events=6, raw_metrics=8)
    assert _preload(test_db, users=4, events=6, raw_metrics=8) == (
        "already loaded: 4 users in scale_ab"
    )
    assert db_session.query(Event).filter(Event.user_id.like(f"{PREFIX}%")).count() == 6
    assert (
        db_session.query(RawMetric).filter(RawMetric.user_id.like(f"{PREFIX}%")).count()
        == 8
    )


def test_it_refuses_a_partly_loaded_experiment(test_db, db_session, scale_ab):
    db_session.add(
        Assignment(
            experiment_id=scale_ab["experiment"],
            variant_id=scale_ab["variants"][0],
            user_id=_user(0),
        )
    )
    db_session.commit()
    with pytest.raises(psycopg2.Error, match="1 of 5 users are already assigned"):
        _preload(test_db, users=5, events=0, raw_metrics=0)


def test_it_refuses_a_database_without_scale_ab(test_db, db_session):
    _clean(db_session)
    with pytest.raises(psycopg2.Error, match="has no experiment scale_ab"):
        _preload(test_db, users=3, events=3, raw_metrics=3)
    assert (
        db_session.query(Assignment)
        .filter(Assignment.user_id.like(f"{PREFIX}%"))
        .count()
        == 0
    )
