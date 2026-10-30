"""A console scene's API calls, refused at load (#1442, video 9's API part).

A ``console`` scene shows calls the tool makes to the take's own stack, each
request and the API's own answer drawn on a page the tool makes. What a call
may be is decided when the storyboard loads, before any stack starts: one of
a closed list, sent by exactly one ``send``, its ids named only by
placeholders, and every key of the design end state's ``api`` set by exactly
one call. Each refusal is shown firing on one planted change to video 9's
own board. No browser, Docker, network or database.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest
import yaml
from showcase.capture import config, console, storyboard, voiced

pytestmark = [pytest.mark.unit]

SHOWCASE = Path(__file__).resolve().parents[4] / "tests" / "acceptance" / "showcase"
V9 = SHOWCASE / "storyboards" / "09-design-in-depth.yaml"
SOURCE = "09-design-in-depth.yaml"


def raw() -> Dict[str, Any]:
    return yaml.safe_load(V9.read_text(encoding="utf-8"))


def api_scene(data: Dict[str, Any]) -> Dict[str, Any]:
    return next(scene for scene in data["scenes"] if scene["id"] == "api")


def api_beat(data: Dict[str, Any], number: int) -> Dict[str, Any]:
    """The API scene's beat *number*, counted from 1: B C D E1 E2 R1 R2."""
    return api_scene(data)["beats"][number - 1]


def board(data: Dict[str, Any]) -> storyboard.Storyboard:
    return storyboard.parse(copy.deepcopy(data), source=SOURCE)


def refused(change: Callable[[Dict[str, Any]], Any], reason: str) -> str:
    data = raw()
    change(data)
    with pytest.raises(storyboard.StoryboardError) as raised:
        board(data)
    assert reason in str(raised.value), str(raised.value)
    return str(raised.value)


B, C, D, E1, E2, R1, R2 = range(1, 8)


# ---------------------------------------------------------------------------
# The real board
# ---------------------------------------------------------------------------


def test_the_real_boards_calls_are_six_of_the_closed_list_in_order():
    made = storyboard.load(V9)
    scene = next(s for s in made.scenes if s.id == "api")
    assert scene.console is not None and scene.card is None and scene.open is None
    calls = [
        (beat.call.method, beat.call.path, beat.call.answer.status)
        if beat.call is not None
        else None
        for beat in scene.beats
    ]
    assert calls == [
        ("POST", "/api/v1/mutual-exclusion-groups/{group}/experiments", 200),
        ("POST", "/api/v1/holdout", 201),
        ("PUT", "/api/v1/experiments/{experiment}/schedule", 200),
        ("PUT", "/api/v1/experiments/{experiment}", 200),
        None,
        ("GET", "/api/v1/experiments/{experiment}", 200),
        ("GET", "/api/v1/holdout", 200),
    ]
    sets = [
        storyboard.API_CALLS[(beat.call.method, beat.call.path)].sets
        for beat in scene.beats
        if beat.call is not None
    ]
    assert sets == [
        "exclusion_group",
        "holdout",
        "scheduled_start",
        "sequential",
        None,
        None,
    ]
    assert made.end_state.design.api == [
        "exclusion_group",
        "holdout",
        "scheduled_start",
        "sequential",
    ]
    sends = [
        [
            type(step).__name__
            for step in beat.steps
            if not isinstance(step, storyboard.Look)
        ]
        for beat in scene.beats
    ]
    assert sends == [["Send"]] * 4 + [[]] + [["Send"]] * 2
    # Each look rings a field its beat's exchange shows, after the send.
    for number, beat in enumerate(scene.beats):
        exchange = beat.call or scene.beats[number - 1].call
        for step in beat.steps:
            if isinstance(step, storyboard.Look):
                field = console.FIELD_SELECTOR.match(step.look.css or "")
                assert field and field.group(1) in exchange.show, step.look.css
        assert all(
            not isinstance(step, storyboard.Look)
            for step in beat.steps[: 1 if beat.call else 0]
        )


def test_the_closed_list_is_exactly_the_plans():
    assert {key: kind.status for key, kind in storyboard.API_CALLS.items()} == {
        ("POST", "/api/v1/mutual-exclusion-groups/{group}/experiments"): 200,
        ("POST", "/api/v1/holdout"): 201,
        ("PUT", "/api/v1/experiments/{experiment}/schedule"): 200,
        ("PUT", "/api/v1/experiments/{experiment}"): 200,
        ("GET", "/api/v1/experiments/{experiment}"): 200,
        ("GET", "/api/v1/holdout"): 200,
    }
    assert storyboard.FIXED_PLACEHOLDERS == ("experiment", "group", "start")


def test_the_console_states_its_off_camera_setup_and_draws_its_label_and_notes():
    made = storyboard.load(V9)
    scene = next(s for s in made.scenes if s.id == "api")
    assert ("api", "off-camera setup") in storyboard.required_departures(made)
    shown = storyboard.scene_screen_text(scene)
    notes = [beat.note for beat in scene.beats if beat.note]
    assert len(notes) == 3
    assert shown == [beat.cue for beat in scene.beats] + [
        "Signed in to the API off camera",
        *notes,
    ]
    data = raw()
    api_scene(data)["departures"] = []
    assert storyboard.departure_problems(board(data)) == [
        "scene api: off-camera setup is not stated on screen (departures)"
    ]
    data = raw()
    api_scene(data)["console"]["label"] = "The API, on camera"
    assert storyboard.departure_problems(board(data)) == [
        "scene api: the off-camera setup statement 'Signed in to the API off"
        " camera' is not in a caption or the card"
    ]


# ---------------------------------------------------------------------------
# Where calls, notes and sends may be
# ---------------------------------------------------------------------------


def test_a_call_or_a_note_outside_a_console_is_refused():
    def call_on_the_wizard(data):
        data["scenes"][0]["beats"][0]["call"] = api_beat(data, B)["call"]

    refused(call_on_the_wizard, "a call or a note is drawn only on a console scene")

    def note_on_the_wizard(data):
        data["scenes"][0]["beats"][0]["note"] = "Drawn by the tool."

    refused(note_on_the_wizard, "a call or a note is drawn only on a console scene")


@pytest.mark.parametrize("part", ["before", "exit"])
def test_a_send_outside_the_beats_steps_is_refused(part):
    refused(
        lambda d: api_beat(d, C).update({part: [{"send": True}]}),
        f"a send is made in the beat's steps, never in {part}",
    )


def test_a_send_with_no_call_is_refused():
    refused(
        lambda d: api_beat(d, E2).update(steps=[{"send": True}]),
        "scene api beat 5: a send with no call to make",
    )


@pytest.mark.parametrize("steps, count", [([], 0), ([{"send": True}] * 2, 2)])
def test_a_call_without_exactly_one_send_is_refused(steps, count):
    refused(
        lambda d: api_beat(d, D).update(steps=steps),
        f"scene api beat 3: a call is made by exactly one send in its steps, not {count}",
    )


def test_a_send_is_true_or_nothing():
    refused(lambda d: api_beat(d, D).update(steps=[{"send": False}]), "send")


def test_a_console_beat_without_a_call_follows_one_that_makes_one():
    def first_without(data):
        beat = api_beat(data, B)
        del beat["call"]
        beat["steps"] = []

    refused(
        first_without,
        "scene api beat 1: a console beat with no call keeps the call before it",
    )

    def two_without(data):
        beats = api_scene(data)["beats"]
        beats.insert(E2, copy.deepcopy(beats[E2 - 1]))

    refused(
        two_without,
        "scene api beat 6: a console beat with no call keeps the call before it",
    )


# ---------------------------------------------------------------------------
# What a call may be
# ---------------------------------------------------------------------------


def test_a_call_not_in_the_closed_list_is_refused():
    refused(
        lambda d: api_beat(d, R2)["call"].update(path="/api/v1/holdout/all"),
        "GET /api/v1/holdout/all is not one of the calls a console may make",
    )
    refused(
        lambda d: api_beat(d, R2)["call"].update(method="DELETE"),
        "method",
    )


def test_another_status_than_the_calls_is_refused():
    def holdout_at_200(data):
        api_beat(data, C)["call"]["answer"]["status"] = 200

    refused(holdout_at_200, "POST /api/v1/holdout answers 201, not 200")
    refused(
        lambda d: api_beat(d, D)["call"]["answer"].update(status=204),
        "status",
    )


def test_a_body_key_the_call_does_not_send_is_refused():
    """The sequential testing PUT changes nothing else, Bayesian included."""
    refused(
        lambda d: api_beat(d, E1)["call"]["body"].update(bayesian_enabled=False),
        "PUT /api/v1/experiments/{experiment} sends no 'bayesian_enabled'",
    )


def test_a_call_with_one_right_body_sends_exactly_it():
    refused(
        lambda d: api_beat(d, D)["call"]["body"].update(start_date="{experiment}"),
        'sends exactly {"start_date": "{start}"}',
    )


def test_a_get_sends_no_body_and_a_post_or_put_sends_one():
    refused(
        lambda d: api_beat(d, R1)["call"].update(body={"a": 1}),
        "a GET sends no body",
    )
    refused(lambda d: api_beat(d, C)["call"].pop("body"), "a POST sends a body")


def test_a_body_over_2_kb_is_refused():
    big = "x" * storyboard.CALL_BODY_BYTES
    refused(
        lambda d: api_beat(d, C)["call"]["body"].update(description=big),
        f"bytes; at most {storyboard.CALL_BODY_BYTES}",
    )


@pytest.mark.parametrize(
    "show, reason",
    [
        ([], "show"),
        (["status"] * 2, "show names a field twice"),
        ([f"f{n}" for n in range(9)], "show"),
        (["status", "group.id"], "is not a top-level field of an answer"),
    ],
)
def test_show_is_one_to_eight_top_level_fields(show, reason):
    refused(lambda d: api_beat(d, B)["call"].update(show=show), reason)


def test_an_answer_field_not_shown_is_refused():
    refused(
        lambda d: api_beat(d, D)["call"]["answer"]["fields"].update(name="x"),
        "the answer's 'name' is checked but not shown",
    )


def test_a_response_body_in_the_board_is_refused():
    refused(
        lambda d: api_beat(d, R2)["call"]["answer"].update(body={"id": "x"}),
        "body",
    )


# ---------------------------------------------------------------------------
# What a call may hold: never a value the run makes, a secret or an address
# ---------------------------------------------------------------------------

#: A made-up id, as an answer would carry one: no call may write one down.
AN_ID = "6b1f0c1e-1111-4222-8333-444455556666"


@pytest.mark.parametrize(
    "change, reason",
    [
        (
            lambda d: api_beat(d, R2)["call"]["answer"]["fields"].update(id=AN_ID),
            "names an id",
        ),
        (
            lambda d: api_beat(d, B)["call"]["body"].update(
                experiment_id=AN_ID.replace("-", "")
            ),
            "names an id",
        ),
        (
            lambda d: api_beat(d, R1)["call"].update(
                path="/api/v1/experiments/" + AN_ID
            ),
            "names an id",
        ),
        (
            lambda d: api_beat(d, C)["call"]["body"].update(name="see https://x.io"),
            "names a scheme",
        ),
        (
            lambda d: api_beat(d, C)["call"]["body"].update(name="at localhost"),
            "names a host",
        ),
        (
            lambda d: api_beat(d, C)["call"]["body"].update(name="api.local:8000"),
            "names a host",
        ),
        (
            lambda d: api_beat(d, C)["call"]["body"].update(
                description=config.STATIC_NEEDLES[0]
            ),
            "holds a secret value (static needle 1)",
        ),
        (
            lambda d: api_beat(d, C)["call"]["body"].update(name="Q4 {holdout"),
            "holds a brace outside a placeholder",
        ),
    ],
    ids=[
        "an-id-in-an-answer",
        "an-id-without-dashes-in-a-body",
        "an-id-in-a-path",
        "a-scheme",
        "localhost",
        "a-host-and-port",
        "a-static-needle",
        "a-stray-brace",
    ],
)
def test_a_value_written_into_a_call_is_refused(change, reason):
    refused(change, reason)


def test_a_path_with_a_scheme_host_and_port_is_refused():
    refused(
        lambda d: api_beat(d, R2)["call"].update(
            path="http://localhost:8000/api/v1/holdout"
        ),
        "a call's path is under /api/v1/",
    )


# ---------------------------------------------------------------------------
# Placeholders and kept ids
# ---------------------------------------------------------------------------


def test_a_placeholder_outside_the_closed_set_is_refused():
    refused(
        lambda d: api_beat(d, R2)["call"]["answer"]["fields"].update(id="{holdout_id}"),
        "scene api beat 7: {holdout_id} is not a placeholder this storyboard has"
        " (start, experiment, group, holdout)",
    )
    refused(
        lambda d: api_beat(d, B)["call"]["answer"]["fields"].update(
            experiment_id="{experiment_id}"
        ),
        "{experiment_id} is not a placeholder this storyboard has"
        " (start, experiment, group)",
    )


def test_a_kept_id_used_before_it_is_kept_is_refused():
    refused(
        lambda d: api_beat(d, B)["call"]["answer"]["fields"].update(
            experiment_id="{holdout}"
        ),
        "scene api beat 1: {holdout} is used before the call that keeps it",
    )


def test_an_id_kept_twice_or_under_a_fixed_name_is_refused():
    refused(
        lambda d: api_beat(d, R2)["call"].update(keep={"holdout": "id"}),
        "'holdout' is kept twice",
    )
    refused(
        lambda d: api_beat(d, C)["call"].update(keep={"group": "id"}),
        "keep 'group'",
    )


def test_the_group_placeholder_needs_the_setups_one_group():
    refused(lambda d: d.pop("setup"), "{group} is not a placeholder")

    def two_groups(data):
        groups = data["setup"]["exclusion_groups"]
        groups.append(dict(groups[0], name="Other tests"))

    refused(two_groups, "{group} is not a placeholder")


# ---------------------------------------------------------------------------
# The design end state's api: every key a call sets, and no other
# ---------------------------------------------------------------------------


def test_a_key_a_call_sets_that_the_end_state_does_not_name_is_refused():
    refused(
        lambda d: d["end_state"]["design"]["api"].remove("holdout"),
        "a call sets 'holdout', which the design end state's api does not name",
    )


def test_a_key_the_end_state_names_that_no_call_sets_is_refused():
    def no_holdout(data):
        beats = api_scene(data)["beats"]
        del beats[R2 - 1]
        del beats[C - 1]

    refused(
        no_holdout,
        "the design end state's api names 'holdout', which no call sets",
    )


def test_a_key_set_twice_is_refused():
    def schedule_twice(data):
        beats = api_scene(data)["beats"]
        beats.insert(D, copy.deepcopy(beats[D - 1]))

    refused(schedule_twice, "an api key is set by two calls")
    refused(
        lambda d: d["end_state"]["design"]["api"].append("holdout"),
        "an api key repeats",
    )


# ---------------------------------------------------------------------------
# The setup's exclusion group
# ---------------------------------------------------------------------------


def test_the_setup_makes_one_group_with_the_seeded_experiment_at_full_traffic():
    setup = storyboard.load(V9).setup
    assert setup.segments == []
    assert [g.model_dump() for g in setup.exclusion_groups] == [
        {
            "name": "Checkout tests",
            "traffic_allocation": 1.0,
            "experiments": ["Checkout Button Color"],
        }
    ]


def test_a_setup_that_makes_nothing_is_refused():
    refused(
        lambda d: d["setup"].update(exclusion_groups=[]),
        "a setup makes at least one segment or exclusion group",
    )


@pytest.mark.parametrize("allocation", [0, 1.5])
def test_a_group_allocation_outside_0_to_1_is_refused(allocation):
    refused(
        lambda d: d["setup"]["exclusion_groups"][0].update(
            traffic_allocation=allocation
        ),
        "traffic_allocation",
    )


# ---------------------------------------------------------------------------
# The estimate and the hold
# ---------------------------------------------------------------------------


def test_a_send_is_estimated_as_a_paint_and_the_calls_allowance():
    assert storyboard.step_ms(storyboard.Send(send=True)) == (
        config.PAINT_MS + config.SEND_MS + storyboard.STEP_OVERHEAD_MS
    )


def test_a_call_beat_is_held_at_least_the_exchange_minimum():
    made = storyboard.load(V9)
    scene = next(s for s in made.scenes if s.id == "api")
    holds = [storyboard.min_hold_ms(beat) for beat in scene.beats]
    e2 = scene.beats[E2 - 1]
    assert holds[E2 - 1] == storyboard.min_cue_ms(e2.cue) < config.EXCHANGE_MIN_MS
    assert [h for i, h in enumerate(holds) if i != E2 - 1] == [
        config.EXCHANGE_MIN_MS
    ] * 6
    assert voiced.Pacing.hold_ms(scene.beats[R2 - 1], None) == config.EXCHANGE_MIN_MS
    design = made.scenes[0]
    assert [storyboard.min_hold_ms(b) for b in design.beats] == [
        storyboard.min_cue_ms(b.cue) for b in design.beats
    ]


def test_the_estimates_hold_each_call_beat_for_its_minimum(monkeypatch):
    """Silent and voiced, the length estimate counts the exchange minimum: on
    each call beat, what it adds over the caption and the steps alone."""
    made = storyboard.load(V9)
    floors: List[int] = [
        config.EXCHANGE_MIN_MS
        - max(
            storyboard.min_cue_ms(beat.cue),
            sum(storyboard.step_ms(s) for s in beat.steps),
        )
        for beat in made.scenes[-1].beats
        if beat.call is not None
    ]
    assert len(floors) == 6 and all(gap > 0 for gap in floors)
    short = {(s, b, n): 2400 for s, b, n, _ in storyboard.narration_lines(made)}
    with_floor = (
        storyboard.estimate_ms(made),
        storyboard.voiced_estimate_ms(made, short),
    )
    monkeypatch.setattr(
        storyboard, "min_hold_ms", lambda b: storyboard.min_cue_ms(b.cue)
    )
    without = (storyboard.estimate_ms(made), storyboard.voiced_estimate_ms(made, short))
    assert with_floor[0] - without[0] == sum(floors)
    assert with_floor[1] > without[1]
