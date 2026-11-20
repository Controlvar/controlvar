"""A flag evaluation hands no targeting context to the metrics it records.

``FeatureFlagService.evaluate_flag_detailed`` used to pass
``metadata={"context": context}`` to ``MetricsService.record_flag_evaluation``,
which dropped it (see ``test_metrics_service.py``). The context is the user's
attributes as the client sent them; the evaluation records the flag, the user
id, the matched rule and the latency, and nothing from the context.
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest

from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.services.feature_flag_service import FeatureFlagService

pytestmark = [pytest.mark.unit, pytest.mark.regression]

MARKER = "ctx-marker-7f3a"


@pytest.mark.parametrize(
    ("status", "rollout"),
    [(FeatureFlagStatus.ACTIVE, 100), (FeatureFlagStatus.INACTIVE, 100)],
    ids=["active", "inactive"],
)
def test_the_evaluation_records_nothing_from_the_context(status, rollout):
    flag = FeatureFlag(
        id=uuid.uuid4(),
        key="no-context",
        name="No context",
        status=status,
        rollout_percentage=rollout,
        owner_id=uuid.uuid4(),
    )
    service = FeatureFlagService(MagicMock())
    with patch(
        "backend.app.services.feature_flag_service.MetricsService.record_flag_evaluation"
    ) as recorded:
        service.evaluate_flag_detailed(
            flag, "u-1", {"email": f"{MARKER}@example.com", "plan": MARKER}
        )

    assert recorded.call_count == 1
    kwargs = recorded.call_args.kwargs
    assert "metadata" not in kwargs, kwargs
    assert MARKER not in repr(recorded.call_args), recorded.call_args
