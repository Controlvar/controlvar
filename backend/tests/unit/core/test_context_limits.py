"""The limits on a targeting context: ``backend/app/core/context_limits.py``.

Pure functions, no database. What the routes answer is pinned in
``backend/tests/integration/api/test_targeting_context_limits.py``; the numbers
on the documentation pages in
``backend/tests/unit/docs/test_targeting_context_limit_docs.py``.
"""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from backend.app.core import context_limits
from backend.app.core.context_limits import (
    CONTEXT_TOO_DEEP,
    CONTEXT_TOO_LARGE,
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_DEPTH,
    check_context,
    check_context_text,
    compact_size,
    context_over_limits,
    nested_deeper_than,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
VECTORS = REPO_ROOT / "tests" / "sdk-contract" / "ruleset-vectors.json"
DEMO = REPO_ROOT / "demo"


# ---------------------------------------------------------------------------
# Context builders, shared with the integration tests
# ---------------------------------------------------------------------------


def sized(size: int, char: str = "x", **extra: Any) -> Dict[str, Any]:
    """A context of exactly ``size`` bytes as compact UTF-8 JSON.

    ``char`` is the padding: ``"é"`` (two bytes) makes a multibyte context,
    finished with one ``"x"`` when ``size`` is odd. ``extra`` keys come first.
    """
    context: Dict[str, Any] = {**extra, "pad": ""}
    room = size - compact_size(context)
    width = len(char.encode("utf-8"))
    pad = char * (room // width) + "x" * (room % width)
    context["pad"] = pad
    assert compact_size(context) == size, (compact_size(context), size)
    return context


def object_chain(levels: int, key: str = "a", **extra: Any) -> Dict[str, Any]:
    """``levels`` nested objects: ``object_chain(2) == {"a": {"a": 1}}``."""
    value: Any = 1
    for _ in range(levels - 1):
        value = {key: value}
    return {**extra, key: value}


def array_chain(levels: int, **extra: Any) -> Dict[str, Any]:
    """One object holding arrays, ``levels`` containers in all:
    ``array_chain(3) == {"a": [[1]]}``."""
    value: Any = 1
    for _ in range(levels - 1):
        value = [value]
    return {**extra, "a": value}


def _load_demo(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their module through sys.modules.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def streampulse_contexts() -> List[Dict[str, Any]]:
    """What the StreamPulse simulator and app send: every device's attributes."""
    devices = _load_demo(
        "_context_limits_streampulse_devices",
        DEMO / "streampulse" / "simulator" / "devices.py",
    )
    population = devices.generate_devices(500)
    return [d.attributes() for d in population] + [
        d.attributes() for d in devices.PRESET_DEVICES
    ]


def shoplab_contexts() -> List[Dict[str, Any]]:
    """What the ShopLab simulator sends: each persona's attributes."""
    traffic = _load_demo(
        "_context_limits_shoplab_traffic",
        DEMO / "shoplab" / "simulator" / "traffic.py",
    )
    rng = random.Random(7)
    return [
        traffic.make_attributes(rng, persona)
        for persona in traffic.PERSONAS
        for _ in range(100)
    ]


def vector_contexts() -> List[Dict[str, Any]]:
    """Every ``context`` in the SDKs' shared ruleset vectors."""

    def walk(node: Any):
        if isinstance(node, dict):
            if isinstance(node.get("context"), dict):
                yield node["context"]
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    return list(walk(json.loads(VECTORS.read_text("utf-8"))))


# ---------------------------------------------------------------------------
# The values
# ---------------------------------------------------------------------------


def test_the_limits_are_8192_bytes_and_10_levels():
    """The API reference and the stability table state these values (search
    for 8,192); change them with it. The docs test ties them."""
    assert MAX_CONTEXT_BYTES == 8192
    assert MAX_CONTEXT_DEPTH == 10
    assert CONTEXT_TOO_LARGE == "Targeting context larger than 8192 bytes"
    assert CONTEXT_TOO_DEEP == "Targeting context nested more than 10 levels deep"


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("char", ["x", "é", "€"], ids=["ascii", "2-byte", "3-byte"])
def test_a_context_of_exactly_the_size_limit_is_accepted_and_one_byte_more_is_not(
    char,
):
    at_limit = sized(MAX_CONTEXT_BYTES, char)
    over = sized(MAX_CONTEXT_BYTES + 1, char)
    assert context_over_limits(at_limit) is None
    assert context_over_limits(over) == CONTEXT_TOO_LARGE


def test_size_is_counted_in_utf8_bytes_of_compact_json():
    assert compact_size({"a": 1, "b": [1, 2]}) == len('{"a":1,"b":[1,2]}')
    assert compact_size({"a": "é"}) == len('{"a":"é"}'.encode("utf-8")) == 10
    # Characters are not escaped: "é" is two bytes, not the six of "é".
    assert compact_size({"a": "é" * 100}) == 8 + 200


def test_a_lone_surrogate_is_counted_rather_than_failing_to_encode():
    context = json.loads('{"a": "\\ud800"}')
    assert compact_size(context) == len('{"a":""}') + 3
    assert context_over_limits(context) is None


# ---------------------------------------------------------------------------
# Depth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chain", [object_chain, array_chain], ids=["objects", "arrays"]
)
def test_a_context_of_exactly_the_depth_limit_is_accepted_and_one_level_more_is_not(
    chain,
):
    assert context_over_limits(chain(MAX_CONTEXT_DEPTH)) is None
    assert context_over_limits(chain(MAX_CONTEXT_DEPTH + 1)) == CONTEXT_TOO_DEEP


def test_arrays_are_levels():
    """One object and ten arrays is eleven levels."""
    context = array_chain(MAX_CONTEXT_DEPTH + 1)
    assert json.dumps(context).count("{") == 1
    assert nested_deeper_than(context, MAX_CONTEXT_DEPTH)
    assert not nested_deeper_than({"a": [1, [2]], "b": {"c": []}}, 3)
    assert nested_deeper_than({"a": [1, [2, [3]]]}, 3)


def test_levels_are_counted_on_every_branch():
    """The deep branch can be anywhere: not only the first or the last key."""
    for position in range(3):
        context: Dict[str, Any] = {f"k{i}": {"x": 1} for i in range(3)}
        context[f"k{position}"] = object_chain(MAX_CONTEXT_DEPTH)
        assert context_over_limits(context) == CONTEXT_TOO_DEEP, position


def test_a_scalar_or_an_empty_context_has_no_depth_to_refuse():
    assert not nested_deeper_than(1, 0)
    assert not nested_deeper_than({}, 1)
    assert not nested_deeper_than([], 1)
    assert nested_deeper_than({}, 0)
    assert context_over_limits(None) is None
    assert context_over_limits({}) is None


def test_depth_is_found_without_recursion():
    """A chain far deeper than the interpreter's recursion limit."""
    value: Any = 1
    for _ in range(sys.getrecursionlimit() * 50):
        value = [value]
    assert nested_deeper_than({"a": value}, MAX_CONTEXT_DEPTH)
    assert context_over_limits({"a": value}) == CONTEXT_TOO_DEEP


class _Untouchable(list):
    """A container the walk must not open."""

    def __iter__(self):
        raise AssertionError("the walk went past the first level over the limit")


def test_the_walk_stops_at_the_first_container_past_the_limit():
    value: Any = _Untouchable([[[1]]])
    for _ in range(MAX_CONTEXT_DEPTH - 1):
        value = [value]
    context = {"a": value}  # the _Untouchable sits at level MAX + 1
    assert nested_deeper_than(context, MAX_CONTEXT_DEPTH)


def test_depth_is_checked_before_size(monkeypatch):
    """A deep chain is refused before anything serialises it: ``json.dumps``
    recurses, and a 930-level array of under 2 KB is enough to make it raise
    when the call starts deep in the stack."""

    def must_not_run(value):
        raise AssertionError("compact_size ran before the depth check")

    monkeypatch.setattr(context_limits, "compact_size", must_not_run)
    deep_and_small = array_chain(930)
    assert len(json.dumps(deep_and_small)) < 3000
    assert context_over_limits(deep_and_small) == CONTEXT_TOO_DEEP
    # Over both limits: the depth detail.
    assert (
        context_over_limits(object_chain(MAX_CONTEXT_DEPTH + 1, pad="x" * 10_000))
        == CONTEXT_TOO_DEEP
    )


# ---------------------------------------------------------------------------
# The 422
# ---------------------------------------------------------------------------


def test_check_context_answers_422_with_the_fixed_detail_only():
    secret = "value-from-the-request"
    for context, detail in (
        (sized(MAX_CONTEXT_BYTES + 1, user=secret), CONTEXT_TOO_LARGE),
        (object_chain(MAX_CONTEXT_DEPTH + 1, user=secret), CONTEXT_TOO_DEEP),
    ):
        with pytest.raises(HTTPException) as caught:
            check_context(context)
        assert caught.value.status_code == 422
        assert caught.value.detail == detail
        with pytest.raises(HTTPException) as caught:
            check_context(context, field="users[3]")
        assert caught.value.detail == f"users[3]: {detail}"
        assert secret not in caught.value.detail
    check_context(None)
    check_context(sized(MAX_CONTEXT_BYTES))


def test_check_context_text_holds_the_parameter_as_sent_to_the_size_limit():
    check_context_text(None)
    check_context_text("")
    check_context_text("x" * MAX_CONTEXT_BYTES)
    for text in ("x" * (MAX_CONTEXT_BYTES + 1), "é" * (MAX_CONTEXT_BYTES // 2 + 1)):
        with pytest.raises(HTTPException) as caught:
            check_context_text(text)
        assert caught.value.status_code == 422
        assert caught.value.detail == CONTEXT_TOO_LARGE
    # Spaces count: the text as sent, not as it would be re-serialised.
    spaced = '{"pad": "' + "x" * (MAX_CONTEXT_BYTES - 12) + '"}  '
    assert compact_size(json.loads(spaced)) <= MAX_CONTEXT_BYTES
    with pytest.raises(HTTPException):
        check_context_text(spaced)


# ---------------------------------------------------------------------------
# What is sent today is within the limits
# ---------------------------------------------------------------------------


def test_every_context_in_the_sdk_vectors_is_within_the_limits():
    contexts = vector_contexts()
    assert len(contexts) >= 1900, len(contexts)
    over = [c for c in contexts if context_over_limits(c) is not None]
    assert not over, over[:3]


def test_the_demo_apps_contexts_are_within_the_limits():
    contexts = streampulse_contexts() + shoplab_contexts()
    assert len(contexts) >= 800, len(contexts)
    over = [c for c in contexts if context_over_limits(c) is not None]
    assert not over, over[:3]
