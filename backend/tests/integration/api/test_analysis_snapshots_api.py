"""
DB-backed tests for the P0 "statistical credibility" surface of the results API.

* ``GET /api/v1/results/{id}`` — real ``AnalysisService`` on seeded rows:
  the ``srm`` block reflects the assignment split, and an
  ``analysis_snapshots`` row (kind ``frequentist``) is written per fresh
  computation, plus a ``bayesian`` row when Bayesian analysis is enabled.
* ``GET /api/v1/results/{id}/bayesian`` — two calls return byte-identical
  posteriors and the same ``seed``.
* ``GET /api/v1/results/{id}/cuped`` and ``/sequential`` — snapshot rows.
* ``GET /api/v1/experiments/{id}/results`` shares the schema, so ``srm``
  is exposed there too.
* A snapshot write failure never fails the response.

Rows created here are deleted in fixture teardown because the shared test
database is not truncated between tests.
"""

import os
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

from backend.app.core.stats_engine import ENGINE_VERSION
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
from backend.app.services.analysis_service import BAYESIAN_N_SAMPLES

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_experiment(db_session, make_experiment):
    """ACTIVE 50/50 experiment with a primary conversion metric; no rows yet."""
    suffix = uuid.uuid4().hex[:8]
    experiment = make_experiment(
        name=f"Snapshot API {suffix}",
        key=f"snapshot-api-{suffix}",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        start_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
        bayesian_enabled=True,
        bayesian_config={"alpha": 1.0, "beta": 1.0, "loss_threshold": 0.001},
    )
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
    yield experiment
    db_session.rollback()
    for model in (AnalysisSnapshot, Event, Assignment):
        db_session.query(model).filter(model.experiment_id == experiment.id).delete(
            synchronize_session=False
        )
    db_session.commit()


def _variants(experiment):
    by_name = {v.name: v for v in experiment.variants}
    return by_name["control"], by_name["treatment"]


def _seed(db_session, experiment, variant, n_users, n_converted):
    now_iso = datetime.now(timezone.utc).isoformat()
    prefix = f"{variant.name}-{uuid.uuid4().hex[:6]}"
    rows = []
    for i in range(n_users):
        user_id = f"{prefix}-{i:05d}"
        rows.append(
            Assignment(
                experiment_id=experiment.id, variant_id=variant.id, user_id=user_id
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


def _snapshots(db_session, experiment_id, kind=None):
    query = db_session.query(AnalysisSnapshot).filter(
        AnalysisSnapshot.experiment_id == experiment_id
    )
    if kind:
        query = query.filter(AnalysisSnapshot.kind == kind)
    return query.order_by(AnalysisSnapshot.created_at).all()


def _utc_days(rows):
    """The UTC calendar day of each row's ``as_of``.

    ``as_of`` is stored as an instant, but it comes back in the database
    session's time zone, not in UTC: with the session in a zone west of UTC,
    the UTC midnight a row is keyed on reads as an hour of the day before, so
    ``as_of.date()`` names the wrong day (#1218).
    """
    return {r.as_of.astimezone(timezone.utc).date() for r in rows}


@pytest.fixture
def local_time_zone(request):
    """Run the test with this process in the time zone ``request.param``."""
    zone = request.param
    previous = os.environ.get("TZ")
    os.environ["TZ"] = zone
    time.tzset()
    try:
        # The probe took: the process is not on UTC.
        assert time.localtime().tm_gmtoff != 0, zone
        yield zone
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


# ---------------------------------------------------------------------------
# GET /results/{id}: SRM + snapshots
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestResultsSrmAndSnapshots:
    def test_balanced_split_reports_no_srm_warning(
        self, admin_client, db_session, seeded_experiment
    ):
        control, treatment = _variants(seeded_experiment)
        _seed(db_session, seeded_experiment, control, 200, 20)
        _seed(db_session, seeded_experiment, treatment, 200, 30)

        response = admin_client.get(
            f"/api/v1/results/{seeded_experiment.id}", params={"use_cache": "false"}
        )

        assert response.status_code == 200, response.text
        srm = response.json()["srm"]
        assert srm is not None
        assert srm["warning"] is False
        assert srm["observed"] == {str(control.id): 200, str(treatment.id): 200}
        assert srm["expected"] == {str(control.id): 200.0, str(treatment.id): 200.0}
        assert srm["chi2"] == pytest.approx(0.0)

    def test_mismatched_split_raises_srm_warning(
        self, admin_client, db_session, seeded_experiment
    ):
        control, treatment = _variants(seeded_experiment)
        _seed(db_session, seeded_experiment, control, 600, 60)
        _seed(db_session, seeded_experiment, treatment, 400, 40)

        response = admin_client.get(
            f"/api/v1/results/{seeded_experiment.id}", params={"use_cache": "false"}
        )

        assert response.status_code == 200, response.text
        srm = response.json()["srm"]
        assert srm["warning"] is True
        assert srm["p_value"] < 0.001
        assert srm["chi2"] == pytest.approx(40.0)  # 2 * 100^2 / 500
        assert srm["observed"] == {str(control.id): 600, str(treatment.id): 400}

    def test_srm_null_before_any_assignment(self, admin_client, seeded_experiment):
        response = admin_client.get(
            f"/api/v1/results/{seeded_experiment.id}", params={"use_cache": "false"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["srm"] is None

    def test_results_call_writes_frequentist_and_bayesian_snapshots(
        self, admin_client, db_session, seeded_experiment
    ):
        control, treatment = _variants(seeded_experiment)
        _seed(db_session, seeded_experiment, control, 100, 10)
        _seed(db_session, seeded_experiment, treatment, 100, 15)
        assert _snapshots(db_session, seeded_experiment.id) == []

        response = admin_client.get(
            f"/api/v1/results/{seeded_experiment.id}", params={"use_cache": "false"}
        )
        assert response.status_code == 200, response.text
        body = response.json()

        db_session.expire_all()
        frequentist = _snapshots(db_session, seeded_experiment.id, "frequentist")
        bayesian = _snapshots(db_session, seeded_experiment.id, "bayesian")
        assert len(frequentist) == 1
        assert len(bayesian) == 1

        snap = frequentist[0]
        assert snap.engine_version == ENGINE_VERSION
        assert snap.seed is None and snap.n_samples is None
        assert snap.as_of is not None
        assert snap.payload["experiment_id"] == str(seeded_experiment.id)
        assert snap.payload["srm"] == body["srm"]
        assert snap.payload["metrics"][0]["variants"][0]["sample_size"] == 100

        bsnap = bayesian[0]
        assert bsnap.seed == body["bayesian_results"]["seed"]
        assert bsnap.n_samples == BAYESIAN_N_SAMPLES
        assert bsnap.payload["decision"] == body["bayesian_results"]["decision"]

        # A second fresh computation on the same day refreshes the same row
        # rather than appending (a polling dashboard must not grow the table).
        admin_client.get(
            f"/api/v1/results/{seeded_experiment.id}", params={"use_cache": "false"}
        )
        db_session.expire_all()
        assert len(_snapshots(db_session, seeded_experiment.id, "frequentist")) == 1

    def test_snapshot_failure_does_not_fail_results(
        self, admin_client, db_session, seeded_experiment
    ):
        control, treatment = _variants(seeded_experiment)
        _seed(db_session, seeded_experiment, control, 50, 5)
        _seed(db_session, seeded_experiment, treatment, 50, 5)

        with patch(
            "backend.app.services.analysis_snapshot_service.build_snapshot",
            side_effect=RuntimeError("snapshot table missing"),
        ):
            response = admin_client.get(
                f"/api/v1/results/{seeded_experiment.id}", params={"use_cache": "false"}
            )

        assert response.status_code == 200, response.text
        assert response.json()["experiment_id"] == str(seeded_experiment.id)
        db_session.expire_all()
        assert _snapshots(db_session, seeded_experiment.id) == []

    def test_bandit_experiment_reports_no_srm(
        self, admin_client, db_session, seeded_experiment
    ):
        """An adaptive experiment must not be flagged for its own reallocation.

        The same 600/400 split warns under ``optimization_type='fixed'``.
        """
        control, treatment = _variants(seeded_experiment)
        _seed(db_session, seeded_experiment, control, 600, 60)
        _seed(db_session, seeded_experiment, treatment, 400, 40)

        fixed = admin_client.get(
            f"/api/v1/results/{seeded_experiment.id}", params={"use_cache": "false"}
        ).json()["srm"]
        assert fixed is not None and fixed["warning"] is True

        seeded_experiment.optimization_type = "thompson_sampling"
        db_session.commit()

        response = admin_client.get(
            f"/api/v1/results/{seeded_experiment.id}", params={"use_cache": "false"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["srm"] is None

    def test_polling_the_results_page_does_not_grow_the_table(
        self, admin_client, db_session, seeded_experiment
    ):
        """N uncached requests in one day leave one row per kind."""
        control, treatment = _variants(seeded_experiment)
        _seed(db_session, seeded_experiment, control, 80, 8)
        _seed(db_session, seeded_experiment, treatment, 80, 12)

        for _ in range(5):
            assert (
                admin_client.get(
                    f"/api/v1/results/{seeded_experiment.id}",
                    params={"use_cache": "false"},
                ).status_code
                == 200
            )
            assert (
                admin_client.get(
                    f"/api/v1/results/{seeded_experiment.id}/bayesian"
                ).status_code
                == 200
            )
            assert (
                admin_client.get(
                    f"/api/v1/results/{seeded_experiment.id}/cuped"
                ).status_code
                == 200
            )

        db_session.expire_all()
        rows = _snapshots(db_session, seeded_experiment.id)
        assert len(rows) == 3
        assert {r.kind for r in rows} == {"frequentist", "bayesian", "cuped"}
        # Every row sits on the UTC day bucket, which is also the seed bucket.
        assert _utc_days(rows) == {datetime.now(timezone.utc).date()}

    @pytest.mark.regression
    @pytest.mark.parametrize(
        "local_time_zone", ["Etc/GMT+12", "Pacific/Kiritimati"], indirect=True
    )
    def test_the_day_bucket_is_the_utc_day_in_any_local_time_zone(
        self, admin_client, db_session, seeded_experiment, local_time_zone
    ):
        """A snapshot is keyed on the UTC day, whatever zone reads or writes it.

        The polling test above failed whenever the database session's time
        zone was west of UTC (#1218): it took ``as_of.date()`` in that zone.
        The keying itself is UTC.  Here both this process and the session
        that reads the rows are put in a zone whose date differs from UTC's:
        UTC-12 before 12:00 UTC, UTC+14 from 10:00 UTC.  Etc/GMT+12 is
        UTC-12 (the POSIX sign is inverted).
        """
        control, treatment = _variants(seeded_experiment)
        _seed(db_session, seeded_experiment, control, 80, 8)
        _seed(db_session, seeded_experiment, treatment, 80, 12)
        for path, params in (
            ("", {"use_cache": "false"}),
            ("/bayesian", {}),
            ("/cuped", {}),
        ):
            response = admin_client.get(
                f"/api/v1/results/{seeded_experiment.id}{path}", params=params
            )
            assert response.status_code == 200, response.text
        today = datetime.now(timezone.utc).date()

        # Read the rows in a session set to the same zone (until the
        # fixture's rollback ends this transaction).
        db_session.execute(
            text("SELECT set_config('TimeZone', :zone, true)"),
            {"zone": local_time_zone},
        )
        assert db_session.execute(text("SHOW TIME ZONE")).scalar() == local_time_zone
        db_session.expire_all()
        rows = _snapshots(db_session, seeded_experiment.id)

        assert {r.kind for r in rows} == {"frequentist", "bayesian", "cuped"}
        assert _utc_days(rows) == {today}
        utc_midnight = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        assert {r.as_of for r in rows} == {utc_midnight}

    def test_experiments_results_route_shares_srm(
        self, admin_client, db_session, seeded_experiment
    ):
        control, treatment = _variants(seeded_experiment)
        _seed(db_session, seeded_experiment, control, 600, 60)
        _seed(db_session, seeded_experiment, treatment, 400, 40)

        response = admin_client.get(
            f"/api/v1/experiments/{seeded_experiment.id}/results"
        )

        assert response.status_code == 200, response.text
        srm = response.json()["srm"]
        # Either a fresh computation or an earlier cached one for the same
        # experiment: both carry the srm block from the same data.
        assert srm is not None and srm["warning"] is True


# ---------------------------------------------------------------------------
# GET /results/{id}/bayesian: determinism
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestBayesianRouteDeterminism:
    def test_two_calls_are_byte_identical_with_same_seed(
        self, admin_client, db_session, seeded_experiment
    ):
        control, treatment = _variants(seeded_experiment)
        _seed(db_session, seeded_experiment, control, 300, 30)
        _seed(db_session, seeded_experiment, treatment, 300, 45)

        first = admin_client.get(f"/api/v1/results/{seeded_experiment.id}/bayesian")
        second = admin_client.get(f"/api/v1/results/{seeded_experiment.id}/bayesian")

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.content == second.content

        body = first.json()
        assert body["is_enabled"] is True
        assert isinstance(body["seed"], int) and body["seed"] >= 0
        assert body["n_samples"] == BAYESIAN_N_SAMPLES
        assert body["engine_version"] == ENGINE_VERSION
        assert len(body["variant_results"]) == 2
        posteriors = [v["posterior"] for v in body["variant_results"]]
        assert posteriors[0]["alpha"] == 31.0 and posteriors[0]["beta"] == 271.0

        db_session.expire_all()
        snaps = _snapshots(db_session, seeded_experiment.id, "bayesian")
        assert len(snaps) == 1  # one row per experiment, kind and UTC day
        assert {s.seed for s in snaps} == {body["seed"]}

    def test_bayesian_route_matches_embedded_block(
        self, admin_client, db_session, seeded_experiment
    ):
        control, treatment = _variants(seeded_experiment)
        _seed(db_session, seeded_experiment, control, 120, 12)
        _seed(db_session, seeded_experiment, treatment, 120, 20)

        standalone = admin_client.get(
            f"/api/v1/results/{seeded_experiment.id}/bayesian"
        ).json()
        embedded = admin_client.get(
            f"/api/v1/results/{seeded_experiment.id}", params={"use_cache": "false"}
        ).json()["bayesian_results"]

        assert standalone == embedded

    def test_bayesian_route_disabled_experiment(self, admin_client, make_experiment):
        experiment = make_experiment(
            name=f"No Bayes {uuid.uuid4().hex[:6]}", status=ExperimentStatus.ACTIVE
        )
        response = admin_client.get(f"/api/v1/results/{experiment.id}/bayesian")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["is_enabled"] is False
        assert body["seed"] is None
        assert body["engine_version"] == ENGINE_VERSION

    def test_bayesian_route_404_for_unknown(self, admin_client):
        response = admin_client.get(f"/api/v1/results/{uuid.uuid4()}/bayesian")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# CUPED / sequential snapshots
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestOtherAnalysisSnapshots:
    def test_cuped_route_writes_snapshot_with_engine_version(
        self, admin_client, db_session, seeded_experiment
    ):
        control, treatment = _variants(seeded_experiment)
        _seed(db_session, seeded_experiment, control, 40, 4)
        _seed(db_session, seeded_experiment, treatment, 40, 8)

        response = admin_client.get(f"/api/v1/results/{seeded_experiment.id}/cuped")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["engine_version"] == ENGINE_VERSION
        assert body["seed"] is None and body["n_samples"] is None

        db_session.expire_all()
        snaps = _snapshots(db_session, seeded_experiment.id, "cuped")
        assert len(snaps) == 1
        assert snaps[0].seed is None
        assert snaps[0].payload["method"] == body["method"]

    def test_sequential_route_writes_snapshot(
        self, admin_client, db_session, seeded_experiment
    ):
        seeded_experiment.sequential_testing_enabled = True
        seeded_experiment.sequential_testing_config = {"alpha": 0.05}
        db_session.commit()
        control, treatment = _variants(seeded_experiment)
        _seed(db_session, seeded_experiment, control, 60, 6)
        _seed(db_session, seeded_experiment, treatment, 60, 9)

        response = admin_client.get(
            f"/api/v1/results/{seeded_experiment.id}/sequential"
        )

        assert response.status_code == 200, response.text
        db_session.expire_all()
        snaps = _snapshots(db_session, seeded_experiment.id, "sequential")
        assert len(snaps) == 1
        assert snaps[0].payload["method"] == response.json()["method"]
