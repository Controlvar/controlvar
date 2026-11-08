"""Video 10, Serve a value with a feature flag (#1512): the storyboard, gate 10's
value fields, and the claims it makes that no frame can prove.

* It is registered between videos 6 and 9, loads, narrates by the rules (G1-G3,
  D84), never names the JavaScript SDK, takes the default gain and has an empty
  override list.
* QA row 8: the key its card quotes is the key typed on camera and the end
  state's; the form's own key differs, so the edit is needed, and the beat
  that makes it says so (EM1512-3).
* QA row 9: each value said is the value typed into its field (found by its
  label, never a test id) and the end state's.
* Gate 10's ``flag`` end state for a flag that serves a value: every new field
  refused at load or at the gate on one planted defect, and the answers the
  board declares are the server's own for that rule.
* Gate 10 reads each declared answer after the last frame (``run.flag_answers``)
  against a fake server: all right passes; a wrong value or a wrong reason
  fails naming the user, the derived answer and the API's; an answer missing
  or not read as declared is refused, never passed; a flag that declares none
  reads nothing and makes no key.
* PE1512-2: video 3's verdict and its end state's dump are main's, byte for
  byte, on the row a pre-values API answers.

The pages of the docs each sentence rests on are checked in
test_showcase_quoted_docs.py (``CLAIMS_10``), which also runs on a pull request
that changes only the docs.
"""

from __future__ import annotations

import copy
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from unittest import mock
from unittest.mock import MagicMock
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml
from showcase import contract
from showcase.capture import config, gates, narration, run, storyboard

from backend.app.core import targeting_adapter
from backend.app.core.flag_values import served_value
from backend.app.core.flag_values import values_equal as api_values_equal
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.services import feature_flag_service
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.tests.unit.showcase import test_showcase_dashboard_testids as testids
from backend.tests.unit.showcase import test_showcase_flag_rollout as v3
from backend.tests.unit.showcase.test_showcase_design_in_depth import (
    as_the_builder_sends,
    drawn_and_said,
    screened,
)

pytestmark = [pytest.mark.unit]

SLUG = "10-serve-a-value"
REPO = Path(__file__).resolve().parents[4]
SHOWCASE = REPO / "tests" / "acceptance" / "showcase"
FRONTEND = REPO / "frontend" / "src"


def board() -> storyboard.Storyboard:
    return storyboard.load(SHOWCASE / config.VIDEOS[SLUG])


def raw_board() -> Dict[str, Any]:
    """Video 10's storyboard as written, to change one thing and parse again."""
    return yaml.safe_load((SHOWCASE / config.VIDEOS[SLUG]).read_text(encoding="utf-8"))


def changed(change: Callable[[Dict[str, Any]], None]) -> storyboard.Storyboard:
    data = raw_board()
    change(data)
    return storyboard.parse(data)


def every_step(made: storyboard.Storyboard) -> List[Any]:
    return [
        step
        for scene in made.scenes
        for beat in scene.beats
        for step in [*beat.before, *beat.steps, *beat.exit]
    ]


def source(relative: str) -> str:
    return (FRONTEND / relative).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The storyboard
# ---------------------------------------------------------------------------


def test_video_10_registers_between_videos_6_and_9_with_no_app_or_override():
    order = list(config.VIDEOS)
    assert config.VIDEOS[SLUG] == "storyboards/10-serve-a-value.yaml"
    assert order.index(SLUG) == order.index("06-shoplab-ab-test") + 1
    assert order.index(SLUG) + 1 == order.index("09-design-in-depth")
    # The two pins on the order stand unedited (PE1512-4).
    assert order[-2:] == ["09-design-in-depth", "04-streampulse-rollout"]
    assert order.index("05-plan-and-team") == 3
    made = board()
    assert made.slug == SLUG and made.title == "Serve a value with a feature flag"
    assert made.seed == "demo" and made.apps == [] and made.traffic is None
    assert made.setup is None
    from showcase.capture import overrides as override_lists

    listed = override_lists.load()
    assert listed[SLUG] == {}
    assert (
        override_lists.override_list(made, run.Ports(28400, 28401, 28402), listed) == []
    )
    # Its own gain (its clips measure louder than the default expects) and
    # the default cap.
    assert contract.voice_gain_db(SLUG) == 3.8
    assert contract.DEFAULT_VOICE_GAIN_DB == 4.5
    assert contract.max_video_ms(SLUG) == contract.MAX_VIDEO_MS


def test_video_10_narrates_by_the_rules_and_states_its_one_departure():
    made = board()
    assert narration.board_problems(made) == []
    assert storyboard.departure_problems(made) == []
    assert storyboard.required_departures(made) == [("flag", "off-camera setup")]
    assert [s.id for s in made.scenes] == ["flag", "sdk"]
    first = made.scenes[0]
    assert [d.says for d in first.departures] == ["Signed in off camera"]
    assert first.beats[0].cue.startswith("Signed in off camera.")
    # Every caption and every sentence, as the voice step and the render check
    # them (G1, G3 and the caption contract), each on its own.
    for scene in made.scenes:
        for beat in scene.beats:
            assert contract.caption_problems(beat.cue) == [], beat.cue
            assert narration.headline_problems(beat.cue, beat.say) == [], beat.cue
            for sentence in beat.say:
                assert narration.sentence_problems(sentence) == [], sentence
                assert contract.caption_problems(sentence) == [], sentence
    payoffs = [b.cue for s in made.scenes for b in s.beats if b.payoff]
    assert payoffs == ["Switch it on when your code is ready."]


def lines_of(made: storyboard.Storyboard) -> List[str]:
    """Every caption, sentence and card line."""
    found = []
    for scene in made.scenes:
        found += storyboard.scene_screen_text(scene)
        for beat in scene.beats:
            found += beat.say
    return found


def test_video_10_names_the_go_sdk_and_never_the_javascript_one():
    """PE1512-6: the card quotes go.md, and nothing said or drawn names
    JavaScript; the card is labelled for Go, and a sentence names Go."""
    made = board()
    card = made.scenes[1].card
    assert card.label == "From the Go SDK docs"
    assert card.code.section == "Flag values"
    assert card.code.doc.endswith("/sdk/go.md")
    assert not [
        line for line in lines_of(made) if re.search("javascript|js sdk", line, re.I)
    ]
    assert (
        "In your app, the Go SDK reads the value for each user."
        in made.scenes[1].beats[0].say
    )


def test_saying_javascript_is_caught():
    made = changed(
        lambda d: d["scenes"][1]["beats"][0]["say"].__setitem__(
            0, "In your app, the JavaScript SDK reads the value for each user."
        )
    )
    assert [line for line in lines_of(made) if re.search("javascript", line, re.I)]


def test_the_card_note_says_what_go_md_says_about_installing():
    """The note is true for go.md: go get takes the module's latest release
    (its words in go.md are checked in test_showcase_quoted_docs.py)."""
    note = board().scenes[1].card.note
    assert "go get installs the Go SDK's latest release" in note
    assert "head of the main branch" not in note


def test_the_type_is_chosen_by_role_and_the_values_typed_by_label():
    """QA: the value fields' test ids are built at run time, so the board finds
    them by their labels, the product's own (VALUE_FIELD_LABELS); the type by
    its radio's role and name (VALUE_TYPE_LABELS)."""
    steps = every_step(board())
    radios = [
        s.click
        for s in steps
        if isinstance(s, storyboard.Click) and s.click.role == "radio"
    ]
    assert [(r.name, r.exact) for r in radios] == [("String", True)]
    labelled = [
        (s.type.label, s.type.exact)
        for s in steps
        if isinstance(s, storyboard.Type) and s.type.label
    ]
    assert labelled == [("On value", True), ("Off value", True)]
    values = source("utils/flagValues.ts")
    for line in (
        "on_value: 'On value',",
        "off_value: 'Off value',",
        "string: 'String',",
    ):
        assert line in values, line
    assert not [
        loc.testid
        for s in steps
        for loc in storyboard._locators(s)
        if loc.testid and re.match(r"flag-o(n|ff)-value", loc.testid)
    ]


def test_every_test_id_video_10_names_is_one_the_dashboard_draws():
    ids = testids.board_ids(board())
    assert "rollout-value-hint" in ids and "flag-value-fields" in ids
    assert testids.missing(ids, testids.product_ids(testids.frontend_sources())) == []


def test_a_test_id_the_dashboard_does_not_draw_is_refused_for_video_10():
    made = changed(
        lambda d: d["scenes"][0]["beats"][5]["steps"][0]["look"].update(
            testid="rollout-hint"
        )
    )
    patterns = testids.product_ids(testids.frontend_sources())
    assert testids.missing(testids.board_ids(made), patterns) == ["rollout-hint"]


def test_no_line_of_video_10_reads_like_a_kept_value():
    lines = drawn_and_said(board())
    # 9 captions, 15 sentences, 6 typed strings and the card's 4 lines.
    assert len(lines) == 34
    assert screened(lines) == []


def test_three_sentences_are_other_videos_word_for_word_and_keep_their_clips():
    """The dry run's 3 cached clips: each the same sentence (so the same key)
    as an earlier video's; the other 12 sentences are new clips."""
    keys = {
        text: contract.clip_key("Ruth", contract.polly_text(text))
        for _s, _b, _l, text in storyboard.narration_lines(board())
    }
    elsewhere = set()
    for slug, relative in config.VIDEOS.items():
        if slug == SLUG:
            continue
        other = storyboard.load(SHOWCASE / relative)
        elsewhere |= {
            contract.clip_key("Ruth", contract.polly_text(text))
            for _s, _b, _l, text in storyboard.narration_lines(other)
        }
    kept = [text for text, key in keys.items() if key in elsewhere]
    assert kept == [
        "Choose New Flag.",
        "Name it, and the key your code uses fills itself in.",
        "Choose Create Feature Flag.",
    ]
    assert len(keys) == 15


# ---------------------------------------------------------------------------
# QA row 8: the card's key is the key typed and the end state's
# ---------------------------------------------------------------------------

CARD_KEY = re.compile(r'EvaluateFlag\(ctx, "([^"]+)"')


def key_problems(made: storyboard.Storyboard) -> List[str]:
    """Why the key on the card, the key in the form and the end state's key are
    not one key, or why the key's edit is not said, or nothing."""
    end = made.end_state.flag
    problems = []
    card_keys = [
        key
        for scene in made.scenes
        if scene.card is not None and scene.card.code is not None
        for line in scene.card.code.lines
        for key in CARD_KEY.findall(line)
    ]
    if card_keys != [end.key]:
        problems.append(
            f"the card quotes the keys {card_keys}; the end state's is {end.key!r}"
        )
    typed = [
        (scene, beat, step)
        for scene in made.scenes
        for beat in scene.beats
        for step in beat.steps
        if isinstance(step, storyboard.Type) and step.type.testid == "flag-key-input"
    ]
    form = typed[-1][2].text if typed else v3.form_key(end.name)
    if form != end.key:
        problems.append(
            f"the form holds the key {form!r}; the end state's is {end.key!r}"
        )
    if typed:
        _scene, beat, step = typed[-1]
        if not step.clear:
            problems.append("the key is typed after the form's own, not over it")
        # EM1512-3: shown in a caption or a sentence of that beat.
        if not any("change it" in text for text in (beat.cue, *beat.say)):
            problems.append("the beat that changes the key does not say so")
    return problems


def test_the_cards_key_is_the_key_typed_and_the_end_states():
    made = board()
    assert key_problems(made) == []
    end = made.end_state.flag
    # The form's own key differs: without the edit the card would name
    # another flag (the reason for the edit, EM1512-3).
    assert v3.form_key(end.name) == "checkout_button_label" != end.key
    assert end.key == "checkout-button-label"


def _key_step(data: Dict[str, Any]) -> Dict[str, Any]:
    return next(
        s
        for s in data["scenes"][0]["beats"][1]["steps"]
        if s.get("type", {}).get("testid") == "flag-key-input"
    )


@pytest.mark.parametrize(
    "change, words",
    [
        (
            lambda d: d["scenes"][1]["card"]["code"]["lines"].__setitem__(
                0,
                'label, _ := client.EvaluateFlag(ctx, "checkout_button_label", user)'
                " // a string flag",
            ),
            "the card quotes the keys ['checkout_button_label']",
        ),
        (
            lambda d: _key_step(d).update(text="checkout-button-labels"),
            "the form holds the key 'checkout-button-labels'",
        ),
        (
            lambda d: d["scenes"][0]["beats"][1]["steps"].remove(_key_step(d)),
            "the form holds the key 'checkout_button_label'",
        ),
        (
            lambda d: d["scenes"][0]["beats"][1].update(
                cue="Name it, and the key fills itself in.",
                say=["Name it, and the key your code uses fills itself in."],
            ),
            "does not say so",
        ),
        (
            lambda d: _key_step(d).pop("clear"),
            "not over it",
        ),
    ],
    ids=["card-key", "typed-key", "no-edit", "edit-unsaid", "not-cleared"],
)
def test_a_key_that_drifts_anywhere_is_caught(change, words):
    problems = key_problems(changed(change))
    assert any(words in p for p in problems), problems


# ---------------------------------------------------------------------------
# QA row 9: each value said is the value typed and the end state's
# ---------------------------------------------------------------------------

SAID_VALUE = re.compile(r"\bthe (on|off) value: (.+)\.$")
LABEL_FIELD = {"On value": "on_value", "Off value": "off_value"}


def value_problems(made: storyboard.Storyboard) -> List[str]:
    end = made.end_state.flag
    said: Dict[str, List[str]] = {}
    for _s, _b, _l, text in storyboard.narration_lines(made):
        found = SAID_VALUE.search(text)
        if found:
            said.setdefault(f"{found.group(1)}_value", []).append(found.group(2))
    typed: Dict[str, List[str]] = {}
    for step in every_step(made):
        if isinstance(step, storyboard.Type) and step.type.label in LABEL_FIELD:
            typed.setdefault(LABEL_FIELD[step.type.label], []).append(step.text)
    problems = []
    for field in ("on_value", "off_value"):
        wanted = getattr(end, field)
        if said.get(field) != [wanted]:
            problems.append(
                f"{field}: said {said.get(field)}, the end state's is {wanted!r}"
            )
        if typed.get(field) != [wanted]:
            problems.append(
                f"{field}: typed {typed.get(field)}, the end state's is {wanted!r}"
            )
    return problems


def test_each_value_said_is_the_value_typed_and_the_end_states():
    made = board()
    assert value_problems(made) == []
    end = made.end_state.flag
    assert (end.value_type, end.on_value, end.off_value) == (
        "string",
        "Complete purchase",
        "Buy now",
    )
    # The card's default is the flag's off value: what the docs' line returns
    # without an answer is what the flag serves everyone while it is off.
    assert 'fmt.Println(label.StringValue("Buy now"))' in made.scenes[1].card.code.lines


@pytest.mark.parametrize(
    "change, words",
    [
        (
            lambda d: d["scenes"][0]["beats"][3]["say"].__setitem__(
                1, "Everyone else gets the off value: Buy Now."
            ),
            "off_value: said ['Buy Now']",
        ),
        (
            lambda d: d["scenes"][0]["beats"][3]["steps"][0].update(
                text="Complete purchases"
            ),
            "on_value: typed ['Complete purchases']",
        ),
        (
            # The answer moves with it: an answer the off value does not match
            # is refused at load before this check is reached.
            lambda d: (
                d["end_state"]["flag"].update(off_value="Buy later"),
                d["end_state"]["flag"]["answers"][1].update(value="Buy later"),
            ),
            "off_value: said ['Buy now'], the end state's is 'Buy later'",
        ),
    ],
    ids=["said", "typed", "end-state"],
)
def test_a_value_that_drifts_anywhere_is_caught(change, words):
    problems = value_problems(changed(change))
    assert any(words in p for p in problems), problems


# ---------------------------------------------------------------------------
# FlagEnd: refused when the board loads
# ---------------------------------------------------------------------------


def _flag(data: Dict[str, Any]) -> Dict[str, Any]:
    return data["end_state"]["flag"]


def _answer(data: Dict[str, Any], index: int) -> Dict[str, Any]:
    return _flag(data)["answers"][index]


@pytest.mark.parametrize(
    "change, words",
    [
        # segment / rule
        (
            lambda d: _flag(d).update(segment="Beta testers"),
            "exactly one of segment and rule",
        ),
        (lambda d: _flag(d).pop("rule"), "exactly one of segment and rule"),
        (
            lambda d: _flag(d)["rule"].update(operator="in_segment"),
            "a segment's id is made at run time",
        ),
        (
            lambda d: _flag(d)["rule"].update(operator="is"),
            "not one the targeting builder offers",
        ),
        (
            lambda d: _flag(d)["rule"].update(rollout_percentage=50),
            "Extra inputs are not permitted",
        ),
        # value_type, on_value, off_value
        (lambda d: _flag(d).pop("off_value"), "declared together"),
        (lambda d: _flag(d).pop("value_type"), "declared together"),
        (lambda d: _flag(d).update(on_value=3), "is not a value a string flag serves"),
        (
            lambda d: _flag(d).update(off_value="Complete purchase"),
            "on_value equals off_value",
        ),
        (lambda d: _flag(d).update(value_type="color"), "value_type"),
        (
            lambda d: _flag(d).update(value_type="number", on_value=3, off_value=3.0),
            "on_value equals off_value",
        ),
        (
            lambda d: _flag(d).update(value_type="number", on_value=True, off_value=0),
            "is not a value a number flag serves",
        ),
        (
            lambda d: _flag(d).update(
                value_type="boolean", on_value=False, off_value=True
            ),
            "serves true when on and false when off",
        ),
        # answers
        (
            # PE1512-3's plant: a pro user said to get the off value by a rule.
            lambda d: _answer(d, 0).update(value="Buy now"),
            "its reason targeting_rule serves 'Complete purchase'",
        ),
        (
            lambda d: _answer(d, 1).update(value="Complete purchase"),
            "its reason rollout serves 'Buy now'",
        ),
        (
            lambda d: _flag(d).update(rollout_percentage=50),
            "would depend on the user's bucket",
        ),
        (
            lambda d: _answer(d, 1).update(reason="inactive"),
            "a flag left active never gives",
        ),
        (
            lambda d: _flag(d).update(status="inactive"),
            "a flag left inactive never gives",
        ),
        (lambda d: _answer(d, 0).update(reason="error"), "reason"),
        (
            lambda d: [
                _flag(d).pop(k) for k in ("value_type", "on_value", "off_value")
            ],
            "declare value_type, on_value and off_value",
        ),
        (lambda d: _answer(d, 0).update(user="a user"), "user"),
        # shown, when named, is recorded
        (lambda d: _flag(d).update(shown="audit"), "no beat records"),
    ],
)
def test_a_flag_end_state_out_of_shape_is_refused_at_load(change, words):
    data = raw_board()
    change(data)
    with pytest.raises(storyboard.StoryboardError, match=re.escape(words)):
        storyboard.parse(data)


@pytest.mark.parametrize(
    "change",
    [
        # At 100%, a user no rule matches gets the on value.
        lambda d: (
            _flag(d).update(rollout_percentage=100),
            _answer(d, 1).update(value="Complete purchase"),
        ),
        # Left off, everyone is answered inactive with the off value.
        lambda d: (
            _flag(d).update(status="inactive"),
            _flag(d).update(
                answers=[
                    {
                        "user": "user-123",
                        "context": {"plan": "pro"},
                        "value": "Buy now",
                        "reason": "inactive",
                    }
                ]
            ),
        ),
        # A number flag's 3 and 3.5 are two values.
        lambda d: (
            _flag(d).update(value_type="number", on_value=3, off_value=3.5),
            _answer(d, 0).update(value=3.0),
            _answer(d, 1).update(value=3.5),
        ),
    ],
    ids=["rollout-100", "inactive", "number"],
)
def test_answers_that_follow_from_their_reason_load(change):
    data = raw_board()
    change(data)
    storyboard.parse(data)


# ---------------------------------------------------------------------------
# Gate 10: the flag the video made, through the API
# ---------------------------------------------------------------------------

ADMIN = {"email": "admin@demo.com", "id": "u-admin"}


def api_flag(**changes: Any) -> Dict[str, Any]:
    """The API's answer for exactly the declared flag: the rule as the targeting
    builder sends it (its ids included)."""
    end = board().end_state.flag
    flag = {
        "id": "f-10",
        "name": end.name,
        "key": end.key,
        "status": "active",
        "rollout_percentage": 0,
        "targeting_rules": as_the_builder_sends(
            {
                "logical_operator": "AND",
                "groups": [
                    {"logical_operator": "AND", "conditions": [end.rule.model_dump()]}
                ],
            }
        ),
        "value_type": "string",
        "on_value": "Complete purchase",
        "off_value": "Buy now",
    }
    flag.update(changes)
    return flag


def entries(actions=("toggle_enable", "feature_flag_create")) -> List[Dict[str, Any]]:
    return [
        {"action_type": a, "user_email": ADMIN["email"], "user_id": ADMIN["id"]}
        for a in actions
    ]


def gate(end: Any = None, **changes: Any):
    if end is None:
        end = board().end_state.flag.model_copy(update={"answers": []})
    kwargs = {
        "named": [api_flag()],
        "segments": [],
        "audit": entries(),
        "actor": ADMIN,
        "shown": None,
    }
    kwargs.update(changes)
    return gates.flag_end(end, **kwargs)


def test_a_correct_answer_passes_gate_10_and_reads_no_segment_or_audit_table():
    ok, numbers, why = gate()
    assert ok, why
    assert (numbers["value_type"], numbers["on_value"], numbers["off_value"]) == (
        "string",
        "Complete purchase",
        "Buy now",
    )
    # Unset: no segment is looked for and no audit table is compared.
    assert "segments_named" not in numbers and "shown_rows" not in numbers
    assert numbers["audit"] == ["feature_flag_create", "toggle_enable"]


def test_declared_answers_are_never_passed_unread():
    """A board that declares answers is refused at the gate when they were not
    read (the read is ``run.flag_answers``, after the last frame), whatever
    else agrees."""
    ok, numbers, why = gate(end=board().end_state.flag)
    assert not ok and why == (
        "the storyboard declares 2 answers and the API was asked for none: an"
        " unread answer is never passed"
    )
    assert numbers["answers"] == []


def _rule(**condition: Any) -> Dict[str, Any]:
    rules = api_flag()["targeting_rules"]
    rules["groups"][0]["conditions"][0].update(condition)
    return rules


@pytest.mark.parametrize(
    "changes, words",
    [
        ({"named": [api_flag(value_type="number")]}, "the flag's type is 'number'"),
        ({"named": [api_flag(value_type=None)]}, "the flag's type is None"),
        (
            {"named": [api_flag(on_value="Complete purchase.")]},
            "the flag's on_value is 'Complete purchase.'",
        ),
        (
            {"named": [api_flag(off_value="Buy Now")]},
            "the flag's off_value is 'Buy Now'",
        ),
        ({"named": [api_flag(on_value=None)]}, "the flag's on_value is None"),
        (
            {"named": [api_flag(targeting_rules=_rule(attribute="plan"))]},
            "the flag's rule is 'plan'",
        ),
        (
            {"named": [api_flag(targeting_rules=_rule(value="Pro"))]},
            "the flag's rule is",
        ),
        (
            {"named": [api_flag(targeting_rules=_rule(operator="not_equals"))]},
            "the flag's rule is",
        ),
        (
            {
                "named": [
                    api_flag(targeting_rules={**_rule(), "rollout_percentage": 50})
                ]
            },
            "admits only part of the users it matches",
        ),
        (
            {
                "named": [
                    api_flag(targeting_rules={**_rule(), "rollout_percentage": "100"})
                ]
            },
            "admits only part of the users it matches",
        ),
        (
            {
                "named": [
                    api_flag(
                        targeting_rules={
                            "groups": [
                                {
                                    "conditions": [
                                        _rule()["groups"][0]["conditions"][0]
                                    ]
                                    * 2
                                }
                            ]
                        }
                    )
                ]
            },
            "one group of one condition",
        ),
        (
            {
                "named": [
                    api_flag(
                        targeting_rules={"groups": [{"conditions": ["user.plan"]}]}
                    )
                ]
            },
            "one group of one condition",
        ),
        ({"named": [api_flag(targeting_rules=None)]}, "one group of one condition"),
        ({"named": [api_flag(rollout_percentage=10)]}, "at 10%, not 0%"),
        ({"named": [api_flag(status="inactive")]}, "not 'active'"),
        ({"named": [api_flag(key="checkout_button_label")]}, "has key"),
        ({"audit": entries(("feature_flag_create",))}, "audit entries are"),
    ],
)
def test_a_flag_end_state_that_disagrees_anywhere_is_refused(changes, words):
    ok, _numbers, why = gate(**changes)
    assert not ok and words in why, why


def test_the_rule_share_check_never_passes_a_rule_the_server_buckets_on():
    """Every stored rule gate 10 passes is one the server serves to every user
    it matches (``targeting_adapter.buckets_on_rule_id``); the gate refuses
    some the server would read as 100 (a string, a boolean), never the reverse."""
    base = _rule()
    for value in (
        None,
        "",
        100,
        100.0,
        150,
        99,
        99.9,
        0,
        50,
        "50",
        "100",
        True,
        False,
        float("nan"),
        [100],
    ):
        rules = {**base, "rollout_percentage": value}
        if gates.rule_admits_all(rules):
            assert not targeting_adapter.buckets_on_rule_id(rules), value
    assert gates.rule_admits_all(
        {k: v for k, v in base.items() if k != "rollout_percentage"}
    )
    assert not gates.rule_admits_all({**base, "rollout_percentage": 99})


@pytest.mark.parametrize(
    "a, b",
    [
        ("Buy now", "Buy now"),
        ("Buy now", "Buy Now"),
        (3, 3.0),
        (True, 1),
        (False, 0),
        ({"a": [1, 2]}, {"a": [1, 2.0]}),
        ({"a": 1}, {"a": 1, "b": 2}),
        ([1, 2], [2, 1]),
        (None, None),
        ("", None),
        ([], {}),
    ],
)
def test_values_equal_is_the_apis(a, b):
    assert storyboard.values_equal(a, b) is api_values_equal(a, b)


# ---------------------------------------------------------------------------
# The declared answers are the server's own for the declared rule
# ---------------------------------------------------------------------------


def server_answer(end: storyboard.FlagEnd, answer: storyboard.FlagAnswer, rules=None):
    """``(value, reason)``: the flag service's own decision for *answer*'s user,
    on the flag the end state declares, the rule as the builder sends it, with
    no database (PE F2's in-process check)."""
    flag = SimpleNamespace(
        id="00000000-0000-0000-0000-000000001512",
        key=end.key,
        status=FeatureFlagStatus.ACTIVE
        if end.status == "active"
        else FeatureFlagStatus.INACTIVE,
        rollout_percentage=end.rollout_percentage,
        targeting_rules=copy.deepcopy(
            rules if rules is not None else api_flag()["targeting_rules"]
        ),
        value_type=end.value_type,
        on_value=end.on_value,
        off_value=end.off_value,
    )
    with mock.patch.object(feature_flag_service, "MetricsService"):
        detail = FeatureFlagService(db=MagicMock()).evaluate_flag_detailed(
            flag, answer.user, dict(answer.context)
        )
    has_value, value = served_value(flag, detail["enabled"])
    assert has_value
    return value, detail["reason"]


def wrong_answers(end: storyboard.FlagEnd, rules=None) -> List[str]:
    wrong = []
    for answer in end.answers:
        got = server_answer(end, answer, rules)
        if got != (answer.value, answer.reason):
            wrong.append(f"{answer.user} {answer.context}: the server says {got}")
    return wrong


def test_the_declared_answers_are_the_servers_for_the_declared_rule():
    end = board().end_state.flag
    assert [(a.context, a.value, a.reason) for a in end.answers] == [
        ({"plan": "pro"}, "Complete purchase", "targeting_rule"),
        ({"plan": "business"}, "Buy now", "rollout"),
    ]
    assert wrong_answers(end) == []


@pytest.mark.parametrize(
    "rules, end_changes",
    [
        pytest.param(_rule(value="Pro"), {}, id="the-rule-a-pro-user-misses"),
        pytest.param(_rule(attribute="user.tier"), {}, id="another-attribute"),
        pytest.param(None, {"rollout_percentage": 100}, id="everyone-else-at-100"),
    ],
)
def test_a_rule_or_rollout_that_answers_otherwise_is_caught(rules, end_changes):
    end = board().end_state.flag.model_copy(update=end_changes)
    assert wrong_answers(end, rules) != []


# ---------------------------------------------------------------------------
# Gate 10 reads the declared answers after the last frame (#1512 stage 2)
# ---------------------------------------------------------------------------

API = "http://127.0.0.1:28401"
TOKEN = "made-up-session-token-0123456789"
GATE_KEY = "made-up-gate-key-0123456789abcdef"


class Kept:
    """The run's redactor, as far as the read uses it: the values kept."""

    def __init__(self) -> None:
        self.values: List[str] = []

    def add(self, value: str) -> None:
        self.values.append(value)


def refused(url: str, code: int) -> HTTPError:
    return HTTPError(url, code, "refused", {}, io.BytesIO(b"{}"))


class FakeServer:
    """The stack's API as gate 10 reads it after the last frame, with no
    network: the flag list, its audit log, who is signed in, the API-key route
    and the evaluate route. The evaluate route answers as the flag service
    does for the stored row, in process (``server_answer``), unless a test
    gives a user another reply (a dict) or an HTTP status (an int)."""

    def __init__(self, replies: Optional[Dict[str, Any]] = None):
        self.row = api_flag()
        self.replies = replies or {}
        #: The names of the API keys made, in order.
        self.keys: List[str] = []
        #: Each evaluate request: (flag key, user, context, the X-API-Key sent).
        self.asked: List[Any] = []

    def api(
        self,
        url: str,
        token: Optional[str] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        assert token == TOKEN, url
        path = urlsplit(url).path
        if path == "/api/v1/api-keys":
            self.keys.append(body["name"])
            return {"id": "k-1", "name": body["name"], "key": GATE_KEY}
        if path == "/api/v1/feature-flags/":
            return {"items": [self.row]}
        if path == "/api/v1/audit-logs/":
            return {"items": entries()}
        if path == "/api/v1/auth/me":
            return dict(ADMIN)
        raise AssertionError(f"gate 10 asked for {url}")

    def with_key(self, url: str, key: str) -> Any:
        parts = urlsplit(url)
        query = parse_qs(parts.query, strict_parsing=True)
        assert sorted(query) == ["context", "user_id"], url
        flag_key = unquote(parts.path.rsplit("/", 1)[1])
        user, context = query["user_id"][0], json.loads(query["context"][0])
        self.asked.append((flag_key, user, context, key))
        assert parts.path.startswith("/api/v1/feature-flags/evaluate/"), url
        if key != GATE_KEY:
            raise refused(url, 401)
        if flag_key != self.row["key"]:
            raise refused(url, 404)
        reply = self.replies.get(user)
        if isinstance(reply, int):
            raise refused(url, reply)
        if reply is not None:
            return reply
        stored = SimpleNamespace(
            **{
                name: self.row[name]
                for name in (
                    "key",
                    "status",
                    "rollout_percentage",
                    "value_type",
                    "on_value",
                    "off_value",
                )
            }
        )
        value, reason = server_answer(
            stored,
            SimpleNamespace(user=user, context=context),
            self.row["targeting_rules"],
        )
        return {
            "key": flag_key,
            "enabled": value == stored.on_value,
            "value": value,
            "value_type": stored.value_type,
            "config": None,
            "reason": reason,
        }


def read_gate(monkeypatch, server: FakeServer, end: Any = None):
    """Gate 10's flag half, as the run calls it after the last frame, against
    *server*: ``(ok, numbers, why)`` and the values the redactor kept."""
    monkeypatch.setattr(run, "_api", server.api)
    monkeypatch.setattr(run, "_api_with_key", server.with_key)
    kept = Kept()
    verdict = run.flag_state(
        end if end is not None else board().end_state.flag, API, TOKEN, {}, kept
    )
    return verdict, kept.values


def test_gate_10_reads_each_declared_answer_after_the_last_frame(monkeypatch):
    server = FakeServer()
    (ok, numbers, why), kept = read_gate(monkeypatch, server)
    assert ok, why
    # One key, made for the read, a needle from then on; every answer asked
    # with it, for the declared user and context, in order.
    assert server.keys == [run.GATE_KEY_NAME] == ["showcase-gate"]
    assert kept == [GATE_KEY]
    assert server.asked == [
        ("checkout-button-label", "user-123", {"plan": "pro"}, GATE_KEY),
        ("checkout-button-label", "user-456", {"plan": "business"}, GATE_KEY),
    ]
    assert numbers["answers"] == [
        {
            "user": "user-123",
            "context": {"plan": "pro"},
            "value": "Complete purchase",
            "reason": "targeting_rule",
        },
        {
            "user": "user-456",
            "context": {"plan": "business"},
            "value": "Buy now",
            "reason": "rollout",
        },
    ]


def answer(value: Any, reason: str) -> Dict[str, Any]:
    return {
        "key": "checkout-button-label",
        "enabled": value == "Complete purchase",
        "value": value,
        "value_type": "string",
        "config": None,
        "reason": reason,
    }


def without(reply: Dict[str, Any], name: str) -> Dict[str, Any]:
    return {key: value for key, value in reply.items() if key != name}


@pytest.mark.parametrize(
    "replies, why",
    [
        pytest.param(
            {"user-456": answer("Complete purchase", "rollout")},
            "the API answers user-456 {'plan': 'business'} with (value, reason)"
            " ('Complete purchase', 'rollout'); the storyboard derives ('Buy now',"
            " 'rollout')",
            id="a-wrong-value",
        ),
        pytest.param(
            {"user-123": answer("Complete purchase", "rollout")},
            "the API answers user-123 {'plan': 'pro'} with (value, reason)"
            " ('Complete purchase', 'rollout'); the storyboard derives"
            " ('Complete purchase', 'targeting_rule')",
            id="a-wrong-reason-rollout-for-targeting_rule",
        ),
        pytest.param(
            {"user-456": answer("Buy now", "targeting_rule")},
            "the API answers user-456 {'plan': 'business'} with (value, reason)"
            " ('Buy now', 'targeting_rule'); the storyboard derives ('Buy now',"
            " 'rollout')",
            id="a-wrong-reason-targeting_rule-for-rollout",
        ),
    ],
)
def test_an_answer_the_api_gives_otherwise_fails_gate_10_naming_the_user(
    monkeypatch, replies, why
):
    (ok, _numbers, said), _kept = read_gate(monkeypatch, FakeServer(replies))
    assert not ok and said == why


@pytest.mark.parametrize(
    "replies, words",
    [
        pytest.param({"user-456": 404}, "with HTTP 404;", id="refused-404"),
        pytest.param({"user-123": 500}, "with HTTP 500;", id="refused-500"),
        pytest.param(
            {"user-456": without(answer("Buy now", "rollout"), "value")},
            "with no value or no reason;",
            id="no-value",
        ),
        pytest.param(
            {
                "user-123": without(
                    answer("Complete purchase", "targeting_rule"), "reason"
                )
            },
            "with no value or no reason;",
            id="no-reason",
        ),
        pytest.param({"user-456": []}, "with [], with no value", id="not-an-object"),
    ],
)
def test_a_missing_answer_is_refused_never_passed(monkeypatch, replies, words):
    (ok, _numbers, why), _kept = read_gate(monkeypatch, FakeServer(replies))
    assert (
        not ok and words in why and why.endswith(": an unread answer is never passed")
    ), why


@pytest.mark.parametrize(
    "answered, words",
    [
        pytest.param(None, "asked for none", id="not-read"),
        pytest.param("first-only", "asked for 1", id="the-second-user-skipped"),
        pytest.param("twice", "asked for 3", id="one-read-twice"),
        pytest.param("another-user", "was read for 'user-999'", id="another-user"),
        pytest.param("another-context", "was read for 'user-456' {}", id="no-context"),
    ],
)
def test_answers_not_read_as_declared_are_refused_never_passed(answered, words):
    end = board().end_state.flag
    read = [
        {
            "user": a.user,
            "context": dict(a.context),
            "answer": answer(a.value, a.reason),
        }
        for a in end.answers
    ]
    changed_read = {
        None: None,
        "first-only": read[:1],
        "twice": [*read, read[1]],
        "another-user": [read[0], {**read[1], "user": "user-999"}],
        "another-context": [read[0], {**read[1], "context": {}}],
    }[answered]
    ok, _numbers, why = gate(end=end, answered=changed_read)
    assert (
        not ok and words in why and why.endswith(": an unread answer is never passed")
    ), why
    # The control: the same reading, as declared, passes.
    ok, _numbers, why = gate(end=end, answered=read)
    assert ok, why


def test_a_flag_with_no_declared_answers_reads_none_and_makes_no_key(monkeypatch):
    """PE1512-2: unset answers read nothing, make no key, add no numbers."""
    server = FakeServer()
    end = board().end_state.flag.model_copy(update={"answers": []})
    (ok, numbers, why), kept = read_gate(monkeypatch, server, end)
    assert ok, why
    assert server.keys == [] and server.asked == [] and kept == []
    assert "answers" not in numbers

    def no_call(*_args, **_kwargs):
        raise AssertionError("an unset answer was read")

    monkeypatch.setattr(run, "_api", no_call)
    monkeypatch.setattr(run, "_api_with_key", no_call)
    assert run.flag_answers(v3.board().end_state.flag, API, TOKEN, Kept()) is None


# ---------------------------------------------------------------------------
# PE1512-2: video 3 is judged and recorded as it was
# ---------------------------------------------------------------------------

#: main's gate 10 verdict for video 3 on the row a pre-values API answers
#: (v3.FLAG has no value_type, on_value or off_value: v0.36.0's schema), taken
#: at 0b2780a5 before this change, byte for byte.
V3_PASS = (
    True,
    {
        "flags_named": 1,
        "key": "express_checkout",
        "status": "inactive",
        "rollout_percentage": 50,
        "targeting_rules": {
            "logical_operator": "AND",
            "groups": [
                {
                    "logical_operator": "AND",
                    "conditions": [
                        {
                            "attribute": "segment",
                            "operator": "in_segment",
                            "value": "seg-beta",
                        }
                    ],
                }
            ],
        },
        "segments_named": 1,
        "audit": [
            "feature_flag_create",
            "toggle_enable",
            "feature_flag_update",
            "toggle_disable",
        ],
        "shown_rows": [
            ["admin@demo.com", "Flag turned off", "Feature flag", "Express checkout"],
            ["admin@demo.com", "Flag changed", "Feature flag", "Express checkout"],
            ["admin@demo.com", "Flag turned on", "Feature flag", "Express checkout"],
            ["admin@demo.com", "Flag created", "Feature flag", "Express checkout"],
        ],
    },
    "",
)
V3_SKIPPED_SWITCH_OFF = (
    False,
    {
        **{k: v for k, v in V3_PASS[1].items() if k not in ("audit", "shown_rows")},
        "audit": ["feature_flag_create", "toggle_enable", "feature_flag_update"],
    },
    "the flag's audit entries are ['feature_flag_create', 'toggle_enable',"
    " 'feature_flag_update'], not ['feature_flag_create', 'toggle_enable',"
    " 'feature_flag_update', 'toggle_disable']",
)
#: main's dump of video 3's end state (gate 10's ``storyboard`` number).
V3_DUMP = {
    "signed_in_as": "admin@demo.com",
    "experiments_listed": None,
    "experiment": None,
    "flag": {
        "name": "Express checkout",
        "key": "express_checkout",
        "segment": "Beta testers",
        "rollout_percentage": 50,
        "status": "inactive",
        "audit": [
            "feature_flag_create",
            "toggle_enable",
            "feature_flag_update",
            "toggle_disable",
        ],
        "shown": "audit",
    },
    "calculations": [],
    "team": None,
    "design": None,
    "rollback": None,
}


def test_video_3s_verdict_on_a_pre_values_row_is_mains():
    assert "value_type" not in v3.FLAG
    assert v3.flag_check() == V3_PASS
    assert v3.flag_check(audit=v3.entries(v3.NEWEST_FIRST[1:])) == V3_SKIPPED_SWITCH_OFF


def test_video_3s_end_state_dump_is_mains():
    assert v3.board().end_state.model_dump() == V3_DUMP
    assert list(v3.board().end_state.model_dump()["flag"]) == list(V3_DUMP["flag"])
