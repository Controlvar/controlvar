"""Video 3, "Roll out a feature flag" (#1066): its storyboard, its steps and its gates.

What the video shows is pinned against the code that draws it: the key the
form makes from the name, the texts the rule builder's selects show, and the
labels the audit log gives the flag's changes. Every refusal is shown firing
on one planted defect. No browser, Docker, network or database: the director
is driven with stand-in pages on a stand-in clock.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from showcase import contract
from showcase.capture import config, director, gates, guard, run, storyboard
from showcase.capture.stack import StackError

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
FRONTEND = REPO / "frontend" / "src"
V3 = REPO / "tests" / "acceptance" / "showcase" / "storyboards" / "03-flag-rollout.yaml"

#: What a caption of this video may not claim (the UX draft's WORDS 3 and 4):
#: that a change reaches users at once (the SDKs keep an answer for up to five
#: minutes, and the page shows no request), or that schedules have a builder
#: (they are made through the API).
CLAIMS = re.compile(
    r"\b(live|instant\w*|immediate\w*|real[- ]time|right away|next request"
    r"|builder|the api answers)\b",
    re.IGNORECASE,
)


def board() -> storyboard.Storyboard:
    return storyboard.load(V3)


def v3_data() -> dict:
    return yaml.safe_load(V3.read_text(encoding="utf-8"))


def steps_of(made: storyboard.Storyboard, kind: type) -> list:
    return [
        step
        for scene in made.scenes
        for item in scene.beats
        for step in [*item.before, *item.steps, *item.exit]
        if isinstance(step, kind)
    ]


def source(relative: str) -> str:
    return (FRONTEND / relative).read_text(encoding="utf-8")


# --- The storyboard -----------------------------------------------------------------


def test_the_storyboard_loads_its_captions_are_fixed_and_it_fits_the_video():
    made = board()
    for cue in storyboard.compile_cues(made):
        assert contract.caption_problems(cue["text"]) == []
    assert storyboard.cue_list_bytes(made) == storyboard.cue_list_bytes(
        storyboard.parse(v3_data())
    )
    length = (
        storyboard.estimate_ms(made) + contract.TITLE_CARD_MS + contract.END_CARD_MS
    )
    # Scrolling is not in the estimate: leave room for it under 75 s.
    assert contract.MIN_VIDEO_MS + 5_000 <= length <= contract.MAX_VIDEO_MS - 5_000


def test_video_3_registers_in_the_tool():
    assert config.VIDEOS["03-flag-rollout"] == "storyboards/03-flag-rollout.yaml"
    assert board().slug == "03-flag-rollout"


def test_no_caption_claims_a_change_reaches_users_at_once_or_a_schedule_builder():
    made = board()
    said = [cue["text"] for cue in storyboard.compile_cues(made)]
    said += [text for *_where, text in storyboard.narration_lines(made)]
    assert [c for c in said if CLAIMS.search(c)] == []
    # The page's own words may say what the captions do not: the schedule card.
    assert "/api/v1/rollout-schedules" in source("pages/feature-flags/[id].tsx")


@pytest.mark.parametrize(
    "caption",
    [
        "The API answers off from the very next request.",
        "Changes go live for everyone.",
        "Switch it off instantly.",
        "Build the rollout stages in the schedule builder.",
        "It applies immediately.",
    ],
)
def test_the_claims_check_catches_the_captions_the_ux_review_dropped(caption):
    assert CLAIMS.search(caption)


def form_key(name: str) -> str:
    """The new-flag form's ``generateKey`` (frontend/src/pages/feature-flags/new.tsx)."""
    return re.sub(r"^_+|_+$", "", re.sub(r"[^a-z0-9]+", "_", name.lower()))[:64]


def test_the_flag_is_made_with_the_key_the_form_gives_its_name():
    made = board()
    end = made.end_state.flag
    page = source("pages/feature-flags/new.tsx")
    for line in (
        ".toLowerCase()",
        ".replace(/[^a-z0-9]+/g, '_')",
        ".replace(/^_+|_+$/g, '')",
        ".substring(0, 64);",
    ):
        assert line in page, f"generateKey no longer has {line}"
    assert form_key(end.name) == end.key == "express_checkout"
    typed = [step.text for step in steps_of(made, storyboard.Type)]
    assert end.name in typed
    assert all(len(text) <= 45 for text in typed)


def test_the_rule_is_chosen_by_the_texts_the_rule_builder_draws():
    """The operator's label and the segment option's text, as the selects show them."""
    made = board()
    chosen = [step.option for step in steps_of(made, storyboard.Choose)]
    assert "  in_segment: 'is in segment'," in source("types/targeting.ts")
    picker = source("components/targeting/SegmentPicker.tsx")
    assert "{`${segment.name} · ${kindLabel(segment)}${reason}`}" in picker
    assert "segment.kind === 'id_list' ? 'ID list' : 'Rules'" in picker
    segment = made.setup.segments[0]
    assert chosen == ["is in segment", f"{segment.name} · Rules"]
    assert made.end_state.flag.segment == segment.name


def test_the_numbers_the_captions_state_are_the_slider_values_the_video_sets():
    made = board()
    slid = [step.value for step in steps_of(made, storyboard.Slide)]
    assert slid == [10, 50]
    numbered = [
        storyboard.numbers_in(cue["text"])
        for cue in storyboard.compile_cues(made)
        if storyboard.numbers_in(cue["text"])
    ]
    assert numbered == [["10%"]]
    assert made.end_state.flag.rollout_percentage == slid[-1]


def test_the_audit_labels_are_the_audit_log_pages():
    labels = source("components/admin/audit/actionLabels.ts")
    for action, label in gates.AUDIT_LABELS.items():
        assert f"  {action}: {{ label: '{label}'," in labels, action
    assert f"  feature_flag: '{gates.FLAG_ENTITY_LABEL}'," in labels
    assert set(board().end_state.flag.audit) <= set(gates.AUDIT_LABELS)


@pytest.mark.parametrize(
    "change, match",
    [
        (lambda d: d["end_state"]["flag"].update(segment="Alpha"), "does not make"),
        (lambda d: d["end_state"]["flag"].update(shown="rows"), "no beat records"),
        (lambda d: d.pop("setup"), "does not make"),
        (
            lambda d: d["scenes"][0]["beats"][2].update(
                before=[{"choose": {"testid": "x"}, "option": "y"}]
            ),
            "cut from the video",
        ),
        (
            lambda d: d["scenes"][0]["beats"][3]["exit"][0].update(value=-1),
            "greater than or equal to 0",
        ),
        (
            lambda d: d["end_state"]["flag"].update(audit=["feature_flag_delete"]),
            "audit",
        ),
        (lambda d: d["end_state"].pop("flag"), "experiments_listed, an experiment"),
    ],
)
def test_a_storyboard_out_of_shape_is_refused(change, match):
    data = v3_data()
    change(data)
    with pytest.raises(storyboard.StoryboardError, match=match):
        storyboard.parse(data)


# --- The setup, off camera ----------------------------------------------------------


def test_the_setup_makes_the_segment_through_the_api():
    sent = []

    def call(url, token, body=None):
        sent.append((url, token, body))
        return {"id": "seg-1", "name": body["name"], "status": "active"}

    made = run.make_setup(board(), "http://127.0.0.1:28400", "t", call=call)
    assert made == {"segments": [{"segment": "Beta testers", "id": "seg-1"}]}
    ((url, token, body),) = sent
    assert url == "http://127.0.0.1:28400/api/v1/segments" and token == "t"
    assert body["kind"] == "rules" and body["name"] == "Beta testers"
    assert body["rules"] == board().setup.segments[0].rules


@pytest.mark.parametrize(
    "answer",
    [
        {"id": "seg-1", "name": "Beta tester", "status": "active"},
        {"id": "seg-1", "name": "Beta testers", "status": "inactive"},
        {"name": "Beta testers", "status": "active"},
        [],
    ],
)
def test_a_setup_the_api_answers_otherwise_is_refused(answer):
    with pytest.raises(StackError, match="setup: segment 'Beta testers'"):
        run.make_setup(board(), "http://api", "t", call=lambda *a, **k: answer)


def test_a_storyboard_without_a_setup_calls_nothing():
    made = storyboard.load(V3.parent / "02-first-experiment.yaml")

    def call(*args, **kwargs):
        raise AssertionError("no call")

    assert run.make_setup(made, "http://api", "t", call=call) == {}


# --- U2: the caption's number is on the page when the caption is -------------------


@pytest.mark.parametrize(
    "shown, ok",
    [
        ("Rollout\nRollout percentage\n10%\n0%\n100%", True),
        # The plant: the slider left at 0 when the caption goes up.
        ("Rollout\nRollout percentage\n0%\n0%\n100%", False),
        # 10% is not found inside 100%.
        ("Rollout\n100%", False),
    ],
)
def test_the_10_percent_caption_needs_the_slider_at_10(shown, ok):
    verdict, _numbers, why = gates.caption_numbers(
        "Everyone else starts at 10%.", shown
    )
    assert verdict is ok, why


# --- Gate 10: the flag the video made -------------------------------------------------

ADMIN = {"email": "admin@demo.com", "id": "u-admin"}
SEGMENTS = [
    {"id": "seg-beta", "name": "Beta testers", "status": "active"},
    {"id": "seg-old", "name": "Beta testers", "status": "archived"},
]
FLAG = {
    "id": "f-1",
    "name": "Express checkout",
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
}
NEWEST_FIRST = [
    "toggle_disable",
    "feature_flag_update",
    "toggle_enable",
    "feature_flag_create",
]


def entries(actions=NEWEST_FIRST, who=ADMIN):
    return [
        {"action_type": a, "user_email": who["email"], "user_id": who["id"]}
        for a in actions
    ]


def table(actions=NEWEST_FIRST, name="Express checkout"):
    """The audit table's body as the page draws it: tab-separated cells."""
    rows = [
        f"10/11/2026, 3:0{i}:00 AM\tadmin@demo.com\t{gates.AUDIT_LABELS[a]}\tFeature flag\t{name}"
        for i, a in enumerate(actions)
    ]
    rows.append(
        "10/11/2026, 2:59:00 AM\tadmin@demo.com\tSegment created\tSegment\tBeta testers"
    )
    return "\n".join(rows) + "\n"


def flag_check(**changes):
    kwargs = {
        "named": [FLAG],
        "segments": SEGMENTS,
        "audit": entries(),
        "actor": ADMIN,
        "shown": table(),
    }
    kwargs.update(changes)
    return gates.flag_end(board().end_state.flag, **kwargs)


def test_the_flag_end_state_passes_when_the_api_and_the_page_agree():
    ok, numbers, why = flag_check()
    assert ok, why
    assert numbers["audit"] == list(reversed(NEWEST_FIRST))


@pytest.mark.regression
def test_the_switch_off_click_skipped_is_refused():
    """QA's plant for gate 10/3: the video never switches the flag off."""
    ok, _numbers, why = flag_check(
        named=[{**FLAG, "status": "active"}], audit=entries(NEWEST_FIRST[1:])
    )
    assert not ok and "not 'inactive'" in why
    ok, _numbers, why = flag_check(audit=entries(NEWEST_FIRST[1:]))
    assert not ok and "audit entries are" in why


def rule(**condition):
    base = {"attribute": "segment", "operator": "in_segment", "value": "seg-beta"}
    return {"groups": [{"conditions": [{**base, **condition}]}]}


@pytest.mark.parametrize(
    "changes, words",
    [
        ({"named": []}, "0 flags are named"),
        ({"named": [FLAG, FLAG]}, "2 flags are named"),
        ({"named": [{**FLAG, "key": "express_checkout_2"}]}, "has key"),
        ({"segments": SEGMENTS[1:]}, "0 active segments"),
        (
            {"named": [{**FLAG, "targeting_rules": rule(value="seg-old")}]},
            "the flag's rule is",
        ),
        (
            {"named": [{**FLAG, "targeting_rules": rule(operator="not_in_segment")}]},
            "the flag's rule is",
        ),
        (
            {
                "named": [
                    {
                        **FLAG,
                        "targeting_rules": {
                            "groups": [
                                {
                                    "conditions": [rule()["groups"][0]["conditions"][0]]
                                    * 2
                                }
                            ]
                        },
                    }
                ]
            },
            "one group of one condition",
        ),
        ({"named": [{**FLAG, "targeting_rules": None}]}, "one group of one condition"),
        ({"named": [{**FLAG, "rollout_percentage": 10}]}, "at 10%, not 50%"),
        (
            {"audit": entries(NEWEST_FIRST + ["feature_flag_update"])},
            "audit entries are",
        ),
        (
            {"audit": entries(who={"email": "system:safety-monitor", "id": None})},
            "not every entry is by the signed-in",
        ),
        ({"actor": {"email": "admin@demo.com", "id": "u-other"}}, "not every entry"),
        ({"shown": table(NEWEST_FIRST[1:])}, "the audit log's top rows showed"),
        ({"shown": table(name="Express")}, "the audit log's top rows showed"),
        ({"shown": None}, "the audit log's top rows showed"),
    ],
)
def test_a_flag_end_state_that_disagrees_anywhere_is_refused(changes, words):
    ok, _numbers, why = flag_check(**changes)
    assert not ok and words in why, why


# --- C9: a native select is chosen, never clicked open, and held ----------------------


class Clock:
    def __init__(self):
        self.ms = 1_000_000.0

    def now(self):
        return self.ms


class Overlay:
    def __exit__(self, *args):
        pass


class Screencast:
    def __init__(self):
        self.overlays = []

    def show_overlay(self, html):
        self.overlays.append(html)
        return Overlay()


class Mouse:
    def __init__(self):
        self.clicks = []

    def move(self, x, y):
        pass

    def click(self, x, y):
        self.clicks.append((x, y))


class Element:
    tag = "DIV"
    box = {"x": 400.0, "y": 300.0, "width": 960.0, "height": 24.0}

    def wait_for(self, state, timeout):
        pass

    def bounding_box(self):
        return dict(self.box)

    def nth(self, index):
        return self

    def evaluate(self, script, *args):
        if script == "el => el.tagName":
            return self.tag
        if script == director.ON_TOP_JS:
            return True
        return self.answer(script)


class Select(Element):
    tag = "SELECT"

    def __init__(self, options, *, revert=False):
        self.options = options
        self.selected = 0
        self.revert = revert
        self.reads = 0

    def select_option(self, label, timeout):
        self.selected = self.options.index(label)

    def answer(self, script):
        assert script == director.SELECTED_TEXT_JS
        self.reads += 1
        if self.revert and self.reads > 1:
            return self.options[0]
        return self.options[self.selected]


class Range(Element):
    def __init__(self, value=0, *, high=100, kind="range", sticks=False):
        self.value = value
        self.high = high
        self.kind = kind
        self.sticks = sticks
        self.filled = []

    def fill(self, text):
        self.filled.append(int(text))
        if not self.sticks:
            self.value = int(text)

    def answer(self, script):
        if script == director.RANGE_JS:
            return ["INPUT", self.kind, 0, self.high, self.value]
        assert script == "el => Number(el.value)"
        return self.value


class StandIn:
    """Enough of a page for a step: elements by test id, a clock that moves on waits."""

    def __init__(self, clock, elements):
        self.clock = clock
        self.elements = elements
        self.mouse = Mouse()
        self.screencast = Screencast()

    def wait_for_timeout(self, ms):
        self.clock.ms += ms

    def get_by_test_id(self, testid):
        return self.elements[testid]


@pytest.fixture
def stage(tmp_path, monkeypatch):
    """A director recording on a stand-in clock, and a function to give it a page."""
    clock = Clock()
    monkeypatch.setattr(director, "_now_ms", clock.now)

    def make(elements):
        page = StandIn(clock, elements)
        made = director.Director(
            context=object(),
            page=page,
            frames=tmp_path / "work" / "video" / "capture" / "frames",
            dashboard="http://127.0.0.1:28401",
            api_port=28400,
            dashboard_port=28401,
            redactor=director.redaction.Redactor(),
            forbidden=(),
            work=guard.WorkRoot(tmp_path / "work"),
        )
        made.recorder.t0 = clock.ms  # recording
        made.where = "scene flag beat 3"
        return made, page

    return make


OPERATORS = ["equals", "not equals", "is in segment"]


def test_a_choice_is_set_without_a_click_and_held_on_its_text(stage):
    select = Select(OPERATORS)
    made, page = stage({"condition-operator": select})
    made.choose(
        storyboard.Choose(
            choose=storyboard.Locator(testid="condition-operator"),
            option="is in segment",
        )
    )
    assert select.selected == 2 and page.mouse.clicks == []
    (held,) = made.tally.choices
    assert held["ok"] and held["shown"] == held["held"] == "is in segment"
    assert held["held_ms"] >= config.SETTLE_MS


@pytest.mark.regression
def test_a_click_on_a_select_is_refused(stage):
    """A click opens a native list, which no screencast frame ever shows."""
    made, page = stage({"condition-operator": Select(OPERATORS)})
    with pytest.raises(
        director.CaptureFailed,
        match="C9: scene flag beat 3: testid 'condition-operator' is a native select",
    ):
        made.step(
            storyboard.Click(click=storyboard.Locator(testid="condition-operator"))
        )
    assert page.mouse.clicks == []


def test_a_select_that_goes_back_during_its_hold_is_refused(stage):
    made, _page = stage({"condition-operator": Select(OPERATORS, revert=True)})
    with pytest.raises(director.CaptureFailed, match="went back to 'equals'"):
        made.choose(
            storyboard.Choose(
                choose=storyboard.Locator(testid="condition-operator"),
                option="is in segment",
            )
        )


@pytest.mark.parametrize(
    "shown, held, ms, words",
    [
        ("equals", "is in segment", 1000, "shows 'equals'"),
        ("is in segment", "equals", 1000, "went back"),
        ("is in segment", "is in segment", config.SETTLE_MS - 1, "held 999 ms"),
    ],
)
def test_the_hold_verdict(shown, held, ms, words):
    ok, _numbers, why = gates.select_hold("is in segment", shown, held, ms)
    assert not ok and words in why
    assert gates.select_hold("is in segment", "is in segment", "is in segment", 1000)[0]


def test_choose_is_not_a_click_on_anything_but_a_select(stage):
    made, _page = stage({"save-flag": Element()})
    with pytest.raises(director.CaptureFailed, match="is not a select"):
        made.choose(
            storyboard.Choose(choose=storyboard.Locator(testid="save-flag"), option="x")
        )


# --- The slider ----------------------------------------------------------------------------


def slide(made, element, value):
    _found, box = made.box(storyboard.Locator(testid="rollout-percentage"))
    made.slide(element, box, value, "testid 'rollout-percentage'")


def test_the_slider_climbs_to_its_value_with_the_cursor_on_its_thumb(stage):
    rng = Range(10)
    made, page = stage({"rollout-percentage": rng})
    slide(made, rng, 50)
    assert rng.value == 50 and rng.filled[-1] == 50
    assert rng.filled == sorted(rng.filled) and len(rng.filled) > 5
    # The cursor ends on the thumb at 50: half the track, less half a thumb each side.
    thumb = config.RANGE_THUMB_CSS_PX * config.ZOOM
    assert made.cursor[0] == pytest.approx(400 + thumb / 2 + (960 - thumb) / 2)


@pytest.mark.parametrize(
    "element, value, words",
    [
        (Range(0, kind="text"), 10, "is not a slider"),
        (Range(0, high=40), 50, "runs from 0 to 40, not to 50"),
        (Range(0, sticks=True), 10, "stopped at 0, not 10"),
    ],
)
def test_a_slide_that_cannot_land_is_refused(stage, element, value, words):
    made, _page = stage({"rollout-percentage": element})
    with pytest.raises(director.CaptureFailed, match=words):
        slide(made, element, value)
