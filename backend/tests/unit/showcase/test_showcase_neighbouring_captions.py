"""No two neighbouring captions of any storyboard read alike.

The render's caption check (gate 5, ``ocr.caption_problems``) reads the band
once during the next cue and refuses the video when the previous caption is
still there, judged by ``ocr.text_similarity`` at ``ocr.CAPTION_ABSENT_BELOW``.
Two captions that read alike therefore refuse the render, but only at the end
of a capture, a voice step and an encode. This test finds the pair first, for
every storyboard, from the files alone.

The pairs are the ones gate 5 makes: the cues in ``compile_cues`` order (scene,
then beat), each compared with the next, each drawn as the band draws it
(``contract.wrap_caption`` lines joined by a space, as ``OutputCue.text``).
The line is ``MAX_NEIGHBOUR_SIMILARITY``, below the render's own 0.5 so that
an edit to a caption does not land on the edge.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import pytest
from showcase import contract
from showcase.capture import config, storyboard
from showcase.render import ocr

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
SHOWCASE = REPO / "tests" / "acceptance" / "showcase"

#: The most two neighbouring captions may resemble each other.
MAX_NEIGHBOUR_SIMILARITY = 0.45


def storyboard_files() -> List[Tuple[str, str]]:
    """(slug, relative path) of every video and every probe storyboard."""
    return sorted({**config.VIDEOS, **config.PROBES}.items())


def drawn(text: str) -> str:
    return " ".join(contract.wrap_caption(text))


def neighbour_scores(texts: List[str]) -> List[Tuple[int, int, float]]:
    """(cue, next cue, similarity) for each neighbouring pair, numbered from 1."""
    drawn_texts = [drawn(text) for text in texts]
    return [
        (number, number + 1, ocr.text_similarity(first, second))
        for number, (first, second) in enumerate(
            zip(drawn_texts, drawn_texts[1:]), start=1
        )
    ]


def too_alike(slug: str, texts: List[str]) -> List[str]:
    return [
        f"{slug} cue {a} -> {b}: similarity {score:.3f} is over"
        f" {MAX_NEIGHBOUR_SIMILARITY} ({texts[a - 1]!r} | {texts[b - 1]!r})"
        for a, b, score in neighbour_scores(texts)
        if score > MAX_NEIGHBOUR_SIMILARITY
    ]


def cue_texts(relative: str) -> List[str]:
    board = storyboard.load(SHOWCASE / relative)
    return [cue["text"] for cue in storyboard.compile_cues(board)]


def test_the_line_sits_below_the_renders_own():
    assert MAX_NEIGHBOUR_SIMILARITY < ocr.CAPTION_ABSENT_BELOW


def test_the_storyboards_are_found():
    slugs = [slug for slug, _ in storyboard_files()]
    assert "10-serve-a-value" in slugs and "01-getting-started" in slugs
    assert len(slugs) == len(config.VIDEOS) + len(config.PROBES)


@pytest.mark.parametrize(("slug", "relative"), storyboard_files())
def test_no_two_neighbouring_captions_read_alike(slug: str, relative: str):
    texts = cue_texts(relative)
    assert len(texts) >= 2, f"{slug} has fewer than two cues"
    assert too_alike(slug, texts) == []


def test_a_pair_that_reads_alike_is_named():
    """The check itself: planted neighbours are reported with the board, the pair, the score."""
    texts = ["Name it, and the key fills in.", "Name it, and the key fills in."]
    assert too_alike("plant", texts) == [
        "plant cue 1 -> 2: similarity 1.000 is over 0.45"
        " ('Name it, and the key fills in.' | 'Name it, and the key fills in.')"
    ]


def test_the_old_rollout_caption_is_caught():
    """Regression: video 10's cue 6 once read 0.514 against cue 5 and refused the render."""
    texts = cue_texts(config.VIDEOS["10-serve-a-value"])
    texts[5] = "Rollout at 0%: no one else gets the on value."
    problems = too_alike("10-serve-a-value", texts)
    # Cue 6 -> 7 read 0.486 as well: under the render's 0.5, over this line.
    assert [p.split(" (")[0] for p in problems] == [
        "10-serve-a-value cue 5 -> 6: similarity 0.514 is over 0.45",
        "10-serve-a-value cue 6 -> 7: similarity 0.486 is over 0.45",
    ]
