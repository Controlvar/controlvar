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
* Its clips: each sentence's clip key (24 already in the voice cache, and the
  2 new ones the founder's voice sitting makes), and no sentence saying a
  date, a clock time or what the global holdout is not.

If a product change makes one of these fail, re-verify what the video says
before changing the test: the narration makes the claim.
"""

from __future__ import annotations

import copy
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest
import yaml
from showcase import contract
from showcase.capture import config, gates, narration, storyboard
from showcase.render import ocr

from backend.app.services.assignment_service import AssignmentService
from backend.app.services.rules_evaluation_service import RulesEvaluationService
from backend.tests.unit.showcase.test_showcase_design_end import DRAWN, OWNER

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
    assert made.traffic is None
    # Its one setup: the exclusion group the re-record adds the draft to (D182).
    assert made.setup.segments == []
    assert [g.name for g in made.setup.exclusion_groups] == ["Checkout tests"]


def test_video_9_narrates_by_the_rules_and_states_its_departures():
    made = board()
    assert narration.board_problems(made) == []
    assert storyboard.departure_problems(made) == []
    first = made.scenes[0]
    assert first.signed_in
    assert [d.says for d in first.departures] == [
        "Signed in off camera",
        "made off camera",
    ]
    assert first.beats[0].cue.startswith("Signed in off camera.")
    assert [s.id for s in made.scenes] == ["design"]
    assert storyboard.required_departures(made) == [("design", "off-camera setup")]
    # The group made off camera is said in ONE caption, on its own beat (D188).
    captions = [beat.cue for beat in first.beats if "made off camera" in beat.cue]
    assert captions == ["Checkout tests, made off camera with Checkout Button Color."]


def _console_scene(data: Dict[str, Any]) -> None:
    data["scenes"][0]["console"] = {"label": "Signed in to the API off camera"}


def _call_on_a_beat(data: Dict[str, Any]) -> None:
    data["scenes"][0]["beats"][0]["call"] = {
        "method": "GET",
        "path": "/api/v1/holdout",
        "show": ["is_active"],
        "answer": {"status": 200},
    }


def _note_on_a_beat(data: Dict[str, Any]) -> None:
    data["scenes"][0]["beats"][0]["note"] = "Drawn by the tool."


def _send_step(data: Dict[str, Any]) -> None:
    data["scenes"][0]["beats"][0]["steps"].append({"send": True})


#: Pydantic's words for a key the schema does not have.
EXTRA = r"\s+Extra inputs are not permitted"


@pytest.mark.parametrize(
    "change, words",
    [
        (_console_scene, r"scenes\.0\.console" + EXTRA),
        (_call_on_a_beat, r"scenes\.0\.beats\.0\.call" + EXTRA),
        (_note_on_a_beat, r"scenes\.0\.beats\.0\.note" + EXTRA),
        (_send_step, re.escape("beat 1 step 2: unknown step ['send']")),
    ],
    ids=["console", "call", "note", "send"],
)
def test_the_retired_api_part_is_refused_at_load(change, words):
    """The console scene is gone from the schema (D182): a board that still
    asks for one, or for a call, a note or a send, does not load."""
    data = raw_board()
    change(data)
    with pytest.raises(storyboard.StoryboardError, match=words):
        storyboard.parse(data)


def kinds_of(beat: Any) -> List[Tuple[str, str]]:
    """``(step kind, test id)`` for each on-camera step of *beat*, in order."""
    found = []
    for step in beat.steps:
        (locator,) = storyboard._locators(step) or [None]
        found.append((type(step).__name__, locator.testid if locator else ""))
    return found


def test_the_size_is_shown_locked_by_opening_the_q4_rows_edit_form():
    """Narration review condition 2: while "its size is fixed" is said, beat
    16 CLICKS the Q4 row's Edit (a look would leave the form shut, and the
    capture would wait for the locked size in vain), then looks at the locked
    size; the Activate click before it ends beat 15."""
    beats = board().scenes[0].beats
    assert beats[15].say == ["It's active now, and its size is fixed."]
    steps = kinds_of(beats[15])
    assert ("Click", "holdout-edit") in steps
    assert ("Look", "holdout-size-locked") in steps
    assert steps.index(("Click", "holdout-edit")) < steps.index(
        ("Look", "holdout-size-locked")
    )
    exit_ = [
        (type(s).__name__, s.click.testid)
        for s in beats[14].exit
        if isinstance(s, storyboard.Click)
    ]
    assert exit_ == [("Click", "holdout-confirm-yes")]


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
        "detail_sequential": "detail-sequential",
        "detail_start": "detail-start",
        "detail_group": "detail-group",
        "detail_holdout": "detail-holdout",
        "detail_note": "api-settings-note",
    }


def test_a_planted_correct_answer_passes_gate_10_for_the_loaded_board():
    """The board as the capture loads it (its ``select`` steps included): the
    API's answer for exactly the declared experiment, and the rows the product
    draws for it, pass gate 10's ``design`` end state; one changed share does
    not."""
    from backend.tests.unit.showcase import test_showcase_design_set as set_

    made = board()
    end = made.end_state.design
    answer = set_.answered()
    given = {
        "named": [answer],
        "total": made.seed_counts.experiments + 1,
        "seed_experiments": made.seed_counts.experiments,
        "owner_id": OWNER,
        "readings": {**DRAWN, **set_.PAYOFF},
        "moved": {
            "sequential_testing_enabled": True,
            "start_date": set_.DATED["scheduled-start"]["instant"],
            "mutual_exclusion_group_id": set_.GROUP,
        },
        "extra": {
            "start_shown": set_.DATED["scheduled-start"]["shown"],
            "group_name": "Checkout tests",
            "holdout": set_.HOLDOUT,
        },
    }
    ok, numbers, why = gates.design_end(end, **given)
    assert ok, why
    assert numbers["experiments_in_api"] == 4
    answer["variants"][0]["traffic_allocation"] = 35
    answer["variants"][1]["traffic_allocation"] = 32
    ok, _, why = gates.design_end(end, **given)
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
    # 19 captions, 26 sentences and 11 typed strings: every one is screened.
    assert len(lines) == 56
    assert "Delivery promise" in lines and "Q4 holdout" in lines
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


# ---------------------------------------------------------------------------
# The clips it needs
# ---------------------------------------------------------------------------

#: The sentences already in the voice cache, and their clip keys: the design
#: part's 13 and 11 of the API part's (both 10-29 sittings), byte for byte the
#: same, so no new clip is made for them.
KEPT_KEYS: Dict[str, str] = {
    "Choose New Experiment.": (
        "3eaa033a60ed7bb342de251eabae586c6d99581b47cc80922cb401d4c327dea2"
    ),
    "Name it, and the key your code uses fills itself in.": (
        "aa651bdcd6d99962fbfe4756d0ba76ebc24c1b90504bb798894e61ce3404ebab"
    ),
    "Make purchases the primary metric: the one you decide on.": (
        "96cb7be81cfa004bf6367cb7e5f3128aa94a42145b1e103404d5e44a858d8dc9"
    ),
    "Add a second metric to track alongside it: add to cart.": (
        "aec81f87ad4ca81c855b161e9b867f9c529710cec7654ca1121fc0b65a172c9d"
    ),
    "Add a third version, and split the traffic so it reaches 100%.": (
        "3395e59ccb57bf528c86b90d6c7732eb1ecacaaaaa4904289b20c4ced35fdd4e"
    ),
    "Choose who can join from what your app sends: US or Canada.": (
        "7c9860831ecef5205b6c54b584e8b07aab59578193876d4fda89539cb639f56f"
    ),
    "Everyone else gets the control, and isn't enrolled.": (
        "f499597c0ed4b625b6b80261bf3ed68037d2d3971d75ef27c41b34b1e877085f"
    ),
    "Next, a confidence level and a correction, which lock once it starts.": (
        "7f03d856d3b3a84cb2400780fdb1b615070bef2b19d4188e04124fa377c72614"
    ),
    "With several versions, one can look like a winner by luck.": (
        "ab6648c5b3139ee254782812524e6c56544bc07d40b60d61e8092f31d8aefb3e"
    ),
    "Benjamini-Hochberg, the default, keeps false winners to about 5% of all"
    " winners.": "773dc09955c377718fbadd1728f0da1759d5343b9dea3d3d9905c17838f24cf5",
    "Turn on Bayesian to also see each version's chance of being best.": (
        "2cd57f5f2d42ca5f33f0838ee6fab1b026b51bd2e6fdc23888c05ff0e4a200d7"
    ),
    "Review your choices, then create it.": (
        "4661fb415fc437a327f1304e6256d5108eebeb55571135e84af73384b1b48fb6"
    ),
    "It's saved as a draft, with its versions, metrics and analysis.": (
        "75bf554a773f9a3fe7d49671ff7d66e487afa21e74c9da6f0cda58394f009f62"
    ),
    "Turn on sequential testing, in beta.": (
        "3afe3df24150d13ca09dbe841139df1345414f6d12a9f3e7712082b67a424633"
    ),
    "Check results whenever you like, and stop once the evidence is strong.": (
        "db3290140562d00a66fd16f1b9b865e481194c77b566a5d450cba377eb4eb531"
    ),
    "Give it a start date, and it starts on its own.": (
        "2904aaaad2279f32415070e917ad61452bf4b1b6374307b29ed81c57b31acbc6"
    ),
    "Until then, it stays a draft.": (
        "04ebdbfce7946bc64c376035e18356d92326e7d297b5972c336c85e8f9c6b981"
    ),
    "A global holdout keeps 5% of new users out of every experiment.": (
        "7e3bcb9d20bf752e7b36375445564f1a24a5713672b4943977189ecfeae51be7"
    ),
    "That includes this one.": (
        "ccfac8f7a6db8706d17cba0f96816cde433bc53c5602a9264854b4a8510863d6"
    ),
    "Experiments in the same exclusion group never share a user.": (
        "4ce7f7653f2738a92213f2a1d5b84176631d59837565f0a9634031dd73028898"
    ),
    "Add this draft to the group with Checkout Button Color.": (
        "3e15d81bb92ef3fdc1d8da90b362629fa49eb9a55126818cfe8b1c0dddb7f1b3"
    ),
    "Read it back: it's still a draft, in its group.": (
        "0869e3343b5d8e67ebf1c6136ce56f85cf8f7808e418b66332a3a15a8adccca6"
    ),
    "It has its start date, and sequential testing is on.": (
        "4900ee378ba1f0648c60dce4e3a834636f6688c2d1e4136c4ea1d14ff612281a"
    ),
    "And the holdout is active.": (
        "1bdf8720af6d78bd305e29e9c8b1292cc663d9e73756d850b03c997badafd6aa"
    ),
}
#: The two new sentences, each a new clip, in the order they are said: the
#: lines of the founder's voice sitting (QA 7a; founder-voice-v9-ui.sh refuses
#: unless its approved lines are exactly these).
NEW_KEYS: Dict[str, str] = {
    "A holdout covers all your experiments, so it has its own page.": (
        "774b3446c56e3babca5df35e8152306d48d32774a7294129651d482b9f6110b3"
    ),
    "It's active now, and its size is fixed.": (
        "e968c1417b3adb613f3b8e370900733a121d2c6d5f07b6c4278b07cc99177ebf"
    ),
}
#: The old closing card's sentence, and its key: never said again.
API_ONLY_LINE = (
    "Set holdouts, exclusion groups, schedules and sequential testing through the API."
)
API_ONLY_KEY = "c9ce71ce9b54fbb2415a8eac9202141892a075a4df94c9f53ea2f4a509d07a9e"


def raw_board() -> Dict[str, Any]:
    """Video 9's storyboard as written, to change one thing and parse again."""
    return yaml.safe_load((SHOWCASE / config.VIDEOS[SLUG]).read_text(encoding="utf-8"))


def clip_keys(made: storyboard.Storyboard) -> Dict[str, str]:
    """Each sentence of *made* and its clip's key, as the voice step makes it."""
    return {
        text: contract.clip_key("Ruth", contract.polly_text(text))
        for _scene, _beat, _line, text in storyboard.narration_lines(made)
    }


#: The cached sentences the API part said and this video no longer does.
DROPPED = (
    "Guided setup ends here: you do the rest through the API.",
    "The answer also carries the group's id and the start date.",
)


def test_video_9s_clips_are_the_24_kept_and_the_2_new():
    keys = clip_keys(board())
    assert (len(KEPT_KEYS), len(NEW_KEYS)) == (24, 2)
    assert keys == {**KEPT_KEYS, **NEW_KEYS}
    assert [text for text in keys if text in NEW_KEYS] == list(NEW_KEYS)
    assert API_ONLY_LINE not in keys and API_ONLY_KEY not in keys.values()
    assert not [text for text in DROPPED if text in keys]
    # The voice step's own count: 2 new clips, 101 billable characters.
    assert sum(len(contract.polly_text(text)) for text in NEW_KEYS) == 101


def test_one_character_changed_in_a_kept_sentence_moves_its_key():
    """The pin above fires: a kept sentence edited by one character is a new
    clip, and the clip it had is no longer used."""
    data = raw_board()
    data["scenes"][0]["beats"][0]["say"] = ["Choose New experiment."]
    keys = clip_keys(storyboard.parse(data))
    assert keys != {**KEPT_KEYS, **NEW_KEYS}
    assert sorted(set(KEPT_KEYS.values()) - set(keys.values())) == [
        KEPT_KEYS["Choose New Experiment."]
    ]


# ---------------------------------------------------------------------------
# What it never says
# ---------------------------------------------------------------------------

#: What the holdout is not: it keeps new users out of every experiment, so it
#: is never one experiment's, nor something an experiment sits behind.
NEVER_SAID = (API_ONLY_LINE, "behind a holdout", "this experiment's holdout")
#: A date or a time of day, said: the start date is drawn and never said, so
#: the narration stays true on every day the video is shown. The narration's
#: own rules (G1) do not refuse one.
DATE_OR_TIME = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend"
    r"|today|tonight|tomorrow|yesterday|midnight|noon|morning|evening"
    r"|o'clock|utc|gmt)\b"
    r"|\b[0-9]{1,2}:[0-9]{2}\b|\b[0-9]{1,2} ?(?:am|pm)\b"
    r"|\b[0-9]{4}-[0-9]{2}-[0-9]{2}\b|\b[0-9]{1,2}/[0-9]{1,2}\b",
    re.IGNORECASE,
)


def board_texts(made: storyboard.Storyboard) -> List[Tuple[str, str]]:
    """``(where, text)`` for every caption, sentence and card line."""
    found = []
    for scene in made.scenes:
        for text in storyboard.scene_screen_text(scene):
            found.append((f"scene {scene.id}", text))
        for number, beat in enumerate(scene.beats, start=1):
            for text in beat.say:
                found.append((f"scene {scene.id} beat {number}", text))
    return found


def unsaid_problems(made: storyboard.Storyboard) -> List[str]:
    return [
        f"{where} says {phrase!r}"
        for where, text in board_texts(made)
        for phrase in NEVER_SAID
        if phrase.lower() in text.lower()
    ]


def dated(made: storyboard.Storyboard) -> List[str]:
    """Every caption, sentence or card line holding a date or a time of day."""
    return [text for _where, text in board_texts(made) if DATE_OR_TIME.search(text)]


def test_video_9_never_says_what_the_holdout_is_not_nor_the_old_card():
    assert len(NEVER_SAID) == 3
    assert unsaid_problems(board()) == []


def test_saying_this_experiments_holdout_is_refused():
    data = raw_board()
    data["scenes"][0]["beats"][0]["say"].append(
        "That includes this experiment's holdout."
    )
    assert unsaid_problems(storyboard.parse(data)) == [
        'scene design beat 1 says "this experiment\'s holdout"'
    ]


def test_no_line_of_video_9_says_a_date_or_a_time():
    assert dated(board()) == []


@pytest.mark.parametrize(
    "sentence",
    [
        "It starts on October 30 at 9:00, on its own.",
        "Give it a start at 9:00, and it starts on its own.",
        "It starts tomorrow, on its own.",
    ],
)
def test_a_dated_sentence_passes_the_narrations_rules_and_is_refused_here(sentence):
    assert narration.sentence_problems(sentence) == []
    data = raw_board()
    data["scenes"][0]["beats"][0]["say"] = [sentence]
    assert dated(storyboard.parse(data)) == [sentence]
