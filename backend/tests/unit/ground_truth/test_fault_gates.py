"""The fault scenarios' gates and driver (#1544 part D2), with no server.

The live suite (``tests/ground_truth/test_faults.py``, run by ``_platform.yml``'s
``ground-truth`` arm) sends each fault scenario through the real API. Here the
same test functions run on what the real driver records against a fake server
that answers every op as the op expects (``faults.expected_answer``) and
reports repeats as a server that stores an event once does:

* each scenario passes every one of its gates; each gate fails, with its line,
  on the defect it is there for (an answer other than the expected one, a
  repeat answered as new, a recommendation the sample-ratio rule overrides, a
  hostile class answered otherwise, a fault that never reached the server);
* the driver pauses, resumes and completes; names each variant by the id the
  create answer gave and stops when an assignment answer names another; sends
  hostile payloads as raw bytes with the key; paces ``/events`` in the SDK
  group;
* one seed for both files of the live suite; the scenarios as the plan sizes
  them, at pull-request scale whatever ``GROUND_TRUTH_SCALE`` says, each
  planted as defined on a correct server's ledger;
* the clean scenarios S1 to S4 keep the clean replay's checks: its own
  ``test_g1_*`` fails on an untagged event, an unassigned user and a missing
  view event, with ``plan_findings``' line, and its ``test_g0_*`` counts the
  users of the scenario's definition, not of the plan.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import subprocess
import sys
from dataclasses import replace
from typing import Any

import httpx
import pytest

from backend.tests.unit.ground_truth.library import (
    REPO_ROOT,
    contract,
    faults,
    generator,
    ledger,
    reference,
    replay,
)
from backend.tests.unit.ground_truth.test_replay_gates import honest_results

# tests/ground_truth is on sys.path once library is imported.
suite = importlib.import_module("test_faults")
clean = importlib.import_module("test_replay")
driver = importlib.import_module("driver")
sensitivity = importlib.import_module("sensitivity")

pytestmark = pytest.mark.unit

SEED = 20261103
#: Stand-ins with no entropy, so no secret scanner mistakes them for one.
TOKEN = "T" * 40
KEY = "K" * 32
EXPERIMENT_ID = "00000000-0000-4000-8000-00000000e001"

g = generator
SHOP = g.Scenario(
    name="F",
    experiment_key="gt-f",
    arms=(
        g.Arm("control", 50, 0.3, repeat=0.3, values=g.Values(3.0, 1.0)),
        g.Arm("treatment", 50, 0.3, repeat=0.3, values=g.Values(3.2, 1.0)),
    ),
    users=600,
    event_name="purchase",
    metrics=suite.PURCHASE,
    event_batch_size=50,
)
SKEW = faults.ClockSkew(future_users_per_arm=2)


def _case(name: str, fault: Any, **changes: Any) -> Any:
    scenario = replace(SHOP, name=name, experiment_key=f"gt-{name.lower()}", **changes)
    return suite.FaultScenario(scenario, (fault, SKEW))


#: Each fault scenario's shape, with counts this small scenario has room for.
SMALL = {
    "F1": _case("F1", faults.Duplicates(r4_drops=12, no_id_repeats=3)),
    "F3": _case(
        "F3",
        faults.LateEvents(
            r2_status_untags=8, paused_assigns=3, completed_assigns=2, paused_resends=2
        ),
    ),
    "F5": _case("F5", faults.EarlyEvents(r2_order_untags=8)),
    "F6": _case(
        "F6",
        faults.VariantMismatch(
            other_arm=5, never_assigned=4, tagged_then_assigned=3, own_arm=2
        ),
    ),
    "F7": _case(
        "F7",
        faults.ExposureLoss(lost_users=150),
        arms=(g.Arm("control", 50, 0.10), g.Arm("treatment", 50, 0.10)),
        event_name="signup",
        metrics=(g.Metric("signups", "signup", "conversion"),),
    ),
    "F9": _case("F9", faults.Hostile(per_class=1), metrics=suite.PURCHASE[:2]),
}
#: The test functions every fault scenario runs, and the one only its fault
#: runs.
COMMON = (
    "test_p_every_answer_is_the_one_its_op_expects",
    "test_g0_every_assignment_answer_is_o_assign",
    "test_g1_counts_and_means_equal_the_ledger",
    "test_g2_statistics_equal_the_reference",
    "test_z_no_answer_is_a_5xx",
    "test_s_the_fault_reached_the_ledger_as_planted",
)
OWN = {
    "F1": "test_f1d_every_repeat_is_answered_as_one",
    "F7": "test_f7_the_sample_ratio_check_overrides_the_recommendation",
    "F9": "test_f9_each_hostile_class_gets_its_listed_answer",
}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class PlanServer:
    """Answers each op of *plan*, in order, as the op expects
    (``faults.expected_answer``), with the variant ids its create answer
    gives; a ``/track`` or ``/events`` answer says ``duplicate`` and a
    ``/batch`` answer ``duplicate_count`` as a server that stores an event
    once does (its own bookkeeping, not the contract's). *answer* may change
    an answer: ``(op, status, body) -> (status, body)``, or raise."""

    def __init__(self, plan: g.Plan, clock: Clock, answer: Any = None) -> None:
        self.ops = iter(plan.ops)
        self.allocations = plan.allocations()
        self.clock = clock
        self.answer = answer
        self.status = "draft"
        self.stored: set[tuple[Any, ...]] = set()
        #: (op, request, the fake clock's time) for every request of an op.
        self.received: list[tuple[g.Op, httpx.Request, float]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == driver.SIGN_IN:
            return httpx.Response(200, json={"access_token": TOKEN})
        if path == driver.API_KEYS:
            return httpx.Response(201, json={"key": KEY})
        if path.startswith("/api/v1/results/"):
            return httpx.Response(200, json={})
        op = next(self.ops)
        self.received.append((op, request, self.clock.now))
        status, body = self._honest(op)
        if self.answer is not None:
            status, body = self.answer(op, status, body)
        return httpx.Response(status, json=body)

    def _repeat(self, event: dict[str, Any]) -> bool:
        if not event.get("event_id"):
            return False
        key = (
            event["user_id"],
            event.get("experiment_key"),
            event.get("feature_flag_key"),
            event.get("event_name") or event["event_type"],
            event["event_id"],
        )
        if key in self.stored:
            return True
        self.stored.add(key)
        return False

    def _honest(self, op: g.Op) -> tuple[int, Any]:
        if op.kind == "lifecycle":
            action = op.body["action"]
            self.status = contract.STATUS_AFTER[action]
            if action == "create":
                variants = [{"id": f"id-{n}", "name": n} for n, _ in self.allocations]
                return 201, {"id": EXPERIMENT_ID, "variants": variants}
            return 200, {"status": self.status}
        if op.kind == "hostile":
            return int(op.expect), {"detail": "refused"}
        status, body = faults.expected_answer(op, self.allocations, status=self.status)
        if not 200 <= status < 300:
            return status, body
        if op.kind in ("track", "events"):
            return status, {**body, "duplicate": self._repeat(op.body)}
        if op.kind == "batch":
            refused = {e["index"] for e in body["errors"] or ()}
            repeats = sum(
                self._repeat(e)
                for i, e in enumerate(op.body["events"])
                if i not in refused
            )
            return status, {**body, "duplicate_count": repeats}
        return status, body


def honest_fault_results(faulted: Any) -> Any:
    """What a correct ``GET /api/v1/results/{id}`` answers, with the
    sample-ratio check's warning and the recommendation it overrides."""
    if faulted.expected is None:
        return None
    experiment = faulted.experiment
    results = honest_results(faulted.scenario, experiment)
    warning = results["srm"]["p_value"] < faults.SRM_ALARM
    results["srm"]["warning"] = warning
    results["summary"].update(
        recommendation=suite.SRM_RECOMMENDATION if warning else "CONTINUE_TESTING",
        recommendation_reason=(
            f"{suite.SRM_REASON_PREFIX} the observed split does not match"
            if warning
            else "Not significant yet."
        ),
    )
    return results


def fake_run(case: Any, tmp_path: Any, *, answer: Any = None) -> tuple[Any, Any]:
    """*case* sent by the real driver to a ``PlanServer``: the record the live
    suite's gates read, with a correct server's results, and the server."""
    clock = Clock()
    server = PlanServer(case.plan(SEED), clock, answer)
    tmp_path.mkdir(parents=True, exist_ok=True)
    with httpx.Client(
        base_url="http://replay.test", transport=httpx.MockTransport(server)
    ) as client:
        session = driver.Driver(client, clock=clock.time, sleep=clock.sleep)
        session.sign_in("admin@example.com", "admin")
        session.create_api_key()
        faulted = suite.run(case, session, tmp_path, seed=SEED)
    faulted.results = honest_fault_results(faulted)
    return faulted, server


def failed(test: Any, faulted: Any) -> str:
    with pytest.raises(pytest.fail.Exception) as raised:
        test(faulted)
    return str(raised.value)


# ---------------------------------------------------------------------------
# A correct server passes every gate; each gate fails on its defect
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(SMALL))
def test_each_fault_scenario_passes_its_gates_on_a_correct_server(name, tmp_path):
    faulted, _ = fake_run(SMALL[name], tmp_path)
    assert faulted.failure is None and faulted.unexpected == []
    for test in (*COMMON, *([OWN[name]] if name in OWN else [])):
        getattr(suite, test)(faulted)


def test_every_gate_function_runs_on_the_fake_server():
    """Every test of the live file is one of the above, so none goes untried
    here."""
    tests = {name for name in dir(suite) if name.startswith("test_")}
    assert tests == {*COMMON, *OWN.values()}


def test_p_names_an_answer_other_than_the_one_expected(tmp_path):
    def stored(op, status, body):
        user = op.body.get("user_id", "")
        if op.kind == "track" and faults.f9_class(user) == "nul-user-id":
            return 200, {"duplicate": False}
        return status, body

    faulted, _ = fake_run(SMALL["F9"], tmp_path, answer=stored)
    message = failed(suite.test_p_every_answer_is_the_one_its_op_expects, faulted)
    assert message.startswith(f"P failed: scenario F9, seed {SEED}"), message
    assert "(track /track, class nul-user-id): answered 200, want 422" in message
    assert f"GROUND_TRUTH_SEED={SEED} python -m pytest {suite.FILE} -k F9" in message


def test_g0_counts_the_users_the_plan_assigns(tmp_path):
    """F7 leaves users unassigned by design: G0 asks an answer of every user
    the plan assigns, which a server that assigns nobody in a batch fails."""
    faulted, _ = fake_run(SMALL["F7"], tmp_path)
    answers = clean.assignment_answers(faulted.records, faulted.scenario.experiment_key)
    assert answers < faulted.scenario.users  # the clean replay's count would fail
    suite.test_g0_every_assignment_answer_is_o_assign(faulted)

    def nobody(op, status, body):
        if op.kind == "assign_batch":
            rows = [{**r, "assigned": False} for r in body["assignments"]]
            return status, {**body, "assignments": rows}
        return status, body

    faulted, _ = fake_run(SMALL["F7"], tmp_path / "nobody", answer=nobody)
    message = failed(suite.test_g0_every_assignment_answer_is_o_assign, faulted)
    users = suite.assigned_by_plan(faulted.plan)
    assert "assignment answers: answered " in message, message
    assert f", users {users} (at least one per user)" in message, message


def test_f1d_names_a_repeat_answered_as_new(tmp_path):
    def forgetful(op, status, body):
        if op.kind in ("track", "events") and 200 <= status < 300:
            return status, {**body, "duplicate": False}
        if op.kind == "batch" and 200 <= status < 300:
            return status, {**body, "duplicate_count": 0}
        return status, body

    honest, _ = fake_run(SMALL["F1"], tmp_path)
    assert len(honest.expected.repeats) == SMALL["F1"].planted[0].r4_drops == 12
    faulted, _ = fake_run(SMALL["F1"], tmp_path / "forgetful", answer=forgetful)
    message = failed(suite.test_f1d_every_repeat_is_answered_as_one, faulted)
    assert message.startswith(f"F1d failed: scenario F1, seed {SEED}: R4 drops 12")
    assert "(/track) 'duplicate': answered False, R4 True (exact)" in message
    assert "(/events) 'duplicate': answered False, R4 True (exact)" in message
    assert "'duplicate_count': answered 0, R4 " in message


def test_f1d_names_a_repeat_the_server_did_not_report_at_all(tmp_path):
    """A server that says nothing about repeats (no ``duplicate``) fails too."""

    def silent(op, status, body):
        if op.kind in ("track", "events") and 200 <= status < 300:
            return status, {k: v for k, v in body.items() if k != "duplicate"}
        return status, body

    faulted, _ = fake_run(SMALL["F1"], tmp_path, answer=silent)
    message = failed(suite.test_f1d_every_repeat_is_answered_as_one, faulted)
    assert "'duplicate': answered None, R4 False (exact)" in message, message


@pytest.mark.parametrize(
    "summary, srm, line",
    [
        (
            {"recommendation": "CONTINUE_TESTING"},
            {},
            "summary 'recommendation': /results 'CONTINUE_TESTING', the SRM rule"
            " 'INCONCLUSIVE' (exact)",
        ),
        ({}, {"warning": False}, "srm 'warning': /results False, the SRM rule True"),
        (
            {"recommendation_reason": "Not significant yet."},
            {},
            "summary 'recommendation_reason': /results 'Not significant yet.', the"
            " SRM rule 'Sample-ratio mismatch:' (starts with)",
        ),
    ],
    ids=["recommendation", "warning", "reason"],
)
def test_f7_names_what_the_sample_ratio_rule_did_not_override(
    tmp_path, summary, srm, line
):
    faulted, _ = fake_run(SMALL["F7"], tmp_path)
    suite.test_f7_the_sample_ratio_check_overrides_the_recommendation(faulted)
    faulted.results["summary"].update(summary)
    faulted.results["srm"].update(srm)
    message = failed(
        suite.test_f7_the_sample_ratio_check_overrides_the_recommendation, faulted
    )
    assert line in message, message


def test_f9_names_a_class_answered_otherwise(tmp_path):
    def lenient(op, status, body):
        if op.kind == "hostile" and op.body["class"] == "body-over-5-mib":
            return 200, {}
        user = op.body.get("user_id", "")
        if op.kind == "events" and faults.f9_class(user) == "edge-max-events":
            return 200, {"duplicate": False}
        return status, body

    faulted, _ = fake_run(SMALL["F9"], tmp_path, answer=lenient)
    message = failed(suite.test_f9_each_hostile_class_gets_its_listed_answer, faulted)
    assert (
        "class 'body-over-5-mib' (/track, raw): answered '200', listed '413'" in message
    )
    assert "class 'edge-max-events' (/events): answered '200', listed '422'" in message


def test_f9_names_a_class_that_got_no_answer(tmp_path):
    def unreachable(op, status, body):
        if op.kind == "hostile" and op.body["class"] == "body-over-5-mib":
            raise httpx.WriteError("broken pipe")
        return status, body

    faulted, _ = fake_run(SMALL["F9"], tmp_path, answer=unreachable)
    assert any(
        "class 'body-over-5-mib' got no answer (WriteError)" in line
        for line in faulted.unexpected
    ), faulted.unexpected
    message = failed(suite.test_f9_each_hostile_class_gets_its_listed_answer, faulted)
    assert "class 'body-over-5-mib': sends answered 0, planted 1 (exact)" in message
    message = failed(suite.test_s_the_fault_reached_the_ledger_as_planted, faulted)
    assert (
        "class 'body-over-5-mib': the definition plants 1 sends; the run shows 0"
        in (message)
    )


def test_s_fails_when_a_fault_never_reached_the_server(tmp_path):
    """Every /events send refused: P names each, and S says the TEST is
    broken, because the gates above would pass a server with R8's defect."""

    def refused(op, status, body):
        if op.kind == "events":
            return 404, {"detail": "no such variant"}
        return status, body

    faulted, _ = fake_run(SMALL["F6"], tmp_path, answer=refused)
    message = failed(suite.test_s_the_fault_reached_the_ledger_as_planted, faulted)
    assert message.startswith(f"sensitivity failed: scenario F6, seed {SEED}")
    assert "the TEST is broken" in message
    assert (
        "F6 (R8): the definition plants 9 /events events counted in no variant"
        " (cases i and ii); the ledger shows 0 (the fault touched nothing)" in message
    )
    message = failed(suite.test_p_every_answer_is_the_one_its_op_expects, faulted)
    assert "answered 404, want a 2xx (accepted)" in message


def test_a_run_that_did_not_complete_fails_every_gate_naming_why(tmp_path):
    def broken(op, status, body):
        if op.kind == "batch":
            return 500, {"detail": "boom"}
        return status, body

    faulted, _ = fake_run(SMALL["F5"], tmp_path, answer=broken)
    assert faulted.server_errors == 1 and "answered 500" in faulted.failure
    for test in COMMON:
        message = failed(getattr(suite, test), faulted)
        assert "answered 500" in message or "5xx seen: 1" in message, (test, message)


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------
def test_the_driver_pauses_resumes_and_completes(tmp_path):
    faulted, server = fake_run(SMALL["F3"], tmp_path)
    steps = [
        (op.body["action"], request.method, request.url.path)
        for op, request, _ in server.received
        if op.kind == "lifecycle"
    ]
    base = f"{driver.EXPERIMENTS}{EXPERIMENT_ID}"
    assert steps == [
        ("create", "POST", driver.EXPERIMENTS),
        ("start", "POST", f"{base}/start"),
        ("pause", "POST", f"{base}/pause"),
        ("resume", "POST", f"{base}/start"),
        ("complete", "POST", f"{base}/complete"),
    ]
    actions = [r["action"] for r in faulted.records if r["kind"] == "action"]
    assert actions == ["create", "start", "pause", "resume", "complete"]


def test_a_lifecycle_step_with_no_rule_stops_the_run(tmp_path, monkeypatch):
    rules = {k: v for k, v in driver.LIFECYCLE_PATHS.items() if k != "pause"}
    monkeypatch.setattr(driver, "LIFECYCLE_PATHS", rules)
    faulted, _ = fake_run(SMALL["F3"], tmp_path)
    assert faulted.failure == "no rule to send lifecycle action 'pause'"


def test_events_name_the_experiment_and_variant_by_the_create_answer_s_ids(tmp_path):
    faulted, server = fake_run(SMALL["F6"], tmp_path)
    sent = [(op, request) for op, request, _ in server.received if op.kind == "events"]
    assert len(sent) == 14
    for op, request in sent:
        body = json.loads(request.content)
        assert request.url.path == ledger.EVENTS
        assert body["experiment_id"] == EXPERIMENT_ID
        assert body["variant_id"] == f"id-{op.body['variant']}"
        assert "experiment_key" not in body and "variant" not in body
    recorded = [
        r["variant"]
        for r in faulted.records
        if r["kind"] == "event" and r["route"] == "/events"
    ]
    assert recorded == [op.body["variant"] for op, _ in sent]


@pytest.mark.parametrize("route", [ledger.ASSIGN_BATCH, ledger.ASSIGN])
def test_an_answer_naming_a_variant_by_another_id_stops_the_run(tmp_path, route):
    def swapped(op, status, body):
        if op.route == route == ledger.ASSIGN_BATCH:
            names = [v["name"] for v in body["variants"].values()]
            ids = list(body["variants"])
            variants = {i: {"name": n} for i, n in zip(ids, reversed(names))}
            return status, {**body, "variants": variants}
        if op.route == route == ledger.ASSIGN:
            return status, {**body, "variant_id": "id-elsewhere"}
        return status, body

    faulted, _ = fake_run(SMALL["F6"], tmp_path, answer=swapped)
    assert faulted.failure is not None
    assert "by another id than the experiment's create answer gave" in faulted.failure


def test_a_create_answer_without_the_variant_ids_stops_the_run(tmp_path):
    def bare(op, status, body):
        if op.kind == "lifecycle" and op.body["action"] == "create":
            return status, {"id": EXPERIMENT_ID}
        return status, body

    faulted, _ = fake_run(SMALL["F1"], tmp_path, answer=bare)
    assert faulted.failure == (
        "create of experiment gt-f1 answered no id for variant(s)"
        " ['control', 'treatment']"
    )


def test_hostile_payloads_go_raw_with_the_key_and_name_the_experiment(tmp_path):
    faulted, server = fake_run(SMALL["F9"], tmp_path)
    sent = [(op, request) for op, request, _ in server.received if op.kind == "hostile"]
    assert sorted(op.body["class"] for op, _ in sent) == sorted(faults.F9_HOSTILE)
    for op, request in sent:
        assert request.content == op.body["content"].encode("utf-8")
        assert request.headers["X-API-Key"] == KEY
        assert request.headers["Content-Type"] == "application/json"
        assert "Authorization" not in request.headers
        assert SMALL["F9"].scenario.experiment_key.encode() in request.content
    assert faulted.hostile_sends == faults.hostile_counts(faulted.plan)
    assert [status for _, _, status in faulted.hostile] == [
        int(faults.F9_HOSTILE[op.body["class"]]) for op, _ in sent
    ]


def test_events_are_paced_in_the_sdk_group(tmp_path):
    _, server = fake_run(SMALL["F6"], tmp_path)
    times = [at for op, _, at in server.received if op.kind == "events"]
    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    assert len(gaps) > 10
    assert min(gaps) >= driver.MIN_INTERVAL[ledger.EVENTS] - 1e-9


# ---------------------------------------------------------------------------
# One seed; the scenarios as the plan sizes them
# ---------------------------------------------------------------------------
_SEEDS = (
    "import sys; sys.path.insert(0, 'tests/ground_truth');"
    " import test_replay, test_faults; from ground_truth import seed;"
    " print('seeds', test_replay.SEED, test_faults.SEED, seed.SEED)"
)


def test_both_files_of_the_live_suite_take_one_seed_when_none_is_given():
    """Each file resolving its own would draw two seeds, and a failure would
    print one that does not reproduce it."""
    env = {k: v for k, v in os.environ.items() if k != "GROUND_TRUTH_SEED"}
    result = subprocess.run(
        [sys.executable, "-B", "-c", _SEEDS],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    (line,) = [x for x in result.stdout.splitlines() if x.startswith("seeds ")]
    replay_seed, faults_seed, resolved = line.split()[1:]
    assert replay_seed == faults_seed == resolved, line


def test_the_reproduce_command_names_its_file():
    assert suite.reproduce("F1", seed=7) == (
        "GROUND_TRUTH_SEED=7 python -m pytest tests/ground_truth/test_faults.py -k F1"
    )
    assert clean.reproduce("S2", seed=7, scale="pr") == (
        "GROUND_TRUTH_SEED=7 python -m pytest tests/ground_truth/test_replay.py -k S2"
    )


def _shape(cases: Any) -> dict[str, Any]:
    return {
        name: (
            case.scenario.users,
            [(a.name, a.allocation) for a in case.scenario.arms],
            [(m.event_name, m.type) for m in case.scenario.metrics],
            case.planted,
        )
        for name, case in cases.items()
    }


def test_the_fault_scenarios_are_the_plan_s():
    """F1, F3, F5 and F6 at 2,000 users, F7 at 10,000 (never below 8,000: a
    smaller loss can miss the sample-ratio alarm), F9 at 1,000; every count
    named; F4 in every one."""
    skew = faults.ClockSkew(future_users_per_arm=3)
    purchase = [
        ("purchase", "conversion"),
        ("purchase", "count"),
        ("purchase", "revenue"),
    ]
    halves = [("control", 50), ("treatment", 50)]
    assert _shape(suite.scenarios(SEED)) == {
        "F1": (2000, halves, purchase, (faults.Duplicates(60, 10), skew)),
        "F3": (2000, halves, purchase, (faults.LateEvents(40, 20, 10, 10), skew)),
        "F5": (2000, halves, purchase, (faults.EarlyEvents(40), skew)),
        "F6": (2000, halves, purchase, (faults.VariantMismatch(40, 40, 20, 20), skew)),
        "F7": (
            10000,
            halves,
            [("signup", "conversion")],
            (faults.ExposureLoss(1000), skew),
        ),
        "F9": (1000, halves, purchase[:2], (faults.Hostile(1), skew)),
    }
    keys = [case.scenario.experiment_key for case in suite.scenarios(SEED).values()]
    assert len(set(keys)) == 6 and all(str(SEED) in key for key in keys)
    clean_keys = {s.experiment_key for s in clean.scenarios(SEED, 1).values()}
    assert not clean_keys & set(keys)


_SCALE = (
    "import sys; sys.path.insert(0, 'tests/ground_truth'); import test_faults;"
    " print('users', sorted((n, c.scenario.users) for n, c in test_faults.SCENARIOS.items()))"
)


def test_the_fault_scenarios_read_no_scale():
    """``GROUND_TRUTH_SCALE=nightly`` (the nightly arm's) changes nothing here:
    the fault scenarios are pull-request scale only."""
    env = {**os.environ, "GROUND_TRUTH_SCALE": "nightly", "GROUND_TRUTH_SEED": "7"}
    result = subprocess.run(
        [sys.executable, "-B", "-c", _SCALE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    want = sorted((n, c.scenario.users) for n, c in suite.scenarios(7).items())
    assert f"users {want}" in result.stdout, result.stdout


@pytest.mark.parametrize("name", sorted(suite.scenarios(SEED)))
def test_each_pull_request_scenario_reaches_a_correct_server_as_planted(name):
    """At the live suite's own sizes: the plan injects (enough eligible events
    and users), and a correct server's ledger shows exactly the planted
    counts."""
    case = suite.scenarios(SEED)[name]
    plan = case.plan(SEED)
    records = faults.expected_records(plan, faults.DECLARED)
    sensitivity.sensitivity(
        case.planted,
        plan,
        records,
        where=f"scenario {name}, seed {SEED}",
        hostile_sends=faults.hostile_counts(plan),
    )
    experiment = contract.valid_events(records).experiments[
        case.scenario.experiment_key
    ]
    assert experiment.variant_mismatches == ()


def test_f7_s_loss_shows_at_its_size():
    """The F7 definition at 10,000 users: the reference's sample-ratio
    p-value is far below the alarm on a correct server's ledger."""
    case = suite.scenarios(SEED)["F7"]
    records = faults.expected_records(case.plan(SEED), faults.DECLARED)
    experiment = contract.valid_events(records).experiments[
        case.scenario.experiment_key
    ]
    sizes = [experiment.sample_size(v) for v in experiment.variants]
    assert reference.srm_p(sizes, experiment.allocations) < 1e-12, sizes


# ---------------------------------------------------------------------------
# S1 to S4 keep the clean replay's checks
# ---------------------------------------------------------------------------
CLEAN_SCENARIOS = clean.scenarios(SEED, 1)


def _clean_replayed(name: str, tmp_path: Any, plant: str | None = None) -> Any:
    """Scenario *name* sent to the writer-level fake server, with one plant,
    and a correct ``/results`` built from the ledger."""
    scenario = CLEAN_SCENARIOS[name]
    plan = g.simulate(SEED, scenario)
    answer = None
    if plant == "untagged-event":
        # One /track event moved before every assignment.
        ops = list(plan.ops)
        at = next(i for i, op in enumerate(ops) if op.kind == "track")
        ops.insert(2, ops.pop(at))
        plan = replace(plan, ops=tuple(ops))
    elif plant == "unassigned-user":

        def answer(op, default):
            status, body = default
            if op.kind == "assign_batch" and not answer.done:
                answer.done = True
                rows = [{**body["assignments"][0], "assigned": False}]
                return status, {**body, "assignments": rows + body["assignments"][1:]}
            return default

        answer.done = False
    elif plant == "missing-exposure":
        last = max(i for i, op in enumerate(plan.ops) if op.kind == "assign")

        def answer(op, default):
            status, body = default
            if op is plan.ops[last]:
                return status, {**body, "assigned": False, "reason": "targeting"}
            return default

    elif plant == "no-batch-assignments":
        plan = replace(
            plan, ops=tuple(op for op in plan.ops if op.kind != "assign_batch")
        )
    text = replay(plan, declared=clean.DECLARED, answer=answer)
    replayed = clean.Replayed(
        scenario=scenario, plan=plan, ledger_path=tmp_path / f"{name}.jsonl"
    )
    replayed.records = ledger.read(io.StringIO(text))
    replayed.expected = contract.valid_events(replayed.records)
    replayed.results = honest_results(scenario, replayed.experiment)
    return replayed


@pytest.mark.parametrize("name", sorted(CLEAN_SCENARIOS))
def test_s1_to_s4_pass_the_clean_checks_on_a_correct_server(name, tmp_path):
    replayed = _clean_replayed(name, tmp_path)
    clean.test_g0_every_assignment_answer_is_o_assign(replayed)
    clean.test_g1_counts_and_means_equal_the_ledger(replayed)
    clean.test_g2_statistics_equal_the_reference(replayed)
    clean.test_z_no_answer_is_a_5xx(replayed)


#: Each plant, and the line of ``plan_findings`` it must fail with.
CLEAN_PLANTS = {
    "untagged-event": "events stored with no variant: ledger 1, plan 0 (exact)",
    "unassigned-user": "users assigned: ledger {less}, plan {users} (exact)",
    "missing-exposure": "view events stored by /assign: ledger {fewer}, plan {views}"
    " (exact)",
}


@pytest.mark.parametrize("plant", sorted(CLEAN_PLANTS))
@pytest.mark.parametrize("name", sorted(CLEAN_SCENARIOS))
def test_s1_to_s4_g1_still_checks_the_plan_was_stored_as_sent(name, plant, tmp_path):
    """The clean replay's own G1 test, on S1 to S4: a fault scenario's gates
    drop ``plan_findings``, the clean ones must not."""
    replayed = _clean_replayed(name, tmp_path, plant)
    users = replayed.scenario.users
    views = sum(1 for op in replayed.plan.ops if op.kind == "assign")
    line = CLEAN_PLANTS[plant].format(
        less=users - 1, users=users, fewer=views - 1, views=views
    )
    message = failed(clean.test_g1_counts_and_means_equal_the_ledger, replayed)
    assert message.startswith(f"G1 failed: scenario {name}"), message
    assert f"  {line}" in message, message


@pytest.mark.parametrize("name", sorted(CLEAN_SCENARIOS))
def test_s1_to_s4_g0_counts_the_users_of_the_definition(name, tmp_path):
    """With no /assign/batch sent, the plan assigns only the users it assigns
    one at a time: the clean G0 still asks an answer of every user the
    scenario defines, so it fails (counting the plan's users, it would pass)."""
    replayed = _clean_replayed(name, tmp_path, "no-batch-assignments")
    answers = clean.assignment_answers(
        replayed.records, replayed.scenario.experiment_key
    )
    assert answers >= suite.assigned_by_plan(replayed.plan)
    message = failed(clean.test_g0_every_assignment_answer_is_o_assign, replayed)
    assert (
        f"assignment answers: answered {answers}, users {replayed.scenario.users}"
        " (at least one per user)" in message
    ), message
