"""``POST /api/v1/power/sample-size`` plans a mean from its real mean (#1034).

A ``mean`` or ``ratio`` metric's ``baseline_rate`` is the metric's own baseline
mean (an average order value of 45), and any finite value above 0 is planned.
Before the fix the schema refused every baseline of 1 or more for every metric
type, and the service refused a mean whose raised value reached 1, so a mean
such as 45 could not be planned at all and an accepted mean could still answer
``per_variant: 0`` (a standard deviation of 1e-200) or leak Python's own
"cannot convert float NaN to integer" as the message.

No database: the route is anonymous and the calculation is closed-form.
"""

import math

import pytest
from fastapi.testclient import TestClient

from backend.app.api.v1.endpoints.power_calculator import OUT_OF_RANGE_MESSAGE
from backend.app.main import app
from backend.app.services.power_calculator_service import PowerCalculatorService

URL = "/api/v1/power/sample-size"


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=True)


def _mean_body(**overrides):
    body = {
        "baseline_rate": 45.0,
        "minimum_detectable_effect": 0.05,
        "metric_type": "mean",
        "baseline_std": 12.0,
    }
    body.update(overrides)
    return body


def _raw(client, text):
    """POST a body as written (JSON cannot carry Infinity or NaN from a dict)."""
    return client.post(URL, content=text, headers={"content-type": "application/json"})


@pytest.mark.unit
@pytest.mark.regression
def test_a_mean_above_one_is_planned_in_its_own_units(client):
    """A mean of 45, sd 12, effect 5% answers 200: 447 per variant, 894 in all."""
    response = client.post(URL, json=_mean_body())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["per_variant"] == 447
    assert body["total"] == 894
    assert body["baseline_rate"] == 45.0
    assert body["mde_absolute"] == pytest.approx(2.25)
    assert body["metric_type"] == "mean"


@pytest.mark.unit
@pytest.mark.regression
def test_the_mean_answer_agrees_with_statsmodels(client):
    """The 447 is the ceiling of statsmodels' 446.51 (effect size 2.25 / 12)."""
    from statsmodels.stats.power import NormalIndPower

    exact = NormalIndPower().solve_power(
        effect_size=2.25 / 12.0, alpha=0.05, power=0.8, ratio=1
    )
    assert math.ceil(exact) == 447
    assert client.post(URL, json=_mean_body()).json()["per_variant"] == 447


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize(
    ("overrides", "per_variant"),
    [
        pytest.param(
            {"metric_type": "ratio", "baseline_rate": 3.2, "baseline_std": 1.1},
            742,
            id="ratio-3.2",
        ),
        pytest.param({"baseline_rate": 1e9, "baseline_std": 1e8}, 63, id="mean-1e9"),
    ],
)
def test_a_ratio_is_planned_as_a_mean_and_a_large_mean_has_no_ceiling(
    client, overrides, per_variant
):
    response = client.post(URL, json=_mean_body(**overrides))
    assert response.status_code == 200, response.text
    assert response.json()["per_variant"] == per_variant


@pytest.mark.unit
@pytest.mark.parametrize("mean", [0.45, 0.96, 1, 1.5, 4500])
def test_a_mean_is_accepted_at_any_size_above_zero(client, mean):
    """0.96 raised by 5% is 1.008: a rate could not, a mean can."""
    response = client.post(URL, json=_mean_body(baseline_rate=mean))
    assert response.status_code == 200, response.text
    assert response.json()["per_variant"] >= 1


@pytest.mark.unit
@pytest.mark.parametrize("mean", [0, -5])
def test_a_mean_of_zero_or_below_is_refused_at_the_field(client, mean):
    response = client.post(URL, json=_mean_body(baseline_rate=mean))
    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["type"] == "greater_than"
    assert detail["loc"] == ["body", "baseline_rate"]


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {
                "baseline_rate": 1.0,
                "minimum_detectable_effect": 0.05,
                "metric_type": "proportion",
            },
            id="one",
        ),
        pytest.param(
            {
                "baseline_rate": 1.5,
                "minimum_detectable_effect": 0.05,
                "metric_type": "proportion",
            },
            id="one-and-a-half",
        ),
        pytest.param(
            {"baseline_rate": 45.0, "minimum_detectable_effect": 0.05},
            id="forty-five-no-metric-type",
        ),
    ],
)
def test_a_proportion_of_one_or_more_is_refused_by_the_schema_naming_metric_type(
    client, body
):
    """The refusal is the schema's: a ``value_error`` at ``["body"]`` whose
    message says to send metric_type 'mean'. A service-only refusal would be a
    422 with a string ``detail``, which the status-only tests cannot tell apart."""
    response = client.post(URL, json=body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["type"] == "value_error"
    assert detail[0]["loc"] == ["body"]
    assert "send metric_type 'mean' and baseline_std" in detail[0]["msg"]
    assert "must be below 1" in detail[0]["msg"]


@pytest.mark.unit
@pytest.mark.regression
def test_a_proportion_whose_treatment_reaches_one_is_refused_by_the_schema(client):
    body = {
        "baseline_rate": 0.96,
        "minimum_detectable_effect": 0.05,
        "metric_type": "proportion",
    }
    response = client.post(URL, json=body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["type"] == "value_error"
    assert detail[0]["loc"] == ["body"]
    assert "raised by minimum_detectable_effect (0.05) is 1.0080" in detail[0]["msg"]
    assert "mde_absolute" not in detail[0]["msg"]


@pytest.mark.unit
@pytest.mark.parametrize("rate", [0.05, 0.5, 0.95])
def test_a_proportion_below_one_is_still_planned(client, rate):
    body = {"baseline_rate": rate, "minimum_detectable_effect": 0.01}
    assert client.post(URL, json=body).status_code == 200


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize("metric_type", ["proportion", "mean"])
def test_an_infinite_baseline_is_refused_as_not_finite(client, metric_type):
    """Infinity is the one input that tests ``allow_inf_nan=False`` (NaN is
    refused by ``gt=0`` regardless): without it the 422 comes from a lower
    layer with a string ``detail``."""
    text = (
        '{"baseline_rate": Infinity, "minimum_detectable_effect": 0.05, '
        f'"metric_type": "{metric_type}", "baseline_std": 12}}'
    )
    response = _raw(client, text)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["type"] == "finite_number"
    assert detail[0]["loc"] == ["body", "baseline_rate"]


@pytest.mark.unit
def test_a_nan_baseline_is_refused_at_the_field(client):
    text = (
        '{"baseline_rate": NaN, "minimum_detectable_effect": 0.05, '
        '"metric_type": "mean", "baseline_std": 12}'
    )
    response = _raw(client, text)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["loc"] == ["body", "baseline_rate"]


# Each answered 200 with per_variant 0, or a 422 carrying Python's own text,
# on main. PB-6: the last two are the finite inputs main accepted that leaked
# "cannot convert float NaN to integer" and "required_sample_size must be > 0".
NO_SIZE = [
    pytest.param(
        '{"baseline_rate": 0.5, "minimum_detectable_effect": 0.05, '
        '"metric_type": "mean", "baseline_std": 1e-200}',
        id="sd-1e-200",
    ),
    pytest.param(
        '{"baseline_rate": 1.5e308, "minimum_detectable_effect": 0.5, '
        '"metric_type": "mean", "baseline_std": 1}',
        id="mean-1.5e308",
    ),
    pytest.param(
        '{"baseline_rate": 1.5e308, "minimum_detectable_effect": 0.5, '
        '"metric_type": "mean", "baseline_std": Infinity}',
        id="mean-1.5e308-sd-infinity",
    ),
    pytest.param(
        '{"baseline_rate": 1e-320, "minimum_detectable_effect": 0.5, '
        '"alpha": 5e-324, "power": 5e-324, "n_variants": 67, '
        '"metric_type": "ratio", "baseline_std": 4.383710408960138e-277}',
        id="finite-nan-leak",
    ),
    pytest.param(
        '{"baseline_rate": 4.317423882736378e-111, '
        '"minimum_detectable_effect": 0.05, "alpha": 0.49371400099587454, '
        '"n_variants": 49, "metric_type": "ratio", '
        '"baseline_std": 4.718523116705284e-258, '
        '"daily_traffic": 956863149060, "traffic_allocation": 5e-324}',
        id="finite-must-be-above-zero-leak",
    ),
]


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize("text", NO_SIZE)
def test_a_size_that_is_zero_or_not_finite_answers_the_fixed_422(client, text):
    response = _raw(client, text)
    assert response.status_code == 422
    if "Infinity" in text:
        # Since #1630 an infinite number in a JSON body is refused before the
        # route runs, at its field; still a 422.
        assert response.json() == {
            "detail": [
                {
                    "type": "finite_number",
                    "loc": ["body", "baseline_std"],
                    "msg": "Input should be a finite number",
                }
            ]
        }
        return
    assert response.json() == {"detail": OUT_OF_RANGE_MESSAGE}


@pytest.mark.unit
@pytest.mark.regression
def test_the_service_plans_a_mean_and_still_refuses_a_proportion_of_one_or_more():
    service = PowerCalculatorService()
    result = service.compute_sample_size(
        baseline_rate=45.0,
        minimum_detectable_effect=0.05,
        metric_type="mean",
        baseline_std=12.0,
    )
    assert (result.per_variant, result.total) == (447, 894)
    with pytest.raises(ValueError, match="send metric_type 'mean' and baseline_std"):
        service.compute_sample_size(baseline_rate=1.5, minimum_detectable_effect=0.05)
    with pytest.raises(ValueError, match="must stay below 1"):
        service.compute_sample_size(baseline_rate=0.96, minimum_detectable_effect=0.05)


@pytest.mark.unit
def test_mde_ignores_a_metric_type_and_says_so(client):
    """``/power/mde`` plans proportions only; its description now says so."""
    description = client.get("/api/v1/openapi.json").json()["paths"][
        "/api/v1/power/mde"
    ]["post"]["description"]
    assert "a metric_type sent with the request is ignored" in description
