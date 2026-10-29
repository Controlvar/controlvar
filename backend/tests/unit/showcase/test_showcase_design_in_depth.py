"""Video 9, Design an experiment in depth (#1066): the storyboard, and the
claims it makes that no frame can prove.

* It is registered, loads, narrates by the rules (G1-G3, D84) and never shows
  a guardrail or the SDK hint; its share boxes are replaced by selecting the
  number, never by emptying the box.
* The targeting rule it declares admits exactly the visitors the narration
  names ("US or Canada"), and everyone else gets the control: decided by the
  server's own eligibility check, on the rule exactly as the builder sends it.
* No caption, sentence, typed string or card line reads like one of the
  values gate 7 keeps off screen (the render's own matcher, against
  ``config.STATIC_NEEDLES`` read at run time; a finding names a value by its
  index only).
* A correct API answer passes gate 10's ``design`` end state for this board.

If a product change makes one of these fail, re-verify what the video says
before changing the test: the narration makes the claim.
"""

from __future__ import annotations

import copy
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest
from showcase.capture import config, gates, narration, storyboard
from showcase.render import ocr

from backend.app.services.assignment_service import AssignmentService
from backend.app.services.rules_evaluation_service import RulesEvaluationService
from backend.tests.unit.showcase.test_showcase_design_end import DRAWN, OWNER, saved

pytestmark = [pytest.mark.unit]

SLUG = "09-design-in-depth"
SHOWCASE = Path(__file__).resolve().parents[4] / "tests" / "acceptance" / "showcase"


def board() -> storyboard.Storyboard:
    return storyboard.load(SHOWCASE / config.VIDEOS[SLUG])


def every_step(beat: Any) -> List[Any]:
    return [*beat.before, *beat.steps, *beat.exit]


# ---------------------------------------------------------------------------
# The storyboard
# ---------------------------------------------------------------------------


def test_video_9_registers_just_before_video_4_on_the_demo_seed_with_no_app_or_override():
    assert config.VIDEOS[SLUG] == "storyboards/09-design-in-depth.yaml"
    assert list(config.VIDEOS)[-2:] == [SLUG, "04-streampulse-rollout"]
    made = board()
    assert made.seed == "demo"
    assert made.apps == []
    assert made.traffic is None and made.setup is None


def test_video_9_narrates_by_the_rules_and_states_its_one_departure():
    made = board()
    assert narration.board_problems(made) == []
    assert storyboard.departure_problems(made) == []
    first = made.scenes[0]
    assert first.signed_in
    assert [d.says for d in first.departures] == ["Signed in off camera"]
    assert first.beats[0].cue.startswith("Signed in off camera.")


def test_video_9_never_shows_a_guardrail_or_the_sdk_hint():
    made = board()
    assert "guardrail" in made.never_show
    assert "Assign a user from your app" in made.never_show


def test_no_line_of_video_9_says_an_excluded_visitor_is_left_out_of_the_results():
    """An excluded visitor gets the control and is not enrolled; an event it
    sends with the experiment's key is still stored, so the narration claims
    only what assignment does."""
    made = board()
    lines = [
        text.lower()
        for scene in made.scenes
        for beat in scene.beats
        for text in (beat.cue, *beat.say)
    ]
    assert not [line for line in lines if "counted in the results" in line]
    assert any("isn't enrolled" in line for line in lines)


def test_video_9_replaces_each_share_by_selecting_it_never_by_emptying_the_box():
    shares = [
        step
        for scene in board().scenes
        for beat in scene.beats
        for step in every_step(beat)
        if isinstance(step, storyboard.Type)
        and (step.type.testid or "").startswith("variant-allocation-")
    ]
    assert [step.text for step in shares] == ["34", "33", "33"]
    assert all(step.select and not step.clear for step in shares)


def test_video_9_records_what_the_review_and_the_page_showed_for_gate_10():
    made = board()
    end = made.end_state
    assert end.experiments_listed is None and end.design is not None
    review = next(
        beat
        for scene in made.scenes
        for beat in scene.beats
        if "review-variants" in beat.record
    )
    assert sorted(review.record) == [
        "review-analysis",
        "review-metrics",
        "review-targeting",
        "review-variants",
    ]
    assert dict(end.design.shown) == {
        "review_variants": "review-variants",
        "review_metrics": "review-metrics",
        "review_targeting": "review-targeting",
        "review_analysis": "review-analysis",
        "detail_analysis": "analysis",
        "detail_bayesian": "bayesian",
    }


def test_a_planted_correct_answer_passes_gate_10_for_the_loaded_board():
    """The board as the capture loads it (its ``select`` steps included): the
    API's answer for exactly the declared experiment, and the rows the product
    draws for it, pass gate 10's ``design`` end state; one changed share does
    not."""
    made = board()
    end = made.end_state.design
    answer = saved(end)
    ok, numbers, why = gates.design_end(
        end,
        named=[answer],
        total=made.seed_counts.experiments + 1,
        seed_experiments=made.seed_counts.experiments,
        owner_id=OWNER,
        readings=DRAWN,
    )
    assert ok, why
    assert numbers["experiments_in_api"] == 4
    answer["variants"][0]["traffic_allocation"] = 35
    answer["variants"][1]["traffic_allocation"] = 32
    ok, _, why = gates.design_end(
        end,
        named=[answer],
        total=made.seed_counts.experiments + 1,
        seed_experiments=made.seed_counts.experiments,
        owner_id=OWNER,
        readings=DRAWN,
    )
    assert not ok
    assert "Control" in why


# ---------------------------------------------------------------------------
# Who can join: the server's own decision on the declared rule
# ---------------------------------------------------------------------------

#: The visitors the narration's "US or Canada" admits, and some it does not
#: ("Everyone else gets the control"). A context is what an app sends.
ADMITTED: Tuple[Dict[str, Any], ...] = (
    {"country": "US"},
    {"country": "CA"},
    {"user": {"country": "CA"}},
)
REFUSED: Tuple[Dict[str, Any], ...] = (
    {"country": "DE"},
    {},
    {"country": "us"},
    {"country": "US, CA"},
)


def as_the_builder_sends(targeting: Dict[str, Any]) -> Dict[str, Any]:
    """The declared rule as the targeting builder sends it: every group and
    condition carries the builder's own ``id`` (``id-<time>-<n>``)."""
    rule = copy.deepcopy(targeting)
    count = 0
    for group in rule["groups"]:
        count += 1
        group["id"] = f"id-1761620000000-{count}"
        for condition in group["conditions"]:
            count += 1
            condition["id"] = f"id-1761620000000-{count}"
    return rule


def declared_rule() -> Dict[str, Any]:
    design = board().end_state.design
    return as_the_builder_sends(design.targeting.model_dump())


def eligibility(rule: Dict[str, Any], context: Dict[str, Any]) -> Tuple[bool, str]:
    """``AssignmentService.check_eligibility``, the server's own decision for a
    new visitor, on an experiment holding *rule*, with no holdout and no
    exclusion group (the video sets neither) and no database."""
    service = AssignmentService.__new__(AssignmentService)
    service.rules_evaluation_service = RulesEvaluationService()
    service.db = None
    experiment = SimpleNamespace(
        id=uuid.uuid4(), targeting_rules=rule, mutual_exclusion_group_id=None
    )
    answer = service.check_eligibility(
        "visitor-9", experiment, context, holdout_check=(False, 0, 0)
    )
    return bool(answer["eligible"]), str(answer["reason"])


def wrong_answers(rule: Dict[str, Any]) -> List[str]:
    """Every declared visitor the rule decides otherwise than the narration says."""
    wrong = []
    for context in ADMITTED:
        got = eligibility(rule, context)
        if got != (True, "assigned"):
            wrong.append(f"{context} should join; the server says {got}")
    for context in REFUSED:
        got = eligibility(rule, context)
        if got != (False, "targeting"):
            wrong.append(f"{context} should get the control; the server says {got}")
    return wrong


def test_the_declared_rule_is_the_one_the_narration_names():
    rule = declared_rule()
    assert len(rule["groups"]) == 1
    (condition,) = rule["groups"][0]["conditions"]
    assert (condition["attribute"], condition["operator"], condition["value"]) == (
        "user.country",
        "in",
        "US, CA",
    )


def test_the_declared_rule_admits_exactly_us_and_canada():
    assert wrong_answers(declared_rule()) == []


def _planted(value: Any = None, *, empty_group: bool = False) -> Dict[str, Any]:
    rule = declared_rule()
    if empty_group:
        rule["groups"][0]["conditions"] = []
    else:
        rule["groups"][0]["conditions"][0]["value"] = value
    return rule


@pytest.mark.parametrize(
    "rule",
    [
        pytest.param(_planted("US CA"), id="a-missing-comma-admits-nobody"),
        pytest.param(_planted("us, ca"), id="lower-case-codes-admit-nobody"),
        pytest.param(_planted(empty_group=True), id="an-empty-group-admits-everyone"),
    ],
)
def test_a_rule_that_admits_others_than_the_narration_says_is_caught(rule):
    assert wrong_answers(rule) != []


# ---------------------------------------------------------------------------
# Gate 7's matcher over every line the video draws or says
# ---------------------------------------------------------------------------


def drawn_and_said(made: storyboard.Storyboard) -> List[str]:
    """Every caption, sentence, typed string and card line of *made*."""
    lines: List[str] = []
    for scene in made.scenes:
        if scene.card is not None:
            lines.extend(scene.card.drawn_text())
        for beat in scene.beats:
            lines.append(beat.cue)
            lines.extend(beat.say)
            lines.extend(
                step.text
                for step in every_step(beat)
                if isinstance(step, storyboard.Type)
            )
    return lines


def screened(lines: List[str]) -> List[Tuple[int, str]]:
    """Gate 7's findings per line, by line number and needle index only."""
    return [
        (number, what)
        for number, line in enumerate(lines)
        for _gate, what in ocr.findings(line, config.STATIC_NEEDLES)
    ]


def test_no_line_of_video_9_reads_like_a_kept_value():
    lines = drawn_and_said(board())
    assert len(lines) > 30
    assert screened(lines) == []


def near_copy(index: int) -> str:
    """A caption-like line holding the static value *index* with one character
    changed: made at run time, never written down."""
    value = config.STATIC_NEEDLES[index]
    middle = len(value) // 2
    swapped = "q" if value[middle] != "q" else "z"
    return f"Name it {value[:middle]}{swapped}{value[middle + 1 :]} here."


@pytest.mark.parametrize("index", range(len(config.STATIC_NEEDLES)))
def test_the_screen_fires_on_a_line_made_from_a_kept_value(index):
    lines = drawn_and_said(board())
    lines[3] = near_copy(index)
    found = screened(lines)
    wanted = f"needle {index + 1} of {len(config.STATIC_NEEDLES)}"
    assert [number for number, _ in found] != []
    assert any(what.startswith(wanted) for _, what in found)
