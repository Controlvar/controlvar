"""Helpers the identity-alias tests share (#449).

Links are written through ``identity_aliases.link``, the one writer: no request
can create a link yet. ``link`` commits, so every id here is fresh (a uuid
suffix) and :class:`World` deletes what a test wrote, links included, because
the shared test database is not truncated between tests.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from sqlalchemy import update
from sqlalchemy.orm import Session

from backend.app.models.analysis_snapshot import AnalysisSnapshot
from backend.app.models.assignment import Assignment
from backend.app.models.bandit_state import BanditState
from backend.app.models.event import Event
from backend.app.models.event_dedup_key import EventDedupKey
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.holdout_population import HoldoutPopulation
from backend.app.models.identity_alias import IdentityAlias
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.global_holdout_service import (
    GlobalHoldoutService,
    holdout_bucket,
)
from backend.app.services.identity_aliases import link


def fresh(prefix: str) -> str:
    """A user id no other test uses."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class World:
    """One test's experiments, ids and links, removed again by :meth:`close`."""

    def __init__(self, db: Session, make_experiment) -> None:
        self.db = db
        self._make_experiment = make_experiment
        self.experiments: List[Experiment] = []
        self.ids: List[str] = []

    # -- setup ---------------------------------------------------------------

    def experiment(self, label: str = "Alias", **overrides) -> Experiment:
        """An ACTIVE A/B experiment: control and treatment at 50/50, and a
        primary ``purchase`` conversion metric."""
        suffix = uuid.uuid4().hex[:8]
        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=3)
        fields = {
            "name": f"{label} {suffix}",
            "key": f"{label.lower().replace(' ', '-')}-{suffix}",
            "status": ExperimentStatus.ACTIVE,
            "experiment_type": ExperimentType.A_B,
            "start_date": start,
        }
        fields.update(overrides)
        exp = self._make_experiment(**fields)
        self.db.add_all(
            [
                Variant(
                    experiment_id=exp.id,
                    name="control",
                    is_control=True,
                    traffic_allocation=50,
                    configuration={"look": "control"},
                ),
                Variant(
                    experiment_id=exp.id,
                    name="treatment",
                    is_control=False,
                    traffic_allocation=50,
                    configuration={"look": "treatment"},
                ),
                Metric(
                    experiment_id=exp.id,
                    name="Purchase",
                    event_name="purchase",
                    metric_type=MetricType.CONVERSION,
                    is_primary=True,
                ),
            ]
        )
        self.db.commit()
        self.db.refresh(exp)
        self.experiments.append(exp)
        return exp

    def id(self, prefix: str) -> str:
        user_id = fresh(prefix)
        self.ids.append(user_id)
        return user_id

    def hashed_variant(self, exp: Experiment, user_id: str) -> str:
        """The variant name the hash gives *user_id* in *exp*."""
        chosen = AssignmentService(self.db)._hash_user_to_variant(user_id, exp)
        return next(v.name for v in exp.variants if v.id == chosen)

    def split_pair(
        self, exp: Experiment, first: str = "anon", second: str = "known"
    ) -> Tuple[str, str]:
        """Two fresh ids the hash puts in different variants of *exp*."""
        a = self.id(first)
        while True:
            u = fresh(second)
            if self.hashed_variant(exp, u) != self.hashed_variant(exp, a):
                self.ids.append(u)
                assert self.hashed_variant(exp, a) != self.hashed_variant(exp, u)
                return a, u

    def hashed_elsewhere(self, exp: Experiment, variant: str, prefix: str) -> str:
        """A fresh id the hash does NOT put in *variant* of *exp*."""
        while True:
            user_id = fresh(prefix)
            if self.hashed_variant(exp, user_id) != variant:
                self.ids.append(user_id)
                return user_id

    def link(self, anonymous_id: str, user_id: str) -> bool:
        for user in (anonymous_id, user_id):
            if user not in self.ids:
                self.ids.append(user)
        return link(self.db, anonymous_id, user_id)

    def store_assignment(
        self, exp: Experiment, user_id: str, variant: str, at: datetime
    ) -> Assignment:
        """An assignment row written straight to the table, as a release that
        knows nothing of links writes it."""
        row = Assignment(
            experiment_id=exp.id,
            variant_id=variant_of(exp, variant).id,
            user_id=user_id,
        )
        row.created_at = at
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        if user_id not in self.ids:
            self.ids.append(user_id)
        return row

    # -- reads ---------------------------------------------------------------

    def rows(self, exp: Experiment, ids: Sequence[str]) -> List[Assignment]:
        self.db.expire_all()
        return (
            self.db.query(Assignment)
            .filter(Assignment.experiment_id == exp.id, Assignment.user_id.in_(ids))
            .order_by(Assignment.created_at, Assignment.id)
            .all()
        )

    def events(
        self, exp: Experiment, ids: Sequence[str], name: Optional[str] = None
    ) -> List[Event]:
        self.db.expire_all()
        query = self.db.query(Event).filter(
            Event.experiment_id == exp.id, Event.user_id.in_(ids)
        )
        if name is not None:
            query = query.filter(Event.event_name == name)
        return query.order_by(Event.created_at).all()

    # -- teardown ------------------------------------------------------------

    def close(self) -> None:
        self.db.rollback()
        for exp in self.experiments:
            for model in (AnalysisSnapshot, BanditState, Event, Assignment):
                self.db.query(model).filter(model.experiment_id == exp.id).delete(
                    synchronize_session=False
                )
        if self.ids:
            self.db.query(EventDedupKey).filter(
                EventDedupKey.user_id.in_(self.ids)
            ).delete(synchronize_session=False)
            self.db.query(Event).filter(Event.user_id.in_(self.ids)).delete(
                synchronize_session=False
            )
            self.db.query(HoldoutPopulation).filter(
                HoldoutPopulation.user_id.in_(self.ids)
            ).delete(synchronize_session=False)
            self.db.query(IdentityAlias).filter(
                IdentityAlias.anonymous_id.in_(self.ids)
                | IdentityAlias.user_id.in_(self.ids)
            ).delete(synchronize_session=False)
        self.db.commit()


def variant_of(exp: Experiment, name: str) -> Variant:
    return next(v for v in exp.variants if v.name == name)


@contextmanager
def no_active_holdout(db: Session) -> Iterator[None]:
    """No global holdout active for the duration, so the hash decides."""
    parked = [row.id for row in db.query(GlobalHoldout).filter(GlobalHoldout.is_active)]
    db.execute(
        update(GlobalHoldout)
        .where(GlobalHoldout.id.in_(parked))
        .values(is_active=False)
    )
    db.commit()
    try:
        yield
    finally:
        db.rollback()
        db.execute(
            update(GlobalHoldout)
            .where(GlobalHoldout.id.in_(parked))
            .values(is_active=True)
        )
        db.commit()


@contextmanager
def measurable_holdout(db: Session, percentage: int = 20) -> Iterator[GlobalHoldout]:
    """An active holdout made and activated through the service, so it has its
    own salt and ``activated_at``. Any other active holdout is parked with a
    plain UPDATE and put back afterwards; the holdout's population rows go
    with it (ON DELETE CASCADE)."""
    parked = [row.id for row in db.query(GlobalHoldout).filter(GlobalHoldout.is_active)]
    db.execute(
        update(GlobalHoldout)
        .where(GlobalHoldout.id.in_(parked))
        .values(is_active=False)
    )
    db.commit()
    holdout = GlobalHoldoutService(db).create_holdout(
        name=f"alias-holdout-{uuid.uuid4().hex[:8]}",
        holdout_percentage=percentage,
        is_active=True,
    )
    assert holdout.is_measurable
    try:
        yield holdout
    finally:
        db.rollback()
        db.query(GlobalHoldout).filter(GlobalHoldout.id == holdout.id).delete()
        db.execute(
            update(GlobalHoldout)
            .where(GlobalHoldout.id.in_(parked))
            .values(is_active=True)
        )
        db.commit()


def held_and_free(holdout: GlobalHoldout, world: World) -> Tuple[str, str]:
    """A fresh id *holdout* holds out, and one it does not."""
    salt, percentage = holdout.hash_salt, holdout.holdout_percentage
    held = next(
        u
        for u in (world.id("held") for _ in range(1000))
        if holdout_bucket(u, salt) < percentage
    )
    free = next(
        u
        for u in (world.id("free") for _ in range(1000))
        if holdout_bucket(u, salt) >= percentage
    )
    return held, free


def assign(client, exp: Experiment, user_id: str, context=None) -> Dict:
    body = {"experiment_key": exp.key, "user_id": user_id}
    if context is not None:
        body["context"] = context
    response = client.post("/api/v1/tracking/assign", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def track(
    client,
    exp: Experiment,
    user_id: str,
    name: str = "purchase",
    value: Optional[float] = None,
    event_id: Optional[str] = None,
) -> Dict:
    body = {
        "event_type": name,
        "event_name": name,
        "user_id": user_id,
        "experiment_key": exp.key,
    }
    if value is not None:
        body["value"] = value
    if event_id is not None:
        body["event_id"] = event_id
    response = client.post("/api/v1/tracking/track", json=body)
    assert response.status_code == 200, response.text
    return response.json()
