"""Each demo seed's rollout stage in progress records when it started (#1528).

The scheduler measures a time-based stage's duration from ``started_at``, and
from ``updated_at`` only for a stage with none. Each seed writes one stage in
progress; it records the instant the scheduler measured from before #1528, so
the demos behave as they did:

* StreamPulse's "Internal + 5%" went live two days ago (``start_date`` and
  ``updated_at`` both say so), so the first tick completes it, which is where
  the rollout story starts;
* the demo seed's and ShopLab's stage starts when the seed runs (its
  ``updated_at`` was the insert time), so it has a day to run.

The seeds' own functions run against a session that records what they add;
no database. ``test_seed_streampulse_reset.py`` runs the StreamPulse one
against PostgreSQL with a real tick.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, List

import pytest

from backend.app.core.rollout_scheduler import RolloutScheduler, stage_started_at
from backend.app.models import register_core_models
from backend.app.models.rollout_schedule import RolloutStage, RolloutStageStatus
from backend.scripts import seed_demo_data, seed_shoplab, seed_streampulse

pytestmark = [pytest.mark.unit]


class _Nothing:
    def filter(self, *args: Any, **kwargs: Any) -> "_Nothing":
        return self

    def first(self) -> None:
        return None


class _RecordingSession:
    """A new database: nothing exists, and every row added is kept."""

    def __init__(self) -> None:
        self.added: List[Any] = []

    def query(self, *args: Any) -> _Nothing:
        return _Nothing()

    def add(self, obj: Any) -> None:
        if getattr(obj, "id", "no id") is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    def add_all(self, objs: List[Any]) -> None:
        for obj in objs:
            self.add(obj)

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def refresh(self, obj: Any) -> None:
        pass


def _in_progress(seed) -> tuple:
    """Run *seed*'s flag seeding; return (the stage in progress, before, after)."""
    register_core_models()
    session = _RecordingSession()
    before = datetime.now(timezone.utc)
    seed.seed_feature_flags(session, SimpleNamespace(id=uuid.uuid4()))
    after = datetime.now(timezone.utc)
    (stage,) = [
        obj
        for obj in session.added
        if isinstance(obj, RolloutStage)
        and obj.status == RolloutStageStatus.IN_PROGRESS
    ]
    return stage, before, after


@pytest.mark.regression
def test_streampulse_s_stage_started_two_days_ago_and_the_first_tick_completes_it():
    stage, before, after = _in_progress(seed_streampulse)
    assert stage.started_at is not None, "the seed records no start"
    assert stage.started_at == stage.updated_at == stage.start_date.replace(tzinfo=None)
    started = stage_started_at(stage)
    assert before - timedelta(days=2) <= started <= after - timedelta(days=2)
    # 24 hours (the default duration) have passed since it started.
    assert RolloutScheduler()._is_stage_eligible_for_completion(stage, after)


@pytest.mark.regression
@pytest.mark.parametrize(
    "seed", [seed_demo_data, seed_shoplab], ids=["demo", "shoplab"]
)
def test_the_demo_and_shoplab_stage_starts_when_seeded(seed):
    stage, before, after = _in_progress(seed)
    assert stage.started_at is not None, "the seed records no start"
    assert before <= stage_started_at(stage) <= after
    # It runs its default 24 hours from the seed, as it did before #1528.
    completes = RolloutScheduler()._is_stage_eligible_for_completion
    assert not completes(stage, after + timedelta(hours=23))
    assert completes(stage, after + timedelta(hours=24))
