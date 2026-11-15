"""A number that moved by an exact amount from one a journey saved earlier.

A seeded experiment already holds a history, so a journey cannot write the
count its results must show after one more purchase as a literal: it reads the
count first (an api step that saves it), acts, and expects the count it read
plus one in the arm the action counts in, and plus zero in the other. An api
step does that with ``expect.delta`` (``{path: {since, by}}``), and a table cell
with ``delta`` in place of ``oracle``.

Held here: what the model and the loader accept and refuse; the comparison
itself (a number, exactly ``before + by``; a boolean or a string is not a
number); what the log says; and the runner, which keeps a saved number as a
number, judges an answer against it, writes what it compared, and checks a
cell the same way.
"""

from __future__ import annotations

import copy
import datetime
import json
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

from docs_runner import checks, loader, log, values
from docs_runner.checks import StepFailed
from docs_runner.model import Cell, Expect, Step

TODAY = datetime.date(2026, 10, 27)
GUIDE = """# Results

## Read the results

Open the results. The table shows **Conversions** for each variant.
"""

BEFORE = {
    "id": "before",
    "doc": "read-the-results",
    "do": {"api": {"method": "GET", "path": "/api/v1/results/1", "as": "anonymous"}},
    "expect": {"status": 200},
    "save": {
        "before-one-page": {"path": "variants.1.conversions"},
        "before-standard": {"path": "variants.0.conversions"},
    },
    "fail": "the results do not answer",
}
AFTER = {
    "id": "after",
    "doc": "read-the-results",
    "do": {"api": {"method": "GET", "path": "/api/v1/results/1", "as": "anonymous"}},
    "expect": {
        "status": 200,
        "delta": {
            "variants.1.conversions": {"since": "before-one-page", "by": 1},
            "variants.0.conversions": {"since": "before-standard", "by": 0},
        },
    },
    "fail": "the purchase is not counted in one_page alone",
}
PAGE = {
    "id": "page",
    "doc": "read-the-results",
    "do": {"goto": "/experiments/1/results"},
    "expect": {
        "cells": [
            {
                "row": "one_page",
                "column": "Conversions",
                "quote": False,
                "reason": "a variant's name",
                "delta": {"since": "before-one-page", "by": 1},
            }
        ],
        "aria": '- heading "Results" [level=1]',
    },
    "fail": "the results page does not show the purchase in one_page",
}
GOOD: Dict[str, Any] = {
    "guide": "results.md",
    "stack": "compose-dev",
    "profile": "core",
    "video": False,
    "written": TODAY,
    "steps": [BEFORE, AFTER, PAGE],
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
        oracles={"double": lambda x: 2 * x},
        today=TODAY,
    )
    return loader.load(path, context)


def _refused(tmp_path: Path, data: Dict[str, Any]) -> List[str]:
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, data)
    return refused.value.problems


def _changed(step_id: str, change) -> Dict[str, Any]:
    data = copy.deepcopy(GOOD)
    change(next(step for step in data["steps"] if step["id"] == step_id))
    return data


# ---------------------------------------------------------------------------
# The model and the loader
# ---------------------------------------------------------------------------
def test_a_journey_with_deltas_loads(tmp_path):
    journey = _load(tmp_path, GOOD)
    after, page = journey.steps[1], journey.steps[2]
    assert after.expect.delta["variants.1.conversions"].by == 1
    assert page.expect.cells[0].delta.since == "before-one-page"
    assert page.expect.cells[0].oracle is None


@pytest.mark.parametrize(
    "by, message",
    [
        pytest.param(1.0, "valid integer", id="a-float"),
        pytest.param("1", "valid integer", id="a-string"),
        pytest.param(True, "valid integer", id="a-boolean"),
    ],
)
def test_by_is_a_whole_number(by, message):
    with pytest.raises(ValidationError, match=message):
        Expect.model_validate({"delta": {"a.b": {"since": "x", "by": by}}})


def test_a_delta_path_is_a_dotted_path():
    with pytest.raises(ValidationError, match="String should match pattern"):
        Expect.model_validate({"delta": {"a..b": {"since": "x", "by": 1}}})


@pytest.mark.parametrize(
    "cell",
    [
        pytest.param(
            {
                "oracle": {"name": "double", "args": {"x": 1}},
                "delta": {"since": "x", "by": 1},
            },
            id="both",
        ),
        pytest.param({}, id="neither"),
    ],
)
def test_a_cell_shows_an_oracles_number_or_a_delta_not_both(cell):
    with pytest.raises(ValidationError, match="exactly one of an oracle's number"):
        Cell.model_validate({"row": "r", "column": "c", **cell})


def test_a_delta_from_a_value_not_saved_before_is_refused(tmp_path):
    data = copy.deepcopy(GOOD)
    data["steps"] = [AFTER, BEFORE, PAGE]
    problems = _refused(tmp_path, data)
    assert "step 1 (after): 'before-one-page' is used before any step saves it" in (
        problems
    )
    assert "step 1 (after): 'before-standard' is used before any step saves it" in (
        problems
    )


def test_a_cell_delta_from_a_value_never_saved_is_refused(tmp_path):
    data = _changed(
        "page", lambda s: s["expect"]["cells"][0]["delta"].update(since="nowhere")
    )
    assert _refused(tmp_path, data) == [
        "step 3 (page): 'nowhere' is used before any step saves it"
    ]


def test_a_delta_from_a_secret_is_refused(tmp_path):
    data = _changed(
        "before", lambda s: s["save"]["before-one-page"].update(secret=True)
    )
    problems = _refused(tmp_path, data)
    assert (
        "step 2 (after): 'before-one-page' is a secret; a delta counts from a"
        " number an api step saved, never from a secret"
    ) in problems
    assert (
        "step 3 (page): 'before-one-page' is a secret; a delta counts from a"
        " number an api step saved, never from a secret"
    ) in problems


def test_a_delta_on_a_screen_step_is_refused(tmp_path):
    data = _changed(
        "page",
        lambda s: s["expect"].update(
            delta={"variants.1.conversions": {"since": "before-one-page", "by": 1}}
        ),
    )
    assert _refused(tmp_path, data) == [
        "step 3 (page): a step in a browser cannot expect ['delta']"
    ]


def test_cells_on_an_api_step_are_refused(tmp_path):
    data = _changed(
        "after", lambda s: s["expect"].update(cells=PAGE["expect"]["cells"])
    )
    assert _refused(tmp_path, data) == [
        "step 2 (after): an api step expects only status or json, not ['cells']"
    ]


def test_a_delta_alone_is_what_an_api_step_expects_of_its_answer(tmp_path):
    data = _changed("after", lambda s: s["expect"].pop("status"))
    assert _load(tmp_path, data).steps[1].expect.given() == ["delta"]


# ---------------------------------------------------------------------------
# The comparison, and what the log says
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "seen, before, by, problem",
    [
        pytest.param(8, 7, 1, "", id="one-more"),
        pytest.param(7, 7, 0, "", id="the-same"),
        pytest.param(6, 7, -1, "", id="one-fewer"),
        pytest.param(8.0, 7, 1, "", id="a-float-equal"),
        pytest.param(7.5, 6.5, 1, "", id="a-fraction-moved-exactly"),
        pytest.param(8.5, 7, 1, "= 8.5; x was 7, so +1 is 8", id="a-fraction-past"),
        pytest.param(7, 7, 1, "= 7; x was 7, so +1 is 8", id="not-counted"),
        pytest.param(9, 7, 1, "= 9; x was 7, so +1 is 8", id="counted-twice"),
        pytest.param(8, 7, 0, "= 8; x was 7, so +0 is 7", id="counted-in-the-other"),
        pytest.param("8", 7, 1, 'is "8", not a number', id="a-string"),
        pytest.param(True, 0, 1, "is true, not a number", id="a-boolean"),
        pytest.param(None, 7, 1, "is null, not a number", id="null"),
    ],
)
def test_the_delta_comparison(seen, before, by, problem):
    assert checks.delta_problem(seen, before, "x", by) == problem


@pytest.mark.parametrize("before", [None, "7", True])
def test_a_delta_counts_only_from_a_number(before):
    with pytest.raises(
        StepFailed, match="no number is saved as 'x' to count \\+1 from"
    ):
        checks.delta_problem(8, before, "x", 1)


def test_the_log_says_what_a_delta_expects(tmp_path):
    journey = _load(tmp_path, GOOD)
    assert checks.describe_expect(journey.steps[1]) == (
        "status 200; json variants.1.conversions = the number saved as"
        " before-one-page +1 exactly; json variants.0.conversions = the number"
        " saved as before-standard +0 exactly"
    )
    assert checks.describe_expect(journey.steps[2]) == (
        'the Conversions of the row "one_page" shows the number saved as'
        " before-one-page +1; the screen matches its ARIA snapshot (1 lines)"
    )


def test_a_cell_with_an_oracle_is_described_as_before():
    cell = Cell.model_validate(
        {"row": "r", "column": "c", "oracle": {"name": "double", "args": {"x": 1}}}
    )
    step = Step.model_validate(
        {
            "id": "s",
            "doc": "x",
            "fail": "x",
            "do": {"goto": "/"},
            "expect": {"cells": [cell.model_dump()]},
        }
    )
    assert checks.describe_expect(step) == 'the c of the row "r" shows double(x=1)'


@pytest.mark.parametrize(
    "value, number",
    [(7, 7), (7.5, 7.5), (0, 0), (True, None), ("7", None), (None, None), ([7], None)],
)
def test_only_a_json_number_is_kept_as_a_number(value, number):
    assert values.as_number(value) == number
    assert (values.as_number(value) is None) == (number is None)


# ---------------------------------------------------------------------------
# The runner (Playwright stood in for, as the unit job lacks it)
# ---------------------------------------------------------------------------
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
    yield module
    sys.modules.pop("docs_runner.execute", None)


class _Answer:
    def __init__(self, body: Any):
        self.status = 200
        self.body = body

    def json(self):
        return self.body


class _Api:
    """Answers each request with the next body."""

    def __init__(self, *bodies: Any):
        self.bodies = list(bodies)

    def fetch(self, path, method, headers, data):
        return _Answer(self.bodies.pop(0))


def _results(standard: Any, one_page: Any) -> Dict[str, Any]:
    return {"variants": [{"conversions": standard}, {"conversions": one_page}]}


def _runner(execute_module, tmp_path):
    return execute_module.JourneyRunner(
        None,
        None,
        log.Log(tmp_path / "results.jsonl"),
        execute_module.Settings(run_dir=tmp_path, run_id="r", sha="s"),
    )


def _running():
    from docs_runner.stacks import Running

    return Running(name="compose-dev", base_url="http://dash/", api_url="http://api")


def _steps(tmp_path):
    return _load(tmp_path, GOOD).steps


def _api_steps(execute_module, tmp_path, before: Any, after: Any):
    """The before step and the after step, answered with *before* then *after*."""
    runner = _runner(execute_module, tmp_path)
    first, second, _ = _steps(tmp_path)
    api = _Api(before, after)
    runner._api_step(first, _running(), api, "j", 1)
    return runner, lambda: runner._api_step(second, _running(), api, "j", 2)


def test_a_saved_number_is_kept_as_a_number(execute_module, tmp_path):
    runner, _ = _api_steps(execute_module, tmp_path, _results(40, 12), None)
    assert runner.numbers == {"before-one-page": 12, "before-standard": 40}
    assert runner.values == {"before-one-page": "12", "before-standard": "40"}


def test_the_counts_moved_as_expected(execute_module, tmp_path):
    runner, after = _api_steps(
        execute_module, tmp_path, _results(40, 12), _results(40, 13)
    )
    observed, snapshot = after()
    assert observed == (
        "status 200; variants.1.conversions 13, +1 from before-one-page;"
        " variants.0.conversions 40, +0 from before-standard"
    )
    written = json.loads((tmp_path / snapshot).read_text(encoding="utf-8"))
    assert written["variants.1.conversions"] == {
        "value": 13,
        "since": "before-one-page",
        "before": 12,
        "by": 1,
    }


@pytest.mark.parametrize(
    "after, problem",
    [
        pytest.param(
            _results(40, 12),
            "json variants.1.conversions = 12; before-one-page was 12, so +1 is 13",
            id="not-counted",
        ),
        pytest.param(
            _results(41, 12),
            "json variants.1.conversions = 12; before-one-page was 12, so +1 is 13;"
            " json variants.0.conversions = 41; before-standard was 40, so +0 is 40",
            id="counted-in-the-other-arm",
        ),
        pytest.param(
            {"variants": [{"conversions": 40}]},
            "json variants.1.conversions absent",
            id="absent",
        ),
        pytest.param(
            _results(40, "13"),
            'json variants.1.conversions is "13", not a number',
            id="text",
        ),
    ],
)
def test_a_count_that_did_not_move_as_expected_fails(
    execute_module, tmp_path, after, problem
):
    _, step = _api_steps(execute_module, tmp_path, _results(40, 12), after)
    with pytest.raises(execute_module._Failed) as failed:
        step()
    assert failed.value.observed == problem


def test_a_count_saved_as_text_cannot_be_counted_from(execute_module, tmp_path):
    runner, step = _api_steps(
        execute_module, tmp_path, _results(40, "12"), _results(40, 13)
    )
    assert "before-one-page" not in runner.numbers
    with pytest.raises(execute_module._Failed) as failed:
        step()
    assert failed.value.observed == (
        "json variants.1.conversions: no number is saved as 'before-one-page' to"
        " count +1 from"
    )


class _Page:
    url = "http://dash/experiments/1/results"

    def goto(self, url):
        return None

    def evaluate(self, script):
        return "/experiments/1/results"

    def locator(self, selector):
        return ("locator", selector)


def _cell_step(execute_module, monkeypatch, tmp_path, shown: str, saved: Any = 12):
    runner = _runner(execute_module, tmp_path)
    if saved is not None:
        runner.numbers["before-one-page"] = saved
    page_step = _steps(tmp_path)[2]
    # Only the cell is under test here: its table, and the ARIA snapshot beside it.
    monkeypatch.setattr(runner, "_cell_text", lambda page, cell, here: shown)
    without_aria = page_step.model_copy(
        update={"expect": page_step.expect.model_copy(update={"aria": None})}
    )
    return runner._browser_step(without_aria, _Page(), _running())


def test_a_cell_one_more_than_the_saved_count_passes(
    execute_module, monkeypatch, tmp_path
):
    observed = _cell_step(execute_module, monkeypatch, tmp_path, "13")
    assert observed == (
        "at /experiments/1/results (Conversions 13, +1 from before-one-page, each as"
        " expected)"
    )


@pytest.mark.parametrize(
    "shown, saved, message",
    [
        pytest.param(
            "12",
            12,
            "the Conversions of the row \"one_page\" shows '12'; before-one-page was"
            " 12, so +1 is 13",
            id="not-counted",
        ),
        pytest.param(
            "1,234,567",
            1234567,
            "the Conversions of the row \"one_page\" shows '1,234,567';"
            " before-one-page was 1234567, so +1 is 1234568",
            id="a-large-count-in-full",
        ),
        pytest.param("—", 12, "no number in '—'", id="no-number"),
        pytest.param(
            "13", None, "no number is saved as 'before-one-page'", id="nothing-saved"
        ),
    ],
)
def test_a_cell_that_did_not_move_as_expected_fails(
    execute_module, monkeypatch, tmp_path, shown, saved, message
):
    with pytest.raises(StepFailed) as failed:
        _cell_step(execute_module, monkeypatch, tmp_path, shown, saved)
    assert message in str(failed.value)


class _Browser:
    """A browser whose pages do nothing: every step of the journey is not run."""

    def new_context(self, **options):
        page = types.SimpleNamespace(video=None, on=lambda event, handler: None)
        return types.SimpleNamespace(
            set_default_timeout=lambda ms: None,
            new_page=lambda: page,
            close=lambda: None,
        )


def test_the_saved_numbers_are_the_journeys_own(execute_module, tmp_path):
    """A new journey starts with none: it cannot count from another's."""
    from docs_runner.model import Journey

    data = copy.deepcopy(GOOD)
    for step in data["steps"]:
        step["not_run"] = "needs-aws"
    runner = execute_module.JourneyRunner(
        None,
        _Browser(),
        log.Log(tmp_path / "results.jsonl"),
        execute_module.Settings(run_dir=tmp_path, run_id="r", sha="s"),
    )
    from docs_runner.stacks import Running

    runner.numbers["before-one-page"] = 12
    runner.run(
        Journey.model_validate(data),
        "next",
        Running(name="compose-dev", base_url="http://dash/"),
        types.SimpleNamespace(anchors={}),
    )
    assert runner.numbers == {}
