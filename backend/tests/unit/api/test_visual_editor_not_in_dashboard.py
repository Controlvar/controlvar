"""The dashboard does not open the visual editor yet (#1545).

The API serves the visual editor's sign-in and session, and the limitations
page and the environment guide say the editor is not yet available in the
dashboard. That holds while no dashboard source calls the routes that open
it: making the editor link, and the editor's own routes. The change that adds
the dashboard's editor button fails this test, and changes those sentences
with it.

Reads files only, with no git, so it runs the same in
``scripts/core_build.sh``'s copy, where ``modules/`` is gone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
#: The dashboard's source: the core app and, in a full checkout, the modules'.
SOURCE_ROOTS = (
    REPO_ROOT / "frontend" / "src",
    REPO_ROOT / "modules" / "frontend" / "src",
)
SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx"})
#: The paths that open the editor: its link, and every route under it.
EDITOR_PATHS = ("/visual/editor-tickets", "/visual-editor/")


def _sources() -> Iterator[Path]:
    for root in SOURCE_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix in SUFFIXES and "tests" not in path.relative_to(root).parts:
                yield path


def test_the_dashboard_does_not_open_the_visual_editor_yet() -> None:
    sources = list(_sources())
    assert len(sources) > 200, f"the scan sees the dashboard: {len(sources)} files"
    found = sorted(
        f"{path.relative_to(REPO_ROOT)}: {needle}"
        for path in sources
        for needle in EDITOR_PATHS
        if needle in path.read_text(encoding="utf-8", errors="replace")
    )
    assert found == [], f"the dashboard calls the visual editor: {found}"
