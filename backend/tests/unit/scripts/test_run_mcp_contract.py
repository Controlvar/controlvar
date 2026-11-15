"""tests/mcp-contract/run_mcp_contract.py: the MCP server's contract suite (#1607).

The suite runs only in CI, as the second step of the SDK Live Contract job
(``.github/workflows/_platform.yml``), against that job's live API. These pin
what can be pinned without it:

* Q3's unit cross-check: the runner's allow-list is exactly the routes the
  server's tools call (read from ``sdk/mcp/src/tools.ts``); each needs a
  credential in the committed OpenAPI snapshot; every one but the evaluation
  is a GET; and the evaluation is the only one in the flag-change inventory
  (``backend/tests/smoke/test_flag_change_routes_guarded.py``), classed there
  as an API-key evaluation.
* The step: in ``_platform.yml`` after the SDK contract, conditioned on the
  ``sdk-live-contract`` suite (so no fuzz pass runs it), with its own
  ``npm ci`` and no fallback, and Node set up for that suite.
* Every decision the runner makes from what it saw, each shown failing on a
  planted defect. The live half of each row is proven red by a tamper branch
  in CI, not here.

The runner is imported by path. Nothing here starts a process, binds a port or
opens a database.
"""

from __future__ import annotations

import ast
import http.client
import importlib.util
import io
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER = REPO_ROOT / "tests" / "mcp-contract" / "run_mcp_contract.py"
PLATFORM = REPO_ROOT / ".github" / "workflows" / "_platform.yml"
TOOLS_TS = REPO_ROOT / "sdk" / "mcp" / "src" / "tools.ts"
TOOLS_JSON = REPO_ROOT / "sdk" / "mcp" / "tools.json"
HTTP_TS = REPO_ROOT / "sdk" / "mcp" / "src" / "http.ts"
OPENAPI = REPO_ROOT / "docs" / "api" / "openapi-v1.full.json"
INVENTORY_TEST = (
    REPO_ROOT / "backend" / "tests" / "smoke" / "test_flag_change_routes_guarded.py"
)
STEP = "Run the MCP server's contract suite"
CONDITION = "${{ !inputs.skip && inputs.suite == 'sdk-live-contract' }}"
EVALUATE = ("POST", "/api/v1/feature-flags/evaluate/{flag_key}")
EXPERIMENT_ID = "3f2b8c1e-5d4a-4b6f-9e2d-1a7c0b9e8f61"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("run_mcp_contract", RUNNER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_mcp_contract"] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Q3's unit cross-check
# ---------------------------------------------------------------------------
def tool_routes() -> set:
    """(method, template) of every request in the server's tools."""
    text = TOOLS_TS.read_text(encoding="utf-8")
    return set(re.findall(r'method: "(GET|POST)",\s*template: "([^"]+)"', text))


def inventory() -> Tuple[Dict[Tuple[str, str], str], Dict[str, str]]:
    """The flag-change inventory and its class constants, read without
    importing the app."""
    tree = ast.parse(INVENTORY_TEST.read_text(encoding="utf-8"))
    constants: Dict[str, str] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            constants[target.id] = node.value.value
        elif target.id == "INVENTORY" and isinstance(node.value, ast.Dict):
            found = {}
            for key, value in zip(node.value.keys, node.value.values):
                found[tuple(ast.literal_eval(key))] = (
                    constants[value.id]
                    if isinstance(value, ast.Name)
                    else ast.literal_eval(value)
                )
            return found, constants
    raise AssertionError("no INVENTORY in test_flag_change_routes_guarded.py")


def allow_list_problems(
    entries, paths: dict, flag_changes: dict, api_key_class: str
) -> List[str]:
    problems = []
    for method, template in entries:
        operation = (paths.get(template) or {}).get(method.lower())
        if operation is None:
            problems.append(f"{method} {template} is not in the OpenAPI snapshot")
            continue
        if not operation.get("security"):
            problems.append(f"{method} {template} has no security requirement")
        cls = flag_changes.get((method, template))
        if (method, template) == EVALUATE:
            if cls != api_key_class:
                problems.append(
                    f"{method} {template} is classed {cls!r} in the flag-change inventory"
                )
            continue
        if method != "GET":
            problems.append(f"{method} {template} is not a GET")
        if cls is not None:
            problems.append(
                f"{method} {template} is in the flag-change inventory ({cls})"
            )
    return problems


def test_the_allow_list_is_the_routes_the_tools_call(runner):
    routes = tool_routes()
    assert len(routes) == 5, routes  # list_feature_flags and get_feature_flag share one
    assert len(set(runner.ALLOW_LIST)) == len(runner.ALLOW_LIST)
    assert set(runner.ALLOW_LIST) == routes


def test_every_allowed_route_needs_a_credential_and_only_the_evaluation_can_change_a_flag(
    runner,
):
    paths = json.loads(OPENAPI.read_text(encoding="utf-8"))["paths"]
    flag_changes, constants = inventory()
    assert EVALUATE in flag_changes and len(flag_changes) > 20, (
        "the inventory reader is broken"
    )
    assert (
        allow_list_problems(
            runner.ALLOW_LIST, paths, flag_changes, constants["API_KEY"]
        )
        == []
    )

    # Planted: an anonymous route, and a route that changes a flag.
    planted = list(runner.ALLOW_LIST) + [("GET", "/api/v1/mcp/manifest")]
    assert allow_list_problems(planted, paths, flag_changes, constants["API_KEY"]) == [
        "GET /api/v1/mcp/manifest has no security requirement"
    ]
    planted = list(runner.ALLOW_LIST) + [
        ("POST", "/api/v1/feature-flags/{flag_id}/toggle")
    ]
    assert allow_list_problems(planted, paths, flag_changes, constants["API_KEY"]) == [
        "POST /api/v1/feature-flags/{flag_id}/toggle is not a GET",
        "POST /api/v1/feature-flags/{flag_id}/toggle is in the flag-change inventory "
        f"({constants['FLAG_RULE']})",
    ]
    # Planted: the evaluation reclassed in the inventory.
    reclassed = {**flag_changes, EVALUATE: constants["FLAG_RULE"]}
    assert allow_list_problems(
        runner.ALLOW_LIST, paths, reclassed, constants["API_KEY"]
    ) == [
        f"POST {EVALUATE[1]} is classed {constants['FLAG_RULE']!r} in the flag-change inventory"
    ]


def test_the_cases_cover_exactly_the_tools_the_server_lists(runner):
    served = [
        tool["name"]
        for tool in json.loads(TOOLS_JSON.read_text(encoding="utf-8"))["tools"]
    ]
    assert sorted({case.tool for case in runner.build_cases()}) == sorted(served)


# ---------------------------------------------------------------------------
# The step in _platform.yml
# ---------------------------------------------------------------------------
def _key(step: dict) -> str:
    return step.get("name") or step.get("uses") or "?"


def platform_step_problems(steps: List[dict]) -> List[str]:
    names = [_key(step) for step in steps]
    if STEP not in names:
        return [f"_platform.yml has no step {STEP!r}"]
    step = steps[names.index(STEP)]
    problems = []
    if names.index(STEP) < names.index("Run suite"):
        problems.append("the MCP step runs before the SDK contract")
    if step.get("if") != CONDITION:
        problems.append(
            f"the MCP step's condition is {step.get('if')!r}, not {CONDITION!r}"
        )
    run = step.get("run", "")
    if "set -euo pipefail" not in run:
        problems.append("the MCP step does not stop on the first failure")
    if not re.search(r"cd sdk/mcp && npm ci\b", run):
        problems.append("the MCP step does not run its own npm ci in sdk/mcp")
    if "||" in run or "npm install" in run:
        problems.append("the MCP step's install has a fallback")
    if "python tests/mcp-contract/run_mcp_contract.py" not in run:
        problems.append("the MCP step does not run the runner")
    node = steps[names.index("Set up Node.js")]
    if "inputs.suite == 'sdk-live-contract'" not in node.get("if", ""):
        problems.append("Node is not set up for the sdk-live-contract suite")
    return problems


def platform_steps() -> List[dict]:
    return yaml.safe_load(PLATFORM.read_text(encoding="utf-8"))["jobs"]["platform"][
        "steps"
    ]


def test_the_step_runs_only_in_the_sdk_live_contract_suite_with_its_own_install():
    steps = platform_steps()
    assert len(steps) > 20, "found almost no steps: the reader is broken"
    assert platform_step_problems(steps) == []


@pytest.mark.parametrize(
    "plant, expected",
    [
        (
            lambda s: s.update({"if": "${{ !inputs.skip }}"}),
            "the MCP step's condition is",
        ),
        (
            lambda s: s.update(
                {
                    "run": s["run"].replace(
                        "--ignore-scripts", "--ignore-scripts || npm install"
                    )
                }
            ),
            "the MCP step's install has a fallback",
        ),
        (
            lambda s: s.update({"run": s["run"].replace("npm ci", "npm install")}),
            "the MCP step does not run its own npm ci in sdk/mcp",
        ),
        (
            lambda s: s.update({"run": s["run"].replace("set -euo pipefail\n", "")}),
            "the MCP step does not stop on the first failure",
        ),
    ],
)
def test_each_planted_step_defect_is_seen(plant, expected):
    steps = platform_steps()
    step = next(step for step in steps if _key(step) == STEP)
    plant(step)
    found = platform_step_problems(steps)
    assert any(problem.startswith(expected) for problem in found), found


def test_a_step_moved_before_the_sdk_contract_or_without_node_is_seen():
    steps = platform_steps()
    step = steps.pop(next(i for i, s in enumerate(steps) if _key(s) == STEP))
    steps.insert(next(i for i, s in enumerate(steps) if _key(s) == "Run suite"), step)
    assert "the MCP step runs before the SDK contract" in platform_step_problems(steps)
    steps = platform_steps()
    node = next(s for s in steps if _key(s) == "Set up Node.js")
    node["if"] = node["if"].replace("inputs.suite == 'sdk-live-contract' || ", "")
    assert platform_step_problems(steps) == [
        "Node is not set up for the sdk-live-contract suite"
    ]


# ---------------------------------------------------------------------------
# Q3 at the wire, Q6: what the proxies recorded
# ---------------------------------------------------------------------------
def record(
    runner,
    method: str,
    template: str,
    key: str = "configured",
    authorization=False,
    cookie=False,
):
    return runner.Recorded(method, template, template, key, authorization, cookie)


def test_q3_the_routes_called_are_exactly_the_allow_list(runner):
    good = [record(runner, method, template) for method, template in runner.ALLOW_LIST]
    assert runner.wire_problems(good) == []
    planted = good + [record(runner, "POST", "/api/v1/feature-flags/{flag_id}/toggle")]
    assert runner.wire_problems(planted) == [
        "POST /api/v1/feature-flags/{flag_id}/toggle is not in the read-only allow-list"
    ]
    # get_experiment_results calling the other results route instead.
    planted = good[:-1] + [record(runner, "GET", "/api/v1/results/{experiment_id}")]
    assert runner.wire_problems(planted) == [
        "GET /api/v1/results/{experiment_id} is not in the read-only allow-list",
        "GET /api/v1/experiments/{experiment_id}/results was never called",
    ]


def test_q3_concrete_paths_map_to_the_documents_routes(runner):
    routes = runner.RouteMap(json.loads(OPENAPI.read_text(encoding="utf-8")))
    assert len(routes) > 100
    cases = {
        ("GET", "/api/v1/feature-flags/"): "/api/v1/feature-flags/",
        (
            "POST",
            "/api/v1/feature-flags/evaluate/mcp_on_ab12cd34",
        ): "/api/v1/feature-flags/evaluate/{flag_key}",
        (
            "POST",
            "/api/v1/feature-flags/evaluate/A.b_c-1",
        ): "/api/v1/feature-flags/evaluate/{flag_key}",
        ("GET", "/api/v1/experiments/"): "/api/v1/experiments/",
        (
            "GET",
            f"/api/v1/experiments/{EXPERIMENT_ID}",
        ): "/api/v1/experiments/{experiment_id}",
        (
            "GET",
            f"/api/v1/experiments/{EXPERIMENT_ID}/results",
        ): "/api/v1/experiments/{experiment_id}/results",
        (
            "POST",
            f"/api/v1/feature-flags/{EXPERIMENT_ID}/toggle",
        ): "/api/v1/feature-flags/{flag_id}/toggle",
        ("GET", f"/api/v1/results/{EXPERIMENT_ID}"): "/api/v1/results/{experiment_id}",
        # The no-slash twin is not in the document: it stays itself, and so
        # is not in the allow-list.
        ("GET", "/api/v1/feature-flags"): "/api/v1/feature-flags",
    }
    for (method, path), template in cases.items():
        assert routes.template(method, path) == template, (method, path)


def test_q6_exactly_the_configured_key_and_nothing_else(runner):
    good = [record(runner, method, template) for method, template in runner.ALLOW_LIST]
    assert runner.credential_problems(good) == []
    assert runner.credential_problems(
        [record(runner, "GET", "/api/v1/feature-flags/", authorization=True)]
    ) == ["request 1 (GET /api/v1/feature-flags/) carried Authorization"]
    assert runner.credential_problems(
        [record(runner, "GET", "/api/v1/feature-flags/", key="other")]
    ) == [
        "request 1 (GET /api/v1/feature-flags/) carried a credential other than the configured one"
    ]
    assert runner.credential_problems(
        [record(runner, "GET", "/api/v1/feature-flags/", key="none")]
    ) == ["request 1 (GET /api/v1/feature-flags/) carried no X-API-Key"]
    assert runner.credential_problems(
        [record(runner, "GET", "/api/v1/feature-flags/", cookie=True)]
    ) == ["request 1 (GET /api/v1/feature-flags/) carried a cookie"]


def test_q6_the_key_is_compared_whole_and_alone(runner):
    configured = ("k" * 37).encode()
    assert runner.classify_key(["k" * 37], configured) == "configured"
    assert runner.classify_key(["k" * 36 + "j"], configured) == "other"
    assert runner.classify_key(["k" * 36], configured) == "other"
    assert runner.classify_key([], configured) == "none"
    assert runner.classify_key(["k" * 37, "k" * 37], configured) == "other"


# ---------------------------------------------------------------------------
# The proxy's relay, without a socket
# ---------------------------------------------------------------------------
class FakeHandler:
    def __init__(
        self, method: str, path: str, headers: Dict[str, str], body: bytes = b""
    ) -> None:
        self.command = method
        self.path = path
        self.headers = http.client.HTTPMessage()
        for name, value in headers.items():
            self.headers[name] = value
        if body:
            self.headers["Content-Length"] = str(len(body))
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.sent: List[tuple] = []
        self.close_connection = False

    def send_response(self, code, message=None):
        self.sent.append(("status", code, message))

    def send_header(self, name, value):
        self.sent.append((name, value))

    def end_headers(self):
        self.sent.append(("end",))


class FakeResponse:
    status = 200
    reason = "OK"
    body = b'{"items": []}'

    def read(self):
        return self.body

    def getheaders(self):
        return [
            ("content-type", "application/json"),
            ("content-length", "999"),
            ("date", "Tue, 03 Nov 2026 06:00:00 GMT"),
            ("server", "uvicorn"),
            ("x-request-id", "abc"),
        ]


class FakeConnection:
    made: List["FakeConnection"] = []
    fail = False

    def __init__(self, host, port, timeout=None):
        self.address = (host, port)
        FakeConnection.made.append(self)

    def request(self, method, target, body=None, headers=None):
        if FakeConnection.fail:
            raise ConnectionRefusedError("refused")
        self.sent = (method, target, body, dict(headers or {}))

    def getresponse(self):
        return FakeResponse()

    def close(self):
        pass


@pytest.fixture
def proxy(runner, monkeypatch):
    FakeConnection.made, FakeConnection.fail = [], False
    monkeypatch.setattr(runner.http.client, "HTTPConnection", FakeConnection)
    routes = runner.RouteMap(json.loads(OPENAPI.read_text(encoding="utf-8")))
    return runner.RecordingProxy("http://127.0.0.1:8000", "k" * 37, routes)


def test_the_relay_records_forwards_and_answers(proxy):
    body = b'{"user_id": "u"}'
    handler = FakeHandler(
        "POST",
        "/api/v1/feature-flags/evaluate/mcp_on_ab12cd34?x=1",
        {
            "Host": "127.0.0.1:55555",
            "X-API-Key": "k" * 37,
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
        },
        body,
    )
    proxy._relay(handler)
    (recorded,) = proxy.records
    assert (recorded.method, recorded.template, recorded.key) == (
        "POST",
        EVALUATE[1],
        "configured",
    )
    assert (recorded.authorization, recorded.cookie) == (False, False)
    (connection,) = FakeConnection.made
    assert connection.address == ("127.0.0.1", 8000)
    method, target, sent_body, headers = connection.sent
    assert (method, target, sent_body) == (
        "POST",
        "/api/v1/feature-flags/evaluate/mcp_on_ab12cd34?x=1",
        body,
    )
    assert {name.lower() for name in headers} == {"x-api-key", "content-type"}
    assert handler.sent[0] == ("status", 200, "OK")
    assert handler.sent[-1] == ("end",)
    sent_headers = {name.lower(): value for name, value in handler.sent[1:-1]}
    assert sent_headers == {
        "content-type": "application/json",
        "x-request-id": "abc",
        "content-length": str(len(FakeResponse.body)),
        "connection": "close",
    }
    assert handler.wfile.getvalue() == FakeResponse.body
    assert handler.close_connection is True


def test_the_relay_records_a_bearer_and_another_key(proxy):
    proxy._relay(
        FakeHandler(
            "GET",
            "/api/v1/feature-flags/",
            {"X-API-Key": "k" * 36 + "j", "Authorization": "Bearer x"},
        )
    )
    proxy._relay(FakeHandler("GET", "/api/v1/feature-flags", {"Cookie": "a=b"}))
    first, second = proxy.records
    assert (first.key, first.authorization) == ("other", True)
    assert (second.key, second.cookie, second.template) == (
        "none",
        True,
        "/api/v1/feature-flags",
    )


def test_an_unreachable_api_answers_502(proxy):
    FakeConnection.fail = True
    handler = FakeHandler("GET", "/api/v1/feature-flags/", {"X-API-Key": "k" * 37})
    proxy._relay(handler)
    assert handler.sent[0] == ("status", 502, "Bad Gateway")
    assert len(proxy.records) == 1


# ---------------------------------------------------------------------------
# Q4: the database, scoped to the run
# ---------------------------------------------------------------------------
ACTIVE, DRAFT = (
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
)
ALLOWED = {ACTIVE: frozenset(["frequentist"]), DRAFT: frozenset()}
NAMES = {ACTIVE: "experiment (mcp_experiment_t)", DRAFT: "draft (mcp_draft_t)"}


def state(audit=0, evaluations=0, api_keys=40, users=20, snapshots=None) -> dict:
    return {
        "audit": audit,
        "evaluations": evaluations,
        "api_keys": api_keys,
        "users": users,
        "snapshots": dict(snapshots or {}),
    }


def test_q4_a_clean_run_has_no_problem(runner):
    before = state(
        snapshots={(ACTIVE, "frequentist", "2026-11-03T00:00:00+00:00"): "t1"}
    )
    after = state(
        evaluations=20,
        snapshots={
            (ACTIVE, "frequentist", "2026-11-03T00:00:00+00:00"): "t2"
        },  # a results read refreshed it
    )
    assert runner.database_problems(before, after, 20, ALLOWED, NAMES) == []


@pytest.mark.parametrize(
    "after, made, expected",
    [
        (
            state(audit=1, evaluations=20),
            20,
            "audit_logs +1 for an MCP-run actor during the run (expected 0)",
        ),
        (
            state(evaluations=40),
            20,
            "raw_metrics: 40 FLAG_EVALUATION rows carry this run's user ids; the MCP run made 20 evaluations",
        ),
        (
            state(evaluations=20, api_keys=41),
            20,
            "api_keys +1 rows during the MCP run (expected 0)",
        ),
        (
            state(evaluations=20, users=19),
            20,
            "users -1 rows during the MCP run (expected 0)",
        ),
        (
            state(
                evaluations=20,
                snapshots={(ACTIVE, "cuped", "2026-11-03T00:00:00+00:00"): "t"},
            ),
            20,
            "analysis_snapshots: a cuped row for the experiment (mcp_experiment_t) was written during the "
            "MCP run (expected: frequentist)",
        ),
        (
            state(
                evaluations=20,
                snapshots={(DRAFT, "frequentist", "2026-11-03T00:00:00+00:00"): "t"},
            ),
            20,
            "analysis_snapshots: a frequentist row for the draft (mcp_draft_t) was written during the "
            "MCP run (expected: none)",
        ),
    ],
)
def test_q4_each_planted_write_is_seen(runner, after, made, expected):
    assert runner.database_problems(state(), after, made, ALLOWED, NAMES) == [expected]


def test_q4_a_row_untouched_or_removed(runner):
    row = {(DRAFT, "bandit", "2026-11-02T10:00:00+00:00"): "t"}
    assert runner.snapshot_problems(row, dict(row), ALLOWED, NAMES) == []
    assert runner.snapshot_problems(row, {}, ALLOWED, NAMES) == [
        "analysis_snapshots: a bandit row for the draft (mcp_draft_t) was deleted during the MCP run"
    ]


# ---------------------------------------------------------------------------
# Q10, Q12 and the comparisons
# ---------------------------------------------------------------------------
def test_q10_a_key_piece_is_found_and_reported_by_place_only(runner):
    secret = "abcdefghijklmnopqrstuvwxyz0123456789A"
    outputs = {
        "stdout": ['{"jsonrpc":"2.0"}'],
        "stderr": ["fine", f"oops {secret[11:20]} here"],
        "tool result": [],
    }
    where = runner.secret_location(secret, outputs)
    assert where == "stderr line 2"
    assert not any(piece in where for piece in runner.pieces(secret))
    outputs["stderr"][1] = f"oops {secret[11:19]} here"  # eight characters: not a piece
    assert runner.secret_location(secret, outputs) is None
    assert runner.redact(f"x{secret}y", [secret]).count("[key]") >= 1
    assert not any(
        piece in runner.redact(f"x{secret}y", [secret])
        for piece in runner.pieces(secret)
    )


def test_q12_a_banner_on_stdout_is_seen(runner):
    assert runner.jsonrpc_problems("s", ['{"jsonrpc":"2.0","id":1,"result":{}}']) == []
    assert runner.jsonrpc_problems(
        "s", ["Controlvar MCP server ready", '{"jsonrpc":"2.0"}']
    ) == ["s: stdout line 1 is not JSON-RPC: 'Controlvar MCP server ready'"]
    assert runner.jsonrpc_problems("s", ['{"jsonrpc":"1.0"}']) == [
        's: stdout line 1 is not JSON-RPC: \'{"jsonrpc":"1.0"}\''
    ]


def test_numbers_compare_by_value_and_everything_else_exactly(runner):
    assert runner.first_difference({"a": [0, 1.5]}, {"a": [0.0, 1.5]}) is None
    assert runner.first_difference(True, 1) == "(top)"
    assert runner.first_difference({"a": {"b": 1}}, {"a": {}}) == ".a.b"
    assert runner.first_difference({"m": [1]}, {"m": [1, 2]}) == ".m.length"
    assert runner.first_difference({"s": "x"}, {"s": "y"}) == ".s"


def test_q7_a_role_that_sees_less_is_seen(runner):
    labels = {0: "list_feature_flags (search past the decoys)"}
    same = {"ADMIN": {"items": [1, 2]}, "VIEWER": {"items": [1, 2]}}
    lists = {"ADMIN": ["a"], "VIEWER": ["a"]}
    assert runner.role_differences(labels, {0: same}, lists) == []
    narrowed = {"ADMIN": {"items": [1, 2]}, "VIEWER": {"items": [1]}}
    assert runner.role_differences(labels, {0: narrowed}, lists) == [
        "list_feature_flags (search past the decoys) as VIEWER differs from ADMIN at .items.length"
    ]
    assert runner.role_differences(
        labels, {0: same}, {"ADMIN": ["a"], "VIEWER": ["a", "b"]}
    ) == ["tools/list as VIEWER differs from ADMIN"]


def test_q2_a_tool_without_a_case_is_seen(runner):
    six = [
        tool["name"]
        for tool in json.loads(TOOLS_JSON.read_text(encoding="utf-8"))["tools"]
    ]
    cases = [case.tool for case in runner.build_cases()]
    assert runner.coverage_problems(six, cases, set()) == []
    assert runner.coverage_problems(six + ["toggle_feature_flag"], cases, set()) == [
        "tool toggle_feature_flag is served but has no contract case"
    ]
    assert runner.coverage_problems(six, cases, {"get_experiment"}) == [
        "tool get_experiment failed a contract case (see the rows below)"
    ]
    assert runner.coverage_problems(six[:-1], cases, set()) == [
        f"tool {six[-1]} has a contract case but is not served"
    ]


# ---------------------------------------------------------------------------
# The cases' checks (Q8, Q14-Q17)
# ---------------------------------------------------------------------------
def ctx(runner):
    run = runner.Run(tag="ab12cd34")
    run.experiment_ids = {"experiment": EXPERIMENT_ID, "draft": DRAFT}
    run.experiment_keys = {
        "experiment": "mcp_experiment_ab12cd34",
        "draft": "mcp_draft_ab12cd34",
    }
    return runner.Context(
        run=run,
        role="VIEWER",
        key="k" * 37,
        origin="http://127.0.0.1:1",
        api_url="http://127.0.0.1:2",
    )


def ok(value) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(is_error=False, text=json.dumps(value))


def test_q14_an_answer_without_its_total_is_seen(runner, monkeypatch):
    monkeypatch.setattr(runner, "flag_count", lambda: 113)
    item = dict.fromkeys(runner.FLAG_LIST_FIELDS, "x")
    page = {"total": 113, "skip": 0, "limit": 100, "items": [item] * 100}
    assert runner._check_everything(ctx(runner), ok(page)) == []
    no_total = {key: value for key, value in page.items() if key != "total"}
    assert runner._check_everything(ctx(runner), ok(no_total)) == [
        "list_flags reported 100 flags; the database holds 113 and the answer does not say it is partial "
        "(total None)"
    ]
    monkeypatch.setattr(runner, "flag_count", lambda: 100)
    assert runner._check_everything(ctx(runner), ok(page)) == [
        "the database holds 100 flags; this check needs more than 100"
    ]


def test_q15_a_decoy_answered_is_seen(runner):
    context = ctx(runner)
    assert runner._check_target(context, ok({"key": "mcp_flag_ab12cd34"})) == []
    assert runner._check_target(context, ok({"key": "mcp_flag_ab12cd34_v2"})) == [
        "get_flag('mcp_flag_ab12cd34') answered 'mcp_flag_ab12cd34_v2'"
    ]


def test_q16_context_not_forwarded_is_seen(runner):
    check = runner._evaluation("us", True, "targeting_rule")
    good = {
        "key": "mcp_us_ab12cd34",
        "enabled": True,
        "value": True,
        "value_type": "boolean",
        "reason": "targeting_rule",
    }
    assert check(ctx(runner), ok(good)) == []
    dropped = dict(good, enabled=False, value=False, reason="rollout")
    assert check(ctx(runner), ok(dropped)) == [
        "evaluate 'mcp_us_ab12cd34': expected enabled True, got False",
        "evaluate 'mcp_us_ab12cd34': expected reason targeting_rule, got 'rollout'",
    ]
    assert check(ctx(runner), ok(dict(good, config=None))) == [
        "evaluate 'mcp_us_ab12cd34' answered fields ['config'] beyond "
        "['enabled', 'key', 'reason', 'value', 'value_type']"
    ]


def test_q17_a_dropped_metric_is_seen(runner, monkeypatch):
    served = {
        "experiment_id": EXPERIMENT_ID,
        "metrics": [{"name": "Purchase", "lift": 0.0}, {"name": "Revenue"}],
    }
    monkeypatch.setattr(runner, "direct", lambda api_url, path, key: (200, served))
    as_js_writes_it = {
        "experiment_id": EXPERIMENT_ID,
        "metrics": [{"name": "Purchase", "lift": 0}, {"name": "Revenue"}],
    }
    assert runner._check_results(ctx(runner), ok(as_js_writes_it)) == []
    dropped = dict(served, metrics=served["metrics"][:1])
    assert runner._check_results(ctx(runner), ok(dropped)) == [
        "get_experiment_results differs from the API's answer at .metrics.length"
    ]


def test_q8_and_the_exact_texts(runner):
    from types import SimpleNamespace

    version = re.search(
        r'MIN_PLATFORM_VERSION = "([^"]+)"', HTTP_TS.read_text(encoding="utf-8")
    ).group(1)
    assert runner.min_platform_version() == version
    text = runner.e12("http://127.0.0.1:5", version)
    assert text.startswith(
        "The Controlvar API at http://127.0.0.1:5 did not accept the key in CONTROLVAR_API_KEY (401: Invalid API Key)."
    )
    assert f"older than Controlvar {version}" in text
    check = runner.exact_error("Feature flag with key 'x' not found")
    assert (
        check(
            ctx(runner),
            SimpleNamespace(is_error=True, text="Feature flag with key 'x' not found"),
        )
        == []
    )
    assert check(
        ctx(runner), SimpleNamespace(is_error=False, text='{"items": []}')
    ) == ['answered isError false: {"items": []}']


# ---------------------------------------------------------------------------
# It never skips, and Q9 comes first
# ---------------------------------------------------------------------------
def test_no_node_is_a_failure_not_a_skip(runner, monkeypatch, capsys):
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    assert runner.main(["--api-url", "http://127.0.0.1:9"]) == 2
    out = capsys.readouterr().out
    assert "FAIL setup" in out and "never skips" in out


def test_no_built_server_is_a_failure(runner, monkeypatch):
    monkeypatch.setattr(
        runner, "BIN", REPO_ROOT / "sdk" / "mcp" / "no-such-dir" / "index.js"
    )
    node, why = runner.node_problem()
    if runner.shutil.which("node") is None:
        assert node is None and "never skips" in why
    else:
        assert node is None and "npm run build" in why


def test_q9_a_dev_bypass_stops_the_suite_before_any_case(runner, monkeypatch, capsys):
    monkeypatch.setattr(runner, "node_problem", lambda: ("node", None))
    monkeypatch.setattr(runner, "get_status", lambda url, headers=None: 200)

    def no_seed(tag):
        raise AssertionError("seeded after Q9 failed")

    monkeypatch.setattr(runner, "seed", no_seed)
    assert runner.main(["--api-url", "http://127.0.0.1:9"]) == 1
    out = capsys.readouterr().out
    assert "FAIL Q9" in out
    assert (
        "the API under test admits a request with no credential: the dev bypass is on"
        in out
    )
    # N5: rows that never ran are not reported as passes.
    lines = out.splitlines()
    assert not any(line.lstrip().startswith("PASS") for line in lines)
    assert (
        "  NOT RUN Q2    every served tool has a passing contract case (Q9 failed)"
        in lines
    )
    assert "  NOT RUN setup the suite could run (Q9 failed)" in lines
    assert "0 passed, 1 failed, 16 not run" in out


def test_a_failed_setup_does_not_report_the_later_rows_as_passes(
    runner, monkeypatch, capsys
):
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    assert runner.main(["--api-url", "http://127.0.0.1:9"]) == 2
    out = capsys.readouterr().out
    assert "  NOT RUN Q9" in out and "0 passed, 1 failed, 16 not run" in out
    assert "PASS" not in out


# ---------------------------------------------------------------------------
# The output redaction, and Q7 naming a path
# ---------------------------------------------------------------------------
def test_the_output_redaction_hides_every_registered_piece(runner, monkeypatch):
    key = "eptk_" + "abcdefghijklmnopqrstuvwxyz0123456789"
    monkeypatch.setattr(runner, "_SECRETS", [key])
    for text in (
        f"failed with {key}",
        f"a piece {key[7:16]} alone",
        f"the tail {key[-9:]}",
    ):
        out = runner.shown(text)
        assert "[key]" in out
        assert not any(piece in out for piece in runner.pieces(key)), out
    assert runner.shown("nothing secret here") == "nothing secret here"


def test_q7_a_results_answer_that_differs_names_its_path(runner):
    from types import SimpleNamespace

    def answer(metrics):
        return SimpleNamespace(
            is_error=False, text=json.dumps({"experiment_id": "e", "metrics": metrics})
        )

    case = next(
        c
        for c in runner.build_cases()
        if c.tool == "get_experiment_results" and c.row == "Q17"
    )
    labels = {0: "get_experiment_results (the active experiment, against the API)"}
    shapes = {
        0: {
            "ADMIN": case.shape(answer([1, 2])),
            "VIEWER": case.shape(answer([1])),
        }
    }
    assert runner.role_differences(labels, shapes, {}) == [
        "get_experiment_results (the active experiment, against the API) as VIEWER "
        "differs from ADMIN at .answer.metrics.length"
    ]
