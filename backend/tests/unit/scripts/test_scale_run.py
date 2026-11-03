# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""The scale measurement: scripts/scale_run.py and tests/scale/k6 (#1608).

Nothing here runs Docker, k6 or a stack. The k6 script's logic lives in
``tests/scale/k6/lib.js``, which imports nothing, and runs here under node
(there is no skip without node: a check that cannot run is not a pass). The
script itself is the glue around it, read as text. scale_run.py's parsers and
rules are driven with fixtures shaped like what the runner probe recorded
(EMS-4), and its ``run`` is driven end to end by :class:`FakeWorld`, which
answers every docker and git call it makes and plays k6 by writing the CSV k6
would write; the results file that comes out must pass scale_evidence.py's
closed schema and recomputation.

The first live run of all of it is the pull request's own smoke, on a
GitHub-hosted runner.
"""

from __future__ import annotations

import ast
import csv
import gzip
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.tests.unit.scripts import scale_evidence_fixtures as fx

se = fx.module()
REPO_ROOT = fx.REPO_ROOT
SCRIPT = REPO_ROOT / "scripts" / "scale_run.py"
K6_DIR = REPO_ROOT / "tests" / "scale" / "k6"
LIB = K6_DIR / "lib.js"
SCENARIOS_JS = K6_DIR / "scenarios.js"


def _load():
    name = "scale_run_under_test"
    if name not in sys.modules:
        import importlib.util

        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


sr = _load()
ss = sr.ss

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# lib.js under node
# ---------------------------------------------------------------------------
#: Imports lib.js as an ES module (a data: URL is always one, whatever the
#: nearest package.json says) and calls each case's export with its arguments.
NODE_HARNESS = r"""
import { readFileSync } from 'node:fs';
const input = JSON.parse(process.argv.at(-1));
const source = readFileSync(input.lib, 'utf8');
const lib = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const out = [];
for (const c of input.cases) {
  try {
    const value = c.fn === null ? lib[c.name] : lib[c.fn](...c.args);
    out.push({ ok: true, value: value === undefined ? null : value });
  } catch (err) {
    out.push({ ok: false, error: String((err && err.message) || err) });
  }
}
process.stdout.write(JSON.stringify(out));
"""


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.fail(
            "node is not on PATH, so tests/scale/k6/lib.js cannot be checked; these "
            "tests need node 18 or later and do not skip"
        )
    return node


def lib(*cases) -> List[Any]:
    """Each case is (export name, *args) or (constant name,): the values."""
    payload = [
        {"fn": None, "name": c[0], "args": []}
        if len(c) == 1
        else {"fn": c[0], "args": list(c[1:])}
        for c in cases
    ]
    result = subprocess.run(
        [
            _node(),
            "--input-type=module",
            "-e",
            NODE_HARNESS,
            json.dumps({"lib": str(LIB), "cases": payload}),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    answers = json.loads(result.stdout)
    return [a["value"] if a["ok"] else RuntimeError(a["error"]) for a in answers]


def const(name: str) -> Any:
    return lib((name,))[0]


def one(name: str, *args) -> Any:
    value = lib((name, *args))[0]
    if isinstance(value, RuntimeError):
        raise value
    return value


FIXTURES = {
    "project": "controlvar-scale",
    "base_url": "http://localhost:18080",
    "api_keys": {
        "scale-sdk": {"id": "k1", "key": "cvk_sdk_key_for_tests"},
        "scale-server": {"id": "k2", "key": "cvk_server_key_for_tests"},
    },
    "flags": {
        "rule": [f"scale_flag_{i:03d}" for i in range(50)],
        "plain": [f"scale_flag_{i:03d}" for i in range(50, 100)],
    },
    "rule_context": dict(ss.RULE_CONTEXT),
    "experiment": {
        "key": ss.EXPERIMENT_KEY,
        "id": "e1",
        "variants": [
            {"name": "control", "id": "v1"},
            {"name": "treatment", "id": "v2"},
        ],
    },
    "returning_users": {
        "prefix": ss.RETURNING_USER_PREFIX,
        "width": ss.RETURNING_USER_WIDTH,
        "count": 100000,
    },
}
CONTEXT = {
    "base": "http://web:8080",
    "host": "localhost:18080",
    "step": 4,
    "fixtures": FIXTURES,
    "eventName": ss.EVENT_NAME,
    "etag": '"abc123"',
}


def test_the_record_s_fields_are_the_summariser_s_rows_and_the_adapter_s():
    """One order in three places: lib.js writes it, scale_run.py reads it, and
    it is scale_evidence.py's ROW_FIELDS without the endpoint (which comes from
    the fixed list) plus the scenario's start time."""
    fields = const("RECORD_FIELDS")
    assert tuple(fields) == sr.RECORD_FIELDS
    expected = [f for f in se.ROW_FIELDS if f != "endpoint"] + ["scenario_start_ms"]
    assert fields == expected
    assert const("RECORD_KEY") == sr.RECORD_KEY


def test_every_request_path_is_one_of_the_summariser_s_endpoints():
    """V42: the scenarios build only the fixed paths, and each method and path
    is the endpoint scale_evidence.py names the request by."""
    paths = const("PATHS")
    for scenario, endpoint in se.ENDPOINTS.items():
        request = one("buildRequest", scenario, 7, CONTEXT)
        assert endpoint == f"{request['method']} {paths[scenario]}", scenario
    assert set(const("SCENARIOS")) == set(se.SCENARIOS)


@pytest.mark.parametrize("iteration", [0, 1, 49, 50, 99, 100, 12345])
def test_evaluations_take_flag_keys_from_the_fixtures_and_user_ids_in_the_query(
    iteration,
):
    """V42: the flag key is one of the fixtures', every flag in turn; the user
    id is only in the query; a rule flag carries the context, a plain one none."""
    request = one("buildRequest", "evaluate_get", iteration, CONTEXT)
    path, _, query = request["url"].removeprefix("http://web:8080").partition("?")
    key = path.removeprefix("/api/v1/feature-flags/evaluate/")
    keys = FIXTURES["flags"]["rule"] + FIXTURES["flags"]["plain"]
    assert key == keys[iteration % 100]
    rule = key in FIXTURES["flags"]["rule"]
    assert request["flagClass"] == ("rule" if rule else "plain")
    assert ("context=" in query) == rule
    assert f"scale-eval-{iteration}" not in path
    assert request["headers"] == {
        "Host": "localhost:18080",
        "X-API-Key": "cvk_sdk_key_for_tests",
    }
    if rule:
        context = re.search(r"context=([^&]+)", query).group(1)
        from urllib.parse import unquote

        assert json.loads(unquote(context)) == ss.RULE_CONTEXT


def test_assignments_send_unique_new_users_or_the_preload_s_returning_ones():
    first = one("buildRequest", "assign", 3, CONTEXT)
    again = one("buildRequest", "assign", 3, {**CONTEXT, "step": 5})
    body = json.loads(first["body"])
    assert body == {"experiment_key": "scale_ab", "user_id": "scale-new-4-3"}
    assert json.loads(again["body"])["user_id"] == "scale-new-5-3"
    assert first["url"] == "http://web:8080/api/v1/tracking/assign"
    returning = one("buildRequest", "assign_returning", 3, CONTEXT)
    index = int(returning["userIndex"])
    assert 0 <= index < 100000
    user = json.loads(returning["body"])["user_id"]
    # The preload's own id format: the prefix, then the index padded to width.
    assert user == f"{ss.RETURNING_USER_PREFIX}{index:0{ss.RETURNING_USER_WIDTH}d}"


def test_returning_users_cover_the_pool_without_repeating():
    """A step of up to 100,000 requests never asks for one user twice."""
    pool = FIXTURES["returning_users"]
    seeds = range(0, 3000, 7)
    seen = {user["index"] for user in lib(*[("returningUser", i, pool) for i in seeds])}
    assert len(seen) == len(seeds)


def test_track_batch_and_ruleset_requests():
    track = json.loads(one("buildRequest", "track", 9, CONTEXT)["body"])
    assert track["event_type"] == ss.EVENT_NAME
    assert track["experiment_key"] == "scale_ab"
    assert track["user_id"].startswith(ss.RETURNING_USER_PREFIX)
    batch = json.loads(one("buildRequest", "batch", 2, CONTEXT)["body"])["events"]
    assert len(batch) == se.BATCH_EVENTS == const("BATCH_EVENTS")
    assert len({event["user_id"] for event in batch}) == se.BATCH_EVENTS
    ruleset = one("buildRequest", "ruleset", 2, CONTEXT)
    assert ruleset["method"] == "GET" and ruleset["body"] is None
    assert ruleset["headers"] == {
        "Host": "localhost:18080",
        "X-API-Key": "cvk_server_key_for_tests",
        "If-None-Match": '"abc123"',
    }


@pytest.mark.parametrize(
    "headers, value",
    [
        ({"X-Ratelimit-Limit": "10000000"}, "10000000"),  # k6: Go's canonical key
        ({"x-ratelimit-limit": "10000000"}, "10000000"),  # nginx, on the wire
        ({"X-RateLimit-Limit": "10000000"}, "10000000"),  # the API's spelling
        ({"X-Ratelimit-Remaining": "9999999"}, ""),  # absent
        ({}, ""),
    ],
    ids=["go-canonical", "lower-case", "api-spelling", "absent", "no-headers"],
)
def test_the_limiter_header_is_found_whatever_its_case(headers, value):
    """PE SMT ruling 4: k6 hands headers on as X-Ratelimit-Limit; a
    case-sensitive lookup would miss it on every answer."""
    assert one("headerValue", headers, const("LIMIT_HEADER")) == value


def test_an_absent_or_odd_limit_header_is_a_mismatch_the_summariser_counts():
    meth = fx.method()
    expect = se.expectations(meth, fx.VARIANTS)
    for headers in ({}, {"X-Ratelimit-Limit": "abc|x"}):
        fields = one(
            "rowFor",
            "track",
            11,
            {"flagClass": "", "userIndex": ""},
            {
                "status": 200,
                "error_code": 0,
                "headers": headers,
                "body": json.dumps({"id": "e1", "variant_id": "v1"}),
                "timings": {"blocked": 0.2, "duration": 3.1},
            },
            1000,
            1793604814000,
        )
        assert fields["rate_limit"] in ("", "x")
        row = {name: fields.get(name, "") for name in se.ROW_FIELDS}
        assert se.judge("track", row, expect) == ""
        assert row["rate_limit"] != str(meth["sdk_rate_limit_per_minute"])


@pytest.mark.parametrize(
    "now, start, iteration, rate, unit, lag",
    [
        (1_000_000, 1_000_000, 0, 1, 200, 0),
        (1_000_203, 1_000_000, 1, 1, 200, 3000),
        (1_000_199, 1_000_000, 1, 1, 200, 0),  # 1 ms early (clock resolution)
        # 63 per 10000 ms: iteration 5 is due 793.65... ms in.
        (1_000_800, 1_000_000, 5, 63, 10000, 6349),
    ],
)
def test_start_lag_is_measured_from_each_request_s_due_time(
    now, start, iteration, rate, unit, lag
):
    """ADDENDUM A item 1: due = iteration x time_unit_ms / rate ms after the
    scenario's start, from the step's exact rate pair."""
    assert one("startLagUs", now, start, iteration, rate, unit) == lag


def test_latency_is_blocked_plus_duration_and_never_adds_connecting():
    """PE-S1: http_req_blocked already holds the connect interval."""
    timings = {"blocked": 12.5, "connecting": 12.0, "duration": 30.25}
    assert one("latencyUs", timings) == 42750
    for path in (LIB, SCENARIOS_JS):
        code = re.sub(r"//[^\n]*", "", path.read_text("utf-8"))
        assert "connecting" not in code, path.name


@pytest.mark.parametrize(
    "status, code, kind",
    [(200, 0, ""), (0, 1050, "timeout"), (0, 1212, "connection"), (503, 1503, "")],
)
def test_errors_are_a_timeout_or_a_connection_never_a_status(status, code, kind):
    assert one("errorKind", status, code) == kind
    assert sr.se.ROW_ERRORS == ("timeout", "connection")


def test_the_mix_follows_its_weights_in_every_run_of_100():
    mix = fx.method()["smoke"]["mix"]
    names = lib(*[("mixScenario", i, mix) for i in range(200)])
    for start in (0, 100):
        window = names[start : start + 100]
        assert {name: window.count(name) for name in mix} == mix


@pytest.mark.parametrize(
    "scenario, status, body, facts",
    [
        (
            "evaluate_get",
            200,
            {"reason": "targeting_rule"},
            {"reason": "targeting_rule"},
        ),
        ("evaluate_get", 200, {"reason": "Not A Code!"}, {"reason": "unexpected"}),
        (
            "assign",
            200,
            {"assigned": True, "reason": "assigned", "variant_name": "control"},
            {"assigned": "1", "reason": "assigned", "variant": "control"},
        ),
        (
            "assign",
            200,
            {"assigned": False, "reason": "paused", "variant_name": "control"},
            {"assigned": "0", "reason": "paused", "variant": "control"},
        ),
        (
            "track",
            200,
            {"id": "e1", "variant_id": None},
            {"id_present": "1", "variant_id_present": "0"},
        ),
        (
            "batch",
            200,
            {"success_count": 100, "failure_count": 0},
            {"success_count": "100", "failure_count": "0"},
        ),
        ("ruleset", 200, {"flags": [{}] * 100}, {"flag_count": "100"}),
        ("ruleset", 304, None, {}),
        ("track", 200, "not json", {}),
    ],
)
def test_the_body_facts_are_codes_and_counts(scenario, status, body, facts):
    text = body if isinstance(body, str) else (json.dumps(body) if body else "")
    found = one("bodyFacts", scenario, status, text)
    assert {k: v for k, v in found.items() if v} == facts


def _record_line(fields: Dict[str, str]) -> str:
    return one("record", fields)


def test_a_record_with_a_character_outside_the_codes_is_refused():
    good = dict.fromkeys(sr.RECORD_FIELDS, "")
    good.update(
        scenario="track", iteration="1", status="200", scenario_start_ms="1793604814000"
    )
    assert _record_line(good).split("|")[0] == "track"
    for bad in ("a|b", "a&b", "a=b", "a,b", "a b", "a\nb"):
        value = lib(("record", {**good, "reason": bad}))[0]
        assert isinstance(value, RuntimeError), bad


def test_the_environment_k6_is_given_is_checked():
    env = {
        "SCALE_SCENARIO": "track",
        "SCALE_RATE": "63",
        "SCALE_TIME_UNIT_MS": "10000",
        "SCALE_DURATION_S": "70",
        "SCALE_SETTLE_MS": "10000",
        "SCALE_VUS": "20",
        "SCALE_TIMEOUT_MS": "5000",
        "SCALE_STEP": "2",
        "SCALE_BASE_URL": "http://web:8080",
        "SCALE_HOST": "localhost:18080",
        "SCALE_ETAG": '"abc"',
        "SCALE_EVENT": ss.EVENT_NAME,
        "SCALE_MIX": "{}",
        "SCALE_FIXTURES": "/fixtures.json",
        "SCALE_SUMMARY": "/out/summary.json",
    }
    parsed = one("readEnv", env)
    assert (parsed["rate"], parsed["timeUnitMs"], parsed["vus"]) == (63, 10000, 20)
    for change in (
        {"SCALE_SCENARIO": "evaluate_post"},
        {"SCALE_BASE_URL": "https://example.com"},
        {"SCALE_RATE": "1.5"},
        {"SCALE_ETAG": '"a b"'},
        {"SCALE_SCENARIO": "mix", "SCALE_MIX": '{"track": 99}'},
    ):
        assert isinstance(lib(("readEnv", {**env, **change}))[0], RuntimeError), change


# ---------------------------------------------------------------------------
# scenarios.js, read as text
# ---------------------------------------------------------------------------
def test_the_scenario_script_keeps_the_url_out_of_k6_s_output():
    """V45 (PE-S2), on the real script."""
    assert se.system_tags_problems(SCENARIOS_JS.read_text("utf-8")) == []


def test_the_scenario_script_is_an_open_model_with_every_vu_allocated():
    text = SCENARIOS_JS.read_text("utf-8")
    assert "executor: 'constant-arrival-rate'" in text
    assert "preAllocatedVUs: env.vus," in text and "maxVUs: env.vus," in text
    assert "dropped_iterations: ['count<1']" in text
    assert f"userAgent: '{sr.USER_AGENT}'" in text
    assert "exec.vu.metrics.metadata[RECORD_KEY] = record(fields)" in text
    assert "delete exec.vu.metrics.metadata[RECORD_KEY]" in text
    assert f"new Trend('{sr.RECORD_METRIC}', true)" in text
    # The clock is read, and the start lag and send offset taken from it,
    # before the request is sent.
    assert text.index("const now = Date.now();") < text.index("http.request(")
    assert text.index("startLagUs(now,") < text.index("http.request(")
    assert text.index("sentOffsetUs(now,") < text.index("http.request(")
    # The measure window is the send time, never the iteration number (#1667).
    assert "sentOffset >= env.settleMs * 1000" in text
    assert "firstMeasured" not in text


#: Stand-ins for k6's modules, enough to load scenarios.js under node and run
#: its default function: http records each request and answers one fixed
#: response; Trend records each sample with the VU's metadata at that moment.
K6_STUBS = {
    "http.js": """
export const calls = [];
export const response = { value: null };
export default {
  request(method, url, body, params) {
    calls.push({ method, url, body, params });
    return response.value;
  },
};
""",
    "execution.js": """
export const state = { iteration: 0 };
export default {
  scenario: {
    get iterationInTest() { return state.iteration; },
    startTime: 1793604814000,
  },
  vu: { metrics: { metadata: {} } },
};
""",
    "metrics.js": """
import exec from './execution.js';
export const samples = [];
export class Trend {
  constructor(name, isTime) { this.name = name; this.isTime = isTime; }
  add(value) { samples.push({ name: this.name, value, metadata: { ...exec.vu.metrics.metadata } }); }
}
""",
}
GLUE_DRIVER = r"""
import http, { calls, response } from 'k6/http';
import exec, { state } from 'k6/execution';
import { samples } from 'k6/metrics';
const input = JSON.parse(process.argv.at(-1));
globalThis.__ENV = input.env;
globalThis.open = () => JSON.stringify(input.fixtures);
const script = await import('./scenarios.js');
response.value = input.response;
const realNow = Date.now;
for (const [i, atMs] of input.iterations) {
  state.iteration = i;
  Date.now = () => exec.scenario.startTime + atMs;
  script.default();
}
Date.now = realNow;
const summary = script.handleSummary({ metrics: {
  scale_request: { values: { count: 2 } },
  scale_measured: { values: { count: 1, 'p(99)': 2.5 } },
} });
process.stdout.write(JSON.stringify({
  options: script.options, calls, samples,
  metadata_after: exec.vu.metrics.metadata, summary,
}));
"""


def test_the_scenario_script_runs_its_steps_as_lib_js_says(tmp_path):
    """scenarios.js itself, under node with stand-ins for k6's modules: its
    options, one request per iteration, one scale_request sample per request
    carrying the record (and nothing after it), and the summary file."""
    (tmp_path / "package.json").write_text('{"type": "module"}', "utf-8")
    for name in ("scenarios.js", "lib.js"):
        shutil.copy(K6_DIR / name, tmp_path / name)
    k6 = tmp_path / "node_modules" / "k6"
    k6.mkdir(parents=True)
    exports = {f"./{n[:-3]}": f"./{n}" for n in K6_STUBS}
    (k6 / "package.json").write_text(
        json.dumps({"name": "k6", "type": "module", "exports": exports}), "utf-8"
    )
    for name, text in K6_STUBS.items():
        (k6 / name).write_text(text, "utf-8")
    (tmp_path / "driver.js").write_text(GLUE_DRIVER, "utf-8")
    env = {
        "SCALE_SCENARIO": "track",
        "SCALE_RATE": "1",
        "SCALE_TIME_UNIT_MS": "200",
        "SCALE_DURATION_S": "70",
        "SCALE_SETTLE_MS": "10000",
        "SCALE_VUS": "20",
        "SCALE_TIMEOUT_MS": "5000",
        "SCALE_STEP": "1",
        "SCALE_BASE_URL": "http://web:8080",
        "SCALE_HOST": "localhost:18080",
        "SCALE_ETAG": "",
        "SCALE_EVENT": ss.EVENT_NAME,
        "SCALE_MIX": "{}",
        "SCALE_FIXTURES": "/fixtures.json",
        "SCALE_SUMMARY": "/out/summary.json",
    }
    response = {
        "status": 200,
        "error_code": 0,
        "headers": {"X-Ratelimit-Limit": "10000000"},
        "body": json.dumps({"id": "e1", "variant_id": "v1"}),
        "timings": {"blocked": 0.25, "connecting": 0.2, "duration": 3.5},
    }
    result = subprocess.run(
        [
            _node(),
            str(tmp_path / "driver.js"),
            json.dumps(
                {
                    "env": env,
                    "fixtures": FIXTURES,
                    "response": response,
                    # [iteration, ms after the scenario's start it was sent]: a
                    # drop before the second, so its number (60) is not its
                    # slot; what counts is that it went out after the settle.
                    "iterations": [[3, 600], [4, 10_000], [60, 12_000]],
                }
            ),
        ],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    scenario = out["options"]["scenarios"]["track"]
    assert scenario == {
        "executor": "constant-arrival-rate",
        "rate": 1,
        "timeUnit": "200ms",
        "duration": "70s",
        "preAllocatedVUs": 20,
        "maxVUs": 20,
        "gracefulStop": "7000ms",
    }
    assert out["options"]["systemTags"] == [
        "status",
        "method",
        "scenario",
        "expected_response",
    ]
    assert out["options"]["thresholds"] == {"dropped_iterations": ["count<1"]}
    assert [c["params"]["timeout"] for c in out["calls"]] == ["5000ms"] * 3
    assert all(c["params"]["redirects"] == 0 for c in out["calls"])
    records = [s for s in out["samples"] if s["name"] == "scale_request"]
    assert [s["value"] for s in records] == [3.75] * 3
    lines = [s["metadata"]["scale_row"] for s in records]
    assert [line.split("|")[1] for line in lines] == ["3", "4", "60"]
    (read,) = sr.read_records(
        ",".join(K6_HEADER)
        + "\n"
        + f"scale_request,1,3.75,,,s,,,scale_row={lines[2]}\n"
    )
    assert read["sent_offset_us"] == "12000000"
    assert read["scenario"] == "track" and read["latency_us"] == "3750"
    assert (
        read["rate_limit"] == "10000000"
        and read["scenario_start_ms"] == "1793604814000"
    )
    # Only the requests sent at or after the settle (10 s) are in
    # scale_measured, whatever their iteration numbers, and those samples
    # carry no record.
    measured = [s for s in out["samples"] if s["name"] == "scale_measured"]
    assert len(measured) == 2 and all(s["metadata"] == {} for s in measured)
    assert out["metadata_after"] == {}
    summary = json.loads(out["summary"]["/out/summary.json"])
    assert summary == {
        "requests": 2,
        "measured": 1,
        "dropped": 0,
        "measured_p99_us": 2500,
    }


# ---------------------------------------------------------------------------
# The generator's records, read back from k6's CSV
# ---------------------------------------------------------------------------
#: k6's CSV header with the script's system tags (sorted, as k6 sorts them).
K6_HEADER = [
    "metric_name",
    "timestamp",
    "metric_value",
    "expected_response",
    "method",
    "scenario",
    "status",
    "extra_tags",
    "metadata",
]


def k6_csv(records: List[Dict[str, str]], noise: bool = True) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(K6_HEADER)
    for record in records:
        if noise:
            writer.writerow(
                [
                    "http_reqs",
                    "1793604814",
                    "1.000000",
                    "true",
                    "GET",
                    "s",
                    "200",
                    "",
                    "",
                ]
            )
        value = "|".join(record[name] for name in sr.RECORD_FIELDS)
        writer.writerow(
            [
                "scale_request",
                "1793604814",
                "4.000000",
                "",
                "",
                "s",
                "",
                "",
                f"scale_row={value}",
            ]
        )
    return out.getvalue()


def records_for(
    meth, scenario: str, number: int, start_ms: int = 1793604814000, **change
):
    rows = fx.step_rows(meth, scenario, number)
    out = []
    for row in rows:
        record = {name: row.get(name, "") for name in sr.RECORD_FIELDS}
        record["scenario_start_ms"] = str(start_ms)
        record.update(change)
        out.append(record)
    return out


def test_the_adapter_reads_back_exactly_the_rows_the_summariser_judges():
    """The CSV k6 writes -> records -> rows: a healthy step is VALID."""
    meth = fx.method()
    records = records_for(meth, "track", 1)
    read = sr.read_records(k6_csv(records))
    assert read == records
    rows = sr.to_rows(read)
    assert sr.scenario_start(read) == 1793604814000
    step, _ = se.summarise_step(
        meth,
        "track",
        se.read_rows(sr.rows_text(rows)),
        fx.observation(meth, "track", 1),
        fx.metrics(),
        fx.metrics(),
    )
    assert step["valid"] and step["keeps_up"]
    assert {row["endpoint"] for row in rows} == {se.ENDPOINTS["track"]}


def test_a_csv_with_a_url_column_or_odd_metadata_is_refused():
    meth = fx.method()
    records = records_for(meth, "track", 1)[:3]
    text = k6_csv(records)
    with pytest.raises(se.Refused, match="url column"):
        sr.read_records(text.replace("status,extra_tags", "status,url,extra_tags", 1))
    with pytest.raises(se.Refused, match="metadata"):
        sr.read_records(text.replace("scale_row=", "iter=3&scale_row=", 1))
    with pytest.raises(se.Refused, match="fields"):
        sr.read_records(text.replace("scale_row=track|", "scale_row=track|extra|", 1))
    with pytest.raises(se.Refused, match="scenario start"):
        sr.scenario_start(
            records + records_for(meth, "track", 1, start_ms=1793604815000)[:1]
        )


def test_a_calibration_record_is_never_a_measured_row():
    meth = fx.method()
    record = records_for(meth, "track", 1)[0]
    with pytest.raises(se.Refused, match="not measured"):
        sr.to_rows([{**record, "scenario": "calibration"}])


def test_records_written_by_lib_js_are_read_by_the_adapter():
    """End to end across the languages: lib.js's rowFor and record, a CSV line
    as k6 writes metadata, scale_run.py's reader, scale_evidence.py's judge."""
    meth = fx.method()
    expect = se.expectations(meth, fx.VARIANTS)
    response = {
        "status": 200,
        "error_code": 0,
        "headers": {"X-Ratelimit-Limit": "10000000"},
        "body": json.dumps(
            {"assigned": True, "reason": "assigned", "variant_name": "treatment"}
        ),
        "timings": {"blocked": 0.5, "duration": 6.0},
    }
    request = one("buildRequest", "assign_returning", 1, CONTEXT)
    fields = one(
        "rowFor",
        "assign_returning",
        1,
        request,
        response,
        2000,
        1793604814000,
        11_000_000,
    )
    line = one("record", fields)
    text = "\n".join(
        [
            ",".join(K6_HEADER),
            f"scale_request,1793604814,6.5,,,s,,,scale_row={line}",
            "",
        ]
    )
    (record,) = sr.read_records(text)
    (row,) = sr.to_rows([record])
    assert row["latency_us"] == "6500" and row["start_lag_us"] == "2000"
    assert row["sent_offset_us"] == "11000000"
    assert row["rate_limit"] == "10000000"
    variant = fx.VARIANTS[int(row["user_index"]) % 2]
    assert se.judge("assign_returning", row, expect) == (
        "" if variant == "treatment" else "body"
    )


def test_the_measured_count_is_by_send_time_not_iteration_number():
    rows = [{"sent_offset_us": str(s * 1_000_000)} for s in (0, 4, 5, 6, 24)]
    assert sr.measured_count(rows, 5) == 3
    assert sr.measured_count(rows, 0) == 5


@pytest.mark.regression
def test_a_step_that_measured_nothing_says_so_and_prints_no_percentile():
    """#1667: the line over an empty set read 'p99 under 1 ms, 0 of 0 failed'."""
    meth = se.load_config(mode="smoke")
    # Four requests, all sent in the 5 s settle; 121 of the 125 dropped.
    rows = [fx.row("track", i, sent_offset_us=i * 1_000_000) for i in range(4)]
    observation = fx.observation(meth, "mix", 1, rows=4, samples=20)
    observation.update(scenario="mix", dropped=121)
    record, _ = se.summarise_step(
        meth,
        "mix",
        se.read_rows(fx.csv_text(rows)),
        observation,
        fx.metrics("a"),
        fx.metrics("a"),
    )
    assert record["measured"] == 0
    line = sr.step_line(record, 0)
    assert line == (
        f"step 1: {se.rps(record['milli_rps'])}: INVALID (nothing-measured), no "
        "request was sent in the measure window, so no latency or failure count "
        "was measured"
    )
    assert "p99" not in line and "failed" not in line.replace("failure count", "")
    # A step that did measure still prints its numbers.
    ok = {**record, "measured": 16, "valid": True, "invalid_reason": "none"}
    assert "p99" in sr.step_line(ok, 0) and "of 16 failed" in sr.step_line(ok, 0)


def test_k6_s_own_counts_check_the_reading():
    summary = {"requests": 350, "measured": 300, "dropped": 0, "measured_p99_us": 9000}
    assert sr.k6_counts_problems(summary, 350, 300, 0) == []
    assert "its CSV holds 340" in sr.k6_counts_problems(summary, 340, 300, 0)[0]
    assert "exits 99 exactly when" in sr.k6_counts_problems(summary, 350, 300, 99)[0]
    dropped = {**summary, "dropped": 37, "requests": 313}
    assert sr.k6_counts_problems(dropped, 313, 300, 99) == []
    assert sr.k6_counts_problems(dropped, 313, 300, 0)


def test_k6_s_version_and_summary_are_read_strictly():
    assert (
        sr.k6_version("k6 v2.3.0 (commit/e088784614, go1.27.1, linux/amd64)\n")
        == "2.3.0"
    )
    with pytest.raises(se.Refused):
        sr.k6_version("")
    with pytest.raises(se.Refused):
        sr.k6_summary(
            json.dumps(
                {"requests": 1.5, "measured": 1, "dropped": 0, "measured_p99_us": 1}
            )
        )


def test_the_generator_image_is_pinned_by_digest():
    assert re.fullmatch(r"grafana/k6:2\.3\.0@sha256:[0-9a-f]{64}", sr.K6_IMAGE)


# ---------------------------------------------------------------------------
# The CPU samplers
# ---------------------------------------------------------------------------
#: The api process's /proc/<pid>/cgroup on the runner probed (EMS-4 item 2).
PROC_CGROUP = "0::/system.slice/docker-" + "a" * 64 + ".scope\n"


def test_the_cgroup_directory_is_read_from_proc_never_assumed():
    assert sr.cgroup_dir(PROC_CGROUP) == (
        "/sys/fs/cgroup/system.slice/docker-" + "a" * 64 + ".scope"
    )
    hybrid = "12:cpu,cpuacct:/docker/abc\n1:name=systemd:/docker/abc\n"
    with pytest.raises(se.Refused, match="cgroup v2"):
        sr.cgroup_dir(hybrid)
    with pytest.raises(se.Refused, match="plain path"):
        sr.cgroup_dir("0::/system.slice/../../etc\n")


def test_cpu_stat_usage_is_read_and_a_missing_one_refused():
    stat = "usage_usec 123456\nuser_usec 100000\nsystem_usec 23456\nnr_periods 0\n"
    assert sr.cpu_usage_usec(stat) == 123456
    with pytest.raises(se.Refused):
        sr.cpu_usage_usec("user_usec 1\n")


PROC_STAT_A = (
    "cpu  100 0 100 1000 0 0 0 0 0 0\n"
    "cpu0 100 0 50 800 10 5 15 20 7 0\n"
    "cpu1 0 0 0 1000 0 0 0 0 0 0\n"
    "cpu2 0 0 0 1000 0 0 0 0 0 0\n"
    "cpu3 0 0 0 1000 0 0 0 0 0 0\n"
    "intr 1 2 3\n"
)
PROC_STAT_B = (
    "cpu  200 0 200 2000 0 0 0 0 0 0\n"
    "cpu0 400 0 150 1200 20 15 35 40 99 0\n"
    "cpu1 50 0 50 1900 0 0 0 0 0 0\n"
    "cpu2 0 0 0 2000 0 0 0 0 0 0\n"
    "cpu3 10 0 0 1990 0 0 0 0 0 0\n"
)


def test_a_thread_s_busy_steal_irq_and_softirq_come_from_proc_stat():
    """Busy is user, nice, system, irq and softirq; steal and idle and iowait
    are not busy; irq and softirq are also recorded apart (PE condition 8);
    guest time is already in user and not counted twice."""
    a, b = sr.proc_stat(PROC_STAT_A), sr.proc_stat(PROC_STAT_B)
    assert set(a) == set(se.THREADS)
    cpu0 = sr.thread_permille(a["cpu0"], b["cpu0"])
    # deltas: user 300, system 100, idle 400, iowait 10, irq 10, softirq 20, steal 20
    assert cpu0 == {
        "busy_permille": 1000 * 430 // 860,
        "steal_permille": (2000 * 20 + 860) // 1720,
        "irq_permille": (2000 * 10 + 860) // 1720,
        "softirq_permille": (2000 * 20 + 860) // 1720,
    }
    assert sr.thread_permille(a["cpu1"], b["cpu1"])["busy_permille"] == 100


def _readings(start: int, seconds: int, rates: Dict[str, int], thread_ticks=None):
    readings = []
    for i in range(seconds + 1):
        usage = {name: i * rate for name, rate in rates.items()}
        ticks = thread_ticks or dict.fromkeys(se.THREADS, (40, 0, 0, 60, 0, 0, 0, 0))
        threads = {t: tuple(v * i for v in vals) for t, vals in ticks.items()}
        readings.append(sr.Reading(start + 1000 * i, threads, usage))
    return readings


def test_a_window_s_cpu_is_from_its_first_and_last_readings():
    rates = {name: 400_000 if name == "api" else 10_000 for name in se.CONTAINERS}
    readings = _readings(1_000_000, 70, rates)
    window = sr.cpu_window(readings, 1_010_000, 1_070_000, se.CONTAINERS)
    assert window.sampled and window.samples == 61
    assert window.cpu["api"] == 400 and window.cpu["generator"] == 10
    assert window.threads["cpu3"]["busy_permille"] == 400


def test_an_unread_container_or_too_few_readings_is_not_sampled_never_zero():
    rates = dict.fromkeys(se.CONTAINERS, 10_000)
    readings = _readings(1_000_000, 70, rates)
    last = readings[60]
    readings[60] = sr.Reading(
        last.at_ms, last.threads, {**last.usage, "generator": None}
    )
    window = sr.cpu_window(readings, 1_010_000, 1_060_000, se.CONTAINERS)
    assert not window.sampled
    assert not sr.cpu_window(readings[:1], 1_000_000, 1_060_000, se.CONTAINERS).sampled


def test_a_container_unread_in_the_first_reading_is_not_sampled_never_zero():
    """R4: with the first reading unreadable, `or 0` would make the window the
    container's whole lifetime of CPU (a generator at 20,012 permille)."""
    rates = dict.fromkeys(se.CONTAINERS, 10_000)
    readings = _readings(1_000_000, 70, rates)
    first = readings[10]
    readings[10] = sr.Reading(
        first.at_ms, first.threads, {**first.usage, "generator": None}
    )
    window = sr.cpu_window(readings, 1_010_000, 1_060_000, se.CONTAINERS)
    assert not window.sampled
    assert window.cpu["generator"] == 0


def test_the_sampler_reads_each_registered_container_and_marks_an_unreadable_one():
    files = {
        "/proc/stat": PROC_STAT_A,
        "/sys/fs/cgroup/x/cpu.stat": "usage_usec 5\n",
    }

    def read(path):
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    sampler = sr.CpuSampler(sr.Io(read=read, now_ms=lambda: 42))
    sampler.register("api", "/sys/fs/cgroup/x")
    sampler.register("generator", "/sys/fs/cgroup/gone")
    sampler.tick()
    (reading,) = sampler.readings
    assert reading.at_ms == 42 and reading.usage == {"api": 5, "generator": None}
    assert reading.threads["cpu0"][0] == 100
    sampler.unregister("generator")
    sampler.tick()
    assert sampler.readings[-1].usage == {"api": 5}


# ---------------------------------------------------------------------------
# The database sampler (PE-S22), the access lines and the API's log
# ---------------------------------------------------------------------------
def _select_list(sql: str) -> str:
    return sql.split("FROM pg_stat_activity", 1)[0]


def test_the_database_sampler_s_sql_is_exactly_this_text():
    """PE-S22, pinned whole: a row value (`(SELECT json_agg(s) FROM
    pg_stat_activity s)`) selects every column without the word `query`, so
    the words below cannot be the pin. A change to the SQL is a change here."""
    assert sr.PG_SAMPLER_SQL == (
        "SELECT 'scale-sample',\n"
        "       floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint,\n"
        "       count(*) FILTER (WHERE state = 'active'),\n"
        "       count(*) FILTER (WHERE state = 'active' AND (wait_event_type = 'IO'\n"
        "                        OR (wait_event_type = 'LWLock' AND wait_event LIKE 'WAL%')))\n"
        "FROM pg_stat_activity\n"
        "WHERE backend_type = 'client backend'\n"
        "  AND pid <> pg_backend_pid()\n"
        f"  AND coalesce(application_name, '') <> '{sr.SAMPLER_APP}'\n"
        f"\\watch {sr.PG_SAMPLE_S}\n"
    )


def test_the_database_sampler_never_selects_query():
    """PE-S22: the sampler reads only the columns it needs, never ``query``."""
    sql = sr.PG_SAMPLER_SQL
    assert not re.search(r"\bquery\b", sql, re.I)
    # No SELECT * and no row value: every selected column is named below.
    assert not re.search(r"(SELECT|,)\s*\*|\.\*|\bpg_stat_activity\s*\)", sql, re.I)
    columns = set(re.findall(r"\b([a-z_]+)\s*(?:=|<>|LIKE)", sql))
    assert columns <= {
        "state",
        "wait_event_type",
        "wait_event",
        "backend_type",
        "pid",
        "application_name",
    }, columns
    assert "pid <> pg_backend_pid()" in sql
    assert f"coalesce(application_name, '') <> '{sr.SAMPLER_APP}'" in sql
    assert sql.rstrip().endswith(f"\\watch {sr.PG_SAMPLE_S}")


#: psql -A -t \watch output: samples, blank lines, and anything else psql says.
PG_OUTPUT = (
    "scale-sample|1793604800000|1|0\n\n"
    "scale-sample|1793604802000|0|0\n"
    "scale-sample|1793604804000|3|2\n\n"
    "psql: a notice\n"
    "scale-sample|1793604806000|1|1\n"
    "scale-sample|1793604890000|9|9\n"
)


def test_the_database_samples_in_a_window_become_the_summariser_s_histogram():
    samples = sr.pg_samples(PG_OUTPUT)
    assert len(samples) == 5
    window = sr.pg_window(samples, 1793604800000, 1793604806000)
    assert window.samples == 4
    assert window.by_count == [1, 2, 0, 1]
    assert window.io_wait_observations == 3


def test_a_window_needs_the_method_s_share_of_database_samples():
    assert sr.pg_needed(fx.method()) == 27
    assert sr.pg_needed(fx.method(mode="smoke")) == 9


#: Access lines in the web image's format (EMS-4 item 6).
ACCESS = "\n".join(
    [
        '172.18.0.5 - - [02/Nov/2026:07:34:57 +0000] "GET /api/v1/sdk/ruleset HTTP/1.1" 304 0 "-" "controlvar-scale" "-"',
        '172.18.0.5 - - [02/Nov/2026:07:34:57 +0000] "POST /api/v1/tracking/track HTTP/1.1" 200 412 "-" "controlvar-scale" "-"',
        '172.18.0.5 - - [02/Nov/2026:07:34:58 +0000] "POST /api/v1/tracking/track HTTP/1.1" 499 0 "-" "controlvar-scale" "-"',
        '172.18.0.1 - - [02/Nov/2026:07:34:57 +0000] "GET /api/v1/feature-flags/evaluate/scale_flag_001 HTTP/1.1" 200 73 "-" "curl/8.5.0" "-"',
        '172.18.0.5 - - [02/Nov/2026:07:34:57 +0000] "GET /x HTTP/1.1" 200 73 "-" "Grafana k6/2.3.0" "-"',
        "2026/11/02 07:34:59 [error] 30#30: *1 upstream timed out",
    ]
)


def test_the_server_count_is_the_generator_s_access_lines_and_its_499s():
    assert sr.access_counts(ACCESS) == (3, 1)


def test_the_fallback_line_is_the_one_the_api_logs():
    from backend.app.middleware.rate_limiter import FALLBACK_MESSAGE

    assert FALLBACK_MESSAGE.startswith(sr.FALLBACK_LOG)
    log = json.dumps({"event": FALLBACK_MESSAGE, "rate_limiter": "per_process"})
    assert sr.fallback_lines(f'{log}\n{{"event": "request"}}\n') == 1


PS_ONE = (
    "UID PID PPID C STIME TTY TIME CMD\n"
    "app 4242 4200 3 07:30 ? 00:00:09 /opt/venv/bin/python /opt/venv/bin/uvicorn "
    "backend.app.main:app --host 0.0.0.0 --port 8000 --workers 1\n"
)
PS_TWO = (
    "UID PID PPID C STIME TTY TIME CMD\n"
    "app 4242 4200 0 07:30 ? 00:00:01 /opt/venv/bin/python /opt/venv/bin/uvicorn "
    "backend.app.main:app --host 0.0.0.0 --port 8000 --workers 2\n"
    "app 4250 4242 0 07:30 ? 00:00:00 /opt/venv/bin/python -c from multiprocessing."
    "resource_tracker import main;main(5)\n"
    "app 4251 4242 2 07:30 ? 00:00:05 /opt/venv/bin/python -c from multiprocessing."
    "spawn import spawn_main; spawn_main(tracker_fd=6, pipe_handle=8) --multiprocessing-fork\n"
    "app 4252 4242 2 07:30 ? 00:00:05 /opt/venv/bin/python -c from multiprocessing."
    "spawn import spawn_main; spawn_main(tracker_fd=6, pipe_handle=10) --multiprocessing-fork\n"
)


def test_the_api_s_workers_are_counted_from_its_processes():
    """V23: WEB_CONCURRENCY=2 shows as 2, against the method's 1."""
    assert sr.web_workers(PS_ONE) == 1
    assert sr.web_workers(PS_TWO) == 2
    with pytest.raises(se.Refused):
        sr.web_workers("PID COMMAND\n1 sh\n")


# ---------------------------------------------------------------------------
# The release measured (EMS-7 (i) and (ii), V46)
# ---------------------------------------------------------------------------
COMMIT = "c" * 40


def test_a_compose_yml_that_is_the_tag_s_and_images_from_its_commit_pass():
    blob = (REPO_ROOT / "deploy" / "compose" / "compose.yml").read_bytes()
    assert (
        sr.release_problems(
            "0.40.0", blob, blob, COMMIT, {"api": COMMIT, "web": COMMIT}
        )
        == []
    )


def test_refusal_i_one_byte_of_compose_yml_differs_from_the_tag_s():
    blob = (REPO_ROOT / "deploy" / "compose" / "compose.yml").read_bytes()
    tag_blob = blob.replace(b"restart: unless-stopped", b"restart: unless-stoppeD", 1)
    assert sr.release_problems(
        "0.40.0", blob, tag_blob, COMMIT, {"api": COMMIT, "web": COMMIT}
    ) == ["compose.yml differs from the one v0.40.0 shipped: refusing to publish"]


def test_refusal_ii_an_image_revision_label_is_not_the_tag_s_commit():
    blob = b"x"
    problems = sr.release_problems(
        "0.40.0", blob, blob, COMMIT, {"api": COMMIT, "web": "d" * 40}
    )
    assert problems == [
        f"the web image's revision {'d' * 12} is not v0.40.0's commit {'c' * 12}: "
        "refusing to publish"
    ]
    assert sr.release_problems("0.40.0", blob, blob, COMMIT, {"api": "", "web": COMMIT})


def test_the_release_is_the_literal_compose_yml_names_for_both_images():
    text = (REPO_ROOT / "deploy" / "compose" / "compose.yml").read_text("utf-8")
    version = sr.compose_version(text)
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
    with pytest.raises(se.Refused):
        sr.compose_version(text.replace(f"-{version} #", "-9.9.9 #", 1))
    assert sr.image_release(f"ghcr.io/controlvar/controlvar:core-{version}", "api") == (
        "core",
        version,
    )
    with pytest.raises(se.Refused):
        sr.image_release("ghcr.io/someone/else:core-1.0.0", "api")


# ---------------------------------------------------------------------------
# The plan, the run record, the layout
# ---------------------------------------------------------------------------
DEFAULT_SCENARIOS = "evaluate_get assign assign_returning track batch ruleset"


def test_the_default_dispatch_is_six_request_types_times_three_runs():
    matrix = sr.plan_matrix(DEFAULT_SCENARIOS, "3")
    assert len(matrix) == 18
    assert [m["scenario"] for m in matrix[::3]] == list(se.SCENARIOS)
    assert sr.plan_matrix("ruleset track", "1") == [
        {"scenario": "track", "rep": 1},
        {"scenario": "ruleset", "rep": 1},
    ]


@pytest.mark.parametrize(
    "scenarios, repetitions",
    [
        ("mix", "3"),
        ("evaluate_post", "3"),
        ("", "3"),
        ("track track", "1"),
        ("track", "0"),
        ("track", "10"),
        ("track", "3; echo"),
    ],
)
def test_a_plan_outside_the_method_is_refused(scenarios, repetitions):
    with pytest.raises(se.Refused):
        sr.plan_matrix(scenarios, repetitions)


GITHUB = {
    "GITHUB_RUN_ID": "40999123456",
    "GITHUB_REPOSITORY": "Controlvar/controlvar",
    "GITHUB_SHA": "b" * 40,
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_RUN_ATTEMPT": "1",
    "ImageOS": "ubuntu24",
    "ImageVersion": "20261026.354.1",
}


def test_the_run_record_is_the_workflow_run_s():
    record = sr.run_record(GITHUB, "2026-11-20T14:03:11Z")
    problems: List[str] = []
    dict(se.RESULTS_SPEC.fields)["run"].check(record, "run", problems)
    assert problems == []
    with pytest.raises(se.Refused, match="GITHUB_SHA"):
        sr.run_record({**GITHUB, "GITHUB_SHA": ""}, "2026-11-20T14:03:11Z")


PE_SPLIT = {
    "api": "0",
    "postgres": "1",
    "web": "1",
    "redis": "1",
    "pg_sampler": "2",
    "generator": "3",
}


def test_the_pe_layout_on_the_probed_topology_is_accepted():
    assert sr.isolation_problems(PE_SPLIT, fx.SMT) == []
    assert sr.isolation_problems(PE_SPLIT, fx.NO_SMT) == []


@pytest.mark.parametrize(
    "split, siblings, words",
    [
        (fx.OLD_SPLIT, fx.SMT, "nginx is on cpu2"),
        (PE_SPLIT, fx.INTERLEAVED, "on cpu1, a hardware thread of the load generator"),
        ({**PE_SPLIT, "pg_sampler": "3"}, fx.SMT, "sampler is on the load generator"),
        ({**PE_SPLIT, "api": "0-1"}, fx.SMT, "one hardware thread"),
    ],
    ids=["old-split", "interleaved-siblings", "sampler-on-k6", "unpinned-api"],
)
def test_a_layout_sharing_the_generator_s_core_is_refused(split, siblings, words):
    problems = sr.isolation_problems(split, siblings)
    assert any(words in problem for problem in problems), problems


def test_an_observation_is_what_the_summariser_reads():
    meth = fx.method()
    cpu = sr.cpu_window(
        _readings(1_000_000, 70, dict.fromkeys(se.CONTAINERS, 10_000)),
        1_010_000,
        1_070_000,
        se.CONTAINERS,
    )
    pg = sr.PgWindow(30, [10, 20], 3)
    seen = sr.observation(
        scenario="track",
        step=1,
        rate=1,
        time_unit_ms=200,
        dropped=0,
        variants=fx.VARIANTS,
        split=PE_SPLIT,
        cpu=cpu,
        pg=pg,
        pg_needed=sr.pg_needed(meth),
        fallback=0,
        server=(350, 0),
        assignments=(100000, 100000),
    )
    problems: List[str] = []
    se.OBSERVATION_SPEC.check(seen, "observation", problems)
    assert problems == [] and seen["sampled"]
    thin = sr.observation(
        scenario="track",
        step=1,
        rate=1,
        time_unit_ms=200,
        dropped=0,
        variants=fx.VARIANTS,
        split=PE_SPLIT,
        cpu=cpu,
        pg=sr.PgWindow(26, [6, 20], 0),
        pg_needed=sr.pg_needed(meth),
        fallback=0,
        server=(350, 0),
        assignments=(100000, 100000),
    )
    assert not thin["sampled"] and thin["pg"]["active_backend_samples_by_count"] == []


def test_the_calibration_shows_a_stall_or_says_the_generator_hides_waiting():
    """V18: 20% of requests delayed by up to 2 s give a p99 near 2 s; a
    generator that does not count waiting reports the server's few ms."""
    stalled = [
        {
            "status": "200",
            "error": "",
            "latency_us": str(5000 + (i % 200) * 10000 if i % 5 == 0 else 5000),
        }
        for i in range(6000)
    ]
    p99, answered, other = sr.calibration_p99(stalled)
    assert answered == 6000 and other == 0 and p99 >= sr.CALIBRATION_MIN_P99_US
    assert sr.calibration_problems(p99, answered) == []
    flat = [{"status": "200", "error": "", "latency_us": "12000"}] * 6000
    p99, answered, _ = sr.calibration_p99(flat)
    assert sr.calibration_problems(p99, answered) == [
        "calibration: planted 2000 ms stall over 20% of the window, measured p99 12 ms: "
        "this generator does not count waiting requests"
    ]


def test_a_failed_calibration_fails_a_dispatch_and_never_a_pull_request():
    """PLAN 3.9: V18 refuses to publish; it never fails a pull request."""
    problem = ["calibration: planted 2000 ms stall ..."]
    assert sr.calibration_exit(problem, report_only=False) == 1
    assert sr.calibration_exit(problem, report_only=True) == 0
    assert sr.calibration_exit([], report_only=False) == 0
    assert sr.calibration_exit([], report_only=True) == 0


@pytest.mark.parametrize(
    "argv, report_only",
    [
        (["calibrate", "--work", "w", "--out", "o"], False),
        (["calibrate", "--report-only", "--work", "w", "--out", "o"], True),
    ],
)
def test_calibrate_passes_report_only_from_its_flag(
    monkeypatch, tmp_path, argv, report_only
):
    seen = []
    monkeypatch.setattr(
        sr,
        "cmd_calibrate",
        lambda work, out, io, only=False: seen.append(only) or 0,
    )
    monkeypatch.setattr(sr.ss, "work_dir", lambda path: path)
    assert sr.main(argv, sr.Io(out=io.StringIO())) == 0
    assert seen == [report_only]


def test_the_stub_stalls_two_seconds_in_every_ten():
    tree = ast.parse(sr.STUB_CODE)
    assert tree  # the stub is Python the api image's interpreter can run
    assert f"% {sr.CALIBRATION_PERIOD_S}" in sr.STUB_CODE
    assert f"if phase < {sr.CALIBRATION_STALL_S}:" in sr.STUB_CODE


# ---------------------------------------------------------------------------
# The command line and the module
# ---------------------------------------------------------------------------
def test_there_is_no_way_to_aim_it_at_another_target():
    """PE-S19: no --target-url, no --host-header; the target is the project's web."""
    options = {
        option
        for action in sr.parser()._subparsers._group_actions
        for command in action.choices.values()
        for option in command._option_string_actions
    }
    assert options == {
        "-h",
        "--help",
        "--work",
        "--out",
        "--scenario",
        "--rep",
        "--mode",
        "--no-resource-sampling",
        "--report-only",
        "--scenarios",
        "--repetitions",
        "--base",
        "--head",
    }


def _imports(path: Path) -> set:
    found = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_the_measurement_is_standard_library_only():
    assert _imports(SCRIPT) - set(sys.stdlib_module_names) == set()


def test_the_harness_loads_the_summariser_once():
    """scale_run.py uses the summariser and the stack module scale_evidence.py
    loaded: the same objects, not copies."""
    assert sr.se is sys.modules["scale_evidence"]
    assert sr.ss is se.stack()


def test_plan_prints_the_matrix_for_the_workflow():
    out = io.StringIO()
    status = sr.main(
        ["plan", "--scenarios", DEFAULT_SCENARIOS, "--repetitions", "3"],
        sr.Io(out=out, now_ms=lambda: 1793604814000),
    )
    assert status == 0
    lines = dict(line.split("=", 1) for line in out.getvalue().splitlines())
    assert len(json.loads(lines["matrix"])["include"]) == 18 == int(lines["jobs"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", lines["dispatched_at"])


def test_tag_prints_the_measured_release_s_tag():
    out = io.StringIO()
    assert sr.main(["tag"], sr.Io(out=out)) == 0
    version = sr.compose_version(sr.COMPOSE_FILE.read_text("utf-8"))
    assert out.getvalue().strip() == f"v{version}"


# ---------------------------------------------------------------------------
# run, end to end, in a fake world
# ---------------------------------------------------------------------------
ID = {"api": "a" * 64, "postgres": "b" * 64, "web": "c" * 64, "redis": "d" * 64}
SAMPLER_ID = "5" * 64
GENERATOR_ID = "6" * 64
COMPOSE_BYTES = sr.COMPOSE_FILE.read_bytes()
VERSION = sr.compose_version(COMPOSE_BYTES.decode())
TAG_COMMIT = fx.sha("tag")
PIDS = {
    ID["api"]: 101,
    ID["postgres"]: 102,
    ID["web"]: 103,
    ID["redis"]: 104,
    SAMPLER_ID: 105,
    GENERATOR_ID: 106,
}
#: Each container's CPU in microseconds a second, and each thread's /proc/stat
#: ticks a second (100 a second), busy just above what is pinned there.
USAGE = {
    "api": 400_000,
    "postgres": 200_000,
    "web": 80_000,
    "redis": 70_000,
    "pg_sampler": 20_000,
    "generator": 120_000,
}
TICKS = {
    "cpu0": (41, 0, 0, 58, 0, 0, 1, 0),
    "cpu1": (35, 0, 1, 63, 0, 0, 1, 0),
    "cpu2": (3, 0, 0, 96, 0, 0, 1, 0),
    "cpu3": (12, 0, 1, 86, 0, 0, 1, 0),
}


class FakeSampler(sr.CpuSampler):
    """Readings every second, for whatever window is asked."""

    def start(self):
        self.started = True

    def between(self, start_ms, end_ms):
        return [
            sr.Reading(
                at,
                {t: tuple(v * at // 1000 for v in ticks) for t, ticks in TICKS.items()},
                {name: rate * at // 1000 for name, rate in USAGE.items()},
            )
            for at in range(start_ms, end_ms + 1, 1000)
        ]


class FakeWorld:
    """Every docker and git call run makes, answered; k6 played by writing the
    CSV and the summary k6 would write into the step's directory."""

    def __init__(
        self,
        tmp: Path,
        *,
        latency_us=4000,
        failing_from=None,
        tag_blob=COMPOSE_BYTES,
        revision=TAG_COMMIT,
        top=PS_ONE,
        environment="production",
        siblings=None,
        nproc=4,
        generator_cpus=(),
        errors_from=None,
        unlogged_from=None,
        silent_at=None,
        silent_then_refused=False,
        restart_on_silence=False,
        refused_at=None,
        tick_in_step=None,
        tick_after_step=None,
        gap_ticks=(),
        scrape_ms=0,
    ):
        self.tmp = tmp
        #: how long an answered /metrics scrape takes: the API reads its
        #: counters when the scrape begins, and the scrape returns this later.
        self.scrape_ms = scrape_ms
        #: the API's safety tick ends 1 s into this step's load, and its metrics
        #: tick just after this step's after-scrape (the first scrape after
        #: `docker wait`), each 950 ms long; with either, or with gap_ticks,
        #: every scrape exports the tick families.
        self.tick_in_step = tick_in_step
        self.tick_after_step = tick_after_step
        #: (step, scheduler, ms): a tick that ran ms and ended just after that
        #: step's after-scrape; step 0 is the warm-up, which takes no scrape:
        #: its ticks end when its k6 exits, before step 1's before-scrape.
        self.gap_ticks = tuple(gap_ticks)
        self.ticks_ended: Optional[List[Tuple[str, int, int]]] = (
            None
            if tick_in_step is None and tick_after_step is None and not gap_ticks
            else []
        )
        #: when each answered /metrics scrape read the counters (fake clock)
        self.scrape_times: List[int] = []
        #: when each step's k6 sent its last request (step -> ms)
        self.last_sends: Dict[int, int] = {}
        self.waited = False
        #: the n-th /metrics scrape is refused (curl exit 7), nothing else wrong.
        self.refused_at = refused_at
        #: after the first timed-out scrape the API refuses connections (curl
        #: exit 7) for the rest of the silence: a crashed API, not a slow one.
        self.silent_then_refused = silent_then_refused
        #: the API container restarts when the silence begins (RestartCount + 1).
        self.restart_on_silence = restart_on_silence
        self.restart_count = 0
        self.silence_begun = False
        #: (n, ms): the n-th /metrics scrape finds the API silent, and every
        #: scrape begun in the next ms milliseconds of the fake clock times out
        #: (curl exit 28 after 30 s).
        self.silent_at = silent_at
        self.silent_until = 0
        self.scrapes = 0
        self.errors_from = errors_from
        self.unlogged_from = unlogged_from
        self.generator_cpus = list(generator_cpus)
        self.now = 1_793_600_000_000
        self.latency_us = latency_us
        self.failing_from = failing_from
        self.tag_blob = tag_blob
        self.revision = revision
        self.top = top
        self.environment = environment
        self.siblings = dict(siblings or fx.SMT)
        self.nproc = nproc
        self.calls: List[List[str]] = []
        self.envs: List[Dict[str, str]] = []
        self.step_rows = 0
        self.k6: Dict[str, Any] = {}
        self.sampler_since = None
        self.out = io.StringIO()

    # -- io ----------------------------------------------------------------
    def io(self):
        return sr.Io(
            run=self.run,
            read=self.read,
            http=self.http,
            now_ms=lambda: self.now,
            sleep=self.sleep,
            environ=GITHUB,
            pin=lambda cpu: None,
            cpu_count=lambda: self.nproc,
            uname=lambda: SimpleNamespace(release="6.8.0-1036-azure", machine="x86_64"),
            linux=True,
            out=self.out,
        )

    def sleep(self, seconds):
        self.now += int(seconds * 1000)

    def end_gap_ticks(self, step):
        """The planted gap ticks of ``step`` end now."""
        for after, name, ms in self.gap_ticks:
            if after == step:
                self.ticks_ended.append((name, self.now, ms))

    def tick_state(self):
        """The fake API's cumulative ticks now (fx.tick_lines' state), or None
        for an API that does not export the tick families."""
        if self.ticks_ended is None:
            return None
        state: Dict[str, Dict[str, Any]] = {}
        for name, at, ms in self.ticks_ended:
            if at <= self.now:
                entry = state.setdefault(name, {"success": 0, "ms": 0})
                entry["success"] += 1
                entry["ms"] += ms
                entry["seconds"] = f"{entry['ms'] // 1000}.{entry['ms'] % 1000:03d}"
        return state

    def http(self, method, url, host, body=None, headers=None, timeout=10.0):
        assert (
            url == "http://127.0.0.1:18080/api/v1/sdk/ruleset"
            and host == "localhost:18080"
        )
        assert headers == {"X-API-Key": "cvk_server_key_for_tests"}
        return 200, {"etag": '"abc123"', "x-ratelimit-limit": "10000000"}, b"{}"

    def read(self, path):
        if path == "/proc/cpuinfo":
            return "processor\t: 0\nmodel name\t: AMD EPYC 9V45 96-Core Processor\n"
        match = re.fullmatch(
            r"/sys/devices/system/cpu/(cpu\d)/topology/thread_siblings_list", path
        )
        if match:
            return self.siblings[match.group(1)] + "\n"
        match = re.fullmatch(r"/proc/(\d+)/cgroup", path)
        if match:
            ident = next(i for i, pid in PIDS.items() if pid == int(match.group(1)))
            return f"0::/system.slice/docker-{ident}.scope\n"
        raise FileNotFoundError(path)

    # -- processes ----------------------------------------------------------
    @staticmethod
    def done(stdout: str = "", code: int = 0, stderr: str = ""):
        return subprocess.CompletedProcess([], code, stdout.encode(), stderr.encode())

    def inspect(self, ref):
        ref = {SAMPLER_ID: sr.SAMPLER_NAME, GENERATOR_ID: sr.GENERATOR_NAME}.get(
            ref, ref
        )
        cpus = {
            ID["api"]: "0",
            ID["postgres"]: "1",
            ID["web"]: "1",
            ID["redis"]: "1",
            sr.SAMPLER_NAME: "2",
            sr.GENERATOR_NAME: "3",
        }
        if ref == sr.GENERATOR_NAME and self.generator_cpus:
            cpus[ref] = self.generator_cpus.pop(0)
        ident = {sr.SAMPLER_NAME: SAMPLER_ID, sr.GENERATOR_NAME: GENERATOR_ID}.get(
            ref, ref
        )
        name = next((n for n, i in ID.items() if i == ref), None)
        env = []
        if name == "api":
            env = [f"ENVIRONMENT={self.environment}", "METRICS_TOKEN=t"]
        if name == "postgres":
            env = [
                "POSTGRES_USER=scaleuser",
                "POSTGRES_DB=experimentation",
                "POSTGRES_PASSWORD=pw-for-tests",
            ]
        image = {
            "api": f"ghcr.io/controlvar/controlvar:core-{VERSION}",
            "web": f"ghcr.io/controlvar/controlvar-web:core-{VERSION}",
            "postgres": "postgres:16-alpine",
            "redis": "redis:7-alpine",
        }.get(name, "x")
        return [
            {
                "Id": ident,
                "State": {"Pid": PIDS[ident], "Running": True},
                "RestartCount": self.restart_count,
                "Image": fx.digest(f"{name}-image"),
                "Config": {"Image": image, "Env": env},
                "HostConfig": {"CpusetCpus": cpus[ref]},
                "NetworkSettings": {"Networks": {"controlvar-scale_default": {}}},
            }
        ]

    def image(self, ref):
        name = next(n for n in ID if fx.digest(f"{n}-image") == ref)
        repo = {
            "api": "ghcr.io/controlvar/controlvar",
            "web": "ghcr.io/controlvar/controlvar-web",
        }.get(name, name)
        labels = (
            {"org.opencontainers.image.revision": self.revision}
            if name in ("api", "web")
            else {}
        )
        return [
            {
                "Id": fx.digest(f"{name}-id"),
                "RepoDigests": [f"{repo}@{fx.digest(name)}"],
                "Config": {"Labels": labels},
                "Os": "linux",
                "Architecture": "amd64",
            }
        ]

    def run(self, argv, env):
        argv = list(argv)
        self.calls.append(argv)
        self.envs.append(dict(env))
        assert set(env) <= {*ss.KEEP_ENV, "PGPASSWORD"}, sorted(env)
        if argv[:2] == ["git", "-C"]:
            if argv[3] == "rev-parse":
                return self.done(TAG_COMMIT + "\n")
            if argv[3] == "show":
                return subprocess.CompletedProcess([], 0, self.tag_blob, b"")
        if argv[:2] == ["docker", "compose"]:
            assert argv[2:4] == ["-p", ss.PROJECT]
            if argv[-3:-1] == ["ps", "-q"]:
                return self.done(ID[argv[-1]] + "\n")
        if argv[:3] == ["docker", "image", "inspect"]:
            return self.done(json.dumps(self.image(argv[3])))
        if argv[:2] == ["docker", "inspect"]:
            return self.done(json.dumps(self.inspect(argv[2])))
        if argv[:2] == ["docker", "top"]:
            return self.done(self.top)
        if argv[:3] == ["docker", "rm", "-f"]:
            return self.done()
        if argv[:2] == ["docker", "pull"]:
            return self.done()
        if argv[:3] == ["docker", "run", "--rm"]:
            return self.done("k6 v2.3.0 (commit/e088784614, go1.27.1, linux/amd64)\n")
        if argv[:5] == ["docker", "run", "-d", "--name", sr.SAMPLER_NAME]:
            assert env.get("PGPASSWORD") == "pw-for-tests"
            assert "PGPASSWORD" in argv and "pw-for-tests" not in " ".join(argv)
            self.sampler_since = self.now
            return self.done(SAMPLER_ID + "\n")
        if argv[:5] == ["docker", "run", "-d", "--name", sr.GENERATOR_NAME]:
            return self.done(self.start_k6(argv) + "\n")
        if argv[:2] == ["docker", "wait"]:
            self.now = self.k6["start"] + self.k6["seconds"] * 1000 + 600
            self.k6["exited"] = self.now
            self.waited = True
            if self.k6["step"] == 0:
                self.end_gap_ticks(0)
            return self.done(str(self.k6["code"]) + "\n")
        if argv[:3] == ["docker", "exec", sr.SAMPLER_NAME]:
            return self.done(self.psql(argv[-1]) + "\n")
        if argv[:3] == ["docker", "exec", ID["api"]]:
            if argv[3:] == ["uname", "-m"]:
                return self.done("x86_64\n")
            self.scrapes += 1
            if self.refused_at == self.scrapes:
                return self.done(
                    "", 7, "curl: (7) Failed to connect to localhost port 8000"
                )
            if self.silent_at and self.scrapes == self.silent_at[0]:
                self.silent_until = self.now + self.silent_at[1]
            if self.now < self.silent_until:
                if self.silence_begun and self.silent_then_refused:
                    return self.done(
                        "", 7, "curl: (7) Failed to connect to localhost port 8000"
                    )
                if not self.silence_begun and self.restart_on_silence:
                    self.restart_count += 1
                self.silence_begun = True
                self.now += 30_000
                return self.done(
                    "", 28, "curl: (28) Operation timed out after 30001 milliseconds"
                )
            text = fx.metrics("a", tick_state=self.tick_state())
            self.scrape_times.append(self.now)
            if self.waited:
                self.waited = False
                if self.k6.get("step") == self.tick_after_step:
                    self.ticks_ended.append(("metrics", self.now, 950))
                if self.k6.get("step"):
                    self.end_gap_ticks(self.k6["step"])
            self.now += self.scrape_ms
            return self.done(text)
        if argv[:2] == ["docker", "logs"]:
            target = argv[-1]
            if target == sr.SAMPLER_NAME:
                return self.done(self.pg_log())
            if target == ID["web"]:
                line = (
                    '172.18.0.5 - - [02/Nov/2026:07:34:57 +0000] "POST /x HTTP/1.1" '
                    f'200 1 "-" "{sr.USER_AGENT}" "-"'
                )
                return self.done("\n".join([line] * self.step_rows) + "\n")
            return self.done("")
        raise AssertionError(f"FakeWorld: unexpected {argv}")

    def psql(self, sql):
        if "current_setting" in sql:
            return "100|128MB|on|fdatasync"
        if sql.startswith("SELECT concat_ws('|', (SELECT count(*)"):
            return "100|50|100000|1000000|1000000"
        if sql.startswith("SELECT count(*)"):
            return "100000"
        raise AssertionError(sql)

    def pg_log(self):
        lines = []
        for at in range(self.sampler_since, self.now, 2000):
            lines.append(f"scale-sample|{at}|1|0")
        return "\n".join(lines) + "\n"

    def start_k6(self, argv):
        variables = dict(
            argv[i + 1].split("=", 1) for i, word in enumerate(argv) if word == "-e"
        )
        mount = next(
            argv[i + 1]
            for i, word in enumerate(argv)
            if word == "-v" and argv[i + 1].endswith(":/out")
        )
        directory = Path(mount.rsplit(":", 1)[0])
        rate, unit = int(variables["SCALE_RATE"]), int(variables["SCALE_TIME_UNIT_MS"])
        seconds = int(variables["SCALE_DURATION_S"])
        settle_us = int(variables["SCALE_SETTLE_MS"]) * 1000
        step = int(variables["SCALE_STEP"])
        scheduled = se.scheduled_count(rate, unit, seconds)
        start = self.now + 100
        failing = self.failing_from is not None and step >= self.failing_from
        mix = json.loads(variables["SCALE_MIX"])
        records = []
        for i in range(scheduled):
            scenario = variables["SCALE_SCENARIO"]
            if scenario == "mix":
                slot, scenario = i % 100, None
                for name, weight in mix.items():
                    if slot < weight:
                        scenario = name
                        break
                    slot -= weight
            erring = self.errors_from is not None and step >= self.errors_from
            row = fx.row(
                scenario,
                i,
                sent_offset_us=i * unit * 1000 // rate,
                status=503 if erring and i % 10 == 0 else 200,
                latency_us=1_500_000 if failing else self.latency_us + i % 900,
            )
            record = {name: row.get(name, "") for name in sr.RECORD_FIELDS}
            record["scenario_start_ms"] = str(start)
            records.append(record)
        (directory / "k6.csv").write_text(k6_csv(records), "utf-8")
        measured = sum(1 for r in records if int(r["sent_offset_us"]) >= settle_us)
        (directory / "summary.json").write_text(
            json.dumps(
                {
                    "requests": len(records),
                    "measured": measured,
                    "dropped": 0,
                    "measured_p99_us": 9000,
                }
            ),
            "utf-8",
        )
        short = self.unlogged_from is not None and step >= self.unlogged_from
        self.step_rows = len(records) - (3 if short else 0)
        self.k6 = {"start": self.now, "seconds": seconds, "code": 0, "step": step}
        if step == self.tick_in_step:
            self.ticks_ended.append(("safety", self.now + 1000, 950))
        if scheduled:
            self.last_sends[step] = start + (scheduled - 1) * unit // rate
        assert variables["SCALE_BASE_URL"] == "http://web:8080"
        assert variables["SCALE_HOST"] == "localhost:18080"
        assert argv[argv.index("--cpuset-cpus") + 1] == "3"
        return GENERATOR_ID


def _work(tmp_path: Path) -> Path:
    work = tmp_path / "scale-work"
    work.mkdir(parents=True)
    ss.write_private(
        work / ".env",
        f"{ss.ENV_MARKER}={ss.PROJECT}\nHTTP_PORT=18080\n"
        "PUBLIC_BASE_URL=http://localhost:18080\nSCALE_METRICS_TOKEN=t\n",
    )
    ss.write_private(work / "fixtures.json", json.dumps(FIXTURES))
    return work


def _run(world: FakeWorld, tmp_path: Path, scenario="mix", mode="smoke", rep=1):
    work = _work(tmp_path)
    out = tmp_path / "out"
    try:
        status = sr.cmd_run(
            work, out, scenario, rep, mode, True, world.io(), FakeSampler(world.io())
        )
    except se.Refused as err:
        return 2, out, str(err)
    return status, out, world.out.getvalue()


def test_the_smoke_runs_two_valid_steps_and_writes_a_results_file_the_summariser_accepts(
    tmp_path,
):
    world = FakeWorld(tmp_path)
    status, out, said = _run(world, tmp_path)
    assert status == 0, said
    results = se.load_results((out / "results.json").read_text("utf-8"))
    (scenario,) = results["scenarios"]
    assert scenario["name"] == "mix"
    steps = scenario["reps"][0]["steps"]
    assert [s["milli_rps"] for s in steps] == fx.method(mode="smoke")["smoke"][
        "rates_milli_rps"
    ]
    assert all(s["valid"] and s["keeps_up"] for s in steps)
    sut = results["system_under_test"]
    assert sut["cpu_split"] == PE_SPLIT
    assert sut["compose_sha256"] == sut["compose_tag_blob_sha256"]
    assert sut["web_concurrency_observed"] == 1
    assert sut["sdk_rate_limit_per_minute"] == 10000000
    assert results["generator"]["image_digest"] == sr.K6_IMAGE.split("@")[1]
    rows = gzip.decompress((out / "rows.csv.gz").read_bytes()).decode()
    header = rows.splitlines()[0].split(",")
    assert header == ["step", *se.ROW_FIELDS]
    assert len(rows.splitlines()) - 1 == sum(s["rows"] for s in steps)
    # The step's files are beside the results, and the raw k6 CSV is gone.
    assert not list(out.glob("step-*/k6.csv"))
    assert (out / "step-01" / "observation.json").is_file()


def test_every_docker_call_carries_only_the_harness_s_environment(tmp_path):
    world = FakeWorld(tmp_path)
    assert _run(world, tmp_path)[0] == 0
    for argv, env in zip(world.calls, world.envs):
        allowed = {*ss.KEEP_ENV} | (
            {"PGPASSWORD"} if sr.SAMPLER_NAME in argv else set()
        )
        assert set(env) <= allowed, argv[:4]
    composes = [c for c in world.calls if c[:2] == ["docker", "compose"]]
    assert composes and all(c[2:4] == ["-p", ss.PROJECT] for c in composes)


def test_a_publication_run_stops_after_two_failing_steps_and_is_valid(tmp_path):
    world = FakeWorld(tmp_path, failing_from=3)
    status, out, said = _run(world, tmp_path, scenario="track", mode="publication")
    assert status == 0, said
    results = se.load_results((out / "results.json").read_text("utf-8"))
    rep = results["scenarios"][0]["reps"][0]
    assert len(rep["steps"]) == 4
    assert (
        rep["knee_step"],
        rep["first_failing_step"],
        rep["first_failing_reason"],
    ) == (2, 3, "p99")
    assert rep["valid"]


def _steps(out: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    results = json.loads((out / "results.json").read_text("utf-8"))
    return results, results["scenarios"][0]["reps"][0]


def test_a_tick_during_a_step_lands_in_that_step_and_one_between_steps_in_none(
    tmp_path,
):
    """The fake API's safety tick ends 1 s into step 2's load; its metrics tick
    ends just after step 2's after-scrape, before step 3's before-scrape. A
    step's ticks are the growth between its OWN two scrapes, so step 2 holds the
    safety tick, and the metrics tick is in no step (it is in the API's count:
    the run's last scrape shows it)."""
    world = FakeWorld(tmp_path, failing_from=3, tick_in_step=2, tick_after_step=2)
    status, out, said = _run(world, tmp_path, scenario="track", mode="publication")
    assert status == 0, said
    results = se.load_results((out / "results.json").read_text("utf-8"))
    steps = results["scenarios"][0]["reps"][0]["steps"]
    assert [s["scheduler_metrics"] for s in steps] == ["present"] * 4
    idle = fx.ticks()
    held = {
        s["step"]: {n: t for n, t in s["scheduler_ticks"].items() if t != idle[n]}
        for s in steps
    }
    safety = {"success": 1, "duration_count": 1, "duration_sum_us": 950_000}
    assert held == {
        1: {},
        2: {"safety": fx.ticks(safety=safety)["safety"]},
        3: {},
        4: {},
    }
    assert world.tick_state()["metrics"]["success"] == 1


def test_each_step_records_the_ticks_that_ended_in_the_gap_before_it(tmp_path):
    """The harness's own step loop on its fake API. A 30 s metrics tick ends
    in the warm-up (which takes no scrape), a 1.25 s safety tick just after
    step 1's after-scrape, and a 30 s metrics tick just after step 2's. No
    step's own scheduler_ticks holds any of them. Step 1's gap is
    "first-step", {} and null, so the warm-up's tick is counted nowhere; step
    2's gap holds the safety tick and step 3's the metrics tick, each with its
    duration, and every gap_ms runs from the previous step's last send to
    when the step's before-scrape answered (each scrape takes 40 ms here):
    both ends err wide. Against gap_ms the metrics tick is longer (it started
    before step 2's sending ended) and the safety tick shorter."""
    planted = ((0, "metrics", 30_000), (1, "safety", 1250), (2, "metrics", 30_000))
    world = FakeWorld(tmp_path, failing_from=3, gap_ticks=planted, scrape_ms=40)
    status, out, said = _run(world, tmp_path, scenario="track", mode="publication")
    assert status == 0, said
    results = se.load_results((out / "results.json").read_text("utf-8"))
    steps = results["scenarios"][0]["reps"][0]["steps"]
    assert len(steps) == 4
    assert [s["scheduler_ticks"] for s in steps] == [fx.ticks()] * 4
    assert [s["gap_metrics"] for s in steps] == ["first-step"] + ["present"] * 3
    safety = {"success": 1, "duration_count": 1, "duration_sum_us": 1_250_000}
    metrics = {"success": 1, "duration_count": 1, "duration_sum_us": 30_000_000}
    assert [s["gap_ticks"] for s in steps] == [
        {},
        fx.ticks(safety=safety),
        fx.ticks(metrics=metrics),
        fx.ticks(),
    ]
    # Step n's before-scrape is the (2n-1)-th scrape; each width runs from
    # the previous step's last send (never from when its k6 exited) to when
    # the scrape answered (never from when it began).
    answered = [at + world.scrape_ms for at in world.scrape_times[0::2]]
    widths = [answered[n - 1] - world.last_sends[n - 1] for n in (2, 3, 4)]
    assert [s["gap_ms"] for s in steps] == [None, *widths]
    gap_s = se.ranges(results["method"])["gap_s"]
    assert all(width > (gap_s + sr.DRAIN_S) * 1000 for width in widths)
    assert [
        tick["duration_sum_us"] > 1000 * step["gap_ms"]
        for step, tick in ((steps[1], safety), (steps[2], metrics))
    ] == [False, True]
    # The API counted all three: the warm-up's is in no field of any step.
    assert world.tick_state()["metrics"]["success"] == 2
    assert world.tick_state()["safety"]["success"] == 1
    assert se.recompute_problems(results) == []


@pytest.mark.regression
def test_a_confirming_step_whose_scrape_times_out_is_failing_once_the_api_recovers(
    tmp_path,
):
    """Run 41612833958: step 17, the confirming step, ran past saturation and
    its after-step /metrics scrape timed out; the run was thrown away as
    incomplete. Now the harness waits (bounded), records how long, and the step
    is FAILING (and never valid)."""
    # scrape 8 is step 4's (the confirming step's) after-scrape; the API stays
    # silent for 100 s of the fake clock.
    world = FakeWorld(tmp_path, failing_from=3, silent_at=(8, 100_000))
    status, out, said = _run(world, tmp_path, scenario="track", mode="publication")
    assert status == 0, said
    results, rep = _steps(out)
    assert results["status"] == "complete"
    assert [s["step"] for s in rep["steps"]] == [1, 2, 3, 4]
    last = rep["steps"][3]
    assert last["scrape_recovery_bound_s"] == sr.RECOVERY_BOUND_S
    assert 100_000 <= last["scrape_recovery_ms"] <= sr.RECOVERY_BOUND_S * 1000
    assert (last["valid"], last["keeps_up"]) == (False, False)
    assert (last["invalid_reason"], last["fail_reason"]) == (
        "api-unresponsive",
        "invalid",
    )
    for earlier in rep["steps"][:3]:
        assert (earlier["scrape_recovery_bound_s"], earlier["scrape_recovery_ms"]) == (
            0,
            0,
        )
    assert (rep["first_failing_step"], rep["knee_step"]) == (3, 2)
    assert rep["valid"]
    # the file passes the evidence script's own check (schema and recomputation)
    checked = se.load_results((out / "results.json").read_text("utf-8"))
    assert se.recompute_problems(checked) == []


def test_a_confirming_step_whose_api_never_recovers_leaves_the_run_incomplete(
    tmp_path,
):
    world = FakeWorld(tmp_path, failing_from=3, silent_at=(8, 10**9))
    status, out, said = _run(world, tmp_path, scenario="track", mode="publication")
    assert status == 1
    assert f"did not answer /metrics within {sr.RECOVERY_BOUND_S} s" in said
    results, rep = _steps(out)
    assert results["status"] == "incomplete"
    assert [s["step"] for s in rep["steps"]] == [1, 2, 3]
    # the bound is on the (fake) clock: the poll made a bounded number of
    # attempts, not an endless loop
    assert world.scrapes <= 8 + sr.RECOVERY_BOUND_S * 1000 // 30_000 + 1


def test_a_scrape_timeout_on_a_step_that_is_not_confirming_stays_failed(tmp_path):
    """Scrape 6 is step 3's after-scrape; step 3 follows a step that kept up."""
    world = FakeWorld(tmp_path, failing_from=3, silent_at=(6, 10**9))
    status, out, said = _run(world, tmp_path, scenario="track", mode="publication")
    assert status == 1
    assert "exited 28" in said and "waiting up to" not in said
    assert world.scrapes == 6
    results, rep = _steps(out)
    assert results["status"] == "incomplete"
    assert [s["step"] for s in rep["steps"]] == [1, 2]


@pytest.mark.regression
def test_a_crash_during_the_recovery_poll_leaves_the_run_incomplete(tmp_path):
    """#1691 review B1: one timeout, then the API refuses connections (curl
    exit 7: it crashed). The poll must not read that as "still recovering"."""
    world = FakeWorld(
        tmp_path, failing_from=3, silent_at=(8, 100_000), silent_then_refused=True
    )
    status, out, said = _run(world, tmp_path, scenario="track", mode="publication")
    assert status == 1
    assert "exited 7" in said and "did not answer /metrics within" not in said
    results, rep = _steps(out)
    assert results["status"] == "incomplete"
    assert [s["step"] for s in rep["steps"]] == [1, 2, 3]


@pytest.mark.regression
def test_a_non_timeout_scrape_failure_on_a_confirming_step_stays_fatal(tmp_path):
    """Only a timeout (exit 28) earns the recovery poll; a refused connection
    on the first scrape of the confirming step is an incomplete run."""
    world = FakeWorld(tmp_path, failing_from=3, refused_at=8)
    status, out, said = _run(world, tmp_path, scenario="track", mode="publication")
    assert status == 1
    assert "exited 7" in said and "waiting up to" not in said
    assert world.scrapes == 8
    results, rep = _steps(out)
    assert results["status"] == "incomplete"
    assert [s["step"] for s in rep["steps"]] == [1, 2, 3]


@pytest.mark.regression
def test_an_api_that_restarted_during_the_step_leaves_the_run_incomplete(tmp_path):
    """The API answers again within the bound, but its RestartCount moved: it
    crashed and `restart: unless-stopped` brought it back. Not "slow"."""
    world = FakeWorld(
        tmp_path, failing_from=3, silent_at=(8, 100_000), restart_on_silence=True
    )
    status, out, said = _run(world, tmp_path, scenario="track", mode="publication")
    assert status == 1
    assert "restarted during step 4" in said and "RestartCount 0 -> 1" in said
    results, rep = _steps(out)
    assert results["status"] == "incomplete"
    assert [s["step"] for s in rep["steps"]] == [1, 2, 3]


def test_a_smoke_step_with_a_failed_request_fails_the_smoke(tmp_path):
    """V20 at smoke scale: a 2xx that did not do its work, or a wrong status,
    fails the smoke."""
    world = FakeWorld(tmp_path, errors_from=2)
    status, _, said = _run(world, tmp_path)
    assert status == 1
    assert "smoke: step 2: 20 of 200 requests failed" in said


def test_an_invalid_smoke_step_fails_the_smoke(tmp_path):
    """V19 at smoke scale: nginx logged three requests fewer than k6 sent."""
    world = FakeWorld(tmp_path, unlogged_from=2)
    status, _, said = _run(world, tmp_path)
    assert status == 1
    assert "smoke: step 2: INVALID (client-server)" in said
    assert "step 2: client 250, server 247: 3 requests unaccounted" in said


def test_a_slow_smoke_step_is_reported_but_never_fails_the_smoke(tmp_path):
    """No pull-request gate asserts a wall-clock timing (PLAN 3.9): the first
    live smoke's batch requests took over a second at 10 requests a second."""
    world = FakeWorld(tmp_path, failing_from=2)
    status, _, said = _run(world, tmp_path)
    assert status == 0, said
    assert "step 2: 10.0 requests a second: valid, did not keep up (p99)" in said
    assert "smoke:" not in said


@pytest.mark.parametrize(
    "change, words",
    [
        (
            {"tag_blob": COMPOSE_BYTES.replace(b"restart:", b"restart :", 1)},
            f"compose.yml differs from the one v{VERSION} shipped: refusing to publish",
        ),
        ({"revision": "d" * 40}, "the api image's revision dddddddddddd is not"),
    ],
    ids=["refusal-i-compose", "refusal-ii-revision"],
)
def test_a_publication_run_refuses_before_measuring_a_release_it_is_not(
    tmp_path, change, words
):
    """V46 (EMS-7 (i) and (ii)), through run itself: refused, nothing written."""
    world = FakeWorld(tmp_path, **change)
    status, out, said = _run(world, tmp_path, scenario="track", mode="publication")
    assert status == 2 and words in said
    assert not (out / "results.json").exists()
    assert not any(
        c[:5] == ["docker", "run", "-d", "--name", sr.GENERATOR_NAME]
        for c in world.calls
    )


def test_a_step_whose_generator_moved_ends_the_run_incomplete(tmp_path):
    """#1616 review N3: the results' cpu_split is the docker inspect record of
    step 1, and every later step's must equal it."""
    world = FakeWorld(tmp_path, generator_cpus=("3", "2"))
    status, out, said = _run(world, tmp_path)
    assert status == 1
    assert "step 2 ran on the split" in said
    results = json.loads((out / "results.json").read_text("utf-8"))
    assert results["status"] == "incomplete"
    assert len(results["scenarios"][0]["reps"][0]["steps"]) == 1
    assert results["system_under_test"]["cpu_split"]["generator"] == "3"


def test_a_step_that_waited_for_the_api_is_invalid_and_failing_never_valid():
    meth = fx.method()
    seen = fx.observation(
        meth, "track", 1, scrape_recovery_bound_s=300, scrape_recovery_ms=95_000
    )
    step, messages = se.summarise_step(
        meth, "track", fx.step_rows(meth, "track", 1), seen, fx.metrics(), fx.metrics()
    )
    assert (step["scrape_recovery_bound_s"], step["scrape_recovery_ms"]) == (300, 95000)
    assert (step["valid"], step["keeps_up"]) == (False, False)
    assert step["invalid_reason"] == "api-unresponsive"
    assert any("did not answer /metrics for 95000 ms" in m for m in messages)


@pytest.mark.parametrize(
    "changes",
    [
        {"scrape_recovery_bound_s": 300, "scrape_recovery_ms": 300_001},
        {"scrape_recovery_bound_s": 300, "scrape_recovery_ms": 0},
        {"scrape_recovery_bound_s": 0, "scrape_recovery_ms": 5},
    ],
    ids=["past-the-bound", "bound-without-a-time", "time-without-a-bound"],
)
def test_an_inconsistent_scrape_recovery_is_refused(changes):
    meth = fx.method()
    seen = fx.observation(meth, "track", 1, **changes)
    with pytest.raises(se.Refused, match="scrape_recovery"):
        se.summarise_step(
            meth,
            "track",
            fx.step_rows(meth, "track", 1),
            seen,
            fx.metrics(),
            fx.metrics(),
        )


def test_a_step_record_keeps_each_thread_s_irq_and_softirq():
    """PE SMT condition 8: recorded apart from user and system, per thread."""
    meth = fx.method()
    seen = fx.observation(meth, "track", 1)
    seen["threads"]["cpu1"].update(irq_permille=7, softirq_permille=31)
    step, _ = se.summarise_step(
        meth, "track", fx.step_rows(meth, "track", 1), seen, fx.metrics(), fx.metrics()
    )
    assert (step["cpu1_irq_permille"], step["cpu1_softirq_permille"]) == (7, 31)
    assert (step["cpu0_irq_permille"], step["cpu0_softirq_permille"]) == (1, 4)
    # not part of foreign CPU twice: foreign is busy minus the pinned containers
    assert step["cpu1_foreign_permille"] == 360 - (200 + 80 + 70)


def test_the_smoke_records_but_does_not_refuse_a_compose_that_moved_on(tmp_path):
    world = FakeWorld(tmp_path, tag_blob=COMPOSE_BYTES + b"\n")
    status, out, said = _run(world, tmp_path)
    assert status == 0, said
    sut = json.loads((out / "results.json").read_text("utf-8"))["system_under_test"]
    assert sut["compose_sha256"] != sut["compose_tag_blob_sha256"]


@pytest.mark.parametrize(
    "change, words",
    [
        ({"top": PS_TWO}, "expected 1 workers (config), observed 2"),
        ({"environment": "test"}, "environment is test"),
        (
            {"siblings": fx.INTERLEAVED},
            "a hardware thread of the load generator's core",
        ),
        ({"nproc": 8}, "the VM reports 8 vCPUs"),
    ],
    ids=["two-workers", "environment-test", "interleaved-siblings", "eight-vcpus"],
)
def test_a_stack_or_runner_not_as_measured_is_refused_in_either_mode(
    tmp_path, change, words
):
    world = FakeWorld(tmp_path, **change)
    status, out, said = _run(world, tmp_path)
    assert status == 2 and words in said, said
    assert not (out / "results.json").exists()


@pytest.mark.parametrize(
    "scenario, mode",
    [("track", "smoke"), ("mix", "publication"), ("calibration", "publication")],
)
def test_run_refuses_a_scenario_outside_its_mode(tmp_path, scenario, mode):
    world = FakeWorld(tmp_path)
    assert _run(world, tmp_path, scenario=scenario, mode=mode)[0] == 2
    assert world.calls == []
