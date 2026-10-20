"""Video 4, "Roll out to devices, roll back by itself" (plan v2.2, T178): its
storyboard and the tool changes it brings.

The video's numbers are pinned against the code that makes them: the devices'
buckets for Player v2 by the server's own rollout function, the crash's counts
and b, and the panel's and the audit log's words. Each contract it adds is
shown refusing its planted defect: the incident's three off-camera steps (the
rollout script's exact argv, the crash sender and its anchor, the wait), B9's
gate, rate and fit, S5's measured card, C6's read-back, gate 10/4, the departures,
A5 and A6, and the apps' dependencies for StreamPulse. No browser, Docker,
network or database: stand-in pages, APIs and sessions.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from urllib.parse import parse_qs

import pytest
import yaml
from showcase import contract
from showcase.capture import (
    build,
    config,
    director,
    gates,
    guard,
    incident,
    narration,
    run,
    storyboard,
)
from showcase.capture import overrides as override_lists
from showcase.capture.procs import Children
from showcase.capture.stack import Ports, StackError

from backend.app.services.feature_flag_service import FeatureFlagService

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
ACCEPTANCE = REPO / "tests" / "acceptance"
CAPTURE = ACCEPTANCE / "showcase" / "capture"
V4 = ACCEPTANCE / "showcase" / "storyboards" / "04-streampulse-rollout.yaml"
STREAMPULSE = REPO / "demo" / "streampulse"
PORTS = Ports(28400, 28401, 28402, 28403, 28404, 28405)
API = "http://127.0.0.1:28400"
DASH = "http://127.0.0.1:28401"
PULSE = "http://127.0.0.1:28404"
FLAG = "streampulse_player_v2"
APP_KEY = config.APP_STATIC_KEY["streampulse"]
#: A stand-in for the StreamPulse server key the seed writes: never a real one.
SERVER_KEY = "eptk_" + "q" * 40
S10 = "sp-galaxy-s10-us-premium"


def board() -> storyboard.Storyboard:
    return storyboard.load(V4)


def v4_data() -> dict:
    return yaml.safe_load(V4.read_text(encoding="utf-8"))


def scene(name: str) -> storyboard.Scene:
    return next(s for s in board().scenes if s.id == name)


def raw_scene(raw: dict, name: str) -> dict:
    return next(s for s in raw["scenes"] if s["id"] == name)


def load_refusal(raw: dict) -> str:
    with pytest.raises(storyboard.StoryboardError) as caught:
        storyboard.parse(raw)
    return str(caught.value)


def crash_plan() -> storyboard.CrashPlan:
    return next(
        item.crashes
        for s in board().scenes
        for item in s.off_camera
        if isinstance(item, storyboard.Crashes)
    )


def in_rollout(device: str, percentage: int) -> bool:
    """The server's own answer: ``FeatureFlagService._evaluate_percentage_rollout``."""
    flag = SimpleNamespace(key=FLAG, rollout_percentage=percentage)
    return FeatureFlagService(None)._evaluate_percentage_rollout(flag, device)


# --- The storyboard ------------------------------------------------------------------


def test_the_storyboard_loads_and_its_captions_are_fixed_and_fit_the_video():
    made = board()
    for cue in storyboard.compile_cues(made):
        assert contract.caption_problems(cue["text"]) == []
    assert storyboard.cue_list_bytes(made) == storyboard.cue_list_bytes(
        storyboard.parse(v4_data())
    )
    length = (
        storyboard.estimate_ms(made) + contract.TITLE_CARD_MS + contract.END_CARD_MS
    )
    assert contract.MIN_VIDEO_MS + 5_000 <= length <= contract.MAX_VIDEO_MS - 5_000


def test_video_4_registers_in_the_tool_last_and_records_streampulse_only():
    assert (
        config.VIDEOS["04-streampulse-rollout"]
        == "storyboards/04-streampulse-rollout.yaml"
    )
    # `all` records it last: the longest take, as plan v2.2's order has it.
    assert list(config.VIDEOS)[-1] == "04-streampulse-rollout"
    assert board().apps == ["streampulse"]
    assert board().seed == "demo"


def test_the_one_payoff_is_the_audit_logs_reason():
    held = [
        (s.id, number)
        for s in board().scenes
        for number, beat in enumerate(s.beats, start=1)
        if beat.payoff
    ]
    assert held == [("audit", 1)]


def test_the_streampulse_scenes_pick_their_device_on_camera():
    """The app keeps no device across a reload (StreamPulseApp.tsx holds it in
    React state), so each scene that films a device other than the default
    iPhone picks it on camera, which fetches the app's flags again."""
    for name, preset in (
        ("devices", "Pixel 7 · Android 14 · DE · free"),
        ("player", "Galaxy S10 · Android 12 · US · premium · app 3.1.0"),
        ("classic", "Galaxy S10 · Android 12 · US · premium · app 3.1.0"),
    ):
        clicked = [
            step.click.text
            for beat in scene(name).beats
            for step in beat.steps
            if isinstance(step, storyboard.Click)
        ]
        assert preset in clicked, name
    devices_ts = (STREAMPULSE / "src" / "lib" / "devices.ts").read_text(
        encoding="utf-8"
    )
    for preset in (
        "iPhone 15 · iOS 17.4 · US · premium",
        "Pixel 7 · Android 14 · DE · free",
        "Galaxy S10 · Android 12 · US · premium · app 3.1.0",
    ):
        assert f"label: '{preset}'" in devices_ts
    app = (STREAMPULSE / "src" / "components" / "StreamPulseApp.tsx").read_text(
        encoding="utf-8"
    )
    assert "useState<Device>(initialDevice)" in app
    assert "localStorage" not in app


def test_after_the_rollback_the_s10_presses_refresh_before_the_agreement():
    """Plan v2 C1: at B12 Refresh now is pressed before any frame shows the
    player, and the agreement is read at the next cue's start."""
    end = board().end_state.rollback
    assert (end.agree.scene, end.agree.beat) == ("classic", 2)
    first = scene("classic").beats[0]
    names = [
        step.click.name or step.click.text
        for step in first.steps
        if isinstance(step, storyboard.Click)
    ]
    assert names == [
        "Galaxy S10 · Android 12 · US · premium · app 3.1.0",
        "Player",
        "Refresh now",
    ]


def locators_of(beat: storyboard.Beat) -> List[storyboard.Locator]:
    """Every element a beat finds: its focus, its readings and its steps'."""
    found = [beat.focus, *beat.record.values()]
    for step in [*beat.before, *beat.steps, *beat.exit]:
        for name in type(step).model_fields:
            value = getattr(step, name)
            if isinstance(value, storyboard.Locator):
                found.append(value)
    return found


def page_sources(root: Path) -> str:
    """The page code under *root*: its components and pages, not its tests."""
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted([*root.rglob("*.tsx"), *root.rglob("*.ts")])
        if not ({"__tests__", "tests"} & set(path.parts))
        and not path.name.endswith((".test.tsx", ".test.ts"))
    )


def drawn_by(locator: storyboard.Locator, code: str) -> List[str]:
    """What *locator* names that *code* does not draw (empty: all of it)."""
    missing = []
    testids = [locator.testid] if locator.testid else []
    testids += re.findall(r"\[data-testid=([\w-]+)\]", locator.css or "")
    for testid in testids:
        templated = re.match(r"(panel-flag-(?:reason-)?)(streampulse_\w+)$", testid)
        if templated:
            prefix, key = templated.groups()
            if f"data-testid={{`{prefix}${{" not in code or f"'{key}'" not in code:
                missing.append(testid)
        elif f'data-testid="{testid}"' not in code:
            missing.append(testid)
    for attribute in re.findall(
        r"\[(data-(?:engine|player|status))", locator.css or ""
    ):
        if f"{attribute}=" not in code:
            missing.append(attribute)
    for ident in re.findall(r"#([\w-]+)", locator.css or ""):
        if f'id="{ident}"' not in code:
            missing.append(f"#{ident}")
    for text in (locator.text, locator.name):
        if text and text not in code:
            missing.append(text)
    return missing


def test_every_element_video_4_finds_is_one_its_page_draws():
    """Each locator's testid, attribute, id or text is in the code of the
    page it is filmed on: the dashboard's for its scenes, StreamPulse's for
    the app's (at the recorded release; a renamed testid fails here, not in a
    take). A card's ``#headline`` is the tool's own."""
    code = {
        "dashboard": page_sources(REPO / "frontend" / "src"),
        "streampulse": page_sources(STREAMPULSE / "src"),
    }
    missing = {}
    for made in board().scenes:
        if made.card is not None:
            continue
        for index, beat in enumerate(made.beats, start=1):
            for locator in locators_of(beat):
                gone = drawn_by(locator, code[made.app])
                if gone:
                    missing[f"{made.id} beat {index}"] = gone
    assert missing == {}
    # The check finds a renamed testid, attribute and label.
    assert drawn_by(storyboard.Locator(testid="safety-state"), code["dashboard"])
    assert (
        drawn_by(
            storyboard.Locator(css="[data-testid=player][data-variant=v2]"),
            code["streampulse"],
        )
        == []
    )  # an attribute it does not know is not checked...
    assert drawn_by(
        storyboard.Locator(css="[data-testid=players][data-player=v2]"),
        code["streampulse"],
    ) == ["players"]  # ...but the testid it qualifies is
    assert (
        drawn_by(
            storyboard.Locator(text="Galaxy S10 · Android 12", exact=True),
            code["streampulse"],
        )
        == []
    )
    assert drawn_by(
        storyboard.Locator(text="Galaxy S11 · Android 12", exact=True),
        code["streampulse"],
    ) == ["Galaxy S11 · Android 12"]


# --- Narration (T165 C6; G1-G3; A9) ----------------------------------------------------

#: Every number the narration says, and where the page draws it at that beat
#: (T165 C6; the capture's G2 checks it on the page).
SPOKEN_NUMBERS = {
    # The preset label "iPhone 15 · iOS 17.4 · US · premium" and the phone's status bar.
    ("devices", 1, 1): ["17.4"],
    # The preset label "Pixel 7 · Android 14 · DE · free".
    ("devices", 2, 1): ["7", "14"],
    # The rule's version field, "17.0.0" (G2 compares 17 and 17.0 as one).
    ("rule", 1, 1): ["17"],
    # The stage list: the "25%" stage, in progress.
    ("stages", 1, 1): ["25%"],
    # The preset label "Galaxy S10 · Android 12 · US · premium · app 3.1.0".
    ("player", 1, 1): ["12"],
    # The card's headline "Player v2 crashes on Android 12".
    ("crashes", 1, 1): ["12"],
    # The rollout's "5%".
    ("paused", 2, 1): ["5%"],
    # The Galaxy S10's preset label, still on screen.
    ("classic", 2, 1): ["12"],
}


def test_every_beat_is_narrated_and_the_lints_report_nothing():
    made = board()
    assert all(beat.say for s in made.scenes for beat in s.beats)
    assert narration.board_problems(made) == []
    assert len(storyboard.narration_lines(made)) == 15


def test_the_spoken_numbers_are_the_ones_listed():
    said = {
        (s, beat, line): storyboard.numbers_in(text)
        for s, beat, line, text in storyboard.narration_lines(board())
        if storyboard.numbers_in(text)
    }
    assert said == SPOKEN_NUMBERS


def test_no_sentence_says_the_measured_gap_or_the_rate():
    """The time card's minutes and the error rate are measured on the run:
    neither is ever said (UX A9, plan v2 C13)."""
    for _s, _b, _l, text in storyboard.narration_lines(board()):
        assert not re.search(r"[0-9]+ minutes?\b", text), text
        assert [n for n in storyboard.numbers_in(text) if n.endswith("%")] in (
            [],
            ["25%"],
            ["5%"],
        ), text


@pytest.mark.regression
def test_a_card_with_a_measured_value_never_says_it():
    """A9: the card after the wait shows the measured minutes; a sentence
    that says a number only that value could show is refused at load."""
    raw = v4_data()
    later = raw_scene(raw, "later")
    later["beats"][0]["say"] = ["2 minutes later, the safety monitor has acted."]
    refused = load_refusal(raw)
    assert "says 2, which only the measured {rollback_minutes} could show" in refused


def test_a_measured_card_may_say_a_number_its_fixed_text_draws():
    """Video 6's traffic card says 8,000, which its headline draws: allowed."""
    v6 = storyboard.load(
        ACCEPTANCE / "showcase" / "storyboards" / "06-shoplab-ab-test.yaml"
    )
    assert narration.measured_problems(v6) == []


# --- D84 and S7: every departure stated where it happens -------------------------------

#: Every departure video 4 states, with the scene that states it.
STATEMENTS = [
    ("devices", "off-camera setup", "Seeded off camera"),
    ("rule", "off-camera setup", "Signed in off camera"),
    ("stages", "off-camera setup", "the demo's script moved it"),
    ("crashes", "simulated traffic", "Simulated by the tool"),
    ("later", "time skip", "Waited off camera"),
    (
        "later",
        "shortened interval",
        "The safety monitor checks every minute in this demo (default: every 5 minutes).",
    ),
]


def test_video_4_requires_and_states_exactly_these_departures():
    made = board()
    assert storyboard.required_departures(made) == [
        ("devices", "off-camera setup"),
        ("rule", "off-camera setup"),
        ("stages", "off-camera setup"),
        ("crashes", "simulated traffic"),
        ("later", "time skip"),
    ]
    assert [
        (s.id, d.kind, d.says) for s in made.scenes for d in s.departures
    ] == STATEMENTS
    assert storyboard.departure_problems(made) == []


@pytest.mark.parametrize("scene_id, kind, says", STATEMENTS)
def test_each_statement_taken_off_the_screen_is_refused(scene_id, kind, says):
    raw = v4_data()
    found = raw_scene(raw, scene_id)
    for item in found["beats"]:
        item["cue"] = item["cue"].replace(says, "Shown here")
    if "card" in found:
        found["card"]["note"] = found["card"]["note"].replace(says, "Shown here")
    problems = storyboard.departure_problems(storyboard.parse(raw))
    assert (
        f"scene {scene_id}: the {kind} statement {says!r} is not in a caption or the card"
        in problems
    )


@pytest.mark.regression
@pytest.mark.parametrize(
    "scene_id, kind",
    [
        ("stages", "off-camera setup"),
        ("crashes", "simulated traffic"),
        ("later", "time skip"),
    ],
)
def test_each_incident_step_requires_its_own_departure(scene_id, kind):
    """S7: the rollout script is off-camera setup, the crashes simulated
    traffic, and the wait a time skip, each on the scene it runs before."""
    raw = v4_data()
    found = raw_scene(raw, scene_id)
    found["departures"] = [d for d in found["departures"] if d["kind"] != kind]
    assert f"scene {scene_id}: {kind} is not stated on screen (departures)" in (
        storyboard.departure_problems(storyboard.parse(raw))
    )


def test_every_off_camera_step_has_its_kind_and_the_other_videos_are_unchanged():
    """A closed table, one row a step; videos 5 and 6 and the probe need
    exactly what they needed before (their seeds and visitors are setup)."""
    assert set(storyboard.OFF_CAMERA_KINDS) == {
        storyboard.SeedApp,
        storyboard.Visitor,
        storyboard.Story,
        storyboard.Crashes,
        storyboard.WaitForRollback,
    }
    v6 = storyboard.load(run.board_path("06-shoplab-ab-test"))
    assert storyboard.required_departures(v6) == [
        ("setup", "off-camera setup"),
        ("shopper-a", "off-camera setup"),
        ("shopper-b", "off-camera setup"),
        ("later", "simulated traffic"),
        ("later", "time skip"),
        ("result", "off-camera setup"),
    ]
    probe = storyboard.load(run.board_path("90-app-probe"))
    assert storyboard.required_departures(probe) == [
        ("storefront", "off-camera setup"),
        ("player", "off-camera setup"),
        ("experiments", "off-camera setup"),
    ]


def test_the_crash_card_states_the_counts_the_sender_sends():
    """S7 (the #1267 pattern): the card's count and its crashes are the
    storyboard's own, and the card says they are simulated."""
    plan = crash_plan()
    note = scene("crashes").card.note
    assert (plan.evaluations, plan.reports) == (200, 40)
    assert f"ask for Player v2 {plan.evaluations} times" in note
    assert f"report {plan.reports} crashes" in note
    assert note.startswith("Simulated by the tool:")
    calls = incident.crash_calls(plan)
    assert sum(c.kind == "evaluate" for c in calls) == plan.evaluations
    assert sum(c.kind == "report" for c in calls) == plan.reports


@pytest.mark.regression
def test_a_storyboard_without_the_own_build_sentence_is_refused():
    """S2 (EM C6 of T166): video 4 says it on the crash card."""
    assert storyboard.own_build_scene(board()) == "crashes"
    raw = v4_data()
    card = raw_scene(raw, "crashes")["card"]
    card["note"] = card["note"].replace(
        " The demo apps run from their own build in this recording.", ""
    )
    assert storyboard.departure_problems(storyboard.parse(raw)) == [
        "the demo apps are recorded, and no scene draws"
        f" {storyboard.OWN_BUILD!r} (EM C6 of T166)"
    ]


def test_no_v4_text_gives_the_server_keys_reason():
    """T156 C15: the storyboard says the crashes are simulated, and nothing of
    the key they are reported with (its name, its scope, or why)."""
    text = V4.read_text(encoding="utf-8").lower()
    for word in (
        "server key",
        "server-side",
        "sdk:ruleset",
        "scope",
        "count toward",
        "counted",
    ):
        assert word not in text, word


@pytest.mark.regression
def test_no_frame_may_show_the_paragraph_on_reports_that_do_not_count():
    """The flag page explains reports it leaves out of the error rate (in its
    own words, which give a key's reason). Video 4 sends none, and the crash
    step refuses any; and a frame that drew that paragraph would be refused by
    a neutral phrase of it on ``never_show``, which names no key."""
    page = (
        REPO / "frontend" / "src" / "pages" / "feature-flags" / "[id].tsx"
    ).read_text(encoding="utf-8")
    block = page[page.index('data-testid="safety-uncounted-reports"') :]
    block = block[: block.index("</p>")]
    singular, plural = re.findall(r"`([^`]*)`", block)
    assert "stored, but do" in board().never_show
    for said in (singular, plural):
        assert "stored, but do" in said
    forbidden = run.forbidden_text(board(), PORTS)
    assert "stored, but do" in forbidden


# --- S4 at load: the incident whole, once, in order ------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("step", [1, 3, 4, 5, 6, 7])
def test_any_story_step_but_2_is_refused_at_load(step):
    """S4: step 3 sends its own incident traffic and step 4 rolls back by hand
    when the monitor is late; only step 2 is the video's."""
    raw = v4_data()
    raw_scene(raw, "stages")["off_camera"] = [{"story": step}]
    assert "story" in load_refusal(raw)


@pytest.mark.regression
@pytest.mark.parametrize(
    "change, words",
    [
        # The rollout script before the StreamPulse seed ran.
        (
            lambda raw: (
                raw_scene(raw, "devices")["off_camera"].insert(0, {"story": 2})
                or raw_scene(raw, "stages").pop("off_camera")
            ),
            "the StreamPulse seed runs first",
        ),
        # The crashes before the rollout script.
        (
            lambda raw: (
                raw_scene(raw, "feed").update(
                    off_camera=[{"crashes": {"evaluations": 200, "reports": 40}}]
                )
                or raw_scene(raw, "crashes").pop("off_camera")
            ),
            "then crashes",
        ),
        # No scene between the crashes and the wait: no B9 to film.
        (
            lambda raw: raw["scenes"].remove(raw_scene(raw, "unhealthy")),
            "at least one scene before wait_for_rollback",
        ),
        # The wait for another percentage than the end state's.
        (
            lambda raw: raw_scene(raw, "later")["off_camera"][0][
                "wait_for_rollback"
            ].update(percentage=0),
            "the wait is for 0%",
        ),
        # Another flag than the one the crashes are sent against.
        (
            lambda raw: raw_scene(raw, "later")["off_camera"][0][
                "wait_for_rollback"
            ].update(flag="streampulse_recs_v2"),
            "the crashes are sent against streampulse_player_v2",
        ),
        # The crashes twice.
        (
            lambda raw: raw_scene(raw, "unhealthy").update(
                off_camera=[{"crashes": {"evaluations": 200, "reports": 40}}]
            ),
            "crashes runs twice",
        ),
        # No rollback end state.
        (
            lambda raw: raw.update(
                end_state={"signed_in_as": "admin@demo.com", "experiments_listed": 3}
            ),
            "and no rollback end state",
        ),
    ],
)
def test_an_incident_out_of_order_or_incomplete_is_refused_at_load(change, words):
    raw = v4_data()
    change(raw)
    assert words in load_refusal(raw)


@pytest.mark.regression
def test_b9_is_a_dashboard_page_never_a_card():
    """B9 films the dashboard's safety check: the scene before the wait is a
    dashboard page, never the app and never a card."""
    raw = v4_data()
    unhealthy = raw_scene(raw, "unhealthy")
    unhealthy.pop("flag_of")
    unhealthy.update(app="streampulse", open="/")
    assert "scene unhealthy is B9" in load_refusal(raw)
    raw = v4_data()
    unhealthy = raw_scene(raw, "unhealthy")
    unhealthy.pop("flag_of")
    unhealthy["card"] = {
        "label": "Off camera",
        "headline": "Unhealthy",
        "note": "The error rate passes its limit.",
    }
    unhealthy["beats"][0]["focus"] = {"css": "#headline"}
    unhealthy["beats"][0].pop("steps")
    assert "scene unhealthy is B9" in load_refusal(raw)


@pytest.mark.regression
@pytest.mark.parametrize(
    "where, words",
    [
        # The time card with a fixed duration (S5 tamper, refused at load).
        (
            lambda raw: raw_scene(raw, "later")["card"].update(
                headline="1 minute later"
            ),
            "a card that says the measured gap the cut hides",
        ),
        # The measured gap named on another card.
        (
            lambda raw: raw_scene(raw, "crashes")["card"].update(
                headline="{rollback_minutes} later"
            ),
            "only the card right after wait_for_rollback",
        ),
    ],
)
def test_the_measured_gap_is_said_on_the_card_after_the_wait_and_nowhere_else(
    where, words
):
    raw = v4_data()
    where(raw)
    assert words in load_refusal(raw)


@pytest.mark.regression
@pytest.mark.parametrize(
    "change, words",
    [
        # B9's badge read after the rollback.
        (
            lambda raw: (
                raw_scene(raw, "paused")["beats"][0]["record"].update(
                    unhealthy={"testid": "safety-status"}
                )
                or raw_scene(raw, "unhealthy")["beats"][0].pop("record")
            ),
            "is read in B9",
        ),
        # The paused badge read before the rollback.
        (
            lambda raw: (
                raw_scene(raw, "unhealthy")["beats"][0]["record"].update(
                    paused={"testid": "rollout-schedule-status"}
                )
                or raw_scene(raw, "paused")["beats"][0].pop("record")
            ),
            "is read after the rollback",
        ),
        # A reading no beat records.
        (
            lambda raw: raw_scene(raw, "audit")["beats"][0]["record"].pop("reason"),
            "which no beat records",
        ),
        # The agreement in a dashboard scene.
        (
            lambda raw: raw["end_state"]["rollback"]["agree"].update(scene="paused"),
            "in StreamPulse after the rollback",
        ),
        # The agreement at a beat that records neither reading.
        (
            lambda raw: raw["end_state"]["rollback"]["agree"].update(beat=1),
            "records the panel's",
        ),
        # A device the tool does not know.
        (
            lambda raw: raw["end_state"]["rollback"]["agree"].update(
                device="sp-custom-12345678"
            ),
            "not a StreamPulse device the tool knows",
        ),
        # Two rollbacks, or one that is not the last entry.
        (
            lambda raw: raw["end_state"]["rollback"].update(
                audit=["safety_rollback", "feature_flag_update"]
            ),
            "ends in one safety_rollback",
        ),
    ],
)
def test_the_rollback_end_state_reads_each_thing_where_it_is_true(change, words):
    raw = v4_data()
    change(raw)
    assert words in load_refusal(raw)


@pytest.mark.regression
@pytest.mark.parametrize(
    "crashes, words",
    [
        ({"evaluations": 200, "reports": 42}, "not a whole number of devices"),
        ({"evaluations": 200, "reports": 45}, "more than the 8 crashing devices"),
        ({"evaluations": 30, "reports": 40}, "cannot carry 40 crashes"),
    ],
)
def test_a_crash_plan_the_sender_cannot_send_is_refused_at_load(crashes, words):
    raw = v4_data()
    raw_scene(raw, "crashes")["off_camera"] = [{"crashes": crashes}]
    assert words in load_refusal(raw)


def test_a_flag_page_is_opened_by_name_on_the_dashboard_only():
    """``flag_of``: one opener a scene, and never an app's."""
    raw = v4_data()
    raw_scene(raw, "rule")["open"] = "/feature-flags"
    assert "exactly one, not both" in load_refusal(raw)
    raw = v4_data()
    devices = raw_scene(raw, "devices")
    devices.pop("open")
    devices["flag_of"] = "StreamPulse: AI search"
    assert "a streampulse scene opens a page of the app" in load_refusal(raw)


# --- The one setting video 4 changes: stated, read back (D84(c), S7, C6) ---------------

#: D84(c)'s words: "every minute in this demo (default: every 5 minutes)".
INTERVAL_STATEMENT = (
    "The safety monitor checks every minute in this demo (default: every 5 minutes)."
)


def test_video_4s_settings_are_its_apps_origin_and_the_stated_interval():
    listed = override_lists.load()
    assert override_lists.override_list(board(), PORTS, listed) == [
        override_lists.Override(
            "CORS_ORIGINS", "http://127.0.0.1:28401,http://127.0.0.1:28404"
        ),
        override_lists.Override("SAFETY_CHECK_INTERVAL_MINUTES", "1"),
    ]
    assert override_lists.statements("04-streampulse-rollout", listed) == [
        ("SAFETY_CHECK_INTERVAL_MINUTES", "1", INTERVAL_STATEMENT)
    ]
    assert "every minute in this demo (default: every 5 minutes)" in INTERVAL_STATEMENT
    assert run.incident_interval_s(board()) == 60.0
    # The card that states it, as a card draws it.
    assert INTERVAL_STATEMENT in " ".join(storyboard.card_texts(scene("later").card))


def with_v4_override(entry: dict) -> dict:
    listed = override_lists.load()
    listed = dict(listed)
    listed["04-streampulse-rollout"] = {
        "CORS_ORIGINS": listed["04-streampulse-rollout"]["CORS_ORIGINS"],
        **entry,
    }
    return listed


@pytest.mark.regression
@pytest.mark.parametrize(
    "entry, words",
    [
        # Stated, but the scene states it as another kind of departure.
        (
            {
                "SAFETY_CHECK_INTERVAL_MINUTES": {
                    "value": "1",
                    "statement": "Waited off camera",
                }
            },
            "no scene states 'Waited off camera' as a shortened interval",
        ),
        # Set, with a reason instead of a statement: the scene's shortened
        # interval is then a claim no stated setting makes.
        (
            {
                "SAFETY_CHECK_INTERVAL_MINUTES": {
                    "value": "1",
                    "why": "x" * 60,
                }
            },
            "states a shortened interval",
        ),
        # Not set at all: the same.
        ({}, "states a shortened interval"),
    ],
)
def test_a_shortened_interval_is_stated_exactly_when_it_is_set(entry, words):
    with pytest.raises(guard.Refused, match=words):
        override_lists.override_list(board(), PORTS, with_v4_override(entry))


def test_the_interval_line_the_api_writes_is_safety_schedulers_own():
    """C6 reads back the line ``SafetyScheduler.start`` logs (backend/app/core/
    safety_scheduler.py at the recorded release)."""
    source = (REPO / "backend" / "app" / "core" / "safety_scheduler.py").read_text(
        encoding="utf-8"
    )
    assert (
        'f"Safety scheduler started with {self.interval_minutes} minute interval"'
        in source
    )
    assert (
        override_lists.log_line("SAFETY_CHECK_INTERVAL_MINUTES", "1")
        == "Safety scheduler started with 1 minute interval"
    )


@pytest.mark.regression
def test_a_stated_setting_is_refused_on_the_uvicorn_stack_before_any_docker_call(
    tmp_path,
):
    """The statement reaches the API through the Quick Start stack's override
    file only: on the uvicorn stack it would be false on screen."""
    options = run.Options(
        videos=["04-streampulse-rollout"],
        ref="v0.36.0",
        repo=REPO,
        work_dir=tmp_path / "work",
        profile="core",
        ports=PORTS,
        out=tmp_path / "videos",
        render=False,
        stack="uvicorn",
    )
    with pytest.raises(
        guard.Refused, match="reaches the API on the quickstart stack only"
    ):
        run.preflight(options, {"HOME": str(tmp_path)})


@pytest.mark.regression
def test_a_shortened_interval_nothing_sets_is_refused_on_the_uvicorn_stack_too(
    tmp_path, monkeypatch
):
    """With the statement taken out of overrides.yaml, the uvicorn stack has
    nothing to refuse it for; the scene's "every minute" would then be false,
    so the preflight refuses the claim itself, on every stack (S7)."""
    data = with_v4_override({})
    monkeypatch.setattr(run.override_lists, "load", lambda: data)
    options = run.Options(
        videos=["04-streampulse-rollout"],
        ref="v0.36.0",
        repo=REPO,
        work_dir=tmp_path / "work",
        profile="core",
        ports=PORTS,
        out=tmp_path / "videos",
        render=False,
        stack="uvicorn",
    )
    with pytest.raises(
        guard.Refused, match="states a shortened interval .* that no setting"
    ):
        run.preflight(options, {"HOME": str(tmp_path)})


# --- story: 2 (S4, PE 6 of T178 14) ----------------------------------------------------


def tree_of(tmp_path: Path) -> Path:
    return tmp_path / "work" / "tree-core"


def wanted_story(tmp_path: Path) -> List[str]:
    return incident.story_argv(tree_of(tmp_path), api=API, dashboard=DASH, app=PULSE)


def test_the_rollout_script_runs_step_2_with_the_tools_three_addresses(tmp_path):
    argv = wanted_story(tmp_path)
    assert argv[1:] == [
        str(
            tree_of(tmp_path)
            / "demo"
            / "streampulse"
            / "simulator"
            / "rollout_story.py"
        ),
        "--step",
        "2",
        "--api-url",
        API,
        "--dashboard-url",
        DASH,
        "--app-url",
        PULSE,
    ]
    incident.refuse_story_argv(argv, wanted_story(tmp_path))
    # The script itself, at the recorded release: those are its options, and
    # without --api-url it would drive a developer's API on port 8000.
    script = (STREAMPULSE / "simulator" / "rollout_story.py").read_text(
        encoding="utf-8"
    )
    assert 'DEFAULT_API_URL = "http://localhost:8000"' in script
    for option in ('"--api-url"', '"--dashboard-url"', '"--app-url"', '"--step"'):
        assert option in script


def story_tampers(tmp_path: Path):
    good = wanted_story(tmp_path)
    no_api = good[:4] + good[6:]
    return [
        ("--api-url omitted", no_api),
        ("a second --api-url", good + ["--api-url", "http://127.0.0.1:28477"]),
        ("an abbreviation", good[:4] + ["--api-u", API] + good[6:]),
        ("an added --token", good + ["--token", "abcdefgh"]),
        ("an added --password", good + ["--password", "abcdefgh"]),
        ("step 3", good[:3] + ["3"] + good[4:]),
    ]


@pytest.mark.regression
@pytest.mark.parametrize("index", range(6))
def test_any_other_rollout_script_argv_is_refused_before_it_starts(tmp_path, index):
    """PE 6: whole-argv equality. argparse resolves a trailing duplicate and an
    abbreviation to another address while the argv still "contains" the
    tool's; an omitted --api-url drives localhost:8000."""
    name, argv = story_tampers(tmp_path)[index]
    with pytest.raises(guard.Refused, match="the rollout script runs as"):
        incident.refuse_story_argv(argv, wanted_story(tmp_path))


def test_the_story_child_is_given_its_own_list_and_nothing_else(tmp_path):
    keys = guard.allowed_env_keys("story")
    assert keys == frozenset(
        {"PATH", "HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED"}
        | set(guard.NO_AWS)
    )
    for key in keys:
        assert not key.endswith("_PROXY") and not key.lower().endswith("_proxy"), key
        assert not key.startswith("CONTROLVAR_"), key
    assert "story" in guard.KINDS


class RecordingChildren:
    """Stands in for ``procs.Children.run``: records the call, returns *status*."""

    def __init__(self, status: int = 0):
        self.status = status
        self.calls: List[Dict[str, Any]] = []

    def run(self, name, argv, *, cwd, env, log, kind, timeout=900):
        self.calls.append(
            {"name": name, "argv": list(argv), "env": dict(env), "kind": kind}
        )
        return self.status


def test_the_story_runs_as_a_child_of_kind_story_from_the_recorded_tree(tmp_path):
    children = RecordingChildren()
    parent = {"PATH": "/usr/bin:/bin", "HOME": "/nowhere", "HTTPS_PROXY": "http://x:1"}
    incident.run_story(
        tree=tree_of(tmp_path),
        children=children,
        logs=tmp_path / "logs",
        parent=parent,
        api_url=API,
        dashboard_url=DASH,
        app_url=PULSE,
        ports=PORTS,
    )
    (call,) = children.calls
    assert call["kind"] == "story" and call["name"] == "story-2"
    assert call["argv"] == wanted_story(tmp_path)
    # The caller's proxy never reaches it (its HTTP calls would honour one).
    assert set(call["env"]) <= guard.allowed_env_keys("story")
    assert "HTTPS_PROXY" not in call["env"]


@pytest.mark.regression
def test_a_story_that_does_not_exit_0_is_refused(tmp_path):
    with pytest.raises(
        director.CaptureFailed, match="S4: the rollout script's step 2 exited 1"
    ):
        incident.run_story(
            tree=tree_of(tmp_path),
            children=RecordingChildren(status=1),
            logs=tmp_path / "logs",
            parent={"PATH": "/usr/bin"},
            api_url=API,
            dashboard_url=DASH,
            app_url=PULSE,
            ports=PORTS,
        )


@pytest.mark.regression
@pytest.mark.parametrize(
    "addresses",
    [
        # The stack naming a developer's API: the script would drive it.
        {"api_url": "http://127.0.0.1:28477"},
        {"dashboard_url": "http://127.0.0.1:28478"},
        {"app_url": "http://localhost:28404"},
    ],
)
def test_a_story_aimed_anywhere_but_the_tools_ports_never_starts(tmp_path, addresses):
    """PE 6: the argv is built from the stack's addresses and must equal the
    one built from the tool's own ports, before the script starts."""
    children = RecordingChildren()
    given = {"api_url": API, "dashboard_url": DASH, "app_url": PULSE, **addresses}
    with pytest.raises(guard.Refused, match="the rollout script runs as"):
        incident.run_story(
            tree=tree_of(tmp_path),
            children=children,
            logs=tmp_path / "logs",
            parent={"PATH": "/usr/bin"},
            ports=PORTS,
            **given,
        )
    assert children.calls == []


@pytest.mark.regression
def test_the_server_key_never_reaches_the_story_child(tmp_path):
    """Kept private once read (``run.keep_private``): a child given it on its
    argv or in its environment is refused before it starts."""
    work = guard.WorkRoot(tmp_path / "work")
    work.path.mkdir(parents=True)
    children = Children(work, {"PATH": "/usr/bin:/bin"})
    run.keep_private(SERVER_KEY, director.redaction.Redactor(), children, [])
    argv = wanted_story(tmp_path) + ["--api-key", SERVER_KEY]
    with pytest.raises(guard.Refused, match="a private key is on its argv"):
        children.run(
            "story-2",
            argv,
            cwd=tmp_path,
            env={"PATH": "/usr/bin"},
            log=tmp_path / "story.log",
            kind="story",
        )


@pytest.mark.regression
def test_a_proxy_given_to_the_story_child_is_refused(tmp_path):
    work = guard.WorkRoot(tmp_path / "work")
    work.path.mkdir(parents=True)
    children = Children(work, {"PATH": "/usr/bin:/bin"})
    with pytest.raises(guard.Refused, match="HTTPS_PROXY"):
        children.run(
            "story-2",
            wanted_story(tmp_path),
            cwd=tmp_path,
            env={"PATH": "/usr/bin", "HTTPS_PROXY": "http://127.0.0.1:28499"},
            log=tmp_path / "story.log",
            kind="story",
        )


class FakeApi:
    """Stands in for ``incident.Api``: GETs answered from a table of path
    prefixes, each an answer or a function of the path; every call kept."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls: List[Any] = []

    def get(self, path):
        self.calls.append(path)
        for prefix, answer in self.routes:
            if path.startswith(prefix):
                return answer(path) if callable(answer) else answer
        raise AssertionError(f"an unexpected GET {path}")

    def get_with_key(self, path, key):
        self.calls.append(("with a key", path, key))
        return self.get(path)


def flag_item(**fields) -> Dict[str, Any]:
    item = {
        "id": "f-player",
        "key": FLAG,
        "name": "StreamPulse: Player v2",
        "rollout_percentage": 25,
        "status": "active",
    }
    item.update(fields)
    return item


def schedule_item(stage_2: str = "in_progress", status: str = "active", target=25):
    return {
        "items": [
            {
                "status": status,
                "stages": [
                    {"stage_order": 1, "status": "completed", "target_percentage": 5},
                    {"stage_order": 2, "status": stage_2, "target_percentage": target},
                    {"stage_order": 3, "status": "pending", "target_percentage": 50},
                ],
            }
        ]
    }


def test_after_the_story_the_flag_is_at_stage_2s_percentage():
    api = FakeApi(
        [
            ("/api/v1/feature-flags/", {"items": [flag_item()]}),
            ("/api/v1/rollout-schedules/", schedule_item()),
        ]
    )
    assert incident.story_done(api, FLAG) == {
        "rollout_percentage": 25,
        "schedule": "active",
        "stage_2": "in_progress",
        "stage_2_target": 25,
    }


@pytest.mark.regression
@pytest.mark.parametrize(
    "flag, schedule",
    [
        (flag_item(rollout_percentage=5), schedule_item(stage_2="pending")),
        (flag_item(rollout_percentage=5), schedule_item()),
        (flag_item(), schedule_item(status="paused")),
    ],
)
def test_a_story_that_left_the_flag_elsewhere_is_refused(flag, schedule):
    api = FakeApi(
        [
            ("/api/v1/feature-flags/", {"items": [flag]}),
            ("/api/v1/rollout-schedules/", schedule),
        ]
    )
    with pytest.raises(
        director.CaptureFailed, match="S4: after the rollout script's step 2"
    ):
        incident.story_done(api, FLAG)


# --- The crash (T156 C15, plan v2 C13, PE 7) -------------------------------------------


def test_the_crash_is_200_asks_and_40_reports_from_android_12_devices():
    calls = incident.crash_calls(crash_plan())
    asks = [c for c in calls if c.kind == "evaluate"]
    reports = [c for c in calls if c.kind == "report"]
    assert (len(asks), len(reports)) == (200, 40)
    crashing = sorted({c.device for c in reports})
    assert crashing == sorted(config.CRASH_DEVICES)
    for device in crashing:
        mine = [c.kind for c in calls if c.device == device]
        # Its crash loop: it asks, it crashes, five times.
        assert mine == ["evaluate", "report"] * config.CRASH_LOOP, device
    for call in calls:
        assert call.attributes["os"] == "Android"
        assert call.attributes["os_version"] == "12.0.0"
        assert call.attributes["employee"] is False
        assert call.attributes["device_id"] == call.device
    # The same every run: a pure function of the plan.
    assert incident.crash_calls(crash_plan()) == calls
    body = incident.report_body(reports[0])
    assert body["feature_flag_key"] == FLAG and body["error_type"] == "crash"
    assert body["message"] == config.CRASH_MESSAGE
    traffic_py = (STREAMPULSE / "simulator" / "traffic.py").read_text(encoding="utf-8")
    assert f'CRASH_MESSAGE = "{config.CRASH_MESSAGE}"' in traffic_py
    assert "INCIDENT_RELAUNCHES = 4" in traffic_py  # CRASH_LOOP is 1 + 4


@pytest.mark.parametrize("device", config.CRASH_DEVICES)
def test_each_crashing_device_is_in_player_v2_at_25_and_out_at_5(device):
    """By the server's own rollout function: a device that crashes in Player v2
    got Player v2 at the 25% stage, and is out of it after the rollback."""
    assert in_rollout(device, 25) and not in_rollout(device, 5)


def test_the_galaxy_s10_is_in_at_25_and_out_at_5_and_the_others_are_out():
    """B7 films the S10 with Player v2, B12 with the classic player; the
    iPhone and the Pixel never get it at either stage."""
    assert in_rollout(S10, 25) and not in_rollout(S10, 5)
    for other in ("sp-iphone-15-us-premium", "sp-pixel-7-de-free"):
        assert not in_rollout(other, 25) and not in_rollout(other, 5)


def test_the_s10_is_asked_about_as_streampulse_sends_it():
    """``config.STREAMPULSE_DEVICES`` is devices.ts's preset, as
    ``toUserContext`` sends it (every attribute, the id included)."""
    source = (STREAMPULSE / "src" / "lib" / "devices.ts").read_text(encoding="utf-8")
    block = source[source.index("device_id: 'sp-galaxy-s10-us-premium'") :]
    block = block[: block.index("}")]
    fields = dict(re.findall(r"(\w+): ('[^']*'|true|false)", block))
    parsed = {
        k: (v == "true" if v in ("true", "false") else v.strip("'"))
        for k, v in fields.items()
    }
    assert parsed == config.STREAMPULSE_DEVICES[S10]
    sent = re.search(r"attributes: \{(.*?)\}", source, re.S).group(1)
    assert sorted(re.findall(r"(\w+): device\.", sent)) == sorted(
        config.STREAMPULSE_DEVICES[S10]
    )


class Answer:
    def __init__(self, status: int, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class Session:
    """Stands in for ``traffic.direct_session()``: every request kept, each
    ask answered by the server's own rollout function at *percentage*."""

    def __init__(self, percentage: int = 25, report_status: int = 201, on=None):
        self.percentage = percentage
        self.report_status = report_status
        self.on = on
        self.requests: List[Dict[str, Any]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append(
            {"method": "GET", "url": url, "params": params, "headers": headers}
        )
        device = params["user_id"]
        enabled = (
            in_rollout(device, self.percentage) if self.on is None else self.on(device)
        )
        return Answer(200, {"key": FLAG, "enabled": enabled, "reason": "rollout"})

    def post(self, url, json=None, headers=None, timeout=None):
        self.requests.append(
            {"method": "POST", "url": url, "json": json, "headers": headers}
        )
        return Answer(self.report_status, {"id": "e1"})


class Wall:
    """A wall clock that moves only when the code sleeps."""

    def __init__(self, at: float = 1_800_000_000.0):
        self.at = at

    def __call__(self) -> float:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.at += seconds


def sender(session=None, **changes):
    made = {
        "api_url": API,
        "allowed": API,
        "app_key": APP_KEY,
        "server_key": SERVER_KEY,
        "session": (lambda: session) if session is not None else (lambda: Session()),
        "sleep": lambda s: None,
        "rate": None,
    }
    made.update(changes)
    return incident.CrashSender(incident.crash_calls(crash_plan()), **made)


def test_the_asks_carry_the_apps_key_and_the_reports_the_server_key():
    session = Session()
    numbers = sender(session).send()
    asks = [r for r in session.requests if r["method"] == "GET"]
    reports = [r for r in session.requests if r["method"] == "POST"]
    assert (len(asks), len(reports)) == (200, 40)
    assert all(r["headers"] == {"X-API-Key": APP_KEY} for r in asks)
    assert all(r["headers"] == {"X-API-Key": SERVER_KEY} for r in reports)
    assert all(r["url"] == f"{API}/api/v1/feature-flags/evaluate/{FLAG}" for r in asks)
    assert all(r["url"] == f"{API}/api/v1/tracking/errors" for r in reports)
    assert (numbers["evaluations"], numbers["reports"]) == (200, 40)
    assert numbers["crashing_devices"] == 8
    # Each ask sends the device's attributes as the app's context.
    assert json.loads(asks[0]["params"]["context"])["os_version"] == "12.0.0"


@pytest.mark.regression
@pytest.mark.parametrize(
    "address",
    ["http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:28477"],
)
def test_a_crash_sender_aimed_anywhere_but_the_stacks_api_refuses_before_any_request(
    address,
):
    """PE 7: a planted developer's API (port 8000) or another port refuses
    before a session is even opened."""
    opened = []
    with pytest.raises(
        director.CaptureFailed, match="S4: the crash sender sends to the stack's API"
    ):
        incident.CrashSender(
            incident.crash_calls(crash_plan()),
            api_url=address,
            allowed=API,
            app_key=APP_KEY,
            server_key=SERVER_KEY,
            session=lambda: opened.append(1) or Session(),
        )
    assert opened == []


@pytest.mark.regression
@pytest.mark.parametrize(
    # Named ids: a key's value never goes into a test's id, and so a log.
    "server_key",
    [None, "", APP_KEY],
    ids=["none read", "empty", "the app's key"],
)
def test_a_crash_without_the_server_key_refuses_before_any_request(server_key):
    with pytest.raises(
        director.CaptureFailed, match="no StreamPulse server key was read"
    ):
        sender(server_key=server_key)


@pytest.mark.regression
def test_a_report_answered_other_than_2xx_refuses_and_names_no_key():
    with pytest.raises(director.CaptureFailed) as caught:
        sender(Session(report_status=401)).send()
    said = str(caught.value)
    assert "S4: the crash sender's report for sp-a12-crash-" in said
    assert "answered 401" in said
    assert SERVER_KEY not in said and APP_KEY not in said


@pytest.mark.regression
def test_a_crashing_device_the_api_answered_off_is_refused():
    """A device that never got Player v2 cannot crash in it: refused at its
    ask, before any crash of it is reported."""
    session = Session(percentage=5)
    with pytest.raises(director.CaptureFailed, match="answered it off"):
        sender(session).send()
    assert [r["method"] for r in session.requests if r["method"] == "POST"] == []
    assert session.requests[-1]["params"]["user_id"] == config.CRASH_DEVICES[0]


def test_the_sender_runs_in_this_process_on_a_session_that_takes_no_proxy():
    """PE 7: ``traffic.direct_session`` by default, which ignores the
    environment's proxy settings; no child process is started."""
    made = incident.CrashSender(
        incident.crash_calls(crash_plan()),
        api_url=API,
        allowed=API,
        app_key=APP_KEY,
        server_key=SERVER_KEY,
    )
    from showcase.capture import traffic

    assert made._session is traffic.direct_session
    assert traffic.direct_session().trust_env is False
    tree = ast.parse((CAPTURE / "incident.py").read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "subprocess" not in imported and "os" not in imported


# --- B9's anchor (T165 C5) -------------------------------------------------------------


def iso(seconds: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def history(wall: Wall, rows_after: List[Dict[str, Any]], before: List[Dict[str, Any]]):
    """The scheduler history the API answers: *before* at the first look,
    then *rows_after* added (newest first) from the second."""
    looks = {"n": 0}

    def answer(path):
        looks["n"] += 1
        return before if looks["n"] == 1 else rows_after + before

    return answer


def test_the_anchor_is_the_first_safety_run_that_completes_after_the_wait_began():
    wall = Wall()
    old = [{"id": "r1", "status": "success", "completed_at": iso(wall.at - 30)}]
    fresh = [{"id": "r2", "status": "success", "completed_at": iso(wall.at - 0.2)}]
    api = FakeApi([("/api/v1/scheduler/safety/history", history(wall, fresh, old))])
    found = incident.wait_for_anchor(api, interval_s=60, wall=wall, sleep=wall.sleep)
    assert found["run"] == "r2" and found["runs_before"] == 1
    assert 0 <= found["lag_s"] <= 1.0


@pytest.mark.regression
def test_a_run_that_was_there_before_the_wait_never_anchors_b9():
    """T165 C5's extra tamper: the history holds a fresh-looking success that
    was already there; with no new run the wait times out, three intervals."""
    wall = Wall()
    there = [{"id": "r1", "status": "success", "completed_at": iso(wall.at - 0.1)}]
    api = FakeApi([("/api/v1/scheduler/safety/history", lambda path: there)])
    with pytest.raises(
        director.CaptureFailed, match="B9: no safety run completed within 180 s"
    ):
        incident.wait_for_anchor(api, interval_s=60, wall=wall, sleep=wall.sleep)
    # Three intervals, no fewer and not many more.
    assert 180 <= wall.at - 1_800_000_000.0 <= 180 + 2 * config.ANCHOR_POLL_S


@pytest.mark.regression
def test_a_new_run_that_failed_is_not_an_anchor():
    wall = Wall()
    failed = [{"id": "r2", "status": "failed", "completed_at": iso(wall.at)}]
    api = FakeApi([("/api/v1/scheduler/safety/history", history(wall, failed, []))])
    with pytest.raises(director.CaptureFailed, match="no safety run completed"):
        incident.wait_for_anchor(api, interval_s=60, wall=wall, sleep=wall.sleep)


@pytest.mark.regression
@pytest.mark.parametrize("skew", [-5.0, 9.0])
def test_clocks_that_disagree_cannot_time_b9(skew):
    """The API's clock ahead of the host's (a run "completed" in the host's
    future), or far behind: refused, as B9 compares the two."""
    wall = Wall()
    fresh = [{"id": "r2", "status": "success", "completed_at": iso(wall.at - skew)}]
    api = FakeApi([("/api/v1/scheduler/safety/history", history(wall, fresh, []))])
    with pytest.raises(director.CaptureFailed, match="the two clocks disagree"):
        incident.wait_for_anchor(api, interval_s=60, wall=wall, sleep=wall.sleep)


@pytest.mark.regression
def test_sends_that_begin_more_than_5_s_after_the_run_are_refused():
    anchor = {"completed_at_s": 1000.0}
    assert incident.check_sends_after(anchor, {"first_send": 1004.9}) == pytest.approx(
        4.9
    )
    with pytest.raises(
        director.CaptureFailed, match="B9: the crashes began 5.2 s after"
    ):
        incident.check_sends_after(anchor, {"first_send": 1005.2})


@pytest.mark.regression
def test_sends_that_begin_before_the_safety_run_completed_are_refused():
    """T165 C5's "within 5 s of its completed_at" has a lower side too: the
    crashes follow a completed run (plan v2 C3's plant: sends started on the
    run's start or its log line). The host's clock and the API's may be
    ``CLOCK_SKEW_MAX_S`` apart, so a send that early is still after it."""
    anchor = {"completed_at_s": 1000.0}
    assert incident.check_sends_after(anchor, {"first_send": 999.2}) == pytest.approx(
        -0.8
    )
    with pytest.raises(
        director.CaptureFailed,
        match="B9: the crashes began 1.5 s before the safety run completed",
    ):
        incident.check_sends_after(anchor, {"first_send": 998.5})


# --- b, the browser's own asks (plan v2 C13) --------------------------------------------


def check_answer(
    total: int, errors: int = 40, healthy=False, left_out: int = 0, metrics=None
):
    """A flag's safety check as the API answers it (``SafetyCheckResponse``):
    the error rate is its ``error_rate`` metric, the seed's one (critical
    0.05), and ``details`` holds only the counts behind it."""
    rate = 0.0 if total == 0 else errors / total
    return {
        "feature_flag_id": "00000000-0000-4000-8000-000000000001",
        "is_healthy": healthy,
        "metrics": [
            {
                "name": "error_rate",
                "current_value": rate,
                "threshold": 0.05,
                "is_healthy": rate <= 0.05,
                "details": {
                    "comparison_type": "greater_than",
                    "warning_threshold": 0.02,
                    "critical_threshold": 0.05,
                    "warning": rate > 0.02,
                    "measured": True,
                },
            }
        ]
        if metrics is None
        else metrics,
        "last_checked": "2026-10-20T10:02:00",
        "details": {
            "feature_flag_key": FLAG,
            "error_count": errors,
            "uncounted_error_count": left_out,
            "total_evaluations": total,
            "timeframe_minutes": 15,
        },
    }


def test_the_stand_in_safety_check_is_the_apis_answer():
    """``check_answer`` is what ``GET .../check`` answers at the recorded
    release: the schema accepts it, ``details`` is ``_error_window``'s counts
    with no rate in them, and the flag page draws the ``error_rate`` metric's
    ``current_value`` (the number B9's rate check compares)."""
    from backend.app.schemas.safety import SafetyCheckResponse

    answer = check_answer(240)
    SafetyCheckResponse.model_validate(answer)
    source = (REPO / "backend" / "app" / "services" / "safety_service.py").read_text(
        encoding="utf-8"
    )
    window = source[source.index("    def _error_window(") :]
    window = window[: window.index("\n    def ", 1)]
    assert set(re.findall(r'"(\w+)": metrics\["\w+"\]', window)) == (
        set(answer["details"]) - {"feature_flag_key"}
    )
    assert '"error_rate"' not in window
    assert (
        'details: Dict[str, Any] = {"feature_flag_key": feature_flag.key, **window}'
        in (source)
    )
    page = (
        REPO / "frontend" / "src" / "pages" / "feature-flags" / "[id].tsx"
    ).read_text(encoding="utf-8")
    row = page[page.index('data-testid="safety-metric"') :]
    assert "{m.current_value}" in row[: row.index("</li>")]


def test_b_is_the_apis_count_less_the_senders():
    api = FakeApi([("/api/v1/safety/feature-flags/f-player/check", check_answer(212))])
    found = incident.browser_evaluations(api, "f-player", crash_plan())
    assert found["browser_evaluations"] == 12 and found["crashes_counted"] == 40
    # The rate as the page draws it: the error_rate metric's, 40 of 212.
    assert found["error_rate"] == pytest.approx(40 / 212)


@pytest.mark.regression
@pytest.mark.parametrize(
    "answer, words",
    [
        # Plan v2 C13's tamper: a browser loop adds 150 asks.
        (check_answer(350), "browser evaluations 150 > 100"),
        (check_answer(180), "fewer than the 200 sent"),
        (check_answer(212, errors=0), "the API counts 0 crashes; 40 were sent"),
        (check_answer(212, left_out=40), "leaves out of the flag's error rate"),
        (check_answer(212, healthy=True), "still reads healthy"),
        # No error rate in the answer: B9 says the error rate passes its limit.
        (check_answer(212, metrics=[]), "does not answer its error rate above"),
        # Unhealthy by another metric, the error rate under its limit.
        (
            check_answer(
                212,
                metrics=[
                    {
                        "name": "error_rate",
                        "current_value": 0.04,
                        "threshold": 0.05,
                        "is_healthy": True,
                    },
                    {
                        "name": "latency_p95",
                        "current_value": 900.0,
                        "threshold": 500.0,
                        "is_healthy": False,
                    },
                ],
            ),
            "does not answer its error rate above",
        ),
    ],
)
def test_a_crash_the_api_does_not_count_as_planned_is_refused(answer, words):
    api = FakeApi([("/api/v1/safety/feature-flags/f-player/check", answer)])
    with pytest.raises(director.CaptureFailed, match=words):
        incident.browser_evaluations(api, "f-player", crash_plan())


# --- The wait, and the B9 gate ----------------------------------------------------------


def audit_entry(action, *, by="system:safety-monitor", user_id=None, at=0.0, **after):
    entry = {
        "action_type": action,
        "user_email": by,
        "user_id": user_id,
        "timestamp": iso(at),
        "reason": after.pop("reason", None),
        "new_value": json.dumps(after) if after else None,
    }
    return entry


ROLLBACK_REASON = "Automatic rollback due to error_rate above threshold (16.67% > 5%)"


def rollback_api(entries, percentage=5):
    return FakeApi(
        [
            (
                "/api/v1/feature-flags/",
                {"items": [flag_item(rollout_percentage=percentage)]},
            ),
            ("/api/v1/audit-logs/", {"items": entries}),
        ]
    )


def wait_item(within_s: int = 180) -> storyboard.RollbackWait:
    return storyboard.RollbackWait(flag=FLAG, percentage=5, within_s=within_s)


def test_the_wait_finds_the_safety_monitors_rollback():
    wall = Wall()
    entries = [audit_entry("safety_rollback", at=wall.at - 1, reason=ROLLBACK_REASON)]
    found = incident.wait_for_rollback(
        rollback_api(entries), wait_item(), wall=wall, sleep=wall.sleep
    )
    assert found["reason"] == ROLLBACK_REASON
    assert found["rollback_at_s"] == pytest.approx(wall.at - 1)


@pytest.mark.regression
def test_a_rollback_made_by_hand_is_refused_at_once():
    """QA C1's plant: the demo's step 4 rolls back by hand when automatic
    rollbacks are off; the video credits the safety monitor, so it refuses."""
    wall = Wall()
    entries = [
        audit_entry("safety_rollback", by="admin@demo.com", user_id="u1", at=wall.at)
    ]
    with pytest.raises(
        director.CaptureFailed,
        match="rolled back by 'admin@demo.com', not by the safety monitor",
    ):
        incident.wait_for_rollback(
            rollback_api(entries), wait_item(), wall=wall, sleep=wall.sleep
        )


@pytest.mark.regression
def test_no_rollback_in_time_is_refused_and_never_made_by_the_tool():
    wall = Wall()
    began = wall.at
    api = rollback_api([], percentage=25)
    with pytest.raises(
        director.CaptureFailed,
        match="did not roll streampulse_player_v2 back to 5% within 180 s",
    ):
        incident.wait_for_rollback(api, wait_item(), wall=wall, sleep=wall.sleep)
    assert all("rollback" not in str(call) for call in api.calls)
    # Refused at within_s, not later.
    assert 180 <= wall.at - began <= 180 + 2 * config.ROLLBACK_POLL_S


def test_nothing_in_the_tool_asks_for_a_rollback():
    """No request the tool makes names a flag's rollback path; the only mention
    of it is the listener's pattern that counts the page's (gate 10/4)."""
    found = []
    for path in sorted(CAPTURE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Docstrings (a string standing as a statement) are prose, not requests.
        prose = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        for node in ast.walk(tree):
            if id(node) in prose:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "/api/v1" in node.value and "rollback" in node.value:
                    found.append((path.name, node.value))
            if isinstance(node, ast.JoinedStr):
                text = "".join(
                    part.value
                    for part in node.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
                if "/api/v1" in text and "rollback" in text:
                    found.append((path.name, text))
    assert found == [
        ("director.py", r"/api/v1/safety/feature-flags/[^/]+/rollback/?$"),
    ]


@pytest.mark.regression
def test_a_rollback_before_b9s_last_frame_is_refused():
    """The B9 gate: the monitor's rollback must come after B9's last frame."""
    assert incident.b9_problem(b9_end=1000.0, rollback_at=1030.0, lag_s=0.3) is None
    problem = incident.b9_problem(b9_end=1000.0, rollback_at=999.0, lag_s=0.3)
    assert "B9 would film a page the monitor had already changed" in problem


# --- S5: the card after the wait (U3, PE 8) ---------------------------------------------


class Walls:
    """Stands in for the director: each scene's wall times, and the tally."""

    def __init__(self, walls, checks=()):
        self.scene_walls = walls
        self.tally = SimpleNamespace(safety_checks=list(checks))


def an_incident(api=None, wall=None, keys=None):
    stack = SimpleNamespace(
        api_url=API,
        dashboard_url=DASH,
        ports=PORTS,
        tree=Path("/nowhere"),
        children=RecordingChildren(),
        logs=Path("/nowhere"),
        parent={"PATH": "/usr/bin"},
        app_url=lambda app: PULSE,
    )
    clock = wall or Wall()
    return incident.Incident(
        board(),
        api=api or FakeApi([]),
        stack=stack,
        keys=[SERVER_KEY] if keys is None else keys,
        interval_s=60.0,
        wall=clock,
        sleep=clock.sleep,
        session=lambda: Session(),
    ), clock


@pytest.mark.regression
def test_the_card_says_the_wall_clock_gap_even_when_the_recording_saw_none():
    """PE 8: the recording's clock does not run in a cut, so the gap on it is 0
    ms; on the wall clock 180 s passed, and the card says 3 minutes."""
    made, wall = an_incident()
    b9_end_ms = 1000.0 * (wall.at - 180.0)
    filled = made.with_minutes(
        scene("later"),
        Walls({"unhealthy": {"start": b9_end_ms - 5000, "end": b9_end_ms}}),
    )
    assert filled.card.headline == "3 minutes later"
    assert storyboard.ROLLBACK_MINUTES not in filled.card.note
    # What U3 reads back: the text that held the placeholder, not the note.
    assert made.card_said == "3 minutes later"
    # Only the card after the wait is filled.
    assert made.with_minutes(scene("crashes"), Walls({})) == scene("crashes")


@pytest.mark.regression
def test_the_real_card_after_the_wait_passes_u3_beside_its_interval_note():
    """The card's note states the interval's default, "every 5 minutes": a
    second duration, which U3 once read as the card saying two gaps, so every
    take refused at the card. U3 reads only the text that held the measured
    value; filled and checked here as a take does both."""
    made, wall = an_incident()
    b9_end_ms = 1000.0 * (wall.at - 100.0)
    walls = Walls({"unhealthy": {"start": b9_end_ms - 5000, "end": b9_end_ms}})
    filled = made.with_minutes(scene("later"), walls)
    assert "every 5 minutes" in filled.card.note
    walls.scene_walls["later"] = {
        "start": 1000.0 * wall.at,
        "end": 1000.0 * wall.at + 6000,
    }
    made.after_scene(scene("later"), walls)
    assert made.numbers["card"]["said"] == "2 minutes"
    assert made.numbers["card"]["gap_s"] == 100.0


@pytest.mark.regression
@pytest.mark.parametrize(
    "said, gap_s, refused",
    [
        ("1 minute later", 180.0, True),  # the fixed card, against a measured 3 minutes
        ("3 minutes later", 180.0, False),
        ("3 minutes later", 205.0, False),
        ("3 minutes later", 215.0, True),
        ("1 minute later", 10.0, True),  # "1 minute" over a gap of 10 s
    ],
)
def test_a_card_whose_minutes_are_not_the_gap_within_30_s_is_refused(
    said, gap_s, refused
):
    problem = incident.card_gap_problem(said, gap_s)
    assert (problem is not None) == refused, problem


@pytest.mark.regression
def test_after_the_card_the_gap_to_its_first_frame_is_checked():
    made, wall = an_incident()
    # What with_minutes keeps for a card that said 1 minute (its headline).
    made.card_said = "1 minute later"
    walls = Walls(
        {
            "unhealthy": {"start": 0.0, "end": 1_000_000.0},
            "later": {"start": 1_000_000.0 + 180_000.0, "end": 1_000_000.0 + 186_000.0},
        }
    )
    with pytest.raises(director.CaptureFailed, match="U3: the time card says 1 minute"):
        made.after_scene(scene("later"), walls)


# --- B9's fit, before anything starts (T165 C5) -----------------------------------------


def test_b9_fits_the_one_minute_interval_silent_and_voiced():
    fit = incident.b9_fit_ms(board())
    assert fit is not None and fit <= (60 - config.B9_SPARE_S) * 1000
    assert incident.b9_fit_problem(fit, 60.0) is None
    # Voiced: every sentence as long as the longest the voice step allows.
    long = {
        (s, b, l): int(4.8 * contract.VOICE_RATE)
        for s, b, l, _t in storyboard.narration_lines(board())
    }
    assert incident.b9_fit_problem(incident.b9_fit_ms(board(), long), 60.0) is None
    assert (
        incident.b9_fit_ms(storyboard.load(run.board_path("06-shoplab-ab-test")))
        is None
    )


@pytest.mark.regression
def test_a_b9_that_cannot_end_15_s_before_the_monitor_acts_is_refused():
    """Refused before anything starts: here the crash card's and B9's
    narration, at 20 s a sentence, leave too little of the minute."""
    longer = {
        (s, b, l): int(20 * contract.VOICE_RATE)
        for s, b, l, _t in storyboard.narration_lines(board())
    }
    problem = incident.b9_fit_problem(incident.b9_fit_ms(board(), longer), 60.0)
    assert problem is not None and "B9 must end 15 s before it can act" in problem
    clips = [
        SimpleNamespace(scene=s, beat=b, line=l, samples=n)
        for (s, b, l), n in longer.items()
    ]
    with pytest.raises(guard.Refused, match="B9 does not fit"):
        run.refuse_b9_fit(board(), clips)


# --- The incident's steps wired together, on stand-ins ----------------------------------


def test_the_story_step_runs_the_script_then_reads_stage_2():
    api = FakeApi(
        [
            ("/api/v1/feature-flags/", {"items": [flag_item()]}),
            ("/api/v1/rollout-schedules/", schedule_item()),
        ]
    )
    made, _ = an_incident(api=api)
    made.step(next(i for i in scene("stages").off_camera), director=None)
    (call,) = made.stack.children.calls
    assert call["argv"] == incident.story_argv(
        Path("/nowhere"), api=API, dashboard=DASH, app=PULSE
    )
    assert made.numbers["story"]["stage_2"] == "in_progress"


def test_the_crash_step_anchors_sends_and_counts_in_that_order():
    wall = Wall()
    fresh = [{"id": "r2", "status": "success", "completed_at": iso(wall.at - 0.2)}]
    api = FakeApi(
        [
            ("/api/v1/scheduler/safety/history", history(wall, fresh, [])),
            ("/api/v1/feature-flags/", {"items": [flag_item()]}),
            ("/api/v1/safety/feature-flags/f-player/check", check_answer(209)),
        ]
    )
    made, _ = an_incident(api=api, wall=wall)
    made.step(next(i for i in scene("crashes").off_camera), director=None)
    numbers = made.numbers["crashes"]
    assert (numbers["evaluations"], numbers["reports"]) == (200, 40)
    assert numbers["browser_evaluations"] == 9
    assert made.numbers["anchor"]["run"] == "r2"
    order = [c for c in api.calls if isinstance(c, str)]
    assert order[0].startswith("/api/v1/scheduler/safety/history")
    assert order[-1].endswith("/check")


@pytest.mark.regression
def test_the_crash_step_with_no_key_read_refuses_before_the_anchor():
    """Before any wait or request: the seed's server key must have been read."""
    api = FakeApi([])
    made, _ = an_incident(api=api, keys=[])
    with pytest.raises(
        director.CaptureFailed, match="no StreamPulse server key was read"
    ):
        made.step(next(i for i in scene("crashes").off_camera), director=None)
    assert api.calls == []


def b9_director(end_s: float, checks):
    return Walls(
        {"unhealthy": {"start": 1000 * (end_s - 6), "end": 1000 * end_s}}, checks
    )


#: The crash's rate as the API counts it, 40 crashes in 240 evaluations (b 40):
#: the rate ROLLBACK_REASON gives, "16.67%".
CRASH_RATE = 40 / 240


def b9_check(where: str = "scene unhealthy", healthy=False, rate=CRASH_RATE):
    """One safety check answer B9's page got, as the director keeps it."""
    return {"where": where, "is_healthy": healthy, "error_rate": rate}


def crashed_incident(entries, wall, counted=CRASH_RATE):
    """The incident after its crash step: the anchor run, and the rate the API
    counted after the crashes (``browser_evaluations``)."""
    made, _ = an_incident(api=rollback_api(entries), wall=wall)
    made.anchor = {"lag_s": 0.3, "completed_at_s": wall.at - 60}
    made.numbers["crashes"] = {} if counted is None else {"error_rate": counted}
    return made


def test_the_wait_and_the_b9_gate_pass_when_the_rollback_follows_b9():
    wall = Wall()
    entries = [audit_entry("safety_rollback", at=wall.at - 2, reason=ROLLBACK_REASON)]
    made = crashed_incident(entries, wall)
    made.wait(
        wait_item(),
        b9_director(
            wall.at - 30,
            [b9_check("scene unhealthy"), b9_check("scene unhealthy beat 1")],
        ),
    )
    assert made.numbers["b9"]["page_checks"] == [False, False]
    assert made.numbers["b9"]["rollback_after_b9_s"] > 25
    # Plan v2 C13: the rate on screen is the API's, and the reason's.
    assert made.numbers["b9"]["rates"] == {
        "page": ["16.67%", "16.67%"],
        "counted": "16.67%",
        "reason": "16.67%",
    }


@pytest.mark.regression
@pytest.mark.parametrize(
    "checks, words",
    [
        ([], "asked for the flag's safety check 0 time"),
        ([b9_check(healthy=True)], "must show it unhealthy"),
        # Another scene's check does not count for B9.
        ([b9_check("scene unhealthy-2 beat 1")], "0 time"),
    ],
)
def test_a_b9_page_that_was_not_answered_unhealthy_is_refused(checks, words):
    wall = Wall()
    entries = [audit_entry("safety_rollback", at=wall.at - 2, reason=ROLLBACK_REASON)]
    made = crashed_incident(entries, wall)
    with pytest.raises(director.CaptureFailed, match=words):
        made.wait(wait_item(), b9_director(wall.at - 30, checks))


@pytest.mark.regression
@pytest.mark.parametrize(
    "checks, reason, counted, words",
    [
        # B9's page drew another rate than the API counted after the crashes.
        ([b9_check(rate=40 / 300)], ROLLBACK_REASON, CRASH_RATE, r"as \['13.33%'\]"),
        # One of B9's answers drew no rate.
        (
            [b9_check(), b9_check("scene unhealthy beat 1", rate=None)],
            ROLLBACK_REASON,
            CRASH_RATE,
            r"as \['16.67%', None\]",
        ),
        # The rollback's reason gives another rate.
        (
            [b9_check()],
            ROLLBACK_REASON.replace("16.67%", "13.33%"),
            CRASH_RATE,
            "the rollback's reason says 13.33%",
        ),
        # A reason that gives no rate.
        (
            [b9_check()],
            "Automatic rollback due to safety issues",
            CRASH_RATE,
            "the rollback's reason says None",
        ),
        # The API's own count, read after the crashes, is another rate.
        ([b9_check()], ROLLBACK_REASON, 40 / 212, "the API counted 18.87%"),
        # ...or was never read.
        ([b9_check()], ROLLBACK_REASON, None, "never read from the API"),
    ],
)
def test_b9_draws_the_error_rate_the_api_counted_and_the_reason_gives(
    checks, reason, counted, words
):
    """Plan v2 C13 (PE 13 of plan v1, "read on-screen rate from the API"): the
    rate B9's page drew is the API's after the crashes and the rate the safety
    monitor rolled back for, as the platform writes it."""
    wall = Wall()
    entries = [audit_entry("safety_rollback", at=wall.at - 2, reason=reason)]
    made = crashed_incident(entries, wall, counted=counted)
    with pytest.raises(director.CaptureFailed, match=f"B9: .*{words}"):
        made.wait(wait_item(), b9_director(wall.at - 30, checks))


@pytest.mark.regression
def test_the_wait_refuses_a_rollback_that_came_before_b9_ended():
    wall = Wall()
    entries = [audit_entry("safety_rollback", at=wall.at - 40, reason=ROLLBACK_REASON)]
    made = crashed_incident(entries, wall)
    with pytest.raises(
        director.CaptureFailed, match="B9: the safety monitor rolled back"
    ):
        made.wait(wait_item(), b9_director(wall.at - 30, [b9_check()]))


@pytest.mark.parametrize(
    "value", [40 / 240, 40 / 236, 40 / 300, 40 / 212, 1 / 14, 0.05, 0.2, 0.123456]
)
def test_the_rate_reads_as_the_rollback_reason_writes_it(value):
    """``gates.rate_reading`` is the platform's own ``format_metric_reading``
    for an error rate, and ``reason_rate`` reads it back out of the reason the
    safety scheduler writes (``describe_breach``)."""
    from backend.app.services.safety_service import (
        describe_breach,
        format_metric_reading,
    )

    assert gates.rate_reading(value) == format_metric_reading("error_rate", value)
    breach = SimpleNamespace(
        name="error_rate",
        current_value=value,
        threshold=0.05,
        details={"comparison_type": "greater_than"},
    )
    reason = f"Automatic rollback due to error_rate {describe_breach(breach)}"
    assert gates.reason_rate(reason) == gates.rate_reading(value)


def test_the_test_reason_is_the_one_the_scheduler_writes_for_the_crash():
    from backend.app.services.safety_service import describe_breach

    breach = SimpleNamespace(
        name="error_rate",
        current_value=CRASH_RATE,
        threshold=0.05,
        details={"comparison_type": "greater_than"},
    )
    assert ROLLBACK_REASON == (
        f"Automatic rollback due to error_rate {describe_breach(breach)}"
    )


def test_the_agreement_is_read_off_camera_at_its_beat_only():
    api = FakeApi(
        [("/api/v1/feature-flags/evaluate/", {"enabled": False, "reason": "rollout"})]
    )
    made, _ = an_incident(api=api)
    made.at_cue("classic", 1)
    assert made.agreement_api is None and api.calls == []
    made.at_cue("classic", 2)
    assert made.agreement_api == {"enabled": False, "reason": "rollout"}
    (_with_key, path, key) = api.calls[0]
    assert key == APP_KEY
    assert path.startswith(f"/api/v1/feature-flags/evaluate/{FLAG}?user_id={S10}")
    context = json.loads(parse_qs(path.split("?", 1)[1])["context"][0])
    assert context == config.STREAMPULSE_DEVICES[S10]


class Steps:
    """Stands in for the incident: the steps it was handed, in order."""

    def __init__(self):
        self.ran = []

    def step(self, item, director):
        self.ran.append((type(item).__name__, director.where))


def run_before(name, incident_like):
    made = SimpleNamespace(where="")
    run.before_scene(
        scene(name),
        users=None,
        warm=lambda name: pytest.fail("no results are warmed"),
        stack=None,
        director=made,
        redactor=None,
        read_keys=[],
        log=lambda line: None,
        incident=incident_like,
    )


def test_each_incident_step_runs_off_camera_before_its_scene():
    steps = Steps()
    for name in ("stages", "crashes", "later"):
        run_before(name, steps)
    assert steps.ran == [
        ("Story", "scene stages, off camera"),
        ("Crashes", "scene crashes, off camera"),
        ("WaitForRollback", "scene later, off camera"),
    ]


@pytest.mark.regression
def test_an_incident_step_with_no_incident_is_refused():
    """A step of the incident in a run that made no incident (its end state
    names no rollback) is refused, never skipped."""
    with pytest.raises(StackError, match="runs only in a video whose end state"):
        run_before("crashes", None)


def test_record_and_capture_video_wire_the_incident():
    """The wiring in ``run``: the incident made for a rollback end state, the
    Director given the flag lookup, the cue hook and A5's texts; each scene's
    off-camera steps through ``before_scene``; S5's card filled before the
    scene and checked after it; gate 10/4 on the API's log, read into memory
    and written only redacted, before gate 10's verdict; and the preflight's
    B9 fit and C6's read-back in ``capture_video``."""
    source = (CAPTURE / "run.py").read_text(encoding="utf-8")
    body = source.split("def record(", 1)[1].split("\ndef ", 1)[0]
    made = body.index("incidents.Incident(")
    assert "if board.end_state.rollback is not None" in body[made : made + 600]
    assert "keys=read_keys," in body[made : made + 600]
    assert "interval_s=incident_interval_s(board)," in body[made : made + 600]
    for wired in (
        "resolve_flag=lambda name: flag_id(stack.api_url, token, name),",
        "at_cue=incident.at_cue if incident is not None else None,",
        "required=required_texts(board, stack.apps),",
        "incident=incident,",
    ):
        assert wired in body, wired
    filled = body.index("shown = incident.with_minutes(shown, director)")
    filmed = body.index("director.scene(shown)")
    checked = body.index("incident.after_scene(scene, director)")
    assert filled < filmed < checked
    tenth = body[
        body.index("# Gate 10: the end state") : body.index('gates["10"] = gate(')
    ]
    for judged_here in (
        "text = stack.api_log()",
        'write_redacted(stack.logs / "api.log", text, redactor)',
        "ok, made, reason = incident.end_state(",
        "director.tally.rollback_posts,",
        "incidents.rollback_log_counts(text, end.rollback.key),",
    ):
        assert judged_here in tenth, judged_here
    for check in (
        'checks["A5"]',
        'checks["A6"]',
        'checks["S4"]',
        'checks["B9"]',
        'checks["U3"]',
    ):
        assert check in body, check
    capture = source.split("def capture_video(", 1)[1].split("\ndef ", 1)[0]
    assert "b9_fit = refuse_b9_fit(board, clips)" in capture
    read = capture.index(
        "read_back = interval_read_back(stack, board, override_lists.load())"
    )
    assert read < capture.index("setup = make_setup(")
    assert 'gates["C6"] = gate(True, **read_back)' in capture
    before = source.split("def before_scene(", 1)[1].split("\ndef ", 1)[0]
    assert "incident.step(item, director)" in before


# --- C6: the stated interval, read back from the API's own log -------------------------

API_LOG = "\n".join(
    [
        "api-1  | INFO  Starting safety monitoring scheduler",
        "api-1  | INFO  Safety scheduler started with 1 minute interval",
        f"api-1  | INFO  a line that names {SERVER_KEY} and must never be kept",
    ]
)


def test_c6_keeps_only_the_line_that_says_the_stated_interval():
    stack = SimpleNamespace(api_log=lambda: API_LOG)
    found = run.interval_read_back(stack, board(), override_lists.load())
    assert found == {
        "lines": {
            "SAFETY_CHECK_INTERVAL_MINUTES": (
                "api-1  | INFO  Safety scheduler started with 1 minute interval"
            )
        }
    }
    assert SERVER_KEY not in json.dumps(found)


@pytest.mark.regression
@pytest.mark.parametrize(
    "log, words",
    [
        # The override never reached the API: it runs every 5 minutes.
        (API_LOG.replace("with 1 minute", "with 5 minute"), "holds 0 line"),
        ("api-1  | INFO  nothing about the scheduler", "holds 0 line"),
        # Two starts: two APIs, or one restarted mid-take.
        (API_LOG + "\n" + API_LOG, "holds 2 line"),
    ],
)
def test_c6_refuses_a_log_that_does_not_say_the_stated_interval_once(log, words):
    stack = SimpleNamespace(api_log=lambda: log)
    with pytest.raises(director.CaptureFailed, match=f"C6: the API's log {words}"):
        run.interval_read_back(stack, board(), override_lists.load())


def test_c6_says_what_the_api_said_instead_and_nothing_else():
    log = API_LOG.replace("with 1 minute", "with 5 minute")
    stack = SimpleNamespace(api_log=lambda: log)
    with pytest.raises(director.CaptureFailed) as caught:
        run.interval_read_back(stack, board(), override_lists.load())
    assert "Safety scheduler started with 5 minute interval" in str(caught.value)
    assert SERVER_KEY not in str(caught.value)


def test_c6_is_nothing_for_a_video_that_states_no_setting():
    stack = SimpleNamespace(api_log=lambda: pytest.fail("the log was read"))
    v6 = storyboard.load(run.board_path("06-shoplab-ab-test"))
    assert run.interval_read_back(stack, v6, override_lists.load()) == {}


def test_the_api_log_is_read_into_memory_and_never_logged():
    """``docker compose logs api`` read as a private output: into this
    process, never into the run's log (PE 7)."""
    seen = {}

    class Docker:
        def out(self, name, argv, *, private=False, merged=False):
            seen.update(name=name, argv=argv, private=private, merged=merged)
            return API_LOG

    from showcase.capture import compose

    stack = SimpleNamespace(
        docker=Docker(), _compose=lambda *args: ["docker", "compose", "-p", "p", *args]
    )
    assert compose.QuickstartStack.api_log(stack) == API_LOG
    assert seen == {
        "name": "api-log",
        "argv": ["docker", "compose", "-p", "p", "logs", "--no-color", "api"],
        "private": True,
        "merged": True,
    }


@pytest.mark.regression
def test_a_merged_read_keeps_both_streams_in_memory_and_logs_neither(tmp_path):
    """``docker compose logs`` may write what the container wrote to its
    standard error to its own: a merged read returns it with the output, and
    the child's log holds the command only (PE 7)."""
    import sys

    work = guard.WorkRoot(tmp_path / "work")
    work.path.mkdir(parents=True)
    parent = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path)}
    children = Children(work, parent)
    log = work.path / "api-log.log"
    # A stand-in for compose: what it prints is in a file, so not on its argv.
    script = tmp_path / "compose_logs.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write('api-1  | uvicorn running\\n')\n"
        "sys.stderr.write('api-1  | Safety scheduler started with 1 minute interval"
        f" {SERVER_KEY}\\n')\n",
        encoding="utf-8",
    )
    status, text = children.capture(
        "api-log",
        [sys.executable, str(script)],
        cwd=work.path,
        env=guard.child_env({}, parent=parent, docker=True),
        log=log,
        kind="compose",
        private=True,
        merged=True,
    )
    assert status == 0
    assert "uvicorn running" in text
    assert "Safety scheduler started with 1 minute interval" in text
    logged = log.read_text(encoding="utf-8")
    assert SERVER_KEY not in logged and "Safety scheduler" not in logged
    assert "held in memory" in logged
    # Without merged, the standard error is the log's (the key file's read
    # keeps its error message there): the reason the API's log is merged.
    status, text = children.capture(
        "api-log-2",
        [sys.executable, str(script)],
        cwd=work.path,
        env=guard.child_env({}, parent=parent, docker=True),
        log=work.path / "unmerged.log",
        kind="compose",
        private=True,
    )
    assert SERVER_KEY not in text
    assert SERVER_KEY in (work.path / "unmerged.log").read_text(encoding="utf-8")


@pytest.mark.regression
def test_the_api_log_is_written_only_redacted_with_the_server_key_registered(tmp_path):
    """PE 7: anything of the API's log the run writes goes through the
    redactor, the server key registered first; and the static needles."""
    redactor = director.redaction.Redactor()
    redactor.add(SERVER_KEY)
    text = API_LOG + f"\napi-1  | {config.STATIC_NEEDLES[0]} {APP_KEY}"
    run.write_redacted(tmp_path / "logs" / "api.log", text, redactor)
    written = (tmp_path / "logs" / "api.log").read_text(encoding="utf-8")
    assert SERVER_KEY not in written and APP_KEY not in written
    assert config.STATIC_NEEDLES[0] not in written
    assert "Safety scheduler started with 1 minute interval" in written


def test_the_rollback_lines_are_counted_by_kind():
    text = "\n".join(
        [
            "INFO Triggering automatic rollback for feature flag streampulse_player_v2 (x)",
            "INFO Rolled back feature flag x from 25% to 5% (turned off: False,"
            " schedules paused: 1) due to y (trigger: automatic)",
            "INFO Successfully rolled back feature flag streampulse_player_v2: done",
        ]
    )
    assert incident.rollback_log_counts(text, FLAG) == {
        "rolled_back": 1,
        "automatic": 1,
        "manual": 0,
    }
    source = (REPO / "backend" / "app" / "services" / "safety_service.py").read_text(
        encoding="utf-8"
    )
    assert "(trigger: {trigger_name})" in source
    scheduler = (REPO / "backend" / "app" / "core" / "safety_scheduler.py").read_text(
        encoding="utf-8"
    )
    assert 'f"Successfully rolled back feature flag {feature_flag.key}:' in scheduler


# --- Gate 10/4: the rollback, every reading agreeing (S8a) ------------------------------

ME = {"email": "admin@demo.com", "id": "u-admin"}
AUDIT_SHOWN = "\n".join(
    [
        "Oct 20, 10:03\tSafety monitor (automatic)\tSafety rollback\tFeature flag"
        "\tStreamPulse: Player v2",
        "Oct 20, 10:01\tadmin@demo.com\tFlag changed\tFeature flag\tStreamPulse: Player v2",
    ]
)
DETAILS = (
    f"User Safety monitor (automatic) Action Safety rollback Reason {ROLLBACK_REASON}"
)


def judged(drawn=None, **changes):
    """Gate 10/4 on the readings a good take makes; *drawn* changes what the
    page drew (by reading name), *changes* what the API and the log said."""
    end = board().end_state.rollback
    made = {
        "named": [flag_item(rollout_percentage=5)],
        "schedules": [{"status": "paused"}],
        "audit": [
            audit_entry(
                "safety_rollback",
                reason=ROLLBACK_REASON,
                trigger_type="automatic",
                previous_percentage=25,
                new_percentage=5,
            ),
            audit_entry("feature_flag_update", by="admin@demo.com", user_id="u-admin"),
            audit_entry("feature_flag_update", by="admin@demo.com", user_id="u-admin"),
        ],
        "actor": ME,
        "kill_named": [
            {
                "key": "streampulse_recs_v2",
                "name": "StreamPulse: Recommendations v2",
                "status": "inactive",
            }
        ],
        "kill_audit": [
            audit_entry("toggle_disable", by="admin@demo.com", user_id="u-admin")
        ],
        "readings": {
            "unhealthy": "Unhealthy",
            "paused": "Paused",
            "rolled_back": "5%",
            "audit": AUDIT_SHOWN,
            "reason": DETAILS,
            "panel": "off: outside the rollout %",
            "phone": "Player\nClassic player\nstreampulse_player_v2",
        },
        "api_at_agreement": {"enabled": False, "reason": "rollout"},
        "rollback_posts": [],
        "log": {"rolled_back": 1, "automatic": 1, "manual": 0},
    }
    made.update(changes)
    made["readings"] = {**made["readings"], **(drawn or {})}
    return gates.rollback_end(end, **made)


def test_gate_10_4_passes_the_monitors_rollback_read_every_way():
    ok, numbers, why = judged()
    assert ok, why
    assert numbers["audit"] == [
        "feature_flag_update",
        "feature_flag_update",
        "safety_rollback",
    ]
    assert numbers["rollback"]["by"] == "system:safety-monitor"
    assert numbers["agreement"]["said"] == {
        "panel": False,
        "phone": False,
        "api": False,
    }


@pytest.mark.regression
@pytest.mark.parametrize(
    "changes, words",
    [
        ({"named": [flag_item(rollout_percentage=25)]}, "the flag is at 25%, not 5%"),
        (
            {"named": [flag_item(rollout_percentage=5, status="inactive")]},
            "not 'active'",
        ),
        ({"schedules": [{"status": "active"}]}, "not one paused"),
        ({"schedules": []}, "not one paused"),
        (
            {
                "audit": [
                    audit_entry(
                        "safety_rollback",
                        reason=ROLLBACK_REASON,
                        trigger_type="automatic",
                        new_percentage=5,
                    )
                ]
            },
            "the flag's audit entries are",
        ),
        # QA C1: the rollback by hand, by the signed-in admin.
        (
            {
                "audit": [
                    audit_entry(
                        "safety_rollback",
                        by="admin@demo.com",
                        user_id="u-admin",
                        reason=ROLLBACK_REASON,
                        trigger_type="manual",
                        new_percentage=5,
                    ),
                    audit_entry(
                        "feature_flag_update", by="admin@demo.com", user_id="u-admin"
                    ),
                    audit_entry(
                        "feature_flag_update", by="admin@demo.com", user_id="u-admin"
                    ),
                ]
            },
            "the rollback was made by 'admin@demo.com'",
        ),
        (
            {
                "audit": [
                    audit_entry(
                        "safety_rollback",
                        reason=ROLLBACK_REASON,
                        trigger_type="manual",
                        new_percentage=5,
                    ),
                    audit_entry(
                        "feature_flag_update", by="admin@demo.com", user_id="u-admin"
                    ),
                    audit_entry(
                        "feature_flag_update", by="admin@demo.com", user_id="u-admin"
                    ),
                ]
            },
            "not automatic to 5%",
        ),
        (
            {
                "audit": [
                    audit_entry(
                        "safety_rollback",
                        reason="Automatic rollback due to latency_p95",
                        trigger_type="automatic",
                        new_percentage=5,
                    ),
                    audit_entry(
                        "feature_flag_update", by="admin@demo.com", user_id="u-admin"
                    ),
                    audit_entry(
                        "feature_flag_update", by="admin@demo.com", user_id="u-admin"
                    ),
                ]
            },
            "not an error-rate breach",
        ),
        (
            {
                "audit": [
                    audit_entry(
                        "safety_rollback",
                        reason=ROLLBACK_REASON,
                        trigger_type="automatic",
                        new_percentage=5,
                    ),
                    audit_entry(
                        "feature_flag_update", by="someone@example.com", user_id="u-2"
                    ),
                    audit_entry(
                        "feature_flag_update", by="admin@demo.com", user_id="u-admin"
                    ),
                ]
            },
            "not every change is by the signed-in",
        ),
        ({"log": {"rolled_back": 2, "automatic": 2, "manual": 0}}, "2 rollback"),
        ({"log": {"rolled_back": 1, "automatic": 1, "manual": 1}}, "1 manual"),
        # Zero POSTs to .../rollback.
        (
            {
                "rollback_posts": [
                    "/api/v1/safety/feature-flags/f/rollback (scene unhealthy beat 1)"
                ]
            },
            "a page asked for a rollback",
        ),
        ({"drawn": {"unhealthy": "Healthy"}}, "unhealthy read 'Healthy'"),
        ({"drawn": {"paused": "Active"}}, "paused read 'Active'"),
        ({"drawn": {"rolled_back": "25%"}}, "rolled_back read '25%'"),
        (
            {"drawn": {"audit": AUDIT_SHOWN.split("\n", 1)[1]}},
            "the audit log's top row showed",
        ),
        ({"drawn": {"reason": "User Safety monitor"}}, "did not show the API's reason"),
        # Plan v2 C1's tamper: Refresh skipped, the stale Player v2 still drawn.
        (
            {
                "drawn": {
                    "phone": "Now playing\nPlayer v2\nstreampulse_player_v2",
                    "panel": "on: inside the rollout %",
                }
            },
            "panel/phone/API disagree at cue classic beat 2",
        ),
        ({"drawn": {"phone": "Now playing\nPlayer v2"}}, "panel/phone/API disagree"),
        # A phone that draws neither player (still loading) says nothing.
        ({"drawn": {"phone": "Player"}}, "panel/phone/API disagree"),
        # The panel alone still on: it was not refreshed.
        ({"drawn": {"panel": "on: inside the rollout %"}}, "panel/phone/API disagree"),
        (
            {"api_at_agreement": {"enabled": True, "reason": "rollout"}},
            "panel/phone/API disagree",
        ),
        ({"api_at_agreement": None}, "panel/phone/API disagree"),
        ({"drawn": {"panel": "evaluating…"}}, "panel/phone/API disagree"),
        (
            {
                "kill_named": [
                    {
                        "key": "streampulse_recs_v2",
                        "name": "StreamPulse: Recommendations v2",
                        "status": "active",
                    }
                ]
            },
            "the kill switch 'StreamPulse: Recommendations v2' is not off",
        ),
        ({"kill_audit": []}, "the kill switch's audit entries are []"),
        (
            {"kill_audit": [audit_entry("toggle_disable", by="system:safety-monitor")]},
            "the kill switch was changed by 'system:safety-monitor'",
        ),
    ],
)
def test_gate_10_4_refuses_each_reading_that_disagrees(changes, words):
    ok, numbers, why = judged(**changes)
    assert not ok and words in why, why


@pytest.mark.regression
def test_the_monitors_audit_row_is_read_as_the_table_draws_it():
    """The audit table's User cell is ``actorLabel(log)``: for the safety
    monitor's rollback (it has no user id) the monitor's name, then
    "(automatic)". Gate 10/4 once wanted the bare name, which no page draws,
    so every take would have refused at the audit's top row; the stand-in
    table carried the bare name too. Here the row is built from the page's
    own code."""
    audit = REPO / "frontend" / "src" / "components" / "admin" / "audit"
    labels = (audit / "actionLabels.ts").read_text(encoding="utf-8")
    name = re.search(r"'system:safety-monitor': '([^']+)'", labels).group(1)
    assert "return `${name} (automatic)`;" in labels
    table = (audit / "AuditLogTable.tsx").read_text(encoding="utf-8")
    assert "{actorLabel(log)}" in table
    drawn = (
        f"Oct 20, 10:03\t{name} (automatic)\tSafety rollback\tFeature flag"
        "\tStreamPulse: Player v2"
    )
    ok, numbers, why = judged(drawn={"audit": drawn})
    assert ok, why
    assert numbers["top_row"][0] == f"{name} (automatic)"


def test_gate_10_4_reads_the_dashboards_and_the_apps_own_words():
    """The labels gate 10/4 compares are the product's, at the recorded release."""
    labels = (
        REPO / "frontend" / "src" / "components" / "admin" / "audit" / "actionLabels.ts"
    ).read_text(encoding="utf-8")
    assert "safety_rollback: { label: 'Safety rollback'" in labels
    assert "'system:safety-monitor': 'Safety monitor'" in labels
    # The User cell of an entry with no user: the actor's name, "(automatic)".
    assert "return `${name} (automatic)`;" in labels
    page = (
        REPO / "frontend" / "src" / "pages" / "feature-flags" / "[id].tsx"
    ).read_text(encoding="utf-8")
    assert "{safety.is_healthy ? 'Healthy' : 'Unhealthy'}" in page
    assert "{humanize(schedule.status)}" in page
    assert gates.PAUSED_LABEL == "Paused" and gates.UNHEALTHY_LABEL == "Unhealthy"
    sp_labels = (STREAMPULSE / "src" / "lib" / "labels.ts").read_text(encoding="utf-8")
    assert "'on: inside the rollout %' : 'off: outside the rollout %'" in sp_labels
    player = (
        STREAMPULSE / "src" / "components" / "screens" / "PlayerScreen.tsx"
    ).read_text(encoding="utf-8")
    assert 'title="Now playing" subtitle="Player v2"' in player
    assert 'title="Player" subtitle="Classic player"' in player
    scheduler = (REPO / "backend" / "app" / "core" / "safety_scheduler.py").read_text(
        encoding="utf-8"
    )
    assert 'f"Automatic rollback due to {failing.name} "' in scheduler


# --- The director: A5, A6, the counted requests, flag pages, wall times -----------------


class Clock:
    ms = 0.0


@pytest.fixture
def stand_in_clock(monkeypatch):
    Clock.ms = 1_800_000_000_000.0
    monkeypatch.setattr(director, "_now_ms", lambda: Clock.ms)


class Request:
    def __init__(self, url, method="GET", key=APP_KEY):
        self.url = url
        self.method = method
        self.headers = {"x-api-key": key} if key else {}
        self.failure = None
        self.post_data_json = None


class Response:
    def __init__(self, url, status, request=None, body=None):
        self.url = url
        self.status = status
        self.request = request
        self._body = body
        self.headers = {
            "content-type": "application/json" if body is not None else "text/html"
        }

    def json(self):
        return self._body


class Context:
    def __init__(self):
        self.handlers = {}

    def route(self, *args):
        pass

    def route_web_socket(self, *args):
        pass

    def add_init_script(self, script):
        pass

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)


class Page:
    def __init__(self):
        self.handlers = {}
        self.visits = []
        self.text = ""
        self.url = "about:blank"

    def goto(self, url):
        self.url = url
        self.visits.append(url)

    def wait_for_timeout(self, ms):
        Clock.ms += ms

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def fire(self, event, value):
        for handler in self.handlers.get(event, []):
            handler(value)

    def evaluate(self, script, arg=None):
        if script == director.VIEWPORT_TEXT_JS:
            return self.text
        raise AssertionError(f"an unexpected page script: {str(script)[:40]}")


def a_director(tmp_path, **kw):
    page = Page()
    made = director.Director(
        context=Context(),
        page=page,
        frames=tmp_path / "work" / "v4" / "capture" / "frames",
        dashboard=DASH,
        api_port=28400,
        dashboard_port=28401,
        redactor=director.redaction.Redactor(),
        forbidden=run.forbidden_text(board(), PORTS),
        work=guard.WorkRoot(tmp_path / "work"),
        apps={"streampulse": 28404},
        app_ready=lambda app: {"experiments": 5, "feature_flags": 4},
        **kw,
    )
    made.install()
    made.check_needles = lambda: None
    made.measure_zoom = lambda wanted=None: {"body": wanted}
    made.self_test_overlay = lambda spec: {"offset_px": 0}
    made.recorder.start = lambda: None
    made.recorder.stop = lambda: None
    made.draw_cursor = lambda: None
    made.ring = lambda box=None: None
    return made, page


#: StreamPulse's phone in the frame (390 x 780 CSS px, 93 CSS px from the top)
#: at the origin's zoom, and at the dashboard's 4/3: arithmetic, the Mac
#: measures the real one (T178 9(e), NOT VERIFIED 5).
def phone_at(zoom: float) -> director.Box:
    return director.Box(800.0, 93 * zoom, 390 * zoom, 780 * zoom)


def test_a6_holds_the_phone_whole_at_the_streampulse_zoom(tmp_path, stand_in_clock):
    made, _ = a_director(tmp_path)
    made.fit_box = lambda testid: phone_at(config.ORIGIN_ZOOM["streampulse"])
    made.check_fit("streampulse", "cue start")
    assert made.tally.fit_checks == 1
    assert 0 <= made.tally.fit_least_px < 10
    # Other origins have no fit element: nothing is checked there.
    made.check_fit("dashboard", "cue start")
    assert made.tally.fit_checks == 1


@pytest.mark.regression
def test_a6_refuses_the_phone_cut_off_at_the_dashboards_zoom(tmp_path, stand_in_clock):
    """The UX draft's A6 plant: zoom 4/3 with no scroll cuts the tab bar off."""
    made, _ = a_director(tmp_path)
    made.fit_box = lambda testid: phone_at(4 / 3)
    with pytest.raises(director.CaptureFailed, match="A6: .*phone is cut off"):
        made.check_fit("streampulse", "cue start")


@pytest.mark.regression
def test_a6_refuses_at_arming_before_any_scene(tmp_path, stand_in_clock):
    made, page = a_director(tmp_path)
    made.fit_box = lambda testid: phone_at(4 / 3)
    with pytest.raises(
        director.CaptureFailed, match=r"A6: streampulse first load \(first load\)"
    ):
        made.arm("streampulse")


@pytest.mark.regression
def test_a6_refuses_a_ring_drawn_around_something_cut_off(tmp_path, stand_in_clock):
    made, _ = a_director(tmp_path)
    made.fit_box = lambda testid: phone_at(config.ORIGIN_ZOOM["streampulse"])
    with pytest.raises(
        director.CaptureFailed, match="A6: .*the ringed element is cut off"
    ):
        made.check_fit("streampulse", "a ring", director.Box(1500, 900, 200, 80))


@pytest.mark.regression
def test_a6_refuses_a_frame_with_no_phone(tmp_path, stand_in_clock):
    made, _ = a_director(tmp_path)
    made.fit_box = lambda testid: None
    with pytest.raises(director.CaptureFailed, match="A6: .*draws no phone"):
        made.check_fit("streampulse", "cue end")


def test_a6_reads_the_phone_by_its_testid_as_phoneframe_draws_it():
    frame = (STREAMPULSE / "src" / "components" / "PhoneFrame.tsx").read_text(
        encoding="utf-8"
    )
    assert 'data-testid="phone"' in frame
    assert config.APP_FIT == {"streampulse": "phone"}
    css = (STREAMPULSE / "src" / "styles" / "globals.css").read_text(encoding="utf-8")
    assert "width: 390px;\n  height: 780px;" in css
    assert config.ORIGIN_ZOOM["streampulse"] == 1.07


def test_a5_the_panel_says_when_it_fetches_on_each_scenes_first_frame(
    tmp_path, stand_in_clock
):
    text, relative = config.APP_REQUIRED_TEXT["streampulse"]
    made, page = a_director(tmp_path, required={"streampulse": text})
    made.where = "scene devices beat 1"
    made.start_text = f"Decisions for sp-iphone-15-us-premium, {text}."
    made.check_required("streampulse")
    assert made.tally.required_checks == ["scene devices beat 1"]
    panel = (REPO / relative).read_text(encoding="utf-8")
    assert text in panel
    # Plan v2 C1's forbidden half is on video 4's never_show.
    assert {"every 5 s", "Within 5 s"} <= set(board().never_show)


@pytest.mark.regression
def test_a5_refuses_a_first_frame_without_the_panels_words(tmp_path, stand_in_clock):
    text, _ = config.APP_REQUIRED_TEXT["streampulse"]
    made, _ = a_director(tmp_path, required={"streampulse": text})
    made.where = "scene feed beat 1"
    made.start_text = "Decisions for sp-iphone-15-us-premium, re-fetched every 5 s."
    with pytest.raises(
        director.CaptureFailed,
        match="A5: scene feed beat 1: the streampulse frame does not show",
    ):
        made.check_required("streampulse")


@pytest.mark.regression
def test_a5_refuses_a_recorded_tree_whose_panel_does_not_say_it(tmp_path):
    tree = tmp_path / "app-streampulse"
    panel = tree / config.APP_REQUIRED_TEXT["streampulse"][1]
    panel.parent.mkdir(parents=True)
    panel.write_text("Decisions for x, re-fetched every 5 s.", encoding="utf-8")
    with pytest.raises(StackError, match="A5: .* does not say"):
        run.required_texts(board(), {"streampulse": tree})
    panel.write_text(
        f"Decisions for x, {config.APP_REQUIRED_TEXT['streampulse'][0]}.",
        encoding="utf-8",
    )
    assert run.required_texts(board(), {"streampulse": tree}) == {
        "streampulse": config.APP_REQUIRED_TEXT["streampulse"][0]
    }


@pytest.mark.regression
@pytest.mark.parametrize("method", ["POST", "GET", "PUT"])
def test_every_request_that_asks_for_a_rollback_is_counted(
    tmp_path, stand_in_clock, method
):
    """Gate 10/4's zero POSTs to .../rollback: the listener counts every
    request to a flag's rollback path, by any method, from any page."""
    made, page = a_director(tmp_path)
    made.where = "scene unhealthy beat 1"
    page.fire(
        "request",
        Request(f"{DASH}/api/v1/safety/feature-flags/f-1/rollback", method, key=None),
    )
    page.fire(
        "request", Request(f"{DASH}/api/v1/safety/feature-flags/f-1/check", key=None)
    )
    assert made.tally.rollback_posts == [
        "/api/v1/safety/feature-flags/f-1/rollback (scene unhealthy beat 1)"
    ]


def test_each_safety_check_the_page_gets_is_kept_with_where(tmp_path, stand_in_clock):
    made, page = a_director(tmp_path)
    made.where = "scene unhealthy"
    url = f"{DASH}/api/v1/safety/feature-flags/f-1/check"
    request = Request(url, key=None)
    page.fire("request", request)
    page.fire("response", Response(url, 200, request, check_answer(240)))
    # The answer's error_rate metric: the rate the page draws, kept for B9.
    assert made.tally.safety_checks == [
        {"where": "scene unhealthy", "is_healthy": False, "error_rate": CRASH_RATE}
    ]
    page.fire("response", Response(url, 200, request, check_answer(240, metrics=[])))
    assert made.tally.safety_checks[-1]["error_rate"] is None


def test_a_flag_page_scene_opens_the_flag_by_its_id_and_times_its_recording(
    tmp_path, stand_in_clock
):
    made, page = a_director(tmp_path, resolve_flag=lambda name: f"id-of-{name}")
    made.beat = lambda sc, index, beat: page.wait_for_timeout(4000)
    made.scene(scene("unhealthy"))
    assert page.visits == [f"{DASH}/feature-flags/id-of-StreamPulse: Player v2"]
    walls = made.scene_walls["unhealthy"]
    assert walls["end"] - walls["start"] >= 4000


def test_the_cue_hook_runs_at_each_cue_start_after_the_readings():
    """The beat's order: A5 at a scene's first cue, A6, the readings, then the
    run's hook (gate 10/4's agreement reads the API there), then the steps."""
    source = (CAPTURE / "director.py").read_text(encoding="utf-8")
    beat = source[source.index("    def beat(self, scene: Scene") :]
    beat = beat[: beat.index("    def card(self")]
    readings = beat.index("self.readings[name] = self.locate(spec).inner_text()")
    hook = beat.index(
        "if self.at_cue is not None:\n            self.at_cue(scene.id, index)\n"
    )
    required = beat.index(
        "if index == 1 and scene.app in self.required:\n"
        "            self.check_required(scene.app)\n"
    )
    fit = beat.index('        self.check_fit(scene.app, "cue start")\n')
    steps = beat.index("for step in beat.steps:")
    assert required < fit < readings < hook < steps
    assert beat.index('        self.check_fit(scene.app, "cue end")\n') > steps


def a_filming_director(tmp_path, fit):
    """The director mid-scene in StreamPulse, recording, its phone at *fit*."""
    made, page = a_director(tmp_path)
    made.where = "scene player beat 1"
    made.filming = "streampulse"
    made.recorder.t0 = Clock.ms
    made.fit_box = lambda testid: fit
    return made, page


@pytest.mark.regression
def test_a6_refuses_a_ring_cut_off_while_filming(tmp_path, stand_in_clock):
    made, _ = a_filming_director(tmp_path, phone_at(config.ORIGIN_ZOOM["streampulse"]))
    made._visible_box = lambda spec: (None, director.Box(1500, 900, 200, 80))
    look = storyboard.Look(look=storyboard.Locator(testid="panel-flag-x"), ms=1000)
    with pytest.raises(
        director.CaptureFailed,
        match=r"A6: scene player beat 1 \(a ring\): the ringed element is cut off",
    ):
        made.step(look)


@pytest.mark.regression
def test_a6_refuses_a_step_that_leaves_the_phone_cut_off(tmp_path, stand_in_clock):
    made, _ = a_filming_director(tmp_path, phone_at(4 / 3))
    made.locate = lambda spec: object()
    made.scroll_into_view = lambda found: None
    scroll = storyboard.ScrollTo(scroll_to=storyboard.Locator(testid="panel-flag-x"))
    with pytest.raises(
        director.CaptureFailed,
        match=r"A6: scene player beat 1 \(after a step\): phone is cut off",
    ):
        made.step(scroll)


def test_a6_is_not_checked_on_a_step_in_a_cut(tmp_path, stand_in_clock):
    """A step in ``before`` runs with the recording stopped: nothing is filmed."""
    made, _ = a_filming_director(tmp_path, phone_at(4 / 3))
    made.recorder.t0 = None
    made.locate = lambda spec: object()
    made.scroll_into_view = lambda found: None
    made.step(storyboard.ScrollTo(scroll_to=storyboard.Locator(testid="panel-flag-x")))
    assert made.tally.fit_checks == 0


# --- Condition 6 for StreamPulse (T178): the ref's lockfile and installed versions -------


def streampulse_tree(tmp_path: Path, lock: dict) -> Path:
    tree = tmp_path / "app-streampulse"
    (tree / "demo" / "streampulse").mkdir(parents=True)
    (tree / "demo" / "streampulse" / "package-lock.json").write_text(json.dumps(lock))
    return tree


def checkout(tmp_path: Path, lock: dict, installed: Dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    app = repo / "demo" / "streampulse"
    app.mkdir(parents=True)
    (app / "package-lock.json").write_text(json.dumps(lock))
    for name, version in installed.items():
        (app / "node_modules" / name).mkdir(parents=True)
        (app / "node_modules" / name / "package.json").write_text(
            json.dumps({"name": name, "version": version})
        )
    return repo


LOCK = {
    "packages": {
        "node_modules/next": {"version": "16.4.0"},
        "node_modules/react": {"version": "19.3.0"},
        "node_modules/react-dom": {"version": "19.3.0"},
    }
}
INSTALLED = {"next": "16.4.0", "react": "19.3.0", "react-dom": "19.3.0"}


def test_streampulses_dependencies_agree_with_the_refs_lockfile(tmp_path):
    repo = checkout(tmp_path, LOCK, INSTALLED)
    found = build.app_dependencies(
        repo, streampulse_tree(tmp_path, LOCK), "streampulse", "v0.36.0"
    )
    assert found["lockfile_is_the_refs"] is True
    assert {k: v["installed"] for k, v in found["packages"].items()} == INSTALLED


@pytest.mark.regression
def test_a_streampulse_lockfile_one_version_off_is_refused(tmp_path):
    moved = json.loads(json.dumps(LOCK))
    moved["packages"]["node_modules/next"]["version"] = "16.4.1"
    repo = checkout(tmp_path, moved, INSTALLED)
    with pytest.raises(
        guard.Refused,
        match="demo/streampulse/package-lock.json in the checkout is not v0.36.0's",
    ):
        build.app_dependencies(
            repo, streampulse_tree(tmp_path, LOCK), "streampulse", "v0.36.0"
        )


@pytest.mark.regression
@pytest.mark.parametrize("name", ["next", "react", "react-dom"])
def test_an_installed_streampulse_package_one_version_off_is_refused(tmp_path, name):
    installed = dict(INSTALLED)
    installed[name] = "0.0.1"
    repo = checkout(tmp_path, LOCK, installed)
    with pytest.raises(
        guard.Refused, match=f"demo/streampulse has {name} 0.0.1 installed"
    ):
        build.app_dependencies(
            repo, streampulse_tree(tmp_path, LOCK), "streampulse", "v0.36.0"
        )


def test_the_real_streampulse_lockfile_pins_the_three_packages():
    lock = json.loads((STREAMPULSE / "package-lock.json").read_text(encoding="utf-8"))
    for name in build.APP_PINNED_PACKAGES:
        assert lock["packages"][f"node_modules/{name}"]["version"], name


# --- The server key: a needle as read, never with its newline (T165 C13) -----------------


@pytest.mark.regression
def test_the_server_key_kept_with_its_newline_is_refused_by_gate_6():
    """T165 C13's evidence: a key read with its "\\n" kept is not the value an
    X-API-Key header carries, so gate 6 must be looking for it as read."""
    ok, _numbers, _why = gates.needle_count(
        config.STATIC_NEEDLES, [SERVER_KEY], [SERVER_KEY]
    )
    assert ok
    ok, _numbers, why = gates.needle_count(
        config.STATIC_NEEDLES, [SERVER_KEY + "\n"], [SERVER_KEY + "\n"]
    )
    assert not ok and why == "a key read from a file was kept with whitespace"
