"""What ``GET /api/v1/results/{id}/sequential`` decides, read through the route.

The real ``SequentialTestingService`` runs behind the route; only the two
database reads are replaced, with known per-arm counts (as in
``test_sequential_contract.py``). Each test pins what the response says for
those counts:

* the mSPRT is two-sided: a treatment worse than the control crosses the
  boundary 1/alpha exactly as a better one does, and the sign of the
  confidence sequence is the direction;
* 10% against 13% and against 7% at 10,000 users per arm, with their intervals;
* the evidence strengths short of a stop, either way, answer ``continue``,
  ``strong_for_null`` included;
* the analysis's own alpha, and not the experiment's confidence level or
  correction method, sets the boundary;
* the configuration's range for ``tau_squared`` and ``planned_looks``, and
  the ``tau_squared`` the analysis uses when none is stored.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.api.deps import get_current_user, get_db
from backend.app.main import app
from backend.app.models.user import User
from backend.app.schemas.experiment import SequentialTestingConfigInput

pytestmark = pytest.mark.unit

EXPERIMENT_UUID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
URL = f"/api/v1/results/{EXPERIMENT_UUID}/sequential"

#: 10% against 13%, and 10% against 7%, at 10,000 users per arm.
BETTER = (1_000, 10_000, 1_300, 10_000)
WORSE = (1_000, 10_000, 700, 10_000)


@pytest.fixture
def client():
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.is_active = True
    user.is_superuser = True
    user.role = "ADMIN"

    def override_get_db():
        yield MagicMock()

    async def override_get_current_user():
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides = {}


def _experiment(
    config: Optional[Dict[str, Any]] = None,
    confidence_level: float = 0.95,
    correction_method: str = "benjamini_hochberg",
) -> MagicMock:
    experiment = MagicMock()
    experiment.sequential_testing_enabled = True
    experiment.sequential_testing_method = "msprt"
    experiment.sequential_testing_config = config
    experiment.confidence_level = confidence_level
    experiment.correction_method = correction_method
    experiment.start_date = datetime.now(timezone.utc) - timedelta(days=3)
    return experiment


def _get(client, counts, experiment=None, params=None) -> Dict[str, Any]:
    with (
        patch(
            "backend.app.api.v1.endpoints.results._get_experiment_for_sequential",
            return_value=experiment or _experiment(),
        ),
        patch(
            "backend.app.api.v1.endpoints.results._get_sequential_data",
            return_value=counts,
        ),
    ):
        response = client.get(URL, params=params or {})
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Two-sided: either direction stops, and the interval says which
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_worse_treatment_stops_for_effect_as_a_better_one_does(client):
    for counts, sign in ((BETTER, 1), (WORSE, -1)):
        body = _get(client, counts)
        msprt = body["msprt_result"]
        assert msprt["lambda_ratio"] >= msprt["boundary"] == 20.0, counts
        assert msprt["can_stop"] is True
        assert msprt["evidence_strength"] == "strong_for_effect"
        assert body["recommended_action"] == "stop_for_effect"
        interval = body["confidence_sequence"]
        # The direction is the sign of the interval, which excludes 0.
        assert sign * interval["lower"] > 0 and sign * interval["upper"] > 0


@pytest.mark.regression
def test_ten_against_thirteen_and_seven_percent_give_the_pages_intervals(client):
    better = _get(client, BETTER)["confidence_sequence"]
    worse = _get(client, WORSE)["confidence_sequence"]
    assert (round(better["lower"], 4), round(better["upper"], 4)) == (0.0157, 0.0443)
    assert (round(worse["lower"], 4), round(worse["upper"], 4)) == (-0.0427, -0.0173)
    # Not mirror images: the variance is larger at 13% than at 7%.
    assert better["width"] > worse["width"]


def test_the_boundary_is_one_over_alpha_in_either_direction(client):
    """Just past 1/alpha stops; just short of it does not, whichever way."""
    # 10% against 14.5% and 6.2% at 1,000 per arm: Lambda 20.41 and 24.25.
    for counts in ((100, 1_000, 145, 1_000), (100, 1_000, 62, 1_000)):
        msprt = _get(client, counts)["msprt_result"]
        assert msprt["lambda_ratio"] >= 20.0 and msprt["can_stop"] is True, counts
    # 10% against 14.4%: Lambda 19.6, below the boundary.
    body = _get(client, (100, 1_000, 144, 1_000))
    assert body["msprt_result"]["lambda_ratio"] < 20.0
    assert body["recommended_action"] == "continue"


# ---------------------------------------------------------------------------
# The evidence strengths short of a stop
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "counts",
    [
        (100, 1_000, 140, 1_000),  # 10% against 14%: Lambda 9.54
        (100, 1_000, 66, 1_000),  # 10% against 6.6%: Lambda 9.70
    ],
)
def test_moderate_for_effect_is_either_direction_and_continues(client, counts):
    body = _get(client, counts)
    assert body["msprt_result"]["evidence_strength"] == "moderate_for_effect"
    assert body["msprt_result"]["can_stop"] is False
    assert body["recommended_action"] == "continue"


def test_moderate_for_null_is_equal_rates_short_of_strong(client):
    # 10% in both arms at 50,000 users: Lambda 0.060, between 1/20 and 3/20.
    body = _get(client, (5_000, 50_000, 5_000, 50_000))
    assert body["msprt_result"]["evidence_strength"] == "moderate_for_null"
    assert body["recommended_action"] == "continue"


@pytest.mark.regression
def test_strong_for_null_is_not_a_stopping_rule(client):
    # 10% in both arms at 100,000 users: Lambda 0.042, below 1/20.
    body = _get(client, (10_000, 100_000, 10_000, 100_000))
    assert body["msprt_result"]["evidence_strength"] == "strong_for_null"
    assert body["msprt_result"]["can_stop"] is False
    assert body["recommended_action"] == "continue"


# ---------------------------------------------------------------------------
# Its own alpha
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "confidence_level, correction_method",
    [(0.99, "bonferroni"), (0.80, "none"), (0.90, "benjamini_hochberg")],
)
def test_the_stored_confidence_level_and_correction_do_not_set_the_boundary(
    client, confidence_level, correction_method
):
    experiment = _experiment(
        config=None,
        confidence_level=confidence_level,
        correction_method=correction_method,
    )
    # 10% against 14.5% at 1,000 per arm stops at alpha 0.05 (Lambda 20.4);
    # at 0.99 confidence (alpha 0.01, boundary 100) it would not.
    body = _get(client, (100, 1_000, 145, 1_000), experiment=experiment)
    assert body["msprt_result"]["boundary"] == 20.0
    assert body["recommended_action"] == "stop_for_effect"

    experiment.sequential_testing_config = {"alpha": 0.1}
    body = _get(client, (100, 1_000, 145, 1_000), experiment=experiment)
    assert body["msprt_result"]["boundary"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# The configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0.0, -0.001, 1.0001, 2.0])
def test_tau_squared_outside_above_zero_and_at_most_one_is_refused(bad):
    with pytest.raises(ValidationError) as caught:
        SequentialTestingConfigInput(tau_squared=bad)
    assert caught.value.errors()[0]["loc"] == ("tau_squared",)


@pytest.mark.parametrize("good", [1e-9, 0.001, 0.5, 1.0])
def test_tau_squared_up_to_one_is_accepted(good):
    assert SequentialTestingConfigInput(tau_squared=good).tau_squared == good


@pytest.mark.regression
def test_with_no_tau_squared_stored_the_analysis_uses_0001(client):
    """The interval with no stored tau^2 is the one at tau^2 = 0.001.

    1,000 of 10,000 against 1,000 of 10,000: V = 9010001/500300060004, and the
    normal-mixture half-width at tau^2 0.001 and alpha 0.05 is
    0.013557851680358 (test_sequential_confidence_sequence.py); at tau^2 0.01
    it would be about 0.0128.
    """
    counts = (1_000, 10_000, 1_000, 10_000)
    for config in (None, {}, {"alpha": 0.05}):
        interval = _get(client, counts, experiment=_experiment(config))[
            "confidence_sequence"
        ]
        assert interval["width"] / 2 == pytest.approx(0.013557851680358, rel=1e-9)
    stored = _get(client, counts, experiment=_experiment({"tau_squared": 0.01}))
    assert not math.isclose(
        stored["confidence_sequence"]["width"] / 2, 0.013557851680358, rel_tol=1e-3
    )


@pytest.mark.parametrize("bad", [0, -1, 101, 1000])
def test_planned_looks_outside_one_to_a_hundred_is_refused(bad):
    with pytest.raises(ValidationError) as caught:
        SequentialTestingConfigInput(planned_looks=bad)
    assert caught.value.errors()[0]["loc"] == ("planned_looks",)


@pytest.mark.parametrize("looks", [1, 5, 100])
def test_planned_looks_are_accepted_and_change_nothing(client, looks):
    assert SequentialTestingConfigInput(planned_looks=looks).planned_looks == looks
    counts = (100, 1_000, 145, 1_000)
    base = _get(client, counts, experiment=_experiment({}))
    with_looks = _get(client, counts, experiment=_experiment({"planned_looks": looks}))
    assert with_looks["alpha_spending"] == []
    assert with_looks["msprt_result"] == base["msprt_result"]
    assert with_looks["confidence_sequence"] == base["confidence_sequence"]
