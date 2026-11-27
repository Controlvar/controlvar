"""The visual editor session's answer offers no setting the editor cannot
save yet (#1545, V60).

``capabilities`` tells the editor which settings beyond the design's own
fields it may offer. Both are off: the split's balance (#448) is not wired
to the editor, and no visitor country is collected. The answer is built
from an unsaved row, so no database is needed: no exclusion group, and no
user, so no holdout lookup.

Planted defect: ``CAPABILITIES["balance_within"] = True`` fails
``capabilities.balance_within: True != False``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    Metric,
    Variant,
)
from backend.app.models.experiment import MetricType as RowMetricType
from backend.app.schemas.visual_editor import EditorCapabilities, EditorSessionState
from backend.app.services import visual_editor

pytestmark = pytest.mark.unit

NOW = datetime(2031, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
PAGE = {"match": "prefix", "url": "https://shop.example.com/"}


def _draft() -> Experiment:
    experiment_id = uuid.uuid4()
    experiment = Experiment(
        id=experiment_id,
        name="Draft",
        key="draft",
        experiment_type="a_b",
        status=ExperimentStatus.DRAFT,
        correction_method="benjamini_hochberg",
        confidence_level=0.95,
        created_at=NOW,
        updated_at=NOW,
        visual_config={
            "version": 1,
            "editor_url": "https://shop.example.com/",
            "pages": [PAGE],
            "goals": [],
        },
    )
    experiment.variants = [
        Variant(
            id=uuid.uuid4(),
            experiment_id=experiment_id,
            name=name,
            is_control=name == "Control",
            traffic_allocation=50,
            created_at=NOW,
            updated_at=NOW,
        )
        for name in ("Control", "B")
    ]
    experiment.metric_definitions = [
        Metric(
            id=uuid.uuid4(),
            experiment_id=experiment_id,
            name="Buy",
            event_name="click_buy",
            metric_type=RowMetricType.CONVERSION,
            is_primary=True,
            aggregation_method="average",
            minimum_sample_size=100,
            lower_is_better=False,
            created_at=NOW,
            updated_at=NOW,
        )
    ]
    return experiment


@pytest.mark.regression
def test_the_answer_offers_neither_the_balance_nor_the_country():
    state = EditorSessionState(**visual_editor.session_state(None, _draft(), NOW, None))

    shown = state.model_dump(mode="json")["experiment"]["capabilities"]
    assert set(shown) == {"balance_within", "country"}, sorted(shown)
    for name, value in shown.items():
        assert value is False, f"capabilities.{name}: {value} != False"
    assert set(EditorCapabilities.model_fields) == {"balance_within", "country"}


def test_without_a_group_or_a_user_the_answer_has_neither():
    state = EditorSessionState(**visual_editor.session_state(None, _draft(), NOW, None))

    assert state.experiment.mutual_exclusion_group is None
    assert state.experiment.holdout is None
    assert [v.traffic_allocation for v in state.experiment.variants] == [50, 50]
