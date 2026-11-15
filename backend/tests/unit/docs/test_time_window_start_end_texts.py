"""No text still says a native ``time_window`` needs ``start`` and ``end`` (#1527).

The ``Condition`` schema used to require ``start`` and ``end``, and a code
comment, a docstring, the reference page, the schema's own message and the
reason the walker gave all said so, or sent writers to an example that carried
them. #1527 removed the requirement, so each of those became false. A claims
registry entry checks that a holding test exists, not that the sentence is
true, so this test asserts the false wording is gone (T7), wherever it is
written under ``backend/``, ``docs/``, ``scripts/`` and, in a full checkout,
``modules/``. Plain file reads; no git.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
THIS = Path(__file__).resolve()
PAGE = REPO_ROOT / "docs" / "Enhanced_Rules_Engine_Reference.md"

#: Wording that #1527 made false. Each is matched with its whitespace folded,
#: so rewrapping a line cannot hide it.
FALSE_WORDING = (
    # The reference page's premise for #1527.
    "the Condition schema requires them",
    "the `Condition` schema requires them",
    "as the example does",
    # rules_engine.py's comment on TIME_WINDOW_KEYS, and test comments.
    "the Condition schema asks for",
    "the ``Condition`` schema asks for",
    "the schema asks for them",
    # targeting_adapter._time_window_value's docstring.
    "The ``Condition`` schema requires ``start``/``end``",
    # The schema's message.
    "requires value as dict with 'start' and 'end' keys",
    # #1595's reason, which sent writers to an example carrying them.
    "see the time_window example on the Targeting",
)

ROOTS = ("backend", "docs", "scripts", "modules")
SUFFIXES = {".py", ".md", ".toml"}


def _folded(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8", errors="replace").split())


def _hits() -> List[str]:
    found = []
    for root in (REPO_ROOT / name for name in ROOTS):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix not in SUFFIXES or not path.is_file():
                continue
            if path.resolve() == THIS or "node_modules" in path.parts:
                continue
            text = _folded(path)
            found.extend(
                f"{path.relative_to(REPO_ROOT).as_posix()}: {wording!r}"
                for wording in FALSE_WORDING
                if wording in text
            )
    return found


def test_no_text_says_a_native_window_needs_start_and_end():
    assert _hits() == []


def test_the_scan_reads_the_files_it_guards():
    """The scan above would pass on nothing if it read nothing: it must read
    the page and the three source files that held the wording."""
    for rel in (
        "docs/Enhanced_Rules_Engine_Reference.md",
        "backend/app/core/rules_engine.py",
        "backend/app/core/targeting_adapter.py",
        "backend/app/schemas/targeting_rule.py",
        "backend/scripts/check_targeting_rules.py",
    ):
        assert len(_folded(REPO_ROOT / rel)) > 1000, rel


def test_the_page_says_what_replaced_it():
    text = _folded(PAGE)
    assert "**`start` and `end`:** the engine does not read them." in text
    assert (
        "A native window needs at least one of `days`, `start_time` with "
        "`end_time`, or `start_date` with `end_date`" in text
    )
