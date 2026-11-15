"""A group stays balanced when its users arrive at four processes at once (#448).

An API runs as several processes (uvicorn workers, ECS tasks), and a group's
next place must be taken once, whichever process asks. An allocator whose
state lives in one process -- or one that reads the counter and writes it back
in two statements -- passes every test run in one process: the requests of one
process take their turns. So this starts four OS processes (``spawn``: each its
own interpreter and engine), which wait on a barrier and then assign 401 users
between them, interleaved, to one balanced 50/50 experiment, in groups of 1,
2, 3 and 395 users. Each request is held a few milliseconds between its
allocation and its commit, which widens every window two processes could meet
in; each child raises the counter step's timeout itself, so no user falls back
to the hash.

Then, on the rows: 401 assignments, every child exited 0, no user outside the
blocks, and in each group the seqs are exactly ``0 .. n - 1`` with every row's
variant the block place of its seq; ``|T - C|`` is ``n mod 2`` as an extra.
"""

from __future__ import annotations

import multiprocessing
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import text

from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    Metric,
    MetricType,
    Variant,
)
from backend.app.models.global_holdout import GlobalHoldout
from backend.tests.integration.balance_checks import balance_rows
from backend.tests.integration.services import balance_processes

pytestmark = pytest.mark.integration

SCHEMA = "test_experimentation"
PROCESSES = 4
#: How long each request is held between its allocation and its commit.
HOLD_MS = 10
GROUP_SIZES = {"g1": 1, "g2": 2, "g3": 3, "g395": 395}
#: Long enough for 401 held requests serialised on one row, with margin.
JOIN_SECONDS = 240


@contextmanager
def _parked_holdouts(db_session):
    """No active global holdout while the children run, restored after."""
    parked = (
        db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active.is_(True)).all()
    )
    for row in parked:
        row.is_active = False
    db_session.commit()
    try:
        yield
    finally:
        db_session.rollback()
        for row in parked:
            db_session.merge(row).is_active = True
        db_session.commit()


@pytest.fixture
def balanced_experiment(db_session, admin_user):
    """An ACTIVE 50/50 experiment balancing within ``tier``; committed, so
    the children see it."""
    with _parked_holdouts(db_session):
        suffix = uuid.uuid4().hex[:8]
        experiment = Experiment(
            name=f"Balance across processes {suffix}",
            key=f"balance-processes-{suffix}",
            status=ExperimentStatus.ACTIVE,
            owner_id=admin_user.id,
            balance_within={"attribute": "tier", "values": sorted(GROUP_SIZES)},
        )
        db_session.add(experiment)
        db_session.flush()
        for name, is_control in (("control", True), ("treatment", False)):
            db_session.add(
                Variant(
                    experiment_id=experiment.id,
                    name=name,
                    is_control=is_control,
                    traffic_allocation=50,
                )
            )
        db_session.add(
            Metric(
                experiment_id=experiment.id,
                name="Purchase",
                event_name="purchase",
                metric_type=MetricType.CONVERSION,
                is_primary=True,
            )
        )
        db_session.commit()
        db_session.refresh(experiment)
        assert experiment.balance_seed, "the insert made no block-order key"
        experiment_id = experiment.id
        yield experiment
        db_session.rollback()
        db_session.execute(
            text("DELETE FROM experiments WHERE id = :id"), {"id": experiment_id}
        )
        db_session.commit()


def _users() -> list:
    """401 users, each with their group, interleaved across the groups."""
    users = []
    for group, size in GROUP_SIZES.items():
        users.extend(
            (f"{group}-{uuid.uuid4().hex[:10]}-{i}", group) for i in range(size)
        )
    users.sort(key=lambda user: user[0][-6:])
    return users


@pytest.mark.regression
def test_four_processes_keep_every_group_balanced(
    balanced_experiment, db_session, test_db
):
    users = _users()
    assert len(users) == 401
    shares = [users[i::PROCESSES] for i in range(PROCESSES)]
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(PROCESSES)
    children = [
        context.Process(
            target=balance_processes.run_child,
            args=(
                test_db.url.render_as_string(hide_password=False),
                SCHEMA,
                str(balanced_experiment.id),
                share,
                barrier,
                HOLD_MS,
            ),
        )
        for share in shares
    ]
    for child in children:
        child.start()
    for child in children:
        child.join(JOIN_SECONDS)
    alive = [child.pid for child in children if child.is_alive()]
    for child in children:
        if child.is_alive():
            child.kill()
            child.join(10)

    assert alive == [], f"children still running after {JOIN_SECONDS} s: {alive}"
    assert [child.exitcode for child in children] == [0] * PROCESSES

    db_session.expire_all()
    found = balance_rows(db_session, balanced_experiment.id)
    assert found.problems == [], found.problems[:10]
    assert found.rows == 401
    assert found.outside_blocks() == 0, "a user fell back to the hash"
    for group, size in GROUP_SIZES.items():
        rows = found.groups[group]
        assert rows.next_seq == size, (group, rows.next_seq)
        assert found.gap(group, "control", "treatment") == size % 2, (
            group,
            dict(rows.in_blocks),
        )
