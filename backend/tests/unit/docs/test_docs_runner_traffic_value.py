"""A ``traffic`` step's ``value``: what each converting user's event carries.

A journey that checks revenue has to know every value its users sent, so the
step never picks one at random: ``value: {mode: ramp, base, step, modulo}``
gives the ``i``-th converting user of a variant (from 0, counted in each
variant on its own, in the order the users were chosen) ``base + step * (i mod
modulo)``. An oracle recomputes the values from the journey alone.

Held here: the sequence itself, as literals, and that the model's docstring
gives the same one; what the model and the loader accept and refuse; that the
step sends each value with its event (and none with an assignment, and none at
all without a ``value``); that the step's file lists every value sent, so a
failure shows them; and that a step without a ``value`` sends and writes
exactly what it did before.

Temporary files only; no network, no browser, no git.
"""

from __future__ import annotations

import copy
import datetime
import json
import re
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

TODAY = datetime.date(2026, 10, 27)
DOCUMENTED = {"mode": "ramp", "base": 10, "step": 1, "modulo": 7}


def _ramp(**fields: Any) -> Any:
    return model.TrafficValue.model_validate({**DOCUMENTED, **fields})


# ---------------------------------------------------------------------------
# The sequence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "fields, count, sent",
    [
        pytest.param(
            {},
            16,
            [10, 11, 12, 13, 14, 15, 16, 10, 11, 12, 13, 14, 15, 16, 10, 11],
            id="the-documented-ramp-wraps-after-modulo",
        ),
        pytest.param(
            {"base": 2.5, "step": 0.5, "modulo": 3},
            7,
            [2.5, 3.0, 3.5, 2.5, 3.0, 3.5, 2.5],
            id="decimal",
        ),
        pytest.param({"base": 5, "step": 0, "modulo": 4}, 3, [5, 5, 5], id="no-step"),
        pytest.param(
            {"base": 20, "step": 3, "modulo": 1},
            4,
            [20, 20, 20, 20],
            id="modulo-one-sends-the-base-alone",
        ),
        pytest.param(
            {"base": 100, "step": -10, "modulo": 3},
            5,
            [100, 90, 80, 100, 90],
            id="a-falling-ramp",
        ),
        pytest.param({}, 0, [], id="no-converting-user"),
    ],
)
def test_the_ramp_sends_base_plus_step_times_i_mod_modulo(fields, count, sent):
    assert traffic.ramp_values(_ramp(**fields), count) == sent


def test_whole_numbers_are_sent_as_whole_numbers():
    sent = traffic.ramp_values(_ramp(), 9)
    assert [type(v) for v in sent] == [int] * 9
    assert json.dumps(sent) == "[10, 11, 12, 13, 14, 15, 16, 10, 11]"


def test_the_models_docstrings_give_the_sequence_the_runner_sends():
    """What a journey's writer reads is what the step sends."""
    for text in (model.TrafficValue.__doc__, model.__doc__):
        flat = " ".join(text.split())
        assert "base + step * (i mod modulo)" in flat
        assert "{mode: ramp, base: 10, step: 1, modulo: 7}" in flat
        listed = re.search(r"sends ((?:\d+, )+)\.\.\.", flat)
        assert listed, flat
        numbers = [int(n) for n in listed.group(1).rstrip(", ").split(", ")]
        assert numbers == traffic.ramp_values(_ramp(), len(numbers))
        assert numbers == [10, 11, 12, 13, 14, 15, 16, 10, 11]
    flat = " ".join(model.TrafficValue.__doc__.split())
    assert "first 9 users convert sends those nine values, 112 in all" in flat
    assert sum(traffic.ramp_values(_ramp(), 9)) == 112


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
SPLIT = [("Control", 50), ("Treatment", 50)]


class Tracking:
    """The tracking API, assigning by the documented hash; it keeps every body."""

    def __init__(self, track_status: int = 200):
        self.track_status = track_status
        self.assigns: List[Dict[str, Any]] = []
        self.tracks: List[Dict[str, Any]] = []

    def __call__(self, method: str, path: str, body: Optional[Dict[str, Any]]):
        if path == traffic.ASSIGN_PATH:
            self.assigns.append(dict(body))
            name = traffic.variant_for(body["user_id"], body["experiment_key"], SPLIT)
            return 200, {"variant_name": name, "assigned": True, "reason": "assigned"}
        if path == traffic.TRACK_PATH:
            self.tracks.append(dict(body))
            return self.track_status, {"event_type": body["event_type"]}
        raise AssertionError(path)


def _send(
    api: Tracking,
    value: Any,
    counts: Tuple[Tuple[str, int, int], ...] = (("Control", 6, 4), ("Treatment", 9, 9)),
):
    """Send the population; *value* only when given, as a step without one does."""
    chosen = traffic.choose("exp", SPLIT, {name: n for name, n, _ in counts}, "u")
    given = {} if value is None else {"value": value}
    outcome = traffic.send(
        api,
        experiment_key="exp",
        event="purchase",
        chosen=chosen,
        converted={name: c for name, _, c in counts},
        prefix="u",
        **given,
    )
    return chosen, outcome


def test_each_converting_users_event_carries_its_value():
    api = Tracking()
    chosen, outcome = _send(api, _ramp())
    assert outcome.problems == []
    by_user = {body["user_id"]: body for body in api.tracks}
    control = [by_user[u]["value"] for u in chosen["Control"][:4]]
    treatment = [by_user[u]["value"] for u in chosen["Treatment"]]
    # Each variant counts from 0 on its own, in the order its users were chosen.
    assert control == [10, 11, 12, 13]
    assert treatment == [10, 11, 12, 13, 14, 15, 16, 10, 11]
    # What a revenue oracle recomputes from the journey alone.
    assert (sum(control), sum(treatment)) == (46, 112)
    assert [body["user_id"] for body in api.tracks] == (
        chosen["Control"][:4] + chosen["Treatment"]
    )
    assert all(set(body) == {"user_id", "experiment_key"} for body in api.assigns)


def test_the_step_file_lists_every_value_sent():
    _, outcome = _send(Tracking(), _ramp())
    record = outcome.record()
    assert record["value"] == DOCUMENTED
    assert record["values_sent"] == {
        "Control": [10, 11, 12, 13],
        "Treatment": [10, 11, 12, 13, 14, 15, 16, 10, 11],
    }
    assert record["variants"] == {
        "Control": {"assigned": 6, "as_chosen": 6, "converted": 4},
        "Treatment": {"assigned": 9, "as_chosen": 9, "converted": 9},
    }
    assert json.loads(json.dumps(record)) == record


def test_a_refused_event_names_its_value_and_the_file_still_lists_it():
    api = Tracking(track_status=422)
    chosen, outcome = _send(api, _ramp(base=7.5, step=2.5, modulo=2))
    assert outcome.problems[0] == (
        f"tracking purchase for {chosen['Control'][0]} (value 7.5) answered 422"
    )
    assert outcome.problems[1] == (
        f"tracking purchase for {chosen['Control'][1]} (value 10.0) answered 422"
    )
    record = outcome.record()
    assert record["values_sent"] == {
        "Control": [7.5, 10.0, 7.5, 10.0],
        "Treatment": [7.5, 10.0, 7.5, 10.0, 7.5, 10.0],
    }
    assert record["variants"]["Control"]["converted"] == 0
    assert len(outcome.problems) == traffic.PROBLEMS_KEPT


def test_without_a_value_no_request_carries_one_and_the_file_is_as_before():
    api = Tracking()
    chosen, outcome = _send(api, None)
    assert api.tracks and all("value" not in body for body in api.tracks)
    assert all(
        list(body) == ["event_type", "user_id", "experiment_key"] for body in api.tracks
    )
    assert outcome.record() == {
        "experiment_key": "exp",
        "users": "u-NNNNN",
        "variants": {
            "Control": {"assigned": 6, "as_chosen": 6, "converted": 4},
            "Treatment": {"assigned": 9, "as_chosen": 9, "converted": 9},
        },
        "requests": 15 + 13,
        "problems": [],
    }
    assert list(outcome.record()) == [
        "experiment_key",
        "users",
        "variants",
        "requests",
        "problems",
    ]


def test_without_a_value_a_refused_event_reads_as_before():
    chosen, outcome = _send(Tracking(track_status=422), None)
    assert outcome.problems[0] == (
        f"tracking purchase for {chosen['Control'][0]} answered 422"
    )


def test_a_mode_the_runner_does_not_know_is_not_sent():
    unknown = types.SimpleNamespace(mode="random", base=1, step=1, modulo=2)
    with pytest.raises(traffic.TrafficError, match="no value mode 'random'"):
        traffic.ramp_values(unknown, 3)


# ---------------------------------------------------------------------------
# The model and the loader
# ---------------------------------------------------------------------------
GUIDE = """# Revenue

## Send the users

Each user who buys sends the purchase with its value.
"""

TRAFFIC = {
    "id": "traffic",
    "doc": "send-the-users",
    "do": {
        "traffic": {
            "experiment": "experiment-id",
            "key": "sdk-key",
            "as": "admin",
            "event": "purchase",
            "users": "revenue-user",
            "variants": {
                "Control": {"assigned": 6, "converted": 4},
                "Treatment": {"assigned": 9, "converted": 9},
            },
            "value": dict(DOCUMENTED),
        }
    },
    "fail": "a user is assigned other than chosen, or a purchase is refused",
}
GOOD: Dict[str, Any] = {
    "guide": "revenue.md",
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
    (docs / "revenue.md").write_text(GUIDE, encoding="utf-8")
    path = tmp_path / "journeys" / "revenue.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    context = loader.Context(
        docs_root=docs,
        inventory={
            "pages": {
                "revenue.md": {"class": "journey", "journey": "revenue", "reason": "x"}
            }
        },
        doc_examples={},
        oracles={},
        today=TODAY,
    )
    return loader.load(path, context)


def _with_value(value: Any) -> Dict[str, Any]:
    data = copy.deepcopy(GOOD)
    data["steps"][2]["do"]["traffic"]["value"] = value
    return data


def test_a_journey_with_a_value_loads(tmp_path):
    plan = _load(tmp_path, GOOD).steps[2].do.traffic
    assert plan.value == model.TrafficValue(mode="ramp", base=10, step=1, modulo=7)
    decimal = _with_value({"mode": "ramp", "base": 19.99, "step": -0.5, "modulo": 3})
    plan = _load(tmp_path, decimal).steps[2].do.traffic
    assert (plan.value.base, plan.value.step, plan.value.modulo) == (19.99, -0.5, 3)


def test_without_a_value_the_step_gives_none(tmp_path):
    data = copy.deepcopy(GOOD)
    del data["steps"][2]["do"]["traffic"]["value"]
    assert _load(tmp_path, data).steps[2].do.traffic.value is None


WHERE = "step 3 (traffic).do.traffic.value"
REFUSED = [
    pytest.param({"base": "ten"}, "base: must be a number, not 'ten'", id="base-text"),
    pytest.param({"base": "10"}, "base: must be a number, not '10'", id="base-quoted"),
    pytest.param({"base": True}, "base: must be a number, not True", id="base-bool"),
    pytest.param({"base": None}, "base: must be a number, not None", id="base-null"),
    pytest.param({"base": [10]}, "base: must be a number, not [10]", id="base-list"),
    pytest.param(
        {"base": float("nan")}, "base: must be a number, not nan", id="base-nan"
    ),
    pytest.param(
        {"base": float("inf")}, "base: must be a number, not inf", id="base-inf"
    ),
    # A whole number too large for a double is a refusal line, not a traceback.
    pytest.param(
        {"base": 10**400},
        "base: must be a number a double can hold, not a whole number of 401 digits",
        id="base-too-large-for-a-double",
    ),
    pytest.param(
        {"step": -(10**309)},
        "step: must be a number a double can hold, not a whole number of 310 digits",
        id="step-too-large-for-a-double",
    ),
    pytest.param({"step": "1"}, "step: must be a number, not '1'", id="step-text"),
    pytest.param({"step": False}, "step: must be a number, not False", id="step-bool"),
    pytest.param(
        {"step": float("-inf")}, "step: must be a number, not -inf", id="step-inf"
    ),
    pytest.param(
        {"modulo": 0},
        "modulo: Input should be greater than or equal to 1",
        id="modulo-zero",
    ),
    pytest.param(
        {"modulo": -7},
        "modulo: Input should be greater than or equal to 1",
        id="modulo-negative",
    ),
    pytest.param(
        {"modulo": 2.5},
        "modulo: Input should be a valid integer",
        id="modulo-decimal",
    ),
    pytest.param(
        {"modulo": 7.0},
        "modulo: Input should be a valid integer",
        id="modulo-whole-decimal",
    ),
    pytest.param(
        {"modulo": "7"}, "modulo: Input should be a valid integer", id="modulo-text"
    ),
    pytest.param(
        {"modulo": True}, "modulo: Input should be a valid integer", id="modulo-bool"
    ),
    pytest.param(
        {"mode": "random"},
        "mode: 'random' is not one of the values allowed here",
        id="mode-random",
    ),
    pytest.param(
        {"mode": "Ramp"},
        "mode: 'Ramp' is not one of the values allowed here",
        id="mode-case",
    ),
    pytest.param({"seed": 3}, "seed: Extra inputs are not permitted", id="unknown-key"),
    pytest.param(
        {"values": [1, 2]}, "values: Extra inputs are not permitted", id="a-list-key"
    ),
]


@pytest.mark.parametrize("change, problem", REFUSED)
def test_a_value_the_runner_cannot_recompute_is_refused(tmp_path, change, problem):
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, _with_value({**DOCUMENTED, **change}))
    assert f"{WHERE}.{problem}" in refused.value.problems


@pytest.mark.parametrize("missing", ["mode", "base", "step", "modulo"])
def test_a_value_says_all_four_fields(tmp_path, missing):
    value = {k: v for k, v in DOCUMENTED.items() if k != missing}
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, _with_value(value))
    assert f"{WHERE}.{missing}: Field required" in refused.value.problems


@pytest.mark.parametrize("value", [10, "ramp", [10, 11]])
def test_a_value_is_a_mapping(tmp_path, value):
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, _with_value(value))
    assert any(
        p.startswith(f"{WHERE}: Input should be") for p in refused.value.problems
    )


# ---------------------------------------------------------------------------
# The runner's step (Playwright stood in for, as the unit job lacks it)
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
    def __init__(self, status: int, body: Any):
        self.status = status
        self._body = body

    def json(self):
        return self._body


class _Api:
    """The experiment read, then the tracking API (``Tracking``) behind ``fetch``."""

    def __init__(self, track_status: int = 200):
        self.tracking = Tracking(track_status)

    def get(self, path, headers):
        variants = [{"name": n, "traffic_allocation": a} for n, a in SPLIT]
        return _Answer(200, {"key": "exp", "variants": variants})

    def fetch(self, path, method, headers, data):
        return _Answer(*self.tracking(method, path, data))


def _run_step(execute_module, tmp_path, step, api: _Api, number: int = 1):
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
    return runner._traffic_step(step, running, api, "j", number)


def _traffic_step(value: Optional[Dict[str, Any]]) -> Step:
    spec = copy.deepcopy(TRAFFIC)
    if value is None:
        del spec["do"]["traffic"]["value"]
    else:
        spec["do"]["traffic"]["value"] = value
    return Step.model_validate(spec)


def test_the_step_sends_its_values_and_writes_them(execute_module, tmp_path):
    api = _Api()
    observed, _ = _run_step(execute_module, tmp_path, _traffic_step(DOCUMENTED), api)
    assert observed == (
        "28 requests: Control 6 assigned as chosen, 4 converted;"
        " Treatment 9 assigned as chosen, 9 converted"
    )
    sent = [body["value"] for body in api.tracking.tracks]
    assert sent == [10, 11, 12, 13, 10, 11, 12, 13, 14, 15, 16, 10, 11]
    written = json.loads((tmp_path / "j" / "01-traffic.traffic.json").read_text())
    assert written["value"] == DOCUMENTED
    assert written["values_sent"] == {
        "Control": [10, 11, 12, 13],
        "Treatment": [10, 11, 12, 13, 14, 15, 16, 10, 11],
    }


def test_a_whole_value_reaches_the_request_body_as_a_whole_number(
    execute_module, tmp_path
):
    """The body says ``10``, not ``10.0``: the number the journey wrote."""
    api = _Api()
    _run_step(execute_module, tmp_path, _traffic_step(DOCUMENTED), api)
    bodies = list(api.tracking.tracks)
    assert [type(body["value"]) for body in bodies] == [int] * 13
    assert '"value": 10}' in json.dumps(bodies[0])
    assert all(".0" not in json.dumps(body["value"]) for body in bodies)


def test_a_step_whose_event_is_refused_shows_the_values(execute_module, tmp_path):
    with pytest.raises(execute_module._Failed) as failed:
        _run_step(
            execute_module, tmp_path, _traffic_step(DOCUMENTED), _Api(track_status=422)
        )
    assert "(value 10) answered 422" in failed.value.observed
    written = json.loads((tmp_path / "j" / "01-traffic.traffic.json").read_text())
    assert written["values_sent"]["Control"] == [10, 11, 12, 13]


def test_a_step_without_a_value_sends_and_writes_as_before(execute_module, tmp_path):
    api = _Api()
    _run_step(execute_module, tmp_path, _traffic_step(None), api)
    assert all("value" not in body for body in api.tracking.tracks)
    written = (tmp_path / "j" / "01-traffic.traffic.json").read_text()
    assert written == (
        json.dumps(
            {
                "experiment_key": "exp",
                "users": "revenue-user-NNNNN",
                "variants": {
                    "Control": {"assigned": 6, "as_chosen": 6, "converted": 4},
                    "Treatment": {"assigned": 9, "as_chosen": 9, "converted": 9},
                },
                "requests": 28,
                "problems": [],
            },
            indent=2,
        )
        + "\n"
    )
