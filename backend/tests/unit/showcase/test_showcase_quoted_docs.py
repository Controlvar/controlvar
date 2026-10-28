"""The showcase's code cards quote the docs, line for line (#1066, video 2).

Video 2's SDK card shows lines of the JavaScript SDK's quick start
(docs/sdk/javascript.md). The capture checks them against the recorded
release's page; these tests check them against this tree's, so a change to the
quick start that the card no longer matches fails on the pull request that
makes it, docs-only or not (test_docs_only_gate.py's DOCS_TESTS).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml
from showcase.capture import config, storyboard

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
STORYBOARDS = REPO / "tests" / "acceptance" / "showcase"
JS_SDK_DOC = REPO / "docs" / "sdk" / "javascript.md"


def code_cards():
    for relative in config.VIDEOS.values():
        board = storyboard.load(STORYBOARDS / relative)
        for scene in board.scenes:
            if scene.card is not None and scene.card.code is not None:
                yield board, scene.card.code


def test_every_code_card_quotes_its_page_of_the_docs():
    cards = list(code_cards())
    assert cards, "no storyboard has a code card: the check found nothing"
    for board, code in cards:
        page = REPO / code.doc
        assert storyboard.quoted_lines(page.read_text(encoding="utf-8"), code) == [], (
            board.slug
        )
        assert not any(storyboard.HOST_PORT.search(line) for line in code.lines)


def first_experiment_card() -> storyboard.Code:
    return next(
        code for board, code in code_cards() if board.slug == "02-first-experiment"
    )


def test_a_code_line_the_docs_do_not_have_is_refused():
    code = first_experiment_card()
    page = JS_SDK_DOC.read_text(encoding="utf-8")
    changed = code.model_copy(update={"lines": [code.lines[0].replace("123", "124")]})
    assert "has no line" in storyboard.quoted_lines(page, changed)[0]
    swapped = code.model_copy(update={"lines": [code.lines[-1], code.lines[0]]})
    assert "after the one before it" in storyboard.quoted_lines(page, swapped)[0]
    elsewhere = code.model_copy(update={"section": "Installation"})
    assert storyboard.quoted_lines(page, elsewhere)


@pytest.mark.regression
def test_a_code_card_the_recorded_release_does_not_have_is_refused(tmp_path):
    from showcase.capture import director, run

    board = storyboard.load(STORYBOARDS / config.VIDEOS["02-first-experiment"])
    page = tmp_path / "docs" / "sdk" / "javascript.md"
    page.parent.mkdir(parents=True)
    page.write_text(JS_SDK_DOC.read_text(encoding="utf-8"), encoding="utf-8")
    run.refuse_unquoted_code(board, tmp_path)
    text = page.read_text(encoding="utf-8")
    page.write_text(text.replace("getAssignment(", "assign("), encoding="utf-8")
    with pytest.raises(director.CaptureFailed, match="C10 copy: scene sdk"):
        run.refuse_unquoted_code(board, tmp_path)
    page.unlink()
    with pytest.raises(director.CaptureFailed, match="not in the recorded tree"):
        run.refuse_unquoted_code(board, tmp_path)


# ---------------------------------------------------------------------------
# Video 9's closing card: what guided setup does not offer, paraphrasing the
# docs. Its note is prose, not quoted lines, so it is held to the pages it
# paraphrases by their text: the dashboard-and-API guide's rows (matched by
# their wording, never by line number) and the sequential-testing page's
# "Turning it on" section.
# ---------------------------------------------------------------------------

DASHBOARD_AND_API = REPO / "docs" / "guides" / "dashboard-and-api.md"
SEQUENTIAL_TESTING = REPO / "docs" / "api" / "sequential-testing.md"
DESIGN_SLUG = "09-design-in-depth"
API_ONLY_SCENE = "api-only"
#: The card's subjects: these words may appear only in its scene (its cue, its
#: sentence and the card), so no other beat names what the dashboard lacks.
API_ONLY_WORDS = ("holdout", "exclusion", "sequential", "schedule")
#: The guide's rows the card paraphrases, by the start of their first cell.
API_ONLY_ROWS = (
    "Schedule an experiment's start and end dates",
    "Global holdouts",
    "Mutual exclusion groups",
)
#: The two beta features the card names, each marked "(beta)" on it.
BETA_ON_THE_CARD = ("global holdout (beta)", "sequential testing (beta)")


def guide_rows(text: str) -> List[List[str]]:
    """The guide's table rows as cells."""
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in text.splitlines()
        if line.strip().startswith("|")
    ]


def api_only_card_problems(
    board: storyboard.Storyboard, guide: str, sequential: str
) -> List[str]:
    """What is wrong with video 9's card against the pages it paraphrases."""
    problems: List[str] = []
    rows = guide_rows(guide)
    for start in API_ONLY_ROWS:
        found = [row for row in rows if row and row[0].startswith(start)]
        if len(found) != 1:
            problems.append(f"the guide has {len(found)} rows starting {start!r}")
        elif len(found[0]) < 2 or found[0][1] != "API only":
            problems.append(f"the guide's {start!r} row is not 'API only'")
    holdout = [row for row in rows if row and row[0].startswith("Global holdouts")]
    if holdout and "(beta)" not in holdout[0][0]:
        problems.append("the guide no longer marks global holdouts beta")
    if "## Turning it on" not in sequential.splitlines():
        problems.append("the sequential-testing page has no 'Turning it on' section")
    if '"analysis_status": "beta"' not in sequential:
        problems.append("the sequential-testing page no longer marks its analysis beta")
    cards = [s for s in board.scenes if s.id == API_ONLY_SCENE and s.card is not None]
    if len(cards) != 1:
        return problems + [f"{board.slug} has no {API_ONLY_SCENE!r} card scene"]
    note = " ".join(cards[0].card.note.split()).lower()
    for marked in BETA_ON_THE_CARD:
        if marked not in note:
            problems.append(f"the card does not say {marked!r}")
    for scene in board.scenes:
        if scene.id == API_ONLY_SCENE:
            continue
        for number, beat in enumerate(scene.beats, start=1):
            for text in (beat.cue, *beat.say):
                for word in API_ONLY_WORDS:
                    if word in text.lower():
                        problems.append(
                            f"scene {scene.id} beat {number} says {word!r}, which only"
                            " the card's scene may"
                        )
    return problems


def design_board() -> storyboard.Storyboard:
    return storyboard.load(STORYBOARDS / config.VIDEOS[DESIGN_SLUG])


def design_raw() -> Dict[str, Any]:
    """Video 9's storyboard as written, to change one thing and parse again."""
    path = STORYBOARDS / config.VIDEOS[DESIGN_SLUG]
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_video_9s_card_paraphrases_the_pages_it_names():
    assert (
        api_only_card_problems(
            design_board(),
            DASHBOARD_AND_API.read_text(encoding="utf-8"),
            SEQUENTIAL_TESTING.read_text(encoding="utf-8"),
        )
        == []
    )


def test_a_card_word_in_another_beat_is_refused():
    raw = design_raw()
    raw["scenes"][0]["beats"][0]["say"] = ["Choose New Experiment, or a holdout."]
    changed = storyboard.parse(raw)
    problems = api_only_card_problems(
        changed,
        DASHBOARD_AND_API.read_text(encoding="utf-8"),
        SEQUENTIAL_TESTING.read_text(encoding="utf-8"),
    )
    assert problems == [
        "scene design beat 1 says 'holdout', which only the card's scene may"
    ]


def test_a_row_the_dashboard_now_does_is_refused():
    guide = DASHBOARD_AND_API.read_text(encoding="utf-8")
    row = next(
        line for line in guide.splitlines() if line.startswith("| Global holdouts")
    )
    changed = guide.replace(row, row.replace("| API only |", "| Dashboard |"))
    assert changed != guide
    problems = api_only_card_problems(
        design_board(), changed, SEQUENTIAL_TESTING.read_text(encoding="utf-8")
    )
    assert problems == ["the guide's 'Global holdouts' row is not 'API only'"]


def test_a_line_added_above_the_rows_changes_nothing():
    guide = DASHBOARD_AND_API.read_text(encoding="utf-8")
    lines = guide.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("| Capability"))
    lines.insert(at, "A line added above the table.")
    changed = "\n".join(lines)
    assert (
        api_only_card_problems(
            design_board(), changed, SEQUENTIAL_TESTING.read_text(encoding="utf-8")
        )
        == []
    )


def test_a_page_without_turning_it_on_or_beta_is_refused():
    sequential = SEQUENTIAL_TESTING.read_text(encoding="utf-8")
    guide = DASHBOARD_AND_API.read_text(encoding="utf-8")
    renamed = sequential.replace("## Turning it on", "## Using it")
    assert api_only_card_problems(design_board(), guide, renamed) == [
        "the sequential-testing page has no 'Turning it on' section"
    ]
    raw = design_raw()
    card = next(s for s in raw["scenes"] if s["id"] == API_ONLY_SCENE)["card"]
    card["note"] = card["note"].replace(
        "sequential testing (beta)", "sequential testing"
    )
    assert api_only_card_problems(storyboard.parse(raw), guide, sequential) == [
        "the card does not say 'sequential testing (beta)'"
    ]
