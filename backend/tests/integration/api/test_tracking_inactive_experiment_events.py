"""
``/tracking/track`` and ``/tracking/batch`` tag an event with the user's
variant only while its experiment is ACTIVE.

The experiment's status is read when the event is received. An event that
names a draft, paused, completed or archived experiment is stored with its
``experiment_id`` and no ``variant_id``, so it counts in no variant's results.
``/tracking/events`` (by ids) and the flag tag are unchanged.

* the lifecycle test counts converting users per variant (``use_cache=false``)
  after each of ACTIVE, PAUSED, ACTIVE and COMPLETED, through both routes;
  with the status check taken out it fails by exactly one. It also pins what
  other readers answer for the two events that count in no variant: those
  that filter on the experiment alone (the results summary's event total,
  the breakdown's segments and their samples, the experiments export's event
  total) count them; the bandit's successes, read by variant, do not;
* the status table pins what is stored for every status, on both routes.

Rows created here are deleted in teardown: the shared test database is not
truncated between tests.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import pytest

from backend.app.core.bandit_scheduler import BanditScheduler
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

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


@pytest.fixture
def experiment(db_session, make_experiment):
    """An ACTIVE A/B experiment with a primary ``purchase`` conversion metric
    and four users assigned to each variant, none of whom has an event."""
    suffix = uuid.uuid4().hex[:8]
    exp = make_experiment(
        name=f"Inactive events {suffix}",
        key=f"inactive-events-{suffix}",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        start_date=datetime.now(timezone.utc) - timedelta(days=1),
    )
    control = Variant(
        experiment_id=exp.id, name="control", is_control=True, traffic_allocation=50
    )
    treatment = Variant(
        experiment_id=exp.id, name="treatment", is_control=False, traffic_allocation=50
    )
    db_session.add_all(
        [
            control,
            treatment,
            Metric(
                experiment_id=exp.id,
                name="Purchase",
                event_name="purchase",
                metric_type=MetricType.CONVERSION,
                is_primary=True,
            ),
        ]
    )
    db_session.commit()
    users: Dict[str, List[str]] = {}
    for variant in (control, treatment):
        users[variant.name] = [f"ie-{suffix}-{variant.name}-{i}" for i in range(4)]
        db_session.add_all(
            Assignment(experiment_id=exp.id, variant_id=variant.id, user_id=user_id)
            for user_id in users[variant.name]
        )
    db_session.commit()
    db_session.refresh(exp)
    yield exp, {"control": control.id, "treatment": treatment.id}, users
    db_session.rollback()
    for model in (AnalysisSnapshot, Event, Assignment):
        db_session.query(model).filter(model.experiment_id == exp.id).delete(
            synchronize_session=False
        )
    db_session.commit()


def _purchase(
    client, route: str, exp, user_id: str, country: str = "US", **extra
) -> None:
    """One ``purchase`` for ``exp``, through ``/track`` or ``/batch``."""
    event = {
        "event_type": "purchase",
        "user_id": user_id,
        "experiment_key": exp.key,
        "value": 20.0,
        "metadata": {"country": country},
        **extra,
    }
    if route == "track":
        response = client.post("/api/v1/tracking/track", json=event)
        assert response.status_code == 200, response.text
        assert response.json()["experiment_id"] == str(exp.id)
    else:
        response = client.post("/api/v1/tracking/batch", json={"events": [event]})
        assert response.status_code == 200, response.text
        assert response.json() == {
            "success_count": 1,
            "failure_count": 0,
            "errors": None,
        }


def _lifecycle(client, exp, action: str, status: ExperimentStatus) -> None:
    response = client.post(f"/api/v1/experiments/{exp.id}/{action}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == status.value


def _results(client, exp, **params) -> dict:
    response = client.get(
        f"/api/v1/results/{exp.id}", params={"use_cache": "false", **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _converting_users(client, exp) -> Dict[str, int]:
    """Converting users per variant, from the primary metric of ``/results``."""
    [metric] = [m for m in _results(client, exp)["metrics"] if m["is_primary"]]
    return {v["variant_name"]: v["conversions"] for v in metric["variants"]}


@pytest.mark.regression
def test_converting_users_per_variant_across_pause_resume_and_complete(
    admin_client, db_session, experiment
):
    """Exact converting users per variant after each status: a purchase
    received while the experiment is paused or completed counts in no variant,
    and the same user's purchase after the resume does."""
    exp, variant_ids, users = experiment
    control, treatment = users["control"], users["treatment"]

    # ACTIVE: one purchase in each variant.
    _purchase(admin_client, "track", exp, control[0])
    _purchase(admin_client, "batch", exp, treatment[0])
    assert _converting_users(admin_client, exp) == {"control": 1, "treatment": 1}

    # PAUSED: a treatment user buys. Stored, counted nowhere.
    _lifecycle(admin_client, exp, "pause", ExperimentStatus.PAUSED)
    _purchase(admin_client, "track", exp, treatment[1], country="FR")
    assert _converting_users(admin_client, exp) == {"control": 1, "treatment": 1}

    # ACTIVE again: the same treatment user buys again, and a control user.
    _lifecycle(admin_client, exp, "start", ExperimentStatus.ACTIVE)
    _purchase(admin_client, "track", exp, treatment[1])
    _purchase(admin_client, "batch", exp, control[1])
    assert _converting_users(admin_client, exp) == {"control": 2, "treatment": 2}

    # COMPLETED: a control user buys. Stored, counted nowhere.
    _lifecycle(admin_client, exp, "complete", ExperimentStatus.COMPLETED)
    _purchase(admin_client, "batch", exp, control[2], country="DE")
    results = _results(admin_client, exp, breakdown="country")
    [metric] = [m for m in results["metrics"] if m["is_primary"]]
    assert {v["variant_name"]: v["conversions"] for v in metric["variants"]} == {
        "control": 2,
        "treatment": 2,
    }

    # What the readers that filter on the experiment alone answer: every
    # event is kept with its experiment, so the summary's event total counts
    # all six (its converting users only the four counted in a variant), and
    # the breakdown finds the paused event's and the completed event's
    # countries, with their users in the sample and no conversion.
    assert results["summary"]["total_events"] == 6
    assert results["summary"]["total_conversions"] == 4
    assert {
        segment["segment_value"]: {
            v["variant_name"]: (v["sample_size"], v["conversions"])
            for v in segment["variants"]
        }
        for segment in results["breakdown"]["segments"]
    } == {
        "US": {"control": (2, 2), "treatment": (2, 2)},
        "FR": {"control": (0, 0), "treatment": (1, 0)},
        "DE": {"control": (1, 0), "treatment": (0, 0)},
    }
    since = (exp.created_at - timedelta(seconds=1)).isoformat()
    exported = admin_client.get(
        "/api/v1/export/experiments",
        params={"format": "json", "start_date": since},
    )
    assert exported.status_code == 200, exported.text
    [row] = [r for r in exported.json() if r.get("experiment_id") == str(exp.id)]
    assert row["total_events"] == 6
    # The bandit's successes are converting users per variant, as /results.
    stats = BanditScheduler(db_session)._stats_from_postgres(
        exp.id, [str(v) for v in variant_ids.values()], exp
    )
    assert {
        name: (stats[str(variant_id)].pulls, stats[str(variant_id)].successes)
        for name, variant_id in variant_ids.items()
    } == {"control": (4, 2), "treatment": (4, 2)}
    # The segmented results find the same segments: the paused and completed
    # events put their users in FR's and DE's samples, with no conversion.
    segmented = admin_client.get(
        f"/api/v1/experiments/{exp.id}/segmented-results/country"
    )
    assert segmented.status_code == 200, segmented.text
    assert {
        segment["segment_value"]: {
            v["variant_name"]: (v["sample_size"], v["conversions"])
            for metric in segment["metrics"]
            for v in metric["variants"]
        }
        for segment in segmented.json()["segments"]
    } == {
        "US": {"control": (2, 2), "treatment": (2, 2)},
        "FR": {"control": (0, 0), "treatment": (1, 0)},
        "DE": {"control": (1, 0), "treatment": (0, 0)},
    }

    db_session.expire_all()
    stored = sorted(
        (row.user_id, str(row.variant_id))
        for row in db_session.query(Event).filter(Event.experiment_id == exp.id)
    )
    in_control, in_treatment = (str(variant_ids[n]) for n in ("control", "treatment"))
    assert stored == sorted(
        [
            (control[0], in_control),
            (treatment[0], in_treatment),
            (treatment[1], "None"),
            (treatment[1], in_treatment),
            (control[1], in_control),
            (control[2], "None"),
        ]
    )


STATUSES = [
    ExperimentStatus.ACTIVE,
    ExperimentStatus.DRAFT,
    ExperimentStatus.PAUSED,
    ExperimentStatus.COMPLETED,
    ExperimentStatus.ARCHIVED,
]


@pytest.mark.regression
def test_each_status_and_route_stores_the_variant_only_while_active(
    admin_client, db_session, experiment, make_feature_flag
):
    """Per status, on both routes: the stored row's experiment, variant and
    flag. Only ACTIVE tags the user's variant; the flag tag is the same in
    every status."""
    exp, variant_ids, users = experiment
    flag = make_feature_flag()
    user_id = users["treatment"][3]
    stored = {}
    try:
        for status in STATUSES:
            db_session.query(type(exp)).filter(type(exp).id == exp.id).update(
                {"status": status}, synchronize_session=False
            )
            db_session.commit()
            for route in ("track", "batch"):
                name = f"purchase-{status.value}-{route}"
                _purchase(
                    admin_client,
                    route,
                    exp,
                    user_id,
                    event_name=name,
                    feature_flag_key=flag.key,
                )
                db_session.expire_all()
                row = db_session.query(Event).filter(Event.event_name == name).one()
                stored[(status.value, route)] = (
                    row.experiment_id == exp.id,
                    row.variant_id,
                    row.feature_flag_id == flag.id,
                )
    finally:
        db_session.rollback()
        db_session.query(Event).filter(Event.feature_flag_id == flag.id).delete(
            synchronize_session=False
        )
        db_session.commit()

    treatment = variant_ids["treatment"]
    assert stored == {
        (status.value, route): (
            True,
            treatment if status is ExperimentStatus.ACTIVE else None,
            True,
        )
        for status in STATUSES
        for route in ("track", "batch")
    }


def test_events_by_ids_keep_the_variant_they_name_for_a_paused_experiment(
    admin_client, db_session, experiment
):
    """``/tracking/events`` stores the ids it is given, whatever the status."""
    exp, variant_ids, users = experiment
    exp.status = ExperimentStatus.PAUSED
    db_session.commit()
    response = admin_client.post(
        "/api/v1/tracking/events",
        json={
            "event_name": "purchase",
            "user_id": users["control"][3],
            "experiment_id": str(exp.id),
            "variant_id": str(variant_ids["control"]),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["variant_id"] == str(variant_ids["control"])
