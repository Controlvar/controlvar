"""Nothing reads or sets a metric's conversion window yet (#1934).

``t1934_metric_window`` adds ``metrics.conversion_window_hours`` and ``Metric``
declares it; no other file of the core application, its Lambda functions or
its scripts names it, so no request can set a window and no result reads one.
The warehouse module has a field of the same name on its own sources, under
``modules/``, which this scan of the core leaves out.

It reads text, so it does not see a name built at run time; nothing in the core
builds column names that way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
COLUMN = "conversion_window_hours"
SCANNED = ("backend/app", "backend/lambda", "backend/scripts")
#: The only files that name the column: the model and the revision.
ALLOWED = {
    "backend/app/models/experiment.py",
    "backend/app/db/migrations/versions/t1934_metric_window_conversion_window_hours.py",
}


def naming_the_column(roots=SCANNED) -> set[str]:
    found = set()
    for root in roots:
        for path in (REPO_ROOT / root).rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".json", ".sql"}:
                continue
            if COLUMN in path.read_text(encoding="utf-8", errors="replace"):
                found.add(path.relative_to(REPO_ROOT).as_posix())
    return found


def test_only_the_model_and_the_revision_name_the_column():
    assert naming_the_column() == ALLOWED


def test_the_scan_reads_the_files_it_must():
    """Without this, a scan that read nothing would pass."""
    assert ALLOWED <= naming_the_column()
    for name in ALLOWED:
        assert (REPO_ROOT / name).is_file(), name
