"""Gate 10 for what video 9 sets on the draft's own pages (D182; plan-ui C5,
QA 3a-3d, 4b, 5a-5c, PE 1): sequential testing on, the start the ``date``
step set, the setup's exclusion group and the global holdout the video
makes, each declared in the end state, held both ways, read from the API off
camera, and read back on the draft's page in the product's own words; the
note under the page's settings, word for word; and the browser's pinned zone
and locale, as the page resolves them.

Each property is shown holding on a planted correct answer, then refused on
one planted defect. The product's words are pinned to frontend/src. No
browser, Docker, network or database.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml
from showcase.capture import gates, run, storyboard

from backend.tests.unit.showcase.test_showcase_design_end import (
    DRAWN,
    OWNER,
    SEED,
    board_data,
    full_design,
    saved,
)

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
GROUP = "8c2e1d4a-1111-4222-8333-0000000000a1"
DRAFT = "0b6b4f1e-0000-4000-8000-0000000000e9"
#: The date step's reading at a take on 2026-10-31 (US DST ends 11-01).
DATED = {
    "scheduled-start": {
        "value": "2026-11-02T09:00",
        "instant": "2026-11-02T17:00:00.000Z",
        "shown": "11/2/2026, 9:00:00 AM",
    }
}
SETUP = {
    "segments": [],
    "exclusion_groups": [
        {
            "group": "Checkout tests",
            "id": GROUP,
            "traffic_allocation": 1.0,
            "experiments": ["Checkout Button Color"],
        }
    ],
}
HOLDOUT = {
    "id": "5d1f9a7b-2222-4333-8444-0000000000b2",
    "name": "Q4 holdout",
    "holdout_percentage": 5,
    "is_active": True,
}
NOTE = (
    "Set through the API, not on this page: a paused experiment's resume, turning"
    " sequential testing on or off after creation."
)
#: What the product draws at the payoff for the planted answer, written out by
#: hand (not rendered by the mirrors under test).
PAYOFF = {
    "review-analysis": DRAWN["review-analysis"] + " · Sequential testing (beta) on",
    "detail-sequential": "On",
    "detail-start": "Starts\n11/2/2026, 9:00:00 AM",
    "detail-group": "Checkout tests",
    "detail-holdout": "Q4 holdout: keeps 5% of new users out of every experiment",
    "api-settings-note": NOTE,
}


def board() -> storyboard.Storyboard:
    return storyboard.load(V9)


def answered(**changes: Any) -> Dict[str, Any]:
    """The API's answer for the draft as the video leaves it."""
    answer = dict(
        saved(full_design()),
        sequential_testing_enabled=True,
        sequential_testing_config=None,
        sequential_testing_method=None,
        mutual_exclusion_group_id=GROUP,
        start_date="2026-11-02T17:00:00",
    )
    answer.update(changes)
    return answer


def judge(
    answer: Optional[Dict[str, Any]] = None,
    readings: Optional[Dict[str, str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Dict[str, Any], str]:
    answer = answer or answered()
    return gates.design_end(
        full_design(),
        named=[answer],
        total=SEED + 1,
        seed_experiments=SEED,
        owner_id=OWNER,
        readings={**DRAWN, **PAYOFF, **(readings or {})},
        moved={
            "sequential_testing_enabled": True,
            "start_date": DATED["scheduled-start"]["instant"],
            "mutual_exclusion_group_id": GROUP,
        },
        extra=extra
        if extra is not None
        else {
            "start_shown": DATED["scheduled-start"]["shown"],
            "group_name": "Checkout tests",
            "holdout": HOLDOUT,
        },
    )


# ---------------------------------------------------------------------------
# The real board declares it
# ---------------------------------------------------------------------------


def test_the_real_board_declares_what_it_sets_on_the_drafts_pages():
    end = board().end_state.design
    assert end.analysis.sequential is True
    assert (end.scheduled_start, end.group) == ("scheduled-start", "Checkout tests")
    assert end.holdout.model_dump() == {"name": "Q4 holdout", "percentage": 5}
    assert {k: v for k, v in end.shown.items() if not k.startswith("review_")} == {
        "detail_analysis": "analysis",
        "detail_bayesian": "bayesian",
        "detail_sequential": "detail-sequential",
        "detail_start": "detail-start",
        "detail_group": "detail-group",
        "detail_holdout": "detail-holdout",
        "detail_note": "api-settings-note",
    }
    payoff = board().scenes[0].beats[-1]
    assert payoff.payoff
    assert sorted(payoff.record) == sorted(
        ["detail-sequential", "detail-start", "detail-group", "detail-holdout"]
        + ["api-settings-note"]
    )


# ---------------------------------------------------------------------------
# design_end with what the video set: held both ways, read back word for word
# ---------------------------------------------------------------------------


def test_a_planted_correct_answer_passes_with_what_the_video_set():
    ok, numbers, why = judge()
    assert ok, why
    assert numbers["shown"]["detail_start"] == "Starts 11/2/2026, 9:00:00 AM"
    assert numbers["shown"]["detail_note"] == NOTE


@pytest.mark.parametrize(
    "changes, words",
    [
        ({"sequential_testing_enabled": False}, "has sequential_testing_enabled False"),
        ({"start_date": "2026-11-02T16:00:00"}, "has start_date '2026-11-02T16:00:00'"),
        ({"start_date": None}, "has start_date None"),
        ({"mutual_exclusion_group_id": None}, "has mutual_exclusion_group_id None"),
        ({"end_date": "2026-12-01T00:00:00"}, "has end_date"),
    ],
    ids=["sequential-off", "start-an-hour-off", "no-start", "no-group", "an-end"],
)
def test_what_the_video_set_is_held_both_ways(changes, words):
    ok, _numbers, why = judge(answered(**changes))
    assert not ok and words in why, why


@pytest.mark.parametrize(
    "record, drawn, words",
    [
        ("detail-sequential", "Off", "detail_sequential showed 'Off'"),
        ("detail-start", "Starts\n11/2/2026, 8:00:00 AM", "detail_start showed"),
        (
            "detail-start",
            "Starts\n11/2/2026, 9:00:00 AM (due, not started yet)",
            "detail_start showed",
        ),
        ("detail-group", "None", "detail_group showed 'None'"),
        ("detail-holdout", "None active", "detail_holdout showed 'None active'"),
        (
            "review-analysis",
            DRAWN["review-analysis"],
            "review_analysis showed",
        ),
        (
            "api-settings-note",
            "Set through the API, not on this page: exclusion groups, the global"
            " holdout, a paused experiment's resume, sequential testing.",
            "detail_note showed",
        ),
    ],
    ids=[
        "sequential",
        "start-an-hour-off",
        "start-due",
        "group-none",
        "holdout-none",
        "review-without-sequential",
        "the-old-note",
    ],
)
def test_a_payoff_row_that_does_not_say_what_was_set_is_refused(record, drawn, words):
    ok, _numbers, why = judge(readings={record: drawn})
    assert not ok and words in why, why


@pytest.mark.parametrize("missing", ["start_shown", "group_name", "holdout"])
def test_a_payoff_row_with_no_source_read_is_refused(missing):
    extra = {
        "start_shown": DATED["scheduled-start"]["shown"],
        "group_name": "Checkout tests",
        "holdout": HOLDOUT,
    }
    del extra[missing]
    ok, _numbers, why = judge(extra=extra)
    assert not ok and "no source was read to compare" in why, why


# ---------------------------------------------------------------------------
# eligibility_end: the groups, the group's members and the holdouts
# ---------------------------------------------------------------------------


def group_read(
    members=("Checkout Button Color", "Delivery promise"), name="Checkout tests"
):
    return {
        "id": GROUP,
        "name": name,
        "experiments": [{"id": "x", "name": m, "status": "draft"} for m in members],
    }


def eligibility(**changes: Any) -> Tuple[bool, Dict[str, Any], str]:
    reads: Dict[str, Any] = {
        "groups": {"items": [{"name": "Checkout tests"}], "total": 1},
        "group": group_read(),
        "holdouts": {"items": [HOLDOUT], "total": 1},
        "active": dict(HOLDOUT),
        "saved": answered(),
    }
    reads.update(changes)
    return gates.eligibility_end(full_design(), board().setup, **reads)


def test_eligibility_passes_when_the_api_holds_what_the_video_set():
    ok, numbers, why = eligibility()
    assert ok, why
    assert numbers["members"] == ["Checkout Button Color", "Delivery promise"]
    assert numbers["holdouts_total"] == 1


@pytest.mark.parametrize(
    "changes, words",
    [
        (
            {
                "groups": {
                    "items": [{"name": "Checkout tests"}, {"name": "Other"}],
                    "total": 2,
                }
            },
            "the API holds the groups ['Checkout tests', 'Other'] (2)",
        ),
        (
            {"group": group_read(("Checkout Button Color",))},
            "holds ['Checkout Button Color']",
        ),
        ({"group": group_read(("Delivery promise",))}, "holds ['Delivery promise']"),
        (
            {"group": group_read(("Checkout Button Color", "Delivery promise", "MAB"))},
            "holds ['Checkout Button Color', 'Delivery promise', 'MAB']",
        ),
        ({"group": None}, "'Checkout tests' could not be read"),
        (
            {"holdouts": {"items": [HOLDOUT, HOLDOUT], "total": 2}},
            "the API holds 2 holdouts",
        ),
        ({"holdouts": {"items": [], "total": 0}}, "the API holds 0 holdouts"),
        ({"active": dict(HOLDOUT, is_active=False)}, "the active holdout is"),
        ({"active": dict(HOLDOUT, holdout_percentage=10)}, "the active holdout is"),
        ({"active": None}, "the active holdout is None"),
        (
            {"saved": answered(sequential_testing_config={"alpha": 0.05})},
            "sequential_testing_config is",
        ),
        (
            {"saved": answered(sequential_testing_method="msprt")},
            "sequential_testing_method is 'msprt'",
        ),
    ],
    ids=[
        "a-second-group",
        "members-checkout-only",
        "members-draft-only",
        "a-third-member",
        "group-unread",
        "two-holdouts",
        "no-holdout",
        "holdout-inactive",
        "holdout-at-10",
        "no-active",
        "sequential-config-set",
        "sequential-method-set",
    ],
)
def test_eligibility_refuses_each_planted_state(changes, words):
    ok, _numbers, why = eligibility(**changes)
    assert not ok and words in why, why


def test_nothing_is_judged_for_a_field_the_end_state_does_not_declare():
    from backend.tests.unit.showcase.test_showcase_design_end import design

    ok, numbers, why = gates.eligibility_end(
        design(), None, groups=None, group=None, holdouts=None, active=None, saved={}
    )
    assert ok and numbers == {}, why


# ---------------------------------------------------------------------------
# The zone and the dates (QA 5a-5c)
# ---------------------------------------------------------------------------


def test_the_pinned_zone_and_locale_pass_and_another_is_refused():
    ok, _numbers, why = gates.zone_end(
        "America/Los_Angeles", "en-US", "America/Los_Angeles", "en-US"
    )
    assert ok, why
    # QA 5a's plant: a board declaring another zone, the context left at the
    # machine's (a Los Angeles zone planted on a Los Angeles Mac proves nothing).
    ok, numbers, why = gates.zone_end(
        "Europe/London", "en-GB", "America/Los_Angeles", "en-US"
    )
    assert not ok
    assert (
        why
        == "the page's zone is America/Los_Angeles; the board declares Europe/London"
    )
    assert numbers["resolved"] == ["America/Los_Angeles", "en-US"]
    ok, _numbers, why = gates.zone_end(
        "America/Los_Angeles", "en-US", "America/Los_Angeles", "en-GB"
    )
    assert not ok and why == "the page's locale is en-GB; the board declares en-US"


def test_a_local_start_is_read_with_its_own_days_offset():
    """QA 5b: 09:00 on 11-02 in Los Angeles is 17:00Z (PST), though the take is
    filmed on 10-31, when the offset is PDT's: the offset at "now" would give
    16:00Z."""
    assert gates.instant_in_zone("2026-11-02T09:00", "America/Los_Angeles") == (
        "2026-11-02T17:00:00.000Z"
    )
    assert gates.instant_in_zone("2026-10-31T09:00", "America/Los_Angeles") == (
        "2026-10-31T16:00:00.000Z"
    )
    assert gates.instant_in_zone("not a time", "America/Los_Angeles") is None


def test_the_dates_pass_in_their_zone():
    ok, _numbers, why = gates.dates_in_zone(DATED, "America/Los_Angeles", "en-US")
    assert ok, why


@pytest.mark.parametrize(
    "change, words",
    [
        ({"instant": "2026-11-02T16:00:00.000Z"}, "in America/Los_Angeles it is"),
        ({"shown": "11/2/2026, 4:00:00 PM"}, "the page drew the date"),
        ({"shown": "11/3/2026, 9:00:00 AM"}, "the page drew the date"),
        ({"shown": "02/11/2026, 09:00:00"}, "the page drew the date"),
    ],
    ids=["instant-an-hour-off", "drawn-in-utc", "drawn-a-day-off", "drawn-en-gb"],
)
def test_a_date_off_in_the_pinned_zone_is_refused(change, words):
    dated = {"scheduled-start": dict(DATED["scheduled-start"], **change)}
    ok, _numbers, why = gates.dates_in_zone(dated, "America/Los_Angeles", "en-US")
    assert not ok and words in why, why
    ok, _numbers, why = gates.dates_in_zone(DATED, "Europe/London", "en-GB")
    assert not ok and "is not read here" in why


def test_a_start_drawn_with_a_narrow_space_is_read():
    assert gates.shown_as_value("11/2/2026, 9:00:00 AM", "2026-11-02T09:00")
    assert gates.shown_as_value("11/2/2026, 12:30:00 PM", "2026-11-02T12:30")
    assert not gates.shown_as_value("11/2/2026, 9:00:30 AM", "2026-11-02T09:00")


def test_the_capture_checks_the_zone_before_any_scene_and_records_it():
    source = Path(run.__file__).read_text(encoding="utf-8")
    body = source.split("def record(", 1)[1].split("\ndef ", 1)[0]
    assert body.index("judge.zone_end(") < body.index("for scene in board.scenes:")
    assert 'raise directing.CaptureFailed("gate 10", zone[2])' in body
    assert 'numbers["zone"] = zone[1]' in body
    assert run.RESOLVED_ZONE_JS == (
        "() => [Intl.DateTimeFormat().resolvedOptions().timeZone, navigator.language]"
    )
    assert "dated=director.dates" in body and "setup=setup" in body


# ---------------------------------------------------------------------------
# design_state: what gate 10 reads from the API for what the video set
# ---------------------------------------------------------------------------


def test_design_state_reads_the_group_and_the_holdouts_off_camera(monkeypatch):
    end = full_design()
    asked: List[str] = []
    answers = {
        "/api/v1/experiments/?limit=100": {
            "items": [{"id": DRAFT, "name": end.name}],
            "total": SEED + 1,
        },
        f"/api/v1/experiments/{DRAFT}": answered(),
        "/api/v1/auth/me": {"id": OWNER},
        "/api/v1/mutual-exclusion-groups": {
            "items": [{"name": "Checkout tests"}],
            "total": 1,
        },
        f"/api/v1/mutual-exclusion-groups/{GROUP}": group_read(),
        "/api/v1/holdout/all": {"items": [HOLDOUT], "total": 1},
        "/api/v1/holdout": dict(HOLDOUT),
    }

    def answer(url, token=None, body=None):
        path = url[len("http://api") :]
        asked.append(path)
        return copy.deepcopy(answers[path])

    monkeypatch.setattr(run, "_api", answer)
    readings = {**DRAWN, **PAYOFF}
    ok, numbers, why = run.design_state(
        end, "http://api", "t", readings, SEED, board=board(), setup=SETUP, dated=DATED
    )
    assert ok, why
    assert asked == list(answers)
    assert numbers["moved"] == {
        "sequential_testing_enabled": True,
        "start_date": "2026-11-02T17:00:00.000Z",
        "mutual_exclusion_group_id": GROUP,
    }
    # The group's name on the page is compared with the group's own, as read.
    answers[f"/api/v1/mutual-exclusion-groups/{GROUP}"] = group_read(name="Renamed")
    ok, _numbers, why = run.design_state(
        end, "http://api", "t", readings, SEED, board=board(), setup=SETUP, dated=DATED
    )
    assert (
        not ok
        and "detail_group showed 'Checkout tests'; the API saved 'Renamed'" in why
    )


# ---------------------------------------------------------------------------
# What the loader refuses
# ---------------------------------------------------------------------------


def refused_at_load(change, words: str) -> None:
    data = board_data()
    change(data["end_state"]["design"])
    with pytest.raises(storyboard.StoryboardError, match=words):
        storyboard.parse(data)


def test_a_start_no_date_step_records_is_refused():
    refused_at_load(
        lambda d: d.update(scheduled_start="another-start"),
        "starts at the reading 'another-start', which no date step records",
    )


def test_a_group_the_setup_does_not_make_is_refused():
    refused_at_load(
        lambda d: d.update(group="Pricing tests"),
        "puts it in 'Pricing tests', which the setup does not make",
    )


@pytest.mark.parametrize("percentage", [0, 21])
def test_a_holdout_outside_1_to_20_is_refused(percentage):
    refused_at_load(lambda d: d["holdout"].update(percentage=percentage), "percentage")


@pytest.mark.parametrize(
    "kind, field",
    [
        ("detail_start", "scheduled_start"),
        ("detail_group", "group"),
        ("detail_holdout", "holdout"),
    ],
)
def test_a_reading_of_what_was_set_without_its_field_is_refused(kind, field):
    def without(design):
        design.pop(field)

    refused_at_load(without, f"compares {kind}, and declares no {field}")


# ---------------------------------------------------------------------------
# The product's words, pinned to frontend/src
# ---------------------------------------------------------------------------


def source(relative: str) -> str:
    return (FRONTEND / relative).read_text(encoding="utf-8")


def test_the_holdout_label_is_the_products():
    text = source("utils/holdouts.ts")
    assert (
        "return `${holdout.name}: keeps ${holdout.holdout_percentage}% of new users"
        " out of every experiment`;" in text
    )
    assert gates.holdout_label("Q4 holdout", 5) == PAYOFF["detail-holdout"]


def test_the_start_term_is_the_products():
    text = source("pages/experiments/[id].tsx")
    assert "term: which === 'start' ? 'Starts' : 'Ends'," in text
    assert gates.START_TERM == "Starts"


def note_on_the_page() -> str:
    """The experiment page's settings note as it renders: the <p> with its test
    id, its tags and JSX spacing taken out, entities decoded."""
    text = source("pages/experiments/[id].tsx")
    found = re.search(
        r'<p [^>]*data-testid="experiment-api-settings-note"[^>]*>(.*?)</p>', text, re.S
    )
    assert found, "the experiment page has no settings note"
    body = found.group(1).replace("{' '}", " ")
    body = re.sub(r"<[^>]+>", "", body).replace("&apos;", "'")
    body = " ".join(body.split())
    return body.replace(" ,", ",").replace(" .", ".")


def test_the_settings_note_is_the_products_word_for_word():
    """PE 1, EM-3: the payoff reads the note whole, and it says exactly this."""
    assert note_on_the_page() == gates.API_SETTINGS_NOTE == NOTE


def test_a_note_that_still_lists_what_the_video_set_is_refused():
    """PE 1's plant: a merge that kept "exclusion groups, the global holdout"."""
    old = (
        "Set through the API, not on this page: exclusion groups, the global holdout,"
        " a paused experiment's resume, sequential testing."
    )
    assert old != gates.API_SETTINGS_NOTE
    ok, _numbers, why = judge(readings={"api-settings-note": old})
    assert not ok and why.startswith("detail_note showed"), why


def test_the_board_records_every_reading_its_end_state_compares():
    made = yaml.safe_load(V9.read_text(encoding="utf-8"))
    recorded = {
        name
        for beat in made["scenes"][0]["beats"]
        for name in (beat.get("record") or {})
    }
    assert set(made["end_state"]["design"]["shown"].values()) <= recorded
