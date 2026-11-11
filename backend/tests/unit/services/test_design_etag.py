"""A visual draft's ``design_etag`` covers every leaf of its design (#1545, V69).

``design_etag`` is what the dashboard's Start sends back: a design that
changed after the page loaded is not started. A field it misses is a change
that can slip in between the review and the click -- PE P1 was exactly that,
``lower_is_better`` outside a four-field metric tuple, which flips the winner
(#1377). So:

* a walker over the design's models recurses through every list, every
  optional and every member of the discriminated unions (each change op, each
  goal kind), down to the leaves; the leaf count is exact, and the members it
  reached are every member there is;
* changing any one leaf changes the etag -- built through real ORM rows, so
  the etag is read from the row's columns as the route reads it;
* every column of a variant or metric row is classified as part of the
  design or not, and every field a write can set is part of it, so a new
  column fails until it is classified.

Planted defects: drop ``lower_is_better`` from ``DESIGN_METRIC_COLUMNS`` and
``metrics[0].lower_is_better: etag unchanged``; a walker that stops at a list
reports fewer leaves than ``LEAVES``.
"""

from __future__ import annotations

import copy
import typing
import uuid
from enum import Enum

import pytest
from pydantic import BaseModel

from backend.app.models.experiment import Experiment, Metric, Variant
from backend.app.models.experiment import MetricType as RowMetricType
from backend.app.schemas import visual as visual_schema
from backend.app.schemas.experiment import MetricBase, VariantBase
from backend.app.schemas.visual import VisualChanges, VisualConfig
from backend.app.services import visual

pytestmark = pytest.mark.unit

PAGE = {"match": "prefix", "url": "https://shop.example.com/"}

BASE_DESIGN = {
    "targeting_rules": {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "country", "operator": "equals", "value": "US"}
                ],
            }
        ],
    },
    "mutual_exclusion_group_id": uuid.UUID("11111111-1111-4111-8111-111111111111"),
    "visual_config": VisualConfig.model_validate(
        {
            "version": 1,
            "editor_url": "https://shop.example.com/",
            "pages": [PAGE],
            "goals": [
                {
                    "event_name": "click_buy",
                    "kind": "click",
                    "selector": "#buy",
                    "page": PAGE,
                },
                {"event_name": "page_thanks", "kind": "page_view", "page": PAGE},
            ],
        }
    ),
    "variants": [
        VariantBase(
            name="Control",
            description="as it is",
            is_control=True,
            traffic_allocation=50,
            configuration={"k": "v"},
        ),
        VariantBase(
            name="B",
            description="new headline",
            is_control=False,
            traffic_allocation=50,
            configuration={"k": "w"},
            visual_changes=VisualChanges.model_validate(
                {
                    "version": 1,
                    "changes": [
                        {"op": "set_text", "selector": "#h", "text": "Hi"},
                        {
                            "op": "set_attribute",
                            "selector": "#a",
                            "name": "title",
                            "value": "Go",
                        },
                        {
                            "op": "set_image",
                            "selector": "#i",
                            "src": "/a.png",
                            "alt": "A",
                        },
                        {"op": "hide", "selector": "#x"},
                        {
                            "op": "set_style",
                            "selector": "#s",
                            "styles": {"color": "red"},
                        },
                    ],
                }
            ),
        ),
    ],
    "metrics": [
        MetricBase(
            name="Clicks",
            description="buy clicks",
            event_name="click_buy",
            metric_type="conversion",
            is_primary=True,
            aggregation_method="average",
            minimum_sample_size=100,
            expected_effect=0.1,
            event_value_path="value",
            lower_is_better=False,
        )
    ],
}

#: The exact number of leaves the walker reaches in ``BASE_DESIGN``, so that
#: a walker that stops early is caught: 2 for the experiment's own fields
#: (targeting_rules is opaque, and the group id); visual_config's 13 (version
#: and editor_url, a page's match and url, a click goal's 5 and a page view's
#: 4); the control's 6 (its visual_changes is null, a leaf); variant B's 5
#: plus its visual_changes' 17 (version, and set_text 3, set_attribute 4,
#: set_image 4, hide 2, set_style 3); and the metric's 10.
LEAVES = 2 + 13 + 6 + 5 + 17 + 10

#: Leaves the etag deliberately does not cover: none.
EXCLUDED: typing.FrozenSet[str] = frozenset()


def _leaves(value, path):
    """(path, leaf value) for every leaf under *value*, and the model classes
    visited, recursing through models, lists and optional values."""
    found, models = [], set()
    if isinstance(value, BaseModel):
        models.add(type(value))
        for name in type(value).model_fields:
            sub, sub_models = _leaves(getattr(value, name), f"{path}.{name}")
            found += sub
            models |= sub_models
    elif isinstance(value, list):
        for index, item in enumerate(value):
            sub, sub_models = _leaves(item, f"{path}[{index}]")
            found += sub
            models |= sub_models
    else:
        found.append(path)
    return found, models


def _walk():
    found, models = [], set()
    for key, value in BASE_DESIGN.items():
        if key in ("targeting_rules", "mutual_exclusion_group_id"):
            found.append(key)
            continue
        sub, sub_models = _leaves(value, key)
        found += sub
        models |= sub_models
    return found, models


def _union_members(annotation):
    members = set()
    for arg in typing.get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            members.add(arg)
        else:
            members |= _union_members(arg)
    return members


def _as_data():
    """BASE_DESIGN as plain values: models dumped, as the rows store them."""
    data = {}
    for key, value in BASE_DESIGN.items():
        if isinstance(value, BaseModel):
            data[key] = value.model_dump(mode="json")
        elif isinstance(value, list):
            data[key] = [item.model_dump(mode="json") for item in value]
        else:
            data[key] = value
    return data


def _rows(data) -> Experiment:
    """An unsaved experiment with its variant and metric rows, from *data*."""
    experiment = Experiment(
        targeting_rules=data["targeting_rules"],
        mutual_exclusion_group_id=data["mutual_exclusion_group_id"],
        visual_config=data["visual_config"],
    )
    experiment.variants = [Variant(**variant) for variant in data["variants"]]
    experiment.metric_definitions = [
        Metric(**{**metric, "metric_type": RowMetricType(metric["metric_type"])})
        for metric in data["metrics"]
    ]
    return experiment


def _parts(path):
    parts = []
    for piece in path.replace("[", ".[").split("."):
        if piece.startswith("["):
            parts.append(int(piece[1:-1]))
        elif piece:
            parts.append(piece)
    return parts


def _mutated(value):
    if value is None:
        return {"version": 1, "changes": []}
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 0.5
    if isinstance(value, str):
        return value + "x"
    if isinstance(value, Enum):
        return next(member for member in type(value) if member != value)
    if isinstance(value, uuid.UUID):
        return uuid.uuid4()
    if isinstance(value, dict):
        return {**value, "mutated": 1}
    raise AssertionError(f"no mutation for {value!r}")


def _with_leaf_changed(data, path):
    data = copy.deepcopy(data)
    if path == "targeting_rules":
        # Opaque, so a nested key changes.
        data["targeting_rules"]["groups"][0]["conditions"][0]["value"] = "CA"
        return data
    parts = _parts(path)
    holder = data
    for part in parts[:-1]:
        holder = holder[part]
    if parts[-1] == "metric_type":
        # A row holds one of the enum's values.
        holder["metric_type"] = "count"
    else:
        holder[parts[-1]] = _mutated(holder[parts[-1]])
    return data


def test_the_walker_reaches_every_leaf_and_every_union_member():
    leaves, models = _walk()
    assert len(leaves) == LEAVES, f"leaves {len(leaves)} != {LEAVES}"
    expected = _union_members(visual_schema.VisualChange) | _union_members(
        visual_schema.VisualGoal
    )
    assert len(expected) == 7
    assert expected <= models, sorted(m.__name__ for m in expected - models)


@pytest.mark.parametrize("path", _walk()[0])
def test_changing_any_leaf_changes_the_etag(path):
    if path in EXCLUDED:
        pytest.skip("deliberately outside the etag")
    data = _as_data()
    before = visual.design_etag(_rows(data))
    after = visual.design_etag(_rows(_with_leaf_changed(data, path)))
    assert before is not None
    assert after != before, f"{path}: etag unchanged"


def test_the_etag_is_null_without_visual_config_and_stable_otherwise():
    data = _as_data()
    assert visual.design_etag(_rows({**data, "visual_config": None})) is None
    first = visual.design_etag(_rows(data))
    assert first == visual.design_etag(_rows(copy.deepcopy(data)))
    assert len(first) == 64 and int(first, 16) >= 0


def test_the_row_order_does_not_change_the_etag():
    data = _as_data()
    swapped = copy.deepcopy(data)
    swapped["variants"].reverse()
    assert visual.design_etag(_rows(data)) == visual.design_etag(_rows(swapped))


def test_the_balance_setting_is_hashed_only_when_set():
    """#448's ``balance_within`` is part of the design (C2), left out while
    null: a design that balances nothing hashes as it did before the setting
    was hashed, and setting or changing it changes the etag."""
    data = _as_data()
    unset = _rows(data)
    assert "balance_within" not in visual.design_document(unset)
    balanced = _rows(data)
    balanced.balance_within = {"attribute": "tier", "values": ["gold", "silver"]}
    narrower = _rows(data)
    narrower.balance_within = {"attribute": "tier", "values": ["gold"]}
    etags = [visual.design_etag(row) for row in (unset, balanced, narrower)]
    assert len(set(etags)) == 3, etags


# --- the classification ------------------------------------------------------


@pytest.mark.parametrize(
    "model,columns",
    [
        (Variant, visual.DESIGN_VARIANT_COLUMNS),
        (Metric, visual.DESIGN_METRIC_COLUMNS),
    ],
    ids=["variant", "metric"],
)
def test_every_row_column_is_classified_for_the_etag(model, columns):
    """A new variant or metric column fails here until it joins the etag or
    is named as one that is not part of a design."""
    table = {column.name for column in model.__table__.columns}
    classified = set(columns) | visual.NOT_DESIGN_ROW_COLUMNS
    assert table - classified == set(), "classify these columns for the etag"
    assert classified - table == set(), "these are not columns"
    assert set(columns) & visual.NOT_DESIGN_ROW_COLUMNS == set()


@pytest.mark.parametrize(
    "schema,columns",
    [
        (VariantBase, visual.DESIGN_VARIANT_COLUMNS),
        (MetricBase, visual.DESIGN_METRIC_COLUMNS),
    ],
    ids=["variant", "metric"],
)
def test_every_field_a_write_sets_is_in_the_etag(schema, columns):
    """What a PUT can write into a row, the etag covers (V69w's server half)."""
    assert set(schema.model_fields) - set(columns) == set()


#: Every Experiment column that is not part of a design, with the reason.
NOT_DESIGN = {
    "id": "identity",
    "created_at": "bookkeeping",
    "updated_at": "bookkeeping",
    "name": "a label, not what visitors see or how they are counted",
    "key": "identity",
    "description": "a label",
    "hypothesis": "a label",
    "status": "the lifecycle, checked by the start route itself",
    "experiment_type": "fixed for a visual experiment; no session changes it",
    "owner_id": "who created it",
    "start_date": "never set on a visual draft (C1)",
    "end_date": "the schedule, not the design",
    "resume_at": "the schedule, not the design",
    "metrics": "the legacy JSONB list; the metric rows are hashed",
    "tags": "a label",
    "experiment_metadata": "notes on the results",
    "optimization_type": "analysis, set on the experiment's page",
    "sequential_testing_enabled": "analysis",
    "sequential_testing_method": "analysis",
    "sequential_testing_config": "analysis",
    "variance_reduction_config": "analysis",
    "correction_method": "analysis",
    "confidence_level": "analysis",
    "bayesian_enabled": "analysis",
    "bayesian_config": "analysis",
    "bayesian_decision": "a result",
    "split_url_config": "never on a visual experiment",
    "workspace_id": "set by the workspaces module",
    "balance_seed": "the key of the block order (#448): made by the server, never sent",
}


def test_every_experiment_column_is_classified_for_the_etag():
    columns = {column.name for column in Experiment.__table__.columns}
    design = set(visual.DESIGN_EXPERIMENT_FIELDS)
    when_set = set(visual.DESIGN_EXPERIMENT_FIELDS_WHEN_SET)
    classified = design | when_set | set(NOT_DESIGN)
    assert columns - classified == set(), "classify these columns for the etag"
    assert classified - columns == set(), "these are not Experiment columns"
    assert design & set(NOT_DESIGN) == set()
    assert when_set & (design | set(NOT_DESIGN)) == set()
