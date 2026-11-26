"""Nothing sets a metric's conversion window yet (#1934).

Results read ``metrics.conversion_window_hours`` (``event_matching``), but no
request can set it: the metric request schemas have no such field, so a
request that sends one has it dropped, and no code of the core application,
its Lambda functions or its scripts assigns it. The change that lets a request
set a window removes this test.

The scan reads text, so it does not see a name built at run time; nothing in
the core builds column names that way.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List

import pytest

from backend.app.schemas import experiment as experiment_schemas

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
COLUMN = "conversion_window_hours"
SCANNED = [
    REPO_ROOT / root for root in ("backend/app", "backend/lambda", "backend/scripts")
]
#: An attribute assignment (``metric.conversion_window_hours = 24``) or a
#: keyword argument (``Metric(conversion_window_hours=24)``).
WRITE = re.compile(rf"\.{COLUMN}\s*=(?!=)|\b{COLUMN}=(?!=)")


def writes(roots: Iterable[Path] = SCANNED) -> Dict[str, List[int]]:
    """``file -> line numbers`` of every write of the column under *roots*,
    the migrations left out."""
    found: Dict[str, List[int]] = {}
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if "migrations" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                if WRITE.search(line):
                    found.setdefault(str(path), []).append(number)
    return found


def test_no_metric_request_schema_takes_a_window():
    schemas = [
        value
        for value in vars(experiment_schemas).values()
        if isinstance(value, type)
        and value.__name__.startswith("Metric")
        and hasattr(value, "model_fields")
    ]
    # MetricBase is what create and update take; MetricResponse what they answer.
    assert {s.__name__ for s in schemas} >= {"MetricBase", "MetricResponse"}
    for schema in schemas:
        assert COLUMN not in schema.model_fields, schema.__name__


def test_nothing_assigns_the_column():
    assert writes() == {}


@pytest.mark.parametrize(
    "line",
    [
        "    metric.conversion_window_hours = 24",
        "    Metric(name='x', conversion_window_hours=24)",
        "    row.conversion_window_hours=hours",
    ],
)
def test_the_scan_sees_each_way_of_writing_one(line, tmp_path):
    (tmp_path / "planted.py").write_text(line + "\n", encoding="utf-8")
    assert writes([tmp_path]) == {str(tmp_path / "planted.py"): [1]}
