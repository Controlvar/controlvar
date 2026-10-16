"""Video 2, "Your first experiment" (#1066): its storyboard, its traffic and its gates.

The numbers the video states are pinned here, against the code that produces
them on the platform: the estimate's sample size and days, the traffic's users
and conversions at the documented seed, and the result the results API gives
for them. Every refusal is shown firing on one planted defect. No browser,
Docker, network or database: the traffic is sent to a stand-in platform that
assigns by the server's hash.
"""

from __future__ import annotations

import http.server
import json
import math
import re
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from showcase import contract
from showcase.capture import config, gates, storyboard, traffic

from backend.app.core.consistent_hash import bucket_of
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.power_calculator_service import sample_size_two_proportions
from backend.app.services.srm_service import compute_srm
from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    binomial_metric_result,
)

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
ACCEPTANCE = REPO / "tests" / "acceptance"
V1 = ACCEPTANCE / "showcase" / "storyboards" / "01-getting-started.yaml"
V2 = ACCEPTANCE / "showcase" / "storyboards" / "02-first-experiment.yaml"

#: Video 1 as approved (D65): the recording between its title and end cards,
#: from the .vtt of the approved render (61.733 s with its 7 s of cards).
V1_RECORDED_MS = 54_733

#: At seed 40, the server's hash and the generator's draws give these (gate 10
#: holds the platform to the same numbers on every recording).
PLANNED = {
    "control": {"users": 31_309, "converting_users": 1_569},
    "treatment": {"users": 31_691, "converting_users": 1_781},
}


def board() -> storyboard.Storyboard:
    return storyboard.load(V2)


def v2_data() -> dict:
    return yaml.safe_load(V2.read_text(encoding="utf-8"))


def beat(words: str) -> storyboard.Beat:
    for scene in board().scenes:
        for item in scene.beats:
            if words in item.cue:
                return item
    raise AssertionError(f"no cue says {words!r}")


def form_key(name: str) -> str:
    """The wizard's ``generateKey`` (frontend/src/components/experiments/new/formState.ts)."""
    return re.sub(r"^_+|_+$", "", re.sub(r"[^a-z0-9]+", "_", name.lower()))[:64]


@pytest.fixture(scope="module")
def plan() -> traffic.Plan:
    made = board()
    assert made.traffic is not None
    return traffic.plan(made.traffic, made.end_state.experiment.key)


# --- The storyboard -----------------------------------------------------------------


def test_the_storyboard_loads_its_captions_are_fixed_and_it_fits_the_video():
    made = board()
    for cue in storyboard.compile_cues(made):
        assert contract.caption_problems(cue["text"]) == []
    assert storyboard.cue_list_bytes(made) == storyboard.cue_list_bytes(
        storyboard.parse(v2_data())
    )
    length = (
        storyboard.estimate_ms(made) + contract.TITLE_CARD_MS + contract.END_CARD_MS
    )
    # Scrolling is not in the estimate: leave room for it under the cap.
    assert contract.MIN_VIDEO_MS + 5_000 <= length <= contract.MAX_VIDEO_MS - 5_000


def test_the_length_estimate_is_within_two_percent_of_video_1s_recording():
    estimate = storyboard.estimate_ms(storyboard.load(V1))
    assert abs(estimate - V1_RECORDED_MS) <= 0.02 * V1_RECORDED_MS, estimate


def test_the_experiment_is_made_with_the_key_the_wizard_gives_its_name():
    made = board()
    assert made.traffic.experiment == made.end_state.experiment.name
    assert form_key(made.end_state.experiment.name) == made.end_state.experiment.key
    typed = [
        step.text
        for scene in made.scenes
        for item in scene.beats
        for step in item.steps
        if isinstance(step, storyboard.Type)
    ]
    assert made.end_state.experiment.name in typed
    assert made.end_state.experiment.winner in typed
    assert made.traffic.event in typed


def test_the_estimate_caption_is_the_calculators_answer_for_the_typed_inputs():
    """Baseline 5%, effect 10%, 80% power, 95% confidence; 10,000 a day, half in the test."""
    typed = [s.text for s in beat("Size it first").steps]
    assert typed == ["5", "10", "10000", "50"]
    per_variant = sample_size_two_proportions(
        p1=0.05, p2=0.05 * 1.10, alpha=0.05, power=0.8, two_tailed=True
    )
    days = math.ceil(per_variant / (10_000 * 0.50 / 2))
    caption = beat("31,234").cue
    assert storyboard.numbers_in(caption) == [f"{per_variant:,}", str(days)]


def test_the_time_card_states_the_traffics_users():
    made = board()
    headline = next(s.card.headline for s in made.scenes if s.card and s.card.headline)
    assert storyboard.numbers_in(headline) == [f"{made.traffic.users:,}"]


def test_the_sdk_card_states_the_simulated_traffics_count_and_rates():
    """D84 (b): the screen says the shoppers are simulated, and how: how many,
    and the rates the generator converts them at."""
    made = board()
    note = next(s.card.note for s in made.scenes if s.traffic == "start")
    assert "simulated" in note
    assert storyboard.numbers_in(note) == [
        f"{made.traffic.users:,}",
        gates._pct(made.traffic.control_rate),
        gates._pct(made.traffic.treatment_rate),
    ]


def test_a_code_card_that_names_a_host_and_port_is_refused():
    data = v2_data()
    card = next(s["card"] for s in data["scenes"] if "code" in s.get("card", {}))
    card["code"]["lines"] = ["  apiUrl: 'http://localhost:8000',"]
    with pytest.raises(storyboard.StoryboardError, match="names a host and port"):
        storyboard.parse(data)


@pytest.mark.parametrize(
    "change, match",
    [
        (lambda d: d["scenes"][1].update(traffic="finish"), "started once"),
        (lambda d: d["scenes"][3].pop("traffic"), "started once"),
        (lambda d: d.pop("traffic"), "storyboard has none"),
        (lambda d: d["scenes"][2].update(open="/experiments"), "exactly one"),
        (lambda d: d["scenes"][1]["card"].update(headline="Two"), "exactly one of"),
    ],
)
def test_a_storyboard_out_of_shape_is_refused(change, match):
    data = v2_data()
    change(data)
    with pytest.raises(storyboard.StoryboardError, match=match):
        storyboard.parse(data)


# --- The traffic at the documented seed ---------------------------------------------


def test_the_traffic_at_the_documented_seed_is_what_the_storyboard_says(plan):
    assert plan.key == "sticky_add_to_cart_bar"
    assert plan.split == PLANNED
    assert sum(arm["users"] for arm in plan.split.values()) == board().traffic.users
    # Both variants reach the sample the estimate asked for.
    assert min(arm["users"] for arm in plan.split.values()) >= 31_234
    assert plan.p_value is not None and plan.p_value < 0.05


def test_the_result_caption_is_what_the_results_api_computes_for_that_traffic(plan):
    """The engine's own functions on the planned counts: the lift, the p-value, the winner."""
    variants = [
        (BinomialVariant("v-control", "Control", True), *_counts(plan, "control")),
        (BinomialVariant("v-sticky", "Sticky bar", False), *_counts(plan, "treatment")),
    ]
    metric = SimpleNamespace(
        id="m-1", name="Conversion", metric_type="conversion", is_primary=True
    )
    result = binomial_metric_result(variants, 0.05, "benjamini_hochberg", metric=metric)
    decision = AnalysisService._summarise_decision(None, [result], True)
    assert decision["recommendation"] == "SHIP_VARIANT"
    assert decision["winning_variant_id"] == "v-sticky"
    reason = decision["recommendation_reason"]
    assert reason == "Sticky bar shows 12.1% improvement on Conversion (p=0.0007)."
    caption = beat("Sticky bar wins").cue
    # "95%" is the analysis line's confidence, the experiment's default.
    assert storyboard.numbers_in(caption) == ["12.1%", "95%"]
    assert storyboard.numbers_in(caption)[0] in storyboard.numbers_in(reason)
    # No sample-ratio warning overrides the recommendation.
    srm = compute_srm(
        {
            "v-control": PLANNED["control"]["users"],
            "v-sticky": PLANNED["treatment"]["users"],
        },
        {"v-control": 50, "v-sticky": 50},
    )
    assert srm is not None and not srm.warning


def _counts(made: traffic.Plan, arm: str):
    return made.split[arm]["users"], made.split[arm]["converting_users"]


def test_the_generator_is_this_checkouts(monkeypatch, tmp_path):
    assert traffic.REPO_ROOT in Path(traffic.generator().__file__).resolve().parents
    monkeypatch.setattr(traffic, "REPO_ROOT", tmp_path)
    with pytest.raises(traffic.TrafficFailed, match="not from this checkout"):
        traffic.generator()


# --- The sender, against a stand-in platform ----------------------------------------


class Answer:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body


class Platform:
    """Assigns as the server does (``bucket_of`` over Control then Sticky bar,
    50/50) and stores events; can answer one status at the Nth assign."""

    def __init__(self, key: str, fail_at: int = 0, status: int = 429):
        self.key = key
        self.fail_at = fail_at
        self.status = status
        self.assigns = 0
        self.lock = threading.Lock()
        self.assigned = {}
        self.events = []

    def session(self):
        return self

    def post(self, url, json, headers, timeout):
        assert headers == {"X-API-Key": "made-up-traffic-key-0123456789"}
        with self.lock:
            if url.endswith("/api/v1/tracking/assign"):
                self.assigns += 1
                if self.assigns == self.fail_at:
                    return Answer(self.status, {"detail": "slow down"})
                control = bucket_of(json["user_id"], json["experiment_key"]) < 50
                self.assigned[json["user_id"]] = control
                return Answer(
                    200,
                    {
                        "variant_name": "Control" if control else "Sticky bar",
                        "is_control": control,
                        "assigned": True,
                    },
                )
            assert json["event_type"] == json["event_name"] == "purchase"
            assert json["experiment_key"] == self.key
            self.events.append(json["user_id"])
            return Answer(200, {"id": f"event-{len(self.events)}"})


def small_plan(users: int = 400) -> traffic.Plan:
    made = board().traffic.model_copy(update={"users": users})
    return traffic.plan(made, "sticky_add_to_cart_bar")


def test_the_sender_reports_what_the_platform_answered_and_it_is_the_plan():
    made = small_plan()
    platform = Platform(made.key)
    sender = traffic.Sender(
        made,
        api_url="http://127.0.0.1:28400",
        api_key="made-up-traffic-key-0123456789",
        workers=3,
        session=platform.session,
    )
    sender.start()
    report = sender.finish(timeout=60)
    assert report.sent == 400 and report.mismatches == 0 and report.not_assigned == 0
    assert report.by_arm == made.split
    assert report.names == {"control": "Control", "treatment": "Sticky bar"}
    converting = sum(arm["converting_users"] for arm in made.split.values())
    assert len(set(platform.events)) == converting == report.events


@pytest.mark.regression
def test_a_refused_call_stops_the_traffic_and_never_names_the_key():
    made = small_plan()
    platform = Platform(made.key, fail_at=50, status=429)
    sender = traffic.Sender(
        made,
        api_url="http://127.0.0.1:28400",
        api_key="made-up-traffic-key-0123456789",
        workers=2,
        session=platform.session,
    )
    sender.start()
    with pytest.raises(traffic.TrafficFailed, match="answered 429") as raised:
        sender.finish(timeout=60)
    assert "made-up-traffic-key" not in str(raised.value)
    assert platform.assigns < 400


def test_waiting_for_the_first_users_gives_up_with_the_count():
    made = small_plan(100)
    sender = traffic.Sender(
        made,
        api_url="http://127.0.0.1:28400",
        api_key="made-up-traffic-key-0123456789",
        workers=1,
        session=Platform(made.key).session,
    )
    with pytest.raises(traffic.TrafficFailed, match="0 of 50 users in"):
        sender.wait_for(50, timeout=0.2)  # never started


# --- U2: the numbers in a caption are on the page -----------------------------------


@pytest.mark.parametrize(
    "caption, shown, ok",
    [
        (
            "31,234 users per variant, about 13 days here.",
            "31,234 users per variant 62,468 users in total About 13 days (about 2 weeks).",
            True,
        ),
        # The UX plant: a rounded number the page never shows.
        (
            "31,000 users per variant, about 13 days here.",
            "31,234 users per variant About 13 days.",
            False,
        ),
        # 13 is not found inside 31,234.
        ("About 13 days here.", "31,234 users per variant", False),
        ("Sticky bar wins: 12.1% more purchases.", "11.8% improvement", False),
        ("Two variants, split evenly between users.", "Currently 100%", True),
    ],
)
def test_a_caption_number_the_page_does_not_show_is_refused(caption, shown, ok):
    verdict, _numbers, why = gates.caption_numbers(caption, shown)
    assert verdict is ok
    if not ok:
        assert why.startswith("the caption says")


# --- Gate 10: the experiment the video made ------------------------------------------

REASON = "Sticky bar shows 12.1% improvement on Conversion (p=0.0007)."


def results_for(split: dict, p_value: float, reason: str = REASON) -> dict:
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
                        "variant_id": "v-sticky",
                        "variant_name": "Sticky bar",
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
            "winning_variant_id": "v-sticky",
            "recommendation_reason": reason,
        },
    }


#: The sample-size API's answer for that traffic, planned with the 10% the
#: wizard saved with the experiment (#1257), on the control's observed rate.
OBSERVED = 1_569 / 31_309
SAMPLE_SIZE = {
    "required_sample_size_per_variant": 31_159,
    "current_sample_size_per_variant": 31_309,
    "is_adequate": True,
    "baseline_rate": OBSERVED,
    "baseline_source": "observed",
    "mde": 0.1,
    "mde_source": "experiment",
}
#: The Planned sample card as the page's innerText gives it (the label is
#: drawn in capitals), and the effect line under it.
PLANNED_SHOWN = "PLANNED SAMPLE\nReached\n31,309 of 31,159 per variant"
EFFECT = (
    "To detect a 10% lift on the control's 5.01%. The 10% was set when the"
    " experiment was created."
)


def end_check(**changes):
    end = board().end_state.experiment
    oracle = traffic.generator().fisher_p_value(PLANNED)
    kwargs = {
        "named": [{"key": end.key, "status": "active"}],
        "results": results_for(PLANNED, oracle),
        "planned": PLANNED,
        "report": PLANNED,
        "oracle_p": oracle,
        "shown": " " + REASON + "\n",
        "sample_size": dict(SAMPLE_SIZE),
        "planned_shown": PLANNED_SHOWN,
        "effect_shown": EFFECT + "\n",
    }
    kwargs.update(changes)
    return gates.experiment_end(end, **kwargs)


def test_the_experiment_end_state_passes_when_all_three_counts_and_the_page_agree():
    ok, numbers, why = end_check()
    assert ok, why
    assert numbers["counted"] == PLANNED


@pytest.mark.regression
def test_one_conversion_more_on_the_platform_is_refused():
    """QA's plant for gate 10/2: one variant's conversions changed by one."""
    counted = {
        "control": dict(PLANNED["control"]),
        "treatment": {**PLANNED["treatment"], "converting_users": 1_782},
    }
    oracle = traffic.generator().fisher_p_value(PLANNED)
    ok, _numbers, why = end_check(results=results_for(counted, oracle))
    assert not ok and "users and conversions differ" in why


@pytest.mark.parametrize(
    "changes, words",
    [
        ({"named": []}, "0 experiments are named"),
        ({"named": [{"key": "sticky", "status": "active"}]}, "has key"),
        ({"named": [{"key": "sticky_add_to_cart_bar", "status": "draft"}]}, "status"),
        ({"report": {"control": {"users": 1, "converting_users": 0}}}, "differ"),
        ({"oracle_p": 0.5}, "is not Fisher's"),
        (
            {"shown": "Sticky bar shows 9.9% improvement on Conversion (p=0.0400)."},
            "the page showed",
        ),
        ({"shown": None}, "the page showed"),
        ({"results": None}, "no answer"),
    ],
)
def test_an_end_state_that_disagrees_anywhere_is_refused(changes, words):
    ok, _numbers, why = end_check(**changes)
    assert not ok and words in why


# --- Gate 10: the Planned sample, as the wizard sized it (#1257, QA row 15) ---------


def test_the_planned_sample_is_planned_with_the_mde_typed_in_the_wizard():
    """The end state's MDE is the one typed; the oracle sizes it as the API's
    own function does, at the typed baseline (the estimate's 31,234) and at
    the control's observed rate at the seed (31,159)."""
    typed = [s.text for s in beat("Size it first").steps]
    end = board().end_state.experiment
    assert end.mde == float(typed[1]) / 100 == 0.10
    observed = PLANNED["control"]["converting_users"] / PLANNED["control"]["users"]
    assert observed == OBSERVED
    for baseline, size in ((float(typed[0]) / 100, 31_234), (observed, 31_159)):
        assert gates.planned_sample_oracle(baseline, end.mde) == size
        assert size == sample_size_two_proportions(
            p1=baseline, p2=baseline * 1.10, alpha=0.05, power=0.8, two_tailed=True
        )
    # At the 5% default the card planned with before #1257: four times as many.
    assert gates.planned_sample_oracle(observed, 0.05) == 121_833
    assert gates.planned_card(SAMPLE_SIZE) == (
        "Reached",
        "31,309 of 31,159 per variant",
        EFFECT,
    )
    ok, numbers, why = end_check()
    assert ok, why
    assert numbers["planned_sample"]["card"] == (
        "Planned sample: Reached · 31,309 of 31,159 per variant"
    )
    assert numbers["planned_sample"]["oracle_required"] == 31_159


def test_the_card_text_follows_the_results_pages_own_words():
    """``planned_card`` repeats ExperimentSummary.tsx's sentences: each one is
    in the page's source as written there."""
    source = (
        REPO
        / "frontend"
        / "src"
        / "components"
        / "results"
        / "ResultsDashboard"
        / "ExperimentSummary.tsx"
    ).read_text(encoding="utf-8")
    for template in (
        "`To detect a ${effect} lift${baseline}. The ${effect} was set when the"
        " experiment was created.`",
        "`To detect a ${effect} lift${baseline}.`",
        "`To detect a ${effect} lift (the default: no effect was saved with this"
        " experiment)${baseline}.`",
        "` on the control's ${pct(plannedSample.baseline_rate)}`",
        "` on a ${pct(plannedSample.baseline_rate)} baseline`",
        "`${current.toLocaleString()} of ${required.toLocaleString()} per variant`",
        "`${current.toLocaleString()} per variant, no plan yet`",
        "value: 'Reached'",
        "value: 'Not yet'",
        'label="Planned sample"',
    ):
        assert template in source, template
    assert gates._pct(0.1) == "10%" and gates._pct(OBSERVED) == "5.01%"
    assert gates._pct(0.056) == "5.6%" and gates._pct(0.125) == "12.5%"


def _answer(**changes):
    return {**SAMPLE_SIZE, **changes}


@pytest.mark.regression
@pytest.mark.parametrize(
    "changes, words",
    [
        # QA row 15's plant: the 5% default's plan, from before #1257.
        (
            {
                "sample_size": _answer(
                    mde=0.05, required_sample_size_per_variant=121_833
                )
            },
            "plans for an effect of 0.05",
        ),
        (
            {
                "sample_size": _answer(
                    mde=0.05,
                    mde_source="default",
                    required_sample_size_per_variant=121_833,
                    is_adequate=False,
                )
            },
            "plans for an effect of 0.05 (default)",
        ),
        ({"sample_size": _answer(mde_source="request")}, "(request)"),
        ({"sample_size": _answer(mde_source=None)}, "(None)"),
        (
            {"sample_size": _answer(required_sample_size_per_variant=31_160)},
            "the API plans 31160 users per variant; 31159 detect",
        ),
        (
            {"sample_size": _answer(required_sample_size_per_variant=31_234)},
            "the API plans 31234 users",
        ),
        (
            {"sample_size": _answer(baseline_rate=0.05, baseline_source="request")},
            "starts from 0.05 (request)",
        ),
        ({"sample_size": None}, "the sample-size API gave no answer"),
        # QA row 15's page plant: the card as the 5% default drew it.
        (
            {"planned_shown": "PLANNED SAMPLE\nNot yet\n31,309 of 121,833 per variant"},
            "the page's Planned sample read 'PLANNED SAMPLE · Not yet · 31,309 of"
            " 121,833 per variant'",
        ),
        ({"planned_shown": "PLANNED SAMPLE\nReached"}, "the page's Planned sample"),
        ({"planned_shown": None}, "the page's Planned sample"),
        (
            {"planned_shown": "Planned total\nReached\n31,309 of 31,159 per variant"},
            "the page's Planned sample",
        ),
        (
            {
                "effect_shown": "To detect a 5% lift (the default: no effect was saved"
                " with this experiment) on the control's 5.01%."
            },
            "the page's effect line read",
        ),
        ({"effect_shown": None}, "the page's effect line read"),
        # Consistent, but the plan is not reached: the video says it is.
        (
            {
                "sample_size": _answer(
                    current_sample_size_per_variant=31_000, is_adequate=False
                ),
                "planned_shown": "PLANNED SAMPLE\nNot yet\n31,000 of 31,159 per variant",
            },
            "not Reached",
        ),
    ],
)
def test_a_planned_sample_that_is_not_the_wizards_plan_is_refused(changes, words):
    ok, _numbers, why = end_check(**changes)
    assert not ok and words in why, why


def test_the_capture_asks_the_sample_size_api_and_compares_the_pages_readings(
    monkeypatch,
):
    """``Users.end_state``: the plan as the results page asks for it (the
    server's own inputs) and the beat's readings of the card, into gate 10."""
    from showcase.capture import run

    seen = []

    def answer(url, token=None, body=None):
        seen.append(url.split("/api/v1", 1)[1])
        if "/experiments/" in url:
            return {
                "items": [
                    {
                        "id": "e-1",
                        "name": "Sticky add-to-cart bar",
                        "key": "sticky_add_to_cart_bar",
                        "status": "active",
                    }
                ]
            }
        if url.endswith("/sample-size"):
            return dict(SAMPLE_SIZE)
        return results_for(PLANNED, traffic.generator().fisher_p_value(PLANNED))

    monkeypatch.setattr(run, "_api", answer)
    users = run.Users(None, "http://127.0.0.1:28400", "token-1", None, print)
    users.sender = SimpleNamespace(plan=SimpleNamespace(split=PLANNED, p_value=None))
    users.report = traffic.Report(by_arm=PLANNED)
    end = board().end_state.experiment
    readings = {"recommendation": REASON, "planned": PLANNED_SHOWN, "effect": EFFECT}
    ok, numbers, why = users.end_state(end, readings)
    assert ok, why
    assert "/results/e-1/sample-size" in seen
    assert numbers["planned_sample"]["mde_source"] == "experiment"
    ok, _numbers, why = users.end_state(end, {**readings, "planned": "Not yet"})
    assert not ok and "the page's Planned sample read 'Not yet'" in why


def test_the_end_state_compares_only_readings_a_beat_records():
    data = v2_data()
    data["end_state"]["experiment"]["planned"] = "plan"
    with pytest.raises(
        storyboard.StoryboardError, match="'plan', which no beat records"
    ):
        storyboard.parse(data)


def test_a_winner_the_storyboard_did_not_name_is_refused():
    results = results_for(PLANNED, traffic.generator().fisher_p_value(PLANNED))
    results["summary"]["recommendation"] = "CONTINUE_TESTING"
    ok, _numbers, why = end_check(results=results)
    assert not ok and "CONTINUE_TESTING" in why


def test_video_2_registers_in_the_tool():
    assert (
        config.VIDEOS["02-first-experiment"] == "storyboards/02-first-experiment.yaml"
    )
    assert "U2" in contract.REQUIRED_CAPTURE_GATES


# --- The wiring: the director's U2 check and the code card's check at capture time ---


class Found:
    """A located element with its text."""

    def __init__(self, text: str):
        self.text = text

    def inner_text(self) -> str:
        return self.text


def a_director(tmp_path, forbidden=()):
    from showcase.capture import director, guard

    return director.Director(
        context=object(),
        page=object(),
        frames=tmp_path / "work" / "video" / "capture" / "frames",
        dashboard="http://127.0.0.1:28401",
        api_port=28400,
        dashboard_port=28401,
        redactor=director.redaction.Redactor(),
        forbidden=forbidden,
        work=guard.WorkRoot(tmp_path / "work"),
    )


@pytest.mark.regression
def test_the_director_refuses_a_caption_number_its_focus_does_not_show(tmp_path):
    from showcase.capture import director

    made = a_director(tmp_path)
    made.where = "scene setup beat 6"
    with pytest.raises(director.CaptureFailed, match="U2: scene setup beat 6"):
        made.check_numbers(
            "31,000 users per variant, about 13 days here.",
            Found("31,234 users per variant About 13 days (about 2 weeks)."),
        )
    made.check_numbers(
        "31,234 users per variant, about 13 days here.",
        Found("31,234 users per variant About 13 days (about 2 weeks)."),
    )
    made.check_numbers("Review it, then create it.", Found("no number at all"))
    assert (made.tally.number_checks, made.tally.numbered_cues) == (3, 2)


# --- never_show: the experiment page's "assign a user" panel stays off camera ----

PANEL = "Assign a user from your app"


class Page:
    """A page whose viewport shows *drawn*, with a clean header and no alert."""

    def __init__(self, drawn: str):
        self.drawn = drawn

    def evaluate(self, script, *args):
        from showcase.capture import director

        if script == director.HEADER_JS:
            return {"wrapped": [], "cut": []}
        if script == director.VIEWPORT_TEXT_JS:
            return self.drawn
        if script == director.BROKEN_JS:
            return {"overlay": False, "alerts": []}
        raise AssertionError("an unexpected script")


class Focus(Found):
    """A located element inside the page area, on top at its centre."""

    def bounding_box(self):
        return {"x": 100.0, "y": 100.0, "width": 200.0, "height": 40.0}

    def evaluate(self, script, *args):
        return True


def test_never_show_keeps_the_assign_panel_out_of_every_cue(tmp_path):
    from showcase.capture import director, run
    from showcase.capture.stack import Ports

    page_source = REPO / "frontend" / "src" / "pages" / "experiments" / "[id].tsx"
    assert PANEL in page_source.read_text(encoding="utf-8"), "the panel's heading moved"
    assert PANEL in board().never_show
    forbidden = run.forbidden_text(board(), Ports(28400, 28401, 28402))
    assert PANEL in forbidden

    made = a_director(tmp_path, forbidden=forbidden)
    made.where = "scene setup beat 8"
    made.page = Page(f"Sticky add-to-cart bar Active {PANEL} curl -X POST")
    with pytest.raises(
        director.CaptureFailed,
        match=f"U4: scene setup beat 8: the page shows '{PANEL}'",
    ):
        made.check_page(
            made_box(), Focus("Active"), "Start it, and it is ready for shoppers."
        )
    # Without the panel the cue passes, and U4 ran in the same check as U2,
    # whose count gate U2 holds equal to the number of cues.
    made.page = Page("Sticky add-to-cart bar Active")
    made.check_page(
        made_box(), Focus("Active"), "Start it, and it is ready for shoppers."
    )
    assert (made.tally.forbidden_checks, made.tally.number_checks) == (2, 1)


def made_box():
    from showcase.capture import director

    return director.Box(100.0, 100.0, 200.0, 40.0)


# --- The tool's HTTP clients take no proxy settings from the environment ----------


def serve(answer):
    """A local HTTP server answering *answer(path)* as JSON (502 when None); its requests."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _reply(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            seen.append((self.command, self.path, dict(self.headers)))
            body = answer(self.path)
            data = json.dumps(body).encode()
            self.send_response(502 if body is None else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _reply

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.fixture
def proxy(monkeypatch):
    """A listener named by HTTP_PROXY and ALL_PROXY; what it was sent."""
    server, seen = serve(lambda path: None)
    address = f"http://127.0.0.1:{server.server_address[1]}"
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, address)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    # urlopen keeps the first opener it builds; a new process reads the environment.
    monkeypatch.setattr(urllib.request, "_opener", None)
    yield seen
    server.shutdown()
    server.server_close()


@pytest.fixture
def api():
    """The stand-in API: assigns every user to Control, answers any other call."""

    def answer(path):
        if path == "/api/v1/tracking/assign":
            return {"assigned": True, "is_control": True, "variant_name": "Control"}
        return {"total": 3}

    server, seen = serve(answer)
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()
    server.server_close()


def outcome(call):
    try:
        return call()
    except Exception as error:  # what the call raised, compared below
        return error


@pytest.mark.regression
def test_the_traffic_goes_to_the_api_with_a_proxy_in_the_environment(proxy, api):
    url, seen = api
    made = traffic.Plan(
        key="sticky_add_to_cart_bar",
        event="purchase",
        users=[SimpleNamespace(user_id="user-1", variant_name="control")],
        events_for=lambda user, arm: [],
        split={},
        p_value=None,
    )
    sender = traffic.Sender(made, api_url=url, api_key="key-1", workers=1)
    sender.start()
    got = outcome(lambda: sender.finish(timeout=30))
    assert proxy == [], f"the proxy was sent {[(m, p) for m, p, _ in proxy]}"
    assert isinstance(got, traffic.Report), got
    assert got.by_arm == {"control": {"users": 1, "converting_users": 0}}
    assert [(m, p, h.get("X-API-Key")) for m, p, h in seen] == [
        ("POST", "/api/v1/tracking/assign", "key-1")
    ]


@pytest.mark.regression
@pytest.mark.parametrize("client", ["run._api", "stack._get"])
def test_the_tools_own_calls_go_to_the_stack_with_a_proxy_in_the_environment(
    proxy, api, client
):
    from showcase.capture import run, stack

    url, seen = api
    if client == "run._api":
        got = outcome(lambda: run._api(f"{url}/api/v1/users/", "token-1"))
        assert proxy == [], f"the proxy was sent {[(m, p) for m, p, _ in proxy]}"
        assert got == {"total": 3}
        assert seen[0][2].get("Authorization") == "Bearer token-1"
    else:
        got = outcome(lambda: stack._get(f"{url}/health"))
        assert proxy == [], f"the proxy was sent {[(m, p) for m, p, _ in proxy]}"
        assert got == b'{"total": 3}'
    assert len(seen) == 1
