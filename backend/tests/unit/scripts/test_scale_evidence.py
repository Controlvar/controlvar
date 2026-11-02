# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""tests/scale/scale_evidence.py: the method file, the summariser, merge, render,
check and verify (#1608).

Every rule is fed a synthetic fixture built to break it, and the fixture is
named for what it plants. Nothing here starts a stack, reads a page of the
documentation or reaches the network: the render is exercised on temporary
copies, verify on a stand-in for ``gh``, and the one in-process request goes
through FastAPI's TestClient with the flag lookup served from memory.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path

import pytest

from backend.tests.unit.scripts import scale_evidence_fixtures as fx

se = fx.module()
REPO_ROOT = fx.REPO_ROOT
SCALE_DIR = REPO_ROOT / "tests" / "scale"
LEAK_GUARD = REPO_ROOT / "scripts" / "leak_guard.py"
LINK = "../performance/scale-results/0.40.0.json"


def refused(call, *args, **kwargs) -> str:
    with pytest.raises(se.Refused) as caught:
        call(*args, **kwargs)
    return str(caught.value)


# ---------------------------------------------------------------------------
# The method file (A1, V15, V34, V44)
# ---------------------------------------------------------------------------
def test_the_method_file_holds_the_named_values():
    method = se.load_config()
    assert method["mode"] == "publication"
    assert method["scenarios"] == list(se.SCENARIOS)
    assert method["step_multiplier_milli"] == 1260
    assert method["step_cap_milli_rps"] == 2_000_000
    assert method["start_milli_rps"] == {
        "evaluate_get": 5000,
        "assign": 5000,
        "assign_returning": 5000,
        "track": 5000,
        "batch": 1000,
        "ruleset": 2000,
    }
    assert [method[k] for k in ("settle_s", "measure_s", "gap_s", "warmup_s")] == [
        10,
        60,
        10,
        30,
    ]
    assert method["stop_after_failing_steps"] == 2
    assert [method[k] for k in ("timeout_ms", "max_vus_per_rps", "min_vus")] == [
        5000,
        2,
        20,
    ]
    assert method["keeps_up"] == {
        "max_errors_per_mille": 1,
        "max_p99_ms": 1000,
        "max_dropped": 0,
    }
    validity = method["validity"]
    assert validity["max_generator_cpu_permille"] == 900
    assert validity["min_sample_permille"] == 900
    assert validity["sent_identity_tolerance"] == 1
    assert validity["max_start_lag_p99_us"] % 1000 == 0
    assert validity["max_start_lag_p99_us"] >= 2000
    assert method["bottleneck"] == {
        "cpu_permille": 900,
        "io_wait_permille": 500,
        "pool_permille": 900,
        "serialised": {
            "max_cpu_permille": 900,
            "max_active_backends": 1,
            "min_share_permille": 800,
        },
    }
    assert (method["repetitions"], method["max_knee_spread_steps"]) == (3, 2)
    assert method["sdk_rate_limit_per_minute"] == 10_000_000
    assert method["expected_web_concurrency"] == 1
    assert method["preload"] == {
        "users": 100_000,
        "events": 1_000_000,
        "raw_metrics": 1_000_000,
    }
    assert method["smoke"]["rates_milli_rps"] == [5000, 10000]
    assert method["smoke"]["mix"] == {
        "evaluate_get": 60,
        "track": 25,
        "assign": 10,
        "batch": 4,
        "ruleset": 1,
    }


def test_the_method_file_is_integers_only():
    text = se.CONFIG.read_text(encoding="utf-8")
    assert se.loads(text)
    assert re.findall(r"[0-9][.eE]", text) == []


def _broken(**changes):
    config = json.loads(se.CONFIG.read_text(encoding="utf-8"))
    for dotted, value in changes.items():
        *path, last = dotted.split("__")
        node = config
        for key in path:
            node = node[key]
        if value is DELETE:
            del node[last]
        else:
            node[last] = value
    return json.dumps(config)


DELETE = object()


@pytest.mark.parametrize(
    "changes, named",
    [
        ({"extra_knob": 1}, "extra_knob: unknown key"),
        ({"repetitions": DELETE}, "repetitions: missing key"),
        ({"mode": "fast"}, "mode: must be one of"),
        ({"step_multiplier_milli": 1000}, "step_multiplier_milli: must be at least"),
        ({"measure_s": 20}, "measure_s: 20 s is a smoke window"),
        ({"smoke__mix__ruleset": 0}, "smoke.mix.ruleset: must be at least 1"),
        ({"smoke__mix__batch": 3}, "smoke.mix: the weights sum to 99"),
        ({"smoke": DELETE}, "smoke: missing key"),
        ({"validity__max_start_lag_p99_us": 2500}, "not a whole number of ms"),
        ({"validity__max_start_lag_p99_us": 1000}, "must be at least 2000"),
        ({"keeps_up__max_p99_ms": 0}, "keeps_up.max_p99_ms: must be at least 1"),
    ],
)
def test_the_loader_refuses_a_wrong_method_file_and_names_the_key(changes, named):
    message = refused(se.load_config, _broken(**changes))
    assert named in message


@pytest.mark.parametrize(
    "scenarios, named",
    [
        (["evaluate_get", "assign", "assign_returning", "track", "batch"], "ruleset"),
        ([*se.SCENARIOS, "evaluate_post"], "unknown scenario 'evaluate_post'"),
        ([*se.SCENARIOS, "mix"], "'mix' is smoke only"),
        ([*se.SCENARIOS, "track"], "'track' appears 2 times"),
    ],
)
def test_the_method_measures_exactly_the_six(scenarios, named):
    assert named in refused(se.load_config, _broken(scenarios=scenarios))


def test_a_float_in_the_method_file_is_refused_with_its_key():
    text = se.CONFIG.read_text(encoding="utf-8").replace(
        '"step_multiplier_milli": 1260', '"step_multiplier_milli": 1.26'
    )
    assert "float 1.26 at step_multiplier_milli" in refused(se.load_config, text)


def test_smoke_mode_runs_the_smoke_block_and_publication_never_does():
    smoke = se.load_config(mode="smoke")
    publication = se.load_config()
    assert se.ranges(smoke) == {
        "settle_s": 5,
        "measure_s": 20,
        "gap_s": 5,
        "warmup_s": 10,
    }
    assert se.ranges(publication) == {
        "settle_s": 10,
        "measure_s": 60,
        "gap_s": 10,
        "warmup_s": 30,
    }
    assert se.step_rates(smoke, "mix") == [5000, 10000]
    assert se.step_rates(publication, "track")[:2] == [5000, 6300]
    assert "mode must be publication or smoke" in refused(se.load_config, mode="fast")


# ---------------------------------------------------------------------------
# Rates, the exact pair, the schedule (A3, V16)
# ---------------------------------------------------------------------------
def test_the_steps_climb_by_the_multiplier_to_the_cap():
    method = se.load_config()
    rates = se.step_rates(method, "evaluate_get")
    assert rates[:6] == [5000, 6300, 7938, 10002, 12603, 15880]
    assert all(b == (a * 1260 + 500) // 1000 for a, b in zip(rates, rates[1:]))
    assert rates[-1] <= 2_000_000 < (rates[-1] * 1260 + 500) // 1000
    assert se.step_rates(method, "batch")[0] == 1000
    assert se.step_rates(method, "ruleset")[0] == 2000


@pytest.mark.parametrize(
    "milli, pair", [(5000, (1, 200)), (6300, (63, 10000)), (6800, (17, 2500))]
)
def test_each_step_is_an_exact_rate_pair(milli, pair):
    assert se.rate_pair(milli) == pair
    assert se.milli_rps_of(*pair) == milli


def test_every_step_of_the_method_has_an_exact_pair():
    method = se.load_config()
    for name in se.SCENARIOS:
        for milli in se.step_rates(method, name):
            assert se.milli_rps_of(*se.rate_pair(milli)) == milli


def test_an_inexact_pair_is_refused():
    assert "not a whole number of milli-requests" in refused(se.milli_rps_of, 1, 3)


@pytest.mark.regression
@pytest.mark.parametrize(
    "pair, expected", [((5, 1000), 350), ((17, 2500), 476), ((1, 1000), 70)]
)
def test_the_schedule_of_a_70_second_scenario(pair, expected):
    """5, 6.8 and 1 requests a second for 70 s: 350, 476 and 70 (the PE's numbers;
    the first draft's formula gave 0 for all three)."""
    assert se.scheduled_count(*pair, 70) == expected


def test_every_vu_exists_before_the_step():
    method = se.load_config()
    assert se.vus(method, 5000) == 20
    assert se.vus(method, 63_500) == 127
    assert se.vus(method, 2_000_000) == 4000


# ---------------------------------------------------------------------------
# /metrics read as sums (PE-S10)
# ---------------------------------------------------------------------------
def test_two_metrics_shapes_give_the_same_sums():
    a = se.metric_sums((fx.FIXTURES / "metrics_a.txt").read_text(encoding="utf-8"))
    b = se.metric_sums((fx.FIXTURES / "metrics_b.txt").read_text(encoding="utf-8"))
    for name in (
        "rate_limit_rejections_total",
        "rate_limit_redis_fallbacks_total",
        "rate_limit_hits_total",
        "http_request_duration_seconds_count",
    ):
        assert a[name] == b[name], name
    assert int(a["rate_limit_rejections_total"]) == 4
    assert all("{" not in name and "=" not in name for name in (*a, *b))


def test_a_counter_delta_is_a_whole_number_that_only_grows():
    before = se.metric_sums(fx.metrics("a", rejections=2))
    after = se.metric_sums(fx.metrics("b", rejections=5))
    assert se.counter_delta(before, after, "rate_limit_rejections_total") == 3
    assert se.counter_delta(before, after, "never_seen_total") == 0
    assert "a counter only grows" in refused(
        se.counter_delta, after, before, "rate_limit_rejections_total"
    )


# ---------------------------------------------------------------------------
# What passed (R2, V20)
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize(
    "scenario, facts, verdict",
    [
        ("assign", {"assigned": "0", "reason": "paused", "variant": ""}, "body"),
        ("evaluate_get", {"flag_class": "rule", "reason": "error"}, "body"),
        ("evaluate_get", {"flag_class": "plain", "reason": "inactive"}, "body"),
        ("batch", {"success_count": "0", "failure_count": "100"}, "body"),
        ("track", {"status": 429}, "status"),
    ],
)
def test_a_2xx_that_did_not_do_the_work_fails(scenario, facts, verdict):
    """QA-E3's five bodies: the old ``expect()`` passed the first four on their
    status alone. The judge reads the body and fails all five."""
    expect = se.expectations(se.load_config(), fx.VARIANTS)
    assert se.judge(scenario, fx.row(scenario, 0), expect) == ""
    assert se.judge(scenario, fx.row(scenario, 0, **facts), expect) == verdict


def test_each_request_type_is_judged_on_its_own_body():
    expect = se.expectations(se.load_config(), fx.VARIANTS)
    cases = [
        ("evaluate_get", {"flag_class": "rule", "reason": "rollout"}),
        ("assign", {"variant": "other"}),
        ("assign_returning", {"variant": fx.VARIANTS[1], "user_index": "4"}),
        ("track", {"variant_id_present": "0"}),
        ("ruleset", {"flag_count": "99"}),
    ]
    for scenario, facts in cases:
        assert se.judge(scenario, fx.row(scenario, 2, **facts), expect) == "body"
    assert se.judge("ruleset", fx.row("ruleset", 0, status=304), expect) == ""
    assert se.judge("track", fx.row("track", 0, error="timeout"), expect) == "timeout"


def test_the_returning_user_gets_the_variant_the_preload_rule_gives():
    expect = se.expectations(se.load_config(), ["treatment", "control"])
    row = fx.row("assign_returning", 7, variant="treatment", user_index="7")
    assert se.judge("assign_returning", row, expect) == ""
    row = fx.row("assign_returning", 8, variant="treatment", user_index="8")
    assert se.judge("assign_returning", row, expect) == "body"


# ---------------------------------------------------------------------------
# One step (A3: VALID and KEEPS UP; V16-V25, R1, R3)
# ---------------------------------------------------------------------------
def summarise(scenario="evaluate_get", number=4, rows=None, method=None, **observed):
    """summarise_step on a healthy step, with the observation's keys (and the
    two metrics texts) replaced as given."""
    method = method or se.load_config()
    rows = fx.step_rows(method, scenario, number) if rows is None else rows
    before = observed.pop("metrics_before", fx.metrics("a"))
    after = observed.pop("metrics_after", fx.metrics("a"))
    observation = fx.observation(method, scenario, number, rows=len(rows))
    observation.update(observed)
    return se.summarise_step(
        method, scenario, se.read_rows(fx.csv_text(rows)), observation, before, after
    )


def test_a_healthy_step_is_valid_and_keeps_up():
    step, messages = summarise()
    assert (step["valid"], step["keeps_up"], step["invalid_reason"]) == (
        True,
        True,
        "none",
    )
    assert messages == []
    assert step["rows"] + step["dropped"] == step["scheduled"] == 700
    # Iteration i is due at i x 500000 / 5001 ms: 101 is the first at or after 10 s.
    assert se.first_measured(5001, 500_000, 10) == 101
    assert step["measured"] == 700 - 101
    assert step["achieved_milli_rps"] == 599 * 1000 // 60
    assert step["milli_rps"] == 10002
    assert step["reasons"] == {"targeting_rule": 299, "rollout": 300}
    assert step["p50_us"] <= step["p95_us"] <= step["p99_us"]


@pytest.mark.regression
def test_throughput_and_percentiles_come_from_the_steady_window_only():
    """QA-E7: the old runner divided by the whole run, ramp included (97.7
    requests a second against 101.2 over the steady window). Here the settle
    window's rows are counted for the schedule and left out of every figure."""
    method = se.load_config()
    rows = fx.step_rows(method, "track", 4)
    settle = se.first_measured(5001, 500_000, 10)
    for row in rows[:settle]:
        row["latency_us"] = "900000"
    step, _ = summarise("track", 4, rows)
    assert step["rows"] == 700
    assert step["measured"] == len(rows) - settle == 599
    assert step["achieved_milli_rps"] == 599 * 1000 // 60
    assert step["p99_us"] < 900_000


@pytest.mark.regression
def test_a_csv_missing_its_last_second_is_invalid():
    """R1: rows lost after they were sent. 1/70 short, which the old 2%
    tolerance let through; the identity is exact to one request."""
    method = se.load_config()
    rows = fx.step_rows(method, "evaluate_get", 4, missing_tail=10)
    step, messages = summarise(rows=rows, server_count=700)
    assert step["invalid_reason"] == "sent-identity"
    assert messages[0] == "step 4: rows 690 + dropped 0 != scheduled 700"


@pytest.mark.regression
def test_the_real_stale_csv_pair_is_invalid():
    """R1's real pair: 5863 rows judged against 5905 served."""
    method = se.load_config()
    step = fx.step_facts(method, "track", 4)
    step.update(rows=5863, scheduled=5905, server_count=5863)
    fields, messages = se.judge_step(method, "track", step)
    assert fields["invalid_reason"] == "sent-identity"
    assert messages[0] == "step 4: rows 5863 + dropped 0 != scheduled 5905"


def test_one_request_short_is_within_the_tolerance():
    method = se.load_config()
    rows = fx.step_rows(method, "evaluate_get", 4, missing_tail=1)
    step, _ = summarise(rows=rows)
    assert step["valid"]


def test_37_drops_with_a_healthy_generator_is_valid_and_did_not_keep_up():
    """ADDENDUM A item 2: k6 advances no counter for a dropped tick, so every
    later start lag is inflated. A step with drops is judged on the drops."""
    method = se.load_config()
    rows = fx.step_rows(method, "evaluate_get", 4, dropped=37)
    for row in rows:
        row["start_lag_us"] = "250000"
    step, messages = summarise(rows=rows, dropped=37)
    assert step["valid"] and not step["keeps_up"]
    assert (step["invalid_reason"], step["fail_reason"]) == ("none", "dropped")
    assert step["percentiles_are_lower_bounds"]
    assert messages == [
        "step 4: 37 scheduled requests never sent; the server did not keep up"
    ]


def test_a_step_with_dropped_requests_never_keeps_up():
    method = se.load_config()
    step = fx.step_facts(method, "track", 4)
    one_dropped = {
        **step,
        "dropped": 1,
        "rows": step["rows"] - 1,
        "server_count": step["rows"] - 1,
    }
    fields, _ = se.judge_step(method, "track", one_dropped)
    assert fields == {
        "valid": True,
        "invalid_reason": "none",
        "keeps_up": False,
        "fail_reason": "dropped",
    }


@pytest.mark.parametrize(
    "changes, reason",
    [
        (
            {
                "start_lag_p99_us": 41_000,
                "generator_cpu_permille": 500,
                "cpu3_busy_permille": 510,
            },
            "start-lag",
        ),
        ({"cpu3_busy_permille": 400, "generator_cpu_permille": 100}, "foreign-cpu"),
        ({"generator_cpu_permille": 950, "cpu3_busy_permille": 960}, "generator-cpu"),
    ],
    ids=[
        "start-lag-with-the-container-at-half",
        "foreign-work-on-cpu3",
        "rule-4-alone",
    ],
)
def test_a_step_that_measures_the_generator_is_invalid(changes, reason):
    """V25: start lag over N while the container is at half; foreign work on the
    generator's core while the container idles; and the container at 950 with
    start lag and foreign CPU in limits (rule 4 on its own)."""
    method = se.load_config()
    step = fx.step_facts(method, "track", 5, **changes)
    assert (step["valid"], step["invalid_reason"]) == (False, reason)
    assert step["fail_reason"] == "invalid" and not step["keeps_up"]


def test_the_start_lag_message():
    method = se.load_config()
    step = fx.step_facts(method, "track", 5)
    _, messages = se.judge_step(method, "track", {**step, "start_lag_p99_us": 41_000})
    assert messages[0] == (
        "step 5: p99 start lag 41 ms over the limit of 5 ms: this step measures the "
        "generator"
    )


def test_a_rate_limiter_answer_without_the_header_is_invalid():
    method = se.load_config()
    rows = fx.step_rows(method, "evaluate_get", 4)
    rows[5]["rate_limit"] = ""
    rows[6]["rate_limit"] = "6000"
    step, messages = summarise(rows=rows)
    assert step["invalid_reason"] == "limiter-header"
    assert "2 answers without X-RateLimit-Limit 10000000" in messages[0]


def test_a_429_or_a_counted_rejection_is_invalid():
    method = se.load_config()
    rows = fx.step_rows(method, "evaluate_get", 4)
    rows[9].update(status="429")
    step, _ = summarise(rows=rows)
    assert step["invalid_reason"] == "limiter-rejected"
    step, messages = summarise(
        metrics_before=fx.metrics("a"), metrics_after=fx.metrics("b", rejections=3)
    )
    assert step["invalid_reason"] == "limiter-rejected"
    assert "the limiter counted 3 rejections" in messages[0]


def test_a_fallback_that_starts_and_ends_inside_the_step_is_caught():
    """The gauge reads 0 at both ends; the counter and the log lines do not."""
    step, messages = summarise(
        metrics_before=fx.metrics("a", fallbacks=0),
        metrics_after=fx.metrics("b", fallbacks=1),
    )
    assert step["invalid_reason"] == "redis-fallback"
    assert messages == ["limiter fell back to per-process counting during step 4"]
    step, _ = summarise(fallback_log_lines=2)
    assert step["invalid_reason"] == "redis-fallback"


def test_too_few_resource_samples_is_invalid():
    step, _ = summarise(samples=53)
    assert step["invalid_reason"] == "samples"
    step, _ = summarise(samples=54)
    assert step["valid"]


def test_client_and_server_counts_must_agree():
    method = se.load_config()
    step = fx.step_facts(method, "track", 4)
    fields, messages = se.judge_step(
        method,
        "track",
        {**step, "rows": 5863, "scheduled": 5863, "server_count": 5905},
    )
    assert fields["invalid_reason"] == "client-server"
    assert messages[0].startswith(
        "step 4: client 5863, server 5905: 42 requests unaccounted"
    )


def test_a_client_timeout_reconciles_against_the_logged_499():
    method = se.load_config()
    rows = fx.step_rows(method, "evaluate_get", 4)
    rows[650] = fx.row("evaluate_get", 650, error="timeout", latency_us=5_000_000)
    step, _ = summarise(rows=rows, server_499=1)
    assert step["valid"] and step["percentiles_are_lower_bounds"]
    step, _ = summarise(rows=rows, server_499=0)
    assert step["invalid_reason"] == "client-server"


def test_a_returning_user_that_wrote_a_row_is_invalid():
    step, messages = summarise(
        "assign_returning", 4, assignments_before=100_000, assignments_after=100_012
    )
    assert step["invalid_reason"] == "assignments-grew"
    assert messages[0] == "assign_returning: assignments grew by 12 during step 4"


def test_a_returning_user_with_another_variant_is_an_error():
    method = se.load_config()
    rows = fx.step_rows(method, "assign_returning", 4)
    for row in rows[200:210]:
        row["variant"] = fx.VARIANTS[(int(row["user_index"]) + 1) % 2]
    step, _ = summarise("assign_returning", 4, rows)
    assert step["errors"] == 10 and step["fail_reason"] == "errors"


def test_an_unexpected_evaluate_reason_is_an_error_and_counted():
    method = se.load_config()
    rows = fx.step_rows(method, "evaluate_get", 4)
    rows[200].update(reason="inactive")
    step, _ = summarise(rows=rows)
    assert step["errors"] == 1
    assert step["reasons"]["inactive"] == 1
    assert step["keeps_up"] is False and step["fail_reason"] == "errors"


def test_one_error_in_a_thousand_keeps_up_and_two_do_not():
    """Step 7 measures 1,199 requests: 1 error is within 1 in 1,000, 2 are not
    (1000 x errors > measured x 1, in integers)."""
    method = se.load_config()
    step = fx.step_facts(method, "track", 7, errors=1)
    assert step["measured"] == 1199 and step["keeps_up"]
    step = fx.step_facts(method, "track", 7, errors=2)
    assert step["fail_reason"] == "errors"


def test_mix_is_smoke_only():
    method = se.load_config()
    assert "mix is smoke only: refusing a mix row" in refused(
        se.summarise_step, method, "mix", [], {}, "", ""
    )
    smoke = se.load_config(mode="smoke")
    # 5 requests a second for 5 + 20 s: 125 requests, of the mix's five kinds.
    rows = [
        fx.row(kind, i)
        for i, kind in enumerate(
            ["evaluate_get", "track", "assign", "batch", "ruleset"] * 25
        )
    ]
    observation = fx.observation(smoke, "mix", 1, rows=len(rows), samples=20)
    observation.update(scenario="mix")
    step, _ = se.summarise_step(
        smoke, "mix", se.read_rows(fx.csv_text(rows)), observation, "", ""
    )
    assert step["valid"] and step["keeps_up"] and step["scheduled"] == 125


def test_a_step_off_the_method_series_is_refused():
    method = se.load_config()
    observation = fx.observation(method, "track", 4)
    observation.update(rate=1, time_unit_ms=100)
    message = refused(se.summarise_step, method, "track", [], observation, "", "")
    assert "step 4 ran at 10000 milli-rps; the method's step 4 is 10002" in message


def test_a_row_twice_is_refused():
    method = se.load_config()
    rows = fx.step_rows(method, "track", 4)
    rows[3] = dict(rows[2])
    assert "iteration 2 appears twice" in refused(summarise, "track", 4, rows)


# ---------------------------------------------------------------------------
# A run: the knee, the first failing step, what stopped it (V28, V29)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["errors", "dropped", "p99"])
def test_the_knee_is_the_step_before_the_first_failing_one(kind):
    method = se.load_config()
    steps = fx.rep_steps(method, "track", 4, [kind, kind])
    assessed = se.assess_rep(method, steps)
    assert (assessed["knee_step"], assessed["first_failing_step"]) == (4, 5)
    assert assessed["first_failing_reason"] == kind
    assert assessed["valid"]


def test_a_run_whose_first_failing_step_is_invalid_is_not_valid():
    method = se.load_config()
    steps = fx.rep_steps(method, "track", 4, ["p99"])
    steps.append(fx.step_facts(method, "track", 6))
    steps[4] = fx.step_facts(method, "track", 5, generator_cpu_permille=950)
    assessed = se.assess_rep(method, steps)
    assert assessed == {
        "valid": False,
        "knee_step": 4,
        "first_failing_step": 5,
        "first_failing_reason": "invalid",
        "bottleneck": "invalid",
    }


def _failing(**changes):
    method = se.load_config()
    base = {
        "p99_us": 1_400_000,
        "api_core_permille": 600,
        "cpu0_busy_permille": 620,
        "active_backend_samples_by_count": [0, 27, 3],
        "io_wait_observations": 0,
    }
    base.update(changes)
    return method, fx.step_facts(method, "track", 6, **base)


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({}, "serialised"),
        ({"active_backend_samples_by_count": [0, 12, 18]}, "none-at-limit"),
        ({"api_core_permille": 950, "cpu0_busy_permille": 960}, "cpu:api"),
        ({"postgres_core_permille": 930}, "cpu:postgres"),
        ({"web_redis_core_permille": 910}, "cpu:web-redis"),
        (
            {"active_backend_samples_by_count": [0, 0, 30], "io_wait_observations": 40},
            "postgres-io",
        ),
        ({"active_backend_samples_by_count": [0] * 36 + [30]}, "db-pool"),
        ({"active_backend_samples_by_count": [0, 24, 6]}, "serialised"),
        ({"active_backend_samples_by_count": [0, 23, 7]}, "none-at-limit"),
        ({"cpu0_steal_permille": 60, "cpu0_busy_permille": 650}, "none-at-limit"),
        ({"p99_us": 900_000}, "none-at-limit"),
    ],
    ids=[
        "1-api-at-60-one-backend-in-90-percent",
        "2-one-backend-in-40-percent",
        "3-api-core-at-95",
        "postgres-core",
        "web-redis-core",
        "4-io-wait-in-most-samples",
        "db-pool-at-36-of-40",
        "5-boundary-24-of-30-fires",
        "5-boundary-23-of-30-does-not",
        "6-cpu0-steal-and-foreign-over-the-limit",
        "not-failed-on-latency",
    ],
)
def test_what_stopped_it_is_computed(changes, expected):
    """V29's fixtures. 24 of 30 is 1000 x 24 >= 800 x 30; 23 of 30 is not. A
    comparison written ``>`` or a share of 750 or 850 moves one of the two."""
    method, step = _failing(**changes)
    assert se.attribute(method, step) == expected


def test_an_invalid_first_failing_step_gives_invalid_never_serialised():
    method, step = _failing(generator_cpu_permille=950, cpu3_busy_permille=960)
    assert step["valid"] is False
    assert se.attribute(method, step) == "invalid"


def test_the_issue_is_linked_only_when_serialised_is_the_result():
    serial = se.render_block(fx.small(), LINK)
    assert serial.count(se.TRACKING_ISSUE) == len(se.SCENARIOS)
    meth = fx.method()
    reps = {
        name: [
            fx.rep(meth, name, n, fx.rep_steps(meth, name, 4, ["dropped", "dropped"]))
            for n in (1, 2, 3)
        ]
        for name in se.SCENARIOS
    }
    hot = se.render_block(fx.results(meth, reps), LINK)
    assert "the API's core was busy at least 90% of the time" in hot
    assert se.TRACKING_ISSUE not in hot
    assert "#823" not in hot


# ---------------------------------------------------------------------------
# The results file (V4, V5, V6, V7, V23, V24, V26, V40, V43, V44, V46)
# ---------------------------------------------------------------------------
def test_a_consistent_results_file_loads_and_is_publishable():
    results = fx.small()
    loaded = se.load_results(se.dumps(results))
    assert loaded == results
    assert se.publication_problems(loaded) == []


@pytest.mark.parametrize(
    "edit, named",
    [
        (
            lambda r: r["run"].update(harness_commit="HEAD"),
            "run.harness_commit must be 40 hex characters, got 'HEAD'",
        ),
        (
            lambda r: r["system_under_test"]["images"]["api"].pop("image_id"),
            "system_under_test.images.api.image_id: missing key",
        ),
        (lambda r: r["run"].pop("harness_commit"), "run.harness_commit: missing key"),
        (lambda r: r.update(comment="hand-edited"), "comment: unknown key"),
        (
            lambda r: r["system_under_test"].update(environment="Production"),
            "system_under_test.environment: must be one of",
        ),
    ],
    ids=["commit-HEAD", "no-image-id", "no-commit", "unknown-key", "mixed-case-env"],
)
def test_the_schema_is_closed(edit, named):
    results = fx.small()
    edit(results)
    assert named in refused(se.load_results, json.dumps(results))


def test_any_float_is_refused_with_its_path():
    results = fx.small()
    text = json.dumps(results)
    p99 = results["scenarios"][0]["reps"][0]["steps"][3]["p99_us"]
    text = text.replace(f'"p99_us": {p99},', '"p99_us": 1199.97,', 1)
    message = refused(se.load_results, text)
    assert "float 1199.97 at scenarios[0].reps[0].steps[3].p99_us" in message


def test_an_edited_aggregate_is_caught_by_the_recomputation():
    results = fx.small()
    results["scenarios"][3]["summary"]["steps"][3]["p99_us"] += 1
    problems = se.recompute_problems(results)
    assert problems == [
        "track: aggregate step 4 p99_us 25001 != 25000, from runs [25000, 25000, 25000]"
    ]


def test_an_edited_step_judgement_is_caught():
    results = fx.small()
    results["scenarios"][0]["reps"][1]["steps"][5]["keeps_up"] = True
    problems = se.recompute_problems(results)
    assert "evaluate_get run 2 step 6: keeps_up True != False" in problems[0]


def test_a_deleted_run_is_caught_and_not_publishable():
    results = fx.small()
    results["scenarios"][2]["reps"].pop()
    assert "assign_returning: summary reps 3 != 2" in se.recompute_problems(results)[0]
    meth = results["method"]
    results["scenarios"][2]["summary"] = se.summarise_scenario(
        meth, results["scenarios"][2]["reps"]
    )
    problems = se.publication_problems(results)
    assert (
        "POST /api/v1/tracking/assign (returning user): 2 runs; the method says 3"
        in problems
    )


def test_a_mix_row_and_a_missing_request_are_refused():
    results = fx.small()
    mix = copy.deepcopy(results["scenarios"][0])
    mix["name"] = "mix"
    results["scenarios"].append(mix)
    assert "mix is smoke only: refusing a mix row" in refused(
        se.render_block, results, LINK
    )
    results = fx.small()
    del results["scenarios"][2]
    assert (
        "measured 5 of the six: missing POST /api/v1/tracking/assign (returning user)"
        in refused(se.render_block, results, LINK)
    )


@pytest.mark.parametrize(
    "edit, named",
    [
        (
            lambda r: r["system_under_test"].update(web_concurrency_observed=2),
            "expected 1 workers (config), observed 2",
        ),
        (
            lambda r: r["system_under_test"].update(
                web_concurrency_expected=2, web_concurrency_observed=2
            ),
            "expected 1 workers (config), observed 2",
        ),
        (
            lambda r: r["system_under_test"].update(environment="test"),
            "environment is test: schedulers and rate limiter are off",
        ),
        (
            lambda r: r["system_under_test"].update(compose_sha256="0" * 64),
            "compose.yml differs from the one v0.40.0 shipped: refusing to publish",
        ),
        (
            lambda r: r["system_under_test"]["images"]["api"].update(revision="1" * 40),
            "the api image's revision 111111111111 is not v0.40.0's commit",
        ),
        (
            lambda r: r["system_under_test"]["cpu_split"].update(generator="2-3"),
            "the generator shares cpu [2] with web",
        ),
        (
            lambda r: r["system_under_test"].update(sdk_rate_limit_per_minute=6000),
            "the API answered with a rate limit of 6,000",
        ),
        (lambda r: r.update(status="incomplete"), "the results are incomplete"),
    ],
    ids=[
        "two-workers",
        "two-workers-stated-and-observed",
        "environment-test",
        "compose-not-the-release",
        "revision-not-the-tag",
        "generator-shares-a-core",
        "default-rate-limit",
        "interrupted",
    ],
)
def test_a_file_that_is_not_the_shipped_shape_is_not_published(edit, named):
    """V23: the expected worker count is the method's constant, so a planted
    WEB_CONCURRENCY=2 that moves both stated and observed still fails. V46:
    the compose file and the images are the release's."""
    results = fx.small()
    edit(results)
    assert any(named in problem for problem in se.publication_problems(results))
    assert named in refused(se.render_block, results, LINK)


def test_an_emulated_api_is_not_published():
    results = fx.small()
    for scenario in results["scenarios"]:
        for rep in scenario["reps"]:
            rep["host"]["machine"] = "aarch64"
    problems = se.publication_problems(results)
    assert "API runs linux/amd64 emulated on aarch64: refusing to publish" in problems


def test_the_data_the_runs_started_from_is_the_method_s():
    results = fx.small()
    rep = results["scenarios"][3]["reps"][0]
    rep["counts_before"]["events"] = 10_000
    summary = se.summarise_scenario(results["method"], results["scenarios"][3]["reps"])
    assert summary["not_publishable"] == ["preconditions"]
    results["scenarios"][3]["summary"] = summary
    assert (
        "POST /api/v1/tracking/track run 1: precondition: expected 1,000,000 events, "
        "found 10,000" in se.publication_problems(results)
    )


def test_a_spread_wider_than_two_steps_is_unstable():
    meth = fx.method()
    reps = [
        fx.rep(meth, "track", n, fx.rep_steps(meth, "track", knee, ["p99"]))
        for n, knee in ((1, 4), (2, 5), (3, 7))
    ]
    summary = se.summarise_scenario(meth, reps)
    assert (summary["knee_spread_steps"], summary["unstable"]) == (3, True)
    assert "unstable" in summary["not_publishable"]


def test_a_run_below_the_series_or_above_the_cap_is_not_a_number():
    meth = fx.method()
    below = [
        fx.rep(meth, "track", n, fx.rep_steps(meth, "track", 0, ["p99", "p99"]))
        for n in (1, 2, 3)
    ]
    assert "below-the-series" in se.summarise_scenario(meth, below)["not_publishable"]
    above = [
        fx.rep(meth, "track", n, fx.rep_steps(meth, "track", 3, [])) for n in (1, 2, 3)
    ]
    summary = se.summarise_scenario(meth, above)
    assert "above-the-cap" in summary["not_publishable"]
    assert above[0]["bottleneck"] == "not-reached"


# ---------------------------------------------------------------------------
# merge (V7, V40, V44)
# ---------------------------------------------------------------------------
def _jobs(results):
    jobs = []
    for scenario in results["scenarios"]:
        for rep in scenario["reps"]:
            job = copy.deepcopy({k: v for k, v in results.items() if k != "scenarios"})
            job["scenarios"] = [
                {
                    "name": scenario["name"],
                    "reps": [copy.deepcopy(rep)],
                    "summary": se.summarise_scenario(results["method"], [rep]),
                }
            ]
            jobs.append(se.load_results(se.dumps(job)))
    return jobs


def test_merge_makes_the_one_file_from_the_eighteen_jobs():
    results = fx.small()
    jobs = _jobs(results)
    assert len(jobs) == 18
    assert se.merge(list(reversed(jobs))) == results


def test_merge_refuses_runs_of_different_images():
    jobs = _jobs(fx.small())
    jobs[7]["system_under_test"]["images"]["api"]["digest"] = fx.digest("rebuilt")
    assert refused(se.merge, jobs) == "reps used different api images"


def test_merge_refuses_smoke_an_interrupted_job_and_a_repeated_run():
    jobs = _jobs(fx.small())
    smoke = copy.deepcopy(jobs)
    smoke[0]["method"]["mode"] = "smoke"
    assert "smoke results cannot be merged" in refused(se.merge, smoke)
    jobs[3]["status"] = "incomplete"
    assert "the run was interrupted" in refused(se.merge, jobs)
    jobs = _jobs(fx.small())
    assert "track run 1 appears twice" in refused(se.merge, [*jobs, jobs[9]])


def test_render_refuses_smoke_results():
    results = fx.small()
    results["method"]["mode"] = "smoke"
    assert "smoke results cannot be rendered" in refused(se.render_block, results, LINK)


# ---------------------------------------------------------------------------
# render: formats, the block and its registry entries (A6, A7, V1)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "milli, text",
    [
        (5040, "5.04"),
        (10_002, "10.0"),
        (101_166, "101"),
        (99_950, "100"),
        (9_995, "10.0"),
        (1_587_600, "1,590"),
        (2_000_000, "2,000"),
        (1000, "1.00"),
    ],
)
def test_rates_print_to_three_significant_figures(milli, text):
    assert se.three_figures(milli) == text


@pytest.mark.parametrize(
    "us, text",
    [
        (999, "under 1 ms"),
        (1000, "1 ms"),
        (1499, "1 ms"),
        (1500, "2 ms"),
        (1_250_400, "1,250 ms"),
    ],
)
def test_latency_prints_in_whole_milliseconds(us, text):
    assert se._ms(us) == text


def test_errors_print_as_counts_never_a_percentage():
    block = se.render_block(fx.small(), LINK)
    assert "| 0 of 1,797 |" in block  # 599 measured in each of 3 runs
    rows = [line for line in block.split("\n") if line.startswith("| GET /api/v1")]
    assert rows and all("%" not in row for row in rows)


def test_the_first_sentence_is_dated_and_names_the_file():
    block = se.render_block(fx.small(), LINK)
    first = se.block_units(block)[0]
    assert se._doc_claims().dated(first)
    assert first.startswith("Measured as of v0.40.0 on 2026-11-20")
    assert "0.40.0.json" in first


def test_the_setup_states_the_limit_the_tool_and_the_method_commit():
    results = fx.small()
    block = se.render_block(results, LINK)
    assert (
        "| SDK rate limit | 10,000,000 a minute per address per path (a measurement "
        "setting, not advice) |" in block
    )
    assert (
        "| Load generator | k6 2.3.0 (AGPL-3.0), the official image, unmodified |"
        in block
    )
    assert f"the harness at commit {results['run']['harness_commit']}" in block
    assert "| The load generator's core | alone on its core |" in block


def test_the_topology_is_stated_from_the_record():
    results = fx.small()
    rep = results["scenarios"][0]["reps"][0]
    rep["host"] = fx.host(1, exclusive=False)
    block = se.render_block(results, LINK)
    assert "| The load generator's core | sharing a physical core with cpu 2 |" in block


def test_a_step_with_drops_shows_its_percentiles_as_at_least():
    meth = fx.method()
    reps = {
        name: [
            fx.rep(meth, name, n, fx.rep_steps(meth, name, 4, ["dropped", "dropped"]))
            for n in (1, 2, 3)
        ]
        for name in se.SCENARIOS
    }
    block = se.render_block(fx.results(meth, reps), LINK)
    assert "| 5 | 12.6 requests a second |" in block
    row = next(line for line in block.split("\n") if "| 5 | 12.6 requests" in line)
    assert "at least" in row


PAGE = """# Capacity and Load Test Results

What the release served on one machine.

<!-- scale:begin -->
<!-- scale:end -->

## Before you compare it with your traffic

Your traffic is not this traffic.
"""


def _registry(entries):
    return se._doc_claims().registry_text("capacity.md", entries)


OUTSIDE = [
    {"text": "Capacity and Load Test Results", "by": ["exempt:label"]},
    {"text": "What the release served on one machine.", "by": ["exempt:navigation"]},
    {"text": "Before you compare it with your traffic", "by": ["exempt:label"]},
    {"text": "Your traffic is not this traffic.", "by": ["exempt:advice"]},
]


def _rendered(results=None):
    results = results or fx.small()
    block = se.render_block(results, LINK)
    return results, *se.apply_render(PAGE, _registry(OUTSIDE), block)


def test_render_writes_the_block_and_the_block_s_registry_entries():
    results, page, registry = _rendered()
    units = [u.text for u in se._doc_claims().units(page)]
    entries = tomllib.loads(registry)["unit"]
    assert [e["text"] for e in entries[:2]] == [e["text"] for e in OUTSIDE[:2]]
    assert entries[:2] == OUTSIDE[:2] and entries[-2:] == OUTSIDE[2:]
    block = entries[2:-2]
    assert all(entry["by"] == [se.MECHANISM] for entry in block)
    assert sum(entry.get("count", 1) for entry in entries) == len(units)
    assert se.check_page(results, page, registry, LINK) == []


def test_render_is_idempotent():
    results, page, registry = _rendered()
    block = se.render_block(results, LINK)
    assert se.apply_render(page, registry, block) == (page, registry)


@pytest.mark.parametrize(
    "plant, named",
    [
        (
            lambda page, reg, r: (page.replace("| 25 ms |", "| 24 ms |", 1), reg, r),
            "block line",
        ),
        (
            lambda page, reg, r: (
                "\n".join(
                    l
                    for l in page.split("\n")
                    if not l.startswith("| POST /api/v1/tracking/track |")
                ),
                reg,
                r,
            ),
            "block line",
        ),
        (
            lambda page, reg, r: (
                page,
                reg.replace(f'"{se.MECHANISM}"', '"exempt:label"', 1),
                r,
            ),
            "registry entry for unit 1",
        ),
    ],
    ids=["a-page-digit", "c-a-row-deleted", "d-a-registry-entry"],
)
def test_check_page_names_a_hand_edit(plant, named):
    results, page, registry = _rendered()
    page, registry, results = plant(page, registry, results)
    problems = se.check_page(results, page, registry, LINK)
    assert any(named in problem for problem in problems), problems


def test_check_page_names_a_changed_file_with_the_page_left_alone():
    results, page, registry = _rendered()
    changed = copy.deepcopy(results)
    for rep in changed["scenarios"][3]["reps"]:
        rep["steps"][3]["p99_us"] = 24_000
    changed["scenarios"][3]["summary"] = se.summarise_scenario(
        changed["method"], changed["scenarios"][3]["reps"]
    )
    problems = se.check_page(changed, page, registry, LINK)
    assert problems and problems[0].startswith("block line")
    assert "25 ms" in problems[0] and "24 ms" in problems[0]


def test_render_refuses_what_it_cannot_keep_apart():
    block = se.render_block(fx.small(), LINK)
    registry = _registry(OUTSIDE)
    assert "not in the layout" in refused(
        se.apply_render, PAGE, "# a note\n" + registry, block
    )
    clash = PAGE.replace("Your traffic is not this traffic.", "What it served")
    assert "both inside and outside" in refused(se.apply_render, clash, registry, block)
    twice = PAGE.replace(se.END, f"{se.END}\n{se.BEGIN}\n{se.END}")
    assert "exactly one" in refused(se.apply_render, twice, registry, block)


def test_the_newest_results_file_is_the_highest_version(tmp_path):
    for name in ("0.9.0.json", "0.40.0.json", "0.10.2.json", "notes.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    assert se.newest_results(tmp_path).name == "0.40.0.json"


# ---------------------------------------------------------------------------
# The Leak Guard proof (V43, V47, EMS-15)
# ---------------------------------------------------------------------------
def _leak_guard(text: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LEAK_GUARD), "--stdin"],
        input=text,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )


def test_the_leak_guard_accepts_the_render_and_refuses_a_planted_figure():
    """PE F8: the guard's claim rule matches the bare text 99.97, so a rendered
    "99.97%" or a float such as 1199.97 in a committed results file would be
    refused. Integers and error counts keep both out."""
    rendered = se.render_block(se.load_results(fx.FULL_SIZE.read_text("utf-8")), LINK)
    clean = _leak_guard(rendered)
    assert clean.returncode == 0, clean.stderr
    planted = _leak_guard(rendered + "| planted | 99.97% of 6,000 |\n")
    assert planted.returncode == 1
    assert "false-claim: '99.97'" in planted.stderr
    assert _leak_guard(json.dumps({"p99_ms": 1199.97})).returncode == 1
    assert _leak_guard(json.dumps({"p99_us": 1199970})).returncode == 0


def test_the_full_size_fixture_is_what_the_builder_makes():
    assert fx.FULL_SIZE.read_text(encoding="utf-8") == se.dumps(fx.full_size()), (
        "regenerate with python -m backend.tests.unit.scripts.scale_evidence_fixtures"
    )


def test_the_full_size_fixture_has_the_real_file_s_shape():
    text = fx.FULL_SIZE.read_text(encoding="utf-8")
    results = se.load_results(text)
    assert se.publication_problems(results) == []
    reps = [rep for scenario in results["scenarios"] for rep in scenario["reps"]]
    assert len(reps) == 18
    assert all(15 <= len(rep["steps"]) <= 17 for rep in reps)
    assert '"environment": "production"' in text
    assert len(set(re.findall(r"sha256:[0-9a-f]{64}", text))) >= 8
    for line in text.splitlines():
        assert '": "/' not in line, line


def test_the_leak_guard_names_a_planted_path_or_environment_in_the_fixture():
    text = fx.FULL_SIZE.read_text(encoding="utf-8")
    assert _leak_guard(text).returncode == 0
    home = "/".join(["", "home", "alice", "scale"])
    with_path = text.replace(
        '"cpu_model": "AMD EPYC', f'"cpu_model": "{home} AMD EPYC', 1
    )
    guarded = _leak_guard(with_path)
    assert guarded.returncode == 1 and "home-directory-path" in guarded.stderr
    assert "cpu_model must be a CPU model name" in refused(se.load_results, with_path)
    shouted = text.replace('"environment": "production"', '"environment": "Production"')
    guarded = _leak_guard(shouted)
    assert guarded.returncode == 1 and "false-claim" in guarded.stderr
    assert "environment: must be one of" in refused(se.load_results, shouted)


# ---------------------------------------------------------------------------
# verify: the committed file is the run's artifact (V3)
# ---------------------------------------------------------------------------
def _gh(head_sha, artifact):
    def run(argv):
        if argv[:2] == ["run", "view"]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"headSha": head_sha}), ""
            )
        if argv[:2] == ["run", "download"]:
            if artifact is None:
                return subprocess.CompletedProcess(
                    argv, 1, "", "no valid artifacts found"
                )
            directory = Path(argv[argv.index("--dir") + 1])
            (directory / se.RESULTS_FILE).write_bytes(artifact)
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    return run


def _committed(tmp_path):
    results = fx.small()
    path = tmp_path / "0.40.0.json"
    path.write_text(se.dumps(results), encoding="utf-8")
    return results, path


def test_verify_accepts_the_artifact_byte_for_byte(tmp_path):
    results, path = _committed(tmp_path)
    runner = _gh(results["run"]["harness_commit"], path.read_bytes())
    assert se.verify(path, 40999123456, "Controlvar/controlvar", runner) == []


def test_verify_refuses_an_edited_value(tmp_path):
    results, path = _committed(tmp_path)
    artifact = path.read_bytes()
    path.write_text(
        path.read_text("utf-8").replace('"p50_us": 3150', '"p50_us": 3151', 1), "utf-8"
    )
    problems = se.verify(
        path,
        40999123456,
        "Controlvar/controlvar",
        _gh(results["run"]["harness_commit"], artifact),
    )
    assert problems == ["results file differs from the artifact of run 40999123456"]


def test_verify_refuses_a_run_of_another_commit(tmp_path):
    results, path = _committed(tmp_path)
    other = fx.sha("another")
    problems = se.verify(
        path, 40999123456, "Controlvar/controlvar", _gh(other, path.read_bytes())
    )
    assert problems == [
        f"run 40999123456 built {other}, the file says {results['run']['harness_commit']}"
    ]
    problems = se.verify(
        path,
        41000000001,
        "Controlvar/controlvar",
        _gh(results["run"]["harness_commit"], path.read_bytes()),
    )
    assert problems[0] == "the file names run 40999123456, not 41000000001"


def test_verify_fails_on_an_expired_artifact_never_skips(tmp_path):
    results, path = _committed(tmp_path)
    problems = se.verify(
        path,
        40999123456,
        "Controlvar/controlvar",
        _gh(results["run"]["harness_commit"], None),
    )
    assert problems == ["artifact of run 40999123456 is gone: re-run"]


# ---------------------------------------------------------------------------
# The command line, and the extension point for the harness
# ---------------------------------------------------------------------------
def _cli(*argv):
    return subprocess.run(
        [sys.executable, "-B", str(SCALE_DIR / "scale_evidence.py"), *argv],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=300,
    )


def test_summarise_exits_by_validity(tmp_path):
    method = se.load_config()
    paths = {
        name: tmp_path / name for name in ("rows.csv", "obs.json", "a.txt", "b.txt")
    }
    rows = fx.step_rows(method, "track", 4)
    paths["rows.csv"].write_text(fx.csv_text(rows), "utf-8")
    paths["a.txt"].write_text(fx.metrics("a"), "utf-8")
    paths["b.txt"].write_text(fx.metrics("b"), "utf-8")
    common = [
        "summarise",
        "--scenario",
        "track",
        "--rows",
        str(paths["rows.csv"]),
        "--observation",
        str(paths["obs.json"]),
        "--metrics-before",
        str(paths["a.txt"]),
        "--metrics-after",
        str(paths["b.txt"]),
        "--out",
        str(tmp_path / "step.json"),
    ]
    paths["obs.json"].write_text(
        json.dumps(fx.observation(method, "track", 4)), "utf-8"
    )
    assert _cli(*common).returncode == 0
    assert json.loads((tmp_path / "step.json").read_text("utf-8"))["keeps_up"] is True
    paths["obs.json"].write_text(
        json.dumps(fx.observation(method, "track", 4, fallback_log_lines=1)), "utf-8"
    )
    failed = _cli(*common)
    assert failed.returncode == 1
    assert "limiter fell back to per-process counting during step 4" in failed.stderr
    paths["obs.json"].write_text('{"dropped": 1.5}', "utf-8")
    refusal = _cli(*common)
    assert refusal.returncode == 2 and "float 1.5 at dropped" in refusal.stderr


def test_merge_render_and_check_on_the_command_line(tmp_path):
    jobs = _jobs(fx.small())
    paths = []
    for index, job in enumerate(jobs):
        path = tmp_path / f"job-{index}.json"
        path.write_text(se.dumps(job), "utf-8")
        paths.append(str(path))
    out = tmp_path / "performance" / "scale-results" / "0.40.0.json"
    out.parent.mkdir(parents=True)
    merged = _cli("merge", *paths, "--out", str(out))
    assert merged.returncode == 0, merged.stderr
    page = tmp_path / "self-hosting" / "capacity.md"
    page.parent.mkdir()
    page.write_text(PAGE, "utf-8")
    registry = tmp_path / "capacity.md.toml"
    registry.write_text(_registry(OUTSIDE), "utf-8")
    rendered = _cli(
        "render",
        "--results",
        str(out),
        "--page",
        str(page),
        "--registry",
        str(registry),
    )
    assert rendered.returncode == 0, rendered.stderr
    assert f"[0.40.0.json]({LINK})" in page.read_text("utf-8")
    args = [
        "check",
        "--results",
        str(out),
        "--publishable",
        "--page",
        str(page),
        "--registry",
        str(registry),
    ]
    assert _cli(*args).returncode == 0
    page.write_text(
        page.read_text("utf-8").replace("| 25 ms |", "| 24 ms |", 1), "utf-8"
    )
    edited = _cli(*args)
    assert edited.returncode == 1 and "block line" in edited.stderr


def test_the_harness_commands_plug_in_beside_the_module(tmp_path):
    (tmp_path / "scale_evidence.py").write_text(
        (SCALE_DIR / "scale_evidence.py").read_text("utf-8"), "utf-8"
    )
    (tmp_path / "scale_harness.py").write_text(
        "def add_commands(commands):\n"
        "    up = commands.add_parser('up')\n"
        "    up.set_defaults(handler=lambda args: 7)\n",
        "utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-B", str(tmp_path / "scale_evidence.py"), "up"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
    )
    assert result.returncode == 7, result.stderr
    assert _cli("up").returncode == 2


# ---------------------------------------------------------------------------
# The k6 script's tags (V45, PE-S2)
# ---------------------------------------------------------------------------
GOOD_SCRIPT = """export const options = {
  systemTags: ['status', 'method', 'scenario', 'expected_response'],
  scenarios: {},
};
"""


@pytest.mark.parametrize(
    "script, problems",
    [
        (GOOD_SCRIPT, []),
        (
            GOOD_SCRIPT.replace(
                "  systemTags: ['status', 'method', 'scenario', 'expected_response'],\n",
                "",
            ),
            ["the script sets systemTags 0 times"],
        ),
        (
            GOOD_SCRIPT.replace("'status',", "'status', 'url',"),
            ["system tags include 'url'"],
        ),
        (
            GOOD_SCRIPT.replace("'method',", "'name', \"error\","),
            ["system tags include 'name'", "system tags include 'error'"],
        ),
    ],
    ids=["explicit-and-clean", "setting-removed", "url-added", "name-and-error"],
)
def test_the_generator_s_tags_carry_no_url(script, problems):
    found = se.system_tags_problems(script)
    assert len(found) == len(problems)
    assert all(want in got for want, got in zip(problems, found))


def test_every_k6_script_under_tests_scale_keeps_the_url_out():
    """The scenario script arrives with the harness; from then on every script
    there is held to the rule above."""
    for script in sorted(SCALE_DIR.rglob("*.js")):
        assert se.system_tags_problems(script.read_text("utf-8")) == [], script


# ---------------------------------------------------------------------------
# Where the harness lives, and that it is linted (R4)
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_tests_scale_holds_no_test_file_no_job_would_collect():
    """pytest collects only backend/tests and modules/backend/tests
    (pyproject.toml testpaths), so a test under tests/scale would never run."""
    assert (SCALE_DIR / "scale_evidence.py").is_file()
    stray = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in SCALE_DIR.rglob("*.py")
        if path.name.startswith("test_") or path.name.endswith("_test.py")
    )
    assert stray == []


def test_the_harness_python_is_lint_clean():
    """tests/scale is outside the lint job's paths, so this runs the same ruff,
    with the repository's configuration, over it."""
    ruff = [sys.executable, "-m", "ruff"]
    config = ["--config", str(REPO_ROOT / "pyproject.toml")]
    files = [str(path) for path in sorted(SCALE_DIR.rglob("*.py"))]
    assert files
    check = subprocess.run(
        [*ruff, "check", *config, *files],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    form = subprocess.run(
        [*ruff, "format", "--check", *config, *files],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert form.returncode == 0, form.stdout + form.stderr


def test_the_pool_constant_is_the_engine_s():
    text = (REPO_ROOT / "backend" / "app" / "db" / "session.py").read_text("utf-8")
    sizes = [
        int(re.search(rf"{key}=([0-9]+)", text).group(1))
        for key in ("pool_size", "max_overflow")
    ]
    assert sum(sizes) == se.POOL_CONNECTIONS_PER_WORKER


# ---------------------------------------------------------------------------
# The evaluate body check, pinned against the real evaluation (EMS-5)
# ---------------------------------------------------------------------------
def test_the_expected_evaluate_reasons_are_what_the_evaluation_answers():
    """A rule flag sent the scenario's matching context, and a plain flag sent
    none, through the real route, targeting adapter and flag service. Only the
    flag lookup and the API key are served from memory."""
    from fastapi.testclient import TestClient

    from backend.app.api import deps
    from backend.app.main import app
    from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus

    def flag(key, rules):
        return FeatureFlag(
            id=uuid.uuid4(),
            key=key,
            name=key,
            status=FeatureFlagStatus.ACTIVE.value,
            owner_id=uuid.uuid4(),
            rollout_percentage=se.FIXTURE_ROLLOUT_PERCENTAGE,
            targeting_rules=copy.deepcopy(rules),
            value_type="boolean",
        )

    flags = {
        "scale_flag_000": flag("scale_flag_000", se.RULE_FLAG_RULES),
        "scale_flag_001": flag("scale_flag_001", None),
    }

    class Lookup:
        def __init__(self, model):
            self.key = None

        def filter(self, clause):
            self.key = clause.right.value
            return self

        def first(self):
            return flags.get(self.key)

    class Session:
        def query(self, model):
            return Lookup(model)

        def add(self, row):
            pass

        def commit(self):
            pass

        def refresh(self, row):
            pass

        def rollback(self):
            pass

    def session():
        yield Session()

    app.dependency_overrides[deps.get_db] = session
    app.dependency_overrides[deps.get_api_key] = lambda: {"scopes": []}
    try:
        client = TestClient(app)
        context = json.dumps(se.RULE_FLAG_CONTEXT)
        answers = {}
        for user in ("user-00001", "user-00002", "user-00003", "user-00004"):
            rule = client.get(
                "/api/v1/feature-flags/evaluate/scale_flag_000",
                params={"user_id": user, "context": context},
                headers={"X-API-Key": "in-memory"},
            )
            plain = client.get(
                "/api/v1/feature-flags/evaluate/scale_flag_001",
                params={"user_id": user},
                headers={"X-API-Key": "in-memory"},
            )
            assert rule.status_code == plain.status_code == 200, rule.text + plain.text
            answers.setdefault("rule_flag", set()).add(rule.json()["reason"])
            answers.setdefault("plain_flag", set()).add(plain.json()["reason"])
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_api_key, None)
    expected = se.load_config()["evaluate_expected_reason"]
    for key, seen in answers.items():
        assert seen == {expected[key]}, (
            f"evaluate_expected_reason.{key} is {expected[key]!r}; the evaluation "
            f"answered {sorted(seen)}"
        )
