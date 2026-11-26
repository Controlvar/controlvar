"""A request sets a flag's split and its rules' own values (#2012).

What a create accepts and refuses, and where and in which words each refusal
is answered: the split at ``["split"]`` or at its entry, a rule's own value at
``["targeting_rules"]`` with its place in the message. The update and the
environment routes check with the same function, ``flag_values.serving_errors``
(``backend/tests/integration/api/test_flag_split_write_api.py`` holds every
write path to it over HTTP).

Every split a write accepts, the read path serves: the flag has a value for
every user, and each user gets one of the split's values.
"""

from __future__ import annotations

import itertools
import uuid

import pytest
from pydantic import ValidationError

from backend.app.core.flag_values import (
    MAX_SERVED_VALUE_BYTES,
    MAX_SPLIT_ENTRIES,
    MAX_VALUE_BYTES,
    MIN_SPLIT_ENTRIES,
    RULE_VALUE_ON_A_BOOLEAN_FLAG,
    SERVED_VALUES_TOO_LARGE,
    SPLIT_ENTRY,
    SPLIT_ON_A_BOOLEAN_FLAG,
    SPLIT_TOO_LONG,
    SPLIT_TOO_SHORT,
    SPLIT_VALUES_EQUAL,
    SPLIT_WEIGHT,
    servable_values,
    serve,
    serving_errors,
)
from backend.app.core.targeting_adapter import (
    RULE_VALUE_WITHOUT_A_CONDITION,
    TargetingRulesError,
    validate_experiment_targeting,
    validate_segment_rules,
)
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.schemas.feature_flag import (
    FeatureFlagCreate,
    FeatureFlagRead,
    FeatureFlagUpdate,
)
from backend.app.schemas.flag_environment import FlagEnvironmentUpdate
from backend.app.services.feature_flag_service import (
    FlagValuesRefused,
    _check_sent_values,
)

pytestmark = [pytest.mark.unit]

USERS = [f"user-{n}" for n in range(200)]
CONDITION = {"attribute": "plan", "operator": "equals", "value": "pro"}
DASHBOARD = {
    "logical_operator": "AND",
    "groups": [{"logical_operator": "AND", "conditions": [CONDITION]}],
}
NATIVE_RULE = {
    "id": "pro",
    "rule": {
        "operator": "and",
        "conditions": [{"attribute": "plan", "operator": "eq", "value": "pro"}],
    },
}


def _split(*weights, values=("red", "green", "blue", "cyan", "plum", "gold")):
    names = itertools.chain(values, (f"v{n}" for n in itertools.count()))
    return [{"value": v, "weight": w} for v, w in zip(names, weights)]


SPLIT = _split(34, 33, 33)


def _body(**fields):
    return {
        "key": "checkout-copy",
        "name": "Checkout copy",
        "value_type": "string",
        "on_value": "on",
        "off_value": "off",
        **fields,
    }


def _refusals(model, data):
    with pytest.raises(ValidationError) as caught:
        model.model_validate(data)
    return [(e["type"], e["loc"], e["msg"]) for e in caught.value.errors()]


# ---------------------------------------------------------------------------
# The split
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "split",
    [
        _split(34, 33, 33),
        _split(*[10] * MAX_SPLIT_ENTRIES),
        _split(100, 0, 0),
        _split(50, 50, 0, values=("on", "off", "third")),
        _split(40, 30, 20, 10),
        _split(34.0, 33, 33),
        None,
    ],
    ids=["34-33-33", "ten", "100-0-0", "on-and-off", "four", "a-float-100", "none"],
)
def test_a_create_accepts_a_split_of_three_to_ten_values(split):
    created = FeatureFlagCreate.model_validate(_body(split=split))
    assert created.split == split


@pytest.mark.parametrize(
    "value_type, split",
    [
        ("number", _split(50, 25, 25, values=(1, 2.5, -3))),
        ("json", _split(50, 25, 25, values=({"a": 1}, [1, 2], {"a": 2}))),
    ],
)
def test_a_number_or_json_flag_has_a_split_of_its_type(value_type, split):
    body = _body(value_type=value_type, on_value=split[0]["value"], off_value=0)
    if value_type == "json":
        body["off_value"] = {"off": True}
    assert FeatureFlagCreate.model_validate({**body, "split": split}).split == split


def _with(split):
    return _body(split=split)


@pytest.mark.parametrize(
    "split, refusals",
    [
        ([], [("split_too_short", ("split",), SPLIT_TOO_SHORT)]),
        (_split(100), [("split_too_short", ("split",), SPLIT_TOO_SHORT)]),
        (_split(50, 50), [("split_too_short", ("split",), SPLIT_TOO_SHORT)]),
        (
            _split(*[9] * 10, 10),
            [("split_too_long", ("split",), SPLIT_TOO_LONG)],
        ),
        (
            _split(30, 30, 30),
            [
                (
                    "split_total",
                    ("split",),
                    "Weights must add up to 100; they add up to 90.",
                )
            ],
        ),
        (
            _split(34, 34, 33),
            [
                (
                    "split_total",
                    ("split",),
                    "Weights must add up to 100; they add up to 101.",
                )
            ],
        ),
        (
            _split(34, 33, 33, values=("red", "green", "red")),
            [("split_values_equal", ("split", 2, "value"), SPLIT_VALUES_EQUAL)],
        ),
        (
            [{"value": "red", "weight": 34}, "green", {"value": "blue", "weight": 66}],
            [("split_entry", ("split", 1), SPLIT_ENTRY)],
        ),
        (
            [{**SPLIT[0], "name": "red"}, *SPLIT[1:]],
            [("split_entry", ("split", 0), SPLIT_ENTRY)],
        ),
        (
            [{"value": None, "weight": 34}, *SPLIT[1:]],
            [
                (
                    "value_type_mismatch",
                    ("split", 0, "value"),
                    "value must be a string, because value_type is string",
                )
            ],
        ),
        (
            _split(33.5, 33.5, 33),
            [
                ("split_weight", ("split", 0, "weight"), SPLIT_WEIGHT),
                ("split_weight", ("split", 1, "weight"), SPLIT_WEIGHT),
            ],
        ),
        (
            _split(True, 66, 33),
            [("split_weight", ("split", 0, "weight"), SPLIT_WEIGHT)],
        ),
        (
            _split("34", 33, 33),
            [("split_weight", ("split", 0, "weight"), SPLIT_WEIGHT)],
        ),
        (
            {"red": 50},
            [("split_type", ("split",), "A split is a list of values and weights.")],
        ),
    ],
    ids=[
        "empty",
        "one",
        "two",
        "eleven",
        "total-90",
        "total-101",
        "duplicate",
        "not-an-object",
        "another-key",
        "null-value",
        "half-weights",
        "true-weight",
        "text-weight",
        "an-object",
    ],
)
def test_a_create_refuses_each_split_at_its_place_and_in_its_words(split, refusals):
    assert _refusals(FeatureFlagCreate, _with(split)) == refusals


def test_a_duplicate_is_named_at_the_later_value_and_compared_as_json():
    """``3`` equals ``3.0`` as the database compares them; ``1`` is not ``true``."""
    number = _body(value_type="number", on_value=7, off_value=8)
    assert _refusals(
        FeatureFlagCreate, {**number, "split": _split(34, 33, 33, values=(3, 4, 3.0))}
    ) == [("split_values_equal", ("split", 2, "value"), SPLIT_VALUES_EQUAL)]
    json_flag = _body(value_type="json", on_value={"on": 1}, off_value={"off": 1})
    split = _split(34, 33, 33, values=({"a": 1}, {"a": True}, [1]))
    assert (
        FeatureFlagCreate.model_validate({**json_flag, "split": split}).split == split
    )


def test_a_boolean_flag_has_no_split():
    body = {"key": "beta", "name": "Beta", "split": SPLIT}
    assert _refusals(FeatureFlagCreate, body) == [
        ("split_boolean", ("split",), SPLIT_ON_A_BOOLEAN_FLAG)
    ]
    assert FeatureFlagCreate.model_validate({**body, "split": None}).split is None


def test_a_split_value_follows_the_value_limits():
    too_large = "x" * (MAX_VALUE_BYTES + 1)
    split = _split(34, 33, 33, values=("red", too_large, "blue"))
    assert _refusals(FeatureFlagCreate, _with(split)) == [
        (
            "value_too_large",
            ("split", 1, "value"),
            f"value larger than {MAX_VALUE_BYTES} bytes",
        )
    ]


@pytest.mark.regression
@pytest.mark.parametrize("model", [FeatureFlagCreate, FeatureFlagUpdate])
@pytest.mark.parametrize("field", ["values", "value", "variants", "weights"])
def test_only_split_carries_more_values_and_the_other_names_stay_refused(model, field):
    """Replaces "no third value or split is accepted" (B3): ``split`` is the
    one field that carries a flag's other values; ``values``, ``value``,
    ``variants`` and ``weights`` are still refused, as any unknown field is."""
    body = _body(**{field: ["a", "b", "c"]})
    assert [r[:2] for r in _refusals(model, body)] == [("extra_forbidden", (field,))]
    assert model.model_validate(_body(split=SPLIT)).split == SPLIT


# ---------------------------------------------------------------------------
# A rule's own value
# ---------------------------------------------------------------------------


RULES = {
    "dashboard": ({**DASHBOARD, "value": "pro-copy"}, "value"),
    "native": ({"rules": [{**NATIVE_RULE, "value": "pro-copy"}]}, "rules[0].value"),
    "default": (
        {
            "rules": [NATIVE_RULE],
            "default_rule": {**NATIVE_RULE, "id": "rest", "value": "pro-copy"},
        },
        "default_rule.value",
    ),
}


def _rules_with(shape, value):
    rules, _ = RULES[shape]
    rules = {**rules}
    if shape == "dashboard":
        rules["value"] = value
    elif shape == "native":
        rules["rules"] = [{**rules["rules"][0], "value": value}]
    else:
        rules["default_rule"] = {**rules["default_rule"], "value": value}
    return rules


@pytest.mark.parametrize("shape", list(RULES))
def test_a_rule_carries_a_value_of_the_flags_type_in_either_shape(shape):
    rules, _ = RULES[shape]
    created = FeatureFlagCreate.model_validate(_body(targeting_rules=rules))
    assert created.targeting_rules == rules


@pytest.mark.parametrize("shape", list(RULES))
@pytest.mark.parametrize(
    "value, message",
    [
        (3, "{place} must be a string, because value_type is string"),
        (None, "{place} must be a string, because value_type is string"),
        ("x" * (MAX_VALUE_BYTES + 1), f"{{place}} larger than {MAX_VALUE_BYTES} bytes"),
    ],
    ids=["a-number", "null", "too-large"],
)
def test_a_rule_value_is_refused_at_the_rules_with_its_place(shape, value, message):
    _, place = RULES[shape]
    assert _refusals(
        FeatureFlagCreate, _body(targeting_rules=_rules_with(shape, value))
    ) == [
        (
            "value_type_mismatch" if "must be" in message else "value_too_large",
            ("targeting_rules",),
            message.format(place=place),
        )
    ]


@pytest.mark.regression
@pytest.mark.parametrize("shape", list(RULES))
@pytest.mark.parametrize("text", ["pro\x00copy", "pro\ud800copy"])
def test_a_rule_value_holding_nul_or_a_lone_surrogate_answers_422_naming_the_field(
    shape, text
):
    """Never a 500: the database cannot store either character."""
    refusals = _refusals(
        FeatureFlagCreate, _body(targeting_rules=_rules_with(shape, text))
    )
    assert [loc for _, loc, _ in refusals] == [("targeting_rules",)]
    ((_, _, message),) = refusals
    assert "targeting_rules" in message or RULES[shape][1] in message
    assert "copy" not in message


def test_a_boolean_flags_rules_have_no_value():
    body = {"key": "beta", "name": "Beta", "targeting_rules": RULES["native"][0]}
    assert _refusals(FeatureFlagCreate, body) == [
        (
            "rule_value_boolean",
            ("targeting_rules",),
            f"rules[0].value: {RULE_VALUE_ON_A_BOOLEAN_FLAG}",
        )
    ]


def test_a_value_on_a_rule_with_no_condition_is_refused():
    body = _body(
        targeting_rules={"logical_operator": "AND", "groups": [], "value": "B"}
    )
    assert _refusals(FeatureFlagCreate, body) == [
        (
            "value_error",
            ("targeting_rules",),
            f"Value error, value: {RULE_VALUE_WITHOUT_A_CONDITION}",
        )
    ]


def test_experiment_and_segment_rules_still_refuse_a_value():
    with pytest.raises(TargetingRulesError, match="unknown key"):
        validate_experiment_targeting({**DASHBOARD, "value": "B"})
    with pytest.raises(TargetingRulesError, match="unknown key"):
        validate_segment_rules({**DASHBOARD, "value": "B"})


# ---------------------------------------------------------------------------
# Everything one environment serves, together
# ---------------------------------------------------------------------------


def _big(prefix: str, count: int):
    """*count* distinct values of exactly MAX_VALUE_BYTES bytes as JSON."""
    return [f"{prefix}{n}".ljust(MAX_VALUE_BYTES - 2, "x") for n in range(count)]


def test_the_largest_split_fits_and_rules_values_share_what_it_leaves():
    largest = _split(*[10] * MAX_SPLIT_ENTRIES, values=_big("s", MAX_SPLIT_ENTRIES))
    assert MAX_SERVED_VALUE_BYTES == MAX_SPLIT_ENTRIES * MAX_VALUE_BYTES
    assert FeatureFlagCreate.model_validate(_with(largest)).split == largest
    over = _body(split=largest, targeting_rules={**DASHBOARD, "value": "a"})
    assert _refusals(FeatureFlagCreate, over) == [
        ("served_values_too_large", ("split",), SERVED_VALUES_TOO_LARGE)
    ]
    rules = {
        "rules": [
            {**NATIVE_RULE, "id": f"r{n}", "value": value}
            for n, value in enumerate(_big("r", 11))
        ]
    }
    assert _refusals(FeatureFlagCreate, _body(targeting_rules=rules)) == [
        ("served_values_too_large", ("targeting_rules",), SERVED_VALUES_TOO_LARGE)
    ]
    rules["rules"].pop()
    assert FeatureFlagCreate.model_validate(_body(targeting_rules=rules))


def test_an_update_counts_what_is_stored_and_clearing_is_always_accepted():
    largest = _split(*[10] * MAX_SPLIT_ENTRIES, values=_big("s", MAX_SPLIT_ENTRIES))
    rules = {**DASHBOARD, "value": "a"}

    def answered(sent, stored):
        return [
            (where, error.type, error.message())
            for where, error in serving_errors("string", sent, stored)
        ]

    assert answered({"targeting_rules": rules}, {"split": largest}) == [
        (("targeting_rules",), "served_values_too_large", SERVED_VALUES_TOO_LARGE)
    ]
    assert answered({"split": None}, {"split": largest, "targeting_rules": rules}) == []
    assert answered({"targeting_rules": DASHBOARD}, {"split": largest}) == []
    assert answered({"split": SPLIT}, {"targeting_rules": rules}) == []


# ---------------------------------------------------------------------------
# What a write accepts, the read path serves
# ---------------------------------------------------------------------------


def _candidates():
    """Splits of 0 to 11 entries, even and uneven, with the values each type
    allows and a few it does not: some are accepted, the rest refused."""
    values = {
        "string": [f"value-{n}" for n in range(12)],
        "number": [n * 1.5 for n in range(12)],
        "json": [{"n": n} for n in range(12)],
    }
    for value_type, pool in values.items():
        for count in range(MAX_SPLIT_ENTRIES + 2):
            for spread in ("even", "first", "zero-tail", "short", "duplicate"):
                if count == 0:
                    yield value_type, []
                    break
                weights = [100 // count] * count
                weights[0] += 100 - sum(weights)
                if spread == "first":
                    weights = [100] + [0] * (count - 1)
                elif spread == "zero-tail" and count > 1:
                    weights = [*weights[:-1], 0]
                    weights[0] += 100 - sum(weights)
                elif spread == "short":
                    weights[-1] -= 1
                chosen = list(pool[:count])
                if spread == "duplicate" and count > 1:
                    chosen[-1] = chosen[0]
                yield (
                    value_type,
                    [{"value": v, "weight": w} for v, w in zip(chosen, weights)],
                )


def _flag(value_type, split, on, off):
    return FeatureFlag(
        key=f"split-{uuid.uuid4().hex[:8]}",
        name="Split",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
        value_type=value_type,
        on_value=on,
        off_value=off,
        split=split,
    )


_PAIRS = {"string": ("on", "off"), "number": (-1, -2), "json": ({"on": 1}, {"off": 1})}


def _accepted_by_every_writer(value_type, split) -> bool:
    on, off = _PAIRS[value_type]
    body = _body(value_type=value_type, on_value=on, off_value=off, split=split)
    try:
        FeatureFlagCreate.model_validate(body)
        created = True
    except ValidationError:
        created = False
    stored = _flag(value_type, None, on, off)
    try:
        _check_sent_values(FeatureFlagUpdate.model_validate({"split": split}), stored)
        updated = True
    except FlagValuesRefused:
        updated = False
    FlagEnvironmentUpdate.model_validate({"split": split})
    environment = not serving_errors(value_type, {"split": split}, {})
    assert created == updated == environment, (value_type, split)
    return created


@pytest.mark.regression
def test_every_split_a_write_accepts_is_served_to_every_user():
    """The read path (the release before this one's evaluation, which already
    reads a split) answers every split a create, an update or an environment
    change accepts: the flag has a value, each user gets one of the split's
    values, and every value of weight above 0 reaches someone."""
    accepted = refused = 0
    for value_type, split in _candidates():
        if not _accepted_by_every_writer(value_type, split):
            refused += 1
            continue
        accepted += 1
        on, off = _PAIRS[value_type]
        flag = _flag(value_type, split, on, off)
        assert servable_values(flag) == (on, off), split
        served = [serve(flag, {"enabled": True, "rule_id": None}, u)[1] for u in USERS]
        values = [entry["value"] for entry in split]
        assert all(value in values for value in served), split
        weighted = [e["value"] for e in split if e["weight"] >= 10]
        assert all(value in served for value in weighted), split
    assert accepted > 50 and refused > 50, (accepted, refused)


def test_a_boolean_flags_response_has_no_split_whatever_is_stored():
    flag = _flag("string", SPLIT, "on", "off")
    flag.id, flag.key = uuid.uuid4(), "k"
    flag.created_at = flag.updated_at = __import__("datetime").datetime(2026, 1, 1)
    flag.default_value = False
    assert FeatureFlagRead.model_validate(flag).split == SPLIT
    flag.value_type, flag.on_value, flag.off_value = "boolean", None, None
    assert FeatureFlagRead.model_validate(flag).split is None
