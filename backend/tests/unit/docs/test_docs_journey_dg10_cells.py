"""A table cell read in the one row that has two named cells (``Cell.also``).

The results page lists each variant once per metric: an experiment with a
conversion metric and a revenue metric has two rows whose variant cell reads
``standard(control)``, so ``row`` alone names no one row and the step fails
"not one row". ``also`` names a second cell of the same row (the metric's,
``Purchaseprimary``), and the cell is read in the one row that has both.
Without ``also`` nothing changes: the same locator, the same lines.

Held here: what the model accepts and refuses; that a quoted ``also`` is held
to the guide's text like ``row`` and ``column``; the report line names both
cells; and the runner finds the row by both (Playwright stood in for by a
table, as the unit job lacks it).
"""

from __future__ import annotations

import copy
import datetime
import sys
import types
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml
from pydantic import ValidationError

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import checks, loader, log
from docs_runner.checks import StepFailed
from docs_runner.model import Cell

TODAY = datetime.date(2026, 10, 28)
GUIDE = """# Results

## Read the results

Open the results. The table has a **Sample Size** for each variant of each
metric, such as **Purchase**.
"""
CELL = {
    "row": "standard(control)",
    "also": "Purchaseprimary",
    "column": "Sample Size",
    "quote": False,
    "reason": "the page's labels for a variant and a metric",
    "delta": {"since": "users", "by": 0},
}
SAVE = {
    "id": "read",
    "doc": "read-the-results",
    "do": {"api": {"method": "GET", "path": "/api/v1/results/1", "as": "anonymous"}},
    "expect": {"status": 200},
    "save": {"users": {"path": "metrics.0.variants.0.sample_size"}},
    "fail": "the results do not answer",
}
PAGE = {
    "id": "page",
    "doc": "read-the-results",
    "do": {"goto": "/results/1"},
    "expect": {"cells": [CELL], "aria": '- heading "Results" [level=1]'},
    "fail": "the results page does not show the API's users",
}
GOOD: Dict[str, Any] = {
    "guide": "results.md",
    "stack": "compose-dev",
    "profile": "core",
    "video": False,
    "written": TODAY,
    "steps": [SAVE, PAGE],
}


def _load(tmp_path: Path, data: Dict[str, Any]):
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "results.md").write_text(GUIDE, encoding="utf-8")
    path = tmp_path / "journeys" / "results.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    context = loader.Context(
        docs_root=docs,
        inventory={
            "pages": {
                "results.md": {"class": "journey", "journey": "results", "reason": "x"}
            }
        },
        doc_examples={},
        oracles={},
        today=TODAY,
    )
    return loader.load(path, context)


def _with_cell(**change) -> Dict[str, Any]:
    data = copy.deepcopy(GOOD)
    cell = data["steps"][1]["expect"]["cells"][0]
    for key, value in change.items():
        if value is None:
            cell.pop(key, None)
        else:
            cell[key] = value
    return data


# ---------------------------------------------------------------------------
# The model and the loader
# ---------------------------------------------------------------------------
def test_also_is_optional_and_absent_by_default():
    plain = {k: v for k, v in CELL.items() if k != "also"}
    assert Cell.model_validate(plain).also is None
    assert Cell.model_validate(CELL).also == "Purchaseprimary"


@pytest.mark.parametrize(
    "also, message",
    [
        pytest.param(
            "standard(control)", "names the row's cell again", id="the-row-again"
        ),
        pytest.param("", "at least 1 character", id="empty"),
        pytest.param("two\nlines", "must be one line", id="two-lines"),
        pytest.param(3, "Input should be a valid string", id="a-number"),
    ],
)
def test_also_refuses(also, message):
    with pytest.raises(ValidationError, match=message):
        Cell.model_validate({**CELL, "also": also})


def test_a_journey_with_also_loads(tmp_path):
    journey = _load(tmp_path, GOOD)
    assert journey.steps[1].expect.cells[0].also == "Purchaseprimary"


def test_a_quoted_also_the_guide_does_not_say_is_refused(tmp_path):
    data = _with_cell(
        quote=True, reason=None, row="Purchase", column="Sample Size", also="Revenue"
    )
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, data)
    assert any(
        "expect.cells[0].also 'Revenue' is not in the guide's text" in p
        for p in refused.value.problems
    ), refused.value.problems


def test_a_quoted_also_the_guide_says_loads(tmp_path):
    """Positive control for the refusal above: the guide says "Sample Size",
    "Purchase" and "variant"."""
    data = _with_cell(quote=True, reason=None, row="variant", also="Purchase")
    assert _load(tmp_path, data).steps[1].expect.cells[0].also == "Purchase"


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def test_the_report_names_both_cells(tmp_path):
    step = _load(tmp_path, GOOD).steps[1]
    assert checks.describe_expect(step).startswith(
        'the Sample Size of the row "standard(control)" / "Purchaseprimary" shows'
        " the number saved as users +0"
    )


def test_the_report_without_also_is_as_before(tmp_path):
    step = _load(tmp_path, _with_cell(also=None, row="one_page")).steps[1]
    assert checks.describe_expect(step).startswith(
        'the Sample Size of the row "one_page" shows the number saved as users +0'
    )


# ---------------------------------------------------------------------------
# The runner (Playwright stood in for by a table)
# ---------------------------------------------------------------------------
HEADER = ["Metric", "Variant", "Sample Size", "Value"]
ROWS = [
    ["Purchaseprimary", "standard(control)", "2,077", "61.92%"],
    ["Purchaseprimary", "one_page", "1,928", "69.76%"],
    ["Order value", "standard(control)", "2,077", "58.85\nmean"],
    ["Order value", "one_page", "1,928", "64.10\nmean"],
]


class _Header:
    def __init__(self, name):
        self.name = name

    def count(self):
        return HEADER.count(self.name)

    def evaluate(self, script):
        return HEADER.index(self.name)


class _Cell:
    def __init__(self, name):
        self.name = name


class _Rows:
    def __init__(self, names: List[str]):
        self.names = names

    def filter(self, has):
        return _Rows(self.names + [has.name])

    def _matches(self):
        return [row for row in ROWS if all(name in row for name in self.names)]

    def count(self):
        return len(self._matches())

    def evaluate(self, script, index):
        (row,) = self._matches()
        return row[index]


class _Table:
    def filter(self, has):
        return self

    def get_by_role(self, role):
        assert role == "row"
        return _Rows([])


class _Page:
    def get_by_role(self, role, name=None, exact=None):
        assert exact in (None, True)
        if role == "columnheader":
            return _Header(name)
        if role == "table":
            return _Table()
        if role == "cell":
            return _Cell(name)
        raise AssertionError(role)


@pytest.fixture
def execute_module(monkeypatch):
    import importlib

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.Error = type("Error", (Exception,), {})
    sync_api.Browser = sync_api.Page = sync_api.Playwright = object
    sync_api.expect = types.SimpleNamespace(set_options=lambda **kwargs: None)
    package = types.ModuleType("playwright")
    package.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.delitem(sys.modules, "docs_runner.execute", raising=False)
    module = importlib.import_module("docs_runner.execute")

    def expect(target):
        def to_have_count(n):
            if target.count() != n:
                raise AssertionError(f"count {target.count()}, not {n}")

        return types.SimpleNamespace(to_have_count=to_have_count)

    expect.set_options = lambda **kwargs: None
    monkeypatch.setattr(module, "expect", expect)
    yield module
    sys.modules.pop("docs_runner.execute", None)


def _runner(execute_module, tmp_path):
    return execute_module.JourneyRunner(
        None,
        None,
        log.Log(tmp_path / "results.jsonl"),
        execute_module.Settings(run_dir=tmp_path, run_id="r", sha="s"),
    )


def _cell(**change) -> Cell:
    data = {**CELL, **change}
    return Cell.model_validate({k: v for k, v in data.items() if v is not None})


@pytest.mark.parametrize(
    "also, column, shown",
    [
        ("Purchaseprimary", "Sample Size", "2,077"),
        ("Order value", "Value", "58.85\nmean"),
    ],
)
def test_the_cell_is_read_in_the_row_that_has_both(
    execute_module, tmp_path, also, column, shown
):
    runner = _runner(execute_module, tmp_path)
    assert (
        runner._cell_text(_Page(), _cell(also=also, column=column), "/results/1")
        == shown
    )


def test_without_also_a_variant_listed_per_metric_is_not_one_row(
    execute_module, tmp_path
):
    """Why ``also`` exists: the same table, the row named by the variant alone."""
    runner = _runner(execute_module, tmp_path)
    with pytest.raises(StepFailed) as failed:
        runner._cell_text(_Page(), _cell(also=None), "/results/1")
    assert str(failed.value) == (
        'not one row with a cell "standard(control)" under "Sample Size" at /results/1'
    )


def test_a_row_without_the_second_cell_is_not_found(execute_module, tmp_path):
    runner = _runner(execute_module, tmp_path)
    with pytest.raises(StepFailed) as failed:
        runner._cell_text(_Page(), _cell(also="Revenue"), "/results/1")
    assert str(failed.value) == (
        'not one row with a cell "standard(control)" and a cell "Revenue" under'
        ' "Sample Size" at /results/1'
    )


def test_a_row_named_once_still_needs_no_also(execute_module, tmp_path):
    """A table that lists each variant once: ``row`` alone, as before."""
    runner = _runner(execute_module, tmp_path)
    global ROWS
    saved = ROWS
    ROWS = saved[:2]
    try:
        text = runner._cell_text(
            _Page(), _cell(also=None, row="one_page"), "/results/1"
        )
    finally:
        ROWS = saved
    assert text == "1,928"


class _Shown:
    url = "http://dash/results/1"

    def goto(self, url):
        return None

    def evaluate(self, script):
        return "/results/1"

    def locator(self, selector):
        return ("locator", selector)


def test_a_cell_that_disagrees_names_both_cells(execute_module, monkeypatch, tmp_path):
    runner = _runner(execute_module, tmp_path)
    runner.numbers["users"] = 2078
    step = _load(tmp_path, GOOD).steps[1]
    without_aria = step.model_copy(
        update={"expect": step.expect.model_copy(update={"aria": None})}
    )
    monkeypatch.setattr(runner, "_cell_text", lambda page, cell, here: "2,077")
    from docs_runner.stacks import Running

    with pytest.raises(StepFailed) as failed:
        runner._browser_step(
            without_aria, _Shown(), Running(name="compose-dev", base_url="http://dash/")
        )
    assert str(failed.value).startswith(
        'the Sample Size of the row "standard(control)" / "Purchaseprimary" shows'
        " '2,077'; users was 2078, so +0 is 2078"
    )
