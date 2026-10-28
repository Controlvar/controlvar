"""A ``traffic`` step's ``history``: returning users' events from before the experiment.

CUPED adjusts a result for what each user did before being assigned
(``docs/api/cuped.md``). A journey that checks it needs returning users whose
history it knows exactly, so ``history: {days_before, converted: {variant:
count}}`` makes the first ``count`` users chosen for a variant send the
step's event once before anyone is assigned, with no experiment key, dated
``days_before`` days back: the page's way of sending pre-experiment history.

Held here: what the model and the loader accept and refuse; that every
history event is sent before the first assignment, by exactly the first users
chosen, with exactly the page's body and a date ``days_before`` days back;
that a refused history event leaves everyone unassigned; that the step's file
records the history; that the runner's step passes the history on; and that a
step without one sends nothing before its assignments, exactly as before.

Temporary files only; no network, no browser, no git.
"""

from __future__ import annotations

import copy
import datetime
import importlib
import json
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import loader, log, model, traffic
from docs_runner.model import Step

TODAY = datetime.date(2026, 10, 28)
NOW = datetime.datetime(2026, 10, 28, 4, 52, 15, 812345, tzinfo=datetime.timezone.utc)
#: ``NOW`` less 3 days, as the page writes a ``timestamp``.
THREE_DAYS_BACK = "2026-10-25T04:52:15Z"
HISTORY = {"days_before": 3, "converted": {"Control": 4, "Treatment": 2}}
SPLIT = [("Control", 50), ("Treatment", 50)]


def _history(**fields: Any) -> Any:
    return model.TrafficHistory.model_validate({**HISTORY, **fields})


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
class Tracking:
    """The tracking API, assigning by the documented hash; it keeps every request."""

    def __init__(self, history_status: int = 200):
        self.history_status = history_status
        self.requests: List[Tuple[str, Dict[str, Any]]] = []

    def __call__(self, method: str, path: str, body: Optional[Dict[str, Any]]):
        assert method == "POST"
        self.requests.append((path, dict(body)))
        if path == traffic.ASSIGN_PATH:
            name = traffic.variant_for(body["user_id"], body["experiment_key"], SPLIT)
            return 200, {"variant_name": name, "assigned": True, "reason": "assigned"}
        if path == traffic.TRACK_PATH:
            status = 200 if "experiment_key" in body else self.history_status
            return status, {"event_type": body["event_type"]}
        raise AssertionError(path)

    def of(self, path: str) -> List[Dict[str, Any]]:
        return [body for p, body in self.requests if p == path]


COUNTS = (("Control", 6, 3), ("Treatment", 5, 5))


def _send(api: Tracking, history: Any, counts=COUNTS, **extra: Any):
    chosen = traffic.choose("exp", SPLIT, {name: n for name, n, _ in counts}, "u")
    given = {} if history is None else {"history": history, "now": NOW}
    outcome = traffic.send(
        api,
        experiment_key="exp",
        event="checkout_completed",
        chosen=chosen,
        converted={name: c for name, _, c in counts},
        prefix="u",
        **given,
        **extra,
    )
    return chosen, outcome


def test_every_history_event_is_sent_before_the_first_assignment():
    api = Tracking()
    chosen, outcome = _send(api, _history())
    assert outcome.problems == []
    paths = [path for path, _ in api.requests]
    first_assignment = paths.index(traffic.ASSIGN_PATH)
    assert first_assignment == 4 + 2
    assert set(paths[:first_assignment]) == {traffic.TRACK_PATH}
    assert traffic.TRACK_PATH not in paths[first_assignment : first_assignment + 11]
    history = [body for body in api.of(traffic.TRACK_PATH) if "timestamp" in body]
    assert history == [body for _, body in api.requests[:first_assignment]]


def test_the_first_users_chosen_send_it_with_the_pages_body_and_date():
    api = Tracking()
    chosen, _ = _send(api, _history())
    history = [body for _, body in api.requests[:6]]
    assert [body["user_id"] for body in history] == (
        chosen["Control"][:4] + chosen["Treatment"][:2]
    )
    for body in history:
        # No experiment key and no flag key: stored as history, counted in no
        # experiment's results (api/cuped.md, "Sending pre-experiment history").
        assert list(body) == ["event_type", "user_id", "timestamp"]
        assert body["event_type"] == "checkout_completed"
        assert body["timestamp"] == THREE_DAYS_BACK
    # The converting users are the first chosen too, so they overlap: Control's
    # 3 converters are among its 4 returning users, Treatment's 2 returning
    # users among its 5 converters.
    tracked = [
        b["user_id"] for b in api.of(traffic.TRACK_PATH) if "experiment_key" in b
    ]
    assert tracked == chosen["Control"][:3] + chosen["Treatment"][:5]
    assert set(chosen["Control"][:3]) < {b["user_id"] for b in history}


def test_the_date_is_days_before_in_utc_to_the_second():
    assert traffic.history_time(_history(), NOW) == THREE_DAYS_BACK
    assert traffic.history_time(_history(days_before=30), NOW) == "2026-09-28T04:52:15Z"
    eastern = datetime.timezone(datetime.timedelta(hours=-4))
    assert traffic.history_time(_history(), NOW.astimezone(eastern)) == THREE_DAYS_BACK
    parsed = datetime.datetime.strptime(THREE_DAYS_BACK, "%Y-%m-%dT%H:%M:%SZ")
    assert NOW.replace(tzinfo=None) - parsed < datetime.timedelta(days=3, seconds=1)
    assert NOW.replace(tzinfo=None) - parsed >= datetime.timedelta(days=3)


def test_a_time_without_its_zone_is_refused():
    with pytest.raises(traffic.TrafficError, match="must carry its zone"):
        traffic.history_time(_history(), NOW.replace(tzinfo=None))


def test_without_a_given_time_the_history_is_dated_from_now():
    api = Tracking()
    chosen = traffic.choose("exp", SPLIT, {"Control": 2, "Treatment": 2}, "u")
    before = datetime.datetime.now(datetime.timezone.utc)
    traffic.send(
        api,
        experiment_key="exp",
        event="e",
        chosen=chosen,
        converted={},
        prefix="u",
        history=_history(converted={"Control": 1}),
    )
    after = datetime.datetime.now(datetime.timezone.utc)
    (body,) = api.of(traffic.TRACK_PATH)
    dated = datetime.datetime.strptime(body["timestamp"], "%Y-%m-%dT%H:%M:%SZ")
    dated = dated.replace(tzinfo=datetime.timezone.utc) + datetime.timedelta(days=3)
    assert before.replace(microsecond=0) <= dated <= after


def test_a_variant_named_with_none_or_not_named_sends_no_history():
    api = Tracking()
    chosen, outcome = _send(api, _history(converted={"Treatment": 0}))
    assert all("experiment_key" in body for body in api.of(traffic.TRACK_PATH))
    assert outcome.record()["history_sent"] == {"Control": 0, "Treatment": 0}
    api = Tracking()
    chosen, outcome = _send(api, _history(converted={"Treatment": 5}))
    history = [b["user_id"] for b in api.of(traffic.TRACK_PATH) if "timestamp" in b]
    assert history == chosen["Treatment"]
    assert outcome.record()["history_sent"] == {"Control": 0, "Treatment": 5}


def test_the_step_file_records_the_history_and_what_was_accepted():
    _, outcome = _send(Tracking(), _history())
    record = outcome.record()
    assert record["history"] == {**HISTORY, "timestamp": THREE_DAYS_BACK}
    assert record["history_sent"] == {"Control": 4, "Treatment": 2}
    assert record["variants"] == {
        "Control": {"assigned": 6, "as_chosen": 6, "converted": 3},
        "Treatment": {"assigned": 5, "as_chosen": 5, "converted": 5},
    }
    assert record["requests"] == 6 + 11 + 8
    assert list(record)[-2:] == ["history", "history_sent"]
    assert json.loads(json.dumps(record)) == record


def test_a_refused_history_event_leaves_everyone_unassigned():
    api = Tracking(history_status=422)
    chosen, outcome = _send(api, _history())
    assert api.of(traffic.ASSIGN_PATH) == []
    assert outcome.problems[0] == (
        f"tracking checkout_completed as history for {chosen['Control'][0]} answered 422"
    )
    assert len(outcome.problems) == 6
    assert outcome.record()["history_sent"] == {"Control": 0, "Treatment": 0}
    assert outcome.variants == {}


def test_refused_history_stops_at_the_problems_kept():
    api = Tracking(history_status=500)
    counts = (("Control", 30, 0), ("Treatment", 30, 0))
    _, outcome = _send(api, _history(converted={"Control": 30}), counts=counts)
    assert len(outcome.problems) == traffic.PROBLEMS_KEPT
    assert len(api.requests) == traffic.PROBLEMS_KEPT
    assert api.of(traffic.ASSIGN_PATH) == []


def test_without_a_history_nothing_is_sent_before_the_assignments():
    api = Tracking()
    chosen, outcome = _send(api, None)
    paths = [path for path, _ in api.requests]
    assert paths[:11] == [traffic.ASSIGN_PATH] * 11
    assert all("timestamp" not in body for _, body in api.requests)
    assert list(outcome.record()) == [
        "experiment_key",
        "users",
        "variants",
        "requests",
        "problems",
    ]
    assert outcome.record()["requests"] == 11 + 8


def test_history_and_a_value_go_together_and_history_carries_none():
    api = Tracking()
    ramp = model.TrafficValue(mode="ramp", base=10, step=1, modulo=7)
    _, outcome = _send(api, _history(), value=ramp)
    history = [b for b in api.of(traffic.TRACK_PATH) if "timestamp" in b]
    assert history and all("value" not in body for body in history)
    tracked = [b["value"] for b in api.of(traffic.TRACK_PATH) if "experiment_key" in b]
    assert tracked == [10, 11, 12, 10, 11, 12, 13, 14]
    assert list(outcome.record())[-4:] == [
        "value",
        "values_sent",
        "history",
        "history_sent",
    ]


def test_the_models_docstring_gives_the_rule_the_runner_follows():
    flat = " ".join(model.TrafficHistory.__doc__.split())
    assert "{days_before: 3, converted: {Control: 80, Treatment: 48}}" in flat
    assert "the first 80 Control users and the first 48 Treatment users" in flat
    assert "with no experiment key" in flat
    api = Tracking()
    counts = (("Control", 256, 40), ("Treatment", 256, 56))
    chosen, _ = _send(
        api, _history(converted={"Control": 80, "Treatment": 48}), counts=counts
    )
    history = [b["user_id"] for b in api.of(traffic.TRACK_PATH) if "timestamp" in b]
    assert history == chosen["Control"][:80] + chosen["Treatment"][:48]
    module = " ".join(model.__doc__.split())
    assert (
        "history: {days_before: <1 to 30>, converted: {<variant>: <count>}}" in module
    )


# ---------------------------------------------------------------------------
# The model and the loader
# ---------------------------------------------------------------------------
GUIDE = """# CUPED

## Send the users

Returning users sent the event before the experiment.
"""

TRAFFIC = {
    "id": "traffic",
    "doc": "send-the-users",
    "do": {
        "traffic": {
            "experiment": "experiment-id",
            "key": "sdk-key",
            "as": "admin",
            "event": "checkout_completed",
            "users": "returning-user",
            "variants": {
                "Control": {"assigned": 6, "converted": 3},
                "Treatment": {"assigned": 5, "converted": 5},
            },
            "history": copy.deepcopy(HISTORY),
        }
    },
    "fail": "a history event or a user is refused",
}
GOOD: Dict[str, Any] = {
    "guide": "cuped.md",
    "stack": "compose-dev",
    "profile": "core",
    "video": False,
    "written": TODAY,
    "steps": [
        {
            "id": "find",
            "doc": "send-the-users",
            "do": {
                "api": {"method": "GET", "path": "/api/v1/experiments/", "as": "admin"}
            },
            "expect": {"status": 200},
            "save": {"experiment-id": {"path": "items.0.id"}},
            "fail": "the experiment is not found",
        },
        {
            "id": "key",
            "doc": "send-the-users",
            "do": {
                "api": {
                    "method": "POST",
                    "path": "/api/v1/api-keys",
                    "as": "admin",
                    "body": {"name": "k"},
                }
            },
            "expect": {"status": 201},
            "save": {"sdk-key": {"path": "key", "secret": True}},
            "fail": "no key",
        },
        TRAFFIC,
    ],
}


def _load(tmp_path: Path, data: Dict[str, Any]):
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "cuped.md").write_text(GUIDE, encoding="utf-8")
    path = tmp_path / "journeys" / "cuped.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    context = loader.Context(
        docs_root=docs,
        inventory={
            "pages": {
                "cuped.md": {"class": "journey", "journey": "cuped", "reason": "x"}
            }
        },
        doc_examples={},
        oracles={},
        today=TODAY,
    )
    return loader.load(path, context)


def _with_history(history: Any) -> Dict[str, Any]:
    data = copy.deepcopy(GOOD)
    data["steps"][2]["do"]["traffic"]["history"] = history
    return data


def test_a_journey_with_a_history_loads(tmp_path):
    plan = _load(tmp_path, GOOD).steps[2].do.traffic
    assert plan.history == model.TrafficHistory(
        days_before=3, converted={"Control": 4, "Treatment": 2}
    )
    edges = _with_history(
        {"days_before": 30, "converted": {"Control": 6, "Treatment": 0}}
    )
    plan = _load(tmp_path, edges).steps[2].do.traffic
    assert (plan.history.days_before, plan.history.converted["Control"]) == (30, 6)
    one = _with_history({"days_before": 1, "converted": {"Treatment": 5}})
    assert _load(tmp_path, one).steps[2].do.traffic.history.days_before == 1


def test_without_a_history_the_step_gives_none(tmp_path):
    data = copy.deepcopy(GOOD)
    del data["steps"][2]["do"]["traffic"]["history"]
    assert _load(tmp_path, data).steps[2].do.traffic.history is None


WHERE = "step 3 (traffic).do.traffic"
REFUSED = [
    pytest.param(
        {"days_before": 0},
        ".history.days_before: Input should be greater than or equal to 1",
        id="days-zero",
    ),
    pytest.param(
        {"days_before": 31},
        ".history.days_before: Input should be less than or equal to 30",
        id="days-past-thirty",
    ),
    pytest.param(
        {"days_before": -3},
        ".history.days_before: Input should be greater than or equal to 1",
        id="days-negative",
    ),
    pytest.param(
        {"days_before": "3"},
        ".history.days_before: Input should be a valid integer",
        id="days-text",
    ),
    pytest.param(
        {"days_before": 3.0},
        ".history.days_before: Input should be a valid integer",
        id="days-decimal",
    ),
    pytest.param(
        {"days_before": True},
        ".history.days_before: Input should be a valid integer",
        id="days-bool",
    ),
    pytest.param(
        {"converted": {}},
        ".history.converted: Dictionary should have at least 1 item after validation",
        id="converted-empty",
    ),
    pytest.param(
        {"converted": {"Control": -1}},
        ".history.converted.Control: Input should be greater than or equal to 0",
        id="count-negative",
    ),
    pytest.param(
        {"converted": {"Control": "4"}},
        ".history.converted.Control: Input should be a valid integer",
        id="count-text",
    ),
    pytest.param(
        {"converted": {"Control": 2.5}},
        ".history.converted.Control: Input should be a valid integer",
        id="count-decimal",
    ),
    pytest.param(
        {"converted": {"Control": True}},
        ".history.converted.Control: Input should be a valid integer",
        id="count-bool",
    ),
    pytest.param(
        {"converted": {"Control": 7}},
        ": history gives Control 7 returning users, more than the 6 it assigns",
        id="count-above-assigned",
    ),
    pytest.param(
        {"converted": {"Variant B": 1}},
        ": history names 'Variant B', which is not one of the step's variants"
        " (Control, Treatment)",
        id="unknown-variant",
    ),
    pytest.param(
        {"lookback": 7},
        ".history.lookback: Extra inputs are not permitted",
        id="unknown-key",
    ),
]


def _problems(tmp_path: Path, history: Any) -> List[str]:
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, _with_history(history))
    return refused.value.problems


@pytest.mark.parametrize("change, problem", REFUSED)
def test_a_history_the_runner_cannot_send_as_written_is_refused(
    tmp_path, change, problem
):
    problems = _problems(tmp_path, {**HISTORY, **change})
    wanted = f"{WHERE}{problem}"
    assert any(p == wanted or p.startswith(wanted) for p in problems), problems


@pytest.mark.parametrize("missing", ["days_before", "converted"])
def test_a_history_says_both_fields(tmp_path, missing):
    history = {k: v for k, v in HISTORY.items() if k != missing}
    assert f"{WHERE}.history.{missing}: Field required" in _problems(tmp_path, history)


@pytest.mark.parametrize("history", [3, "returning", [4, 2]])
def test_a_history_is_a_mapping(tmp_path, history):
    assert any(
        p.startswith(f"{WHERE}.history: Input should be")
        for p in _problems(tmp_path, history)
    )


# ---------------------------------------------------------------------------
# The runner's step (Playwright stood in for, as the unit job lacks it)
# ---------------------------------------------------------------------------
@pytest.fixture
def execute_module(monkeypatch):
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
    def __init__(self, status: int, body: Any):
        self.status = status
        self._body = body

    def json(self):
        return self._body


class _Api:
    """The experiment read, then the tracking API (``Tracking``) behind ``fetch``."""

    def __init__(self, history_status: int = 200):
        self.tracking = Tracking(history_status)

    def get(self, path, headers):
        variants = [{"name": n, "traffic_allocation": a} for n, a in SPLIT]
        return _Answer(200, {"key": "exp", "variants": variants})

    def fetch(self, path, method, headers, data):
        return _Answer(*self.tracking(method, path, data))


def _run_step(execute_module, tmp_path, step, api: _Api):
    from docs_runner.stacks import Running

    runner = execute_module.JourneyRunner(
        None,
        None,
        log.Log(tmp_path / "results.jsonl"),
        execute_module.Settings(run_dir=tmp_path, run_id="r", sha="s"),
    )
    runner.values = {"experiment-id": "e1"}
    runner.secrets = {"sdk-key": "Sk0ExampleValueNotKept99"}
    runner.tokens[("http://api", "admin")] = "already-signed-in"
    running = Running(name="compose-dev", base_url="http://dash/", api_url="http://api")
    return runner._traffic_step(step, running, api, "j", 1)


def _step(history: Optional[Dict[str, Any]]) -> Step:
    spec = copy.deepcopy(TRAFFIC)
    if history is None:
        del spec["do"]["traffic"]["history"]
    else:
        spec["do"]["traffic"]["history"] = history
    return Step.model_validate(spec)


def test_the_step_sends_its_history_first_and_writes_it(execute_module, tmp_path):
    api = _Api()
    observed, _ = _run_step(execute_module, tmp_path, _step(HISTORY), api)
    assert observed == (
        "25 requests: Control 6 assigned as chosen, 3 converted;"
        " Treatment 5 assigned as chosen, 5 converted"
    )
    paths = [path for path, _ in api.tracking.requests]
    assert paths[:6] == [traffic.TRACK_PATH] * 6
    assert paths[6] == traffic.ASSIGN_PATH
    first = api.tracking.requests[0][1]
    assert list(first) == ["event_type", "user_id", "timestamp"]
    sent = datetime.datetime.strptime(first["timestamp"], "%Y-%m-%dT%H:%M:%SZ")
    back = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - sent
    assert datetime.timedelta(days=3) <= back < datetime.timedelta(days=3, minutes=5)
    written = json.loads((tmp_path / "j" / "01-traffic.traffic.json").read_text())
    assert written["history"]["converted"] == HISTORY["converted"]
    assert written["history"]["timestamp"] == first["timestamp"]
    assert written["history_sent"] == {"Control": 4, "Treatment": 2}


def test_a_step_whose_history_is_refused_fails_before_any_assignment(
    execute_module, tmp_path
):
    api = _Api(history_status=400)
    with pytest.raises(execute_module._Failed) as failed:
        _run_step(execute_module, tmp_path, _step(HISTORY), api)
    assert "as history for returning-user-" in failed.value.observed
    assert "answered 400" in failed.value.observed
    assert api.tracking.of(traffic.ASSIGN_PATH) == []


def test_a_step_without_a_history_sends_and_writes_as_before(execute_module, tmp_path):
    api = _Api()
    _run_step(execute_module, tmp_path, _step(None), api)
    assert api.tracking.requests[0][0] == traffic.ASSIGN_PATH
    written = json.loads((tmp_path / "j" / "01-traffic.traffic.json").read_text())
    assert "history" not in written and "history_sent" not in written
    assert written["requests"] == 19
