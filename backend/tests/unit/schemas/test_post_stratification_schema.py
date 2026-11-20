"""The post-stratification request's defaults and bounds (#963).

The route is left out of the API document, so the document no longer carries
these two rows of the request table in docs/statistics/post-stratification.md:
``metric_col`` defaults to ``"metric_value"`` and ``alpha`` defaults to 0.05
and is exclusive of 0 and 1. This is what holds them.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.schemas.post_stratification import PostStratificationRequest

pytestmark = pytest.mark.unit


def test_metric_col_defaults_to_metric_value():
    assert PostStratificationRequest(stratum_cols=["s"]).metric_col == "metric_value"


def test_alpha_defaults_to_0_05():
    assert PostStratificationRequest(stratum_cols=["s"]).alpha == 0.05


@pytest.mark.parametrize("alpha", [0.0, 1.0])
def test_alpha_is_exclusive_of_0_and_1(alpha):
    with pytest.raises(ValidationError):
        PostStratificationRequest(stratum_cols=["s"], alpha=alpha)
