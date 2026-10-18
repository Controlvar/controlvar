"""`docs/getting-started/quick-start.md` — the claims its examples depend on.

Until the documentation runner executes the page (Stream D), these pin what can
be checked without a stack:

* Step 7 creates its flag switched off, so the `/activate` that follows is a
  real step rather than a no-op on a flag that was already on;
* Step 7 evaluates `user-2`, and the page says `user-2` is inside the 10%
  rollout and `user-123` is not.  That is decided by the service's bucketing,
  so it is asserted through the service: a change to the hash fails here, with
  this message, instead of as a quick-start that quietly prints `false`;
* no shell example carries a `#` comment.  macOS's default zsh does not treat
  `#` as a comment when a line is pasted (`interactivecomments` is off), so
  `... | jq .status  # "healthy"` hands `#` and `"healthy"` to `jq` as file
  names and fails (#98).  Expected values belong in the prose.
"""

from __future__ import annotations

import pathlib
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.app.services.feature_flag_service import FeatureFlagService

pytestmark = [pytest.mark.unit, pytest.mark.regression]

PAGE = (
    pathlib.Path(__file__).resolve().parents[4]
    / "docs"
    / "getting-started"
    / "quick-start.md"
)

# ```bash, or the tagged form the documentation runner requires (```{.bash exec}).
_FENCE = re.compile(r"^```(?:\{\.)?(\w*)[^`]*$")


def _shell_blocks() -> list[tuple[int, list[str]]]:
    """(first line number, lines) of every ```bash fence in the page."""
    blocks, current, start = [], None, 0
    for number, line in enumerate(PAGE.read_text(encoding="utf-8").splitlines(), 1):
        match = _FENCE.match(line)
        if (
            current is None
            and match
            and match.group(1) in ("bash", "sh", "shell", "console")
        ):
            current, start = [], number + 1
        elif current is not None and line.strip() == "```":
            blocks.append((start, current))
            current = None
        elif current is not None:
            current.append(line)
    assert current is None, f"unclosed fence starting at line {start}"
    return blocks


def _comment_column(line: str) -> int | None:
    """Column of a shell comment in *line*, or None.

    A `#` starts a comment only outside quotes and at the start of a word --
    `$#`, `${#x}`, `a#b` and a URL's `#anchor` are not comments.
    """
    quote = None
    for column, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#" and (column == 0 or line[column - 1] in " \t;&|("):
            return column
    return None


def _step_7() -> list[str]:
    text = PAGE.read_text(encoding="utf-8")
    section = text.split("## Step 7", 1)[1].split("\n## ", 1)[0]
    return section.splitlines()


def test_the_page_has_exactly_its_shell_examples():
    # Exact, so a parser that silently stops reading the page cannot pass the
    # comment test below by finding nothing.  Update it with the page.
    assert len(_shell_blocks()) == 15


def test_no_shell_example_carries_a_comment():
    found = [
        f"quick-start.md:{start + offset}: {line.strip()}"
        for start, lines in _shell_blocks()
        for offset, line in enumerate(lines)
        if _comment_column(line) is not None
    ]
    assert not found, (
        "comments break when pasted into zsh; move them to prose:\n" + "\n".join(found)
    )


@pytest.mark.parametrize(
    "line, is_comment",
    [
        ('curl -s localhost:8000/health/ready | jq .status  # "healthy"', True),
        ("# a whole-line comment", True),
        ("echo a#b", False),
        ("echo $# ${#PATH}", False),
        ("curl http://localhost:8000/docs#/flags", False),
        ("""-d '{"colour":"#00ff00"}'""", False),
    ],
)
def test_the_comment_detector(line, is_comment):
    assert (_comment_column(line) is not None) is is_comment


def test_step_7_creates_the_flag_switched_off():
    creates = [
        line for line in _step_7() if "feature-flags/ " in line or "-d '{" in line
    ]
    assert any('"is_active":false' in line.replace(" ", "") for line in creates), (
        "Step 7 must create the flag with is_active false, or /activate proves nothing"
    )


def test_step_7_evaluates_the_user_the_prose_names():
    evaluate = [line for line in _step_7() if "evaluate/new_checkout" in line]
    assert len(evaluate) == 1 and "user_id=user-2" in evaluate[0]


def test_the_rollout_puts_user_2_in_and_user_123_out():
    service = FeatureFlagService(db=MagicMock())
    flag = SimpleNamespace(key="new_checkout", rollout_percentage=10)
    assert service._evaluate_percentage_rollout(flag, "user-2") is True, (
        "the quick-start says user-2 is inside new_checkout's 10% rollout"
    )
    assert service._evaluate_percentage_rollout(flag, "user-123") is False, (
        "the quick-start says user-123 is outside new_checkout's 10% rollout"
    )
