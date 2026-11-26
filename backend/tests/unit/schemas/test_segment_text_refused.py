"""A segment's create and update bodies refuse NUL and unpaired surrogates in
``name``, ``description`` and ``rules``, anywhere, in a key or a value, nested
or not (#2175).

The database cannot store either character, so such a request answered 500
(or 404 on an update). ``SegmentCreateBody`` and ``SegmentUpdateBody`` now
refuse it before the model reads the body: one error per field, at the field,
with the fixed message, whatever else in the body is wrong. ``SegmentCreate``
itself is also the preview's body, which stores nothing, and still accepts
such text. The routes are
``backend/tests/integration/api/test_segment_text_refused.py``.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Dict

import pytest
from pydantic import TypeAdapter, ValidationError

from backend.app.schemas.segment import (
    SegmentCreate,
    SegmentCreateBody,
    SegmentUpdate,
    SegmentUpdateBody,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

CHARACTERS = {"nul": "\x00", "surrogate": "\ud800"}
EMOJI = "\U0001f600"

RULES = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "country", "operator": "equals", "value": "US"}
            ],
        }
    ],
}


def _rules(change: Callable[[Dict[str, Any], str], Any]):
    def build(character: str) -> Dict[str, Any]:
        rules = copy.deepcopy(RULES)
        change(rules, character)
        return rules

    return build


def _c(rules):
    return rules["groups"][0]["conditions"][0]


def _deep_key(rules, c):
    """A key 50 objects down inside a condition."""
    inner: Dict[str, Any] = {"k" + c: 1}
    for _ in range(50):
        inner = {"n": inner}
    _c(rules)["value"] = inner


#: Every string of the rules, keys included; some of them another check
#: refused before this one existed (an attribute, an operator, a logical
#: operator, a semantic version, a pattern, an unknown key).
RULE_PLACES = {
    "value": _rules(lambda r, c: _c(r).update(value="US" + c)),
    "list-item": _rules(
        lambda r, c: _c(r).update(operator="in", value=["US", "CA" + c])
    ),
    "condition-id": _rules(lambda r, c: _c(r).update(id="c1" + c)),
    "group-id": _rules(lambda r, c: r["groups"][0].update(id="g" + c)),
    "top-level-id": _rules(lambda r, c: r.update(id="t" + c)),
    "attribute": _rules(lambda r, c: _c(r).update(attribute="country" + c)),
    "operator": _rules(lambda r, c: _c(r).update(operator="equals" + c)),
    "logical-operator": _rules(lambda r, c: r.update(logical_operator="AND" + c)),
    "group-logical-operator": _rules(
        lambda r, c: r["groups"][0].update(logical_operator="AND" + c)
    ),
    "pattern": _rules(lambda r, c: _c(r).update(operator="regex", value="^US" + c)),
    "semantic-version": _rules(
        lambda r, c: _c(r).update(operator="semver_gte", value="1.2.3" + c)
    ),
    "condition-key": _rules(lambda r, c: _c(r).update({"z" + c: 1})),
    "top-level-key": _rules(lambda r, c: r.update({"z" + c: 1})),
    "deep-key": _rules(_deep_key),
}

TEXT_PLACES = {
    "the-whole-text": lambda c: c,
    "after-text": lambda c: "ab" + c,
    "in-a-list": lambda c: ["ab" + c],
}

#: A create needs a name and, for a rules segment, rules; an update nothing.
BODIES = {
    "create": (SegmentCreateBody, {"name": "Segment text", "rules": RULES}),
    "create-id-list": (SegmentCreateBody, {"name": "Segment text", "kind": "id_list"}),
    "update": (SegmentUpdateBody, {}),
}


def _refusal(field: str) -> Dict[str, Any]:
    return {
        "type": "value_error",
        "loc": (field,),
        "msg": f"Value error, {field} must not contain NUL or unpaired surrogate characters",
    }


def _plain(error: ValidationError) -> list:
    """The errors without their context: a ``ValueError`` in it compares by
    identity."""
    return error.errors(include_url=False, include_context=False)


def _errors(body_type, body) -> list:
    with pytest.raises(ValidationError) as caught:
        TypeAdapter(body_type).validate_python(body)
    return [{k: e[k] for k in ("type", "loc", "msg")} for e in caught.value.errors()]


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", TEXT_PLACES)
@pytest.mark.parametrize("field", ("name", "description"))
@pytest.mark.parametrize("body", BODIES)
def test_the_name_and_description_are_refused_wherever_the_character_is(
    body, field, place, character
):
    body_type, base = BODIES[body]
    sent = {**base, field: TEXT_PLACES[place](CHARACTERS[character])}

    assert _errors(body_type, sent) == [_refusal(field)]


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", RULE_PLACES)
@pytest.mark.parametrize("body", ("create", "update"))
def test_the_rules_are_refused_wherever_the_character_is(body, place, character):
    body_type, base = BODIES[body]
    rules = RULE_PLACES[place](CHARACTERS[character])

    assert _errors(body_type, {**base, "rules": rules}) == [_refusal("rules")]


@pytest.mark.parametrize("body", BODIES)
def test_each_field_is_refused_at_its_place_and_nothing_else_is_read(body):
    """One error per field, in the order name, description, rules; the
    model's own checks (here a kind on an update, an unknown kind) are not
    run on a refused body."""
    body_type, base = BODIES[body]
    sent = {
        **base,
        "name": "a\x00",
        "description": "d\ud800",
        "rules": RULE_PLACES["value"]("\x00"),
        "kind": "neither",
    }

    assert _errors(body_type, sent) == [
        _refusal("name"),
        _refusal("description"),
        _refusal("rules"),
    ]


@pytest.mark.parametrize("body", BODIES)
def test_bodies_without_the_character_are_accepted_as_before(body):
    body_type, base = BODIES[body]
    model = SegmentCreate if body_type is SegmentCreateBody else SegmentUpdate
    for fields in (
        {},
        {"name": "ab" + EMOJI, "description": "d" + EMOJI},
        {"description": None},
    ):
        sent = {**base, **fields}
        assert TypeAdapter(body_type).validate_python(sent) == model.model_validate(
            sent
        )
    if "rules" in base or body == "update":
        for rules in (RULE_PLACES["value"](EMOJI), RULE_PLACES["list-item"](EMOJI)):
            sent = {**base, "rules": rules}
            accepted = TypeAdapter(body_type).validate_python(sent)
            assert accepted.rules == rules


@pytest.mark.parametrize("body", BODIES)
def test_other_values_are_left_to_the_model(body):
    """The check refuses only text it finds; a value of another type, or a
    body that is not an object, gets the model's own answer, as before."""
    body_type, base = BODIES[body]
    model = SegmentCreate if body_type is SegmentCreateBody else SegmentUpdate
    for sent in (
        [],
        None,
        "a\x00",
        *(
            {**base, field: probe}
            for field in ("name", "description", "rules")
            for probe in ({}, [], False, 1)
        ),
    ):
        with pytest.raises(ValidationError) as checked:
            TypeAdapter(body_type).validate_python(sent)
        try:
            model.model_validate(sent)
        except ValidationError as plain:
            assert _plain(checked.value) == _plain(plain)
        else:  # pragma: no cover - every probe is refused by the model
            pytest.fail(f"{sent!r} was accepted by the model")


@pytest.mark.parametrize("character", CHARACTERS)
def test_the_preview_body_still_accepts_the_character(character):
    """``SegmentCreate`` alone is the preview's body: it stores nothing, so
    text the database cannot store is accepted there as before."""
    c = CHARACTERS[character]
    rules = RULE_PLACES["value"](c)

    accepted = SegmentCreate.model_validate({"name": "Segment text", "rules": rules})

    assert accepted.rules == rules
    if character == "nul":
        named = SegmentCreate.model_validate({"name": "ab" + c, "rules": RULES})
        assert named.name == "ab" + c
