"""Video 12, Balance the split within groups (#1890): the storyboard, gate 10's
``balance_within`` (S3 of the V11-V13 plan, with PE 1 and PE 2), and the
claims it makes that no frame can prove.

* It is registered right after video 5 (no pin on the order moves), loads,
  narrates by the rules (G1-G3, D84), takes the default gain for now (its own
  is measured from its voice sitting, EMV11-4) and has an empty override list.
* Its words are the dashboard's: "Balance the split within groups", said as
  drawn; never "even", "evenly", the setting's field name or "every group";
  and nothing about results (the setup only: no per-group results view ships
  in the release it is recorded at).
* The key its card quotes is the key typed on camera and the end state's; the
  form's own key differs, so the beat that changes it says so (video 10's
  precedent). The attribute said is the one typed and the end state's; the
  values typed are the end state's, and the narration does not name them
  (values match exactly, and a sentence cannot say ``high_value``).
* S3: ``DesignEnd.balance_within``. Set, the API's echo must equal it exactly,
  and absent or null refuses; the Review row and the experiment page's row are
  each held to their own string (PE 1). Unset, it is not read, not compared
  and not dumped: video 9's verdict and its end state's dump are main's, byte
  for byte (PE 2, ``V9_FULL_PASS`` and ``V9_DUMP``, taken at d4106bc5).
* The product's words the mirrors use are pinned to frontend/src.

The pages of the docs each sentence rests on are checked in
test_showcase_quoted_docs.py (``CLAIMS_12``), which also runs on a pull request
that changes only the docs. The test ids are tied to frontend/src in
test_showcase_dashboard_testids.py (PE 8).
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest
import yaml
from showcase import contract
from showcase.capture import config, gates, narration, run, storyboard

from backend.tests.unit.showcase import test_showcase_design_end as v9
from backend.tests.unit.showcase import test_showcase_design_set as v9_set
from backend.tests.unit.showcase.test_showcase_design_in_depth import (
    drawn_and_said,
    screened,
)

pytestmark = [pytest.mark.unit]

SLUG = "12-balance-within"
REPO = Path(__file__).resolve().parents[4]
SHOWCASE = REPO / "tests" / "acceptance" / "showcase"
FRONTEND = REPO / "frontend" / "src"
OWNER = v9.OWNER
SEED = v9.SEED


def board() -> storyboard.Storyboard:
    return storyboard.load(SHOWCASE / config.VIDEOS[SLUG])


def raw_board() -> Dict[str, Any]:
    """Video 12's storyboard as written, to change one thing and parse again."""
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


def lines_of(made: storyboard.Storyboard) -> List[str]:
    """Every caption, sentence and card line."""
    found = []
    for scene in made.scenes:
        found += storyboard.scene_screen_text(scene)
        for beat in scene.beats:
            found += beat.say
    return found


def source(relative: str) -> str:
    return (FRONTEND / relative).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The storyboard
# ---------------------------------------------------------------------------


def test_video_12_registers_after_video_5_with_no_app_or_override():
    order = list(config.VIDEOS)
    assert config.VIDEOS[SLUG] == "storyboards/12-balance-within.yaml"
    assert order.index(SLUG) == order.index("05-plan-and-team") + 1
    # The pins on the order stand unedited: video 5 fourth, video 10 between
    # 6 and 9, and 9 then 4 last.
    assert order.index("05-plan-and-team") == 3
    assert order.index("10-serve-a-value") == order.index("06-shoplab-ab-test") + 1
    assert order[-2:] == ["09-design-in-depth", "04-streampulse-rollout"]
    made = board()
    assert made.slug == SLUG and made.title == "Balance the split within groups"
    assert made.seed == "demo" and made.apps == [] and made.traffic is None
    assert made.setup is None
    assert made.time_zone is None and made.locale is None
    from showcase.capture import overrides as override_lists

    listed = override_lists.load()
    assert listed[SLUG] == {}
    assert (
        override_lists.override_list(made, run.Ports(28400, 28401, 28402), listed) == []
    )
    # The default gain until its own is measured (EMV11-4), and the default cap.
    assert SLUG not in contract.VOICE_GAIN_DB_BY_VIDEO
    assert contract.voice_gain_db(SLUG) == contract.DEFAULT_VOICE_GAIN_DB == 4.5
    assert contract.max_video_ms(SLUG) == contract.MAX_VIDEO_MS


def test_video_12_narrates_by_the_rules_and_states_its_one_departure():
    made = board()
    assert narration.board_problems(made) == []
    assert storyboard.departure_problems(made) == []
    assert storyboard.required_departures(made) == [("design", "off-camera setup")]
    assert [s.id for s in made.scenes] == ["design", "assign"]
    first = made.scenes[0]
    assert [d.says for d in first.departures] == ["Signed in off camera"]
    assert first.beats[0].cue.startswith("Signed in off camera.")
    for scene in made.scenes:
        for beat in scene.beats:
            assert contract.caption_problems(beat.cue) == [], beat.cue
            assert narration.headline_problems(beat.cue, beat.say) == [], beat.cue
            for sentence in beat.say:
                assert narration.sentence_problems(sentence) == [], sentence
                assert contract.caption_problems(sentence) == [], sentence
    payoffs = [b.cue for s in made.scenes for b in s.beats if b.payoff]
    assert payoffs == ["Saved as a draft, balanced within tier."]


def test_video_12_is_a_draft_board_that_sets_nothing_up_and_sends_no_traffic():
    made = board()
    assert made.setup is None and made.traffic is None
    assert all(not scene.off_camera and not scene.warm_results for scene in made.scenes)
    end = made.end_state.design
    assert end.status == "draft"
    assert (end.scheduled_start, end.group, end.holdout) == (None, None, None)
    assert end.analysis.sequential is False


# ---------------------------------------------------------------------------
# The words: the dashboard's, and nothing about results
# ---------------------------------------------------------------------------

#: What video 12 never says or draws (#1890's constraints, plan W1/W3): the
#: setting's field name, "even"/"evenly", "every group" (small groups can be
#: merged in the results by group), and anything about results or a winner
#: (it shows the setup only).
NEVER = re.compile(
    r"\beven(ly)?\b|balance_within|\bevery group\b|\bresults?\b|\bwinn|\bsignifican",
    re.IGNORECASE,
)
SWITCH = "Balance the split within groups"


def unsaid(made: storyboard.Storyboard) -> List[str]:
    return [line for line in lines_of(made) if NEVER.search(line)]


def test_video_12_says_the_switch_as_drawn_and_never_a_word_it_must_not():
    made = board()
    assert unsaid(made) == []
    said = [s for _s, _b, _l, s in storyboard.narration_lines(made)]
    assert any(SWITCH in sentence for sentence in said)
    assert f"export const BALANCE_SWITCH_LABEL = '{SWITCH}';" in source(
        "components/experiments/new/balanceWithin.ts"
    )


@pytest.mark.parametrize(
    "sentence",
    [
        "Turn it on, and the split is shared out evenly.",
        "Each group gets an even split.",
        "Every group gets the same split.",
        "The results show each group.",
        "It finds the winner sooner.",
    ],
)
def test_a_line_saying_a_word_video_12_never_says_is_caught(sentence):
    """The transcript grep's plants: each sentence passes the narration's own
    rules, and is caught here."""
    made = changed(lambda d: d["scenes"][0]["beats"][3]["say"].__setitem__(1, sentence))
    assert narration.board_problems(made) == []
    assert unsaid(made) == [sentence]


def test_the_setting_s_field_name_is_caught_where_the_narration_rules_allow_it():
    """A sentence cannot say ``balance_within``: a caption is plain text, so
    the narration's own rules refuse the underscore. The card's note is not a
    caption, so the grep catches it there."""
    with pytest.raises(storyboard.StoryboardError, match="a caption is plain text"):
        changed(
            lambda d: d["scenes"][0]["beats"][3]["say"].__setitem__(
                1, "The setting is called balance_within."
            )
        )
    note = "As the guide gives it: balance_within names tier."
    made = changed(lambda d: d["scenes"][1]["card"].update(note=note))
    assert unsaid(made) == [note]


def test_no_line_of_video_12_reads_like_a_kept_value():
    lines = drawn_and_said(board())
    # 8 captions, 13 sentences, 8 typed strings and the card's 3 lines.
    assert len(lines) == 32
    assert "Pricing test" in lines and "high_value" in lines
    assert screened(lines) == []


def test_four_sentences_are_other_videos_word_for_word_and_keep_their_clips():
    """The cached clips: each sentence the same (so the same key) as an
    earlier video's; the other 9 are new clips."""
    keys = {
        text: contract.clip_key("Ruth", contract.polly_text(text))
        for _s, _b, _l, text in storyboard.narration_lines(board())
    }
    elsewhere = set()
    for slug, relative in config.VIDEOS.items():
        if int(slug[:2]) >= int(SLUG[:2]):
            continue
        other = storyboard.load(SHOWCASE / relative)
        elsewhere |= {
            contract.clip_key("Ruth", contract.polly_text(text))
            for _s, _b, _l, text in storyboard.narration_lines(other)
        }
    kept = [text for text, key in keys.items() if key in elsewhere]
    assert kept == [
        "Choose New Experiment.",
        "Name it, and the key your code uses fills itself in.",
        "You can change it: here, to the key the docs use.",
        "Make purchases the primary metric: the one you decide on.",
    ]
    assert len(keys) == 13


# ---------------------------------------------------------------------------
# The key: the card's, the one typed, and the end state's
# ---------------------------------------------------------------------------

CARD_KEY = re.compile(r'"experiment_key": "([^"]+)"')


def form_key(name: str) -> str:
    """``generateKey`` (components/experiments/new/formState.ts): the key the
    form fills in from the name."""
    key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return key[:64]


def key_problems(made: storyboard.Storyboard) -> List[str]:
    """Why the card's key, the form's and the end state's are not one key, or
    why the key's edit is not said, or nothing."""
    end = made.end_state.design
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
        (beat, step)
        for scene in made.scenes
        for beat in scene.beats
        for step in beat.steps
        if isinstance(step, storyboard.Type) and step.type.testid == "experiment-key"
    ]
    form = typed[-1][1].text if typed else form_key(end.name)
    if form != end.key:
        problems.append(
            f"the form holds the key {form!r}; the end state's is {end.key!r}"
        )
    if typed:
        beat, step = typed[-1]
        if not step.clear:
            problems.append("the key is typed after the form's own, not over it")
        if not any("change it" in text for text in (beat.cue, *beat.say)):
            problems.append("the beat that changes the key does not say so")
    return problems


def test_the_cards_key_is_the_key_typed_and_the_end_states():
    made = board()
    assert key_problems(made) == []
    end = made.end_state.design
    assert form_key(end.name) == "pricing_test" != end.key == "pricing-test"
    text = source("components/experiments/new/formState.ts")
    assert ".replace(/[^a-z0-9]+/g, '_')" in text
    assert ".replace(/^_+|_+$/g, '')" in text


def _key_step(data: Dict[str, Any]) -> Dict[str, Any]:
    return next(
        s
        for s in data["scenes"][0]["beats"][1]["steps"]
        if s.get("type", {}).get("testid") == "experiment-key"
    )


@pytest.mark.parametrize(
    "change, words",
    [
        (
            lambda d: _key_step(d).update(text="pricing-tests"),
            "the form holds the key 'pricing-tests'",
        ),
        (
            lambda d: d["scenes"][0]["beats"][1]["steps"].remove(_key_step(d)),
            "the form holds the key 'pricing_test'",
        ),
        (
            lambda d: d["scenes"][0]["beats"][1].update(
                cue="Name it, and the key fills itself in.",
                say=["Name it, and the key your code uses fills itself in."],
            ),
            "does not say so",
        ),
        (lambda d: _key_step(d).pop("clear"), "not over it"),
        (
            lambda d: d["end_state"]["design"].update(key="pricing_test"),
            "the card quotes the keys ['pricing-test']",
        ),
    ],
    ids=["typed-key", "no-edit", "edit-unsaid", "not-cleared", "end-state-key"],
)
def test_a_key_that_drifts_anywhere_is_caught(change, words):
    problems = key_problems(changed(change))
    assert any(words in p for p in problems), problems


# ---------------------------------------------------------------------------
# The attribute: said, typed and declared alike; the values: typed and declared
# ---------------------------------------------------------------------------

SAID_ATTRIBUTE = re.compile(r"\banother attribute: (\w+)\.$")


def balance_problems(made: storyboard.Storyboard) -> List[str]:
    end = made.end_state.design.balance_within
    sentences = [s for _s, _b, _l, s in storyboard.narration_lines(made)]
    attributes = [m.group(1) for s in sentences if (m := SAID_ATTRIBUTE.search(s))]
    typed: Dict[str, List[str]] = {}
    chosen = []
    switched = [
        step
        for step in every_step(made)
        if isinstance(step, storyboard.Click) and step.click.testid == "balance-switch"
    ]
    for step in every_step(made):
        if isinstance(step, storyboard.Type) and (step.type.testid or "").startswith(
            "balance-"
        ):
            typed.setdefault(step.type.testid, []).append(step.text)
        if isinstance(step, storyboard.Choose) and step.choose.testid == (
            "balance-group-by"
        ):
            chosen.append(step.option)
    problems = []
    if len(switched) != 1:
        problems.append(
            f"the switch is clicked {len(switched)} times: once turns it on, and"
            " twice leaves it off"
        )
    if chosen != ["Another attribute…"]:
        problems.append(f"the group-by chosen is {chosen}, not Another attribute…")
    if typed.get("balance-custom-key") != [end.attribute] or attributes != [
        end.attribute
    ]:
        problems.append(
            f"the attribute typed is {typed.get('balance-custom-key')} and said"
            f" {attributes}; the end state's is {end.attribute!r}"
        )
    if typed.get("balance-value-input") != end.values:
        problems.append(
            f"the values typed are {typed.get('balance-value-input')}; the end"
            f" state's are {end.values}"
        )
    return problems


def test_the_attribute_said_and_the_values_typed_are_the_ones_declared():
    made = board()
    assert balance_problems(made) == []
    assert made.end_state.design.balance_within.model_dump() == {
        "attribute": "tier",
        "values": ["high_value", "churn_risk", "new"],
    }


def _values_beat(data: Dict[str, Any]) -> Dict[str, Any]:
    return data["scenes"][0]["beats"][4]


def _typed_value(data: Dict[str, Any], index: int) -> Dict[str, Any]:
    return [
        s
        for s in _values_beat(data)["steps"]
        if s.get("type", {}).get("testid") == "balance-value-input"
    ][index]


@pytest.mark.parametrize(
    "change, words",
    [
        (lambda d: _typed_value(d, 1).update(text="churn"), "the values typed"),
        (
            lambda d: d["end_state"]["design"]["balance_within"].update(
                values=["high_value", "new", "churn_risk"]
            ),
            "the values typed",
        ),
        (
            lambda d: _values_beat(d)["steps"][1].update(text="plan"),
            "the attribute typed",
        ),
        (
            lambda d: _values_beat(d)["steps"][0].update(option="Saved segments…"),
            "the group-by chosen",
        ),
        (
            lambda d: d["scenes"][0]["beats"][3]["steps"].pop(0),
            "the switch is clicked 0 times",
        ),
        (
            lambda d: d["scenes"][0]["beats"][3]["steps"].insert(
                1, {"click": {"testid": "balance-switch"}}
            ),
            "the switch is clicked 2 times",
        ),
    ],
    ids=[
        "typed",
        "declared-order",
        "attribute",
        "segments",
        "switch-left-off",
        "switch-clicked-twice",
    ],
)
def test_an_attribute_or_value_that_drifts_anywhere_is_caught(change, words):
    problems = balance_problems(changed(change))
    assert any(words in p for p in problems), problems


def test_the_split_said_is_the_forms_and_the_end_states():
    """ "At 50/50": the form's two shares (formState.ts ``DEFAULT_VARIANTS``),
    never typed, and the end state's; G2 finds them in the share boxes."""
    made = board()
    beat = made.scenes[0].beats[3]
    assert storyboard.numbers_in(beat.say[0]) == ["50", "50"]
    end = made.end_state.design
    assert [v.allocation for v in end.variants] == [50, 50]
    assert not [
        s
        for s in every_step(made)
        if isinstance(s, storyboard.Type)
        and (s.type.testid or "").startswith("variant-allocation-")
    ]
    text = source("components/experiments/new/formState.ts")
    assert "traffic_allocation: 50, is_control: true }" in text
    assert "traffic_allocation: 50, is_control: false }" in text


def test_the_switch_is_a_labelled_checkbox_whose_outline_is_not_removed():
    """#1890: a labelled checkbox (the label wraps it and its words), with no
    class that removes the browser's focus outline. Whether the ring is drawn
    is the browser's: no frame of a pointer click shows it."""
    text = source("components/experiments/new/BalanceWithinFields.tsx")
    block = text.split('data-testid="balance-within">')[1].split("{balance.on && (")[0]
    assert block.strip().startswith("<label")
    assert 'type="checkbox"' in block and 'data-testid="balance-switch"' in block
    assert "{BALANCE_SWITCH_LABEL}" in block and "</label>" in block
    assert "outline-none" not in block


# ---------------------------------------------------------------------------
# S3: gate 10's balance_within
# ---------------------------------------------------------------------------

#: What the product draws for the real board's experiment, written out by hand
#: (not rendered by the mirrors under test): the Review rows at beat 6 and the
#: experiment page's balance value at beat 7.
DRAWN_12 = {
    "review-variants": "Control: 50% (control)\nTreatment: 50%",
    "review-metrics": "Purchase (purchase, Conversion) — primary metric",
    "review-balance": "Balance: within tier (high_value, churn_risk, new)",
    "detail-balance": "within tier (high_value, churn_risk, new)",
}
ECHO = {"attribute": "tier", "values": ["high_value", "churn_risk", "new"]}


def design() -> storyboard.DesignEnd:
    return board().end_state.design


def answered(**changes: Any) -> Dict[str, Any]:
    """The API's answer for the draft as the video leaves it: the echo is
    there, as #448's server sends it."""
    answer = dict(v9.saved(design()), balance_within=copy.deepcopy(ECHO))
    answer.update(changes)
    return answer


def judge(answer: Dict[str, Any] | None = None, readings: Dict[str, str] | None = None):
    return gates.design_end(
        design(),
        named=[answer if answer is not None else answered()],
        total=SEED + 1,
        seed_experiments=SEED,
        owner_id=OWNER,
        readings=DRAWN_12 if readings is None else readings,
    )


def test_a_planted_correct_answer_passes_with_the_echo():
    ok, numbers, why = judge()
    assert ok, why
    assert numbers["balance_within"] == ECHO
    assert numbers["shown"] == {
        "review_variants": ["Control: 50% (control)", "Treatment: 50%"],
        "review_metrics": ["Purchase (purchase, Conversion) — primary metric"],
        "review_balance": "Balance: within tier (high_value, churn_risk, new)",
        "detail_balance": "within tier (high_value, churn_risk, new)",
    }


def test_an_answer_with_no_balance_within_is_refused():
    """An older server drops the field and answers 200: absent is refused."""
    answer = answered()
    del answer["balance_within"]
    ok, _, why = judge(answer)
    assert not ok
    assert "has no 'balance_within'" in why


@pytest.mark.parametrize(
    "echo",
    [
        None,
        {"attribute": "tier", "values": ["high_value", "churn_risk"]},
        {"attribute": "tier", "values": ["high_value", "new", "churn_risk"]},
        {"attribute": "user_tier", "values": ["high_value", "churn_risk", "new"]},
        {"segments": ["8d4c6f0e-3a1b-4c2d-9e8f-7a6b5c4d3e2f"]},
        {"attribute": "tier", "values": ["high_value", "churn_risk", "new"], "x": 1},
    ],
    ids=["null", "a-value-dropped", "reordered", "another-key", "segments", "extra"],
)
def test_an_echo_other_than_the_declared_setting_is_refused(echo):
    ok, numbers, why = judge(answered(balance_within=echo))
    assert not ok
    assert why.startswith(f"balance_within is {echo!r}"), why
    assert numbers["balance_within"] == echo


def test_each_reading_is_held_to_its_own_string():
    """PE 1: the Review row starts "Balance:"; the page's value does not (its
    "Balance" is a sibling term). Either string on the other reading refuses."""
    mirrors = gates.design_mirrors(design(), answered())
    assert mirrors["review_balance"] == (
        "Balance: within tier (high_value, churn_risk, new)"
    )
    assert mirrors["detail_balance"] == "within tier (high_value, churn_risk, new)"
    review, detail = DRAWN_12["review-balance"], DRAWN_12["detail-balance"]
    ok, _, why = judge(readings=dict(DRAWN_12, **{"detail-balance": review}))
    assert not ok and why.startswith("detail_balance showed"), why
    ok, _, why = judge(readings=dict(DRAWN_12, **{"review-balance": detail}))
    assert not ok and why.startswith("review_balance showed"), why


@pytest.mark.parametrize(
    "record, drawn, kind",
    [
        ("review-balance", "Balance: off", "review_balance"),
        ("review-balance", "Balance: within tier", "review_balance"),
        (
            "review-balance",
            "Balance: within tier (high_value, new, churn_risk)",
            "review_balance",
        ),
        ("detail-balance", "off", "detail_balance"),
        ("detail-balance", "within tier (high_value, churn_risk)", "detail_balance"),
    ],
)
def test_a_balance_row_that_does_not_say_what_was_saved_is_refused(record, drawn, kind):
    ok, _, why = judge(readings=dict(DRAWN_12, **{record: drawn}))
    assert not ok and why.startswith(f"{kind} showed"), why


def test_the_echo_is_compared_before_the_rows():
    """With no echo the rows have nothing to say, and the refusal names the echo."""
    ok, _, why = judge(answered(balance_within=None))
    assert not ok and why.startswith("balance_within is None"), why


@pytest.mark.parametrize(
    "setting, words",
    [
        (None, "off"),
        ({}, "off"),
        ({"attribute": "tier"}, "within tier"),
        ({"attribute": "tier", "values": []}, "within tier"),
        (
            {"attribute": "platform", "values": ["ios", "web"]},
            "within Platform (ios, web)",
        ),
        ({"attribute": "user_tier", "values": ["gold"]}, "within User Tier (gold)"),
        ({"attribute": "country"}, "within Country"),
        ({"attribute": ""}, "within an attribute not named yet"),
    ],
)
def test_balance_words_say_the_setting_as_the_product_does(setting, words):
    assert gates.balance_words(setting) == words


BALANCE_WORDS = "components/experiments/new/balanceWithin.ts"
WIZARD = "components/experiments/new/Wizard.tsx"
DETAIL = "pages/experiments/[id].tsx"


def test_the_balance_key_labels_are_the_products():
    text = source(BALANCE_WORDS)
    block = text.split("export const BALANCE_KEY_OPTIONS = [")[1].split("] as const;")[
        0
    ]
    found = dict(re.findall(r"\{ value: '([a-z_]+)', label: '([^']+)' \}", block))
    assert found == gates.BALANCE_KEY_LABELS
    assert len(found) == 3


def test_balance_summary_is_drawn_as_the_mirror_says():
    text = source(BALANCE_WORDS)
    for piece in (
        "if (!setting) return 'off';",
        "const attribute = setting.attribute ?? '';",
        "if (attribute === '') return 'within an attribute not named yet';",
        "const option = BALANCE_KEY_OPTIONS.find((o) => o.value === attribute);",
        "const label = option ? option.label : attribute;",
        "const values = setting.values ?? [];",
        "return values.length > 0 ? `within ${label} (${values.join(', ')})`"
        " : `within ${label}`;",
    ):
        assert piece in text, piece


def test_the_two_balance_rows_are_drawn_as_the_mirrors_say():
    """The Review row is one element holding "Balance:" and the summary; the
    page's test id is on the value only, its term a sibling ``dt`` (PE 1)."""
    wizard = source(WIZARD)
    assert (
        '<p className="mt-1" data-testid="review-balance">\n'
        "                  Balance: {balanceSummary(payload.balance_within ?? null,"
        " segmentNames)}\n"
        "                </p>"
    ) in wizard
    assert gates.REVIEW_BALANCE_TERM == "Balance:"
    page = source(DETAIL)
    assert (
        '<dt className="text-slate-500">Balance</dt>\n'
        '      <dd data-testid="experiment-balance">{balanceSummary(setting ?? null,'
        " names)}</dd>"
    ) in page
    assert (
        "{experiment.balance_within !== undefined && <BalanceRow"
        " setting={experiment.balance_within} />}"
    ) in page


def test_design_state_reads_the_echo_from_the_experiment_in_full(monkeypatch):
    """Gate 10 reads the named experiment by id (the list is not trusted for
    the echo); an older server's answer without the field refuses."""
    end = design()
    full = answered()
    listed = {"items": [{"id": full["id"], "name": end.name}], "total": SEED + 1}
    asked: List[str] = []

    def answer(url, token=None, body=None):
        asked.append(url)
        if url.endswith("/api/v1/experiments/?limit=100"):
            return listed
        if url.endswith(f"/api/v1/experiments/{full['id']}"):
            return full
        if url.endswith("/api/v1/auth/me"):
            return {"id": OWNER, "email": "admin@demo.com"}
        raise AssertionError(url)

    monkeypatch.setattr(run, "_api", answer)
    ok, numbers, why = run.design_state(end, "http://api", "token", DRAWN_12, SEED)
    assert ok, why
    assert numbers["balance_within"] == ECHO
    assert asked == [
        "http://api/api/v1/experiments/?limit=100",
        f"http://api/api/v1/experiments/{full['id']}",
        "http://api/api/v1/auth/me",
    ]
    del full["balance_within"]
    ok, _, why = run.design_state(end, "http://api", "token", DRAWN_12, SEED)
    assert not ok and "has no 'balance_within'" in why


# ---------------------------------------------------------------------------
# S3 at load
# ---------------------------------------------------------------------------


def refused_at_load(change: Callable[[Dict[str, Any]], None], words: str) -> None:
    data = raw_board()
    change(data)
    with pytest.raises(storyboard.StoryboardError) as raised:
        storyboard.parse(copy.deepcopy(data), source="12-balance-within.yaml")
    assert words in str(raised.value), str(raised.value)


def _design(data: Dict[str, Any]) -> Dict[str, Any]:
    return data["end_state"]["design"]


@pytest.mark.parametrize("kind", ["review_balance", "detail_balance"])
def test_a_balance_reading_without_a_declared_setting_is_refused(kind):
    def without(data):
        _design(data).pop("balance_within")
        shown = _design(data)["shown"]
        for other in ("review_balance", "detail_balance"):
            if other != kind:
                shown.pop(other)

    refused_at_load(without, f"compares {kind}, and declares no balance_within")


def test_a_balanced_split_whose_shares_are_not_multiples_of_5_is_refused():
    def shares(data):
        _design(data)["variants"][0]["allocation"] = 34
        _design(data)["variants"][1]["allocation"] = 66

    refused_at_load(shares, "a balanced split's shares are multiples of 5")
    # The same split, not balanced, is guided setup's to save.
    data = raw_board()
    shares(data)
    design_data = _design(data)
    design_data.pop("balance_within")
    for kind in ("review_balance", "detail_balance"):
        design_data["shown"].pop(kind)
    storyboard.DesignEnd.model_validate(design_data)


def test_the_load_checks_are_the_apis_own():
    """The attribute's pattern and the share step are the API's (#448): the
    server's own refusal of a balanced 34/66 split, and of a dotted key."""
    from backend.app.core.balanced_blocks import balance_problem
    from backend.app.schemas.balance_within import ATTRIBUTE_PATTERN

    assert storyboard.BALANCE_ATTRIBUTE == f"^{ATTRIBUTE_PATTERN.pattern}$"
    assert not ATTRIBUTE_PATTERN.fullmatch("user.tier")
    assert storyboard.BALANCE_SHARE_STEP == 5

    def refused(allocations: List[int]) -> Any:
        return balance_problem(
            ECHO,
            optimization_type="fixed",
            experiment_type="a_b",
            allocations=allocations,
        )

    assert refused([50, 50]) is None
    assert "every share must be a multiple of 5" in refused([34, 66])


@pytest.mark.parametrize(
    "balance, words",
    [
        (
            {"attribute": "tier", "values": ["new", "new"]},
            "a value to keep apart repeats",
        ),
        ({"attribute": "user.tier", "values": ["new"]}, "String should match pattern"),
        ({"attribute": "tier", "values": []}, "at least 1 item"),
        ({"attribute": "tier"}, "values"),
        ({"segments": ["8d4c6f0e"]}, "segments"),
    ],
    ids=["repeat", "dotted", "no-values", "values-missing", "segments"],
)
def test_a_setting_the_api_would_refuse_is_refused_at_load(balance, words):
    refused_at_load(lambda d: _design(d).update(balance_within=balance), words)


# ---------------------------------------------------------------------------
# PE 2: video 9, which declares no balance, is judged and dumped as on main
# ---------------------------------------------------------------------------

#: main's dump of video 9's end state (gate 10's ``storyboard`` number), taken
#: at d4106bc5 before ``balance_within`` existed.
V9_DUMP = {
    "signed_in_as": "admin@demo.com",
    "experiments_listed": None,
    "experiment": None,
    "flag": None,
    "calculations": [],
    "team": None,
    "design": {
        "name": "Delivery promise",
        "key": "delivery_promise",
        "status": "draft",
        "type": "a_b",
        "variants": [
            {
                "name": "Control",
                "allocation": 34,
                "control": True,
                "configuration": None,
            },
            {
                "name": "Treatment",
                "allocation": 33,
                "control": False,
                "configuration": None,
            },
            {
                "name": "Variant 2",
                "allocation": 33,
                "control": False,
                "configuration": None,
            },
        ],
        "metrics": [
            {
                "name": "Purchase",
                "event": "purchase",
                "type": "conversion",
                "primary": True,
                "mde": None,
                "lower_is_better": False,
            },
            {
                "name": "Add to cart",
                "event": "add_to_cart",
                "type": "conversion",
                "primary": False,
                "mde": None,
                "lower_is_better": False,
            },
        ],
        "targeting": {
            "logical_operator": "AND",
            "groups": [
                {
                    "logical_operator": "AND",
                    "conditions": [
                        {
                            "attribute": "user.country",
                            "operator": "in",
                            "value": "US, CA",
                        }
                    ],
                }
            ],
        },
        "analysis": {
            "confidence": 0.95,
            "correction": "benjamini_hochberg",
            "bayesian": True,
            "sequential": True,
        },
        "shown": {
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
        },
        "scheduled_start": "scheduled-start",
        "group": "Checkout tests",
        "holdout": {"name": "Q4 holdout", "percentage": 5},
    },
    "rollback": None,
}
#: main's verdict on video 9's full board for its planted correct answer
#: (test_showcase_design_in_depth.py's), taken at d4106bc5.
V9_FULL_PASS = (
    True,
    {
        "experiments_named": 1,
        "key": "delivery_promise",
        "status": "draft",
        "experiment_type": "a_b",
        "owner_matches": True,
        "experiments_in_api": 4,
        "seed_experiments": 3,
        "variants": ["Control", "Treatment", "Variant 2"],
        "metrics": ["Add to cart", "Purchase"],
        "targeting_rules": {
            "logical_operator": "AND",
            "groups": [
                {
                    "logical_operator": "AND",
                    "conditions": [
                        {
                            "attribute": "user.country",
                            "operator": "in",
                            "value": "US, CA",
                            "id": "condition-0-0",
                        }
                    ],
                    "id": "group-0",
                }
            ],
        },
        "analysis": [0.95, "benjamini_hochberg", True],
        "shown": {
            "review_variants": [
                "Control: 34% (control)",
                "Treatment: 33%",
                "Variant 2: 33%",
            ],
            "review_metrics": [
                "Purchase (purchase, Conversion) — primary metric",
                "Add to cart (add_to_cart, Conversion)",
            ],
            "review_targeting": (
                "Targeting: People who match all of these conditions: user.country is"
                " one of US, CA"
            ),
            "review_analysis": (
                "95% confidence · Benjamini-Hochberg correction · Bayesian analysis on"
                " · Sequential testing (beta) on"
            ),
            "detail_analysis": "95% confidence · Benjamini-Hochberg",
            "detail_bayesian": "On",
            "detail_sequential": "On",
            "detail_start": "Starts 11/2/2026, 9:00:00 AM",
            "detail_group": "Checkout tests",
            "detail_holdout": "Q4 holdout: keeps 5% of new users out of every experiment",
            "detail_note": (
                "Set through the API, not on this page: a paused experiment's resume,"
                " turning sequential testing on or off after creation."
            ),
        },
    },
    "",
)


def v9_board() -> storyboard.Storyboard:
    return storyboard.load(SHOWCASE / config.VIDEOS["09-design-in-depth"])


def v9_full(answer: Dict[str, Any]):
    """Video 9's full board judged as test_showcase_design_in_depth.py judges it."""
    made = v9_board()
    return gates.design_end(
        made.end_state.design,
        named=[answer],
        total=made.seed_counts.experiments + 1,
        seed_experiments=made.seed_counts.experiments,
        owner_id=OWNER,
        readings={**v9.DRAWN, **v9_set.PAYOFF},
        moved={
            "sequential_testing_enabled": True,
            "start_date": v9_set.DATED["scheduled-start"]["instant"],
            "mutual_exclusion_group_id": v9_set.GROUP,
        },
        extra={
            "start_shown": v9_set.DATED["scheduled-start"]["shown"],
            "group_name": "Checkout tests",
            "holdout": v9_set.HOLDOUT,
        },
    )


def test_video_9s_end_state_dump_is_mains():
    dump = v9_board().end_state.model_dump()
    assert dump == V9_DUMP
    assert list(dump["design"]) == list(V9_DUMP["design"])
    assert "balance_within" in storyboard.DesignEnd.model_fields
    assert "balance_within" not in dump["design"]


def test_video_12s_end_state_dump_keeps_its_setting():
    dump = board().end_state.model_dump()["design"]
    assert dump["balance_within"] == ECHO
    assert list(dump)[-1] == "balance_within"


@pytest.mark.parametrize(
    "echo",
    ["absent", None, ECHO],
    ids=["absent", "null", "a-setting"],
)
def test_video_9s_verdict_is_mains_whatever_the_api_echoes(echo):
    """Video 9 declares no balance, so its echo is not read: absent, null or
    a setting, its verdict is main's, numbers and all."""
    answer = v9_set.answered()
    if echo != "absent":
        answer["balance_within"] = copy.deepcopy(echo)
    assert v9_full(answer) == V9_FULL_PASS
