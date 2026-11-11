"""A visual experiment's page changes, pages and goals, as the server reads them (#1545).

The rows of PLAN-v3.1's VERIFICATION held here, each with the defect that
fails it:

* V1  the operation set is exactly S1's (add ``insert_html`` and it is not
      classified);
* V2  every string field of every operation has a refused corpus entry (add
      ``title`` to ``set_attribute`` with none and it is uncovered);
* V3  every hostile payload in the hand-written corpus is refused, the count
      is exact, the error type is one of a fixed few, and no refusal repeats
      the value sent; V99 grows the corpus with negative offsets (C13);
* V33 page rules give exact answers (lower-casing the path flips one);
* V34 every limit is exact: N accepted, N + 1 refused;
* V54 a goal's event name is ``^[a-z0-9_]{1,100}$``, unique, never an
      exposure type, and (on create) some metric's event name.

The corpus is ``backend/scripts/generate_visual_vectors.py``'s; the file it
generates is held to it in ``test_visual_vectors.py``.
"""

from __future__ import annotations

import json
import typing

import pytest
from pydantic import ValidationError

from backend.app.schemas import visual
from backend.app.schemas.experiment import ExperimentCreate
from backend.app.schemas.visual import (
    MAX_CHANGES,
    MAX_CHANGES_BYTES,
    VISUAL_OPS,
    VisualChanges,
    VisualConfig,
    page_matches,
    stored_size,
)
from backend.scripts.generate_visual_vectors import ACCEPTED, PAGE_RULES, REFUSED

pytestmark = pytest.mark.unit

#: The error types a refusal may carry: the visual rules' own, and pydantic's
#: structural ones (an unknown key, an unknown op, a wrong type).
REFUSAL_TYPES = {
    "visual_changes",
    "extra_forbidden",
    "literal_error",
    "union_tag_invalid",
    "union_tag_not_found",
    "missing",
    "int_type",
    "string_type",
}

#: Every fixed text the module words a refusal with.
FIXED_TEXTS = {
    value
    for name, value in vars(visual).items()
    if name.isupper() and isinstance(value, str) and value.endswith(".")
}

#: The operations' models, one per op.
OP_MODELS = typing.get_args(typing.get_args(visual.VisualChange)[0])


def _errors(document):
    try:
        VisualChanges.model_validate(document)
    except ValidationError as error:
        return error.errors()
    return []


# --- V1 ---------------------------------------------------------------------


def test_visual_ops_are_exactly_these():
    ops = {model.model_fields["op"].annotation.__args__[0] for model in OP_MODELS}
    assert ops == set(VISUAL_OPS)
    assert set(VISUAL_OPS) == {
        "set_text",
        "set_attribute",
        "set_image",
        "hide",
        "set_style",
    }


# --- V2 ---------------------------------------------------------------------


def _is_string_field(annotation) -> bool:
    if annotation is str:
        return True
    if typing.get_origin(annotation) is typing.Literal:
        return all(isinstance(arg, str) for arg in typing.get_args(annotation))
    return any(_is_string_field(arg) for arg in typing.get_args(annotation))


def test_every_string_field_has_a_refused_corpus_entry():
    fields = set()
    for model in OP_MODELS:
        op = model.model_fields["op"].annotation.__args__[0]
        for name, field in model.model_fields.items():
            if name == "op" or not _is_string_field(field.annotation):
                continue
            fields.add("selector" if name == "selector" else f"{op}.{name}")
    covered = {field for _, field, _ in REFUSED}
    uncovered = sorted(fields - covered)
    assert uncovered == [], f"uncovered field: {', '.join(uncovered)}"
    # The introspection finds every field it should, so an empty answer
    # above is not a broken reader.
    assert {
        "selector",
        "set_text.text",
        "set_attribute.value",
        "set_image.src",
    } <= fields


# --- V3 and V99 -------------------------------------------------------------


def test_the_refused_corpus_has_exactly_these_entries():
    assert len(REFUSED) == 83
    assert len({case_id for case_id, _, _ in REFUSED}) == len(REFUSED)
    margins = [case_id for case_id, _, _ in REFUSED if case_id.startswith("margin-")]
    assert len(margins) == 12  # V99's twelve


@pytest.mark.parametrize(
    "case_id,field,document", REFUSED, ids=[case[0] for case in REFUSED]
)
def test_each_hostile_payload_is_refused(case_id, field, document):
    errors = _errors(document)
    assert errors, f"{case_id}: accepted"
    assert {error["type"] for error in errors} <= REFUSAL_TYPES, errors
    # Fixed text: each of the visual rules' own refusals is one of the module's
    # constants, word for word, so it cannot carry a value sent (pydantic's
    # structural ones name an unknown op tag, which is not a value).
    for error in errors:
        if error["type"] == "visual_changes":
            assert error["msg"] in FIXED_TEXTS, (case_id, error)


def test_the_fixed_texts_are_found():
    """Without this an empty FIXED_TEXTS would make the check above vacuous."""
    assert visual.LINK_REFUSED in FIXED_TEXTS and len(FIXED_TEXTS) > 15


@pytest.mark.parametrize("case_id,document", ACCEPTED, ids=[c[0] for c in ACCEPTED])
def test_each_accepted_payload_is_accepted(case_id, document):
    assert _errors(document) == []


def test_text_is_stored_as_sent_and_never_parsed():
    document = {
        "version": 1,
        "changes": [{"op": "set_text", "selector": "h1", "text": "<b>x</b>"}],
    }
    parsed = VisualChanges.model_validate(document)
    assert parsed.model_dump(exclude_unset=True) == document


@pytest.mark.parametrize(
    "value",
    ["0", "auto", "0 auto", "1px 2em 3rem 4%", "0.5px", ".5em", "10PX"],
)
def test_margins_take_non_negative_lengths(value):
    assert visual.style_problem("margin", value) is None


@pytest.mark.parametrize(
    "value", ["-1px", "0 -1px", "1px 2px 3px 4px 5px", "var(--x)", "1e2px", "+1px"]
)
def test_margins_refuse_everything_else(value):
    assert visual.style_problem("margin", value) == visual.MARGIN_REFUSED


# --- V34 --------------------------------------------------------------------


def _text_doc(count, length=1):
    return {
        "version": 1,
        "changes": [{"op": "set_text", "selector": "h1", "text": "x" * length}] * count,
    }


def test_at_most_100_changes():
    assert _errors(_text_doc(MAX_CHANGES)) == []
    assert [e["type"] for e in _errors(_text_doc(MAX_CHANGES + 1))] == ["too_long"]


def test_text_of_at_most_2000_characters():
    assert _errors(_text_doc(1, 2000)) == []
    assert [e["type"] for e in _errors(_text_doc(1, 2001))] == ["string_too_long"]


def test_at_most_64_kib():
    """A document of exactly 64 KiB is accepted; one byte more is refused."""
    document = _text_doc(32, 2000)
    document["changes"].append({"op": "set_text", "selector": "h1", "text": ""})
    size = stored_size(document)
    document["changes"][-1]["text"] = "x" * (MAX_CHANGES_BYTES - size)
    assert stored_size(document) == MAX_CHANGES_BYTES
    assert len(document["changes"][-1]["text"]) < 2000
    assert _errors(document) == []
    document["changes"][-1]["text"] += "x"
    assert [e["msg"] for e in _errors(document)] == [visual.CHANGES_TOO_LARGE]


def test_a_selector_of_at_most_512_characters():
    ok = {"version": 1, "changes": [{"op": "hide", "selector": "a" * 512}]}
    long = {"version": 1, "changes": [{"op": "hide", "selector": "a" * 513}]}
    assert _errors(ok) == []
    assert [e["msg"] for e in _errors(long)] == [visual.SELECTOR_REFUSED]


def test_at_most_20_styles_of_at_most_200_characters():
    free = sorted(visual.ALLOWED_STYLE_PROPERTIES - {"flex-direction"})
    many = dict.fromkeys(free[:20], "1px")
    assert len(many) == 20 and len(free) > 20

    def doc(styles):
        return {
            "version": 1,
            "changes": [{"op": "set_style", "selector": "p", "styles": styles}],
        }

    assert _errors(doc(many)) == []
    assert [e["msg"] for e in _errors(doc({**many, free[20]: "1px"}))] == [
        visual.STYLE_COUNT_REFUSED
    ]
    assert _errors(doc({"font-family": "a" * 200})) == []
    assert [e["msg"] for e in _errors(doc({"font-family": "a" * 201}))] == [
        visual.STYLE_VALUE_REFUSED
    ]


def test_an_attribute_value_of_at_most_2048_and_an_alt_of_at_most_500():
    def attribute(length):
        return {
            "version": 1,
            "changes": [
                {
                    "op": "set_attribute",
                    "selector": "a",
                    "name": "title",
                    "value": "x" * length,
                }
            ],
        }

    def image(length):
        return {
            "version": 1,
            "changes": [
                {
                    "op": "set_image",
                    "selector": "i",
                    "src": "/a.png",
                    "alt": "x" * length,
                }
            ],
        }

    assert _errors(attribute(2048)) == []
    assert [e["type"] for e in _errors(attribute(2049))] == ["string_too_long"]
    assert _errors(image(500)) == []
    assert [e["type"] for e in _errors(image(501))] == ["string_too_long"]


# --- S2, S10: pages and goals -----------------------------------------------

PAGE = {"match": "prefix", "url": "https://shop.example.com/"}


def _config(**overrides):
    config = {"version": 1, "editor_url": "https://shop.example.com/", "pages": [PAGE]}
    config.update(overrides)
    return config


def _config_errors(config):
    try:
        VisualConfig.model_validate(config)
    except ValidationError as error:
        return error.errors()
    return []


def _goal(event_name, kind="page_view", **extra):
    goal = {"event_name": event_name, "kind": kind, "page": PAGE}
    goal.update(extra)
    return goal


def test_one_to_ten_page_rules():
    pages = [
        {"match": "exact", "url": f"https://shop.example.com/{i}"} for i in range(11)
    ]
    assert _config_errors(_config(pages=[PAGE, *pages[:9]])) == []
    assert [e["type"] for e in _config_errors(_config(pages=[PAGE, *pages[:10]]))] == [
        "too_long"
    ]
    assert [e["type"] for e in _config_errors(_config(pages=[]))] == ["too_short"]


def test_zero_to_ten_goals():
    goals = [_goal(f"page_{i}") for i in range(11)]
    assert _config_errors(_config()) == []
    assert _config_errors(_config(goals=goals[:10])) == []
    assert [e["type"] for e in _config_errors(_config(goals=goals))] == ["too_long"]


@pytest.mark.parametrize(
    "url",
    [
        "https://shop.example.com/a?q=1",
        "https://shop.example.com/a#x",
        "javascript:alert(1)",
        "/relative",
        "https://u:p@shop.example.com/",
        "https://shop.example.com/a b",
    ],
)
def test_a_page_is_a_full_url_without_a_query(url):
    errors = _config_errors(_config(pages=[{"match": "exact", "url": url}]))
    assert visual.PAGE_URL_REFUSED in [e["msg"] for e in errors]


def test_the_editor_page_is_one_of_the_pages_and_may_carry_a_query():
    pages = [{"match": "exact", "url": "https://shop.example.com/landing"}]
    assert (
        _config_errors(
            _config(editor_url="https://shop.example.com/landing?utm=1", pages=pages)
        )
        == []
    )
    errors = _config_errors(
        _config(editor_url="https://shop.example.com/x", pages=pages)
    )
    assert [(e["loc"], e["msg"]) for e in errors] == [
        (("editor_url",), visual.EDITOR_URL_NOT_A_PAGE)
    ]


@pytest.mark.parametrize(
    "event_name",
    ["Click_buy", "click-buy", "", "x" * 101, "exposure", "experiment_exposure", "a b"],
)
def test_v54_a_goal_event_name_is_lower_case_and_never_an_exposure_type(event_name):
    errors = _config_errors(_config(goals=[_goal(event_name)]))
    assert visual.EVENT_NAME_REFUSED in [e["msg"] for e in errors], errors


def test_v54_names_of_100_characters_are_accepted():
    assert _config_errors(_config(goals=[_goal("a" * 100)])) == []


def test_v54_each_goal_has_its_own_event_name():
    errors = _config_errors(_config(goals=[_goal("page_a"), _goal("page_a")]))
    assert [(e["loc"], e["msg"]) for e in errors] == [
        (("goals", 1, "event_name"), visual.DUPLICATE_GOAL_EVENT)
    ]


def test_a_goal_page_is_on_one_of_the_sites():
    other = {"match": "exact", "url": "https://other.example.com/thanks"}
    errors = _config_errors(_config(goals=[_goal("page_a", page=other)]))
    assert [(e["loc"], e["msg"]) for e in errors] == [
        (("goals", 0, "page"), visual.GOAL_PAGE_HOST_REFUSED)
    ]


def test_a_click_goal_names_a_selector():
    assert (
        _config_errors(_config(goals=[_goal("click_buy", "click", selector="#buy")]))
        == []
    )
    errors = _config_errors(_config(goals=[_goal("click_buy", "click", selector="a{")]))
    assert visual.SELECTOR_REFUSED in [e["msg"] for e in errors]
    errors = _config_errors(_config(goals=[_goal("hover_buy", "hover")]))
    assert [e["type"] for e in errors] == ["union_tag_invalid"]


def test_a_config_refuses_an_unknown_key_and_another_version():
    assert [e["type"] for e in _config_errors(_config(script="x"))] == [
        "extra_forbidden"
    ]
    assert [e["msg"] for e in _config_errors(_config(version=2))] == [
        visual.VERSION_REFUSED
    ]


# --- V33 --------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_id,rule,host,path,expected", PAGE_RULES, ids=[c[0] for c in PAGE_RULES]
)
def test_page_rules_give_exact_answers(case_id, rule, host, path, expected):
    assert page_matches(host, path, rule) is expected


def test_the_page_rule_corpus_has_exactly_these_entries():
    assert len(PAGE_RULES) == 19
    assert {expected for *_, expected in PAGE_RULES} == {True, False}


# --- on create (S2', S10, S1) -----------------------------------------------

VARIANTS = [
    {"name": "Control", "is_control": True, "traffic_allocation": 50},
    {"name": "B", "is_control": False, "traffic_allocation": 50},
]
METRIC = {"name": "Clicks", "event_name": "click_buy"}
CHANGES = {"version": 1, "changes": [{"op": "hide", "selector": ".banner"}]}


def _create(**overrides):
    payload = {"name": "visual", "variants": VARIANTS, "metrics": [METRIC]}
    payload.update(overrides)
    return ExperimentCreate(**payload)


def _create_errors(**overrides):
    try:
        _create(**overrides)
    except ValidationError as error:
        return [(e["type"], e["loc"], e["msg"]) for e in error.errors()]
    return []


def test_a_visual_experiment_may_be_created_without_a_metric():
    assert _create(visual_config=_config(), metrics=[]).metrics == []


def test_any_other_experiment_still_needs_one():
    (error,) = _create_errors(metrics=[])
    assert error[:2] == ("too_short", ("metrics",))


def test_v54_on_create_a_goal_names_one_of_the_metrics():
    goal = _goal("click_buy", "click", selector="#buy")
    assert _create_errors(visual_config=_config(goals=[goal])) == []
    errors = _create_errors(visual_config=_config(goals=[goal]), metrics=[])
    assert errors == [
        (
            "visual_config",
            ("visual_config", "goals", 0, "event_name"),
            visual.GOAL_WITHOUT_METRIC_REFUSED,
        )
    ]


def test_page_changes_need_visual_config_and_a_non_control_variant():
    with_changes = [VARIANTS[0], {**VARIANTS[1], "visual_changes": CHANGES}]
    on_control = [{**VARIANTS[0], "visual_changes": CHANGES}, VARIANTS[1]]
    assert _create_errors(visual_config=_config(), variants=with_changes) == []
    assert _create_errors(variants=with_changes) == [
        (
            "visual_changes",
            ("variants", 1, "visual_changes"),
            visual.CHANGES_WITHOUT_CONFIG_REFUSED,
        )
    ]
    assert _create_errors(visual_config=_config(), variants=on_control) == [
        (
            "visual_changes",
            ("variants", 0, "visual_changes"),
            visual.CONTROL_CHANGES_REFUSED,
        )
    ]


def test_a_split_url_experiment_cannot_be_visual():
    """R3's last clause, on the schema."""
    split = {
        "variants": [
            {
                "name": "Control",
                "url": "https://shop.example.com/a",
                "traffic_allocation": 50,
            },
            {
                "name": "B",
                "url": "https://shop.example.com/b",
                "traffic_allocation": 50,
            },
        ]
    }
    errors = _create_errors(
        experiment_type="split_url", split_url_config=split, visual_config=_config()
    )
    assert errors == [
        ("visual_config", ("visual_config",), visual.SPLIT_URL_VISUAL_REFUSED)
    ]


def test_no_start_date_on_create_or_update():
    """V71a2: the one writer of a draft's start date is PUT /schedule."""
    from backend.app.schemas.experiment import ExperimentUpdate

    assert "start_date" not in ExperimentCreate.model_fields
    assert "start_date" not in ExperimentUpdate.model_fields


def test_the_stable_update_keeps_dropping_unknown_keys():
    """R11: the visual schemas forbid unknown keys; ExperimentUpdate does not (#595)."""
    from backend.app.schemas.experiment import ExperimentUpdate

    assert ExperimentUpdate.model_config.get("extra", "ignore") == "ignore"
    assert ExperimentUpdate(name="x", not_a_field=1).model_dump(exclude_unset=True) == {
        "name": "x"
    }
