"""``GET /api/v1/power/curve`` answers 15 points, one per relative effect size.

The power-analysis page shows three of them for ``baseline=0.05``, ``mde=0.10``
and says the example is abridged. This holds the count, the sizes and the
three shown points as the route answers them.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.unit, pytest.mark.regression]

SIZES = [
    0.01, 0.02, 0.03, 0.05, 0.07, 0.10, 0.12, 0.15,
    0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50,
]  # fmt: skip


@pytest.fixture
def client():
    user = MagicMock(spec=User)
    user.id = 1
    user.role = UserRole.ANALYST
    user.is_active = True
    user.is_superuser = False
    app.dependency_overrides[deps.get_current_active_user] = lambda: user
    app.dependency_overrides[deps.get_current_user] = lambda: user
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides = {}


def test_the_curve_has_fifteen_points_and_the_three_the_page_shows(client):
    resp = client.get(
        "/api/v1/power/curve",
        params={"baseline": 0.05, "alpha": 0.05, "power": 0.80, "mde": 0.10},
    )
    assert resp.status_code == 200, resp.text
    points = resp.json()["points"]
    assert len(points) == 15
    assert [p["effect_size_relative"] for p in points] == pytest.approx(SIZES)
    shown = {
        p["effect_size_relative"]: (
            p["sample_size_per_variant"],
            p["is_current_target"],
        )
        for p in points
        if p["effect_size_relative"] in (0.01, 0.10, 0.50)
    }
    assert shown == {
        0.01: (2_996_698, False),
        0.10: (31_234, True),
        0.50: (1_471, False),
    }
    assert sum(p["is_current_target"] for p in points) == 1
