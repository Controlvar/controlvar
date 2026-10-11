"""The showcase's code cards quote the docs, line for line (#1066, video 2).

Video 2's SDK card shows lines of the JavaScript SDK's quick start
(docs/sdk/javascript.md). The capture checks them against the recorded
release's page; these tests check them against this tree's, so a change to the
quick start that the card no longer matches fails on the pull request that
makes it, docs-only or not (test_docs_only_gate.py's DOCS_TESTS).
"""

from __future__ import annotations

from pathlib import Path

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
