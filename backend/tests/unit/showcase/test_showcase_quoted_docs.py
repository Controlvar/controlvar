"""The showcase's code cards quote the docs, line for line (#1066, video 2).

Video 2's SDK card shows lines of the JavaScript SDK's quick start
(docs/sdk/javascript.md). The capture checks them against the recorded
release's page; these tests check them against this tree's, so a change to the
quick start that the card no longer matches fails on the pull request that
makes it, docs-only or not (test_docs_only_gate.py's DOCS_TESTS).

Video 9's API part (#1442) says what the dashboard does not offer and what
each API call does. Each of its sentences has a row of ``CLAIMS``: what the
video draws while it is said, checked against the storyboard, and the pages
that state it, read from this tree (by their words, never by line number).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

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
# Video 9's API part: what guided setup does not offer, set through the API.
# Its words are held to the pages that state them: the dashboard-and-API
# guide's rows (matched by their wording, never by line number), the
# sequential-testing page's "Turning it on" section, and each sentence's row
# of CLAIMS.
# ---------------------------------------------------------------------------

DASHBOARD_AND_API = REPO / "docs" / "guides" / "dashboard-and-api.md"
SEQUENTIAL_TESTING = REPO / "docs" / "api" / "sequential-testing.md"
DESIGN_SLUG = "09-design-in-depth"
#: The API card, and the console scene that makes the calls.
API_SCENES = ("api-intro", "api")
#: The API part's subjects: these words may appear only in its scenes (their
#: captions, sentences, notes and card), so no dashboard beat names what the
#: dashboard lacks.
API_WORDS = ("holdout", "exclusion", "sequential", "schedule", "cuped")
#: No guide row is held "API only" any more: scheduling a draft, exclusion
#: groups and the global holdout each have a dashboard screen (#1454, #1455).
#: Video 9's API part is withdrawn (D182) and is no longer checked against
#: those rows; video 9's re-plan retires it (EM-2). The holdout row's "(beta)"
#: is still held below.


def guide_rows(text: str) -> List[List[str]]:
    """The guide's table rows as cells."""
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in text.splitlines()
        if line.strip().startswith("|")
    ]


def scene_texts(scene: storyboard.Scene) -> List[Tuple[int, str]]:
    """``(beat, text)`` for each caption, sentence and note of *scene*, and its
    card's lines and console's label as beat 0."""
    found = [(0, text) for text in storyboard.card_texts(scene.card)]
    if scene.console is not None:
        found.append((0, scene.console.label))
    for number, beat in enumerate(scene.beats, start=1):
        found += [(number, text) for text in (beat.cue, *beat.say)]
        if beat.note is not None:
            found.append((number, beat.note))
    return found


def sequential_beat(board: storyboard.Storyboard) -> Optional[storyboard.Beat]:
    """The console beat whose call turns sequential testing on."""
    for scene in board.scenes:
        for beat in scene.beats:
            call = beat.call
            if call is not None and (
                storyboard.API_CALLS[(call.method, call.path)].sets == "sequential"
            ):
                return beat
    return None


def api_part_problems(
    board: storyboard.Storyboard, guide: str, sequential: str
) -> List[str]:
    """What is wrong with video 9's API part against the pages it rests on."""
    problems: List[str] = []
    rows = guide_rows(guide)
    holdout = [row for row in rows if row and row[0].startswith("Global holdouts")]
    if holdout and "(beta)" not in holdout[0][0]:
        problems.append("the guide no longer marks global holdouts beta")
    if "## Turning it on" not in sequential.splitlines():
        problems.append("the sequential-testing page has no 'Turning it on' section")
    if '"analysis_status": "beta"' not in sequential:
        problems.append("the sequential-testing page no longer marks its analysis beta")
    ids = [scene.id for scene in board.scenes]
    missing = [scene for scene in API_SCENES if scene not in ids]
    if missing:
        return problems + [f"{board.slug} has no {missing[0]!r} scene"]
    beat = sequential_beat(board)
    if beat is None:
        problems.append("no console beat turns sequential testing on")
    else:
        if "(beta)" not in beat.cue:
            problems.append(
                "the sequential testing beat's caption does not say '(beta)'"
            )
        if not (beat.note or "").startswith("Beta."):
            problems.append("the sequential testing beat's note does not open 'Beta.'")
        if not any("in beta" in sentence for sentence in beat.say):
            problems.append("the sequential testing beat does not say 'in beta'")
    for scene in board.scenes:
        if scene.id in API_SCENES:
            continue
        for number, text in scene_texts(scene):
            for word in API_WORDS:
                if word in text.lower():
                    problems.append(
                        f"scene {scene.id} beat {number} says {word!r}, which only"
                        " the API scenes may"
                    )
    return problems


def design_board() -> storyboard.Storyboard:
    return storyboard.load(STORYBOARDS / config.VIDEOS[DESIGN_SLUG])


def design_raw() -> Dict[str, Any]:
    """Video 9's storyboard as written, to change one thing and parse again."""
    path = STORYBOARDS / config.VIDEOS[DESIGN_SLUG]
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def api_raw_beat(raw: Dict[str, Any], number: int) -> Dict[str, Any]:
    return next(s for s in raw["scenes"] if s["id"] == "api")["beats"][number - 1]


def test_video_9s_api_part_rests_on_the_pages_it_names():
    assert (
        api_part_problems(
            design_board(),
            DASHBOARD_AND_API.read_text(encoding="utf-8"),
            SEQUENTIAL_TESTING.read_text(encoding="utf-8"),
        )
        == []
    )


@pytest.mark.parametrize(
    ("line", "word"),
    [
        ("Choose New Experiment, or a holdout.", "holdout"),
        ("Choose New Experiment, then CUPED.", "cuped"),
    ],
)
def test_an_api_word_in_a_dashboard_beat_is_refused(line, word):
    raw = design_raw()
    raw["scenes"][0]["beats"][0]["say"] = [line]
    changed = storyboard.parse(raw)
    problems = api_part_problems(
        changed,
        DASHBOARD_AND_API.read_text(encoding="utf-8"),
        SEQUENTIAL_TESTING.read_text(encoding="utf-8"),
    )
    assert problems == [
        f"scene design beat 1 says {word!r}, which only the API scenes may"
    ]


def test_a_line_added_above_the_rows_changes_nothing():
    guide = DASHBOARD_AND_API.read_text(encoding="utf-8")
    lines = guide.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("| Capability"))
    lines.insert(at, "A line added above the table.")
    changed = "\n".join(lines)
    assert (
        api_part_problems(
            design_board(), changed, SEQUENTIAL_TESTING.read_text(encoding="utf-8")
        )
        == []
    )


def test_a_page_without_turning_it_on_or_beta_is_refused():
    sequential = SEQUENTIAL_TESTING.read_text(encoding="utf-8")
    guide = DASHBOARD_AND_API.read_text(encoding="utf-8")
    renamed = sequential.replace("## Turning it on", "## Using it")
    assert api_part_problems(design_board(), guide, renamed) == [
        "the sequential-testing page has no 'Turning it on' section"
    ]
    raw = design_raw()
    beat = api_raw_beat(raw, 4)
    beat["cue"] = beat["cue"].replace(" (beta)", "")
    assert api_part_problems(storyboard.parse(raw), guide, sequential) == [
        "the sequential testing beat's caption does not say '(beta)'"
    ]


# ---------------------------------------------------------------------------
# CLAIMS: each sentence of video 9's API part, what is drawn while it is
# said, and the pages that state it
# ---------------------------------------------------------------------------


class Drawn(NamedTuple):
    """Something the video draws while a sentence is said.

    ``kind`` is where: ``headline`` (the card's), ``cue``, ``note``, ``path``
    (the request's path), ``body`` (a key the request sends), ``show`` (a field
    of the answer drawn) or ``answer`` (a shown field and the value it must
    have). A beat with no call draws the call before it."""

    kind: str
    what: str
    value: Any = None


class Claim(NamedTuple):
    scene: str
    beat: int
    drawn: Tuple[Drawn, ...]
    #: ``(file under the repository, words it holds)``, whitespace aside.
    pages: Tuple[Tuple[str, str], ...] = ()


MEGS = "docs/api/mutual-exclusion-groups.md"
GUIDE = "docs/guides/dashboard-and-api.md"
USER_GUIDE = "docs/guides/user-guide.md"
SEQUENTIAL = "docs/api/sequential-testing.md"
SCHEDULER = "backend/app/core/scheduler.py"

CLAIMS: Dict[str, Claim] = {
    "Guided setup ends here: you do the rest through the API.": Claim(
        "api-intro",
        1,
        (Drawn("headline", "Through the API"),),
        (
            (
                SEQUENTIAL,
                "Sequential testing is set on the experiment, when you create it",
            ),
        ),
    ),
    "Experiments in the same exclusion group never share a user.": Claim(
        "api",
        1,
        (Drawn("note", "Experiments in the same group never share a user."),),
        (
            (MEGS, "Users are assigned to at most one experiment within the group."),
            (
                MEGS,
                "A user enrolled in one experiment of a group is never enrolled in"
                " another experiment of that group",
            ),
        ),
    ),
    "Add this draft to the group with Checkout Button Color.": Claim(
        "api",
        1,
        (
            Drawn("path", "/mutual-exclusion-groups/{group}/experiments"),
            Drawn("body", "experiment_id"),
            Drawn("answer", "status", "ok"),
            Drawn("cue", "with Checkout Button Color"),
            Drawn("note", "Checkout tests and its member Checkout Button Color"),
        ),
        ((MEGS, "### POST /api/v1/mutual-exclusion-groups/{group_id}/experiments"),),
    ),
    "A global holdout keeps 5% of new users out of every experiment.": Claim(
        "api",
        2,
        (
            Drawn("path", "/holdout"),
            Drawn("body", "holdout_percentage"),
            Drawn("answer", "holdout_percentage", 5),
            Drawn("answer", "is_active", True),
            Drawn("note", "5% of new users are kept out of every experiment"),
        ),
        (
            (
                MEGS,
                "A global holdout keeps a percentage of new users out of every"
                " experiment",
            ),
        ),
    ),
    "That includes this one.": Claim(
        "api",
        2,
        (Drawn("note", "this one included"), Drawn("cue", "every experiment")),
        ((MEGS, "out of **all** experiments platform-wide"),),
    ),
    "Give it a start date, and it starts on its own.": Claim(
        "api",
        3,
        (
            Drawn("body", "start_date"),
            Drawn("answer", "start_date", "{start}"),
            Drawn("cue", "it starts on its own"),
        ),
        (
            (USER_GUIDE, "The experiment activates at `start_date`"),
            (SCHEDULER, "Experiment.start_date <= current_time"),
        ),
    ),
    "Until then, it stays a draft.": Claim(
        "api",
        3,
        (Drawn("answer", "status", "draft"),),
        (
            (USER_GUIDE, "on a `DRAFT` experiment"),
            (SCHEDULER, "Experiment.status == ExperimentStatus.DRAFT"),
        ),
    ),
    "Turn on sequential testing, in beta.": Claim(
        "api",
        4,
        (
            Drawn("body", "sequential_testing_enabled"),
            Drawn("answer", "sequential_testing_enabled", True),
            Drawn("cue", "Sequential testing (beta)"),
            Drawn("note", "Beta."),
        ),
        (
            (SEQUENTIAL, "## Turning it on"),
            (
                SEQUENTIAL,
                "| `sequential_testing_enabled` | bool | `false` | Turns the analysis"
                " on |",
            ),
            (SEQUENTIAL, '"analysis_status": "beta"'),
        ),
    ),
    "Check results whenever you like, and stop once the evidence is strong.": Claim(
        "api",
        4,
        (
            Drawn("cue", "check often, stop early"),
            Drawn("note", "Check whenever you like"),
        ),
        (
            (
                SEQUENTIAL,
                "Sequential testing lets you continuously monitor experiment results"
                " and stop as soon as you have enough evidence",
            ),
            (SEQUENTIAL, "stays valid however often you read the results"),
        ),
    ),
    "The answer also carries the group's id and the start date.": Claim(
        "api",
        5,
        (
            Drawn("answer", "mutual_exclusion_group_id", "{group}"),
            Drawn("answer", "start_date", "{start}"),
        ),
    ),
    "Read it back: it's still a draft, in its group.": Claim(
        "api",
        6,
        (
            Drawn("path", "/experiments/{experiment}"),
            Drawn("answer", "status", "draft"),
            Drawn("answer", "mutual_exclusion_group_id", "{group}"),
        ),
    ),
    "It has its start date, and sequential testing is on.": Claim(
        "api",
        6,
        (
            Drawn("answer", "start_date", "{start}"),
            Drawn("answer", "sequential_testing_enabled", True),
        ),
    ),
    "And the holdout is active.": Claim(
        "api",
        7,
        (
            Drawn("path", "/holdout"),
            Drawn("answer", "id", "{holdout}"),
            Drawn("answer", "is_active", True),
        ),
        (
            (MEGS, "### GET /api/v1/holdout"),
            (MEGS, "Returns the currently active global holdout"),
        ),
    ),
}


def squeezed(text: str) -> str:
    return " ".join(text.split())


def read_page(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


def drawn_problem(scene: storyboard.Scene, number: int, item: Drawn) -> Optional[str]:
    """Why *item* is not drawn at beat *number* of *scene*, or None."""
    beat = scene.beats[number - 1]
    call = beat.call or next(
        (b.call for b in reversed(scene.beats[: number - 1]) if b.call), None
    )
    if item.kind == "headline":
        ok = scene.card is not None and item.what in (scene.card.headline or "")
    elif item.kind == "cue":
        ok = item.what in beat.cue
    elif item.kind == "note":
        ok = item.what in (beat.note or "")
    elif call is None:
        return f"draws no call, so no {item.kind} {item.what!r}"
    elif item.kind == "path":
        ok = item.what in call.path
    elif item.kind == "body":
        ok = item.what in (call.body or {})
    elif item.kind == "show":
        ok = item.what in call.show
    elif item.kind == "answer":
        ok = (
            item.what in call.show
            and call.answer.fields.get(item.what, ...) == item.value
        )
    else:
        return f"has an unknown kind {item.kind!r}"
    return None if ok else f"does not draw {item.kind} {item.what!r}"


def claims_problems(
    board: storyboard.Storyboard,
    claims: Dict[str, Claim] = CLAIMS,
    read: Callable[[str], str] = read_page,
) -> List[str]:
    """Every sentence of the API scenes has its row; each row's sentence is
    said where it says, everything it names is drawn there, and every page it
    names holds its words."""
    problems: List[str] = []
    scenes = {scene.id: scene for scene in board.scenes}
    said = {
        (scene.id, number, sentence)
        for scene in board.scenes
        if scene.id in API_SCENES
        for number, beat in enumerate(scene.beats, start=1)
        for sentence in beat.say
    }
    for scene_id, number, sentence in sorted(said):
        if sentence not in claims:
            problems.append(
                f"scene {scene_id} beat {number} says {sentence!r}, which has no"
                " CLAIMS row"
            )
    for sentence, claim in claims.items():
        where = f"scene {claim.scene} beat {claim.beat}"
        if (claim.scene, claim.beat, sentence) not in said:
            problems.append(f"{where} does not say {sentence!r}")
            continue
        for item in claim.drawn:
            problem = drawn_problem(scenes[claim.scene], claim.beat, item)
            if problem:
                problems.append(f"{where} {problem} while it says {sentence!r}")
        for relative, words in claim.pages:
            try:
                text = read(relative)
            except OSError:
                problems.append(f"{relative} is gone ({sentence!r})")
                continue
            if squeezed(words) not in squeezed(text):
                problems.append(f"{relative} no longer says {words!r} ({sentence!r})")
    return problems


def test_every_sentence_of_the_api_part_has_its_claims_row_and_it_holds():
    made = design_board()
    assert claims_problems(made) == []
    rows = sorted((c.scene, c.beat) for c in CLAIMS.values())
    assert len(rows) == 13 and len(CLAIMS) == 13


def test_a_sentence_with_no_claims_row_is_refused():
    raw = design_raw()
    api_raw_beat(raw, 3)["say"].append("The scheduler checks it often.")
    assert claims_problems(storyboard.parse(raw)) == [
        "scene api beat 3 says 'The scheduler checks it often.', which has no"
        " CLAIMS row"
    ]


def test_a_claim_whose_drawing_is_gone_is_refused():
    raw = design_raw()
    beat = api_raw_beat(raw, 2)
    beat["note"] = beat["note"].replace(", this one included", "")
    assert claims_problems(storyboard.parse(raw)) == [
        "scene api beat 2 does not draw note 'this one included' while it says"
        " 'That includes this one.'"
    ]
    raw = design_raw()
    api_raw_beat(raw, 6)["call"]["answer"]["fields"]["status"] = "active"
    assert claims_problems(storyboard.parse(raw)) == [
        "scene api beat 6 does not draw answer 'status' while it says"
        ' "Read it back: it\'s still a draft, in its group."'
    ]


def test_a_claim_whose_page_no_longer_says_it_is_refused():
    def changed(relative: str) -> str:
        text = read_page(relative)
        if relative == MEGS:
            return text.replace("**all** experiments", "most experiments")
        return text

    assert claims_problems(design_board(), read=changed) == [
        f"{MEGS} no longer says 'out of **all** experiments platform-wide'"
        " ('That includes this one.')"
    ]
