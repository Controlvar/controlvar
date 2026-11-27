"""What the metric window's column adds to the code (#1934), for the tests that
copy this tree and take it out to stand for a release without it.

``t1934_metric_window``'s file, in ``Metric`` the column and its check, the
two result keys that read it (``MetricResult.conversion_window_hours`` and
``VariantResult.open_window_users``), and what writes and answers it: the
field of ``MetricBase`` and ``MetricResponse``, the clone's keyword, the key
of the experiment's dict and the audit's column. Without them the copy takes
no window and answers none, as the release before them did. The design etag's
``DESIGN_METRIC_COLUMNS_WHEN_SET`` stays: without the column it reads null and
hashes nothing, as before. Each pattern is a whole statement and is found
exactly once; if one is not found, the test that uses this says so rather than
testing the wrong tree.
"""

from __future__ import annotations

import re
from pathlib import Path

REVISION = "t1934_metric_window"

#: The file under ``backend/app``: what is removed from it.
COLUMN = {
    ("models", "experiment.py"): (
        re.compile(
            r"(?ms)^    # How many hours after a user's assignment the metric counts.*?"
            r"^    conversion_window_hours = Column\(Integer, nullable=True\)\n"
        ),
        re.compile(
            r"(?ms)^            # NULL, or a whole number of hours from 1 to 8760\n"
            r"            CheckConstraint\(\n.*?"
            r'^                name="check_metric_conversion_window",\n'
            r"^            \),\n"
        ),
    ),
    ("schemas", "results.py"): (
        re.compile(
            r"(?ms)^    open_window_users: Optional\[int\] = Field\(\n.*?^    \)\n"
        ),
        re.compile(
            r"(?ms)^    conversion_window_hours: Optional\[int\] = Field\(\n.*?^    \)\n"
        ),
    ),
    ("schemas", "experiment.py"): (
        re.compile(
            r'(?ms)^    # Strict: "24", 24\.0 and true are refused.*?'
            r"^    conversion_window_hours: Optional\[StrictInt\] = Field\(\n.*?"
            r"^    \)\n"
        ),
        re.compile(
            r"(?m)^    conversion_window_hours: Optional\[int\] = Field\(\n"
            r"^        None, description=CONVERSION_WINDOW_DESCRIPTION\n"
            r"^    \)\n"
        ),
    ),
    ("services", "experiment_service.py"): (
        re.compile(
            r"(?m)^                conversion_window_hours="
            r"metric\.conversion_window_hours,\n"
        ),
        re.compile(
            r'(?m)^                    "conversion_window_hours": '
            r"m\.conversion_window_hours,\n"
        ),
    ),
    ("services", "audit_service.py"): (
        re.compile(r'(?m)^                "conversion_window_hours",\n'),
    ),
}


def without_the_metric_window(tree: Path) -> None:
    """Remove the revision and the model's column and check from the copy at
    *tree*."""
    versions = tree / "backend" / "app" / "db" / "migrations" / "versions"
    (revision_file,) = versions.glob(f"{REVISION}_*.py")
    revision_file.unlink()
    for parts, additions in COLUMN.items():
        module = tree.joinpath("backend", "app", *parts)
        source = module.read_text(encoding="utf-8")
        for addition in additions:
            source, found = addition.subn("", source)
            assert found == 1, f"{found} matches of {addition.pattern}: update it"
        module.write_text(source, encoding="utf-8")
