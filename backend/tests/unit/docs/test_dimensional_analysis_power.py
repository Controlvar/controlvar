"""The dimensional analysis guide makes no claim about power it cannot back.

It said segments under about 200 users per variant "will rarely show
significance even with large effects". A 10% to 20% conversion lift at 200 per
variant has a z statistic near 2.8, about 80% power at the 5% level, so the
sentence was false for large effects. The page now says only that small
segments have low power. A wording test: it holds the page to that and to the
arithmetic above, not to any one model of the service.
"""

from __future__ import annotations

from math import sqrt
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

PAGE = Path(__file__).resolve().parents[4] / "docs/api/dimensional-analysis.md"


def _page() -> str:
    return " ".join(PAGE.read_text(encoding="utf-8").split())


def test_the_page_does_not_say_large_effects_rarely_reach_significance():
    text = _page()
    assert "rarely show significance" not in text
    assert "~200 per variant" not in text
    assert (
        "have low power: only large effects reach significance, and the Bonferroni"
        " threshold makes that harder."
    ) in text


def test_a_large_effect_at_200_per_variant_is_detected():
    n, p1, p2 = 200, 0.10, 0.20
    pooled = (p1 + p2) / 2
    z = (p2 - p1) / sqrt(2 * pooled * (1 - pooled) / n)
    assert z > 2.5
