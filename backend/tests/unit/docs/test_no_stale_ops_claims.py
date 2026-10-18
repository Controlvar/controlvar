"""Operator-facing text no longer says things the code and the deploy path do not do.

Each phrasing below was true once, or never, and was removed (#768, #792, #798):

* **"Restore the snapshot the deploy took before migrating."** The rollback
  runbook undoes a migration while the release that contains it is serving,
  before the API is rolled back; the emergency route is a point-in-time
  restore to a new cluster.
* **"Restore IN PLACE."** Neither Aurora restore (point-in-time or from a
  snapshot) keeps the original cluster; both create a new one.
* **"Not yet run against a real AWS account."** The forward Deploy has run to
  success on staging.
* **The issue chooser's link to Discussions.** Discussions are turned off on
  the repository, so the link answered 404.
* **The frontend Dockerfile's "the published site" default.** An empty
  `NEXT_PUBLIC_DOCS_URL` sends the dashboard's docs links to the Markdown on
  GitHub (`frontend/src/services/docs.ts`).
* **The conftest "regardless of POSTGRES_PORT".** It reads `POSTGRES_PORT`.

This is a sweep, not a proof: it forbids the removed phrasings (and close
variants), so a sentence restored by a bad merge fails here with the file and
line. A new wording of the same false claim is not caught; review is.

Reads only files; no git, so it runs the same in `scripts/core_build.sh`'s
copy, which has no `.git`.
"""

from __future__ import annotations

import re
from itertools import count
from pathlib import Path
from typing import List, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
DOCS = REPO_ROOT / "docs"

#: Non-docs files that carry the same kind of operator-facing statement.
EXTRA_FILES = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "frontend" / "Dockerfile",
    REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml",
]

#: (pattern, why it is false). Matched case-insensitively against each file
#: with comment and quote markers dropped from line starts and whitespace
#: collapsed, so a claim that wraps across lines is still found.
STALE: Tuple[Tuple[str, str], ...] = (
    (
        r"restore the snapshot the deploy took",
        "undo the migration before the API rollback; the emergency route is a "
        "point-in-time restore to a new cluster",
    ),
    (r"restor\w*\W{1,6}in[- ]place", "every Aurora restore creates a new cluster"),
    (r"in-place (cluster )?restore", "every Aurora restore creates a new cluster"),
    (
        r"not yet run against a real aws account",
        "the forward Deploy has run to success on staging",
    ),
    (r"/controlvar/discussions", "Discussions are turned off on the repository"),
    (r"empty means the published site", "empty means the Markdown on GitHub"),
    (r"dashboard links to the mkdocs site", "the default is the Markdown on GitHub"),
    (r"regardless of `?postgres_port", "the conftest reads POSTGRES_PORT"),
)

_LINE_MARKERS = re.compile(r"^[ \t]*(?:#|>)+[ \t]?", re.M)


def _files() -> List[Path]:
    return sorted(DOCS.rglob("*.md")) + [p for p in EXTRA_FILES if p.exists()]


def _flat(text: str) -> str:
    return " ".join(_LINE_MARKERS.sub("", text).split())


def test_the_swept_files_exist():
    """A sweep over nothing passes; make sure the files it names are there."""
    assert len(sorted(DOCS.rglob("*.md"))) > 50
    for path in EXTRA_FILES:
        assert path.is_file(), path


@pytest.mark.parametrize("pattern,why", STALE, ids=[p for p, _ in STALE])
def test_no_stale_ops_claim(pattern, why):
    rx = re.compile(pattern, re.I)
    hits = []
    for path in _files():
        text = path.read_text(errors="replace")
        if not rx.search(_flat(text)):
            continue
        # Name the line for the report; fall back to the file for a wrapped hit.
        lines = [
            f"{path.relative_to(REPO_ROOT)}:{n}"
            for n, line in zip(count(1), text.splitlines())
            if rx.search(line)
        ]
        hits.extend(lines or [str(path.relative_to(REPO_ROOT))])
    assert not hits, f"{why}: {hits}"
