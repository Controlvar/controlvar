"""The guided setup's estimate and ``/utils`` use the results page's formula.

Before #685, ``GET /api/v1/experiments/analysis/sample-size`` (the guided
setup's Estimate step) and ``POST /api/v1/utils/utils/sample-size`` each had
their own copy of an unpooled two-proportion formula, while the results page's
Sample Size tab and ``PowerCalculatorService`` use the pooled one (pooled
variance under the null, unpooled under the alternative). At the documented
12% baseline / 5% relative MDE the routes answered 47,034 per variant and the
tab 47,036. Both routes now call ``sample_size_two_proportions``, the helper the
service's own sample-size path calls, with two arms and no multiple-comparison
correction, and multiply by the number of variants for the total.

They also refuse, with a 422 in the guided setup's own words, a treatment rate
of 100% or more; the shared formula would otherwise take the square root of a
negative variance and answer 500.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.user import User, UserRole
from backend.app.services.power_calculator_service import (
    TREATMENT_RATE_CEILING_MESSAGE,
    PowerCalculatorService,
    sample_size_two_proportions,
)

WIZARD_URL = "/api/v1/experiments/analysis/sample-size"
UTILS_URL = "/api/v1/utils/utils/sample-size"
REPO_ROOT = Path(__file__).resolve().parents[4]
ESTIMATE_TS = (
    REPO_ROOT
    / "frontend"
    / "src"
    / "components"
    / "experiments"
    / "new"
    / "estimate.ts"
)

BASELINE = 0.12
RELATIVE_MDE = 0.05

# The guided setup's Significance and Power choices.
SIGNIFICANCE_OPTIONS = (0.01, 0.05, 0.10)
POWER_OPTIONS = (0.80, 0.90, 0.95)


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
        # 500 a caller would see, so the refusal tests can tell 422 from 500.
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides = {}


def ask_wizard(client, **params):
    return client.get(WIZARD_URL, params=params)


def ask_utils(client, variant_count=2, **body):
    return client.post(UTILS_URL, params={"variant_count": variant_count}, json=body)


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize("alpha", SIGNIFICANCE_OPTIONS)
@pytest.mark.parametrize("power", POWER_OPTIONS)
@pytest.mark.parametrize("variant_count", (2, 3))
def test_both_routes_agree_with_the_service(client, alpha, power, variant_count):
    """Every option the guided setup offers, at two and three variants."""
    helper = sample_size_two_proportions(
        p1=BASELINE,
        p2=BASELINE + BASELINE * RELATIVE_MDE,
        alpha=alpha,
        power=power,
        two_tailed=True,
    )
    # The service's public path, at two variants (where it applies no
    # Bonferroni correction), is the same number.
    service = PowerCalculatorService().compute_sample_size(
        baseline_rate=BASELINE,
        minimum_detectable_effect=RELATIVE_MDE,
        alpha=alpha,
        power=power,
        n_variants=2,
    )
    assert service.per_variant == helper

    wizard = ask_wizard(
        client,
        baseline_rate=BASELINE,
        minimum_detectable_effect=RELATIVE_MDE,
        significance_level=alpha,
        statistical_power=power,
        variant_count=variant_count,
    )
    utils = ask_utils(
        client,
        variant_count=variant_count,
        baseline_rate=BASELINE,
        minimum_detectable_effect=RELATIVE_MDE,
        significance_level=alpha,
        statistical_power=power,
    )
    for response in (wizard, utils):
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["samples_per_variant"] == helper
        assert body["total_samples"] == helper * variant_count


@pytest.mark.unit
@pytest.mark.regression
def test_documented_case_on_both_routes(client):
    wizard = ask_wizard(client, baseline_rate=0.12, minimum_detectable_effect=0.05)
    utils = ask_utils(client, baseline_rate=0.12, minimum_detectable_effect=0.05)
    for response in (wizard, utils):
        assert response.status_code == 200, response.text
        assert response.json()["samples_per_variant"] == 47036
        assert response.json()["total_samples"] == 94072


@pytest.mark.unit
@pytest.mark.regression
def test_an_mde_above_one_is_still_answered(client):
    """A 10% baseline and a +200% effect (10% -> 30%) is a valid API input;
    guided setup refuses an effect of 100% or more since #1257. The service's ``compute_sample_size`` refuses an MDE of 1 or more;
    these routes do not go through that check."""
    wizard = ask_wizard(client, baseline_rate=0.10, minimum_detectable_effect=2.0)
    utils = ask_utils(client, baseline_rate=0.10, minimum_detectable_effect=2.0)
    for response in (wizard, utils):
        assert response.status_code == 200, response.text
        assert response.json()["samples_per_variant"] == 62
        assert response.json()["total_samples"] == 124


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize(
    ("baseline", "mde"),
    [
        (0.5, 1.5),  # 125%
        (0.5, 1.0),  # exactly 100%
        (0.9, 0.2),  # 108%
    ],
)
def test_a_treatment_rate_of_100_percent_or_more_is_refused(client, baseline, mde):
    wizard = ask_wizard(client, baseline_rate=baseline, minimum_detectable_effect=mde)
    utils = ask_utils(client, baseline_rate=baseline, minimum_detectable_effect=mde)
    for response in (wizard, utils):
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == TREATMENT_RATE_CEILING_MESSAGE


@pytest.mark.unit
def test_the_refusal_uses_the_guided_setups_own_words():
    """The 422 says what the Estimate step says before it sends the request."""
    source = ESTIMATE_TS.read_text()
    match = re.search(r"ceiling:\s*'([^']*)'", source)
    assert match, f"no `ceiling` problem in {ESTIMATE_TS}"
    assert match.group(1) == TREATMENT_RATE_CEILING_MESSAGE
