"""The showcase's code cards quote the docs, line for line (#1066, video 2).

Video 2's SDK card shows lines of the JavaScript SDK's quick start
(docs/sdk/javascript.md). The capture checks them against the recorded
release's page; these tests check them against this tree's, so a change to the
quick start that the card no longer matches fails on the pull request that
makes it, docs-only or not (test_docs_only_gate.py's DOCS_TESTS).

The claims check (``claims_problems``): every sentence a board says has a row
of a claims table, naming what the video draws while it is said and the pages
of the docs that state it, read from this tree (by their words, never by line
number). Video 9's re-record (D182) brings its own table; until then the check
is held to a fixture table over the first two beats of video 9's board.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

import pytest
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
# CLAIMS: each sentence, what is drawn while it is said, and the pages that
# state it
# ---------------------------------------------------------------------------

DESIGN_SLUG = "09-design-in-depth"


class Drawn(NamedTuple):
    """Something the video draws while a sentence is said.

    ``kind`` is where: ``cue`` (the beat's caption holds ``what``)."""

    kind: str
    what: str


class Claim(NamedTuple):
    scene: str
    beat: int
    drawn: Tuple[Drawn, ...]
    #: ``(file under the repository, words it holds)``, whitespace aside.
    pages: Tuple[Tuple[str, str], ...] = ()


def squeezed(text: str) -> str:
    return " ".join(text.split())


def read_page(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


def drawn_problem(scene: storyboard.Scene, number: int, item: Drawn) -> Optional[str]:
    """Why *item* is not drawn at beat *number* of *scene*, or None."""
    beat = scene.beats[number - 1]
    if item.kind == "cue":
        ok = item.what in beat.cue
    else:
        return f"has an unknown kind {item.kind!r}"
    return None if ok else f"does not draw {item.kind} {item.what!r}"


def claims_problems(
    board: storyboard.Storyboard,
    claims: Dict[str, Claim],
    read: Callable[[str], str] = read_page,
) -> List[str]:
    """Every sentence of every scene has its row; each row's sentence is said
    where it says, everything it names is drawn there, and every page it names
    holds its words."""
    problems: List[str] = []
    scenes = {scene.id: scene for scene in board.scenes}
    said = {
        (scene.id, number, sentence)
        for scene in board.scenes
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


#: The machinery's fixture: video 9's first two beats, as written, and a row
#: for each of their two sentences (the real table comes with the re-record).
WIZARD_GUIDE = "docs/guides/experiment-wizard.md"
FIXTURE_CLAIMS: Dict[str, Claim] = {
    "Choose New Experiment.": Claim(
        "design",
        1,
        (Drawn("cue", "Past the basics of guided setup"),),
        ((WIZARD_GUIDE, "**Experiments → + New Experiment** opens guided setup"),),
    ),
    "Name it, and the key your code uses fills itself in.": Claim(
        "design",
        2,
        (Drawn("cue", "the key fills itself in"),),
        ((WIZARD_GUIDE, "Name, key, description, hypothesis"),),
    ),
}


def first_two_beats(
    change: Callable[[List[storyboard.Beat]], None] = lambda beats: None,
) -> storyboard.Storyboard:
    """Video 9's board cut to its first scene's first two beats; *change* may
    edit them first. Copied, not parsed: the cut board names readings that no
    beat left records, which only the end state reads."""
    board = storyboard.load(STORYBOARDS / config.VIDEOS[DESIGN_SLUG])
    scene = board.scenes[0]
    beats = list(scene.beats[:2])
    change(beats)
    return board.model_copy(
        update={"scenes": [scene.model_copy(update={"beats": beats})]}
    )


def test_the_claims_check_holds_on_its_fixture():
    made = first_two_beats()
    sentences = [
        text for _scene, _beat, _line, text in storyboard.narration_lines(made)
    ]
    assert sentences == list(FIXTURE_CLAIMS)
    assert len(FIXTURE_CLAIMS) == 2
    assert claims_problems(made, FIXTURE_CLAIMS) == []


def test_a_sentence_with_no_claims_row_is_refused():
    def one_more(beats: List[storyboard.Beat]) -> None:
        beats[1] = beats[1].model_copy(
            update={"say": [*beats[1].say, "The scheduler checks it often."]}
        )

    assert claims_problems(first_two_beats(one_more), FIXTURE_CLAIMS) == [
        "scene design beat 2 says 'The scheduler checks it often.', which has no"
        " CLAIMS row"
    ]


def test_a_claim_whose_drawing_is_gone_is_refused():
    def other_caption(beats: List[storyboard.Beat]) -> None:
        beats[0] = beats[0].model_copy(update={"cue": "Signed in off camera."})

    assert claims_problems(first_two_beats(other_caption), FIXTURE_CLAIMS) == [
        "scene design beat 1 does not draw cue 'Past the basics of guided setup'"
        " while it says 'Choose New Experiment.'"
    ]


def test_a_claim_whose_page_no_longer_says_it_is_refused():
    def changed(relative: str) -> str:
        text = read_page(relative)
        if relative == WIZARD_GUIDE:
            return text.replace("opens guided setup", "starts guided setup")
        return text

    assert claims_problems(first_two_beats(), FIXTURE_CLAIMS, read=changed) == [
        f"{WIZARD_GUIDE} no longer says '**Experiments → + New Experiment** opens"
        " guided setup' ('Choose New Experiment.')"
    ]
