"""The visual script's website details against the server (#1545).

Two contract files, both read by the browser script's own tests in sdk/visual:

* ``tests/sdk-contract/visitor-attributes.json``, the details' vocabulary: its
  keys and closed values are the server's (``WEB_DETAIL_RULES``,
  ``WEB_DEVICES``, ``WEB_BROWSERS``, ``WEB_VISITORS``), never a second list,
  and each example is a value the server keeps.
* ``tests/sdk-contract/visual-script-vectors.json``, what the script sends for
  each browser (written by hand, held to the script by
  ``sdk/visual/__tests__/contexts.test.ts``): the server keeps every detail in
  it, so nothing the script sends is dropped.

And two properties of the server with the script's details: a detail the
script leaves out matches no negative rule, for every negative operator the
dashboard's rules have; and the largest context the script can send is within
the 8192-byte context limit.

Needs no database and no ``.git``, so it runs in ``scripts/core_build.sh``'s
copy too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.core import targeting_adapter
from backend.app.core.context_limits import MAX_CONTEXT_BYTES, compact_size
from backend.app.schemas import visual_tracking
from backend.app.schemas.targeting_rule import OperatorType, TargetingRules
from backend.scripts.generate_visual_visitor_vectors import server_matches

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACT = REPO_ROOT / "tests" / "sdk-contract"
VOCABULARY = CONTRACT / "visitor-attributes.json"
SCRIPT_VECTORS = CONTRACT / "visual-script-vectors.json"
CORPORA = ("user_agents", "languages", "referrers", "pages", "collects")
CLOSED = {
    "web_device": visual_tracking.WEB_DEVICES,
    "web_browser": visual_tracking.WEB_BROWSERS,
    "web_visitor": visual_tracking.WEB_VISITORS,
}


def _vocabulary():
    return json.loads(VOCABULARY.read_text(encoding="utf-8"))


def _script_vectors():
    data = json.loads(SCRIPT_VECTORS.read_text(encoding="utf-8"))
    return [vector for corpus in CORPORA for vector in data[corpus]]


def test_the_vocabulary_names_the_servers_details_in_its_order():
    keys = [detail["key"] for detail in _vocabulary()["details"]]
    assert keys == list(visual_tracking.WEB_DETAIL_RULES)


def test_each_closed_detail_has_the_servers_values_and_no_other_detail_has_values():
    problems = []
    for detail in _vocabulary()["details"]:
        values = detail["values"]
        if detail["key"] in CLOSED:
            listed = [value["value"] for value in values or []]
            if listed != list(CLOSED[detail["key"]]):
                problems.append(
                    f"{detail['key']} values {listed} != server {list(CLOSED[detail['key']])}"
                )
            if len({value["label"] for value in values or []}) != len(listed):
                problems.append(f"{detail['key']} has two values with one label")
        elif values is not None:
            problems.append(f"{detail['key']} is open on the server but lists values")
    assert problems == []
    device = next(d for d in _vocabulary()["details"] if d["key"] == "web_device")
    assert device["label"] == "Device type"
    assert [value["label"] for value in device["values"]] == [
        "Phone",
        "Tablet",
        "Desktop",
    ]


def test_each_example_is_a_value_the_server_keeps():
    dropped = [
        f"{detail['key']}: {detail['example']!r}"
        for detail in _vocabulary()["details"]
        if not visual_tracking.web_detail_is_kept(detail["key"], detail["example"])
    ]
    assert dropped == []


def test_the_landing_page_is_the_one_detail_off_by_default():
    vocabulary = _vocabulary()
    off = [d["key"] for d in vocabulary["details"] if not d["default"]]
    assert off == ["web_landing_path"]
    assert vocabulary["visit_minutes"] == 30
    by_default = [d["collect"] for d in vocabulary["details"] if d["default"]]
    assert vocabulary["collect_default"] == list(dict.fromkeys(by_default))


def test_the_server_keeps_every_detail_the_script_sends():
    vectors = _script_vectors()
    assert len(vectors) == 93
    problems = []
    for vector in vectors:
        cleaned, dropped = visual_tracking.clean_web_details(vector["context"])
        if dropped or cleaned != vector["context"]:
            problems.append(f"{vector['id']}: the server drops {dropped}")
    assert problems == []


def test_the_check_names_a_detail_the_server_drops():
    _, dropped = visual_tracking.clean_web_details(
        {"web_device": None, "web_country": "FR"}
    )
    assert dropped == ["web_device", "web_country"]


def _dashboard_negative_operators():
    """The dashboard's operators that match when a value is not something.
    Segment membership is not an attribute condition, so it is not one."""
    segment = {"in_segment", "not_in_segment"}
    return sorted(
        op
        for op in targeting_adapter.DASHBOARD_OPERATORS
        if op.startswith("not_") and op not in segment
    )


def _native_negative_operators():
    """The same operators in the native rules shape: the engine's own names."""
    return sorted(
        op.value
        for op in OperatorType
        if op.name.startswith("NOT_") and op is not OperatorType.NOT_IN_SEGMENT
    )


def _dashboard_rule(key, operator, value):
    """One condition, as the dashboard's builder stores it."""
    return {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": key, "operator": operator, "value": value}
                ],
            }
        ],
    }


def _native_rule(key, operator, value):
    """One condition in the native ``TargetingRules`` shape."""
    rule = {
        "id": f"native-{key}-{operator}",
        "rule": {
            "operator": "and",
            "conditions": [{"attribute": key, "operator": operator, "value": value}],
        },
        "rollout_percentage": 100,
    }
    TargetingRules(rules=[rule])  # a malformed rule fails here, not as "no match"
    return {"version": "1.0", "rules": [rule]}


#: shape -> (its negative operators, how it writes one condition).
SHAPES = {
    "dashboard": (_dashboard_negative_operators, _dashboard_rule),
    "native": (_native_negative_operators, _native_rule),
}


def test_each_shape_has_the_three_negative_operators():
    vectors = json.loads(
        (CONTRACT / "visual-visitor-vectors.json").read_text(encoding="utf-8")
    )
    assert _dashboard_negative_operators() == sorted(vectors["negative_operators"])
    assert _native_negative_operators() == ["neq", "not_contains", "not_in"]


def _value(operator):
    return ["zzz"] if operator == "not_in" else "zzz"


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_detail_left_out_matches_no_negative_operator(shape):
    operators, rule = SHAPES[shape]
    matched = []
    for key in visual_tracking.WEB_DETAIL_RULES:
        for operator in operators():
            case = f"absent-{shape}-{key}-{operator}"
            if server_matches({}, rule(key, operator, _value(operator)), case):
                matched.append(f"{shape}: absent {key} matched {operator}")
    assert matched == []


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_same_rules_match_a_detail_that_is_sent(shape):
    """The positive control: each rule above is one the server evaluates."""
    operators, rule = SHAPES[shape]
    examples = {d["key"]: d["example"] for d in _vocabulary()["details"]}
    missed = []
    for key in visual_tracking.WEB_DETAIL_RULES:
        for operator in operators():
            case = f"sent-{shape}-{key}-{operator}"
            context = {key: examples[key]}
            if not server_matches(context, rule(key, operator, _value(operator)), case):
                missed.append(
                    f"{shape}: {key}={examples[key]} did not match {operator}"
                )
    assert missed == []


def _longest_details():
    """Every detail at the longest value the server keeps."""
    details = {
        "web_device": max(visual_tracking.WEB_DEVICES, key=len),
        "web_browser": max(visual_tracking.WEB_BROWSERS, key=len),
        "web_language": "fil",
        "web_visitor": max(visual_tracking.WEB_VISITORS, key=len),
        "web_referrer": "a" * visual_tracking.MAX_REFERRER_LENGTH,
    }
    for key in visual_tracking.UTM_KEYS:
        details[key] = "a" * visual_tracking.MAX_UTM_LENGTH
    details["web_landing_path"] = "/" + "a" * (
        visual_tracking.MAX_LANDING_PATH_LENGTH - 1
    )
    for key, value in details.items():
        assert visual_tracking.web_detail_is_kept(key, value), key
    return details


#: data-attributes as the browser sends it (compact JSON), in kinds of text the
#: server counts differently: (name, the JSON for n units).
_KINDS = (
    ("one-byte characters", lambda n: '{"a":"' + "a" * n + '"}'),
    ("three-byte characters", lambda n: '{"a":"' + "\u20ac" * n + '"}'),
    ("four-byte characters", lambda n: '{"a":"' + "\U0001f600" * n + '"}'),
    ("escaped control characters", lambda n: '{"a":"' + "\\u0001" * n + '"}'),
    (
        "numbers the server writes longer",
        lambda n: '{"a":[' + ",".join(["1e-7"] * n) + "]}",
    ),
)


def test_the_largest_context_the_script_sends_fits_the_limit():
    """data-attributes at the script's limit (4096 bytes of compact JSON as the
    browser sends it) with every detail at its longest, counted as the route
    counts it, in each kind of text."""
    details = _longest_details()
    sizes = {}
    for name, text in _KINDS:
        n = 0
        while len(text(n + 1).encode("utf-8")) <= 4096:
            n += 1
        attributes = json.loads(text(n))
        sizes[name] = compact_size({**attributes, **details})
    worst = max(sizes.values())
    assert worst <= MAX_CONTEXT_BYTES, sizes
    assert sizes["numbers the server writes longer"] == worst
