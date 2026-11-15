"""
The sequential analysis compares the control against the same treatment arm
on every call (#929).

``Experiment.variants`` had no ``order_by``, so PostgreSQL returned the rows
in their physical order: the insertion order on a fresh table, but a row
rewritten later in the heap moves to the end, and a plain ``ANALYZE`` can
change the plan and with it the order.  ``_get_sequential_data`` takes "the
first treatment" from that list, so on an experiment with three or more arms
``GET /results/{id}/sequential``, and the block ``GET /results/{id}`` embeds,
could compare the control against a different arm from one call to the next.
The relationship is now ordered by ``created_at`` then ``id``.

Two layouts put the treatment created first AFTER the other treatment in the
heap.  ``inserted_later`` inserts it last, with an earlier ``created_at``.
``rewritten_later`` inserts the rows in creation order and then rewrites the
first treatment's row (deleted and inserted again, unchanged, in one
statement), which is where an UPDATE that cannot reuse the row's place puts
it.  Each layout is asserted as a precondition on the bare query, so a
database that happened to return creation order fails the precondition
instead of passing the test by luck.

Real rows and the real ``AnalysisService``: three variants, one primary
conversion metric, distinct user counts per arm so the arm compared shows in
``confidence_sequence.sample_size``.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from backend.app.models.analysis_snapshot import AnalysisSnapshot
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import (
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)

LAYOUTS = ("inserted_later", "rewritten_later")

#: (name, is_control, users, converted); the control and the first treatment
#: have 200 users each, the second treatment 100, so the arm the analysis
#: compared is readable from ``sample_size`` (400 against 300).
ARMS = {
    "control": (True, 200, 20),
    "treatment": (False, 200, 30),
    "treatment-b": (False, 100, 10),
}
#: The order the variants were created in: ``created_at`` ascending.
CREATION_ORDER = ("control", "treatment", "treatment-b")
#: The order PostgreSQL returns them in under either layout.
PHYSICAL_ORDER = ["control", "treatment-b", "treatment"]

REWRITE_ROW = text(
    "WITH moved AS (DELETE FROM variants WHERE id = :id RETURNING *) "
    "INSERT INTO variants SELECT * FROM moved"
)
BARE_ORDER = text("SELECT name FROM variants WHERE experiment_id = :id")
#: The heap order itself: what every plan returns for rows with equal keys
#: (a sequential scan reads the heap in order, and an index scan on
#: ``experiment_id`` returns equal keys in heap-TID order).
CTID_ORDER = text("SELECT name FROM variants WHERE experiment_id = :id ORDER BY ctid")
#: Writes of the layout before giving up and letting the precondition fail.
LAYOUT_ATTEMPTS = 10


@pytest.fixture
def make_three_arm_experiment(db_session, make_experiment):
    """An ACTIVE three-variant experiment with sequential testing on, laid out
    so that the bare query returns ``PHYSICAL_ORDER``; rows removed after."""
    created = []

    def _make(layout):
        suffix = uuid.uuid4().hex[:8]
        experiment = make_experiment(
            name=f"Three-arm sequential {suffix}",
            key=f"three-arm-sequential-{suffix}",
            status=ExperimentStatus.ACTIVE,
            experiment_type=ExperimentType.MULTIVARIATE,
            start_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
            sequential_testing_enabled=True,
            sequential_testing_config={"method": "msprt"},
        )
        created.append(experiment)
        base = datetime(2026, 9, 1, 0, 0, 0)
        stamps = {
            name: base + timedelta(minutes=CREATION_ORDER.index(name))
            for name in CREATION_ORDER
        }
        insertion = {
            "inserted_later": ("control", "treatment-b", "treatment"),
            "rewritten_later": CREATION_ORDER,
        }[layout]
        # The whole layout is written in ONE transaction, on one connection.
        # A new tuple goes into the connection's current target page, and
        # into that page's lowest unused line pointer, so each later INSERT
        # of the transaction lands after the earlier ones.  Across commits
        # it does not: a new connection asks the free space map for a page,
        # and a ``variants`` heap with free space from earlier tests (rows
        # deleted, then vacuumed) hands back an earlier page (#1678).  The
        # one case left is a page that fills up mid-layout, so the physical
        # (ctid) order is read back and the layout is written again, a
        # bounded number of times, until it is what the layout promises.
        variants = {}
        for _attempt in range(LAYOUT_ATTEMPTS):
            variants = {}
            for name in insertion:
                is_control, _users, _converted = ARMS[name]
                variant = Variant(
                    experiment_id=experiment.id,
                    name=name,
                    is_control=is_control,
                    traffic_allocation=34 if is_control else 33,
                    created_at=stamps[name],
                    updated_at=stamps[name],
                )
                db_session.add(variant)
                db_session.flush()  # one INSERT per row, in this order
                variants[name] = variant
            if layout == "rewritten_later":
                db_session.execute(REWRITE_ROW, {"id": variants["treatment"].id})
            by_ctid = (
                db_session.execute(CTID_ORDER, {"id": experiment.id}).scalars().all()
            )
            if by_ctid == PHYSICAL_ORDER:
                break
            db_session.rollback()
        db_session.commit()
        bare = db_session.execute(BARE_ORDER, {"id": experiment.id}).scalars().all()
        assert bare == PHYSICAL_ORDER, (
            f"precondition: the bare query returned {bare}; the layout {layout!r} "
            "did not put the first treatment after the other one"
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
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = []
        for name, (_is_control, n_users, n_converted) in ARMS.items():
            variant = variants[name]
            for i in range(n_users):
                user_id = f"{name}-{suffix}-{i:05d}"
                rows.append(
                    Assignment(
                        experiment_id=experiment.id,
                        variant_id=variant.id,
                        user_id=user_id,
                    )
                )
                if i < n_converted:
                    rows.append(
                        Event(
                            event_type="purchase",
                            event_name="purchase",
                            user_id=user_id,
                            experiment_id=experiment.id,
                            variant_id=variant.id,
                            value=1.0,
                            created_at=now_iso,
                        )
                    )
        db_session.add_all(rows)
        db_session.commit()
        return experiment

    yield _make
    db_session.rollback()
    for experiment in created:
        for model in (AnalysisSnapshot, Event, Assignment):
            db_session.query(model).filter(model.experiment_id == experiment.id).delete(
                synchronize_session=False
            )
    db_session.commit()


def _get(client, path, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.regression
@pytest.mark.parametrize("layout", LAYOUTS)
def test_variants_load_in_creation_order(db_session, make_three_arm_experiment, layout):
    """``experiment.variants`` is the creation order, whatever the heap order."""
    experiment = make_three_arm_experiment(layout)

    db_session.expire_all()

    assert [v.name for v in experiment.variants] == list(CREATION_ORDER)


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.regression
@pytest.mark.parametrize("layout", LAYOUTS)
def test_sequential_analysis_compares_the_control_against_the_treatment_created_first(
    admin_client, make_three_arm_experiment, layout
):
    """Two calls compare the same arms: the control (200 users) against the
    treatment created first (200 users), never ``treatment-b`` (100)."""
    experiment = make_three_arm_experiment(layout)

    first = _get(admin_client, f"/api/v1/results/{experiment.id}/sequential")
    second = _get(admin_client, f"/api/v1/results/{experiment.id}/sequential")
    embedded = _get(
        admin_client, f"/api/v1/results/{experiment.id}", use_cache="false"
    )["sequential_testing"]

    assert first["confidence_sequence"]["sample_size"] == 400
    assert second == first
    assert embedded == first
