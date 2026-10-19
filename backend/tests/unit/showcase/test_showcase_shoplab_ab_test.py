"""Video 6, "An A/B test, from setup to winner" (plan v2.2, T178): its storyboard,
its traffic and shoppers, and the tool changes it brings.

The numbers the video states are pinned against the code that makes them on the
platform: the traffic's users and conversions at seed 34 with the two shoppers
filmed in ShopLab, the planned sample at the typed 5%, and the result the
results API gives for them; each shopper's arm by the server's own hash. Each
contract the video adds (one browser context with declared visitors, gate
10/6, the apps' dependencies, the own-build sentence, the required capture
gates, traffic run in one card) is shown refusing its planted defect. No
browser, Docker, network or database: stand-in pages, contexts and APIs.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from showcase import contract
from showcase.capture import build, config, director, gates, guard, run, storyboard
from showcase.capture import traffic as traffic_module

from backend.app.core.consistent_hash import bucket_of
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.power_calculator_service import sample_size_two_proportions
from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    binomial_metric_result,
)

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
ACCEPTANCE = REPO / "tests" / "acceptance"
V6 = ACCEPTANCE / "showcase" / "storyboards" / "06-shoplab-ab-test.yaml"
SHOPLAB = REPO / "demo" / "shoplab" / "src"
KEY = "shoplab_checkout_flow"

#: At seed 34, the server's hash and the generator's draws give these
#: (PE condition 4 of T178 14); gate 10 holds the platform to them.
GENERATOR = {
    "control": {"users": 4_031, "converting_users": 2_524},
    "treatment": {"users": 3_969, "converting_users": 2_718},
}
#: ...and with the two shoppers on camera: a Control non-buyer, a One page buyer.
WITH_SHOPPERS = {
    "control": {"users": 4_032, "converting_users": 2_524},
    "treatment": {"users": 3_970, "converting_users": 2_719},
}
SHOPPER_A = "5ccec58f-0e70-4378-a129-7842bc337b8d"
SHOPPER_B = "2e01ea31-e519-4791-aae4-88cc29e105a1"
REASON = "One page shows 9.4% improvement on Conversion (p=0.0000)."


def board() -> storyboard.Storyboard:
    return storyboard.load(V6)


def v6_data() -> dict:
    return yaml.safe_load(V6.read_text(encoding="utf-8"))


def scene(name: str) -> storyboard.Scene:
    return next(s for s in board().scenes if s.id == name)


def form_key(name: str) -> str:
    """The wizard's ``generateKey`` (frontend/src/components/experiments/new/formState.ts)."""
    return re.sub(r"^_+|_+$", "", re.sub(r"[^a-z0-9]+", "_", name.lower()))[:64]


def typed() -> list:
    return [
        step.text
        for s in board().scenes
        for item in s.beats
        for step in item.steps
        if isinstance(step, storyboard.Type)
    ]


def plan_at(seed: int) -> traffic_module.Plan:
    made = board().traffic
    assert made is not None
    return traffic_module.plan(made.model_copy(update={"seed": seed}), KEY)


@pytest.fixture(scope="module")
def plan() -> traffic_module.Plan:
    made = board()
    return traffic_module.plan(made.traffic, made.end_state.experiment.key)


# --- The storyboard -----------------------------------------------------------------


def test_the_storyboard_loads_and_its_captions_are_fixed_and_fit_the_video():
    made = board()
    for cue in storyboard.compile_cues(made):
        assert contract.caption_problems(cue["text"]) == []
    assert storyboard.cue_list_bytes(made) == storyboard.cue_list_bytes(
        storyboard.parse(v6_data())
    )
    length = (
        storyboard.estimate_ms(made) + contract.TITLE_CARD_MS + contract.END_CARD_MS
    )
    assert contract.MIN_VIDEO_MS + 5_000 <= length <= contract.MAX_VIDEO_MS - 5_000


def test_the_off_camera_card_and_the_result_are_held_still():
    """The ``later`` card's note states three departures in about 45 words, so
    it holds still after its narration (``config.PAYOFF_MS``), as the result
    does; its wording is the D84 statements' (round 1 review, S7)."""
    made = board()
    held = [
        (scene.id, number)
        for scene in made.scenes
        for number, beat in enumerate(scene.beats, start=1)
        if beat.payoff
    ]
    assert held == [("later", 1), ("result", 1)]


def test_video_6_registers_in_the_tool_and_records_shoplab_only():
    assert config.VIDEOS["06-shoplab-ab-test"] == "storyboards/06-shoplab-ab-test.yaml"
    assert board().apps == ["shoplab"]
    assert board().seed == "demo"


def test_the_experiment_is_made_with_the_key_shoplabs_checkout_asks_for():
    """PE condition 4: named "ShopLab checkout flow", so the wizard keys it
    shoplab_checkout_flow, the key ShopLab's checkout asks for."""
    made = board()
    end = made.end_state.experiment
    assert end.name == "ShopLab checkout flow" == made.traffic.experiment
    assert form_key(end.name) == end.key == KEY
    env = (SHOPLAB / "lib" / "env.ts").read_text(encoding="utf-8")
    assert f"checkoutFlow: '{KEY}'" in env
    words = typed()
    assert end.name in words and end.winner in words and made.traffic.event in words


def test_the_one_page_variant_carries_the_configuration_the_checkout_reads():
    """checkout.tsx draws one page only when ``configuration.steps === 1``."""
    configuration = next(w for w in typed() if w.startswith("{"))
    assert json.loads(configuration) == {"steps": 1}
    checkout = (SHOPLAB / "pages" / "checkout.tsx").read_text(encoding="utf-8")
    assert "configuration?.steps === 1 ? 'one_page' : 'standard'" in checkout
    for shopper in board().end_state.experiment.on_camera_shoppers:
        assert f'data-testid="{shopper.drawn}"' in checkout


def test_the_estimate_step_types_62_and_5_and_the_end_state_plans_for_5_percent():
    """N1 and PE condition 4: the 5% typed is saved with the experiment, so the
    results page plans for it (``mde_source`` "experiment", gate 10)."""
    setup = scene("setup")
    estimate = next(b for b in setup.beats if b.focus.testid == "estimate-baseline")
    fields = [(s.type.testid, s.text) for s in estimate.steps]
    assert fields == [("estimate-baseline", "62"), ("estimate-mde", "5")]
    assert board().end_state.experiment.mde == float(fields[1][1]) / 100 == 0.05


def test_the_traffic_is_the_plans():
    made = board().traffic
    assert (made.users, made.seed, made.control_rate, made.treatment_rate) == (
        8_000,
        34,
        0.62,
        0.68,
    )
    assert made.event == "purchase"


# --- The traffic at seed 34, through the oracle (T165 C13, PE condition 4) ------------


def reached(split: dict, mde: float = 0.05):
    """The oracle's plan on the control's observed rate, and the margin."""
    control = split["control"]
    observed = control["converting_users"] / control["users"]
    need = gates.planned_sample_oracle(observed, mde)
    smallest = min(arm["users"] for arm in split.values())
    return observed, need, smallest, smallest - need


def test_the_traffic_at_seed_34_is_what_the_storyboard_says(plan):
    assert plan.key == KEY and plan.event == "purchase"
    assert plan.split == GENERATOR
    assert sum(arm["users"] for arm in plan.split.values()) == board().traffic.users
    assert f"{plan.p_value:.3g}" == "3.65e-08"
    with_shoppers = gates.with_shoppers(
        plan.split,
        {
            "control": {"users": 1, "converting_users": 0},
            "treatment": {"users": 1, "converting_users": 1},
        },
    )
    assert with_shoppers == WITH_SHOPPERS
    observed, need, smallest, margin = reached(with_shoppers)
    assert round(observed, 6) == 0.625992
    assert (need, smallest, margin) == (3_684, 3_970, 286)
    # The oracle agrees with the product's own function at that rate.
    assert need == sample_size_two_proportions(
        p1=observed, p2=observed * 1.05, alpha=0.05, power=0.8, two_tailed=True
    )
    p = traffic_module.generator().fisher_p_value(with_shoppers)
    assert f"{p:.3g}" == "3.21e-08"


@pytest.mark.regression
def test_the_seed_9_tamper_does_not_reach_the_planned_sample():
    """The check fires: at seed 9 the same traffic misses the plan by 32."""
    split = gates.with_shoppers(
        plan_at(9).split,
        {
            "control": {"users": 1, "converting_users": 0},
            "treatment": {"users": 1, "converting_users": 1},
        },
    )
    _observed, need, smallest, margin = reached(split)
    assert (need, smallest, margin) == (3_985, 3_953, -32)
    # And gate 10's card for that answer reads Not yet, which it refuses.
    end = board().end_state.experiment
    answer = {
        "required_sample_size_per_variant": need,
        "current_sample_size_per_variant": smallest,
        "is_adequate": False,
        "baseline_rate": split["control"]["converting_users"]
        / split["control"]["users"],
        "baseline_source": "observed",
        "mde": 0.05,
        "mde_source": "experiment",
    }
    value, detail, _line = gates.planned_card(answer)
    ok, _numbers, why = gates.planned_sample_end(
        end,
        answer=answer,
        control=split["control"],
        planned_shown=f"PLANNED SAMPLE\n{value}\n{detail}",
        effect_shown=gates.planned_card(answer)[2],
    )
    assert not ok and "not Reached" in why


def test_the_wizards_own_estimate_at_62_and_5_is_3783_and_is_never_shown():
    """The estimate is typed, not calculated on camera: the card plans on the
    observed 62.6% instead (T167), and no caption or sentence says 3,783."""
    assert gates.planned_sample_oracle(0.62, 0.05) == 3_783
    for _scene, _beat, _line, text in storyboard.narration_lines(board()):
        assert "3,783" not in text
    for cue in storyboard.compile_cues(board()):
        assert "3,783" not in cue["text"]


def test_the_result_caption_is_what_the_results_api_computes_for_that_traffic():
    variants = [
        (
            BinomialVariant("v-control", "Control", True),
            WITH_SHOPPERS["control"]["users"],
            WITH_SHOPPERS["control"]["converting_users"],
        ),
        (
            BinomialVariant("v-one", "One page", False),
            WITH_SHOPPERS["treatment"]["users"],
            WITH_SHOPPERS["treatment"]["converting_users"],
        ),
    ]
    metric = SimpleNamespace(
        id="m-1", name="Conversion", metric_type="conversion", is_primary=True
    )
    result = binomial_metric_result(variants, 0.05, "benjamini_hochberg", metric=metric)
    decision = AnalysisService._summarise_decision(None, [result], True)
    assert decision["recommendation"] == "SHIP_VARIANT"
    assert decision["winning_variant_id"] == "v-one"
    assert decision["recommendation_reason"] == REASON
    caption = next(b for b in scene("result").beats).cue
    assert storyboard.numbers_in(caption) == ["9.4%", "95%"]
    assert "9.4%" in storyboard.numbers_in(REASON)
    assert board().end_state.experiment.winner == "One page"


def test_the_planned_sample_card_reads_reached_3970_of_3684():
    answer = {
        "required_sample_size_per_variant": 3_684,
        "current_sample_size_per_variant": 3_970,
        "is_adequate": True,
        "baseline_rate": 2_524 / 4_032,
        "baseline_source": "observed",
        "mde": 0.05,
        "mde_source": "experiment",
    }
    assert gates.planned_card(answer) == (
        "Reached",
        "3,970 of 3,684 per variant",
        "To detect a 5% lift on the control's 62.6%. The 5% was set when the"
        " experiment was created.",
    )


# --- The time card: the simulated shoppers, their count and rates (D84 b, #1267) ------


def test_the_time_card_states_the_traffics_count_rates_and_the_time_skip():
    made = board()
    later = scene("later")
    assert later.traffic == "run"
    card = later.card
    assert storyboard.numbers_in(card.headline) == [f"{made.traffic.users:,}"]
    assert storyboard.numbers_in(card.note) == [
        f"{made.traffic.users:,}",
        gates._pct(made.traffic.control_rate),
        gates._pct(made.traffic.treatment_rate),
        "0",
    ]
    assert "Simulated" in card.note and run.TRAFFIC_MINUTES in card.note
    assert "Duration 0d" in card.note
    # Its narration says the count, as the headline draws it, and "simulated".
    said = later.beats[0].say
    assert storyboard.numbers_in(" ".join(said)) == [f"{made.traffic.users:,}"]
    assert "simulated" in " ".join(said)


def test_the_time_card_says_the_measured_minutes_only_after_the_run():
    later = scene("later")
    report = traffic_module.Report(wall_seconds=151.0)
    shown = run.with_traffic_minutes(later, report)
    assert "sent in 3 minutes (measured)" in shown.card.note
    with pytest.raises(run.StackError, match="before the traffic finished"):
        run.with_traffic_minutes(later.model_copy(update={"traffic": "start"}), report)


# --- The shoppers: each id lands in its arm by the server's own hash -----------------


def test_each_shoppers_visitor_id_is_a_uuid_v4_in_its_arm_by_the_servers_hash(plan):
    """S1: each shopper's arm is the server's, from ``bucket_of`` imported, never
    written again; and no shopper is one of the generator's users."""
    end = board().end_state.experiment
    assert [s.visitor for s in end.on_camera_shoppers] == [SHOPPER_A, SHOPPER_B]
    generated = {user.user_id for user in plan.users}
    for shopper in end.on_camera_shoppers:
        assert storyboard.UUID_V4.fullmatch(shopper.visitor)
        assert uuid.UUID(shopper.visitor).version == 4
        control = bucket_of(shopper.visitor, KEY) < 50
        assert control == (shopper.arm == "Control"), shopper
        assert shopper.visitor not in generated
    assert [s.buys for s in end.on_camera_shoppers] == [False, True]


def test_the_visitor_id_rule_is_shoplabs_own():
    """visitor.ts replaces an id its UUID_V4 refuses: the tool refuses the same ids."""
    source = (SHOPLAB / "lib" / "visitor.ts").read_text(encoding="utf-8")
    found = re.search(r"const UUID_V4 = /(.+)/i;", source)
    assert found is not None
    assert found.group(1) == storyboard.UUID_V4.pattern
    assert f"VISITOR_ID_KEY = '{config.APP_VISITOR_KEY['shoplab']}'" in source


def test_the_shoppers_carts_are_shoplabs_shape_and_products():
    """A cart ShopLab cannot read draws "Your cart is empty", which the video
    never shows (never_show)."""
    cart_source = (SHOPLAB / "lib" / "cart.ts").read_text(encoding="utf-8")
    assert "export const CART_KEY = 'shoplab.cart';" in cart_source
    products = (SHOPLAB / "data" / "products.ts").read_text(encoding="utf-8")
    for name in ("shopper-a", "shopper-b"):
        storage = scene(name).visitor
        assert set(storage) == {"shoplab.visitor_id", "shoplab.cart"}
        items = json.loads(storage["shoplab.cart"])
        assert len(items) == 1
        assert set(items[0]) == {"productId", "price", "quantity"}
        price = re.search(
            r"id: '" + items[0]["productId"] + r"',.*?price: ([0-9]+),", products, re.S
        )
        assert price is not None and int(price.group(1)) == items[0]["price"]
    assert "Your cart is empty" in board().never_show
    checkout = (SHOPLAB / "pages" / "checkout.tsx").read_text(encoding="utf-8")
    assert "Your cart is empty" in checkout


# --- The narration -------------------------------------------------------------------

#: Every number the narration says, by (scene, beat, line), and where the page
#: draws it at that beat (T165 C6; the capture's G2 checks it on the page).
SPOKEN_NUMBERS = {
    # The review's "Minimum detectable effect: 5% (saved)."
    ("setup", 6, 1): ["5%"],
    # The stepper's "3 Payment".
    ("shopper-a", 1, 1): ["3"],
    # The time card's headline "8,000 shoppers later".
    ("later", 1, 1): ["8,000"],
    # The recommendation "One page shows 9.4% improvement ..." and "95% confidence".
    ("result", 1, 1): ["9.4%"],
    ("result", 1, 2): ["95%"],
}


def test_every_beat_is_narrated_and_the_lints_report_nothing():
    from showcase.capture import narration

    made = board()
    assert all(beat.say for s in made.scenes for beat in s.beats)
    assert narration.board_problems(made) == []


def test_the_spoken_numbers_are_the_ones_listed():
    said = {
        (s, beat, line): storyboard.numbers_in(text)
        for s, beat, line, text in storyboard.narration_lines(board())
        if storyboard.numbers_in(text)
    }
    assert said == SPOKEN_NUMBERS


def test_three_sentences_are_video_2s_word_for_word():
    """Their clips are made already: the founder's voice run asks Polly only
    for the rest."""
    v2 = storyboard.load(
        ACCEPTANCE / "showcase" / "storyboards" / "02-first-experiment.yaml"
    )
    v2_lines = {text for *_where, text in storyboard.narration_lines(v2)}
    v6_lines = [text for *_where, text in storyboard.narration_lines(board())]
    shared = [text for text in v6_lines if text in v2_lines]
    assert shared == [
        "Name it, and the key your code uses fills itself in.",
        "Pick the event that counts as a win: here, a purchase.",
        "Choose Start, and it's ready for shoppers.",
    ]


# --- D84 and S2: every departure stated where it happens ------------------------------

#: Every departure video 6 states, with the scene that states it.
STATEMENTS = [
    ("setup", "off-camera setup", "Signed in off camera"),
    ("shopper-a", "off-camera setup", "set up off camera"),
    ("shopper-b", "off-camera setup", "set up and form filled off camera"),
    ("later", "simulated traffic", "Simulated by the tool"),
    ("later", "time skip", "so the results page reads Duration 0d"),
    ("result", "off-camera setup", "Computed off camera first"),
]


def test_video_6_requires_and_states_exactly_these_departures():
    made = board()
    assert storyboard.required_departures(made) == [
        ("setup", "off-camera setup"),
        ("shopper-a", "off-camera setup"),
        ("shopper-b", "off-camera setup"),
        ("later", "simulated traffic"),
        ("later", "time skip"),
        ("result", "off-camera setup"),
    ]
    assert [
        (s.id, d.kind, d.says) for s in made.scenes for d in s.departures
    ] == STATEMENTS
    assert storyboard.departure_problems(made) == []


@pytest.mark.parametrize("scene_id, kind, says", STATEMENTS)
def test_each_statement_taken_off_the_screen_is_refused(scene_id, kind, says):
    raw = v6_data()
    found = next(s for s in raw["scenes"] if s["id"] == scene_id)
    for item in found["beats"]:
        item["cue"] = item["cue"].replace(says, "Shown here")
    if "card" in found:
        found["card"]["note"] = found["card"]["note"].replace(says, "Shown here")
    problems = storyboard.departure_problems(storyboard.parse(raw))
    assert (
        f"scene {scene_id}: the {kind} statement {says!r} is not in a caption or the card"
        in problems
    )


def test_shopper_bs_form_filled_in_a_cut_is_what_its_scene_states():
    """D84: shopper B's details are typed in the first beat's ``before`` (a cut,
    off camera); the scene's statement names the form, on screen in that beat."""
    shopper_b = scene("shopper-b")
    typed_off_camera = [
        step.type.label
        for step in shopper_b.beats[0].before
        if isinstance(step, storyboard.Type)
    ]
    assert typed_off_camera == [
        "Full name",
        "Email",
        "Street address",
        "City",
        "Postal code",
        "Card number",
    ]
    says = [d.says for d in shopper_b.departures if d.kind == "off-camera setup"]
    assert says == ["set up and form filled off camera"]
    assert says[0] in shopper_b.beats[0].cue


#: A step that enters data, as a beat's ``before`` would hold it.
ENTERED_IN_A_CUT = {
    "type": {"type": {"testid": "search-input"}, "text": "purchase"},
    "choose": {"choose": {"testid": "search-input"}, "option": "purchase"},
    "slide": {"slide": {"testid": "search-input"}, "value": 5},
}


def test_the_steps_that_enter_data_are_type_choose_and_slide():
    assert sorted(ENTERED_IN_A_CUT) == sorted(
        step.__name__.lower() for step in storyboard.ENTERS_DATA
    )


def v2_with_a_cut(steps=None):
    """Video 2's storyboard, its arriving scene's first ``before`` replaced."""
    raw = yaml.safe_load(
        (
            ACCEPTANCE / "showcase" / "storyboards" / "02-first-experiment.yaml"
        ).read_text(encoding="utf-8")
    )
    if steps is not None:
        arriving = next(s for s in raw["scenes"] if s["id"] == "arriving")
        arriving["beats"][0]["before"] = steps
    return raw


@pytest.mark.regression
@pytest.mark.parametrize("kind", sorted(ENTERED_IN_A_CUT))
def test_a_scene_that_enters_data_in_a_cut_must_state_off_camera_setup(kind):
    """A step in ``before`` runs while recording is paused: what it types,
    chooses or slides to is entered off camera, and the scene must say so
    (D84). A click in a cut is not counted (video 2 opens its Live tab that
    way, unchanged). The load refuses a choose or a slide in a cut outright
    (the test below), so the row is read on the validated storyboard, as it
    would be if that refusal were ever lifted."""
    plain = storyboard.Storyboard.model_validate(v2_with_a_cut(), strict=False)
    assert ("arriving", "off-camera setup") not in storyboard.required_departures(plain)
    made = storyboard.Storyboard.model_validate(
        v2_with_a_cut([ENTERED_IN_A_CUT[kind]]), strict=False
    )
    assert ("arriving", "off-camera setup") in storyboard.required_departures(made)
    assert storyboard.departure_problems(made) == [
        "scene arriving: off-camera setup is not stated on screen (departures)"
    ]
    # Video 2 as committed clicks in a cut (its Live tab) and needs nothing more.
    v2 = storyboard.load(
        ACCEPTANCE / "showcase" / "storyboards" / "02-first-experiment.yaml"
    )
    assert any(
        isinstance(step, storyboard.Click)
        for s in v2.scenes
        for beat in s.beats
        for step in beat.before
    )
    assert storyboard.departure_problems(v2) == []


@pytest.mark.regression
@pytest.mark.parametrize("kind", ["choose", "slide"])
def test_a_choose_or_a_slide_in_a_cut_is_refused_at_load(kind):
    """A choose or a slide is filmed or not done (C9): the load refuses either
    in a ``before``, so no storyboard reaches the row above with one; a type
    in a cut loads and needs only its statement."""
    with pytest.raises(storyboard.StoryboardError, match="is cut from the video"):
        storyboard.parse(v2_with_a_cut([ENTERED_IN_A_CUT[kind]]))
    typed = storyboard.parse(v2_with_a_cut([ENTERED_IN_A_CUT["type"]]))
    assert ("arriving", "off-camera setup") in storyboard.required_departures(typed)


@pytest.mark.regression
def test_a_storyboard_with_app_scenes_and_no_own_build_sentence_is_refused():
    """S2 (EM C6 of T166): delete the sentence and the load refuses."""
    assert storyboard.own_build_scene(board()) == "later"
    raw = v6_data()
    later = next(s for s in raw["scenes"] if s["id"] == "later")
    later["card"]["note"] = (
        later["card"]["note"]
        .replace("The demo apps run from their own build in this recording.", "")
        .strip()
    )
    assert storyboard.departure_problems(storyboard.parse(raw)) == [
        "the demo apps are recorded, and no scene draws"
        f" {storyboard.OWN_BUILD!r} (EM C6 of T166)"
    ]


@pytest.mark.regression
def test_the_probe_states_the_own_build_too_and_every_storyboard_loads_clean():
    """PE condition 9: 90-app-probe is a caller of S2, with no exemption; the
    probe and every video load with nothing missing on screen."""
    probe = storyboard.load(run.board_path("90-app-probe"))
    assert storyboard.own_build_scene(probe) == "storefront"
    for slug in list(config.VIDEOS) + list(config.PROBES):
        assert (
            storyboard.departure_problems(storyboard.load(run.board_path(slug))) == []
        )
    raw = yaml.safe_load(run.board_path("90-app-probe").read_text(encoding="utf-8"))
    raw["scenes"][0]["beats"] = raw["scenes"][0]["beats"][1:]
    raw["scenes"][0]["departures"] = raw["scenes"][0]["departures"][:1]
    assert storyboard.departure_problems(storyboard.parse(raw)) == [
        "the demo apps are recorded, and no scene draws"
        f" {storyboard.OWN_BUILD!r} (EM C6 of T166)"
    ]


def test_videos_1_to_3_need_no_own_build_sentence():
    for slug in ("01-getting-started", "02-first-experiment", "03-flag-rollout"):
        made = storyboard.load(run.board_path(slug))
        assert made.apps == [] and storyboard.own_build_problems(made) == []


# --- The visitors a scene is filmed as: refused at load (T178 condition 5, S1) -------


def with_visitor(storage, scene_id="shopper-a"):
    raw = v6_data()
    found = next(s for s in raw["scenes"] if s["id"] == scene_id)
    found["off_camera"] = [
        item for item in found["off_camera"] if "visitor" not in item
    ] + [{"visitor": storage}]
    return raw


def load_refusal(raw) -> str:
    with pytest.raises(storyboard.StoryboardError) as caught:
        storyboard.parse(raw)
    return str(caught.value)


A_CART = '[{"productId":"p003","price":59,"quantity":1}]'


@pytest.mark.regression
@pytest.mark.parametrize(
    "storage, words",
    [
        # A valid UUID that is not version 4: ShopLab would swap it for a random one.
        (
            {"shoplab.visitor_id": "5ccec58f-0e70-1378-a129-7842bc337b8d"},
            "is not a UUID v4",
        ),
        ({"shoplab.visitor_id": "shopper-a"}, "is not a UUID v4"),
        # Python's `$` matches before a trailing newline; visitor.ts's does not.
        ({"shoplab.visitor_id": SHOPPER_A + "\n"}, "is not a UUID v4"),
        ({"shoplab.cart": A_CART}, "names its id (shoplab.visitor_id)"),
        (
            {"shoplab.visitor_id": SHOPPER_A, "shoplab.session_token": "x"},
            "never sets a token",
        ),
        (
            {"shoplab.visitor_id": SHOPPER_A, "dashboard.TOKEN": "x"},
            "never sets a token",
        ),
        (
            {"shoplab.visitor_id": SHOPPER_A, "streampulse.device": "x"},
            "only shoplab's own keys",
        ),
        (
            {"shoplab.visitor_id": SHOPPER_A, "shoplab.cart": "[" + "1," * 120 + "1]"},
            "over 200 characters",
        ),
        (
            {
                "shoplab.visitor_id": SHOPPER_A,
                "shoplab.a": "1",
                "shoplab.b": "2",
                "shoplab.c": "3",
                "shoplab.d": "4",
            },
            "at most 4 keys",
        ),
    ],
)
def test_a_visitor_the_app_would_not_keep_or_that_carries_a_secret_is_refused(
    storage, words
):
    assert words in load_refusal(with_visitor(storage))


def test_a_visitor_is_set_only_in_an_app_with_visitors():
    raw = v6_data()
    raw["scenes"][0]["off_camera"] = [{"visitor": {"shoplab.visitor_id": SHOPPER_A}}]
    assert "only in an app with visitors, not dashboard" in load_refusal(raw)


@pytest.mark.regression
def test_an_apps_first_scene_without_its_visitor_is_refused():
    """PE condition 3: ShopLab would first load (the off-camera arming) as a
    random visitor, counted in the experiment and declared by nobody."""
    raw = v6_data()
    first = next(s for s in raw["scenes"] if s["id"] == "shopper-a")
    first["off_camera"] = [{"seed": "shoplab"}]
    assert "the first sets one too" in load_refusal(raw)


@pytest.mark.regression
def test_a_counted_experiment_with_an_app_and_no_visitors_is_refused():
    raw = v6_data()
    for item in raw["scenes"]:
        item["off_camera"] = [
            o for o in item.get("off_camera", []) if "visitor" not in o
        ]
    raw["end_state"]["experiment"]["on_camera_shoppers"] = []
    assert "would load as a random visitor" in load_refusal(raw)


def test_a_switch_must_follow_a_scene_of_the_same_app():
    raw = v6_data()
    scenes = raw["scenes"]
    b = next(s for s in scenes if s["id"] == "shopper-b")
    scenes.remove(b)
    scenes.insert(scenes.index(next(s for s in scenes if s["id"] == "result")), b)
    assert "so it follows a shoplab scene" in load_refusal(raw)


def test_a_visitor_set_twice_is_refused():
    raw = with_visitor({"shoplab.visitor_id": SHOPPER_A}, "shopper-b")
    raw["end_state"]["experiment"]["on_camera_shoppers"][1]["visitor"] = SHOPPER_A
    assert "is set twice" in load_refusal(raw)


@pytest.mark.parametrize(
    "change",
    [
        lambda shoppers: shoppers.pop(),
        lambda shoppers: shoppers.append(dict(shoppers[0])),
        lambda shoppers: shoppers[1].update(visitor=str(uuid.UUID(int=7, version=4))),
    ],
)
def test_the_shoppers_are_exactly_the_visitors_the_scenes_set(change):
    raw = v6_data()
    change(raw["end_state"]["experiment"]["on_camera_shoppers"])
    assert "are not, each once, the visitors the scenes set" in load_refusal(raw)


def test_traffic_runs_in_one_scene_or_starts_and_finishes_in_two():
    raw = v6_data()
    next(s for s in raw["scenes"] if s["id"] == "shopper-b")["traffic"] = "start"
    assert "or run once" in load_refusal(raw)
    raw = v6_data()
    next(s for s in raw["scenes"] if s["id"] == "later")["traffic"] = "finish"
    assert "or run once" in load_refusal(raw)


def test_a_run_scene_states_both_the_simulated_traffic_and_the_time_skip():
    raw = v6_data()
    later = next(s for s in raw["scenes"] if s["id"] == "later")
    later["departures"] = []
    assert storyboard.departure_problems(storyboard.parse(raw)) == [
        "scene later: simulated traffic is not stated on screen (departures)",
        "scene later: time skip is not stated on screen (departures)",
    ]


# --- Off camera before each scene: seeds, visitors, the traffic (run.before_scene) ---


class Recorded:
    """Stands in for the traffic, the stack and the director: records each call."""

    def __init__(self):
        self.calls = []
        self.children = SimpleNamespace(private=[])

    def start(self):
        self.calls.append("traffic start")

    def finish(self):
        self.calls.append("traffic finish")

    def seed_app(self, app, history):
        """ShopLab's seed reads no server key back: nothing to keep."""
        self.calls.append(f"seed {app} history={history}")

    def set_visitor(self, app, storage):
        how = "init script" if not any("visitor" in c for c in self.calls) else "switch"
        self.calls.append(f"visitor {storage['shoplab.visitor_id']} {how}")
        return {"visitor": storage["shoplab.visitor_id"], "how": how}


def test_before_each_scene_the_seed_then_the_visitor_and_the_traffic_runs_once():
    made = Recorded()
    warmed = []
    for item in board().scenes:
        made.calls.append(f"-- {item.id}")
        run.before_scene(
            item,
            users=made,
            warm=warmed.append,
            stack=made,
            director=made,
            redactor=director.redaction.Redactor(),
            read_keys=[],
            log=lambda line: None,
        )
    assert made.calls == [
        "-- setup",
        "-- shopper-a",
        "seed shoplab history=False",
        f"visitor {SHOPPER_A} init script",
        "-- shopper-b",
        f"visitor {SHOPPER_B} switch",
        "-- later",
        "traffic start",
        "traffic finish",
        "-- result",
    ]
    assert warmed == ["ShopLab checkout flow"]


# --- One browser context, two shoppers (T178 condition 5; PE conditions 2 and 3) -----

API = "http://127.0.0.1:28400"
DASH = "http://127.0.0.1:28401"
SHOP = "http://127.0.0.1:28403"
SHOP_KEY = config.APP_STATIC_KEY["shoplab"]
VISITOR_KEY = config.APP_VISITOR_KEY["shoplab"]
MARKS = ("checkout-standard", "checkout-one-page")


class Clock:
    ms = 0.0


@pytest.fixture(autouse=True)
def _stand_in_clock(monkeypatch):
    """The director's clock advances only when a page waits (no real sleeping)."""
    Clock.ms = 0.0
    monkeypatch.setattr(director, "_now_ms", lambda: Clock.ms)


def origin_of(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


class Request:
    def __init__(self, url, body=None, key=SHOP_KEY, failure=None):
        self.url = url
        self.headers = {"x-api-key": key} if key else {}
        self.failure = failure
        self.post_data_json = body


class Response:
    def __init__(self, url, status, request=None, body=None):
        self.url = url
        self.status = status
        self.request = request
        self._body = body
        self.headers = {"content-type": "application/json" if body else "text/html"}

    def json(self):
        return self._body


class Context:
    def __init__(self):
        self.handlers = {}
        self.scripts = []
        self.routes = []
        #: Set to drop every init script, as a context that lost them would.
        self.lose_scripts = False

    def route(self, pattern, handler):
        self.routes.append(handler)

    def route_web_socket(self, pattern, handler):
        pass

    def add_init_script(self, script):
        if not self.lose_scripts:
            self.scripts.append(script)

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def fire(self, event, value):
        for handler in self.handlers.get(event, []):
            handler(value)


def run_init_script(script, origin, local):
    """What a ``director.VISITOR_INIT`` script does, read from its own text."""
    found = re.search(r"location\.origin !== (\"[^\"]*\")\) return;", script)
    if found is None or json.loads(found.group(1)) != origin:
        return
    key = json.loads(
        re.search(
            r"localStorage\.getItem\((\"[^\"]*\")\) !== null\) return;", script
        ).group(1)
    )
    items = json.loads(re.search(r"const items = (\{.*?\});", script).group(1))
    store = local.setdefault(origin, {})
    if key in store:
        return
    for name, value in items.items():
        store.setdefault(name, value)


def answer(page, url, status, body=None, reply=None, key=SHOP_KEY):
    request = Request(url, body, key=key)
    page.fire("request", request)
    page.fire("response", Response(url, status, request, reply))
    page.fire("requestfinished", request)
    return request


def shoplab_load(page, url):
    """ShopLab's first render: its visitor id from localStorage (a new random
    one when absent or not a UUID v4, as visitor.ts does), then the checkout
    test's assignment for that id, answered by the server's hash."""
    if origin_of(url) != SHOP:
        return
    store = page.local.setdefault(SHOP, {})
    ident = store.get(VISITOR_KEY)
    if not (ident and storyboard.UUID_V4.fullmatch(ident)):
        ident = str(uuid.uuid4())
        store[VISITOR_KEY] = ident
    control = bucket_of(ident, KEY) < 50
    answer(
        page,
        f"{API}/api/v1/tracking/assign",
        200,
        body={"experiment_key": KEY, "user_id": ident},
        reply={
            "experiment_key": KEY,
            "user_id": ident,
            "variant_name": "Control" if control else "One page",
            "is_control": control,
            "configuration": None if control else {"steps": 1},
            "assigned": True,
        },
    )


class Page:
    def __init__(self, context):
        self.context = context
        self.url = "about:blank"
        self.local = {}
        self.session = {}
        self.handlers = {}
        self.text = ""
        self.testids = []

    def goto(self, url):
        self.url = url
        for script in self.context.scripts:
            run_init_script(script, origin_of(url), self.local)
        shoplab_load(self, url)

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
        here = origin_of(self.url)
        if script == director.STORAGE_JS:
            return self.local.get(here, {}).get(arg)
        if script == director.SWITCH_JS:
            store = self.local.setdefault(here, {})
            store.clear()
            self.session.pop(here, None)
            store.update(arg)
            return sorted(store)
        if script == director.MARKS_JS:
            return [mark for mark in arg if mark in self.testids]
        if script == director.VIEWPORT_TEXT_JS:
            return self.text
        if script == director.HEADER_JS:
            return {"links": 3, "wrapped": [], "cut": []}
        if script == director.BROKEN_JS:
            return {"overlay": False, "alerts": []}
        raise AssertionError(f"an unexpected page script: {str(script)[:40]}")


def a_director(tmp_path, *, visitor_apps=("shoplab",)):
    context = Context()
    page = Page(context)
    made = director.Director(
        context=context,
        page=page,
        frames=tmp_path / "work" / "v6" / "capture" / "frames",
        dashboard=DASH,
        api_port=28400,
        dashboard_port=28401,
        redactor=director.redaction.Redactor(),
        forbidden=run.forbidden_text(board(), run.Ports(28400, 28401, 28402)),
        work=guard.WorkRoot(tmp_path / "work"),
        apps={"shoplab": 28403},
        app_ready=lambda app: {"experiments": 4, "feature_flags": 2},
        visitor_apps=visitor_apps,
        marks=MARKS,
    )
    made.install()
    # Only the browser's drawing and the needle reads are stood in for.
    made.check_needles = lambda: None
    made.measure_zoom = lambda wanted=None: {"body": wanted}
    made.self_test_overlay = lambda spec: {"offset_px": 0}
    made.recorder.start = lambda: None
    made.recorder.stop = lambda: None
    made.draw_cursor = lambda: None
    made.ring = lambda box=None: None
    return made, page, context


def film(made, page, name, during=None, drawn=()):
    """Off camera, the scene's visitor; then the scene, each beat running
    *during* (the plant) after reading the page's marks."""
    item = scene(name)
    made.where = f"scene {name}, off camera"
    made.set_visitor("shoplab", item.visitor)
    page.testids = list(drawn)

    def beat(sc, index, b):
        made.where = f"scene {sc.id} beat {index}"
        made.read_marks(sc.app)
        if during is not None:
            during(made, page)

    made.beat = beat
    made.scene(item)


def shoppers_filmed(tmp_path, during_b=None):
    made, page, context = a_director(tmp_path)
    film(made, page, "shopper-a", drawn=["checkout-standard"])
    film(made, page, "shopper-b", during_b, drawn=["checkout-one-page"])
    return made, page, context


def test_one_context_the_first_visitor_by_init_script_the_second_by_the_switch(
    tmp_path,
):
    made, page, context = shoppers_filmed(tmp_path)
    assert [v["how"] for v in made.visitors_set] == ["init script", "switch"]
    assert [v["visitor"] for v in made.visitors_set] == [SHOPPER_A, SHOPPER_B]
    # The arming load ran as shopper A (the init script), never as a stranger.
    assert page.local[SHOP][VISITOR_KEY] == SHOPPER_B
    assert set(made.tally.assigns) == {(SHOPPER_A, KEY), (SHOPPER_B, KEY)}
    assert len(made.tally.assigns[(SHOPPER_A, KEY)]) == 2  # arming, then the scene
    assert made.tally.marks == {
        SHOPPER_A: ["checkout-standard"],
        SHOPPER_B: ["checkout-one-page"],
    }
    assert made.tally.visitor_refusals == []
    assert made.tally.visitor_checks == 3
    numbers = made.visitor_numbers()
    assert numbers["refused"] == [] and numbers["requests_checked"] == 3


def test_the_init_script_is_the_shoplab_origins_only_and_keeps_what_is_there():
    script = director.visitor_init(SHOP, VISITOR_KEY, scene("shopper-a").visitor)
    elsewhere = {}
    run_init_script(script, DASH, elsewhere)
    assert elsewhere == {}
    fresh = {}
    run_init_script(script, SHOP, fresh)
    assert fresh[SHOP] == scene("shopper-a").visitor
    switched = {SHOP: dict(scene("shopper-b").visitor)}
    run_init_script(script, SHOP, switched)
    assert switched[SHOP] == scene("shopper-b").visitor
    assert json.dumps(SHOP) in script and "localStorage.getItem" in script


@pytest.mark.regression
def test_a_request_carrying_shopper_as_id_after_the_switch_is_refused(tmp_path):
    """EM condition 5: shopper A's page, still open, asks again after the switch."""

    def stale(made, page):
        answer(
            page,
            f"{API}/api/v1/tracking/batch",
            200,
            body={"events": [{"user_id": SHOPPER_A, "event_name": "page_view"}]},
        )

    with pytest.raises(
        director.CaptureFailed, match=f"S1: .*names visitor {SHOPPER_A}"
    ):
        shoppers_filmed(tmp_path, stale)


@pytest.mark.regression
def test_an_undeclared_shoplab_load_is_refused(tmp_path):
    """PE condition 3: the init script lost, ShopLab mints a random visitor on
    its first (arming) load; the request names it and the run refuses."""
    made, page, context = a_director(tmp_path)
    context.lose_scripts = True
    with pytest.raises(director.CaptureFailed, match="S1"):
        film(made, page, "shopper-a", drawn=["checkout-standard"])
    assert made.tally.visitor_refusals, "the stranger's request was not seen"


@pytest.mark.regression
def test_an_app_armed_before_its_first_visitor_is_refused(tmp_path):
    made, page, _ = a_director(tmp_path)
    with pytest.raises(
        director.CaptureFailed, match="first loaded with no visitor set"
    ):
        made.arm("shoplab")
    made, page, _ = a_director(tmp_path)
    made.armed["shoplab"] = {}
    with pytest.raises(director.CaptureFailed, match="loaded before its first visitor"):
        made.set_visitor("shoplab", scene("shopper-a").visitor)


def test_the_switch_runs_on_the_apps_origin_and_must_leave_only_the_visitors_keys(
    tmp_path,
):
    made, page, _ = a_director(tmp_path)
    film(made, page, "shopper-a", drawn=["checkout-standard"])
    page.url = f"{DASH}/experiments"
    with pytest.raises(director.CaptureFailed, match="switched on shoplab's origin"):
        made.set_visitor("shoplab", scene("shopper-b").visitor)
    page.url = f"{SHOP}/checkout"
    page.evaluate = lambda script, arg=None: [
        "shoplab.cart",
        "shoplab.extra",
        VISITOR_KEY,
    ]
    with pytest.raises(director.CaptureFailed, match="after the switch shoplab holds"):
        made.set_visitor("shoplab", scene("shopper-b").visitor)


def test_a_page_that_holds_another_visitor_after_it_loads_is_refused(tmp_path):
    made, page, _ = a_director(tmp_path)
    film(made, page, "shopper-a", drawn=["checkout-standard"])
    page.local[SHOP][VISITOR_KEY] = str(uuid.uuid4())
    with pytest.raises(director.CaptureFailed, match="holds visitor"):
        made.check_visitor_storage("shoplab")


def test_only_an_app_filmed_as_visitors_gets_one(tmp_path):
    made, page, _ = a_director(tmp_path, visitor_apps=())
    with pytest.raises(director.CaptureFailed, match="not filmed as declared visitors"):
        made.set_visitor("shoplab", scene("shopper-a").visitor)


# PE condition 2's plants, each after the visitor switch (single-context design).


@pytest.mark.regression
@pytest.mark.parametrize("status", [401, 404])
def test_after_the_switch_an_answer_of_400_or_more_is_refused(tmp_path, status):
    with pytest.raises(
        director.CaptureFailed, match=f"P2: shoplab: answers 400: {status}"
    ):
        shoppers_filmed(
            tmp_path,
            lambda made, page: answer(
                page,
                f"{API}/api/v1/feature-flags/evaluate/shoplab_new_search?user_id={SHOPPER_B}",
                status,
            ),
        )


@pytest.mark.regression
def test_after_the_switch_a_failed_request_is_refused(tmp_path):
    def fail(made, page):
        failed = Request(f"{API}/api/v1/tracking/batch", failure="net::ERR_FAILED")
        page.fire("request", failed)
        page.fire("requestfailed", failed)

    with pytest.raises(
        director.CaptureFailed, match="P2: shoplab: failed: /api/v1/tracking/batch"
    ):
        shoppers_filmed(tmp_path, fail)


@pytest.mark.regression
def test_after_the_switch_a_request_unanswered_at_the_scenes_end_is_refused(tmp_path):
    def hang(made, page):
        page.fire(
            "request",
            Request(f"{API}/api/v1/tracking/assign", {"user_id": SHOPPER_B}),
        )

    with pytest.raises(
        director.CaptureFailed, match="P2: shoplab: unanswered: /api/v1/tracking/assign"
    ):
        shoppers_filmed(tmp_path, hang)


@pytest.mark.regression
def test_after_the_switch_an_x_api_key_that_is_not_a_needle_is_refused(tmp_path):
    with pytest.raises(director.CaptureFailed, match="X-API-Key that is not a needle"):
        shoppers_filmed(
            tmp_path,
            lambda made, page: answer(
                page,
                f"{API}/api/v1/tracking/assign",
                200,
                key="eptk_" + "u" * 40,
            ),
        )


@pytest.mark.regression
def test_after_the_switch_a_popup_is_refused(tmp_path):
    def popup(made, page):
        page.context.fire("page", Page(page.context))

    with pytest.raises(director.CaptureFailed, match="P2: a page opened outside"):
        shoppers_filmed(tmp_path, popup)


class Route:
    """A route the guard decides: ``continue_`` really connects to its address."""

    def __init__(self, url):
        self.request = SimpleNamespace(url=url)
        self.aborted = None

    def abort(self, reason):
        self.aborted = reason

    def continue_(self):
        from urllib.parse import urlparse

        where = urlparse(self.request.url)
        with socket.create_connection((where.hostname, where.port), timeout=2):
            pass


@pytest.mark.regression
def test_after_the_switch_a_loopback_port_outside_the_allowed_set_is_refused(tmp_path):
    """The test's own listener, on a free port it owns (never 3000, 3100, 3200,
    3300 or 8000): the guard aborts the request, and nothing connects."""
    made, page, context = shoppers_filmed(tmp_path)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    assert port not in (3000, 3100, 3200, 3300, 8000)
    accepted = []

    def accept():
        listener.settimeout(1.5)
        try:
            connection, _ = listener.accept()
            accepted.append(connection)
            connection.close()
        except OSError:
            pass

    waiting = threading.Thread(target=accept)
    waiting.start()
    try:
        route = Route(f"http://127.0.0.1:{port}/steal")
        context.routes[0](route)
        waiting.join(timeout=3)
    finally:
        listener.close()
    assert route.aborted == "blockedbyclient"
    assert accepted == []
    with pytest.raises(
        director.CaptureFailed, match=f"gate 15c: request to 127.0.0.1:{port}"
    ):
        made.check_events()


@pytest.mark.regression
def test_after_the_switch_a_never_show_string_on_the_page_is_refused(tmp_path):
    """S1: a cart ShopLab could not read draws "Your cart is empty" (U4)."""
    made, page, _ = shoppers_filmed(tmp_path)
    page.text = "Checkout Your cart is empty Browse products"
    focus = SimpleNamespace(
        bounding_box=lambda: {"x": 100.0, "y": 100.0, "width": 200.0, "height": 40.0},
        evaluate=lambda script, *args: True,
        inner_text=lambda: "One page",
    )
    with pytest.raises(
        director.CaptureFailed, match="U4: .* the page shows 'Your cart is empty'"
    ):
        made.check_page(
            director.Box(100, 100, 200, 40), focus, "Its variant: One page."
        )
    page.text = "Checkout Delivery address One page"
    made.check_page(director.Box(100, 100, 200, 40), focus, "Its variant: One page.")


@pytest.mark.regression
def test_after_the_switch_a_narrated_number_must_be_on_that_page(tmp_path):
    """G2 reads the scene's own page: refused when the number is absent, passes
    when it is drawn."""
    made, page, _ = shoppers_filmed(tmp_path)
    page.text = "Checkout Delivery address"
    with pytest.raises(director.CaptureFailed, match="G2: .*the narration says 3"):
        made.check_spoken(("This shopper gets 3 steps.",), "Checkout")
    page.text = "Checkout 1 Address 2 Shipping 3 Payment"
    made.check_spoken(("This shopper gets 3 steps.",), "Checkout")


class Browser:
    def __init__(self, contexts):
        self.contexts = contexts


@pytest.mark.regression
def test_a_context_the_capture_did_not_make_is_refused_at_teardown():
    """EM condition 5: the capture's one context, and no other: one opened by
    other code, or the capture's own replaced by another."""
    mine, other = object(), object()
    assert run.refuse_other_contexts(Browser([mine]), mine) == 1
    with pytest.raises(
        director.CaptureFailed, match="S1: the browser holds 2 contexts"
    ):
        run.refuse_other_contexts(Browser([mine, other]), mine)
    with pytest.raises(
        director.CaptureFailed, match="S1: the browser holds 1 contexts"
    ):
        run.refuse_other_contexts(Browser([other]), mine)


def test_the_capture_makes_one_context_and_checks_it_at_teardown():
    """run.py calls new_context once, and refuse_other_contexts before it closes
    the browser."""
    import ast

    source = (ACCEPTANCE / "showcase" / "capture" / "run.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    made = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "new_context"
    ]
    assert len(made) == 1
    assert "contexts = refuse_other_contexts(browser, context)" in source
    assert source.index("refuse_other_contexts(browser, context)") < source.index(
        "browser.close()"
    )


def test_off_camera_steps_draw_nothing_and_on_camera_ones_do(tmp_path):
    """Shopper B's details are typed in a beat's ``before`` (a cut): the cursor
    and the click pulse are not drawn then, and are while recording."""
    made, page, _ = a_director(tmp_path)
    del made.draw_cursor  # the real ones, on a counting screencast
    del made.ring
    shown = []
    page.screencast = SimpleNamespace(
        show_overlay=lambda html: shown.append(html) or SimpleNamespace(__exit__=None)
    )
    made._dispose = lambda handle: None
    page.mouse = SimpleNamespace(move=lambda x, y: None)
    made.move_to(300, 300, ms=30)
    made.click_pulse(300, 300)
    made.ring(director.Box(1, 1, 10, 10))
    assert shown == []
    made.recorder.t0 = Clock.ms
    made.move_to(320, 320, ms=30)
    made.click_pulse(320, 320)
    made.ring(director.Box(1, 1, 10, 10))
    assert len(shown) >= 3


# --- Gate 10/6: each shopper's arm four ways, and the counts with them (S8b) ----------

ASSIGNS = {
    (SHOPPER_A, KEY): [
        {
            "variant_name": "Control",
            "is_control": True,
            "assigned": True,
            "configuration": None,
        }
    ],
    (SHOPPER_B, KEY): [
        {
            "variant_name": "One page",
            "is_control": False,
            "assigned": True,
            "configuration": {"steps": 1},
        }
    ],
}
API_ROWS = {
    SHOPPER_A: [
        {"experiment_name": "ShopLab checkout flow", "variant_name": "Control"}
    ],
    SHOPPER_B: [
        {"experiment_name": "ShopLab checkout flow", "variant_name": "One page"}
    ],
}
MARKED = {SHOPPER_A: ["checkout-standard"], SHOPPER_B: ["checkout-one-page"]}
EVENTS = [
    {
        "user_id": SHOPPER_A,
        "experiment_key": KEY,
        "event_name": "page_view",
        "status": 200,
    },
    {
        "user_id": SHOPPER_B,
        "experiment_key": KEY,
        "event_name": "purchase",
        "status": 200,
    },
]


def shoppers_check(**changes):
    kwargs = {
        "event": "purchase",
        "assigns": ASSIGNS,
        "api_assignments": API_ROWS,
        "marks": MARKED,
        "events": EVENTS,
    }
    kwargs.update(changes)
    return gates.shoppers_end(board().end_state.experiment, **kwargs)


def test_the_shoppers_agree_four_ways_and_add_one_user_to_each_arm():
    ok, numbers, why, added = shoppers_check()
    assert ok, why
    assert added == {
        "control": {"users": 1, "converting_users": 0},
        "treatment": {"users": 1, "converting_users": 1},
    }
    assert [s["network"] for s in numbers["shoppers"]] == [["Control"], ["One page"]]


@pytest.mark.regression
@pytest.mark.parametrize(
    "changes, words",
    [
        # The non-circular oracle (QA A2): the page and the declaration agree,
        # the server put the shopper in the other arm.
        (
            {
                "assigns": {
                    **ASSIGNS,
                    (SHOPPER_B, KEY): [
                        {
                            **ASSIGNS[(SHOPPER_B, KEY)][0],
                            "variant_name": "Control",
                            "is_control": True,
                        }
                    ],
                }
            },
            "the storefront was assigned ['Control']",
        ),
        (
            {
                "assigns": {
                    **ASSIGNS,
                    (SHOPPER_B, KEY): [
                        {**ASSIGNS[(SHOPPER_B, KEY)][0], "assigned": False}
                    ],
                }
            },
            "the storefront was assigned ['One page']",
        ),
        ({"assigns": {(SHOPPER_A, KEY): ASSIGNS[(SHOPPER_A, KEY)]}}, "never asked for"),
        (
            {
                "api_assignments": {
                    **API_ROWS,
                    SHOPPER_B: [
                        {
                            "experiment_name": "ShopLab checkout flow",
                            "variant_name": "Control",
                        }
                    ],
                }
            },
            "GET /tracking/assignments says ['Control']",
        ),
        (
            {"api_assignments": {**API_ROWS, SHOPPER_A: []}},
            "GET /tracking/assignments says []",
        ),
        # The page drew the other arm, or both.
        (
            {"marks": {**MARKED, SHOPPER_B: ["checkout-standard"]}},
            "the page drew ['checkout-standard']",
        ),
        (
            {
                "marks": {
                    **MARKED,
                    SHOPPER_A: ["checkout-standard", "checkout-one-page"],
                }
            },
            "the page drew",
        ),
        ({"marks": {SHOPPER_B: ["checkout-one-page"]}}, "the page drew []"),
        # The buyer's purchase never answered 2xx; the non-buyer sent one.
        ({"events": EVENTS[:1]}, "buys on camera, and no 'purchase'"),
        (
            {"events": [EVENTS[0], {**EVENTS[1], "status": 500}]},
            "buys on camera, and no 'purchase'",
        ),
        (
            {"events": EVENTS + [{**EVENTS[1], "user_id": SHOPPER_A}]},
            "does not buy, and sent 1",
        ),
    ],
)
def test_a_shopper_the_server_page_or_purchase_disagrees_about_is_refused(
    changes, words
):
    ok, _numbers, why, _added = shoppers_check(**changes)
    assert not ok and words in why, why


V6_SAMPLE = {
    "required_sample_size_per_variant": 3_684,
    "current_sample_size_per_variant": 3_970,
    "is_adequate": True,
    "baseline_rate": 2_524 / 4_032,
    "baseline_source": "observed",
    "mde": 0.05,
    "mde_source": "experiment",
}
V6_PLANNED_SHOWN = "PLANNED SAMPLE\nReached\n3,970 of 3,684 per variant"
V6_EFFECT = (
    "To detect a 5% lift on the control's 62.6%. The 5% was set when the"
    " experiment was created."
)


def results_for(split, p_value, reason=REASON):
    return {
        "metrics": [
            {
                "is_primary": True,
                "variants": [
                    {
                        "variant_id": "v-control",
                        "variant_name": "Control",
                        "is_control": True,
                        "sample_size": split["control"]["users"],
                        "conversions": split["control"]["converting_users"],
                        "p_value": None,
                    },
                    {
                        "variant_id": "v-one",
                        "variant_name": "One page",
                        "is_control": False,
                        "sample_size": split["treatment"]["users"],
                        "conversions": split["treatment"]["converting_users"],
                        "p_value": p_value,
                    },
                ],
            }
        ],
        "summary": {
            "recommendation": "SHIP_VARIANT",
            "winning_variant_id": "v-one",
            "recommendation_reason": reason,
        },
    }


def end_check(**changes):
    end = board().end_state.experiment
    oracle = traffic_module.generator().fisher_p_value(WITH_SHOPPERS)
    kwargs = {
        "named": [{"key": KEY, "status": "active"}],
        "results": results_for(WITH_SHOPPERS, oracle),
        "planned": GENERATOR,
        "report": GENERATOR,
        "oracle_p": oracle,
        "shown": REASON,
        "sample_size": dict(V6_SAMPLE),
        "planned_shown": V6_PLANNED_SHOWN,
        "effect_shown": V6_EFFECT,
        "shoppers": shoppers_check(),
    }
    kwargs.update(changes)
    return gates.experiment_end(end, **kwargs)


def test_gate_10_counts_the_generator_plus_the_shoppers():
    ok, numbers, why = end_check()
    assert ok, why
    assert (
        numbers["counted"] == numbers["planned"] == numbers["answered"] == WITH_SHOPPERS
    )
    assert numbers["planned_sample"]["card"] == (
        "Planned sample: Reached · 3,970 of 3,684 per variant"
    )


@pytest.mark.regression
def test_a_purchase_answered_but_never_counted_is_refused():
    """QA B2: the app's purchase answered 200 without reaching the platform:
    the results API counts the generator's users plus the shoppers' users, one
    conversion short."""
    counted = gates.with_shoppers(
        GENERATOR,
        {
            "control": {"users": 1, "converting_users": 0},
            "treatment": {"users": 1, "converting_users": 0},
        },
    )
    ok, _numbers, why = end_check(
        results=results_for(counted, traffic_module.generator().fisher_p_value(counted))
    )
    assert not ok and "users and conversions differ" in why


@pytest.mark.regression
def test_shoppers_never_checked_or_refused_fail_gate_10():
    ok, _numbers, why = end_check(shoppers=None)
    assert not ok and why == "the on-camera shoppers were never checked"
    ok, _numbers, why = end_check(
        shoppers=shoppers_check(marks={**MARKED, SHOPPER_B: ["checkout-standard"]})
    )
    assert not ok and "the page drew ['checkout-standard']" in why
    # Counted as if the shoppers were not there: the platform holds two more.
    ok, _numbers, why = end_check(
        results=results_for(
            GENERATOR, traffic_module.generator().fisher_p_value(GENERATOR)
        )
    )
    assert not ok and "users and conversions differ" in why


def test_the_capture_reads_the_assignments_with_an_api_key_and_judges_the_shoppers(
    monkeypatch,
):
    """``Users.end_state``: P24 read with the traffic's key, the director's tally."""
    seen, keyed = [], []

    def api(url, token=None, body=None):
        seen.append(url.split("/api/v1", 1)[1])
        if "/experiments/" in url:
            return {
                "items": [
                    {
                        "id": "e-6",
                        "name": "ShopLab checkout flow",
                        "key": KEY,
                        "status": "active",
                    }
                ]
            }
        if url.endswith("/sample-size"):
            return dict(V6_SAMPLE)
        return results_for(
            WITH_SHOPPERS, traffic_module.generator().fisher_p_value(WITH_SHOPPERS)
        )

    def with_key(url, key):
        keyed.append((url.rsplit("/", 1)[1], key))
        return API_ROWS[url.rsplit("/", 1)[1]]

    monkeypatch.setattr(run, "_api", api)
    monkeypatch.setattr(run, "_api_with_key", with_key)
    users = run.Users(None, API, "token-1", None, print)
    users.sender = SimpleNamespace(
        plan=SimpleNamespace(split=GENERATOR, p_value=None, event="purchase"),
        api_key="made-up-traffic-key-0123456789",
    )
    users.report = traffic_module.Report(by_arm=GENERATOR)
    observed = SimpleNamespace(assigns=ASSIGNS, marks=MARKED, events=EVENTS)
    end = board().end_state.experiment
    readings = {
        "recommendation": REASON,
        "planned": V6_PLANNED_SHOWN,
        "effect": V6_EFFECT,
    }
    ok, numbers, why = users.end_state(end, readings, observed=observed)
    assert ok, why
    assert keyed == [
        (SHOPPER_A, "made-up-traffic-key-0123456789"),
        (SHOPPER_B, "made-up-traffic-key-0123456789"),
    ]
    assert numbers["counted"] == WITH_SHOPPERS
    ok, _numbers, why = users.end_state(end, readings, observed=None)
    assert not ok and why == "the on-camera shoppers were never observed"


# --- The storefront assigns against the experiment the video made ---------------------


def test_shoplab_first_loads_only_against_the_experiment_made_on_camera(monkeypatch):
    lists = {
        "experiments": [
            {"key": KEY, "name": "ShopLab checkout flow", "status": "active"},
            {"key": "shoplab_hero_banner", "name": "ShopLab: Hero", "status": "active"},
        ],
        "feature-flags": [{"key": "shoplab_new_search", "status": "active"}],
    }

    def api(url, token=None, body=None):
        items = lists["experiments" if "/experiments/" in url else "feature-flags"]
        return {"items": items, "total": len(items)}

    monkeypatch.setattr(run, "_api", api)
    end = board().end_state.experiment
    assert run.app_ready(API, "t", "shoplab", made=end)["made_on_camera"] == KEY
    # QA A3's plant: the seed made the key first, under its own name.
    lists["experiments"][0]["name"] = "ShopLab: Checkout flow — 3 steps vs one page"
    with pytest.raises(run.StackError, match="not by 'ShopLab checkout flow'"):
        run.app_ready(API, "t", "shoplab", made=end)
    assert run.visitor_apps(board()) == ("shoplab",)
    assert run.visitor_apps(storyboard.load(run.board_path("90-app-probe"))) == ()


def test_what_a_request_names_and_sends():
    assert director.users_named(
        f"{API}/api/v1/feature-flags/evaluate/f?user_id={SHOPPER_A}&context=%7B%7D",
        None,
    ) == [SHOPPER_A]
    assert director.users_named(
        f"{API}/api/v1/tracking/batch",
        {"events": [{"user_id": SHOPPER_A}, {"user_id": SHOPPER_B}, "x"]},
    ) == [SHOPPER_A, SHOPPER_B]
    assert director.events_named(
        f"{API}/api/v1/tracking/batch",
        {
            "events": [
                {"user_id": SHOPPER_B, "experiment_key": KEY, "event_name": "purchase"},
                {
                    "user_id": SHOPPER_B,
                    "feature_flag_key": "f",
                    "event_name": "purchase",
                },
            ]
        },
    ) == [(SHOPPER_B, KEY, "purchase")]
    assert director.events_named(
        f"{API}/api/v1/tracking/track",
        {"user_id": SHOPPER_B, "experiment_key": KEY, "event_type": "purchase"},
    ) == [(SHOPPER_B, KEY, "purchase")]
    assert (
        director.events_named(f"{API}/api/v1/tracking/assign", {"user_id": "x"}) == []
    )


# --- T178 condition 6: the recorded apps' dependencies are the ref's ------------------


def lockfile(next_version="16.4.0", react="19.3.0", react_dom="19.3.0") -> str:
    return json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "shoplab"},
                "node_modules/next": {"version": next_version},
                "node_modules/react": {"version": react},
                "node_modules/react-dom": {"version": react_dom},
            },
        },
        indent=2,
    )


def checkout_and_ref(tmp_path, *, ours=None, installed=None):
    """A checkout's demo/shoplab (lockfile and installed packages) and the ref's
    archived demo/shoplab (its lockfile)."""
    repo, tree = tmp_path / "checkout", tmp_path / "tree"
    app = repo / "demo" / "shoplab"
    app.mkdir(parents=True)
    (app / "package-lock.json").write_text(ours or lockfile(), encoding="utf-8")
    versions = {"next": "16.4.0", "react": "19.3.0", "react-dom": "19.3.0"}
    versions.update(installed or {})
    for name, version in versions.items():
        if version is None:
            continue
        package = app / "node_modules" / name
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"version": version}))
    (tree / "demo" / "shoplab").mkdir(parents=True)
    (tree / "demo" / "shoplab" / "package-lock.json").write_text(
        lockfile(), encoding="utf-8"
    )
    return repo, tree


def test_the_apps_dependencies_agree_with_the_refs_lockfile(tmp_path):
    repo, tree = checkout_and_ref(tmp_path)
    found = build.app_dependencies(repo, tree, "shoplab", "v0.36.0")
    assert found["lockfile_is_the_refs"] is True
    assert found["packages"] == {
        name: {"lockfile": version, "installed": version}
        for name, version in (
            ("next", "16.4.0"),
            ("react", "19.3.0"),
            ("react-dom", "19.3.0"),
        )
    }


@pytest.mark.regression
def test_a_lockfile_one_version_off_is_refused(tmp_path):
    repo, tree = checkout_and_ref(tmp_path, ours=lockfile(next_version="16.4.1"))
    with pytest.raises(
        guard.Refused,
        match="package-lock.json in the checkout is not v0.36.0's .it pins next 16.4.1",
    ):
        build.app_dependencies(repo, tree, "shoplab", "v0.36.0")


@pytest.mark.regression
@pytest.mark.parametrize("name", ["next", "react", "react-dom"])
def test_an_installed_package_one_version_off_is_refused(tmp_path, name):
    off = {"next": "16.4.1", "react": "19.3.1", "react-dom": "19.3.1"}[name]
    repo, tree = checkout_and_ref(tmp_path, installed={name: off})
    with pytest.raises(guard.Refused, match=f"has {name} {off} installed"):
        build.app_dependencies(repo, tree, "shoplab", "v0.36.0")


def test_a_missing_lockfile_or_package_is_refused(tmp_path):
    repo, tree = checkout_and_ref(tmp_path / "a", installed={"react-dom": None})
    with pytest.raises(guard.Refused, match="has no react-dom installed"):
        build.app_dependencies(repo, tree, "shoplab", "v0.36.0")
    repo, tree = checkout_and_ref(tmp_path / "b")
    (repo / "demo" / "shoplab" / "package-lock.json").unlink()
    with pytest.raises(
        guard.Refused, match="the checkout has no demo/shoplab/package-lock.json"
    ):
        build.app_dependencies(repo, tree, "shoplab", "v0.36.0")


def test_the_refusal_comes_before_any_copy_or_build(tmp_path, monkeypatch):
    """build_app checks the dependencies after the ref's tree is archived and
    before the checkout's node_modules are copied or next build runs."""
    repo, ref_tree = checkout_and_ref(tmp_path, installed={"next": "16.4.1"})
    (ref_tree / "demo" / "shoplab" / "package.json").write_text("{}")
    tree = tmp_path / "app-shoplab"

    def archive(repo_, commit, target, paths=()):
        import shutil

        shutil.copytree(ref_tree, target)

    copied, built = [], []
    monkeypatch.setattr(build, "archive", archive)
    monkeypatch.setattr(
        build, "clone_app_node_modules", lambda *a, **k: copied.append(a)
    )
    with pytest.raises(guard.Refused, match="has next 16.4.1 installed"):
        build.build_app(
            repo,
            "0" * 40,
            tree,
            "shoplab",
            {"NEXT_PUBLIC_CONTROLVAR_API_URL": API},
            tmp_path / "build.log",
            spawn=lambda *a, **k: built.append(a) or 0,
            ref="v0.36.0",
        )
    assert copied == [] and built == []


def test_the_real_shoplab_lockfile_pins_the_three_packages():
    packages = build._lock_packages(REPO / "demo" / "shoplab" / "package-lock.json")
    for name in build.APP_PINNED_PACKAGES:
        assert (packages.get(f"node_modules/{name}") or {}).get("version"), name


def test_the_manifest_records_each_apps_dependencies():
    source = (ACCEPTANCE / "showcase" / "capture" / "run.py").read_text(
        encoding="utf-8"
    )
    assert '"apps": {' in source and 'versions.get("apps", {}).get(app)' in source
    assert 'app_versions[app] = built["dependencies"]' in source


# --- S3: the render refuses a capture that never evaluated P2, C8 or D84 --------------


@pytest.mark.regression
@pytest.mark.parametrize("name", ["P2", "C8", "D84"])
def test_a_manifest_without_p2_c8_or_d84_is_refused_by_the_render(tmp_path, name):
    from backend.tests.unit.showcase import capture_dirs

    assert name in contract.REQUIRED_CAPTURE_GATES
    whole = capture_dirs.make_capture(tmp_path / "whole")
    contract.validate_capture_dir(whole)
    manifest = capture_dirs.default_manifest()
    del manifest["gates"][name]
    root = capture_dirs.make_capture(tmp_path / "without", manifest=manifest)
    with pytest.raises(
        contract.CaptureRefused, match=f"capture gate {name} is not in the manifest"
    ):
        contract.validate_capture_dir(root)
