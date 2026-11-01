"""Metric names in ``ExperimentCreate`` and ``ExperimentUpdate`` (#1461).

The request-level behaviour (422 on create and update, the stored metrics
kept, the order of the answers, the wizard) is pinned by
``backend/tests/integration/api/test_experiment_metric_names.py``; this file
pins the schemas and the one check they share, with no database.
"""

import copy

import pytest
from pydantic import ValidationError

from backend.app.schemas.experiment import (
    DUPLICATE_METRIC_MESSAGE,
    ExperimentCreate,
    ExperimentUpdate,
    MetricBase,
    refuse_duplicate_metric_names,
)

pytestmark = [pytest.mark.unit]

#: The UX text, written out so a change to it fails here.
UX_TEXT = (
    "duplicate metric name; each name may appear once per experiment "
    "(first seen at metrics[{first}], repeated at metrics[{second}])"
)

PAYLOAD = {
    "name": "Metric names",
    "experiment_type": "a_b",
    "variants": [
        {"name": "Control", "is_control": True, "traffic_allocation": 50},
        {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
    ],
}


def _metrics(*names):
    return [{"name": name, "event_name": "purchase"} for name in names]


def _errors(build):
    with pytest.raises(ValidationError) as caught:
        build()
    return caught.value.errors(include_url=False, include_input=False)


def test_the_message_is_the_ux_text():
    assert DUPLICATE_METRIC_MESSAGE == UX_TEXT


@pytest.mark.regression
@pytest.mark.parametrize(
    "names, first, second",
    [
        (("a", "a"), 0, 1),
        (("a", "b", "a"), 0, 2),
        (("a", "b", "b", "a"), 1, 2),
        (("x", "a", "b", "c", "b", "a"), 2, 4),
    ],
)
@pytest.mark.parametrize("schema", ["create", "update"])
def test_a_repeat_is_refused_naming_the_first_repeated_pair(
    schema, names, first, second
):
    metrics = _metrics(*names)
    if schema == "create":
        errors = _errors(lambda: ExperimentCreate(**PAYLOAD, metrics=metrics))
    else:
        errors = _errors(lambda: ExperimentUpdate(metrics=metrics))

    assert errors == [
        {
            "type": "value_error",
            "loc": ("metrics",),
            "msg": UX_TEXT.format(first=first, second=second),
        }
    ]


@pytest.mark.parametrize(
    "names",
    [("Revenue", "revenue"), ("a", " a"), ("a", "a "), ("a",), ("a", "b", "c")],
)
@pytest.mark.parametrize("schema", ["create", "update"])
def test_names_are_compared_exactly(schema, names):
    """Nothing is trimmed or case-folded: the unique index on (experiment,
    name) stores each of these pairs, so the schema accepts them."""
    metrics = _metrics(*names)
    if schema == "create":
        built = ExperimentCreate(**PAYLOAD, metrics=metrics)
    else:
        built = ExperimentUpdate(metrics=metrics)

    assert [metric.name for metric in built.metrics] == list(names)


@pytest.mark.regression
def test_an_empty_list_is_too_short_on_both():
    create = _errors(lambda: ExperimentCreate(**PAYLOAD, metrics=[]))
    update = _errors(lambda: ExperimentUpdate(metrics=[]))

    assert update == create
    assert [(e["type"], e["loc"]) for e in update] == [("too_short", ("metrics",))]


def test_null_on_update_leaves_the_metrics_alone():
    update = ExperimentUpdate(metrics=None)

    assert update.model_dump(exclude_unset=True) == {"metrics": None}


def test_the_check_is_pure():
    """Same list, same answer, any number of times; the list is returned
    as it came and is not changed."""
    accepted = [MetricBase(**m) for m in _metrics("a", "b")]
    before = copy.deepcopy(accepted)
    for _ in range(3):
        assert refuse_duplicate_metric_names(accepted) is accepted
    assert accepted == before

    refused = [MetricBase(**m) for m in _metrics("a", "b", "a")]
    messages = set()
    for _ in range(3):
        with pytest.raises(Exception) as caught:
            refuse_duplicate_metric_names(refused)
        messages.add(str(caught.value))
    assert messages == {UX_TEXT.format(first=0, second=2)}

    assert refuse_duplicate_metric_names(None) is None


def test_the_create_routes_second_build_gives_the_same_model():
    """The create route builds ``ExperimentCreate`` from the request, then
    again from that model's dump; the second build must accept what the
    first did and give the same model."""
    first = ExperimentCreate(**PAYLOAD, metrics=_metrics("Signup", "Purchase"))
    second = ExperimentCreate(**first.model_dump())

    assert second == first


def test_the_update_schema_has_a_minimum_of_one_metric():
    metrics = ExperimentUpdate.model_json_schema()["properties"]["metrics"]
    array, null = metrics["anyOf"]

    assert array["minItems"] == 1
    assert null == {"type": "null"}
