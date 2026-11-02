"""Fixtures for the split URL routing tests (#393).

``split_experiment`` makes split URL experiments straight in the database, as
ACTIVE ones a test can route through at once. Each first deletes any
experiment with its key, and every one it made is deleted in teardown, with
its events (assignments, variants, metrics and bandit state go with the
experiment). ``parked_holdouts`` turns any active global holdout off for the
test and back on afterwards: the backend integration tests create holdouts,
and an active one would move the exact counts here depending on test order.
``parked_split_url_experiments`` pauses every other active split URL
experiment of the shared database the same way, so the routes table holds
only the test's own. Every count a test makes filters by its own
experiment's id.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from backend.app.models.bandit_state import BanditState
from backend.app.models.event import Event
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.user import User

#: (name, is_control, traffic_allocation, url)
VariantSpec = Tuple[str, bool, float, str]

CONTROL_URL = "https://e.com/checkout"
TREATMENT_URL = "https://e.com/checkout-v2"

FIFTY_FIFTY: Sequence[VariantSpec] = (
    ("Control", True, 50, CONTROL_URL),
    ("Treatment", False, 50, TREATMENT_URL),
)


def delete_experiments(db: Session, ids: List[uuid.UUID]) -> None:
    """Delete experiments and their events; the rest goes with them."""
    if not ids:
        return
    db.query(Event).filter(Event.experiment_id.in_(ids)).delete(
        synchronize_session=False
    )
    db.query(Experiment).filter(Experiment.id.in_(ids)).delete(
        synchronize_session=False
    )
    db.commit()


@pytest.fixture
def parked_holdouts(db_session: Session) -> Iterator[None]:
    parked = [
        row.id
        for row in db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active)
    ]
    db_session.execute(
        update(GlobalHoldout)
        .where(GlobalHoldout.id.in_(parked))
        .values(is_active=False)
    )
    db_session.commit()
    try:
        yield
    finally:
        db_session.rollback()
        db_session.execute(
            update(GlobalHoldout)
            .where(GlobalHoldout.id.in_(parked))
            .values(is_active=True)
        )
        db_session.commit()


@pytest.fixture
def parked_split_url_experiments(db_session: Session) -> Iterator[None]:
    """Pause every other ACTIVE split URL experiment for the test, so the
    routes table holds only the test's own; resume them afterwards."""
    parked = [
        row.id
        for row in db_session.query(Experiment).filter(
            Experiment.experiment_type == ExperimentType.SPLIT_URL,
            Experiment.status == ExperimentStatus.ACTIVE,
        )
    ]
    db_session.execute(
        update(Experiment)
        .where(Experiment.id.in_(parked))
        .values(status=ExperimentStatus.PAUSED)
    )
    db_session.commit()
    try:
        yield
    finally:
        db_session.rollback()
        db_session.execute(
            update(Experiment)
            .where(Experiment.id.in_(parked))
            .values(status=ExperimentStatus.ACTIVE)
        )
        db_session.commit()


@pytest.fixture
def split_experiment(
    db_session: Session,
    admin_user: User,
    parked_holdouts: None,
    parked_split_url_experiments: None,
) -> Iterator[Callable[..., Experiment]]:
    """Make an ACTIVE split URL experiment: ``make(key, variants=..., ...)``."""
    made: List[uuid.UUID] = []

    def make(
        key: str,
        variants: Sequence[VariantSpec] = FIFTY_FIFTY,
        *,
        status: ExperimentStatus = ExperimentStatus.ACTIVE,
        started_days_ago: float = 1,
        optimization_type: str = "fixed",
        bandit_weights: Optional[Dict[str, float]] = None,
        split_url_config: Optional[dict] = None,
        experiment_type: ExperimentType = ExperimentType.SPLIT_URL,
        metric_type: MetricType = MetricType.CONVERSION,
        targeting_rules: Optional[dict] = None,
    ) -> Experiment:
        stale = [
            e.id for e in db_session.query(Experiment).filter(Experiment.key == key)
        ]
        delete_experiments(db_session, stale)
        config = split_url_config
        if config is None and experiment_type == ExperimentType.SPLIT_URL:
            config = {
                "variants": [
                    {"name": name, "url": url, "traffic_allocation": share}
                    for name, _, share, url in variants
                ]
            }
        experiment = Experiment(
            name=f"Split URL {key} {uuid.uuid4().hex[:6]}",
            description="Split URL routing test",
            hypothesis="The variant page converts better",
            key=key,
            status=status,
            experiment_type=experiment_type,
            owner_id=admin_user.id,
            start_date=datetime.now(timezone.utc) - timedelta(days=started_days_ago),
            split_url_config=config,
            optimization_type=optimization_type,
            targeting_rules=targeting_rules,
        )
        db_session.add(experiment)
        db_session.flush()
        made.append(experiment.id)
        rows = [
            Variant(
                experiment_id=experiment.id,
                name=name,
                is_control=is_control,
                traffic_allocation=share,
            )
            for name, is_control, share, _ in variants
        ]
        db_session.add_all(rows)
        db_session.add(
            Metric(
                experiment_id=experiment.id,
                name="Purchase",
                event_name="purchase",
                metric_type=metric_type,
                is_primary=True,
            )
        )
        db_session.commit()
        if bandit_weights is not None:
            by_name = {row.name: row for row in rows}
            db_session.add(
                BanditState(
                    experiment_id=experiment.id,
                    algorithm=optimization_type,
                    variant_weights={
                        str(by_name[name].id): {"weight": weight}
                        for name, weight in bandit_weights.items()
                    },
                    total_pulls=0,
                )
            )
            db_session.commit()
        db_session.refresh(experiment)
        return experiment

    try:
        yield make
    finally:
        db_session.rollback()
        delete_experiments(db_session, made)
