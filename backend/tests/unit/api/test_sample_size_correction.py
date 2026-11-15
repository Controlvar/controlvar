"""The new-experiment estimate plans each comparison with the experiment's correction (#820).

``GET /api/v1/experiments/analysis/sample-size`` (guided setup's Estimate step)
planned every comparison with the control at ``significance_level``, whatever
correction the experiment had; FastAPI dropped an unknown ``correction_method``
and answered 47,036 at 3 variants, 95% and Benjamini-Hochberg, where the
results page's Sample Size tab plans 56,961. Now the route takes
``correction_method`` (``none``, the default, ``bonferroni`` or
``benjamini_hochberg``, the tab's pattern) and, with either correction, plans
each of the ``variant_count - 1`` comparisons at
``significance_level / (variant_count - 1)``, as the tab does. The answer
echoes ``correction_method`` as sent, ``comparisons`` and ``alpha`` (each
comparison's level); ``significance_level`` still echoes the level sent.

``variant_count`` takes at most 100 and ``daily_traffic`` at most 10**12 (the
power routes' bounds, #1003): without the first, a 309-digit count with a
correction would be a 500 the fix creates. The duration answers the fixed
out-of-range 422 when the users per variant per day come to 0 or the day
count is not finite; those answered 500.

Every expected size is a literal at baseline 0.12, relative effect 0.05,
power 0.8, and each is checked again here against statsmodels'
``samplesize_proportions_2indep_onetail``, which imports nothing from the
product. No database.
"""

import math
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from statsmodels.stats.proportion import samplesize_proportions_2indep_onetail

from backend.app.api import deps
from backend.app.api.v1.endpoints.power_calculator import OUT_OF_RANGE_MESSAGE
from backend.app.main import app
from backend.app.models.user import User, UserRole

WIZARD_URL = "/api/v1/experiments/analysis/sample-size"
BASE = {"baseline_rate": 0.12, "minimum_detectable_effect": 0.05}


@pytest.fixture
def client():
    user = MagicMock(spec=User)
    user.id = 1
    user.username = "analyst"
    user.email = "analyst@example.com"
    user.role = UserRole.ANALYST
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


def oracle(alpha: float, one_sided: bool) -> int:
    """statsmodels' size for 12% -> 12.6% at 80% power, rounded up."""
    return math.ceil(
        samplesize_proportions_2indep_onetail(
            diff=0.126 - 0.12,
            prop2=0.12,
            power=0.8,
            ratio=1,
            alpha=alpha,
            alternative="larger" if one_sided else "two-sided",
        )
    )


# (correction_method, variant_count, significance_level, is_one_sided, size).
# 90% at 4 variants and the one-sided case are here so that no planted defect
# hides behind a coincidence (90% at 3 corrected == 95% at 2 == 47,036).
CORRECTED = [
    ("bonferroni", 2, 0.05, False, 47036),
    ("bonferroni", 3, 0.05, False, 56961),
    ("bonferroni", 4, 0.05, False, 62739),
    ("bonferroni", 5, 0.05, False, 66826),
    ("benjamini_hochberg", 2, 0.05, False, 47036),
    ("benjamini_hochberg", 3, 0.05, False, 56961),
    ("benjamini_hochberg", 4, 0.05, False, 62739),
    ("benjamini_hochberg", 5, 0.05, False, 66826),
    ("benjamini_hochberg", 3, 0.01, False, 79780),
    ("benjamini_hochberg", 4, 0.1, False, 52850),
    ("none", 3, 0.05, False, 47036),
    ("benjamini_hochberg", 3, 0.05, True, 47036),
    ("benjamini_hochberg", 100, 0.05, False, 111823),
]


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize(
    ("method", "variants", "sig", "one_sided", "size"),
    [
        pytest.param(*c, id=f"{c[0]}-{c[1]}-{c[2]}{'-one-sided' if c[3] else ''}")
        for c in CORRECTED
    ],
)
def test_the_estimate_plans_each_comparison_at_the_corrected_level(
    client, method, variants, sig, one_sided, size
):
    response = client.get(
        WIZARD_URL,
        params={
            **BASE,
            "significance_level": sig,
            "variant_count": variants,
            "is_one_sided": one_sided,
            "correction_method": method,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    per_comparison = sig if method == "none" else sig / (variants - 1)
    assert oracle(per_comparison, one_sided) == size
    assert body["samples_per_variant"] == size
    assert body["total_samples"] == size * variants
    assert body["correction_method"] == method
    assert body["comparisons"] == variants - 1
    assert body["alpha"] == pytest.approx(per_comparison, rel=1e-12)
    assert body["significance_level"] == sig


@pytest.mark.unit
@pytest.mark.regression
def test_bonferroni_and_benjamini_hochberg_give_the_same_estimate(client):
    """Benjamini-Hochberg is planned at Bonferroni's level, as the tab plans it."""
    for variants in (3, 4, 5):
        answers = {
            method: client.get(
                WIZARD_URL,
                params={**BASE, "variant_count": variants, "correction_method": method},
            ).json()["samples_per_variant"]
            for method in ("bonferroni", "benjamini_hochberg")
        }
        assert answers["bonferroni"] == answers["benjamini_hochberg"], variants


@pytest.mark.unit
@pytest.mark.regression
def test_without_correction_method_the_estimate_is_uncorrected(client):
    """A request without the parameter answers as before, and says so."""
    response = client.get(WIZARD_URL, params={**BASE, "variant_count": 3})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["samples_per_variant"] == 47036
    assert body["total_samples"] == 141108
    assert body["correction_method"] == "none"
    assert body["comparisons"] == 2
    assert body["alpha"] == 0.05
    assert body["significance_level"] == 0.05


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize("method", ["holm", "BH", "Bonferroni", ""])
def test_an_unknown_correction_is_refused(client, method):
    """Main dropped it and planned 47,036; ``holm`` and ``""`` would otherwise
    be planned as a correction by a ``!= "none"`` test."""
    response = client.get(
        WIZARD_URL, params={**BASE, "variant_count": 3, "correction_method": method}
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert [(d["type"], d["loc"]) for d in detail] == [
        ("string_pattern_mismatch", ["query", "correction_method"])
    ]


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"variant_count": 101}, id="101"),
        pytest.param(
            {
                "variant_count": "1" + "0" * 309,
                "correction_method": "benjamini_hochberg",
            },
            id="10e309-bh",
        ),
        pytest.param(
            {
                "variant_count": "1" + "0" * 309,
                "daily_traffic": 1000,
                "traffic_allocation": 1,
            },
            id="10e309-traffic",
        ),
    ],
)
def test_more_than_100_variants_is_refused(client, params):
    response = client.get(WIZARD_URL, params={**BASE, **params})

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert [(d["type"], d["loc"], d["ctx"]) for d in detail] == [
        ("less_than_equal", ["query", "variant_count"], {"le": 100})
    ]


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize(
    "daily_traffic",
    [
        pytest.param(10**12 + 1, id="10e12+1"),
        pytest.param("1" + "0" * 309, id="10e309"),
    ],
)
def test_more_than_10_to_the_12_daily_users_is_refused(client, daily_traffic):
    response = client.get(
        WIZARD_URL,
        params={**BASE, "daily_traffic": daily_traffic, "traffic_allocation": 1},
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert [(d["type"], d["loc"], d["ctx"]) for d in detail] == [
        ("less_than_equal", ["query", "daily_traffic"], {"le": 10**12})
    ]


@pytest.mark.unit
def test_the_largest_counts_are_still_planned(client):
    """100 variants and 10**12 daily users are inside the bounds."""
    response = client.get(
        WIZARD_URL,
        params={
            **BASE,
            "variant_count": 100,
            "correction_method": "benjamini_hochberg",
            "daily_traffic": 10**12,
            "traffic_allocation": 1,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["samples_per_variant"] == 111823
    assert body["estimated_duration_days"] == {"days": 1, "weeks": 1, "months": 1}


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize(
    "params",
    [
        pytest.param(
            {**BASE, "daily_traffic": 1, "traffic_allocation": "5e-324"},
            id="share-5e-324",
        ),
        pytest.param(
            {**BASE, "daily_traffic": 1, "traffic_allocation": "1e-310"},
            id="share-1e-310",
        ),
        pytest.param(
            {
                "baseline_rate": "1e-150",
                "minimum_detectable_effect": 0.5,
                "daily_traffic": 1,
                "traffic_allocation": "1e-300",
            },
            id="baseline-1e-150-share-1e-300",
        ),
    ],
)
def test_a_duration_that_cannot_be_calculated_answers_the_fixed_422(client, params):
    """Each answered 500 on main: users per variant per day of 0 (a
    ZeroDivisionError), or a day count that is not finite."""
    response = client.get(WIZARD_URL, params=params)

    assert response.status_code == 422, response.text
    assert response.json() == {"detail": OUT_OF_RANGE_MESSAGE}
