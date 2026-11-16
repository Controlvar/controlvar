"""The demo guides describe the bandit panel the dashboard has, not a chart it never had.

`demo/DEMO_GUIDE.md` (Scene 4) and `demo/shoplab/README.md` (product sort)
told a presenter to open a "Bandit view", show an "allocation history chart"
and "watch the live updates". None of those existed. A bandit experiment's
page now has one read-only panel, "Current traffic weights", that loads once
and on Refresh, and below it a "Weight history" section: a table of past
weights, not a chart (#442, #1958).

Two checks:

* a sweep over the demo guides, every page under `docs/` and the README for
  the phrasings that were removed (and close variants). It is a sweep, not a
  proof: a new wording of the same claim is review's job;
* both demo guides name the panel by the heading the dashboard renders, read
  from the component's source, so renaming either side fails here.

Reads only files; no git, so it runs the same in `scripts/core_build.sh`'s
copy. It is in the docs-only gate's "Docs content tests" through its
directory, `backend/tests/unit/docs/`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
DEMO_GUIDE = REPO_ROOT / "demo" / "DEMO_GUIDE.md"
SHOPLAB_README = REPO_ROOT / "demo" / "shoplab" / "README.md"
PANEL_SOURCE = (
    REPO_ROOT
    / "frontend"
    / "src"
    / "components"
    / "experiments"
    / "BanditWeightsSection.tsx"
)
HISTORY_SOURCE = PANEL_SOURCE.with_name("BanditWeightHistory.tsx")

#: (pattern, why it is false), matched against lower-cased text with markup
#: dropped and whitespace collapsed, so a claim that wraps lines still matches.
STALE_BANDIT: Tuple[Tuple[str, str], ...] = (
    (
        r"bandit view",
        "there is no separate bandit view; the panel is on the experiment's page",
    ),
    (
        r"allocation (history )?chart",
        "the dashboard has no chart of bandit weights; its Weight history is a table",
    ),
    (
        r"history chart",
        "the dashboard's Weight history is a table of past weights, not a chart",
    ),
    (
        r"watch the live updates?",
        "the panel loads once and on Refresh; it does not update by itself",
    ),
)

_DROP = frozenset("*`")


def _normalise(text: str) -> Tuple[str, List[int]]:
    """Lower-case, markup dropped, whitespace runs collapsed; with source lines."""
    flat: List[str] = []
    lines: List[int] = []
    line = 1
    for char in text:
        if char == "\n":
            line += 1
        if char in _DROP:
            continue
        if char.isspace():
            if flat and flat[-1] == " ":
                continue
            char = " "
        flat.append(char.lower())
        lines.append(line)
    return "".join(flat), lines


def _hits(path: Path) -> Iterator[str]:
    flat, lines = _normalise(path.read_text(encoding="utf-8", errors="replace"))
    rel = path.relative_to(REPO_ROOT).as_posix()
    for pattern, why in STALE_BANDIT:
        for match in re.finditer(pattern, flat):
            yield f"{rel}:{lines[match.start()]}: {match.group(0)!r} -- {why}"


def _files() -> List[Path]:
    docs = sorted((REPO_ROOT / "docs").rglob("*.md"))
    assert len(docs) > 50, f"found {len(docs)} pages under docs/: the scan is broken"
    demo = sorted((REPO_ROOT / "demo").glob("*.md")) + sorted(
        (REPO_ROOT / "demo").glob("*/README.md")
    )
    assert DEMO_GUIDE in demo and SHOPLAB_README in demo, demo
    return [REPO_ROOT / "README.md", *demo, *docs]


def _panel_heading() -> str:
    source = PANEL_SOURCE.read_text(encoding="utf-8")
    match = re.search(r'id="bandit-weights-heading"[^>]*>\s*([^<{]+?)\s*</h2>', source)
    assert match, f"no bandit-weights-heading <h2> in {PANEL_SOURCE}"
    return match.group(1)


def test_no_page_promises_a_bandit_chart_or_live_updates():
    hits = [hit for path in _files() for hit in _hits(path)]
    assert not hits, (
        "these describe a bandit screen the dashboard does not have; it has a "
        "read-only 'Current traffic weights' panel and a 'Weight history' table "
        "(a table of past weights, not a chart) on the experiment's page:\n"
        + "\n".join(hits)
    )


@pytest.mark.parametrize("guide", [DEMO_GUIDE, SHOPLAB_README], ids=lambda p: p.name)
def test_demo_guides_name_the_panel_the_dashboard_renders(guide: Path):
    heading = _panel_heading()
    assert heading == "Current traffic weights"
    flat, _ = _normalise(guide.read_text(encoding="utf-8"))
    assert heading.lower() in flat, (
        f"{guide.relative_to(REPO_ROOT)} does not name the panel {heading!r}"
    )


def _history_heading() -> str:
    source = HISTORY_SOURCE.read_text(encoding="utf-8")
    match = re.search(
        r'id="bandit-weight-history-heading"[^>]*>\s*([^<{]+?)\s*</h2>', source, re.S
    )
    assert match, f"no bandit-weight-history-heading <h2> in {HISTORY_SOURCE}"
    return match.group(1)


@pytest.mark.parametrize("guide", [DEMO_GUIDE, SHOPLAB_README], ids=lambda p: p.name)
def test_demo_guides_name_the_history_the_dashboard_renders(guide: Path):
    heading = _history_heading()
    assert heading == "Weight history"
    flat, _ = _normalise(guide.read_text(encoding="utf-8"))
    assert heading.lower() in flat, (
        f"{guide.relative_to(REPO_ROOT)} does not name the section {heading!r}"
    )
