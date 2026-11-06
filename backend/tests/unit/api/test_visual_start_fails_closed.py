"""A visual draft's stored values are read as held, so an unknown one fails closed (#1545).

Only ``None`` reads as absent. A ``visual_config`` of any other type -- a
model object a later caller might pass instead of the dict a JSONB column
holds, or an attribute of a test double -- counts as visual settings: the
experiment is visual, ``/start`` asks for its design etag before anything
else, and the start check refuses a value that fails its schema.

Planted defect: the earlier read, which took a value of a type no JSONB
column holds for absent, makes such a draft non-visual, and ``/start`` then
skips the etag check (``DESIGN_ETAG_MISSING`` is never answered, and the
draft goes on to the other start checks).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.app.api.deps import CacheControl
from backend.app.api.v1.endpoints import experiments
from backend.app.models.experiment import ExperimentStatus
from backend.app.models.user import UserRole
from backend.app.schemas.visual import VisualConfig
from backend.app.services import visual

pytestmark = pytest.mark.unit

PAGE = {"match": "prefix", "url": "https://shop.example.com/"}
CONFIG = {"version": 1, "editor_url": "https://shop.example.com/", "pages": [PAGE]}
CHANGES = {"version": 1, "changes": [{"op": "hide", "selector": ".banner"}]}

#: Values no JSONB column holds, each standing for a visual_config.
HELD = [VisualConfig.model_validate(CONFIG), MagicMock(name="visual_config")]
HELD_IDS = ["model-object", "test-double-attribute"]


def _draft(visual_config, changes=CHANGES):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=ExperimentStatus.DRAFT,
        experiment_type="a_b",
        visual_config=visual_config,
        targeting_rules=None,
        mutual_exclusion_group_id=None,
        start_date=None,
        end_date=None,
        split_url_config=None,
        variants=[
            SimpleNamespace(name="Control", is_control=True, visual_changes=None),
            SimpleNamespace(name="B", is_control=False, visual_changes=changes),
        ],
        metric_definitions=[SimpleNamespace(name="Clicks", event_name="click_buy")],
    )


@pytest.mark.regression
@pytest.mark.parametrize("held", HELD, ids=HELD_IDS)
def test_a_visual_config_of_any_type_but_none_is_visual(held):
    draft = _draft(held)
    assert visual.is_visual(draft)
    assert visual.design_etag(draft) is not None
    assert not visual.is_visual(_draft(None))


@pytest.mark.regression
@pytest.mark.parametrize("held", HELD, ids=HELD_IDS)
@pytest.mark.asyncio
async def test_start_asks_such_a_draft_for_its_design_etag_and_starts_nothing(held):
    draft = _draft(held)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = draft
    user = SimpleNamespace(id=uuid.uuid4(), role=UserRole.DEVELOPER, is_superuser=False)

    with (
        patch("backend.app.api.deps.get_experiment_change_access", return_value=draft),
        patch("backend.app.api.v1.endpoints.experiments.ExperimentService") as service,
    ):
        with pytest.raises(HTTPException) as refused:
            await experiments.start_experiment(
                experiment_id=draft.id,
                start=None,
                db=db,
                current_user=user,
                cache_control=CacheControl(enabled=False, skip=False, redis=None),
            )

    assert (refused.value.status_code, refused.value.detail) == (
        400,
        visual.DESIGN_ETAG_MISSING.format(id=draft.id),
    )
    service.return_value.start_experiment.assert_not_called()


def test_the_start_check_refuses_values_it_cannot_read():
    """What the etag check lets through still fails the start check: a
    visual_config or page changes of a type no schema accepts."""
    assert visual.visual_start_problem(_draft(object())) == (
        visual.VISUAL_CONFIG_NOT_VALID
    )
    assert visual.visual_start_problem(
        _draft(CONFIG, changes=object())
    ) == visual.CHANGES_NOT_VALID.format(name="B")
