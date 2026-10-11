"""The docs-journey runner's one timed wait, ``poll``, and the ``after`` expectation.

The safety guide's automatic rollback happens on the safety monitor's own
timer (every 5 minutes on the Docker guide's stack), so the journey
``feature-flag-safety`` waits for it: its step ``deciding-tick`` asks the
monitor's run history again every 15 s, for up to 420 s, until a run that
started after the errors were reported has finished (``expect.after``, on the
stack's clock). Whether that wait can fail is decided by pure code, tested
here in the unit job:

* ``waiting.poll`` asks until the expectation holds or the bound passes, and
  a never-matching answer ends at the bound as not held, with the last answer;
* ``execute``'s api step, run against a stand-in for ``playwright.sync_api``
  (as ``test_docs_site_crawl.py`` does), turns that into a FAIL that shows the
  last answer, never a pass and never NOT RUN;
* ``after`` passes only for a later time, and fails for the same time, an
  earlier one, an absent path and a value that is not a time;
* the loader takes ``poll`` only on a GET api step, still refuses ``wait``
  as a sleep, and refuses ``after`` where it cannot apply;
* the safety journey is built as the plan says: nothing declared NOT RUN, the
  wait bounded at 420 s, the control read after the run that decides, and
  nothing that sets the monitor's interval.

Temporary files only; no network, no browser, no sleep.
"""

from __future__ import annotations

import copy
import datetime
import functools
import importlib
import json
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import checks, loader, registry, waiting
from docs_runner.log import FAIL, PASS
from docs_runner.model import Journey, Step

TODAY = datetime.date(2026, 10, 6)


# ---------------------------------------------------------------------------
# waiting.poll, on a clock that only moves when the poll sleeps
# ---------------------------------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: List[float] = []
        self.asked_at: List[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _poll(clock: Clock, answers: Callable[[int], Any], holds, up_to=420, every=15):
    asked = {"n": 0}

    def ask():
        asked["n"] += 1
        clock.asked_at.append(clock.now)
        return answers(asked["n"])

    return waiting.poll(
        ask,
        holds,
        up_to_seconds=up_to,
        every_seconds=every,
        clock=clock.clock,
        sleep=clock.sleep,
    )


def test_an_answer_that_holds_at_once_is_not_asked_again():
    clock = Clock()
    polled = _poll(clock, lambda n: n, lambda answer: True)
    assert (polled.held, polled.asks, polled.answer, polled.seconds) == (
        True,
        1,
        1,
        0.0,
    )
    assert clock.slept == []


def test_a_poll_stops_at_the_first_answer_that_holds():
    clock = Clock()
    polled = _poll(clock, lambda n: n, lambda answer: answer == 3)
    assert (polled.held, polled.asks, polled.answer) == (True, 3, 3)
    assert clock.asked_at == [0, 15, 30]
    assert waiting.describe(polled, 420) == "held after 30 s (3 asks)"


def test_a_never_matching_answer_ends_at_the_bound_with_the_last_answer():
    """Planted defect for the bound: an answer that never holds. The poll asks
    every 15 s, the last time at 420 s, and returns the 29th answer, not held."""
    clock = Clock()
    polled = _poll(clock, lambda n: f"answer {n}", lambda answer: False)
    assert polled.held is False
    assert polled.asks == 29
    assert polled.answer == "answer 29"
    assert clock.asked_at[-1] == 420
    assert sum(clock.slept) == 420
    assert waiting.describe(polled, 420) == (
        "did not hold within 420 s (29 asks over 420 s)"
    )


def test_the_last_ask_is_made_at_the_bound_when_it_is_not_a_multiple():
    clock = Clock()
    polled = _poll(clock, lambda n: n, lambda answer: False, up_to=20, every=15)
    assert clock.asked_at == [0, 15, 20]
    assert polled.asks == 3


def test_a_request_that_cannot_be_sent_is_not_asked_again():
    clock = Clock()

    def answers(n):
        raise ConnectionError("refused")

    with pytest.raises(ConnectionError):
        _poll(clock, answers, lambda answer: False)
    assert clock.asked_at == [0]


@pytest.mark.parametrize("up_to, every", [(10, 0), (10, 15), (10, -1)])
def test_a_poll_without_a_bound_to_wait_for_is_refused(up_to, every):
    with pytest.raises(ValueError):
        _poll(Clock(), lambda n: n, lambda answer: False, up_to=up_to, every=every)


# ---------------------------------------------------------------------------
# after: times on the stack's clock
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value, since, seconds",
    [
        ("2026-10-07T05:47:20.000000+00:00", "2026-10-07T05:45:00.000000", 140.0),
        ("2026-10-07T05:47:20Z", "2026-10-07T05:47:19.5", 0.5),
        ("2026-10-07T07:47:20+02:00", "2026-10-07T05:47:10", 10.0),
        ("2026-10-07T05:47:20", "2026-10-07T05:47:20+00:00", 0.0),
        ("2026-10-07T05:47:00+00:00", "2026-10-07T05:47:20.25", -20.25),
    ],
)
def test_seconds_after_reads_a_time_without_a_zone_as_utc(value, since, seconds):
    assert checks.seconds_after(value, since) == pytest.approx(seconds)


@pytest.mark.parametrize(
    "seconds, shown",
    [
        (282.219, "282.2"),
        (-115.0, "-115.0"),
        (1.0, "1.0"),
        (0.035, "0.035"),
        (0.1, "0.1"),
        (0.000001, "0.000001"),
        (0.0, "0"),
    ],
)
def test_a_gap_under_a_second_is_never_shown_as_none(seconds, shown):
    assert checks.shown_seconds(seconds) == shown


@pytest.mark.parametrize("value", [None, 1759816040, "", "soon", "05:47", True])
def test_a_value_that_is_not_a_time_is_refused(value):
    with pytest.raises(checks.StepFailed, match="is not a time"):
        checks.seconds_after(value, "2026-10-07T05:47:20")


# ---------------------------------------------------------------------------
# execute's api step, against a stand-in for playwright.sync_api
# ---------------------------------------------------------------------------
class PlaywrightError(Exception):
    """Stands in for ``playwright.sync_api.Error``."""


@pytest.fixture
def execute(monkeypatch):
    """``docs_runner.execute``, imported against a stand-in ``playwright.sync_api``."""
    api = types.ModuleType("playwright.sync_api")
    api.Browser = api.Page = api.Playwright = object
    api.Error = PlaywrightError
    api.expect = SimpleNamespace(set_options=lambda **options: None)
    package = types.ModuleType("playwright")
    package.sync_api = api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    monkeypatch.delitem(sys.modules, "docs_runner.execute", raising=False)
    module = importlib.import_module("docs_runner.execute")
    yield module
    sys.modules.pop("docs_runner.execute", None)
    runner = sys.modules.get("docs_runner")
    if runner is not None and hasattr(runner, "execute"):
        delattr(runner, "execute")


class Answer:
    def __init__(self, document: Any, status: int = 200):
        self.status = status
        self.document = document

    def json(self):
        if isinstance(self.document, Exception):
            raise self.document
        return self.document


class FakeApi:
    """``api.fetch``: the n-th request gets ``answers(n)``."""

    def __init__(self, answers: Callable[[int], Answer]):
        self.answers = answers
        self.asked: List[str] = []

    def fetch(self, path, method="GET", headers=None, data=None):
        self.asked.append(path)
        return self.answers(len(self.asked))


ERRORS_AT = "2026-10-07T05:45:00.000000"


def _runner(execute, tmp_path: Path, clock: Clock, monkeypatch):
    settings = execute.Settings(run_dir=tmp_path / "run", run_id="local", sha="abc")
    runner = execute.JourneyRunner(
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), settings
    )
    # The run's admin is already signed in, so no sign-in is paced.
    running = SimpleNamespace(api_url="http://api", accounts={})
    runner.tokens[("http://api", "admin")] = "token-for-the-tests"
    runner.values = {"errors-at": ERRORS_AT}
    monkeypatch.setattr(
        execute.waiting,
        "poll",
        functools.partial(waiting.poll, clock=clock.clock, sleep=clock.sleep),
    )
    return runner, running


def _tick_step(poll: bool = True, **expect: Any) -> Step:
    data: Dict[str, Any] = {
        "id": "deciding-tick",
        "doc": "auto-rollback",
        "do": {
            "api": {
                "method": "GET",
                "path": "/api/v1/scheduler/safety/history?limit=1",
                "as": "admin",
            }
        },
        "expect": expect or {"status": 200, "after": {"0.started_at": "errors-at"}},
        "fail": "no pass of the monitor finished in time",
    }
    if poll:
        data["poll"] = {"up_to_seconds": 420, "every_seconds": 15}
    return Step.model_validate(data)


def _history(started_at: Any) -> Answer:
    return Answer(
        [{"scheduler_name": "safety", "started_at": started_at, "status": "success"}]
    )


def _run_step(execute, runner, running, api, step: Step, tmp_path: Path):
    journey = SimpleNamespace(stack="compose-dev", guide="feature-flags/safety.md")
    record = runner._step(
        journey, "safety", 26, step, running, None, api, None, {}, False
    )
    written = tmp_path / "run" / "safety" / f"26-{step.id}.api.json"
    return record, json.loads(written.read_text(encoding="utf-8"))


def test_a_poll_that_never_holds_fails_at_the_bound_showing_the_last_answer(
    execute, tmp_path, monkeypatch
):
    """Tamper (d): the history never shows a run after the errors. The step
    FAILs (not NOT RUN, not a pass) after 29 asks over 420 s, and what it
    shows is the 29th answer's, the last one."""
    clock = Clock()
    runner, running = _runner(execute, tmp_path, clock, monkeypatch)
    # Each answer's run started earlier than the last: the 29th, 115 s before.
    api = FakeApi(
        lambda n: _history(f"2026-10-07T05:4{0 if n < 29 else 3}:05.000000+00:00")
    )
    record, written = _run_step(execute, runner, running, api, _tick_step(), tmp_path)
    assert record.result == FAIL
    assert record.reason == ""
    assert len(api.asked) == 29
    assert record.observed == (
        "did not hold within 420 s (29 asks over 420 s); the last answer:"
        " json 0.started_at is 115.0 s before errors-at"
    )
    assert written["poll"] == {
        "up_to_seconds": 420,
        "every_seconds": 15,
        "held": False,
        "asks": 29,
        "seconds": 420.0,
    }
    assert written["0.started_at"]["value"] == "2026-10-07T05:43:05.000000+00:00"
    assert runner.polls == [
        "safety step 26 (deciding-tick): did not hold within 420 s (29 asks over 420 s)"
    ]


def test_a_poll_passes_on_the_answer_that_holds_and_says_how_long(
    execute, tmp_path, monkeypatch
):
    clock = Clock()
    runner, running = _runner(execute, tmp_path, clock, monkeypatch)
    api = FakeApi(
        lambda n: _history(
            "2026-10-07T05:47:17.400000+00:00"
            if n >= 10
            else "2026-10-07T05:42:17.000000+00:00"
        )
    )
    record, written = _run_step(execute, runner, running, api, _tick_step(), tmp_path)
    assert record.result == PASS
    assert len(api.asked) == 10
    assert record.observed == (
        "held after 135 s (10 asks): status 200; 0.started_at 137.4 s after errors-at"
    )
    assert written["0.started_at"]["seconds_after"] == 137.4
    assert written["poll"]["held"] is True
    # The job log's line carries numbers only: no time the answer held.
    assert runner.polls == [
        "safety step 26 (deciding-tick): held after 135 s (10 asks);"
        " 0.started_at 137.4 s after errors-at"
    ]
    assert "05:47" not in runner.polls[0]


def test_a_step_without_poll_asks_once(execute, tmp_path, monkeypatch):
    clock = Clock()
    runner, running = _runner(execute, tmp_path, clock, monkeypatch)
    api = FakeApi(lambda n: _history("2026-10-07T05:40:00+00:00"))
    record, written = _run_step(
        execute, runner, running, api, _tick_step(poll=False), tmp_path
    )
    assert record.result == FAIL
    assert len(api.asked) == 1
    assert clock.slept == []
    assert "poll" not in written
    assert runner.polls == []


@pytest.mark.parametrize(
    "answer, problem",
    [
        pytest.param(
            _history("2026-10-07T05:45:00.000001+00:00"),
            None,
            id="one-microsecond-later-passes",
        ),
        pytest.param(
            _history("2026-10-07T05:45:00+00:00"),
            "json 0.started_at is at the same time as errors-at, not after it",
            id="the-same-time-fails",
        ),
        pytest.param(
            _history("2026-10-07T05:44:59.900000+00:00"),
            "json 0.started_at is 0.1 s before errors-at",
            id="earlier-fails",
        ),
        pytest.param(
            Answer([{"scheduler_name": "safety"}]),
            "json 0.started_at absent",
            id="an-absent-path-fails",
        ),
        pytest.param(
            Answer([]),
            "json 0.started_at absent",
            id="no-run-at-all-fails",
        ),
        pytest.param(
            _history("yesterday"),
            "json 0.started_at: 'yesterday' is not a time",
            id="a-value-that-is-not-a-time-fails",
        ),
        pytest.param(
            _history(None),
            "json 0.started_at: null is not a time",
            id="null-fails",
        ),
        pytest.param(
            Answer(ValueError("not JSON")),
            "the answer is not JSON",
            id="an-answer-that-is-not-json-fails",
        ),
    ],
)
def test_after_passes_only_for_a_later_time(
    execute, tmp_path, monkeypatch, answer, problem
):
    clock = Clock()
    runner, running = _runner(execute, tmp_path, clock, monkeypatch)
    api = FakeApi(lambda n: answer)
    record, _ = _run_step(
        execute, runner, running, api, _tick_step(poll=False), tmp_path
    )
    if problem is None:
        assert record.result == PASS, record.observed
        assert record.observed == (
            "status 200; 0.started_at 0.000001 s after errors-at"
        )
    else:
        assert record.result == FAIL
        assert problem in record.observed, record.observed


def test_after_fails_when_the_saved_time_is_missing(execute, tmp_path, monkeypatch):
    clock = Clock()
    runner, running = _runner(execute, tmp_path, clock, monkeypatch)
    runner.values = {}
    api = FakeApi(lambda n: _history("2026-10-07T05:47:00+00:00"))
    record, _ = _run_step(
        execute, runner, running, api, _tick_step(poll=False), tmp_path
    )
    assert record.result == FAIL
    assert "no value saved as 'errors-at'" in record.observed


# ---------------------------------------------------------------------------
# The loader: poll only on a GET api step, wait still a sleep, after in place
# ---------------------------------------------------------------------------
GUIDE = """# Sample guide

## Sign in

Open the dashboard and click **Sign in**.

## Auto-Rollback

The monitor turns the flag off.
"""

INVENTORY: Dict[str, Any] = {
    "pages": {
        "guides/sample.md": {"class": "journey", "journey": "sample", "reason": "x"}
    }
}

GOOD: Dict[str, Any] = {
    "guide": "guides/sample.md",
    "stack": "compose-dev",
    "profile": "core",
    "video": False,
    "written": TODAY,
    "steps": [
        {
            "id": "check",
            "doc": "auto-rollback",
            "do": {"api": {"method": "GET", "path": "/check", "as": "admin"}},
            "expect": {"status": 200},
            "save": {"errors-at": {"path": "last_checked"}},
            "fail": "the check does not answer",
        },
        {
            "id": "open",
            "doc": "sign-in",
            "do": {"goto": "/login"},
            "expect": {"status": 200, "aria": '- button "Sign in"'},
            "fail": "the sign-in page does not answer",
        },
        {
            "id": "tick",
            "doc": "auto-rollback",
            "do": {"api": {"method": "GET", "path": "/history", "as": "admin"}},
            "poll": {"up_to_seconds": 420, "every_seconds": 15},
            "expect": {"status": 200, "after": {"0.started_at": "errors-at"}},
            "fail": "no run after the errors",
        },
    ],
}


def _load(tmp_path: Path, data: Dict[str, Any]) -> Journey:
    docs = tmp_path / "docs"
    (docs / "guides").mkdir(parents=True, exist_ok=True)
    (docs / "guides" / "sample.md").write_text(GUIDE, encoding="utf-8")
    path = tmp_path / "journeys" / "sample.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    context = loader.Context(
        docs_root=docs,
        inventory=INVENTORY,
        doc_examples={},
        oracles={},
        today=TODAY,
    )
    return loader.load(path, context)


def _with(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    data = copy.deepcopy(GOOD)
    change(data)
    return data


def _step(data: Dict[str, Any], step_id: str) -> Dict[str, Any]:
    return next(step for step in data["steps"] if step["id"] == step_id)


def test_a_journey_that_polls_loads(tmp_path):
    journey = _load(tmp_path, GOOD)
    tick = journey.steps[2]
    assert (tick.poll.up_to_seconds, tick.poll.every_seconds) == (420, 15)
    assert tick.expect.after == {"0.started_at": "errors-at"}
    assert checks.describe_action(tick) == (
        "GET /history as admin, again every 15 s until the expectation holds,"
        " for up to 420 s"
    )
    assert checks.describe_expect(tick) == (
        "status 200; json 0.started_at later than errors-at"
    )


POLL = {"up_to_seconds": 420, "every_seconds": 15}

REFUSED = [
    pytest.param(
        lambda d: _step(d, "open").update(poll=POLL),
        "step 2 (open): poll sends an api step's request again until its expect"
        " holds; a goto step cannot poll",
        id="poll-on-a-browser-step",
    ),
    pytest.param(
        lambda d: _step(d, "tick")["do"]["api"].update(method="POST"),
        "step 3 (tick): poll sends a GET again; sending a POST again would repeat"
        " its change",
        id="poll-on-a-post",
    ),
    pytest.param(
        lambda d: _step(d, "tick").update(
            poll={"up_to_seconds": 10, "every_seconds": 15}
        ),
        "every_seconds cannot be more than up_to_seconds",
        id="poll-every-longer-than-its-bound",
    ),
    pytest.param(
        lambda d: _step(d, "tick").update(
            poll={"up_to_seconds": 420, "every_seconds": 1}
        ),
        "step 3 (tick).poll.every_seconds",
        id="poll-every-second",
    ),
    pytest.param(
        lambda d: _step(d, "tick").update(
            poll={"up_to_seconds": 3600, "every_seconds": 15}
        ),
        "step 3 (tick).poll.up_to_seconds",
        id="poll-for-an-hour",
    ),
    pytest.param(
        lambda d: _step(d, "tick").update(
            wait={"up_to_seconds": 420, "every_seconds": 15}
        ),
        "step 3 (tick): wait is a sleep; a step waits for what it expects,"
        " never for a time",
        id="wait-is-still-a-sleep",
    ),
    pytest.param(
        lambda d: _step(d, "tick")["do"]["api"].update(wait=420),
        "step 3 (tick).do.api: wait is a sleep",
        id="wait-inside-the-action-is-still-a-sleep",
    ),
    pytest.param(
        lambda d: _step(d, "tick")["expect"].update(
            after={"0.started_at": "not-saved"}
        ),
        "step 3 (tick): 'not-saved' is used before any step saves it",
        id="after-a-value-never-saved",
    ),
    pytest.param(
        lambda d: _step(d, "check")["save"]["errors-at"].update(secret=True),
        "step 3 (tick): 'errors-at' is a secret",
        id="after-a-secret",
    ),
    pytest.param(
        lambda d: _step(d, "open")["expect"].update(after={"x": "errors-at"}),
        "step 2 (open): a step in a browser cannot expect ['after']",
        id="after-on-a-browser-step",
    ),
    pytest.param(
        lambda d: _step(d, "tick")["expect"].pop("status"),
        "step 3 (tick): an api step needs status or json in expect",
        id="after-alone-is-not-enough",
    ),
]


@pytest.mark.parametrize("change, expected", REFUSED)
def test_a_planted_poll_or_after_defect_is_refused(tmp_path, change, expected):
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, _with(change))
    assert any(expected in p for p in refused.value.problems), refused.value.problems


def test_needs_scheduler_is_no_longer_a_reason_a_journey_may_declare(tmp_path):
    """A step that waits for the stack's scheduler polls for it instead."""
    assert not registry.is_declarable("needs-scheduler")
    assert "needs-scheduler" not in registry.DECLARED
    data = _with(lambda d: _step(d, "tick").update(not_run="needs-scheduler"))
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, data)
    assert any(
        "not_run 'needs-scheduler' is not a reason a journey may give" in p
        for p in refused.value.problems
    ), refused.value.problems


# ---------------------------------------------------------------------------
# The safety journey, as the plan builds it
# ---------------------------------------------------------------------------
SAFETY = RUNNER_ROOT / "journeys" / "feature-flag-safety.yaml"


def _safety() -> Journey:
    return loader.load(SAFETY, loader.context_for(REPO_ROOT, today=TODAY))


def _ids(journey: Journey) -> List[str]:
    return [step.id for step in journey.steps]


def test_the_safety_journey_runs_the_automatic_rollback():
    journey = _safety()
    assert [step.id for step in journey.steps if step.not_run] == []
    ids = _ids(journey)
    # The errors, then the run that decides, then what it decided.
    assert ids.index("report-errors") < ids.index("deciding-tick")
    assert ids[ids.index("deciding-tick") :] == [
        "deciding-tick",
        "automatic-rollback",
        "off-after-rollback",
        "rollback-audit",
        "control-stays-on",
    ]
    tick = journey.steps[ids.index("deciding-tick")]
    assert tick.do.api.method == "GET"
    assert tick.do.api.path == "/api/v1/scheduler/safety/history?limit=1"
    assert (tick.poll.up_to_seconds, tick.poll.every_seconds) == (420, 15)
    assert tick.expect.after == {"0.started_at": "errors-at"}
    # Only the step that waits for the monitor polls.
    assert [step.id for step in journey.steps if step.poll] == ["deciding-tick"]


def test_the_safety_journey_orders_its_times_on_the_stacks_clock():
    """control configured < quiet check < unhealthy check < the deciding run,
    each link an ``after`` on the stack's clock; so the run that decides
    started after the control was configured (PE C3), and the audit entry is
    later than the check made just before the errors."""
    steps = {step.id: step for step in _safety().steps}
    assert steps["control-configure"].save["control-at"].path == "updated_at"
    assert steps["quiet"].expect.after == {"last_checked": "control-at"}
    assert steps["quiet"].save["quiet-at"].path == "last_checked"
    assert steps["unhealthy"].expect.after == {"last_checked": "quiet-at"}
    assert steps["unhealthy"].save["errors-at"].path == "last_checked"
    assert steps["rollback-audit"].expect.after == {"0.timestamp": "quiet-at"}
    ids = list(steps)
    assert ids.index("control-configure") < ids.index("quiet")
    assert ids.index("quiet") == ids.index("report-errors") - 1
    assert ids.index("unhealthy") == ids.index("report-errors") + 1


def test_the_safety_journey_holds_the_exact_rollback_facts():
    steps = {step.id: step for step in _safety().steps}
    assert steps["automatic-rollback"].expect.json_ == {
        "status": "inactive",
        "rollout_percentage": 0,
    }
    evaluated = steps["off-after-rollback"].do.evaluations
    assert (evaluated.flag, evaluated.reason) == ("docs-journey-safety", "inactive")
    audit = steps["rollback-audit"].expect.json_
    assert audit["0.user_email"] == "system:safety-monitor"
    assert audit["0.user_id"] is None
    assert audit["0.action_type"] == "safety_rollback"
    assert audit["0.reason"] == (
        "Automatic rollback due to error_rate above threshold (10% > 5%)"
    )
    assert json.loads(audit["0.new_value"]) == {
        "deactivated": True,
        "new_percentage": 0,
        "paused_schedules": [],
        "previous_percentage": 5,
        "trigger_type": "automatic",
    }
    assert steps["control-stays-on"].expect.json_ == {
        "status": "active",
        "rollout_percentage": 50,
    }


def test_nothing_sets_the_safety_monitors_interval_for_the_docs_journeys():
    """The 420 s bound is derived from the default 5-minute interval, which the
    guide promises and the Docker guide's stack does not change: no file the
    docs journeys run with sets a scheduler interval."""
    config = (REPO_ROOT / "backend" / "app" / "core" / "config.py").read_text()
    assert re.search(r"^\s*SAFETY_CHECK_INTERVAL_MINUTES: int = 5$", config, re.M)
    for path in (
        REPO_ROOT / "docker-compose.yml",
        REPO_ROOT / ".github" / "workflows" / "docs-journeys.yml",
        RUNNER_ROOT / "docs_runner" / "stacks.py",
        RUNNER_ROOT / "conftest.py",
        SAFETY,
    ):
        text = path.read_text(encoding="utf-8")
        set_here = re.findall(r"(?:SAFETY|ROLLOUT)_CHECK_INTERVAL_MINUTES\s*[:=]", text)
        assert set_here == [], f"{path.relative_to(REPO_ROOT)} sets {set_here}"
