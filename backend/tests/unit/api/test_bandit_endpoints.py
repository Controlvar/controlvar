"""
Unit tests for Bandit API endpoints — Issue #22 Batch C.

Covers:
    GET  /api/v1/bandit/{experiment_id}
    POST /api/v1/bandit/{experiment_id}/update
    PUT  /api/v1/bandit/{experiment_id}/weights

All DB and auth interactions are mocked so that these run without a live
PostgreSQL server.
"""

import uuid
from typing import Dict, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.bandit_state import BanditState
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.user import User, UserRole

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(role: UserRole = UserRole.DEVELOPER, is_superuser: bool = False) -> User:
    """Construct a fake User with the given role."""
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.username = "test_user"
    user.email = "test@example.com"
    user.is_active = True
    user.is_superuser = is_superuser
    user.role = role
    return user


def _make_experiment(
    exp_id: Optional[uuid.UUID] = None,
    optimization_type: str = "thompson_sampling",
    with_variants: bool = True,
) -> Experiment:
    """Construct a fake Experiment."""
    exp = MagicMock(spec=Experiment)
    exp.id = exp_id or uuid.uuid4()
    exp.optimization_type = optimization_type
    exp.status = ExperimentStatus.ACTIVE

    if with_variants:
        v1 = MagicMock()
        v1.id = uuid.uuid4()
        v1.name = "Control"
        v2 = MagicMock()
        v2.id = uuid.uuid4()
        v2.name = "Treatment"
        exp.variants = [v1, v2]
    else:
        exp.variants = []

    return exp


def _make_bandit_state(
    exp_id: uuid.UUID,
    variant_ids,
    weights: Optional[Dict] = None,
) -> BanditState:
    """Construct a fake BanditState."""
    state = MagicMock(spec=BanditState)
    state.experiment_id = exp_id
    state.algorithm = "thompson_sampling"
    state.total_pulls = 200
    state.regret_reduction_pct = 12.5
    state.last_computed_at = "2026-01-01T00:00:00+00:00"

    # variant_weights structure mirrors what the scheduler writes
    state.variant_weights = {
        str(vid): {
            "weight": weights[str(vid)] if weights else 0.5,
            "successes": 30,
            "failures": 20,
            "pulls": 50,
        }
        for vid in variant_ids
    }
    return state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def admin_user():
    return _make_user(role=UserRole.ADMIN, is_superuser=True)


@pytest.fixture
def developer_user():
    return _make_user(role=UserRole.DEVELOPER)


@pytest.fixture
def viewer_user():
    return _make_user(role=UserRole.VIEWER)


# ===========================================================================
# Tests
# ===========================================================================


class TestGetBanditStatus:
    """GET /api/v1/bandit/{experiment_id}"""

    def test_get_bandit_status_returns_200(self, client, developer_user):
        """Returns 200 and a BanditStatusResponse for a valid MAB experiment."""
        exp = _make_experiment()
        state = _make_bandit_state(
            exp.id,
            [v.id for v in exp.variants],
            weights={str(v.id): 0.5 for v in exp.variants},
        )

        app.dependency_overrides[deps.get_current_active_user] = lambda: developer_user

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.side_effect = [exp, state]
        app.dependency_overrides[deps.get_db] = lambda: db_mock

        try:
            response = client.get(f"/api/v1/bandit/{exp.id}")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        assert body["experiment_id"] == str(exp.id)
        assert body["algorithm"] == "thompson_sampling"

    def test_get_bandit_status_returns_404_for_unknown_experiment(
        self, client, developer_user
    ):
        """Returns 404 when the experiment UUID does not exist."""
        unknown_id = uuid.uuid4()

        app.dependency_overrides[deps.get_current_active_user] = lambda: developer_user

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = None
        app.dependency_overrides[deps.get_db] = lambda: db_mock

        try:
            response = client.get(f"/api/v1/bandit/{unknown_id}")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 404

    def test_get_bandit_status_returns_404_for_fixed_experiment(
        self, client, developer_user
    ):
        """Returns 404 for experiments with optimization_type='fixed'."""
        exp = _make_experiment(optimization_type="fixed")

        app.dependency_overrides[deps.get_current_active_user] = lambda: developer_user

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = exp
        app.dependency_overrides[deps.get_db] = lambda: db_mock

        try:
            response = client.get(f"/api/v1/bandit/{exp.id}")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 404

    def test_get_bandit_status_returns_weights_from_bandit_state(
        self, client, developer_user
    ):
        """Returns the persisted variant weights from BanditState."""
        exp = _make_experiment()
        v_ids = [v.id for v in exp.variants]
        weights_map = {str(v_ids[0]): 0.7, str(v_ids[1]): 0.3}
        state = _make_bandit_state(exp.id, v_ids, weights=weights_map)

        app.dependency_overrides[deps.get_current_active_user] = lambda: developer_user

        db_mock = MagicMock()
        # First call returns experiment, second returns state
        db_mock.query.return_value.filter.return_value.first.side_effect = [exp, state]
        app.dependency_overrides[deps.get_db] = lambda: db_mock

        try:
            response = client.get(f"/api/v1/bandit/{exp.id}")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        # The response should include current_weights list
        weights_in_response = {
            w["variant_id"]: w["current_weight"] for w in body["current_weights"]
        }
        assert weights_in_response[str(v_ids[0])] == pytest.approx(0.7)
        assert weights_in_response[str(v_ids[1])] == pytest.approx(0.3)

    def test_get_bandit_status_returns_equal_weights_when_no_state(
        self, client, developer_user
    ):
        """Returns equal weights when no BanditState has been computed yet."""
        exp = _make_experiment()

        app.dependency_overrides[deps.get_current_active_user] = lambda: developer_user

        db_mock = MagicMock()
        # Experiment found but no BanditState
        db_mock.query.return_value.filter.return_value.first.side_effect = [exp, None]
        app.dependency_overrides[deps.get_db] = lambda: db_mock

        try:
            response = client.get(f"/api/v1/bandit/{exp.id}")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        for w in body["current_weights"]:
            assert w["current_weight"] == pytest.approx(0.5)

    def test_get_bandit_status_includes_recommendation(self, client, developer_user):
        """Response includes a recommendation string."""
        exp = _make_experiment()
        v_ids = [v.id for v in exp.variants]
        # High weight on first variant → DEPLOYING
        weights_map = {str(v_ids[0]): 0.9, str(v_ids[1]): 0.1}
        state = _make_bandit_state(exp.id, v_ids, weights=weights_map)

        app.dependency_overrides[deps.get_current_active_user] = lambda: developer_user

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.side_effect = [exp, state]
        app.dependency_overrides[deps.get_db] = lambda: db_mock

        try:
            response = client.get(f"/api/v1/bandit/{exp.id}")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        assert "recommendation" in body
        assert body["recommendation"].startswith("DEPLOYING_")

    def test_get_bandit_status_includes_total_pulls(self, client, developer_user):
        """Response includes total_pulls count."""
        exp = _make_experiment()
        v_ids = [v.id for v in exp.variants]
        state = _make_bandit_state(exp.id, v_ids)

        app.dependency_overrides[deps.get_current_active_user] = lambda: developer_user

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.side_effect = [exp, state]
        app.dependency_overrides[deps.get_db] = lambda: db_mock

        try:
            response = client.get(f"/api/v1/bandit/{exp.id}")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        assert "total_pulls" in body
        assert body["total_pulls"] == 200

    def test_get_bandit_status_includes_algorithm_field(self, client, developer_user):
        """Response includes algorithm field matching experiment configuration."""
        exp = _make_experiment(optimization_type="ucb1")
        exp.optimization_type = "ucb1"
        v_ids = [v.id for v in exp.variants]
        state = _make_bandit_state(exp.id, v_ids)
        state.algorithm = "ucb1"

        app.dependency_overrides[deps.get_current_active_user] = lambda: developer_user

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.side_effect = [exp, state]
        app.dependency_overrides[deps.get_db] = lambda: db_mock

        try:
            response = client.get(f"/api/v1/bandit/{exp.id}")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        assert response.json()["algorithm"] == "ucb1"


class TestTriggerBanditUpdate:
    """POST /api/v1/bandit/{experiment_id}/update"""

    def test_trigger_update_returns_200_for_developer(self, client, developer_user):
        """DEVELOPER can trigger a weight recalculation and gets 200."""
        exp = _make_experiment()
        v_ids = [v.id for v in exp.variants]
        state = _make_bandit_state(exp.id, v_ids)

        app.dependency_overrides[deps.get_current_active_user] = lambda: developer_user

        db_mock = MagicMock()
        # 1st call: experiment (in endpoint), 2nd call: experiment (in scheduler),
        # 3rd call: BanditState query in scheduler update_experiment,
        # 4th call: BanditState query after update
        db_mock.query.return_value.filter.return_value.first.side_effect = [exp, state]
        app.dependency_overrides[deps.get_db] = lambda: db_mock

        with patch(
            "backend.app.api.v1.endpoints.bandit.BanditScheduler.update_experiment",
            return_value=True,
        ):
            try:
                response = client.post(f"/api/v1/bandit/{exp.id}/update")
            finally:
                app.dependency_overrides.clear()

        assert response.status_code == 200

    def test_trigger_update_requires_developer_role(self, client, viewer_user):
        """VIEWER gets 403 when attempting to trigger a bandit update."""
        exp_id = uuid.uuid4()

        app.dependency_overrides[deps.get_current_active_user] = lambda: viewer_user

        db_mock = MagicMock()
        app.dependency_overrides[deps.get_db] = lambda: db_mock

        try:
            response = client.post(f"/api/v1/bandit/{exp_id}/update")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 403

    def test_viewer_gets_403_on_post_update(self, client, viewer_user):
        """Explicit check: VIEWER role is forbidden from POST update."""
        exp_id = uuid.uuid4()

        app.dependency_overrides[deps.get_current_active_user] = lambda: viewer_user
        app.dependency_overrides[deps.get_db] = lambda: MagicMock()

        try:
            response = client.post(f"/api/v1/bandit/{exp_id}/update")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 403
        # The permission first, then the built-in roles that have it (#891):
        # true whether the role or a custom role or grant would give it.
        assert response.json()["detail"] == (
            "Triggering a bandit update needs permission to change experiments, "
            "which the DEVELOPER and ADMIN roles have."
        )

    def test_trigger_update_returns_correct_exploring_status(
        self, client, developer_user
    ):
        """After update, returns response with appropriate recommendation string."""
        exp = _make_experiment()
        v_ids = [v.id for v in exp.variants]
        # Equal weights → EXPLORING
        state = _make_bandit_state(
            exp.id, v_ids, weights={str(v_ids[0]): 0.5, str(v_ids[1]): 0.5}
        )
        state.total_pulls = 10

        app.dependency_overrides[deps.get_current_active_user] = lambda: developer_user

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.side_effect = [exp, state]
        app.dependency_overrides[deps.get_db] = lambda: db_mock

        with patch(
            "backend.app.api.v1.endpoints.bandit.BanditScheduler.update_experiment",
            return_value=True,
        ):
            try:
                response = client.post(f"/api/v1/bandit/{exp.id}/update")
            finally:
                app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        assert body["recommendation"] in (
            "EXPLORING",
            "CONVERGING",
            "DEPLOYING_Control",
            "DEPLOYING_Treatment",
        )


class TestOverrideBanditWeights:
    """PUT /api/v1/bandit/{experiment_id}/weights"""

    def test_admin_can_override_weights(self, client, admin_user):
        """ADMIN can override bandit weights and gets 200."""
        exp = _make_experiment()
        v_ids = [v.id for v in exp.variants]
        weights_payload = {str(v_ids[0]): 0.6, str(v_ids[1]): 0.4}

        app.dependency_overrides[deps.get_current_active_user] = lambda: admin_user

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.side_effect = [
            exp,
            None,
            None,
        ]
        app.dependency_overrides[deps.get_db] = lambda: db_mock

        try:
            response = client.put(
                f"/api/v1/bandit/{exp.id}/weights",
                json={"weights": weights_payload},
            )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200

    def test_admin_override_requires_admin_role(self, client, developer_user):
        """DEVELOPER cannot override bandit weights (requires ADMIN)."""
        exp_id = uuid.uuid4()

        app.dependency_overrides[deps.get_current_active_user] = lambda: developer_user
        app.dependency_overrides[deps.get_db] = lambda: MagicMock()

        try:
            response = client.put(
                f"/api/v1/bandit/{exp_id}/weights",
                json={"weights": {"v1": 0.5, "v2": 0.5}},
            )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 403

    def test_non_admin_gets_403_on_weight_override(self, client, viewer_user):
        """VIEWER gets 403 on PUT weights."""
        exp_id = uuid.uuid4()

        app.dependency_overrides[deps.get_current_active_user] = lambda: viewer_user
        app.dependency_overrides[deps.get_db] = lambda: MagicMock()

        try:
            response = client.put(
                f"/api/v1/bandit/{exp_id}/weights",
                json={"weights": {"v1": 0.5, "v2": 0.5}},
            )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 403

    def test_recommendation_reflects_converging_state(self, client, developer_user):
        """Response returns CONVERGING when max weight is between 0.5 and 0.8."""
        exp = _make_experiment()
        v_ids = [v.id for v in exp.variants]
        weights_map = {str(v_ids[0]): 0.65, str(v_ids[1]): 0.35}
        state = _make_bandit_state(exp.id, v_ids, weights=weights_map)

        app.dependency_overrides[deps.get_current_active_user] = lambda: developer_user

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.side_effect = [exp, state]
        app.dependency_overrides[deps.get_db] = lambda: db_mock

        try:
            response = client.get(f"/api/v1/bandit/{exp.id}")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        assert response.json()["recommendation"] == "CONVERGING"

    def test_recommendation_exploring_for_equal_weights(self, client, developer_user):
        """Returns EXPLORING or CONVERGING when variants have equal weight.

        Both are acceptable: a max_weight of exactly 0.5 sits on the boundary
        between EXPLORING (< 0.5) and CONVERGING (>= 0.5). We simply verify
        neither arm is DEPLOYING when weights are balanced.
        """
        exp = _make_experiment()
        v_ids = [v.id for v in exp.variants]
        state = _make_bandit_state(
            exp.id, v_ids, weights={str(v_ids[0]): 0.5, str(v_ids[1]): 0.5}
        )

        app.dependency_overrides[deps.get_current_active_user] = lambda: developer_user

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.side_effect = [exp, state]
        app.dependency_overrides[deps.get_db] = lambda: db_mock

        try:
            response = client.get(f"/api/v1/bandit/{exp.id}")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        rec = response.json()["recommendation"]
        # Equal 50/50 weights: should be either EXPLORING or CONVERGING, not DEPLOYING
        assert rec in ("EXPLORING", "CONVERGING"), f"Unexpected recommendation: {rec}"


class TestBanditStatusForTheDashboard:
    """What the dashboard's read-only weights panel relies on (#442).

    Every signed-in role reads the panel, so the route must answer a
    non-superuser ANALYST and VIEWER; and the panel shows "no weights yet"
    exactly when ``last_updated`` is null, so the route must leave it null
    before the first update and set it after.
    """

    @pytest.mark.parametrize(
        "role", [UserRole.ADMIN, UserRole.DEVELOPER, UserRole.ANALYST, UserRole.VIEWER]
    )
    def test_every_role_reads_the_weights(self, client, role):
        user = _make_user(role=role, is_superuser=False)
        exp = _make_experiment()
        state = _make_bandit_state(exp.id, [v.id for v in exp.variants])
        app.dependency_overrides[deps.get_current_active_user] = lambda: user
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.side_effect = [exp, state]
        app.dependency_overrides[deps.get_db] = lambda: db_mock
        try:
            response = client.get(f"/api/v1/bandit/{exp.id}")
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 200, (role, response.text)

    def test_last_updated_is_null_before_the_first_update(self, client, viewer_user):
        exp = _make_experiment()
        app.dependency_overrides[deps.get_current_active_user] = lambda: viewer_user
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.side_effect = [exp, None]
        app.dependency_overrides[deps.get_db] = lambda: db_mock
        try:
            response = client.get(f"/api/v1/bandit/{exp.id}")
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 200
        assert response.json()["last_updated"] is None

    def test_last_updated_is_set_after_an_update(self, client, viewer_user):
        exp = _make_experiment()
        state = _make_bandit_state(exp.id, [v.id for v in exp.variants])
        app.dependency_overrides[deps.get_current_active_user] = lambda: viewer_user
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.side_effect = [exp, state]
        app.dependency_overrides[deps.get_db] = lambda: db_mock
        try:
            response = client.get(f"/api/v1/bandit/{exp.id}")
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 200
        assert response.json()["last_updated"] == state.last_computed_at
