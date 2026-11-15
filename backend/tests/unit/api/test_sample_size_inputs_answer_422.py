"""Sample-size inputs that cannot be calculated with answer 422, not 500 (#958).

``GET /api/v1/ai/sample-size`` put its query straight into the normal
approximation. A NaN, a zero effect, a confidence or power of 1, or an effect
so small that its square underflows reached ``math.ceil`` as NaN or infinity
and answered 500; a treatment rate (``baseline_rate + mde``) above 1 or below 0
answered 200 with a size that is not one (-10, 7, 0). Each now answers 422
with fixed text, checked in this order:

1. ``baseline_rate``, ``confidence`` or ``power`` NaN or outside 0 to 1:
   ``<param> must be a number from 0 to 1``;
2. ``mde`` of 0: ``SAMPLE_SIZE_NOT_FINITE_MESSAGE``;
3. a treatment rate above 1: ``TREATMENT_RATE_CEILING_MESSAGE``; below 0:
   ``OUT_OF_RANGE_MESSAGE``;
4. a size that is not a finite number above 0, or days that leave
   floating-point range: ``OUT_OF_RANGE_MESSAGE``.

``POST /api/v1/utils/utils/sample-size`` divided the daily traffic by the
allocation and the variant count with no check: an allocation so small that a
variant's users per day come to 0 (``5e-324``), days that are not finite
(``1e-310``), or a ``daily_traffic`` or ``variant_count`` too large for a float
answered 500. It now answers 422 with ``OUT_OF_RANGE_MESSAGE``, as its twin
``GET /api/v1/experiments/analysis/sample-size`` does (#820).

Inputs that were answered with a size keep their size. No refusal repeats a
value sent.

Since #1742 a number parameter of either operation (``baseline_rate``,
``mde``, ``confidence``, ``power``; the body's four rates and
``traffic_allocation``) that is NaN, an infinity or too large for a double is
refused before the handler sees it: 422 with ``finite_number`` at that
parameter, where one of the fixed texts answered it before. The integer
parameters keep their own answers. The rows that sent one moved to
``test_the_ai_sample_size_refuses_a_non_finite_parameter_at_it``.
"""

import math
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.api.v1.endpoints.power_calculator import OUT_OF_RANGE_MESSAGE
from backend.app.core.finite_json import FINITE_NUMBER_MESSAGE, FINITE_NUMBER_TYPE
from backend.app.main import app
from backend.app.models.user import User, UserRole
from backend.app.services.power_calculator_service import (
    SAMPLE_SIZE_NOT_FINITE_MESSAGE,
    TREATMENT_RATE_CEILING_MESSAGE,
)

pytestmark = [pytest.mark.unit]

AI_URL = "/api/v1/ai/sample-size"
UTILS_URL = "/api/v1/utils/utils/sample-size"


def _range(name: str) -> str:
    return f"{name} must be a number from 0 to 1"


#: Every text a refusal may carry: fixed, so none repeats a value sent.
FIXED_TEXTS = {
    _range("baseline_rate"),
    _range("confidence"),
    _range("power"),
    SAMPLE_SIZE_NOT_FINITE_MESSAGE,
    TREATMENT_RATE_CEILING_MESSAGE,
    OUT_OF_RANGE_MESSAGE,
}


@pytest.fixture
def client():
    user = MagicMock(spec=User)
    user.id = 1
    user.username = "viewer"
    user.email = "viewer@example.com"
    user.role = UserRole.VIEWER
    user.is_active = True
    user.is_superuser = False
    user.hashed_password = "hashed"

    app.dependency_overrides[deps.get_current_active_user] = lambda: user
    app.dependency_overrides[deps.get_current_user] = lambda: user
    try:
        # raise_server_exceptions=False: an unhandled error must surface as the
        # 500 a caller would see, so these tests can tell 422 from 500.
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides = {}


def _ai(client, baseline, mde, **extra):
    return client.get(AI_URL, params={"baseline_rate": baseline, "mde": mde, **extra})


# (baseline_rate, mde, other query parameters, the 422's text). On 695e51c7 the
# first group answered 500 and the second 200 with a size.
AI_REFUSED = [
    # 500 before
    pytest.param(0, 0, {}, SAMPLE_SIZE_NOT_FINITE_MESSAGE, id="0-0"),
    pytest.param(0.1, 0, {}, SAMPLE_SIZE_NOT_FINITE_MESSAGE, id="mde-0"),
    pytest.param(0.1, 0.05, {"confidence": 1}, OUT_OF_RANGE_MESSAGE, id="confidence-1"),
    pytest.param(0.1, 0.05, {"power": 1}, OUT_OF_RANGE_MESSAGE, id="power-1"),
    pytest.param(0.1, 0.05, {"power": 0}, OUT_OF_RANGE_MESSAGE, id="power-0"),
    pytest.param(0.1, 1e-300, {}, OUT_OF_RANGE_MESSAGE, id="mde-1e-300"),
    pytest.param(0.5, 1e-160, {}, OUT_OF_RANGE_MESSAGE, id="mde-1e-160"),
    pytest.param(0.1, 0.05, {"confidence": 1.5}, _range("confidence"), id="conf-1.5"),
    pytest.param(0.1, 0.05, {"power": -0.5}, _range("power"), id="power-minus"),
    pytest.param(1e-300, 1e-300, {"daily_traffic": 1}, OUT_OF_RANGE_MESSAGE, id="tiny"),
    pytest.param(
        0.5, 1.8e-154, {"daily_traffic": 1}, OUT_OF_RANGE_MESSAGE, id="days-overflow"
    ),
    # 200 with a size before
    pytest.param(
        0.9, 0.5, {}, TREATMENT_RATE_CEILING_MESSAGE, id="0.9-0.5-was-minus-10"
    ),
    pytest.param(0.5, 0.6, {}, TREATMENT_RATE_CEILING_MESSAGE, id="0.5-0.6-was-7"),
    pytest.param(0.1, -0.2, {}, OUT_OF_RANGE_MESSAGE, id="0.1-minus-0.2-was-0"),
    pytest.param(-0.1, 0.05, {}, _range("baseline_rate"), id="baseline-minus-was-506"),
    pytest.param(-0.1, 0.3, {}, _range("baseline_rate"), id="baseline-minus-was-9"),
    pytest.param(1.5, 0.05, {}, _range("baseline_rate"), id="baseline-1.5"),
    pytest.param(
        0.1, 0.05, {"confidence": -0.5}, _range("confidence"), id="conf-minus"
    ),
    pytest.param(
        0.1, 0.05, {"confidence": 0, "power": 0.5}, OUT_OF_RANGE_MESSAGE, id="size-0"
    ),
]


@pytest.mark.regression
@pytest.mark.parametrize(("baseline", "mde", "extra", "text"), AI_REFUSED)
def test_the_ai_sample_size_refuses_with_fixed_text(client, baseline, mde, extra, text):
    response = _ai(client, baseline, mde, **extra)
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": text}


def _not_finite_at(*names: str) -> dict:
    return {
        "detail": [
            {
                "type": FINITE_NUMBER_TYPE,
                "loc": ["query", name],
                "msg": FINITE_NUMBER_MESSAGE,
            }
            for name in names
        ]
    }


# (baseline_rate, mde, other query parameters, the parameter refused). These
# were AI_REFUSED rows answered with a fixed text; since #1742 the parameter
# itself is refused, before the handler.
AI_NOT_FINITE = [
    pytest.param("nan", 0.05, {}, "baseline_rate", id="baseline-nan"),
    pytest.param(0.1, "nan", {}, "mde", id="mde-nan"),
    pytest.param(0.1, 0.05, {"confidence": "nan"}, "confidence", id="conf-nan"),
    pytest.param(0.1, 0.05, {"power": "nan"}, "power", id="power-nan"),
    pytest.param(0.1, "inf", {}, "mde", id="mde-inf"),
    pytest.param(0.1, "-inf", {}, "mde", id="mde-minus-inf"),
]


@pytest.mark.regression
@pytest.mark.parametrize(("baseline", "mde", "extra", "name"), AI_NOT_FINITE)
def test_the_ai_sample_size_refuses_a_non_finite_parameter_at_it(
    client, baseline, mde, extra, name
):
    response = _ai(client, baseline, mde, **extra)
    assert response.status_code == 422, response.text
    assert response.json() == _not_finite_at(name)


# Answers 695e51c7 gave, unchanged: a baseline of 0 or 1, a confidence of 0, a
# treatment rate of exactly 0 or 1, and negative effects inside the range.
AI_ANSWERED = [
    pytest.param(0, 0.1, {}, 75, None, id="baseline-0"),
    pytest.param(1.0, -0.1, {}, 75, None, id="baseline-1"),
    pytest.param(0.1, 0.05, {"confidence": 0}, 62, None, id="confidence-0"),
    pytest.param(0.9, 0.1, {}, 75, None, id="treatment-exactly-1"),
    pytest.param(0.1, -0.1, {}, 75, None, id="treatment-exactly-0"),
    pytest.param(0.2, -0.05, {}, 907, None, id="negative-mde"),
    pytest.param(0.1, 0.05, {}, 687, None, id="plain"),
    pytest.param(0.1, 0.05, {"daily_traffic": 1}, 687, 1374, id="one-user-a-day"),
    pytest.param(0.08, 0.004, {"daily_traffic": 10000}, 73855, 15, id="documented"),
]


@pytest.mark.parametrize(("baseline", "mde", "extra", "size", "days"), AI_ANSWERED)
def test_ai_answers_that_worked_do_not_change(client, baseline, mde, extra, size, days):
    response = _ai(client, baseline, mde, **extra)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["required_per_variant"] == size
    assert body["total_required"] == 2 * size
    assert body["days_to_significance"] == days


def test_the_largest_sizes_are_still_answered(client):
    """Just inside the refusals: a size near the top of floating-point range,
    with and without days, is still a 200 and a positive whole number."""
    for params in (
        {"mde": 1.8e-154},
        {"mde": 1e-150, "daily_traffic": 1},
    ):
        response = client.get(AI_URL, params={"baseline_rate": 0.5, **params})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["required_per_variant"] > 10**290
        if "daily_traffic" in params:
            assert body["days_to_significance"] > body["required_per_variant"]


GRID = [
    "nan",
    "-inf",
    -1.0,
    -0.1,
    -1e-300,
    0,
    1e-300,
    1e-160,
    1e-17,
    0.05,
    0.5,
    0.95,
    1,
    1.5,
    "inf",
]


#: The grid's values that are not finite, and the order the route declares its
#: parameters in, which is the order their 422 errors come in.
NOT_FINITE = {"nan", "inf", "-inf"}
AI_PARAMETERS = ("baseline_rate", "mde", "confidence", "power")


@pytest.mark.regression
def test_no_input_on_the_grid_answers_5xx_or_a_size_below_1(client):
    """baseline_rate x mde, then confidence and power, over the grid: every
    answer is a 200 with a size of at least 1, or a 422 with one of the fixed
    texts. Since #1742 a request with a value that is not finite answers 422
    with ``finite_number`` at each such parameter instead."""
    requests = [{"baseline_rate": b, "mde": m} for b in GRID for m in GRID]
    requests += [
        {"baseline_rate": 0.1, "mde": 0.05, "confidence": c, "power": p}
        for c in GRID
        for p in GRID
    ]
    for params in requests:
        response = client.get(AI_URL, params=params)
        assert response.status_code in (200, 422), (params, response.status_code)
        body = response.json()
        not_finite = [name for name in AI_PARAMETERS if params.get(name) in NOT_FINITE]
        if not_finite:
            assert response.status_code == 422, (params, body)
            assert body == _not_finite_at(*not_finite), (params, body)
        elif response.status_code == 422:
            assert body["detail"] in FIXED_TEXTS, (params, body)
        else:
            assert body["required_per_variant"] >= 1, (params, body)


# ---------------------------------------------------------------------------
# POST /api/v1/utils/utils/sample-size
# ---------------------------------------------------------------------------

UTILS_BODY = {"baseline_rate": 0.1, "minimum_detectable_effect": 0.15}


def _utils(client, **query):
    return client.post(UTILS_URL, params=query, json=UTILS_BODY)


@pytest.mark.regression
@pytest.mark.parametrize(
    "query",
    [
        pytest.param({"daily_traffic": 1, "traffic_allocation": 5e-324}, id="5e-324"),
        pytest.param({"daily_traffic": 1, "traffic_allocation": 1e-310}, id="1e-310"),
        pytest.param({"daily_traffic": 10**400, "traffic_allocation": 1}, id="traffic"),
        pytest.param(
            {"daily_traffic": 1, "traffic_allocation": 1, "variant_count": 10**400},
            id="variants",
        ),
    ],
)
def test_the_utils_day_arithmetic_refuses_with_the_out_of_range_text(client, query):
    response = _utils(client, **query)
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": OUT_OF_RANGE_MESSAGE}


def test_utils_answers_that_worked_do_not_change(client):
    response = _utils(client, daily_traffic=1000, traffic_allocation=0.5)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["samples_per_variant"] == 6693
    assert body["estimated_duration_days"] == {"days": 27, "weeks": 4, "months": 1}

    # Days near the top of floating-point range are still answered.
    response = _utils(client, daily_traffic=1, traffic_allocation=1e-300)
    assert response.status_code == 200, response.text
    days = response.json()["estimated_duration_days"]["days"]
    assert days == math.ceil(6693 / (1e-300 / 2))

    # Without days to count, a huge variant count is only multiplied.
    response = _utils(client, variant_count=10**400)
    assert response.status_code == 200, response.text
    assert response.json()["total_samples"] == 6693 * 10**400
