"""Video 11, Find the flags you can remove (#1889): its board, the setup that
makes its flags and dates them back (S1 of the V11-V13 plan), and gate 10's
stale end state (S2).

* The board: registered after video 5 (no pin on the order moves), the seed
  and its counts unchanged, the browser's zone and locale pinned (PE 6: the
  Updated column is ``toLocaleDateString``), the departures in the first
  caption, the kill switch's beat and the archive card cut (EMV11-6, check
  fix 6), and the narration in the page's own words.
* S1 at load: at most six flags, keys the API takes and the seed does not
  use, ages clear of N, updated no earlier than created, and the end state
  derived from the flags' fields by the docs' table (``derived_stale``),
  which is held equal to the server's own classifier here.
* S1 at run time (``run.make_flags``): the calls in order, each with its
  status; the key minted and kept a needle with its name; the evaluations
  with ``X-API-Key`` (check fix 5); the dating step's SQL built from checked
  values only; the read-back; ``?stale=true`` judged before the first frame.
* S2 (``gates.stale_end``): the API's list and the page's rows, hint and
  counts, after the last frame.

Each property is shown holding, then refused on one planted defect. No
browser, Docker, network or database.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest
import yaml
from showcase import contract
from showcase.capture import compose, config, gates, offcamera, run, storyboard, voiced
from showcase.capture import stack as stacks

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
SHOWCASE = REPO / "tests" / "acceptance" / "showcase"
SLUG = "11-stale-flags"
SOURCE = f"{SLUG}.yaml"
FRONTEND = REPO / "frontend" / "src"
LIST_PAGE = FRONTEND / "pages" / "feature-flags" / "index.tsx"
NO_ARCHIVE_TEST = FRONTEND / "tests" / "pages" / "feature-flag-key-and-archive.test.tsx"
SEED_SCRIPT = REPO / "backend" / "scripts" / "seed_demo_data.py"

#: The run's admin token and the key the setup makes, as test values.
TOKEN = "test-admin-token-not-a-real-one"
KEY = "setup-key-for-this-test-only-0123"
API = "http://127.0.0.1:28400"
NOW = dt.datetime(2026, 11, 11, 4, 0, tzinfo=dt.timezone.utc)
FLAG_IDS = {
    "payments-kill-switch": "0b6b4f1e-0000-4000-8000-000000000111",
    "legacy-signup-form": "0b6b4f1e-0000-4000-8000-000000000112",
    "holiday-banner": "0b6b4f1e-0000-4000-8000-000000000113",
    "old-search-ranking": "0b6b4f1e-0000-4000-8000-000000000114",
}
STALE = {
    "legacy-signup-form": ["on_for_everyone"],
    "holiday-banner": ["off_for_everyone"],
    "old-search-ranking": ["not_evaluated"],
}


def board() -> storyboard.Storyboard:
    return storyboard.load(SHOWCASE / config.VIDEOS[SLUG])


def raw() -> Dict[str, Any]:
    return yaml.safe_load((SHOWCASE / config.VIDEOS[SLUG]).read_text(encoding="utf-8"))


def refused(change: Callable[[Dict[str, Any]], Any], reason: str) -> str:
    data = raw()
    change(data)
    with pytest.raises(storyboard.StoryboardError) as raised:
        storyboard.parse(copy.deepcopy(data), source=SOURCE)
    assert reason in str(raised.value), str(raised.value)
    return str(raised.value)


def beats() -> List[storyboard.Beat]:
    return list(board().scenes[0].beats)


def squeezed(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------


def test_video_11_registers_after_video_5_and_moves_no_pin_on_the_order():
    order = list(config.VIDEOS)
    assert config.VIDEOS[SLUG] == "storyboards/11-stale-flags.yaml"
    # Between videos 5 and 6, the one slot that moves none of the pins below;
    # video 12 holds the place right after video 5 when both are registered.
    assert order.index("05-plan-and-team") < order.index(SLUG)
    assert order.index(SLUG) < order.index("06-shoplab-ab-test")
    # The pins on the order stand unedited.
    assert order.index("05-plan-and-team") == 3
    assert order.index("10-serve-a-value") + 1 == order.index("09-design-in-depth")
    assert order[order.index("10-serve-a-value") - 1] == "06-shoplab-ab-test"
    assert order[-2:] == ["09-design-in-depth", "04-streampulse-rollout"]
    made = board()
    assert made.slug == SLUG and made.title == "Find the flags you can remove"
    assert made.seed == "demo" and made.apps == [] and made.traffic is None
    from showcase.capture import overrides as override_lists

    assert override_lists.load()[SLUG] == {}


def test_the_seed_and_its_counts_are_unchanged():
    """#1889: the demo data and its counts do not change; the setup's flags
    are made after gate 14 counts the seed's."""
    assert board().seed_counts.model_dump() == {
        "experiments": 3,
        "feature_flags": 2,
        "users": 4,
    }


#: PE 6: the browser the take's list is drawn in.
PINNED = {"timezone_id": "America/Los_Angeles", "locale": "en-US"}


def pin_problems(made: storyboard.Storyboard) -> List[str]:
    """Why the take's Updated column could be drawn otherwise, or nothing."""
    if run.context_zone(made) != PINNED:
        return [f"the browser runs in {run.context_zone(made)}, not {PINNED}"]
    return []


def test_the_board_pins_the_zone_and_locale_the_updated_column_is_drawn_in():
    """PE 6: the list's Updated column is the browser's toLocaleDateString."""
    assert "d.toLocaleDateString()" in LIST_PAGE.read_text(encoding="utf-8")
    assert pin_problems(board()) == []


def test_a_board_with_the_pin_removed_is_refused():
    data = raw()
    del data["time_zone"], data["locale"]
    assert pin_problems(storyboard.parse(data, source=SOURCE)) == [
        f"the browser runs in {{}}, not {PINNED}"
    ]


def test_the_first_caption_says_the_sign_in_and_the_setup():
    """#1889: four flags made off camera and dated back, said on screen; the
    number said as a word is the setup's count of flags."""
    made = board()
    cue = made.scenes[0].beats[0].cue
    assert cue == "Signed in off camera. Four flags made off camera, dated weeks back."
    assert len(made.setup.flags) == 4
    assert storyboard.required_departures(made) == [("flags", "off-camera setup")]
    assert [d.says for d in made.scenes[0].departures] == [
        "Signed in off camera",
        "Four flags made off camera, dated weeks back",
    ]
    assert storyboard.departure_problems(made) == []


def test_the_plans_kill_switch_caption_is_refused_on_length_so_its_beat_is_cut():
    """EMV11-6 and PE 5: the opening caption would say a flag was tagged
    permanent through the API; that caption is refused on length, so the
    kill switch's beat is cut. No beat focuses on it or names it; it is
    still made, and gate 10 holds it not stale."""
    plan = (
        "Signed in off camera. Four flags made off camera, dated weeks back. One"
        " is tagged permanent through the API."
    )
    assert "19 words; at most 12" in storyboard.caption_problem(plan)
    made = board()
    said = " ".join(s for b in beats() for s in [b.cue, *b.say]).lower()
    assert "kill switch" not in said and "payments" not in said
    for beat in beats():
        assert "payments-kill-switch" not in (beat.focus.css or "")
    assert made.end_state.stale.not_stale == ["payments-kill-switch"]
    kill = next(f for f in made.setup.flags if f.key == "payments-kill-switch")
    assert storyboard.is_permanent(kill.tags)


def test_the_archive_card_is_cut_and_the_dashboard_archive_pin_is_named():
    """Check fix 6: no card on any scene. The dashboard now has an archive
    action (#442), and the test that pins it is still there; the card stays
    cut, which is a plan decision, not a gap in the dashboard."""
    assert all(scene.card is None for scene in board().scenes)
    pinned = NO_ARCHIVE_TEST.read_text(encoding="utf-8")
    assert "archiving (#442): the flag's page offers" in squeezed(pinned)


def hint_beat(made: Optional[storyboard.Storyboard] = None) -> storyboard.Beat:
    """The one beat that looks at the hint the page draws with Stale pressed."""
    (beat,) = [
        b
        for b in (made or board()).scenes[0].beats
        if any(
            isinstance(s, storyboard.Look) and s.look.testid == "flag-stale-hint"
            for s in b.steps
        )
    ]
    return beat


#: The words said only with the hint in frame: "archive" only where the page
#: draws it (the plan's banned-word row), and the cut kill-switch beat's
#: words, permanent and tags, only where the hint says them.
HINT_WORDS = ("archive", "permanent", "tag")


def said_away_from_the_hint(made: storyboard.Storyboard) -> List[str]:
    hint = hint_beat(made)
    return [
        sentence
        for beat in made.scenes[0].beats
        if beat is not hint
        for sentence in beat.say
        if any(word in sentence.lower() for word in HINT_WORDS)
    ]


def test_archive_permanent_and_tags_are_said_only_with_the_hint_on_screen():
    assert said_away_from_the_hint(board()) == []
    for word in HINT_WORDS:
        assert any(word in s.lower() for s in hint_beat().say), word
        assert word in gates.STALE_HINT.lower()


def test_archive_said_away_from_the_hint_is_refused():
    data = raw()
    data["scenes"][0]["beats"][4]["say"].append("Nothing is archived for you.")
    made = storyboard.parse(data, source=SOURCE)
    assert said_away_from_the_hint(made) == ["Nothing is archived for you."]


def row_word_problems(made: storyboard.Storyboard) -> List[str]:
    """PE 6: one beat per stale row, focused on that row, whose first sentence
    names the flag as the setup names it and holds the reason line the page
    draws for it (N the API's 30); the problems, or nothing."""
    problems = []
    end = made.end_state.stale
    names = {flag.key: flag.name for flag in made.setup.flags}
    for key, reasons in end.stale.items():
        found = [
            b
            for b in made.scenes[0].beats
            if b.focus.css == f"[data-testid=flag-row][data-flag-key={key}]"
        ]
        if len(found) != 1:
            problems.append(f"{len(found)} beats focus on {key}'s row")
            continue
        if not found[0].say[0].startswith(names[key]):
            problems.append(f"{key}'s beat does not name {names[key]!r}")
        for reason in reasons:
            words = gates.stale_reason_text(reason, end.days)
            if words.lower() not in found[0].say[0].lower():
                problems.append(f"{key}'s beat does not say {words!r}")
    return problems


def test_each_stale_rows_beat_says_the_pages_words_for_its_reason():
    assert row_word_problems(board()) == []
    keys = [b.focus.css.split("data-flag-key=")[1].rstrip("]") for b in beats()[1:4]]
    assert keys == list(STALE)


def test_a_row_beat_that_says_other_words_is_refused():
    data = raw()
    data["scenes"][0]["beats"][1]["say"][0] = (
        "Legacy signup form has been on for 30 days."
    )
    assert row_word_problems(storyboard.parse(data, source=SOURCE)) == [
        "legacy-signup-form's beat does not say"
        " 'On for everyone, unchanged for 30 days'"
    ]


def test_the_hint_is_said_in_the_pages_words_split_at_its_semicolon():
    """PE 6: the hint's two sentences, the second split at its semicolon
    because a spoken sentence is at most 12 words; U's own line is not said."""
    said = hint_beat().say[:3]
    assert said == [
        "Stale is a suggestion: nothing is changed or archived for you.",
        "A flag tagged permanent is never stale.",
        "Tags are set through the API.",
    ]
    joined = " ".join(said).replace("stale. Tags", "stale; tags")
    assert joined == gates.STALE_HINT
    whole = "A flag tagged permanent is never stale; tags are set through the API."
    assert "13 words; at most 12" in storyboard.caption_problem(whole)


def what_changes(made: storyboard.Storyboard) -> List[Dict[str, Any]]:
    """Every step that could change something on the page: each click, and
    any type, choose, slide or date step."""
    found = []
    for beat in made.scenes[0].beats:
        for step in [*beat.before, *beat.steps, *beat.exit]:
            if isinstance(step, storyboard.Click):
                found.append(step.click.model_dump(exclude_defaults=True))
            elif isinstance(step, storyboard.ENTERS_DATA):
                found.append({"enters": type(step).__name__})
    return found


#: Listing changes no flag (S2): the one change on camera presses the Stale
#: filter, by its role and name (its test id is built at run time).
STALE_BUTTON = {"role": "button", "name": "Stale", "exact": True}


def test_the_board_changes_no_flag_on_camera():
    assert what_changes(board()) == [STALE_BUTTON]
    source = LIST_PAGE.read_text(encoding="utf-8")
    assert "{ label: 'Stale', value: 'stale' }" in source


def test_a_toggle_clicked_on_camera_is_refused():
    data = raw()
    data["scenes"][0]["beats"][1]["steps"].append(
        {"click": {"role": "switch", "name": "Turn legacy-signup-form off"}}
    )
    assert what_changes(storyboard.parse(data, source=SOURCE)) != [STALE_BUTTON]


def test_the_filtered_list_is_read_only_after_the_table_has_lost_the_kill_switch():
    """The hint is drawn as soon as Stale is pressed, before the list reloads:
    the cut waits for it and for the table without the kill switch's row, so
    the rows read are the filtered list's."""
    after = beats()[5]
    waits = [s.wait_for for s in after.before if isinstance(s, storyboard.WaitFor)]
    assert [w.testid or w.css for w in waits] == [
        "flag-stale-hint",
        "[data-testid=flags-table]:not(:has([data-flag-key=payments-kill-switch]))",
    ]
    assert beats()[4].exit[0].click.name == "Stale"


def test_the_narration_is_15_sentences_and_the_voiced_projection_is_in_range():
    """Video 9's fitted clip model (0.376 s + 0.0664 s a character; p90 0.406
    s more), as the plan sizes a board before its voice sitting, projected as
    the capture projects it (``voiced.projected_ms``: the title and end cards
    included), since the 55 s floor and the cap are both on that length."""
    made = board()
    lines = storyboard.narration_lines(made)
    assert len(lines) == 15

    def clips(extra: float) -> List[voiced.Clip]:
        return [
            voiced.Clip(
                scene=scene,
                beat=beat,
                line=line,
                text=text,
                key="",
                path=Path(),
                samples=int((0.376 + 0.0664 * len(text) + extra) * contract.VOICE_RATE),
                sha256="",
            )
            for scene, beat, line, text in lines
        ]

    central = voiced.projected_ms(made, clips(0.0))
    p90 = voiced.projected_ms(made, clips(0.406))
    cap = round(contract.max_video_ms(SLUG) * contract.PROJECTION_SHARE)
    assert contract.MIN_VIDEO_MS < central < p90 < cap, (central, p90, cap)


def test_the_silent_estimate_is_under_the_floor_and_the_take_decides():
    """S5 (EMV11-8): no padding. The silent take is under 55 s and exits 1
    with the length line, accepted only because the voiced take follows."""
    assert storyboard.estimate_ms(board()) < contract.MIN_VIDEO_MS


# ---------------------------------------------------------------------------
# S1 and S2 at load
# ---------------------------------------------------------------------------


def flag(data: Dict[str, Any], key: str) -> Dict[str, Any]:
    return next(f for f in data["setup"]["flags"] if f["key"] == key)


def test_the_setup_makes_the_four_flags_with_no_rules_or_schedule():
    made = board()
    assert [f.model_dump() for f in made.setup.flags] == [
        {
            "name": "Payments kill switch",
            "key": "payments-kill-switch",
            "status": "active",
            "rollout": 100,
            "tags": ["permanent"],
            "evaluate": True,
            "created_days_ago": 120,
            "updated_days_ago": 120,
        },
        {
            "name": "Legacy signup form",
            "key": "legacy-signup-form",
            "status": "active",
            "rollout": 100,
            "tags": [],
            "evaluate": True,
            "created_days_ago": 90,
            "updated_days_ago": 60,
        },
        {
            "name": "Holiday banner",
            "key": "holiday-banner",
            "status": "inactive",
            "rollout": 0,
            "tags": [],
            "evaluate": True,
            "created_days_ago": 75,
            "updated_days_ago": 50,
        },
        {
            "name": "Old search ranking",
            "key": "old-search-ranking",
            "status": "active",
            "rollout": 30,
            "tags": [],
            "evaluate": False,
            "created_days_ago": 60,
            "updated_days_ago": 50,
        },
    ]
    assert made.setup.segments == [] and made.setup.exclusion_groups == []
    stale = made.end_state.stale
    assert {k: list(v) for k, v in stale.stale.items()} == STALE
    assert (stale.days, stale.evaluations_seen) == (30, True)
    assert storyboard.derived_stale(list(made.setup.flags)) == (STALE, True)


@pytest.mark.parametrize(
    "change, reason",
    [
        (
            lambda d: flag(d, "holiday-banner").update(updated_days_ago=80),
            "holiday-banner is updated 80 days ago, before it was created",
        ),
        (
            lambda d: flag(d, "holiday-banner").update(updated_days_ago=45),
            "holiday-banner's updated_days_ago is 45: within 15 days of N (30)",
        ),
        (
            lambda d: flag(d, "old-search-ranking").update(
                created_days_ago=31, updated_days_ago=10
            ),
            "old-search-ranking's created_days_ago is 31: within 15 days",
        ),
        (
            lambda d: flag(d, "old-search-ranking").update(key="new_dashboard_ui"),
            "the setup makes new_dashboard_ui, a key the demo seed's flag has",
        ),
        (
            lambda d: flag(d, "old-search-ranking").update(key="old'; DROP--"),
            "String should match pattern",
        ),
        (
            lambda d: flag(d, "old-search-ranking").update(key="Old-Search"),
            "String should match pattern",
        ),
        (
            lambda d: flag(d, "old-search-ranking").update(rules={"groups": []}),
            "Extra inputs are not permitted",
        ),
        (
            lambda d: flag(d, "holiday-banner").update(key="legacy-signup-form"),
            "a setup flag's key or name repeats",
        ),
        (
            lambda d: d["setup"]["flags"].extend(
                copy.deepcopy(d["setup"]["flags"][3:]) for _ in range(3)
            ),
            "at most 6 items",
        ),
    ],
    ids=[
        "updated-before-created",
        "updated-near-n",
        "created-near-n",
        "a-seed-key",
        "a-quote-in-the-key",
        "an-uppercase-key",
        "a-rule",
        "a-key-twice",
        "seven-flags",
    ],
)
def test_a_setup_flag_s1_refuses_at_load(change, reason):
    refused(change, reason)


@pytest.mark.parametrize(
    "change, reason",
    [
        (
            lambda d: d["end_state"]["stale"]["stale"].update(
                {"holiday-banner": ["on_for_everyone"]}
            ),
            "the docs' table gives",
        ),
        (
            lambda d: flag(d, "payments-kill-switch").update(tags=["payments"]),
            "the docs' table gives",
        ),
        (
            lambda d: flag(d, "old-search-ranking").update(evaluate=True),
            "the docs' table gives",
        ),
        (
            lambda d: d["end_state"]["stale"].update(days=1),
            "a STALE_FLAG_DAYS of its own is refused",
        ),
        (
            lambda d: d["end_state"]["stale"].update(evaluations_seen=False),
            "evaluations_seen False; the setup's evaluations give True",
        ),
        (
            lambda d: [f.update(evaluate=False) for f in d["setup"]["flags"]],
            "the setup evaluates none of its flags",
        ),
        (
            lambda d: flag(d, "holiday-banner").update(name="Stale holiday banner"),
            "names 'stale'",
        ),
        (
            lambda d: d["end_state"]["stale"]["stale"].update(
                {"old-search-ranking": ["not_evaluated", "not_evaluated"]}
            ),
            "are not each once, in the API's order",
        ),
        (
            lambda d: d["end_state"]["stale"].update(not_stale=[]),
            "the rows read before Stale is pressed are",
        ),
        (
            lambda d: d["end_state"]["stale"]["filtered"].update(
                {"payments-kill-switch": "listed-kill-switch"}
            ),
            "the rows read with Stale pressed are",
        ),
        (
            lambda d: d.pop("setup"),
            "a setup that makes flags declares the stale end state",
        ),
        (
            lambda d: d["end_state"]["stale"].update(hint="the-hint"),
            "compares 'the-hint', which no beat records",
        ),
        (
            lambda d: d["end_state"]["stale"].update(rows_filtered="rows"),
            "compares 'rows', which no beat counts",
        ),
        (
            lambda d: d["scenes"][0]["beats"][5]["count"].update(
                {"pills-listed": "[data-testid=flag-stale-pill]"}
            ),
            "the count 'pills-listed' is named twice",
        ),
        (
            lambda d: d["scenes"][0]["beats"][0]["count"].update(
                {"stale-hint": "[data-testid=flag-stale-hint]"}
            ),
            "the count 'stale-hint' is named twice",
        ),
        (
            lambda d: d["scenes"][0]["beats"][0]["count"].update({"pills-x": " "}),
            "names a CSS selector of 1 to 200 characters",
        ),
        (
            lambda d: d["scenes"][0]["beats"][0]["count"].update(
                {"Pills": "[data-testid=flag-stale-pill]"}
            ),
            "a count's name is lowercase letters, digits and dashes",
        ),
        (
            lambda d: d["scenes"][0]["beats"][5]["record"].update(
                {
                    "listed-old-search": d["scenes"][0]["beats"][0]["record"].pop(
                        "listed-old-search"
                    )
                }
            ),
            "before Stale is pressed are read at one beat",
        ),
        (
            lambda d: d["scenes"][0]["beats"][0]["count"].update(
                {"rows-stale": d["scenes"][0]["beats"][5]["count"].pop("rows-stale")}
            ),
            "before Stale is pressed are read at one beat",
        ),
    ],
    ids=[
        "a-reason-not-the-tables",
        "the-permanent-tag-dropped",
        "the-unevaluated-flag-evaluated",
        "stale-flag-days-1",
        "no-evaluations-seen",
        "nothing-evaluated",
        "a-name-saying-stale",
        "a-reason-twice",
        "the-kill-switch-not-listed",
        "the-kill-switch-filtered",
        "flags-with-no-setup",
        "a-hint-never-read",
        "a-count-never-taken",
        "a-count-named-twice",
        "a-count-named-as-a-reading",
        "an-empty-selector",
        "a-name-in-capitals",
        "a-listed-row-read-with-stale-pressed",
        "a-filtered-count-before-the-filter",
    ],
)
def test_the_stale_end_state_s2_refuses_at_load(change, reason):
    refused(change, reason)


def test_a_stale_end_state_with_no_setup_flags_is_refused():
    data = raw()
    data["setup"]["flags"] = []
    data["setup"]["exclusion_groups"] = [
        {"name": "G", "traffic_allocation": 1.0, "experiments": ["x"]}
    ]
    with pytest.raises(storyboard.StoryboardError, match="declares the stale end"):
        storyboard.parse(data, source=SOURCE)


def test_a_stale_end_state_stands_alone():
    data = raw()
    data["end_state"]["experiments_listed"] = 3
    storyboard.parse(data, source=SOURCE)
    data["end_state"]["flag"] = {
        "name": "x",
        "key": "x",
        "rule": {"attribute": "a", "operator": "equals", "value": "b"},
        "rollout_percentage": 0,
        "status": "active",
        "audit": ["feature_flag_create"],
    }
    with pytest.raises(storyboard.StoryboardError, match="a stale end state stands"):
        storyboard.parse(data, source=SOURCE)


# ---------------------------------------------------------------------------
# The docs' table is the server's: derived_stale against stale_flags.classify
# ---------------------------------------------------------------------------


def as_row(made: storyboard.FlagSetup, now: dt.datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id=made.key,
        key=made.key,
        status=made.status.upper(),
        rollout_percentage=made.rollout,
        targeting_rules=None,
        split=None,
        tags=list(made.tags),
        created_at=now - dt.timedelta(days=made.created_days_ago),
        updated_at=now - dt.timedelta(days=made.updated_days_ago),
    )


def server_says(flags: List[storyboard.FlagSetup]) -> Dict[str, List[str]]:
    from backend.app.services import stale_flags

    rows = [as_row(f, NOW) for f in flags]
    evaluated = {f.key for f in flags if f.evaluate}
    found = stale_flags.classify(
        rows,
        now=NOW,
        days=30,
        evaluations_seen=bool(evaluated),
        evaluated_ids=evaluated,
        active_schedule_ids=set(),
    )
    return {key: reasons for key, reasons in found.items() if reasons}


def grid() -> List[storyboard.FlagSetup]:
    made = []
    for status in ("active", "inactive"):
        for rollout in (0, 30, 100):
            for tags in ([], ["permanent"], [" Permanent "], ["billing"]):
                for evaluate in (True, False):
                    for created, updated in ((90, 60), (90, 10), (10, 5)):
                        made.append(
                            storyboard.FlagSetup(
                                name=f"f{len(made)}",
                                key=f"f{len(made)}",
                                status=status,
                                rollout=rollout,
                                tags=tags,
                                evaluate=evaluate,
                                created_days_ago=created,
                                updated_days_ago=updated,
                            )
                        )
    return made


def test_the_docs_table_at_load_is_the_servers_own_classifier():
    """PE1512-3's rule, checked both ways: every combination a setup flag can
    declare (status, rollout, tags, evaluated, old or new, changed or not)
    is classified by derived_stale exactly as the server classifies it."""
    flags = grid()
    derived, seen = storyboard.derived_stale(flags)
    assert seen is True
    assert derived == server_says(flags)
    assert storyboard.derived_stale(list(board().setup.flags))[0] == server_says(
        list(board().setup.flags)
    )
    assert len(derived) > 20


def test_a_derivation_that_forgets_the_permanent_tag_is_caught(monkeypatch):
    monkeypatch.setattr(storyboard, "is_permanent", lambda tags: False)
    flags = grid()
    assert storyboard.derived_stale(flags)[0] != server_says(flags)


def test_the_stale_constants_are_the_servers():
    from backend.app.core.config import Settings
    from backend.app.services import stale_flags

    assert storyboard.STALE_REASONS == stale_flags.REASONS
    assert storyboard.PERMANENT_TAG == stale_flags.PERMANENT_TAG
    assert storyboard.STALE_DAYS == Settings.model_fields["STALE_FLAG_DAYS"].default
    from backend.app.schemas.feature_flag import StaleReason

    assert StaleReason.__args__ == storyboard.StaleReason.__args__


def test_the_seed_flag_keys_are_the_seeds_and_it_dates_none_of_them():
    """The two keys a setup flag may not take, and the demo seed's flags
    made at seed time: no created_at or updated_at is written for them, so
    they are new on every stack, and never stale."""
    source = SEED_SCRIPT.read_text(encoding="utf-8")
    keys = tuple(re.findall(r'^\s+key[0-9] = "([a-z_]+)"$', source, re.MULTILINE))
    assert keys == storyboard.SEED_FLAG_KEYS
    flags = source.split("def seed_feature_flags", 1)[1].split("\ndef ", 1)[0]
    assert flags.count("FeatureFlag(") == 2
    assert re.findall(r"\b(?:created|updated)_at\s*=", flags) == []


# ---------------------------------------------------------------------------
# The page's words: gates' copies of index.tsx
# ---------------------------------------------------------------------------


def test_the_pill_the_reasons_and_the_hint_are_the_pages_words():
    source = LIST_PAGE.read_text(encoding="utf-8")
    switch = source.split("function staleReasonText", 1)[1].split("\n}\n", 1)[0]
    cases = dict(re.findall(r"case '([a-z_]+)':\s*return `([^`]+)`;", switch))
    assert {k: v.replace("${n}", "{days}") for k, v in cases.items()} == (
        gates.STALE_REASON_WORDS
    )
    assert list(gates.STALE_REASON_WORDS) == list(storyboard.STALE_REASONS)
    pill = source.split('data-testid="flag-stale-pill"', 1)[1].split("</span>", 1)[0]
    assert squeezed(pill.split(">", 1)[1]) == gates.STALE_PILL
    hint = source.split('data-testid="flag-stale-hint"', 1)[1].split("</p>", 1)[0]
    assert squeezed(hint.split(">", 1)[1]) == gates.STALE_HINT
    assert "flag-stale-notice" in source and "flag-stale-reasons" in source


def test_a_reason_the_page_rewords_is_caught():
    source = LIST_PAGE.read_text(encoding="utf-8").replace(
        "return `On for everyone, unchanged for ${n} days`;",
        "return `On for all, unchanged for ${n} days`;",
    )
    switch = source.split("function staleReasonText", 1)[1].split("\n}\n", 1)[0]
    cases = dict(re.findall(r"case '([a-z_]+)':\s*return `([^`]+)`;", switch))
    assert cases["on_for_everyone"] != (
        gates.STALE_REASON_WORDS["on_for_everyone"].replace("{days}", "${n}")
    )


# ---------------------------------------------------------------------------
# offcamera.send's api_key (check fix 5)
# ---------------------------------------------------------------------------


def opener_answering(status: int, body: Any, seen: List[Any]):
    import json

    def opener(request):
        seen.append(request)
        return status, json.dumps(body).encode()

    return opener


def test_an_api_key_call_sends_x_api_key_and_no_bearer():
    seen: List[Any] = []
    got = offcamera.send(
        API,
        TOKEN,
        "GET",
        "/api/v1/feature-flags/evaluate/holiday-banner?user_id=showcase-setup",
        None,
        200,
        opener=opener_answering(200, {"key": "holiday-banner"}, seen),
        api_key=KEY,
    )
    assert got == (200, {"key": "holiday-banner"})
    (request,) = seen
    assert request.get_header("X-api-key") == KEY
    assert request.get_header("Authorization") is None
    assert TOKEN not in str(request.header_items())


def test_a_call_without_a_key_still_sends_the_bearer_token_only():
    seen: List[Any] = []
    offcamera.send(
        API,
        TOKEN,
        "POST",
        "/api/v1/api-keys",
        {"name": "showcase-setup"},
        201,
        opener=opener_answering(201, {}, seen),
    )
    (request,) = seen
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert request.get_header("X-api-key") is None


@pytest.mark.parametrize("key", ["", " padded", "two words", "line\nbreak"])
def test_a_key_that_is_not_one_printable_word_is_refused_before_the_call(key):
    seen: List[Any] = []
    with pytest.raises(offcamera.CallRefused, match="an API key is one printable"):
        offcamera.send(
            API,
            TOKEN,
            "GET",
            "/api/v1/feature-flags/evaluate/x?user_id=u",
            None,
            200,
            opener=opener_answering(200, {}, seen),
            api_key=key,
        )
    assert seen == []


# ---------------------------------------------------------------------------
# The dating step: SQL from checked values only, one transaction
# ---------------------------------------------------------------------------

ROWS = [
    ("payments-kill-switch", 120, 120),
    ("legacy-signup-form", 90, 60),
    ("holiday-banner", 75, 50),
    ("old-search-ranking", 60, 50),
]


def test_each_flag_is_dated_by_one_update_returning_its_key():
    statements = stacks.dating_statements(ROWS[1:2])
    assert statements == [
        "UPDATE experimentation.feature_flags SET"
        " created_at = (now() AT TIME ZONE 'UTC') - make_interval(days => 90),"
        " updated_at = (now() AT TIME ZONE 'UTC') - make_interval(days => 60)"
        " WHERE key = 'legacy-signup-form' RETURNING key"
    ]
    argv = stacks.psql_argv(["psql"], ROWS)
    assert argv[:7] == [
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "--single-transaction",
        "-q",
        "-A",
        "-t",
    ]
    assert argv[7::2] == ["-c"] * 4
    assert argv[8::2] == stacks.dating_statements(ROWS)


@pytest.mark.parametrize(
    "rows, words",
    [
        ([], "has no flag to date"),
        ([("legacy'; DROP TABLE x; --", 90, 60)], "refuses the key"),
        ([("Legacy", 90, 60)], "refuses the key"),
        ([("legacy signup", 90, 60)], "refuses the key"),
        ([("", 90, 60)], "refuses the key"),
        ([("legacy-signup-form", "90", 60)], "refuses '90' days"),
        ([("legacy-signup-form", 90.0, 60)], "refuses 90.0 days"),
        ([("legacy-signup-form", True, 60)], "refuses True days"),
        ([("legacy-signup-form", 90, -1)], "refuses -1 days"),
        ([("legacy-signup-form", 3651, 60)], "refuses 3651 days"),
    ],
)
def test_the_dating_step_refuses_anything_but_a_key_and_whole_days(rows, words):
    with pytest.raises(stacks.StackError, match=re.escape(words)):
        stacks.dating_statements(rows)


def test_the_returned_keys_are_read_one_a_line():
    assert stacks.dated_keys("a\n\n b \n") == ["a", "b"]


class Children:
    """``procs.Children.capture``'s face, answering *output* with *status*."""

    def __init__(self, status: int, output: str):
        self.status, self.output, self.seen = status, output, []

    def capture(self, name, argv, **kw):
        self.seen.append((name, list(argv), kw))
        return self.status, self.output


def test_the_uvicorn_stack_dates_in_its_own_container_and_port(tmp_path):
    made = stacks.Stack.__new__(stacks.Stack)
    made.container = "showcase-pg-run1"
    made.ports = stacks.Ports(28400, 28401, 28402)
    made.tree, made.logs, made.docker_env = tmp_path, tmp_path, {"PATH": "/bin"}
    made.children = Children(0, "payments-kill-switch\nlegacy-signup-form\n")
    assert made.date_flags(ROWS[:2]) == ["payments-kill-switch", "legacy-signup-form"]
    ((name, argv, kw),) = made.children.seen
    assert name == "date-flags" and kw["kind"] == "docker"
    assert argv == stacks.psql_argv(
        [
            "docker",
            "exec",
            "showcase-pg-run1",
            "psql",
            "-U",
            "postgres",
            "-d",
            "experimentation",
            "-h",
            "127.0.0.1",
            "-p",
            "28402",
        ],
        ROWS[:2],
    )
    made.children = Children(3, "")
    with pytest.raises(stacks.StackError, match="the dating step's psql exited 3"):
        made.date_flags(ROWS[:2])


def test_the_quickstart_stack_dates_in_its_postgres_service():
    made = compose.QuickstartStack.__new__(compose.QuickstartStack)
    made.project, made.files = "showcase-run1", [Path("/w/docker-compose.yml")]
    seen: List[Any] = []

    class Docker:
        def out(self, name, argv, **kw):
            seen.append((name, list(argv)))
            return "holiday-banner\n"

    made.docker = Docker()
    assert made.date_flags(ROWS[2:3]) == ["holiday-banner"]
    ((name, argv),) = seen
    assert name == "date-flags"
    assert argv[:13] == [
        "docker",
        "compose",
        "-p",
        "showcase-run1",
        "-f",
        "/w/docker-compose.yml",
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
    ]
    assert argv[13:] == ["experimentation", *stacks.psql_argv([], ROWS[2:3])]


# ---------------------------------------------------------------------------
# S1 at run time: run.make_flags against a fake API
# ---------------------------------------------------------------------------

SEED_ROWS = [
    SimpleNamespace(
        id="new_dashboard_ui",
        key="new_dashboard_ui",
        name="New Dashboard UI",
        status="ACTIVE",
        rollout_percentage=50,
        targeting_rules={},
        tags=["ui", "dashboard", "redesign"],
    ),
    SimpleNamespace(
        id="beta_features",
        key="beta_features",
        name="Beta Features",
        status="ACTIVE",
        rollout_percentage=100,
        targeting_rules={
            "logical_operator": "OR",
            "groups": [
                {
                    "logical_operator": "AND",
                    "conditions": [
                        {"attribute": "user.role", "operator": "in", "value": ["beta"]}
                    ],
                }
            ],
        },
        tags=[],
    ),
]


def naive(when: dt.datetime) -> str:
    """A time as the API answers a naive UTC column."""
    return when.astimezone(dt.timezone.utc).replace(tzinfo=None).isoformat()


class FakeApi:
    """The take's API as ``make_flags`` and gate 10 meet it: each answer made
    by the server's own classifier (stale_flags.classify) from what the calls
    did, each call kept in order.

    ``dates``: whether the dating step moves the flags; ``skew``: hours the
    read-back is off by (a time zone slip); ``lose``: flags whose evaluation
    the server fails to record (it swallows that failure); ``days``: the
    API's STALE_FLAG_DAYS; ``touch``: flags written after the setup."""

    def __init__(self):
        self.seen: List[Tuple[str, str, Any, int, Optional[str]]] = []
        self.flags: Dict[str, Dict[str, Any]] = {}
        self.evaluated: set = set()
        self.lose: set = set()
        self.dates = True
        self.skew = 0
        self.days = 30
        self.touch: set = set()
        self.rows: List[Any] = []
        self.drop_row: Optional[str] = None

    def send(self, api_url, token, method, path, body, status, *, api_key=None):
        assert api_url == API and token == TOKEN
        self.seen.append((method, path, body, status, api_key))
        got, answer = self.answer(method, path, body, api_key)
        if got != status:
            raise offcamera.CallRefused(
                f"{method} {path} was answered {got}, not {status}"
            )
        return got, answer

    def item(self, key: str) -> Dict[str, Any]:
        made = self.flags[key]
        return {
            "id": made["id"],
            "key": key,
            "name": made["name"],
            "status": made["status"],
            "rollout_percentage": made["rollout"],
            "tags": made["tags"],
            "created_at": naive(made["created"]),
            "updated_at": naive(made["updated"]),
        }

    def answer(self, method: str, path: str, body: Any, api_key: Optional[str]):
        if (method, path) == ("POST", "/api/v1/feature-flags/"):
            self.flags[body["key"]] = {
                "id": FLAG_IDS[body["key"]],
                "name": body["name"],
                "status": "active" if body["is_active"] else "inactive",
                "rollout": body["rollout_percentage"],
                "tags": body["tags"],
                "created": NOW,
                "updated": NOW,
            }
            return 201, self.item(body["key"])
        if (method, path) == ("POST", "/api/v1/api-keys"):
            return 201, {"id": "k-1", "name": body["name"], "key": KEY, "prefix": "x"}
        if path.startswith("/api/v1/feature-flags/evaluate/"):
            key = path.rsplit("/", 1)[1].split("?", 1)[0]
            if api_key != KEY or not path.endswith("?user_id=showcase-setup"):
                return 401, {"detail": "Invalid API Key"}
            if key not in self.lose:
                self.evaluated.add(key)
            inactive = self.flags[key]["status"] == "inactive"
            return 200, {"key": key, "reason": "inactive" if inactive else "rollout"}
        if path.startswith("/api/v1/feature-flags/?"):
            return 200, self.listing("stale=true" in path)
        if path.startswith("/api/v1/feature-flags/"):
            ident = path.rsplit("/", 1)[1]
            (key,) = [k for k, f in self.flags.items() if f["id"] == ident]
            read = self.item(key)
            if self.skew:
                read["created_at"] = naive(
                    self.flags[key]["created"] + dt.timedelta(hours=self.skew)
                )
            return 200, read
        raise AssertionError(f"an unexpected call {method} {path}")

    def date_flags(self, rows):
        self.rows = list(rows)
        for key, created, updated in rows:
            if self.dates:
                self.flags[key]["created"] = NOW - dt.timedelta(days=created)
                self.flags[key]["updated"] = NOW - dt.timedelta(days=updated)
        return [key for key, _c, _u in rows if key != self.drop_row]

    def listing(self, stale: bool) -> Dict[str, Any]:
        from backend.app.services import stale_flags

        for key in self.touch:
            self.flags[key]["updated"] = NOW
        rows = [
            SimpleNamespace(
                id=key,
                key=key,
                name=f["name"],
                status=f["status"].upper(),
                rollout_percentage=f["rollout"],
                targeting_rules=None,
                split=None,
                tags=f["tags"],
                created_at=f["created"],
                updated_at=f["updated"],
            )
            for key, f in self.flags.items()
        ] + [
            SimpleNamespace(**vars(r), created_at=NOW, updated_at=NOW)
            for r in SEED_ROWS
        ]
        reasons = stale_flags.classify(
            rows,
            now=NOW,
            days=self.days,
            evaluations_seen=bool(self.evaluated),
            evaluated_ids=self.evaluated,
            active_schedule_ids={"new_dashboard_ui"},
        )
        items = [
            {
                "key": r.key,
                "name": r.name,
                "stale": bool(reasons[r.id]),
                "stale_reasons": reasons[r.id],
                "updated_at": naive(r.updated_at),
            }
            for r in rows
            if not stale or reasons[r.id]
        ]
        return {
            "items": items,
            "total": len(items),
            "skip": 0,
            "limit": 100,
            "stale_days": self.days,
            "evaluations_seen": bool(self.evaluated),
        }


def make(api: FakeApi, **kw: Any) -> Tuple[Dict[str, Any], List[str], List[Any]]:
    needles: List[str] = []
    answers: List[Any] = []
    made = run.make_setup(
        board(),
        API,
        TOKEN,
        send=api.send,
        answers=answers,
        needles=needles,
        date_flags=kw.pop("date_flags", api.date_flags),
        clock=lambda: NOW,
    )
    return made, needles, answers


def test_the_setup_makes_mints_evaluates_dates_reads_back_and_judges_in_order():
    api = FakeApi()
    made, needles, answers = make(api)
    calls = [(m, p, s, k) for m, p, _b, s, k in api.seen]
    evaluate = "/api/v1/feature-flags/evaluate/{}?user_id=showcase-setup"
    assert calls == [
        *[("POST", "/api/v1/feature-flags/", 201, None)] * 4,
        ("POST", "/api/v1/api-keys", 201, None),
        ("GET", evaluate.format("payments-kill-switch"), 200, KEY),
        ("GET", evaluate.format("legacy-signup-form"), 200, KEY),
        ("GET", evaluate.format("holiday-banner"), 200, KEY),
        *[("GET", f"/api/v1/feature-flags/{FLAG_IDS[k]}", 200, None) for k, *_ in ROWS],
        ("GET", "/api/v1/feature-flags/?limit=100", 200, None),
        ("GET", "/api/v1/feature-flags/?stale=true&limit=100", 200, None),
    ]
    assert [b for _m, _p, b, _s, _k in api.seen[:5]] == [
        {
            "key": "payments-kill-switch",
            "name": "Payments kill switch",
            "is_active": True,
            "rollout_percentage": 100,
            "tags": ["permanent"],
        },
        {
            "key": "legacy-signup-form",
            "name": "Legacy signup form",
            "is_active": True,
            "rollout_percentage": 100,
            "tags": [],
        },
        {
            "key": "holiday-banner",
            "name": "Holiday banner",
            "is_active": False,
            "rollout_percentage": 0,
            "tags": [],
        },
        {
            "key": "old-search-ranking",
            "name": "Old search ranking",
            "is_active": True,
            "rollout_percentage": 30,
            "tags": [],
        },
        {"name": "showcase-setup"},
    ]
    assert api.rows == ROWS
    assert needles == [KEY, "showcase-setup"] and run.SETUP_KEY_NAME == "showcase-setup"
    assert len(answers) == 4
    assert made["segments"] == [] and "exclusion_groups" not in made
    assert made["flags"] == [
        {
            "key": key,
            "id": FLAG_IDS[key],
            "evaluated": key != "old-search-ranking",
            "created_at": naive(NOW - dt.timedelta(days=created)),
            "updated_at": naive(NOW - dt.timedelta(days=updated)),
        }
        for key, created, updated in ROWS
    ]
    assert KEY not in repr(made) and TOKEN not in repr(made)


@pytest.mark.parametrize(
    "plant, words",
    [
        (lambda a: setattr(a, "dates", False), "more than 10 minutes from the"),
        (lambda a: setattr(a, "skew", 7), "created_at reads"),
        (lambda a: setattr(a, "drop_row", "holiday-banner"), "not one row for each"),
        (
            lambda a: a.lose.add("holiday-banner"),
            "before the first frame, ?stale=true lists",
        ),
        (lambda a: setattr(a, "days", 1), "stale_days 1"),
    ],
    ids=[
        "seed-as-shipped",
        "time-zone-slip",
        "a-key-matching-no-row",
        "drop-one-evaluate",
        "stale-flag-days-leak",
    ],
)
def test_the_setup_refuses_before_the_first_frame(plant, words):
    """The plan's S1 plants: no backdate, a skew of hours, a key matching no
    row, an evaluation the server failed to record, STALE_FLAG_DAYS changed."""
    api = FakeApi()
    plant(api)
    with pytest.raises(run.StackError, match=re.escape(words)) as raised:
        make(api)
    if api.lose:
        assert "'holiday-banner': ['not_evaluated', 'off_for_everyone']" in str(
            raised.value
        )


@pytest.mark.parametrize(
    "method, path, answer, words",
    [
        (
            "POST",
            "/api/v1/feature-flags/",
            (409, {"detail": "taken"}),
            "answered 409, not 201",
        ),
        (
            "POST",
            "/api/v1/api-keys",
            (201, {"name": "showcase-setup"}),
            "not answered as one key",
        ),
        ("POST", "/api/v1/api-keys", (201, {"name": "other", "key": KEY}), "so named"),
        (
            "GET",
            "/api/v1/feature-flags/evaluate/holiday-banner?user_id=showcase-setup",
            (200, {"key": "holiday-banner", "reason": "error"}),
            "holiday-banner was evaluated as 'error', not 'inactive'",
        ),
    ],
)
def test_the_setup_refuses_any_other_answer(monkeypatch, method, path, answer, words):
    api = FakeApi()
    real = api.answer

    def planted(m, p, b, k):
        if (m, p) == (method, path):
            return answer
        return real(m, p, b, k)

    monkeypatch.setattr(api, "answer", planted)
    with pytest.raises(run.StackError, match=re.escape(words)):
        make(api)


def test_a_flag_answered_in_another_state_is_refused(monkeypatch):
    api = FakeApi()
    real = api.answer

    def planted(m, p, b, k):
        got, answer = real(m, p, b, k)
        if p == "/api/v1/feature-flags/" and b["key"] == "holiday-banner":
            answer = dict(answer, status="active")
        return got, answer

    monkeypatch.setattr(api, "answer", planted)
    with pytest.raises(run.StackError, match="flag holiday-banner was answered with"):
        make(api)


def test_a_stack_that_cannot_date_refuses_the_setup():
    with pytest.raises(run.StackError, match="the stack cannot date flags back"):
        make(FakeApi(), date_flags=None)


def test_the_setup_sends_through_offcamera_send_when_no_sender_is_given(monkeypatch):
    """As video 9's (PE 3 of its re-plan): the capture gives no sender."""
    api = FakeApi()
    monkeypatch.setattr(offcamera, "send", api.send, raising=True)
    made = run.make_setup(
        board(),
        API,
        TOKEN,
        answers=[],
        needles=[],
        date_flags=api.date_flags,
        clock=lambda: NOW,
    )
    assert [f["key"] for f in made["flags"]] == [k for k, *_ in ROWS]
    assert len(api.seen) == 14


def capture_source(name: str) -> str:
    source = Path(run.__file__).read_text(encoding="utf-8")
    return source.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]


def test_the_capture_gives_the_setup_its_dating_and_keeps_its_needles():
    body = capture_source("capture_video")
    assert body.index('gates["14"] = gate(counts == wanted') < body.index(
        "setup = make_setup("
    )
    assert "needles=setup_needles" in body
    assert 'date_flags=getattr(stack, "date_flags", None)' in body
    assert "setup_needles=setup_needles" in body
    record = capture_source("record")
    assert record.index("for value in setup_needles:\n        redactor.add(value)") < (
        record.index("read_keys: List[str] = list(setup_needles)")
    )
    assert record.index("read_keys: List[str] = list(setup_needles)") < record.index(
        "judge.needle_count(config.STATIC_NEEDLES, redactor.values, read_keys)"
    )
    # Gate 10: after the last frame, with what the page drew and the setup.
    judged_at = record.index("\n            if end.stale is not None:\n")
    assert record.index('gates["10"] = gate(not why, **numbers)') > judged_at
    assert record[judged_at:].startswith(
        "\n            if end.stale is not None:\n"
        "                # S2: after the last frame, the list read again, judged with\n"
        "                # the setup's updated_at and what the page drew.\n"
        "                ok, made, reason = stale_state(\n"
        "                    end.stale,\n"
        "                    stack.api_url,\n"
        "                    token,\n"
        "                    director.readings,\n"
        "                    director.counts,\n"
        "                    setup,\n"
        "                )\n"
        '                numbers["stale"] = made\n'
        "                if not ok:\n"
        "                    why.append(reason)\n"
    )
    for stack_class in (stacks.Stack, compose.QuickstartStack):
        assert callable(getattr(stack_class, "date_flags"))


def test_gate_6_requires_the_setups_key_and_name_as_needles():
    """PE 3: the key is a needle; gate 6 refuses a run that is not looking
    for it (``read_keys``)."""
    kept = {KEY, "showcase-setup"}
    ok, _numbers, _why = gates.needle_count(
        config.STATIC_NEEDLES, kept, [KEY, "showcase-setup"]
    )
    assert ok
    ok, _numbers, why = gates.needle_count(
        config.STATIC_NEEDLES, {"showcase-setup"}, [KEY, "showcase-setup"]
    )
    assert not ok and "is not a needle" in why


# ---------------------------------------------------------------------------
# S2: gate 10's stale end state
# ---------------------------------------------------------------------------


def row(key: str, name: str, status: str, rollout: int, reasons: List[str]) -> str:
    """A list row's text as innerText reads it: cells by tabs, lines inside."""
    lines = [status, *(["Stale"] if reasons else [])]
    lines += [gates.stale_reason_text(r, 30) for r in reasons]
    return "\t".join(
        [key, name, "Boolean", "\n".join(lines), f"{rollout}%", "9/1/2026", ""]
    )


LISTED = {
    "listed-kill-switch": row(
        "payments-kill-switch", "Payments kill switch", "On", 100, []
    ),
    "listed-legacy-signup": row(
        "legacy-signup-form", "Legacy signup form", "On", 100, ["on_for_everyone"]
    ),
    "listed-holiday-banner": row(
        "holiday-banner", "Holiday banner", "Off", 0, ["off_for_everyone"]
    ),
    "listed-old-search": row(
        "old-search-ranking", "Old search ranking", "On", 30, ["not_evaluated"]
    ),
}
READINGS = {
    **LISTED,
    "stale-legacy-signup": LISTED["listed-legacy-signup"],
    "stale-holiday-banner": LISTED["listed-holiday-banner"],
    "stale-old-search": LISTED["listed-old-search"],
    "stale-hint": (
        "Stale is a suggestion: nothing is changed or archived for you. A flag tagged"
        " permanent\n is never stale; tags are set through the API."
    ),
}
COUNTS = {
    "pills-listed": 3,
    "rows-listed": 6,
    "notice-listed": 0,
    "pills-stale": 3,
    "rows-stale": 3,
    "notice-stale": 0,
}


def after_setup() -> Tuple[FakeApi, Dict[str, Any]]:
    api = FakeApi()
    made, _needles, _answers = make(api)
    return api, made


def judged(api: FakeApi, made: Dict[str, Any], readings=None, counts=None):
    return run.stale_state(
        board().end_state.stale,
        API,
        TOKEN,
        READINGS if readings is None else readings,
        COUNTS if counts is None else counts,
        made,
    )


@pytest.fixture
def fake_reads(monkeypatch):
    """run._api answered by the fake API, as after the last frame."""
    holder: Dict[str, FakeApi] = {}

    def read(url: str, token: Optional[str] = None, body: Any = None) -> Any:
        assert token == TOKEN and body is None and url.startswith(API)
        return holder["api"].answer("GET", url[len(API) :], None, None)[1]

    monkeypatch.setattr(run, "_api", read)
    return holder


def test_gate_10_passes_the_take_as_the_board_declares_it(fake_reads):
    api, made = after_setup()
    fake_reads["api"] = api
    ok, numbers, why = judged(api, made)
    assert (ok, why) == (True, ""), why
    assert numbers["stale"] == STALE
    assert (numbers["filtered_total"], numbers["listed_total"]) == (3, 6)
    assert numbers["counts"] == COUNTS


def change(mapping: Dict[str, Any], **values: Any) -> Dict[str, Any]:
    return {**mapping, **values}


@pytest.mark.parametrize(
    "readings, counts, words",
    [
        (
            change(
                READINGS,
                **{
                    "listed-legacy-signup": READINGS["listed-legacy-signup"].replace(
                        "On for everyone", "On for all"
                    )
                },
            ),
            None,
            "legacy-signup-form's row before Stale is pressed shows the Stale pill and []",
        ),
        (None, change(COUNTS, **{"pills-listed": 0}), "drew 0 for 'pills-listed'"),
        (
            change(READINGS, **{"stale-hint": ""}),
            change(COUNTS, **{"rows-stale": 6, "pills-stale": 3}),
            "the hint reads ''",
        ),
        (None, change(COUNTS, **{"rows-stale": 6}), "drew 6 for 'rows-stale'"),
        (None, change(COUNTS, **{"notice-stale": 1}), "drew 1 for 'notice-stale'"),
        (
            change(
                READINGS,
                **{
                    "listed-kill-switch": row(
                        "payments-kill-switch",
                        "Payments kill switch",
                        "On",
                        100,
                        ["on_for_everyone"],
                    )
                },
            ),
            None,
            "payments-kill-switch's row before Stale is pressed shows the Stale pill",
        ),
        (
            change(
                READINGS,
                **{
                    "stale-old-search": READINGS["stale-old-search"].replace(
                        "\nStale", ""
                    )
                },
            ),
            None,
            "old-search-ranking's row with Stale pressed shows no Stale pill",
        ),
        (
            {k: v for k, v in READINGS.items() if k != "stale-holiday-banner"},
            None,
            "no reading 'stale-holiday-banner'",
        ),
    ],
    ids=[
        "a-reason-renamed",
        "the-pill-testid-dropped",
        "the-filter-click-skipped",
        "the-unfiltered-rows-read",
        "the-notice-shown",
        "a-pill-on-the-kill-switch",
        "a-stale-row-without-its-pill",
        "a-row-never-read",
    ],
)
def test_gate_10_refuses_what_the_page_drew_wrong(fake_reads, readings, counts, words):
    """The plan's S2 plants on the screen half: a renamed reason, the pill's
    test id dropped (counted 0), the filter click skipped (no hint, the
    unfiltered rows), the notice, a pill where the API has none, and none
    where it has one."""
    api, made = after_setup()
    fake_reads["api"] = api
    ok, _numbers, why = judged(api, made, readings, counts)
    assert not ok and words in why, why


@pytest.mark.parametrize(
    "plant, words",
    [
        (lambda a: a.touch.add("legacy-signup-form"), "something changed the flag"),
        (lambda a: a.touch.add("payments-kill-switch"), "something changed the flag"),
        (lambda a: setattr(a, "days", 1), "stale_days 1"),
        (lambda a: a.evaluated.clear(), "evaluations_seen False"),
    ],
    ids=[
        "a-later-write",
        "a-toggle-on-the-kill-switch",
        "stale-flag-days-leak",
        "no-evaluations",
    ],
)
def test_gate_10_refuses_an_api_that_moved_after_the_setup(fake_reads, plant, words):
    """A flag written after the setup (a toggle clicked in the board), a
    STALE_FLAG_DAYS leak, the evaluations gone."""
    api, made = after_setup()
    plant(api)
    fake_reads["api"] = api
    ok, _numbers, why = judged(api, made)
    assert not ok and words in why, why


def test_gate_10_refuses_a_setup_never_read_back(fake_reads):
    api, made = after_setup()
    fake_reads["api"] = api
    ok, _numbers, why = judged(api, {"segments": []})
    assert not ok and "the setup read back []" in why


def listing() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    api, _made = after_setup()
    return api.listing(False), api.listing(True)


def test_the_api_half_refuses_an_empty_stale_list_and_a_stale_seed_flag():
    listed, filtered = listing()
    end = board().end_state.stale
    assert gates.stale_api(end, listed=listed, filtered=filtered)[0]
    empty = dict(filtered, items=[], total=0)
    ok, _n, why = gates.stale_api(end, listed=listed, filtered=empty)
    assert not ok and "?stale=true lists {}; the storyboard expects" in why
    seeded = copy.deepcopy(listed)
    beta = next(i for i in seeded["items"] if i["key"] == "beta_features")
    beta.update(stale=True, stale_reasons=["on_for_everyone"])
    ok, _n, why = gates.stale_api(end, listed=seeded, filtered=filtered)
    assert not ok and "the list marks beta_features stale True" in why
    reordered = copy.deepcopy(filtered)
    reordered["items"][0]["stale_reasons"] = ["off_for_everyone", "not_evaluated"]
    ok, _n, why = gates.stale_api(end, listed=listed, filtered=reordered)
    assert not ok and "?stale=true lists" in why


@pytest.mark.parametrize("field", ["stale_days", "evaluations_seen", "total"])
def test_an_answer_without_a_stale_field_is_refused(field):
    """An older server answers the list without them, and 200: refused."""
    listed, filtered = listing()
    del filtered[field]
    ok, _n, why = gates.stale_api(
        board().end_state.stale, listed=listed, filtered=filtered
    )
    assert not ok and f"has no {field!r}" in why


def test_an_item_without_its_reasons_is_refused():
    listed, filtered = listing()
    del listed["items"][0]["stale_reasons"]
    ok, _n, why = gates.stale_api(
        board().end_state.stale, listed=listed, filtered=filtered
    )
    assert not ok and "has no 'stale_reasons'" in why


def test_a_list_cut_short_by_its_page_is_refused():
    listed, filtered = listing()
    listed["total"] = 7
    ok, _n, why = gates.stale_api(
        board().end_state.stale, listed=listed, filtered=filtered
    )
    assert not ok and "lists 6 of its 7" in why


# ---------------------------------------------------------------------------
# The director's counts, and the end state's dump
# ---------------------------------------------------------------------------


class CountingPage:
    def __init__(self, counts: Dict[str, int]):
        self.counts, self.asked = counts, []

    def evaluate(self, script: str, css: str) -> int:
        from showcase.capture import director

        assert script == director.COUNT_JS
        self.asked.append(css)
        return self.counts[css]


def test_the_director_counts_each_selector_at_the_cue_start(tmp_path):
    from showcase.capture import director, guard

    made = director.Director(
        context=object(),
        page=CountingPage(
            {
                "[data-testid=flag-stale-pill]": 3,
                "[data-testid=flag-row]": 6,
                "[data-testid=flag-stale-notice]": 0,
            }
        ),
        frames=tmp_path / "work" / "video" / "capture" / "frames",
        dashboard="http://127.0.0.1:28401",
        api_port=28400,
        dashboard_port=28401,
        redactor=director.redaction.Redactor(),
        forbidden=(),
        work=guard.WorkRoot(tmp_path / "work"),
    )
    made.count_beat(beats()[0])
    assert made.counts == {"pills-listed": 3, "rows-listed": 6, "notice-listed": 0}
    assert director.COUNT_JS == "(css) => document.querySelectorAll(css).length"


def test_the_beat_counts_after_its_readings_and_before_the_hook():
    from showcase.capture import director

    source = Path(director.__file__).read_text(encoding="utf-8")
    beat = source[source.index("    def beat(self, scene: Scene") :]
    beat = beat[: beat.index("    def card(self")]
    assert (
        beat.index("self.readings[name] = self.locate(spec).inner_text()")
        < beat.index("self.count_beat(beat)")
        < beat.index("if self.at_cue is not None:")
    )


def test_only_video_11s_end_state_dump_names_stale():
    """Unset, ``stale`` is left out of the dump: every other video's gate 10
    ``storyboard`` number is as it was (video 3's V3_DUMP pins one)."""
    for slug, relative in config.VIDEOS.items():
        dump = storyboard.load(SHOWCASE / relative).end_state.model_dump()
        assert ("stale" in dump) is (slug == SLUG), slug
    assert board().end_state.model_dump()["stale"]["stale"] == STALE
