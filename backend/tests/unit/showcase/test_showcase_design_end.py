"""Gate 10 "design" (#1066, video 9): an experiment the video designs and
leaves a draft, read back through the API.

The storyboard declares the experiment (``storyboard.DesignEnd``); the
judge (``gates.design_end``) holds the saved experiment to it, property by
property, and holds the Review rows and the experiment page's analysis rows to
the product's own words for what was saved. Each refusal is shown firing on
one planted defect, and the product's words are pinned to frontend/src. No
browser, Docker, network or database.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import yaml
from showcase import contract
from showcase.capture import gates, run, storyboard

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
FRONTEND = REPO / "frontend" / "src"
V9 = (
    REPO
    / "tests"
    / "acceptance"
    / "showcase"
    / "storyboards"
    / "09-design-in-depth.yaml"
)
OWNER = "6b1f0c1e-0000-4000-8000-000000000001"
SEED = 3

#: What the product draws for the real board's experiment, written out by
#: hand (not rendered by the mirrors under test): the Review rows at beat 10
#: and the experiment page's analysis rows at beat 11.
DRAWN = {
    "review-variants": "Control: 34% (control)\nTreatment: 33%\nVariant 2: 33%\n",
    "review-metrics": (
        "Purchase (purchase, Conversion) — primary metric\n"
        "Add to cart (add_to_cart, Conversion)"
    ),
    "review-targeting": "Targeting: 1 rule group",
    "review-analysis": (
        "95% confidence · Benjamini-Hochberg correction · Bayesian analysis on"
    ),
    "analysis": "95% confidence ·  Benjamini-Hochberg",
    "bayesian": "On",
}


def board_data() -> Dict[str, Any]:
    """The real board as data, less any ``select`` on a step: this file tests
    the end state, which does not depend on how a value was typed."""
    data = yaml.safe_load(V9.read_text(encoding="utf-8"))
    for scene in data["scenes"]:
        for beat in scene["beats"]:
            for part in ("before", "steps", "exit"):
                for step in beat.get(part) or []:
                    step.pop("select", None)
    return data


def design() -> storyboard.DesignEnd:
    return storyboard.DesignEnd.model_validate(board_data()["end_state"]["design"])


def saved(end: storyboard.DesignEnd) -> Dict[str, Any]:
    """The API's answer for the experiment exactly as the board declares it,
    with the ids the builder adds to each targeting group and condition."""
    rules = None
    if end.targeting is not None:
        rules = end.targeting.model_dump()
        for g, group in enumerate(rules["groups"]):
            group["id"] = f"group-{g}"
            for c, condition in enumerate(group["conditions"]):
                condition["id"] = f"condition-{g}-{c}"
    return {
        "id": "0b6b4f1e-0000-4000-8000-0000000000e9",
        "name": end.name,
        "key": end.key,
        "description": None,
        "status": "draft",
        "owner_id": OWNER,
        "experiment_type": end.type,
        "variants": [
            {
                "id": f"variant-{i}",
                "name": v.name,
                "description": None,
                "traffic_allocation": v.allocation,
                "is_control": v.control,
                "configuration": v.configuration,
            }
            for i, v in enumerate(end.variants)
        ],
        "metrics": [
            {
                "id": f"metric-{i}",
                "name": m.name,
                "event_name": m.event,
                "metric_type": m.type,
                "is_primary": m.primary,
                "expected_effect": m.mde,
                "lower_is_better": m.lower_is_better,
            }
            for i, m in enumerate(end.metrics)
        ],
        "targeting_rules": rules,
        "confidence_level": end.analysis.confidence,
        "correction_method": end.analysis.correction,
        "bayesian_enabled": end.analysis.bayesian,
        "sequential_testing_enabled": False,
        "variance_reduction_config": None,
        "mutual_exclusion_group_id": None,
        "optimization_type": "fixed",
        "split_url_config": None,
        "start_date": None,
        "end_date": None,
        "resume_at": None,
    }


def judge(
    answer: Dict[str, Any] | None = None,
    *,
    named: List[Dict[str, Any]] | None = None,
    total: int = SEED + 1,
    owner: Any = OWNER,
    readings: Dict[str, str] | None = None,
    end: storyboard.DesignEnd | None = None,
):
    end = end or design()
    if named is None:
        named = [answer if answer is not None else saved(end)]
    return gates.design_end(
        end,
        named=named,
        total=total,
        seed_experiments=SEED,
        owner_id=owner,
        readings=DRAWN if readings is None else readings,
    )


def refused(answer: Dict[str, Any], reason: str, **kwargs: Any) -> None:
    ok, _, why = judge(answer, **kwargs)
    assert not ok, "the plant passed"
    assert reason in why, why


# ---------------------------------------------------------------------------
# The real board, and a planted correct answer
# ---------------------------------------------------------------------------


def test_the_real_boards_design_parses():
    end = design()
    assert (end.name, end.key, end.status, end.type) == (
        "Delivery promise",
        "delivery_promise",
        "draft",
        "a_b",
    )
    assert [(v.name, v.allocation, v.control) for v in end.variants] == [
        ("Control", 34, True),
        ("Treatment", 33, False),
        ("Variant 2", 33, False),
    ]
    assert [(m.name, m.event, m.primary, m.mde) for m in end.metrics] == [
        ("Purchase", "purchase", True, None),
        ("Add to cart", "add_to_cart", False, None),
    ]
    assert end.targeting.model_dump() == {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "user.country", "operator": "in", "value": "US, CA"}
                ],
            }
        ],
    }
    assert set(end.shown) == {
        "review_variants",
        "review_metrics",
        "review_targeting",
        "review_analysis",
        "detail_analysis",
        "detail_bayesian",
    }


def test_the_real_board_parses_with_its_end_state():
    board = storyboard.parse(board_data(), source="09-design-in-depth.yaml")
    assert board.end_state.design == design()
    assert board.traffic is None


def test_a_planted_correct_answer_passes():
    ok, numbers, why = judge()
    assert ok, why
    assert numbers["shown"]["review_variants"] == [
        "Control: 34% (control)",
        "Treatment: 33%",
        "Variant 2: 33%",
    ]
    assert numbers["experiments_in_api"] == SEED + 1


def test_the_api_order_of_variants_and_metrics_does_not_matter():
    answer = saved(design())
    answer["variants"] = list(reversed(answer["variants"]))
    answer["metrics"] = [answer["metrics"][1], answer["metrics"][0]]
    ok, _, why = judge(answer)
    assert ok, why


def test_a_configuration_is_compared_parsed():
    data = board_data()
    data["end_state"]["design"]["variants"][1]["configuration"] = {"a": 1, "b": [2]}
    end = storyboard.DesignEnd.model_validate(data["end_state"]["design"])
    answer = saved(end)
    answer["variants"][1]["configuration"] = {"b": [2], "a": 1}
    readings = dict(DRAWN)
    readings["review-variants"] = (
        'Control: 34% (control)\nTreatment: 33%, configuration {"a":1,"b":[2]}\n'
        "Variant 2: 33%"
    )
    ok, _, why = judge(answer, end=end, readings=readings)
    assert ok, why
    readings["review-variants"] = readings["review-variants"].replace('"a":1', '"a":2')
    ok, _, why = judge(answer, end=end, readings=readings)
    assert not ok and "review_variants showed" in why


# ---------------------------------------------------------------------------
# Gate 10 "design": one plant per property
# ---------------------------------------------------------------------------


def test_a_second_experiment_with_the_name_is_refused():
    one = saved(design())
    ok, _, why = judge(named=[one, dict(one, id="another")])
    assert not ok and "2 experiments are named 'Delivery promise'" in why
    ok, _, why = judge(named=[])
    assert not ok and "0 experiments are named" in why


def test_another_key_is_refused():
    refused(dict(saved(design()), key="delivery_promise_2"), "has key")


def test_an_experiment_that_is_not_a_draft_is_refused():
    refused(dict(saved(design()), status="active"), "is 'active', not 'draft'")


def test_another_owner_is_refused():
    refused(saved(design()), "not the signed-in user", owner="someone-else")
    refused(saved(design()), "not the signed-in user", owner=None)


def test_an_api_total_other_than_the_seed_and_one_is_refused():
    refused(saved(design()), "the API holds 5 experiments", total=SEED + 2)


def test_another_type_is_refused():
    refused(dict(saved(design()), experiment_type="mv"), "is type 'mv'")


def test_a_variant_at_0_percent_is_refused():
    answer = saved(design())
    answer["variants"][2]["traffic_allocation"] = 0
    answer["variants"][1]["traffic_allocation"] = 66
    refused(answer, "variant 'Treatment' is 66%")


def test_another_control_is_refused():
    answer = saved(design())
    answer["variants"][0]["is_control"] = False
    answer["variants"][1]["is_control"] = True
    refused(answer, "variant 'Control' has is_control False")


def test_a_configuration_the_storyboard_did_not_set_is_refused():
    answer = saved(design())
    answer["variants"][1]["configuration"] = {"steps": 1}
    refused(answer, "variant 'Treatment''s configuration")


def test_a_missing_or_extra_variant_is_refused():
    answer = saved(design())
    answer["variants"] = answer["variants"][:2]
    refused(answer, "the variants are")
    answer = saved(design())
    answer["variants"][2]["name"] = "Treatment"
    refused(answer, "the variants are")


def test_another_metric_is_refused():
    answer = saved(design())
    answer["metrics"][1]["metric_type"] = "revenue"
    refused(answer, "metric 'Add to cart' is")
    answer = saved(design())
    answer["metrics"][1]["event_name"] = "add-to-cart"
    refused(answer, "metric 'Add to cart' is")
    answer = saved(design())
    answer["metrics"][0]["is_primary"] = False
    answer["metrics"][1]["is_primary"] = True
    refused(answer, "metric 'Purchase' is")
    answer = saved(design())
    answer["metrics"] = answer["metrics"][:1]
    refused(answer, "the metrics are")


def test_a_planned_effect_the_storyboard_did_not_type_is_refused():
    answer = saved(design())
    answer["metrics"][0]["expected_effect"] = 0.1
    refused(answer, "has the planned effect 0.1")


def test_a_planned_effect_is_held_to_the_typed_one():
    data = board_data()
    data["end_state"]["design"]["metrics"][0]["mde"] = 0.1
    end = storyboard.DesignEnd.model_validate(data["end_state"]["design"])
    ok, _, why = judge(saved(end), end=end)
    assert ok, why
    answer = saved(end)
    answer["metrics"][0]["expected_effect"] = 0.1000001
    ok, _, why = judge(answer, end=end)
    assert not ok and "storyboard typed 0.1" in why
    answer["metrics"][0]["expected_effect"] = None
    ok, _, why = judge(answer, end=end)
    assert not ok and "storyboard typed 0.1" in why


def test_lower_is_better_set_is_refused():
    answer = saved(design())
    answer["metrics"][1]["lower_is_better"] = True
    refused(answer, "has lower_is_better True")


def test_a_targeting_value_typed_differently_is_refused():
    answer = saved(design())
    answer["targeting_rules"]["groups"][0]["conditions"][0]["value"] = "US,CA"
    refused(answer, "the stored targeting differs")


def test_a_top_level_targeting_id_is_still_a_difference():
    answer = saved(design())
    answer["targeting_rules"]["id"] = "rule-1"
    refused(answer, "the stored targeting differs")


def test_an_id_inside_a_condition_value_is_not_dropped():
    data = board_data()
    condition = data["end_state"]["design"]["targeting"]["groups"][0]["conditions"][0]
    condition["operator"] = "equals"
    end = storyboard.DesignEnd.model_validate(data["end_state"]["design"])
    answer = saved(end)
    answer["targeting_rules"]["groups"][0]["conditions"][0]["operator"] = "in"
    ok, _, why = judge(answer, end=end)
    assert not ok and "the stored targeting differs" in why


def test_no_targeting_when_the_storyboard_declares_some_is_refused():
    answer = saved(design())
    answer["targeting_rules"] = None
    refused(answer, "the stored targeting differs")


def test_targeting_when_the_storyboard_lets_everyone_join_is_refused():
    data = board_data()
    del data["end_state"]["design"]["targeting"]
    end = storyboard.DesignEnd.model_validate(data["end_state"]["design"])
    readings = dict(DRAWN, **{"review-targeting": "Targeting: everyone"})
    for empty in (None, {}, {"logical_operator": "AND", "groups": []}):
        answer = dict(saved(end), targeting_rules=empty)
        ok, _, why = judge(answer, end=end, readings=readings)
        assert ok, why
    answer = saved(design())
    ok, _, why = judge(answer, end=end, readings=readings)
    assert not ok and "lets everyone join" in why


def test_another_confidence_correction_or_bayesian_setting_is_refused():
    refused(dict(saved(design()), confidence_level=0.99), "confidence_level is 0.99")
    refused(
        dict(saved(design()), correction_method="bonferroni"),
        "correction_method is 'bonferroni'",
    )
    refused(dict(saved(design()), bayesian_enabled=False), "bayesian_enabled is False")


@pytest.mark.parametrize(
    "key,value",
    [
        ("sequential_testing_enabled", True),
        ("variance_reduction_config", {"enabled": True}),
        ("mutual_exclusion_group_id", "8c2e0000-0000-4000-8000-000000000001"),
        ("optimization_type", "thompson_sampling"),
        ("split_url_config", {"urls": {}}),
        ("start_date", "2026-11-01T00:00:00Z"),
        ("end_date", "2026-12-01T00:00:00Z"),
        ("resume_at", "2026-11-15T00:00:00Z"),
    ],
)
def test_a_field_guided_setup_never_sets_is_refused(key, value):
    refused(dict(saved(design()), **{key: value}), f"has {key}")


@pytest.mark.parametrize(
    "where,key",
    [
        ("experiment", "end_date"),
        ("experiment", "owner_id"),
        ("experiment", "bayesian_enabled"),
        ("variant", "configuration"),
        ("metric", "lower_is_better"),
    ],
)
def test_an_absent_key_is_refused_never_defaulted(where, key):
    answer = saved(design())
    if where == "experiment":
        del answer[key]
    elif where == "variant":
        del answer["variants"][0][key]
    else:
        del answer["metrics"][0][key]
    refused(answer, f"has no {key!r}")


def test_a_wrong_review_row_is_refused():
    for record, drawn, kind in (
        (
            "review-variants",
            "Control: 50% (control)\nTreatment: 50%",
            "review_variants",
        ),
        (
            "review-variants",
            "Variant 2: 33%\nTreatment: 33%\nControl: 34% (control)",
            "review_variants",
        ),
        (
            "review-metrics",
            "Purchase (purchase, Revenue) — primary metric\n"
            "Add to cart (add_to_cart, Conversion)",
            "review_metrics",
        ),
        ("review-targeting", "Targeting: everyone", "review_targeting"),
        (
            "review-analysis",
            "95% confidence · Benjamini-Hochberg correction",
            "review_analysis",
        ),
    ):
        readings = dict(DRAWN, **{record: drawn})
        ok, _, why = judge(readings=readings)
        assert not ok, record
        assert f"{kind} showed" in why, why


def test_a_wrong_detail_row_is_refused():
    for record, drawn, kind in (
        ("analysis", "99% confidence · Benjamini-Hochberg", "detail_analysis"),
        ("analysis", "95% confidence · no correction", "detail_analysis"),
        ("bayesian", "Off", "detail_bayesian"),
    ):
        ok, _, why = judge(readings=dict(DRAWN, **{record: drawn}))
        assert not ok and f"{kind} showed" in why, why


def test_a_reading_never_recorded_is_refused():
    readings = {k: v for k, v in DRAWN.items() if k != "bayesian"}
    ok, _, why = judge(readings=readings)
    assert not ok and "no reading 'bayesian' was recorded" in why


def test_the_mirrors_name_each_setting_as_the_product_does():
    assert gates.format_confidence(0.95) == "95%"
    assert gates.format_confidence(0.9) == "90%"
    assert gates.format_confidence(0.925) == "92.5%"
    assert gates.describe_settings(0.99, "none") == "99% confidence · no correction"
    assert (
        gates.describe_settings(0.9, "bonferroni")
        == "90% confidence · Bonferroni correction"
    )
    assert gates.correction_name("none") == "None"


# ---------------------------------------------------------------------------
# Refused at load
# ---------------------------------------------------------------------------


def refused_at_load(change, reason: str) -> None:
    data = board_data()
    change(data)
    with pytest.raises(storyboard.StoryboardError) as raised:
        storyboard.parse(copy.deepcopy(data), source="09-design-in-depth.yaml")
    assert reason in str(raised.value), str(raised.value)


def _design(data):
    return data["end_state"]["design"]


def test_load_refuses_a_design_with_a_flag_or_an_experiment():
    def with_flag(data):
        data["end_state"]["flag"] = {
            "name": "f",
            "key": "f",
            "segment": "s",
            "rollout_percentage": 10,
            "status": "active",
            "audit": ["feature_flag_create"],
            "shown": "analysis",
        }

    refused_at_load(with_flag, "a design end state stands alone")

    def with_experiment(data):
        data["end_state"]["experiment"] = {
            "name": "e",
            "key": "e",
            "status": "active",
            "winner": "Control",
            "shown": "analysis",
            "mde": 0.05,
            "planned": "analysis",
            "effect": "analysis",
        }

    refused_at_load(with_experiment, "a design end state stands alone")


def test_load_refuses_a_design_with_traffic():
    def with_traffic(data):
        data["traffic"] = {
            "experiment": "Delivery promise",
            "users": 1000,
            "seed": 1,
            "control_rate": 0.1,
            "treatment_rate": 0.2,
            "event": "purchase",
        }
        data["scenes"][0]["traffic"] = "run"

    refused_at_load(with_traffic, "its storyboard sends no traffic")


def test_load_refuses_a_repeated_variant_name():
    refused_at_load(
        lambda d: _design(d)["variants"][2].update(name="Treatment"),
        "a variant name repeats",
    )


def test_load_refuses_allocations_that_do_not_total_100():
    refused_at_load(
        lambda d: _design(d)["variants"][2].update(allocation=32),
        "add up to 99, not 100",
    )


def test_load_refuses_other_than_one_control():
    refused_at_load(
        lambda d: _design(d)["variants"][1].update(control=True),
        "exactly one variant is the control",
    )
    refused_at_load(
        lambda d: _design(d)["variants"][0].update(control=False),
        "exactly one variant is the control",
    )


def test_load_refuses_a_configuration_over_the_dashboards_limit():
    # {"text":"..."} is the text plus 11 bytes in its compact form.
    big = {"text": "x" * (storyboard.CONFIGURATION_MAX_BYTES - 10)}
    refused_at_load(
        lambda d: _design(d)["variants"][1].update(configuration=big),
        "configuration is 16385 bytes; the guided setup takes at most 16384",
    )
    fits = {"text": "x" * (storyboard.CONFIGURATION_MAX_BYTES - 11)}
    data = board_data()
    _design(data)["variants"][1]["configuration"] = fits
    storyboard.DesignEnd.model_validate(_design(data))


def test_load_refuses_a_repeated_metric_name():
    refused_at_load(
        lambda d: _design(d)["metrics"][1].update(name="Purchase"),
        "a metric name repeats",
    )


def test_load_refuses_other_than_one_primary_metric():
    refused_at_load(
        lambda d: _design(d)["metrics"][1].update(primary=True),
        "exactly one metric is primary",
    )
    refused_at_load(
        lambda d: _design(d)["metrics"][0].update(primary=False),
        "exactly one metric is primary",
    )


def test_load_refuses_an_mde_on_a_metric_that_is_not_primary():
    refused_at_load(
        lambda d: _design(d)["metrics"][1].update(mde=0.1),
        "is not the primary metric",
    )


def test_load_refuses_a_setting_the_guided_setup_does_not_offer():
    refused_at_load(
        lambda d: _design(d)["analysis"].update(confidence=0.8),
        "confidence 0.8 is not one the guided setup offers",
    )
    refused_at_load(
        lambda d: _design(d)["analysis"].update(correction="holm"),
        "correction 'holm' is not one the guided setup offers",
    )


def _condition(data):
    return _design(data)["targeting"]["groups"][0]["conditions"][0]


def test_load_refuses_an_operator_the_builder_does_not_offer():
    refused_at_load(
        lambda d: _condition(d).update(operator="is one of"),
        "is not one the targeting builder offers",
    )
    refused_at_load(
        lambda d: _condition(d).update(operator="in_segment", value="x"),
        "a segment's id is made at run time",
    )


def test_load_refuses_a_value_the_builder_does_not_send():
    refused_at_load(
        lambda d: _condition(d).update(value=["US", "CA"]),
        "the builder sends the value box's text",
    )
    refused_at_load(
        lambda d: _condition(d).update(operator="is_null"),
        "has no value box",
    )


def test_load_refuses_a_design_that_compares_no_review_row():
    def no_review(data):
        shown = _design(data)["shown"]
        for kind in [k for k in shown if k.startswith("review_")]:
            del shown[kind]

    refused_at_load(no_review, "at least one Review row")


def test_load_refuses_a_reading_no_beat_records():
    refused_at_load(
        lambda d: _design(d)["shown"].update(review_metrics="review-metric-rows"),
        "compares review_metrics with 'review-metric-rows', which no beat records",
    )


def test_load_refuses_an_unknown_reading_kind():
    refused_at_load(
        lambda d: _design(d)["shown"].update(detail_targeting="analysis"),
        "detail_targeting",
    )


def test_an_end_state_with_only_a_design_is_something():
    end = storyboard.EndState.model_validate(
        {
            "signed_in_as": "admin@demo.com",
            "design": board_data()["end_state"]["design"],
        }
    )
    assert end.design is not None


# ---------------------------------------------------------------------------
# The product's words, pinned to frontend/src
# ---------------------------------------------------------------------------


def source(relative: str) -> str:
    return (FRONTEND / relative).read_text(encoding="utf-8")


WIZARD = "components/experiments/new/Wizard.tsx"
SETTINGS = "components/results/shared/analysisSettings.ts"
DETAIL = "pages/experiments/[id].tsx"


def test_the_review_rows_are_drawn_as_the_mirrors_say():
    wizard = source(WIZARD)
    for piece in (
        "{v.name}: {v.traffic_allocation}%{v.is_control ? ' (control)' : ''}",
        "{', configuration '}",
        "{JSON.stringify(v.configuration)}",
        "{m.name} (<code className=\"font-mono\">{m.event_name}</code>,{' '}",
        "{METRIC_TYPE_LABELS[m.metric_type]}){m.is_primary ? ' — primary metric' : ''}",
        "? `Targeting: ${state.rules.groups.length} rule ${",
        "state.rules.groups.length === 1 ? 'group' : 'groups'",
        ": 'Targeting: everyone'}",
        "{payload.bayesian_enabled ? ' · Bayesian analysis on' : ''}",
        'data-testid="review-variants"',
        'data-testid="review-metrics"',
        'data-testid="review-targeting"',
        'data-testid="review-analysis"',
    ):
        assert piece in wizard, piece
    assert gates.CONFIGURATION_SEPARATOR == ", configuration "
    assert gates.BAYESIAN_ON == " · Bayesian analysis on"


def test_the_review_analysis_row_is_describe_settings():
    wizard = source(WIZARD)
    assert re.search(
        r"\{describeSettings\(\s*payload\.confidence_level \?\? state\.confidenceLevel,"
        r"\s*payload\.correction_method \?\? state\.correctionMethod,\s*\)\}",
        wizard,
    )


def test_the_settings_words_are_the_products():
    text = source(SETTINGS)
    assert "case 'benjamini_hochberg':\n      return 'Benjamini-Hochberg';" in text
    assert "case 'bonferroni':\n      return 'Bonferroni';" in text
    assert "default:\n      return 'None';" in text
    assert "return `${Number((level * 100).toFixed(1))}%`;" in text
    assert (
        "const correction = method === 'none' ? 'no correction' :"
        " `${correctionName(method)} correction`;"
    ) in text
    assert "return `${formatConfidence(level)} confidence · ${correction}`;" in text
    assert gates.CORRECTION_NAMES == {
        "benjamini_hochberg": "Benjamini-Hochberg",
        "bonferroni": "Bonferroni",
    }


def test_the_metric_type_labels_are_the_products():
    text = source("types/experiments.ts")
    block = text.split("export const METRIC_TYPE_LABELS")[1].split("};")[0]
    found = dict(re.findall(r"(\w+): '([^']+)'", block))
    assert found == gates.METRIC_TYPE_LABELS


def test_the_experiment_pages_analysis_rows_are_drawn_as_the_mirrors_say():
    text = source(DETAIL)
    for piece in (
        'data-testid="experiment-analysis"',
        "{formatConfidence(experiment.confidence_level)} confidence ·{' '}",
        "{experiment.correction_method === 'none'",
        "? 'no correction'",
        ": correctionName(experiment.correction_method)}",
        "<dd data-testid=\"experiment-bayesian\">{experiment.bayesian_enabled ? 'On' : 'Off'}</dd>",
    ):
        assert piece in text, piece


def test_the_settings_the_guided_setup_offers_are_the_products():
    text = source(SETTINGS)
    assert "export const CONFIDENCE_OPTIONS = [0.9, 0.95, 0.99] as const;" in text
    assert (
        "export const CORRECTION_OPTIONS: CorrectionMethod[] = ['benjamini_hochberg',"
        " 'bonferroni', 'none'];"
    ) in text
    assert storyboard.CONFIDENCE_OPTIONS == (0.9, 0.95, 0.99)
    assert storyboard.CORRECTION_OPTIONS == ("benjamini_hochberg", "bonferroni", "none")


def test_the_operators_are_the_builders():
    text = source("types/targeting.ts")
    block = text.split("export type OperatorType =")[1].split(";")[0]
    offered = re.findall(r"'([a-z_]+)'", block)
    assert set(offered) == set(storyboard.BUILDER_OPERATORS) | set(
        storyboard.SEGMENT_OPERATORS
    )
    assert len(offered) == len(set(offered))
    row = source("components/targeting/ConditionRow.tsx")
    assert "const NO_VALUE_OPERATORS: OperatorType[] = ['is_null', 'is_not_null'];" in (
        row
    )
    assert storyboard.NO_VALUE_OPERATORS == ("is_null", "is_not_null")


def test_the_configuration_limit_is_the_dashboards():
    text = source("components/experiments/new/formState.ts")
    assert "export const CONFIGURATION_MAX_BYTES = 16_384;" in text
    assert storyboard.CONFIGURATION_MAX_BYTES == 16_384


# ---------------------------------------------------------------------------
# design_state: what gate 10 reads from the API
# ---------------------------------------------------------------------------


def test_design_state_reads_the_list_the_experiment_and_the_user(monkeypatch):
    end = design()
    full = saved(end)
    listed = {"items": [{"id": full["id"], "name": end.name}], "total": SEED + 1}
    asked: List[str] = []

    def answer(url, token=None, body=None):
        asked.append(url)
        assert token == "token" and body is None
        if url.endswith("/api/v1/experiments/?limit=100"):
            return listed
        if url.endswith(f"/api/v1/experiments/{full['id']}"):
            return full
        if url.endswith("/api/v1/auth/me"):
            return {"id": OWNER, "email": "admin@demo.com"}
        raise AssertionError(url)

    monkeypatch.setattr(run, "_api", answer)
    ok, numbers, why = run.design_state(end, "http://api", "token", DRAWN, SEED)
    assert ok, why
    assert asked == [
        "http://api/api/v1/experiments/?limit=100",
        f"http://api/api/v1/experiments/{full['id']}",
        "http://api/api/v1/auth/me",
    ]
    listed["total"] = SEED + 2
    ok, _, why = run.design_state(end, "http://api", "token", DRAWN, SEED)
    assert not ok and "the API holds 5 experiments" in why


def test_design_state_does_not_read_one_experiment_when_two_have_the_name(
    monkeypatch,
):
    end = design()
    item = {"id": "a", "name": end.name}

    def answer(url, token=None, body=None):
        if url.endswith("?limit=100"):
            return {"items": [item, dict(item, id="b")], "total": SEED + 2}
        if url.endswith("/auth/me"):
            return {"id": OWNER}
        raise AssertionError(url)

    monkeypatch.setattr(run, "_api", answer)
    ok, _, why = run.design_state(end, "http://api", "token", DRAWN, SEED)
    assert not ok and "2 experiments are named" in why


def test_gate_10_judges_a_design_end_state():
    """The gate-10 block hands a design to design_state with the seed's count."""
    text = Path(run.__file__).read_text(encoding="utf-8")
    block = text.split("# Gate 10: the end state, through the API.")[1].split(
        'gates["10"]'
    )[0]
    assert "if end.design is not None:" in block
    assert "design_state(" in block
    assert "board.seed_counts.experiments" in block


def test_the_manifest_keeps_gate_10_and_the_render_refuses_one_without_it(tmp_path):
    from backend.tests.unit.showcase.capture_dirs import default_manifest, make_capture

    assert "10" in contract.REQUIRED_CAPTURE_GATES
    assert "api" not in contract.REQUIRED_CAPTURE_GATES
    manifest = default_manifest()
    del manifest["gates"]["10"]
    root = make_capture(tmp_path / "c", manifest=manifest)
    with pytest.raises(
        contract.CaptureRefused, match="capture gate 10 is not in the manifest"
    ):
        contract.validate_capture_dir(root)


# ---------------------------------------------------------------------------
# What the video sets beyond guided setup (``moved``), and nothing else
# ---------------------------------------------------------------------------

#: The setup's group and the start the video set, as stand-ins: the run makes
#: the group's id, and the start is taken when the video sets it.
GROUP = "8c2e1d4a-1111-4222-8333-0000000000a1"
START = "2026-10-30T09:00:00Z"


def test_untouched_keeps_its_8_rows():
    assert [k for k, _ in gates.UNTOUCHED] == [
        "sequential_testing_enabled",
        "variance_reduction_config",
        "mutual_exclusion_group_id",
        "optimization_type",
        "split_url_config",
        "start_date",
        "end_date",
        "resume_at",
    ]


def design_after(**changes) -> Tuple[bool, Dict[str, Any], str]:
    end = design()
    answer = dict(
        saved(end),
        sequential_testing_enabled=True,
        mutual_exclusion_group_id=GROUP,
        start_date="2026-10-30T09:00:00",
    )
    answer.update(changes)
    return gates.design_end(
        end,
        named=[answer],
        total=4,
        seed_experiments=3,
        owner_id=answer["owner_id"],
        readings=DRAWN,
        moved={
            "sequential_testing_enabled": True,
            "mutual_exclusion_group_id": GROUP,
            "start_date": START,
        },
    )


def test_design_end_passes_with_what_the_video_moved():
    ok, _numbers, why = design_after()
    assert ok, why


@pytest.mark.parametrize(
    "changes, words",
    [
        ({"status": "active"}, "is 'active', not 'draft'"),
        ({"sequential_testing_enabled": False}, "has sequential_testing_enabled False"),
        ({"bayesian_enabled": False}, "bayesian_enabled is False"),
        ({"start_date": "2026-10-30T10:00:00"}, "has start_date '2026-10-30T10:00:00'"),
        ({"end_date": "2026-12-01T00:00:00"}, "has end_date"),
        ({"optimization_type": "thompson_sampling"}, "has optimization_type"),
    ],
)
def test_design_end_refuses_anything_else_moved(changes, words):
    ok, _numbers, why = design_after(**changes)
    assert not ok and words in why, why
