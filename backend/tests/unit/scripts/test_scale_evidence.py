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

import ast
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
        ({"repetitions": 4}, "repetitions: 4 is even"),
        ({"cpu_split": dict(fx.SPLIT)}, "cpu_split: unknown key"),
    ],
)
def test_the_loader_refuses_a_wrong_method_file_and_names_the_key(changes, named):
    """Among them: an even repetition count, which has no median run, and a
    CPU split, which the method file never holds (the split is recorded from
    the running containers, never copied from a configuration)."""
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


@pytest.mark.regression
@pytest.mark.parametrize(
    "line",
    [
        'rate_limit_rejections_total{endpoint="track"} 5.0 # {trace_id="x"} 1.0',
        'rate_limit_redis_fallbacks_total 5.0 # {trace_id="x"} 1.0',
        'rate_limit_rejections_total{endpoint="track"} 5.0 1762000000000',
    ],
    ids=["exemplar-after-labels", "exemplar-unlabelled", "timestamp"],
)
def test_anything_after_the_value_is_refused_never_read_as_the_value(line):
    """An OpenMetrics exemplar used to be read as the value: the old parser
    took the text after the last ``}``, so ``5.0 # {...} 1.0`` summed to 1.0."""
    message = refused(se.metric_sums, line + "\n")
    assert "metrics line 1: something follows the value" in message


def test_a_label_value_may_hold_a_closing_brace():
    sums = se.metric_sums('x_total{a="}",b="\\"}"} 3.0\n')
    assert sums == {"x_total": 3}
    assert "its labels are not closed" in refused(se.metric_sums, 'x_total{a="}" 3\n')


@pytest.mark.regression
@pytest.mark.parametrize("which", ["before", "after"])
def test_a_scrape_without_the_fallback_counter_is_refused(which):
    """An empty /metrics scrape at both ends used to give counter deltas of 0
    and a VALID step: the counter legs of the limiter rules passed on nothing."""
    empty = {f"metrics_{which}": ""}
    message = refused(summarise, **empty)
    assert f"the metrics {which} step 4 have no rate_limit_redis_fallbacks_total" in (
        message
    )
    other = {f"metrics_{which}": 'rate_limit_hits_total{endpoint="track"} 5.0\n'}
    assert "have no rate_limit_redis_fallbacks_total" in refused(summarise, **other)


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
    fields, messages = se.judge_step(method, "track", step, fx.SPLIT)
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
    fields, _ = se.judge_step(method, "track", one_dropped, fx.SPLIT)
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


METHOD = se.load_config()
N = METHOD["validity"]["max_start_lag_p99_us"]
F = METHOD["validity"]["max_foreign_cpu_permille"]
G = METHOD["validity"]["max_generator_cpu_permille"]


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"start_lag_p99_us": N}, "none"),
        ({"start_lag_p99_us": N + 1}, "start-lag"),
        ({"generator_cpu_permille": 100, "cpu3_busy_permille": 100 + F}, "none"),
        (
            {"generator_cpu_permille": 100, "cpu3_busy_permille": 101 + F},
            "foreign-cpu",
        ),
        ({"generator_cpu_permille": G - 1, "cpu3_busy_permille": G + 9}, "none"),
        ({"generator_cpu_permille": G, "cpu3_busy_permille": G + 10}, "generator-cpu"),
    ],
    ids=[
        "start-lag-equal-to-N",
        "start-lag-N-plus-1",
        "foreign-equal-to-F",
        "foreign-F-plus-1",
        "generator-at-899",
        "generator-at-900",
    ],
)
def test_the_generator_rules_hold_at_their_boundaries(changes, reason):
    """VALID rules 2, 3 and 4 at their limits, as V29 pins 24 and 23 of 30: a
    start lag of exactly N and a foreign CPU of exactly F are VALID, one more
    is not; the generator at 900 per-mille is INVALID and at 899 is not."""
    step = fx.step_facts(METHOD, "track", 5, **changes)
    assert step["invalid_reason"] == reason
    assert step["valid"] is (reason == "none")


def test_the_start_lag_message():
    method = se.load_config()
    step = fx.step_facts(method, "track", 5)
    _, messages = se.judge_step(
        method, "track", {**step, "start_lag_p99_us": 41_000}, fx.SPLIT
    )
    assert messages[0] == (
        "step 5: p99 start lag 41 ms over the limit of 5 ms: this step measures the "
        "generator"
    )


def test_the_generator_s_thread_is_the_recorded_one():
    """Rule 3 reads the foreign CPU of the thread the split pins k6 to, not a
    thread named in the code."""
    method = se.load_config()
    moved = {**fx.SPLIT, "generator": "2", "pg_sampler": "3"}
    step = fx.step_facts(method, "track", 5, moved, cpu2_busy_permille=400)
    assert step["invalid_reason"] == "foreign-cpu"
    _, messages = se.judge_step(method, "track", step, moved)
    assert "of the generator's hardware thread, cpu2 (limit 100)" in messages[0]
    assert fx.step_facts(method, "track", 5, cpu2_busy_permille=400)["valid"]


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
        fx.SPLIT,
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
        fx.row(kind, i, sent_offset_us=i * 200_000)
        for i, kind in enumerate(
            ["evaluate_get", "track", "assign", "batch", "ruleset"] * 25
        )
    ]
    observation = fx.observation(smoke, "mix", 1, rows=len(rows), samples=20)
    observation.update(scenario="mix")
    step, _ = se.summarise_step(
        smoke,
        "mix",
        se.read_rows(fx.csv_text(rows)),
        observation,
        fx.metrics("a"),
        fx.metrics("a"),
    )
    assert step["valid"] and step["keeps_up"] and step["scheduled"] == 125


def _dropped_step(sent_at_s, dropped, row_changes=None):
    """A smoke step (5 requests a second, 5 s settle, 20 s window) that dropped
    ``dropped`` iterations, as the V16 tamper produced it: the rows are numbered
    by START order (0, 1, 2, ...), whatever slot each was due in, and carry the
    time each was sent. Returns the step record and its messages."""
    smoke = se.load_config(mode="smoke")
    kinds = ["evaluate_get", "track", "assign", "batch", "ruleset"]
    row_changes = row_changes or {}
    rows = [
        fx.row(
            kinds[i % 5],
            i,
            sent_offset_us=int(at * 1_000_000),
            **row_changes.get(i, {}),
        )
        for i, at in enumerate(sent_at_s)
    ]
    observation = fx.observation(smoke, "mix", 1, rows=len(rows), samples=20)
    observation.update(scenario="mix", dropped=dropped)
    return se.summarise_step(
        smoke,
        "mix",
        se.read_rows(fx.csv_text(rows)),
        observation,
        fx.metrics("a"),
        fx.metrics("a"),
    )


@pytest.mark.regression
def test_a_dropped_step_measures_the_requests_sent_in_its_window():
    """#1667. 20 of the 125 scheduled requests were sent, one every 1.25 s, so
    the iteration numbers 0..19 are START order and only 5..24 of the slots
    were ever reached. The first four went out in the 5 s settle; the other
    sixteen in the window. Windowing on the iteration number (``iteration >=
    125 - 100``) found none of them."""
    sent = [i * 1.25 for i in range(20)]
    step, _ = _dropped_step(
        sent,
        dropped=105,
        row_changes={
            # A failure and a slow answer in the settle: not measured.
            1: {"status": 503, "latency_us": 9_000_000},
            # A failure in the window: measured.
            7: {"status": 503, "latency_us": 12_000},
            # Start lag is not computed once iterations were dropped.
            19: {"start_lag_us": 7_000_000},
        },
    )
    assert step["rows"] == 20 and step["dropped"] == 105
    assert step["measured"] == 16
    assert step["errors"] == 1
    assert step["p99_us"] == 12_000
    assert step["start_lag_p99_us"] == 0
    assert step["fail_reason"] == "dropped" and step["valid"]
    assert step["percentiles_are_lower_bounds"]


@pytest.mark.regression
def test_a_step_that_dropped_everything_in_its_window_is_invalid_and_says_so():
    """#1667. Four requests, all sent in the settle: nothing was measured, and
    the step does not offer a percentile or an '<e> of <m> failed' for it."""
    step, messages = _dropped_step([0, 1.0, 2.0, 3.0], dropped=121)
    assert step["measured"] == 0
    assert not step["valid"] and not step["keeps_up"]
    assert step["invalid_reason"] == "nothing-measured"
    assert (
        "step 1: no request was sent in the measure window, so nothing was measured"
        in messages
    )


def test_the_observation_carries_every_container_and_every_thread():
    """Per-container CPU and /proc/stat busy and steal for all four hardware
    threads, and the split as recorded: a missing one is refused, and so is an
    unpinned container (docker inspect's empty cpuset) or one on two threads."""
    method = se.load_config()
    rows = se.read_rows(fx.csv_text(fx.step_rows(method, "track", 4)))

    def observed(edit):
        observation = fx.observation(method, "track", 4)
        edit(observation)
        return refused(
            se.summarise_step,
            method,
            "track",
            rows,
            observation,
            fx.metrics("a"),
            fx.metrics("a"),
        )

    assert "observation.threads.cpu2: missing key" in observed(
        lambda o: o["threads"].pop("cpu2")
    )
    assert "observation.cpu_permille.redis: missing key" in observed(
        lambda o: o["cpu_permille"].pop("redis")
    )
    assert "observation.cpu_split.generator: missing key" in observed(
        lambda o: o["cpu_split"].pop("generator")
    )
    assert "observation.cpu_split.generator must be a CPU list" in observed(
        lambda o: o["cpu_split"].update(generator="")
    )
    assert "observation.cpu_split.web is '1-2': each container is pinned" in observed(
        lambda o: o["cpu_split"].update(web="1-2")
    )
    assert "observation.cpu_split.redis: cpu5 is not one of the sampled" in observed(
        lambda o: o["cpu_split"].update(redis="5")
    )


def test_foreign_cpu_is_each_thread_s_busy_minus_its_pinned_containers():
    """cpu1 holds PostgreSQL, nginx and Redis: 200 + 80 + 70 of its 360."""
    step, _ = summarise("track", 4)
    assert [step[f"cpu{i}_foreign_permille"] for i in range(4)] == [20, 10, 30, 20]
    assert step["web_cpu_permille"] == 80 and step["redis_cpu_permille"] == 70


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
    assessed = se.assess_rep(method, steps, fx.SPLIT)
    assert (assessed["knee_step"], assessed["first_failing_step"]) == (4, 5)
    assert assessed["first_failing_reason"] == kind
    assert assessed["valid"]


def test_a_run_whose_first_failing_step_is_invalid_is_not_valid():
    method = se.load_config()
    steps = fx.rep_steps(method, "track", 4, ["p99"])
    steps.append(fx.step_facts(method, "track", 6))
    steps[4] = fx.step_facts(method, "track", 5, generator_cpu_permille=950)
    assessed = se.assess_rep(method, steps, fx.SPLIT)
    assert assessed == {
        "valid": False,
        "knee_step": 4,
        "first_failing_step": 5,
        "first_failing_reason": "invalid",
        "bottleneck": "invalid",
    }


def _failing(split=None, **changes):
    method = se.load_config()
    base = {
        "p99_us": 1_400_000,
        "api_cpu_permille": 600,
        "cpu0_busy_permille": 620,
        "active_backend_samples_by_count": [0, 27, 3],
        "io_wait_observations": 0,
    }
    base.update(changes)
    return method, fx.step_facts(method, "track", 6, split, **base)


# On cpu1, PostgreSQL, nginx and Redis use 300 + 100 + 100 = 500 per-mille.
CPU1 = {
    "postgres_cpu_permille": 300,
    "web_cpu_permille": 100,
    "redis_cpu_permille": 100,
}


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({}, "serialised"),
        ({"active_backend_samples_by_count": [0, 12, 18]}, "none-at-limit"),
        ({"api_cpu_permille": 950, "cpu0_busy_permille": 960}, "cpu:api"),
        ({"postgres_cpu_permille": 930}, "cpu:postgres+web+redis"),
        (
            {
                "postgres_cpu_permille": 500,
                "web_cpu_permille": 250,
                "redis_cpu_permille": 200,
            },
            "cpu:postgres+web+redis",
        ),
        (
            {"active_backend_samples_by_count": [0, 0, 30], "io_wait_observations": 40},
            "postgres-io",
        ),
        ({"active_backend_samples_by_count": [0] * 36 + [30]}, "db-pool"),
        ({"active_backend_samples_by_count": [0, 24, 6]}, "serialised"),
        ({"active_backend_samples_by_count": [0, 23, 7]}, "none-at-limit"),
        ({"cpu0_steal_permille": 60, "cpu0_busy_permille": 650}, "none-at-limit"),
        (
            {**CPU1, "cpu1_steal_permille": 60, "cpu1_busy_permille": 550},
            "none-at-limit",
        ),
        ({**CPU1, "cpu1_steal_permille": 60, "cpu1_busy_permille": 540}, "serialised"),
        ({"p99_us": 900_000}, "none-at-limit"),
    ],
    ids=[
        "1-api-at-60-one-backend-in-90-percent",
        "2-one-backend-in-40-percent",
        "3-api-thread-at-95",
        "postgres-thread",
        "cpu1-group-500-plus-450-reaches-900",
        "4-io-wait-in-most-samples",
        "db-pool-at-36-of-40",
        "5-boundary-24-of-30-fires",
        "5-boundary-23-of-30-does-not",
        "6-cpu0-steal-and-foreign-over-the-limit",
        "6b-cpu1-steal-and-foreign-over-the-limit",
        "6b-cpu1-steal-and-foreign-at-the-limit",
        "not-failed-on-latency",
    ],
)
def test_what_stopped_it_is_computed(changes, expected):
    """V29's fixtures, on the override's split. 24 of 30 is 1000 x 24 >= 800 x
    30; 23 of 30 is not. A comparison written ``>`` or a share of 750 or 850
    moves one of the two. A CPU group is a hardware thread's pinned containers:
    PostgreSQL at 500 with nginx and Redis at 450 is cpu1 at 950, which no
    single container reaches. Steal plus foreign CPU over F on cpu0 or on cpu1
    (6, 6b: 60 + 50) is not ``serialised``; exactly F (60 + 40) still is."""
    method, step = _failing(**changes)
    assert se.attribute(method, step, fx.SPLIT) == expected


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"postgres_cpu_permille": 930}, "cpu:postgres"),
        (
            {"web_cpu_permille": 500, "redis_cpu_permille": 410},
            "cpu:web+redis+pg_sampler",
        ),
        ({"cpu1_steal_permille": 60, "cpu1_busy_permille": 360}, "none-at-limit"),
    ],
    ids=["postgres-alone-on-cpu1", "web-redis-sampler-on-cpu2", "cpu1-starved"],
)
def test_the_groups_follow_whatever_split_was_recorded(changes, expected):
    """The first layout (web, Redis and the sampler on cpu2) gives other groups:
    nothing about which container shares a thread is written in the code."""
    method, step = _failing(fx.OLD_SPLIT, **changes)
    assert se.attribute(method, step, fx.OLD_SPLIT) == expected


def test_an_invalid_first_failing_step_gives_invalid_never_serialised():
    method, step = _failing(generator_cpu_permille=950, cpu3_busy_permille=960)
    assert step["valid"] is False
    assert se.attribute(method, step, fx.SPLIT) == "invalid"


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
    assert (
        "the API kept its hardware thread, cpu0, busy at least 90% of the time" in hot
    )
    assert se.TRACKING_ISSUE not in hot
    assert "#823" not in hot


# ---------------------------------------------------------------------------
# The schedulers' ticks during a step: recorded as data, never judged
# ---------------------------------------------------------------------------
#: An API that has been up a while (fx.tick_lines' cumulative state).
UP_A_WHILE = {
    "safety": {"success": 3, "seconds": "2.85"},
    "metrics": {"success": 1, "seconds": "3.7"},
    "experiment": {"success": 1, "seconds": "0.004"},
}
#: Every scheduler ticking every way between two scrapes.
BUSY = {
    name: {"success": 3, "partial": 2, "failed": 1, "skipped": 4, "seconds": "123.456"}
    for name in se.SCHEDULERS
}
TICK_FIELDS = ("scheduler_metrics", "scheduler_ticks")
JUDGEMENT = ("valid", "invalid_reason", "keeps_up", "fail_reason")


def _judgement(record):
    return {key: record[key] for key in JUDGEMENT}


def test_a_tick_between_the_two_scrapes_is_counted_under_its_scheduler_and_outcome():
    after = {
        "safety": {"success": 4, "seconds": "3.80123456789"},
        "metrics": {"success": 1, "failed": 1, "seconds": "5.15"},
        "experiment": {"success": 1, "skipped": 1, "seconds": "0.004"},
        "rollout": {"success": 1, "seconds": "0.0061"},
    }
    exported, ticks = se.scheduler_ticks(
        fx.tick_lines(UP_A_WHILE), fx.tick_lines(after)
    )
    assert exported == "present"
    assert ticks == fx.ticks(
        # 0.95123456789 s, to the microsecond
        safety={"success": 1, "duration_count": 1, "duration_sum_us": 951_235},
        metrics={"failed": 1, "duration_count": 1, "duration_sum_us": 1_450_000},
        # a skipped tick is counted and not timed
        experiment={"skipped": 1},
        # a scheduler whose first tick ends in the step counts from 0
        rollout={"success": 1, "duration_count": 1, "duration_sum_us": 6100},
    )


@pytest.mark.parametrize("gap", [False, True], ids=["in-step-2", "between-2-and-3"])
def test_a_planted_tick_lands_in_the_step_whose_scrapes_it_ends_between(gap):
    """Three steps from six cumulative scrapes, each step's before and after. A
    safety tick that ends between step 2's two scrapes is in step 2 and no
    other; the same tick ending after step 2's after-scrape and before step 3's
    before-scrape is in no step."""
    ticked = {**UP_A_WHILE, "safety": {"success": 4, "seconds": "3.8"}}
    first = 4 if gap else 3  # the scrape the tick first shows in
    scrapes = [
        fx.metrics(tick_state=ticked if index >= first else UP_A_WHILE)
        for index in range(6)
    ]
    records = [
        summarise(
            "track",
            number,
            metrics_before=scrapes[2 * number - 2],
            metrics_after=scrapes[2 * number - 1],
        )[0]
        for number in (1, 2, 3)
    ]
    one = fx.ticks(
        safety={"success": 1, "duration_count": 1, "duration_sum_us": 950_000}
    )
    expected = [fx.ticks(), fx.ticks() if gap else one, fx.ticks()]
    assert [record["scheduler_ticks"] for record in records] == expected
    assert {record["scheduler_metrics"] for record in records} == {"present"}


def test_an_api_without_the_tick_metrics_is_recorded_as_absent_never_as_zeros():
    """An image from before the two families: "absent", naming no scheduler. A
    fresh API that exports them with no sample yet is "present" with every
    scheduler at zero. Both records pass the closed schema and agree."""
    absent, _ = summarise()  # fx.metrics() exports no tick family
    assert (absent["scheduler_metrics"], absent["scheduler_ticks"]) == ("absent", {})
    fresh, _ = summarise(
        metrics_before=fx.metrics(tick_state={}),
        metrics_after=fx.metrics(tick_state={}),
    )
    assert (fresh["scheduler_metrics"], fresh["scheduler_ticks"]) == (
        "present",
        fx.ticks(),
    )
    for record in (absent, fresh):
        problems = []
        se.STEP_SPEC.check(record, "step", problems)
        assert problems == []
        assert se.scheduler_problem(record) == ""


@pytest.mark.parametrize(
    "before, after, named",
    [
        (
            fx.tick_lines(UP_A_WHILE),
            "",
            "the scheduler tick metrics are not in both scrapes: before the step "
            "scheduler_tick_duration_seconds, scheduler_ticks_total, after it neither",
        ),
        (
            "",
            'scheduler_ticks_total{name="safety",status="success"} 1.0\n',
            "scheduler_ticks_total before its family's # TYPE line",
        ),
        (
            fx.tick_lines({"warehouse": {"success": 1, "seconds": "0.1"}}),
            fx.tick_lines(UP_A_WHILE),
            "a tick of a scheduler named 'warehouse', not one of bandit, experiment",
        ),
        (
            "# TYPE scheduler_ticks_total counter\n"
            'scheduler_ticks_total{name="safety",status="timeout"} 1.0\n',
            fx.tick_lines(UP_A_WHILE),
            "a tick whose outcome is 'timeout'",
        ),
        (
            "# TYPE scheduler_ticks_total counter\n"
            'scheduler_ticks_total{name="safety"} 1.0\n',
            fx.tick_lines(UP_A_WHILE),
            "scheduler_ticks_total has no status label",
        ),
        (
            "# TYPE scheduler_ticks_total gauge\n",
            fx.tick_lines(UP_A_WHILE),
            "scheduler_ticks_total is a gauge, not a counter",
        ),
        (
            fx.tick_lines({"safety": {"success": 4, "seconds": "3.8"}}),
            fx.tick_lines(UP_A_WHILE),
            'scheduler_ticks_total{name="safety",status="success"} went from 4.0 '
            "to 3.0; a counter only grows",
        ),
        (
            fx.tick_lines({"safety": {"success": 3, "seconds": "3.0"}}),
            fx.tick_lines({"safety": {"success": 3, "seconds": "2.0"}}),
            "a sum of durations only grows",
        ),
    ],
    ids=[
        "one-scrape-only",
        "no-type-line",
        "unknown-scheduler",
        "unknown-outcome",
        "no-status-label",
        "not-a-counter",
        "counter-went-down",
        "sum-went-down",
    ],
)
def test_tick_metrics_that_cannot_be_read_as_one_step_are_refused(before, after, named):
    assert named in refused(se.scheduler_ticks, before, after)


def test_the_api_s_own_exposition_reads_back_as_the_ticks_it_recorded(monkeypatch):
    """The API's recording function and prometheus_client's own text, on a
    registry of this test's (the process-wide one holds other tests' made-up
    scheduler names): the families' names, labels and layout read back as
    recorded, and their # TYPE lines are there before anything ticks."""
    from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

    from backend.app.core import metrics as prom

    registry = CollectorRegistry()
    real = prom.scheduler_ticks_total
    monkeypatch.setattr(
        prom,
        "scheduler_ticks_total",
        Counter(real._name, real._documentation, real._labelnames, registry=registry),
    )
    real = prom.scheduler_tick_duration_seconds
    monkeypatch.setattr(
        prom,
        "scheduler_tick_duration_seconds",
        Histogram(
            real._name,
            real._documentation,
            real._labelnames,
            registry=registry,
            buckets=real._upper_bounds,
        ),
    )
    before = generate_latest(registry).decode("utf-8")
    prom.record_scheduler_tick("safety", "success", 0.95)
    prom.record_scheduler_tick("metrics", "partial", 1.5)
    prom.record_scheduler_tick("experiment", "skipped", 0.002)
    after = generate_latest(registry).decode("utf-8")
    assert se.scheduler_ticks(before, after) == (
        "present",
        fx.ticks(
            safety={"success": 1, "duration_count": 1, "duration_sum_us": 950_000},
            metrics={"partial": 1, "duration_count": 1, "duration_sum_us": 1_500_000},
            experiment={"skipped": 1},
        ),
    )
    assert se.scheduler_ticks(before, before) == ("present", fx.ticks())


def test_the_scheduler_names_outcomes_and_families_are_the_api_s():
    """SCHEDULERS is every name the code ticks under (each run_locked_tick call
    passes its module's SCHEDULER_NAME), TICK_STATUSES every outcome it
    records, and the two families are declared with the labels read here."""
    names, calls = set(), 0
    for root in (REPO_ROOT / "backend" / "app", REPO_ROOT / "modules" / "backend"):
        for path in sorted(root.rglob("*.py")) if root.is_dir() else []:
            text = path.read_text("utf-8")
            names |= set(re.findall(r'^SCHEDULER_NAME = "([a-z_]+)"$', text, re.M))
            for call in re.finditer(r"(?<!def )run_locked_tick\(\s*(\w+)", text):
                calls += 1
                assert call.group(1) == "SCHEDULER_NAME", path
    assert names == set(se.SCHEDULERS)
    assert calls >= len(se.SCHEDULERS)
    core = REPO_ROOT / "backend" / "app" / "core"
    tick = (core / "scheduler_tick.py").read_text("utf-8")
    outcomes = set(re.findall(r'^STATUS_[A-Z]+ = "([a-z]+)"$', tick, re.M))
    assert outcomes == set(se.TICK_STATUSES)
    metrics = (core / "metrics.py").read_text("utf-8")
    assert re.search(
        r'Counter\(\s*"scheduler_ticks_total",\s*"[^"]*",\s*\["name", "status"\],',
        metrics,
    )
    assert re.search(
        r'Histogram\(\s*"scheduler_tick_duration_seconds",\s*"[^"]*",\s*\["name"\],',
        metrics,
    )


@pytest.mark.parametrize(
    "situation, expected",
    [
        ({}, (True, "none", True, "none")),
        ({"latency_us": 1_500_000}, (True, "none", False, "p99")),
        ({"dropped": 37}, (True, "none", False, "dropped")),
        ({"generator": 960}, (False, "generator-cpu", False, "invalid")),
    ],
    ids=["healthy", "p99", "dropped", "generator-cpu"],
)
def test_the_tick_fields_change_no_step_judgement(situation, expected):
    """The same rows and observation summarised with no tick metrics, with
    idle schedulers, and with every scheduler ticking every way between the
    scrapes: the same VALID, KEEPS UP, reasons and messages, and every other
    field equal. Only the two tick fields differ."""
    method = se.load_config()
    rows = fx.step_rows(
        method,
        "track",
        4,
        dropped=situation.get("dropped", 0),
        latency_us=situation.get("latency_us", 4000),
    )
    observed = {"dropped": situation.get("dropped", 0)}
    if "generator" in situation:
        cpu = fx.observation(method, "track", 4)["cpu_permille"]
        observed["cpu_permille"] = {**cpu, "generator": situation["generator"]}
    scrapes = {
        "none": (fx.metrics(), fx.metrics()),
        "idle": (fx.metrics(tick_state={}), fx.metrics(tick_state={})),
        "busy": (fx.metrics(tick_state={}), fx.metrics(tick_state=BUSY)),
    }
    judged = {
        name: summarise(
            "track",
            4,
            rows=rows,
            metrics_before=before,
            metrics_after=after,
            **observed,
        )
        for name, (before, after) in scrapes.items()
    }
    base, said = judged["none"]
    assert tuple(_judgement(base).values()) == expected
    others = {key: value for key, value in base.items() if key not in TICK_FIELDS}
    for name, (record, messages) in judged.items():
        assert (_judgement(record), messages) == (_judgement(base), said), name
        assert {k: v for k, v in record.items() if k not in TICK_FIELDS} == others
    assert judged["busy"][0]["scheduler_ticks"]["safety"]["success"] == 3


def test_the_tick_fields_change_no_knee_bottleneck_or_summary():
    """Three runs (failing on latency; on drops then errors; on an invalid
    step) re-judged from their facts with no tick fields, with "absent", and
    with every scheduler ticking every way in every step: the same step
    judgements, knees, first failing steps and reasons, validity, bottlenecks
    and scenario summary."""
    method = se.load_config()
    invalid = fx.rep_steps(method, "track", 4, ["p99"])
    invalid[4] = fx.step_facts(method, "track", 5, generator_cpu_permille=950)
    runs = [
        fx.rep_steps(method, "track", 4, ["p99", "p99"]),
        fx.rep_steps(method, "track", 3, ["dropped", "errors"]),
        invalid,
    ]
    busy = fx.ticks(
        **{
            name: {
                "success": 3,
                "partial": 2,
                "failed": 1,
                "skipped": 4,
                "duration_count": 6,
                "duration_sum_us": 123_456_000,
            }
            for name in se.SCHEDULERS
        }
    )
    variants = {
        "removed": {},
        "absent": {"scheduler_metrics": "absent", "scheduler_ticks": {}},
        "busy": {"scheduler_metrics": "present", "scheduler_ticks": busy},
    }
    outcomes = {}
    for how, fields in variants.items():
        reps, said = [], []
        for number, steps in enumerate(runs, 1):
            judged = []
            for step in steps:
                facts = {
                    key: value
                    for key, value in step.items()
                    if key not in TICK_FIELDS + JUDGEMENT
                }
                facts.update(copy.deepcopy(fields))
                verdict, messages = se.judge_step(method, "track", facts, fx.SPLIT)
                judged.append({**facts, **verdict})
                said.append(messages)
            reps.append(fx.rep(method, "track", number, judged))
        outcomes[how] = (
            [[_judgement(step) for step in rep["steps"]] for rep in reps],
            [
                {key: rep[key] for key in se.assess_rep(method, rep["steps"], fx.SPLIT)}
                for rep in reps
            ],
            se.summarise_scenario(method, reps),
            said,
        )
    assert outcomes["absent"] == outcomes["removed"]
    assert outcomes["busy"] == outcomes["removed"]
    assessed = outcomes["removed"][1]
    assert [rep["knee_step"] for rep in assessed] == [4, 3, 4]
    assert [rep["first_failing_reason"] for rep in assessed] == [
        "p99",
        "dropped",
        "invalid",
    ]


def test_the_summariser_s_tick_fields_are_declared_in_the_closed_step_schema():
    """The summariser's own record, every scheduler ticking, passes STEP_SPEC
    with nothing left over; a field the schema does not declare is refused."""
    record, _ = summarise(
        metrics_before=fx.metrics(tick_state={}),
        metrics_after=fx.metrics(tick_state=BUSY),
    )
    problems = []
    se.STEP_SPEC.check(record, "step", problems)
    assert problems == []
    record["scheduler_ticks_by_minute"] = {}
    se.STEP_SPEC.check(record, "step", problems)
    assert problems == ["step.scheduler_ticks_by_minute: unknown key"]


@pytest.mark.parametrize(
    "edit, named",
    [
        (
            lambda step: step["scheduler_ticks"].update(warehouse=fx.ticks()["safety"]),
            "steps[0].scheduler_ticks.warehouse: unknown key",
        ),
        (
            lambda step: step["scheduler_ticks"]["safety"].update(timed_out=0),
            "steps[0].scheduler_ticks.safety.timed_out: unknown key",
        ),
        (
            lambda step: step["scheduler_ticks"]["safety"].pop("skipped"),
            "steps[0].scheduler_ticks.safety.skipped: missing key",
        ),
        (
            lambda step: step.pop("scheduler_metrics"),
            "steps[0].scheduler_metrics: missing key",
        ),
        (
            lambda step: step.update(scheduler_metrics="partly"),
            "steps[0].scheduler_metrics: must be one of ['present', 'absent']",
        ),
        (
            lambda step: step.update(scheduler_metrics="absent"),
            "evaluate_get run 1 step 1: scheduler_ticks names bandit, experiment, "
            "metrics, rollout, safety with scheduler_metrics absent",
        ),
        (
            lambda step: step["scheduler_ticks"].pop("bandit"),
            "evaluate_get run 1 step 1: scheduler_ticks has no bandit with "
            "scheduler_metrics present",
        ),
    ],
    ids=[
        "unknown-scheduler",
        "unknown-field",
        "missing-field",
        "no-presence",
        "unknown-presence",
        "absent-with-schedulers",
        "present-missing-one",
    ],
)
def test_a_results_file_s_tick_fields_are_closed_and_agree(edit, named):
    results = fx.small()
    edit(results["scenarios"][0]["reps"][0]["steps"][0])
    assert named in refused(se.load_results, json.dumps(results))


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


@pytest.mark.regression
def test_runs_off_the_method_s_rate_series_are_refused():
    """One request type's runs built from 4,000 milli-rps instead of the
    method's 5,000: every step consistent with itself, so only a comparison
    with the method's series catches it (the page would still say the first
    step offered 5.00)."""
    meth = fx.method()
    other = copy.deepcopy(meth)
    other["start_milli_rps"]["track"] = 4000
    reps = {
        name: [
            fx.rep(
                meth,
                name,
                n,
                fx.rep_steps(other if name == "track" else meth, name, 4, ["p99"] * 2),
            )
            for n in (1, 2, 3)
        ]
        for name in se.SCENARIOS
    }
    results = fx.results(meth, reps)
    problems = se.recompute_problems(results)
    assert problems[:1] == [
        "track run 1 step 1: ran at 4000 milli-rps; the method's step 1 is 5000"
    ]
    assert all(problem.startswith("track run ") for problem in problems)
    assert len(problems) == 3 * 6
    assert "the method's step 1 is 5000" in refused(se.load_results, se.dumps(results))


def test_the_siblings_and_the_split_are_consistent_records():
    results = fx.small()
    results["scenarios"][0]["reps"][1]["host"]["thread_siblings"]["cpu3"] = "3"
    problems = se.results_problems(results)
    assert (
        "evaluate_get run 2: thread_siblings.cpu2 is '2-3' but cpu3's is '3'"
        in problems
    )
    results = fx.small()
    results["scenarios"][0]["reps"][0]["host"]["generator_core_exclusive"] = True
    assert se.results_problems(results) == [
        "evaluate_get run 1: generator_core_exclusive disagrees with the load "
        "generator's siblings cpu2 and cpu3"
    ]
    results = fx.small()
    results["system_under_test"]["cpu_split"]["api"] = "0-1"
    assert se.results_problems(results) == [
        "system_under_test.cpu_split.api is '0-1': each container is pinned to one "
        "hardware thread, so that its CPU is that thread's"
    ]


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
            lambda r: r["system_under_test"]["cpu_split"].update(generator="2"),
            "the generator shares cpu [2] with pg_sampler",
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
        "generator-shares-the-sampler-s-thread",
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


@pytest.mark.parametrize(
    "split, siblings, named",
    [
        (
            fx.OLD_SPLIT,
            fx.SMT,
            [
                "run 1: nginx ran on cpu2, a hardware thread of the load generator's "
                "core (cpu2 and cpu3, as the VM reported it): refusing to publish",
                "run 1: Redis ran on cpu2",
            ],
        ),
        (
            fx.SPLIT,
            fx.INTERLEAVED,
            [
                "run 1: PostgreSQL ran on cpu1, a hardware thread of the load "
                "generator's core (cpu1 and cpu3, as the VM reported it)",
                "run 1: nginx ran on cpu1",
                "run 1: Redis ran on cpu1",
            ],
        ),
        (fx.SPLIT, fx.SMT, []),
        (fx.SPLIT, fx.NO_SMT, []),
    ],
    ids=[
        "old-split-siblings-2-3-refused",
        "new-split-interleaved-siblings-refused",
        "new-split-siblings-0-1-and-2-3-accepted",
        "new-split-no-smt-accepted",
    ],
)
def test_nothing_under_test_shares_the_load_generator_s_physical_core(
    split, siblings, named
):
    """The generator's recorded cpuset expanded by each rep's thread siblings:
    the logically disjoint first layout put nginx and Redis on k6's core."""
    results = fx.small(split=split, siblings=siblings)
    assert se.results_problems(results) == []
    problems = se.publication_problems(results)
    if not named:
        assert problems == []
        assert se.render_block(results, LINK)
    for want in named:
        assert any(want in problem for problem in problems), problems
    for problem in problems:
        assert "refusing to publish" in problem


def test_the_isolation_check_runs_per_rep():
    """One rep whose VM paired its threads differently is named, and only it."""
    results = fx.small()
    rep = results["scenarios"][4]["reps"][2]
    rep["host"] = fx.host(3, fx.INTERLEAVED)
    problems = se.publication_problems(results)
    assert problems == [
        f"POST /api/v1/tracking/batch (100 events each) run 3: {name} ran on cpu1, "
        "a hardware thread of the load generator's core (cpu1 and cpu3, as the VM "
        "reported it): refusing to publish"
        for name in ("PostgreSQL", "nginx", "Redis")
    ]


def test_a_topology_beyond_the_four_recorded_threads_is_not_published():
    results = fx.small()
    results["scenarios"][0]["reps"][0]["host"]["nproc"] = 8
    assert (
        "GET /api/v1/feature-flags/evaluate/{key} run 1: the VM reports 8 vCPUs and "
        "siblings cpu0, cpu1, cpu2 and cpu3; the record holds the topology of cpu0 "
        "to cpu3 only" in se.publication_problems(results)
    )


def test_more_runs_than_the_method_s_are_not_published():
    """The page says "Over 3 runs" and states the median run's knee."""
    meth = fx.method()
    reps = [
        fx.rep(meth, "track", n, fx.rep_steps(meth, "track", 4, ["p99", "p99"]))
        for n in (1, 2, 3, 4)
    ]
    summary = se.summarise_scenario(meth, reps)
    assert summary["not_publishable"] == ["reps"]
    results = fx.small()
    results["scenarios"][3]["reps"] = reps
    results["scenarios"][3]["summary"] = summary
    assert (
        "POST /api/v1/tracking/track: 4 runs; the method says 3"
        in se.publication_problems(results)
    )


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


@pytest.mark.regression
def test_the_page_says_what_kept_up_with_and_its_columns_are():
    """F1: the old sentence said the figures were the median run's; the
    percentiles are each a median over the runs at that step, and the errors
    are those runs added up (0 of 1,797 is 599 in each of 3)."""
    block = se.render_block(fx.small(), LINK)
    assert "median run's" not in block
    assert (
        "Kept up with is the median of the 3 runs' knees, a run's knee being the "
        "last rate it kept up with before its first failing step. Beside it, each "
        "percentile is the median of the runs that reached that step, and Errors "
        "adds up those runs' failed and measured requests." in block
    )


@pytest.mark.regression
def test_a_step_that_dropped_requests_says_the_server_did_not_keep_up():
    """F2: "requests it could not send" read as the load generator failing."""
    meth = fx.method()
    reps = {
        name: [
            fx.rep(meth, name, n, fx.rep_steps(meth, name, 4, ["dropped", "dropped"]))
            for n in (1, 2, 3)
        ]
        for name in se.SCENARIOS
    }
    block = se.render_block(fx.results(meth, reps), LINK)
    row = next(
        line for line in block.split("\n") if line.startswith("| POST /api/v1/t")
    )
    assert "| the server did not keep up: scheduled requests could not be sent |" in row
    assert "could not send" not in block


NEW_LAYOUT_ROWS = [
    "| CPU split, as each container's cpuset was recorded | the API on cpu0; "
    "PostgreSQL, nginx and Redis on cpu1; the database sampler on cpu2; the load "
    "generator on cpu3 |",
    "| Hardware threads in 18 of 18 runs | 4 vCPUs, which the VM reports as 2 cores "
    "of 2 hardware threads: the core of cpu0 and cpu1 had pinned to it the API on "
    "cpu0 and PostgreSQL, nginx and Redis on cpu1; the core of cpu2 and cpu3 had "
    "pinned to it the database sampler on cpu2 and the load generator on cpu3 |",
]


def test_the_setup_names_what_ran_on_each_physical_core():
    """The new layout's render names PostgreSQL, nginx and Redis on the API's
    physical core, from the recorded split and siblings."""
    block = se.render_block(se.load_results(fx.FULL_SIZE.read_text("utf-8")), LINK)
    for row in NEW_LAYOUT_ROWS:
        assert row in block.split("\n")


def test_the_page_never_calls_a_hardware_thread_a_core():
    """The word core appears only where the VM's physical cores are described; nothing
    says alone, dedicated or full core, and nothing equates a thread with a
    Fargate task."""
    results = se.load_results(fx.FULL_SIZE.read_text("utf-8"))
    block = se.render_block(results, LINK)
    models = {r["host"]["cpu_model"] for s in results["scenarios"] for r in s["reps"]}
    for line in block.split("\n"):
        # "core profile" is the release's profile, and the CPU model is the
        # vendor's recorded name: neither is the page's wording.
        words = line.replace("core profile", "")
        for model in models:
            words = words.replace(model, "")
        if "core" in words.lower():
            assert line.startswith("| Hardware threads in "), line
    lowered = block.lower()
    for phrase in ("alone on", "dedicated", "full core", "fargate", "its core"):
        assert phrase not in lowered, phrase
    assert lowered.count("vcpu") == 1


def test_each_service_s_thread_is_rendered_from_the_split():
    """F5: the row used to print web's CPU for nginx, Redis and the samplers."""
    split = {**fx.SPLIT, "redis": "0"}
    block = se.render_block(fx.small(split=split), LINK)
    assert (
        "| CPU split, as each container's cpuset was recorded | the API and Redis on "
        "cpu0; PostgreSQL and nginx on cpu1; the database sampler on cpu2; the load "
        "generator on cpu3 |" in block
    )
    assert (
        "had pinned to it the API and Redis on cpu0 and PostgreSQL and nginx on cpu1"
        in block
    )


def test_each_distinct_topology_is_rendered_with_its_count():
    """Per rep, not a union across hosts: one rep on a VM without SMT."""
    results = fx.small()
    results["scenarios"][0]["reps"][0]["host"] = fx.host(1, fx.NO_SMT)
    assert se.results_problems(results) == []
    block = se.render_block(results, LINK)
    rows = [line for line in block.split("\n") if line.startswith("| Hardware")]
    assert rows == [
        NEW_LAYOUT_ROWS[1].replace("18 of 18", "17 of 18"),
        "| Hardware threads in 1 of 18 runs | 4 vCPUs, which the VM reports as 4 "
        "cores of one hardware thread each: the core of cpu0 had pinned to it the "
        "API on cpu0; the core of cpu1 had pinned to it PostgreSQL, nginx and Redis "
        "on cpu1; the core of cpu2 had pinned to it the database sampler on cpu2; "
        "the core of cpu3 had pinned to it the load generator on cpu3 |",
    ]


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


@pytest.mark.regression
def test_check_page_names_a_stale_entry_the_mechanism_holds():
    """F7: an extra entry held by the mechanism, for text no longer in the
    block, used to pass check (only doc_claims named it)."""
    results, page, registry = _rendered()
    entries = tomllib.loads(registry)["unit"]
    stale = {"text": "It served 5,000 requests a second.", "by": [se.MECHANISM]}
    entries.insert(3, stale)
    planted = se._doc_claims().registry_text("capacity.md", entries)
    assert se.check_page(results, page, planted, LINK) == [
        "registry entry 'It served 5,000 requests a second.' is held by "
        f"{se.MECHANISM} but is not a unit of the block"
    ]
    other = {"text": "It served 5,000 requests a second.", "by": ["exempt:advice"]}
    entries[3] = other
    unheld = se._doc_claims().registry_text("capacity.md", entries)
    assert se.check_page(results, page, unheld, LINK) == []


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


def test_the_full_size_fixture_is_on_the_measured_topology_and_split():
    """The probe's runner: siblings 0-1, 0-1, 2-3, 2-3 in every rep; the
    override's split (#1614): the API on cpu0, PostgreSQL, nginx and Redis on
    cpu1, the sampler on cpu2, k6 on cpu3."""
    results = se.load_results(fx.FULL_SIZE.read_text("utf-8"))
    assert results["system_under_test"]["cpu_split"] == {
        "api": "0",
        "postgres": "1",
        "web": "1",
        "redis": "1",
        "pg_sampler": "2",
        "generator": "3",
    }
    for scenario in results["scenarios"]:
        for rep in scenario["reps"]:
            assert rep["host"]["thread_siblings"] == {
                "cpu0": "0-1",
                "cpu1": "0-1",
                "cpu2": "2-3",
                "cpu3": "2-3",
            }
            assert rep["host"]["generator_core_exclusive"] is False


def test_the_fixture_split_is_the_override_s():
    """The fixtures' system-under-test cpusets are the ones the harness's
    override applies, so the renders above show the layout that is measured."""
    import yaml

    override = yaml.safe_load((SCALE_DIR / "compose.scale.yml").read_text("utf-8"))
    services = override["services"]
    applied = {name: str(service["cpuset"]) for name, service in services.items()}
    assert applied == {name: fx.SPLIT[name] for name in se.SYSTEM_UNDER_TEST}


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
def _gh(head_sha, artifact, event="workflow_dispatch", branch="main"):
    def run(argv):
        if argv[:2] == ["run", "view"]:
            assert argv[argv.index("--json") + 1] == "headSha,event,headBranch"
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({"headSha": head_sha, "event": event, "headBranch": branch}),
                "",
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


@pytest.mark.parametrize(
    "event, branch, words",
    [
        ("workflow_dispatch", "ci/tampered-harness", "branch ci/tampered-harness"),
        ("pull_request", "main", "event pull_request"),
    ],
    ids=["another-branch", "not-a-dispatch"],
)
def test_verify_refuses_a_run_not_dispatched_on_main(tmp_path, event, branch, words):
    """N2: a measure dispatch from any branch, edited harness included, makes an
    artifact with the same name; only a dispatch on main is publishable."""
    results, path = _committed(tmp_path)
    runner = _gh(results["run"]["harness_commit"], path.read_bytes(), event, branch)
    problems = se.verify(path, 40999123456, "Controlvar/controlvar", runner)
    assert len(problems) == 1 and words in problems[0], problems


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


#: Every JavaScript file under tests/scale: the one k6 entry script, and the
#: library it imports, which is no entry script (a new file fails here until
#: it is classified).
K6_ENTRY_SCRIPTS = ["k6/scenarios.js"]
K6_LIBRARIES = ["k6/lib.js"]


def test_every_k6_script_under_tests_scale_keeps_the_url_out():
    """The entry script is held to the rule above. The library sets no k6
    options and imports nothing, so it cannot change which tags k6 writes."""
    found = sorted(p.relative_to(SCALE_DIR).as_posix() for p in SCALE_DIR.rglob("*.js"))
    assert found == sorted(K6_ENTRY_SCRIPTS + K6_LIBRARIES)
    for name in K6_ENTRY_SCRIPTS:
        script = (SCALE_DIR / name).read_text("utf-8")
        assert se.system_tags_problems(script) == [], name
    for name in K6_LIBRARIES:
        text = (SCALE_DIR / name).read_text("utf-8")
        assert not re.search(
            r"^\s*import\b|\brequire\s*\(|\boptions\b|\bsystemTags\b", text, re.M
        ), name


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


def _imports(path: Path) -> set:
    """The top-level packages a file imports (absolute imports only)."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


@pytest.mark.parametrize(
    "path",
    [SCALE_DIR / "scale_evidence.py", REPO_ROOT / "scripts" / "scale_stack.py"],
    ids=["scale_evidence", "scale_stack"],
)
def test_the_summariser_and_the_harness_it_reads_are_standard_library_only(path):
    """render and check run where nothing is installed, and they load
    scripts/scale_stack.py for the flag design: it must stay standard library
    only too."""
    assert _imports(path) - set(sys.stdlib_module_names) == set()


def test_the_flag_design_is_the_harness_s_own_never_a_copy():
    """One source: the rule, its context and the flag counts are the objects
    scripts/scale_stack.py creates the fixtures from."""
    stack = se.stack()
    assert se.RULE_FLAG_RULES is stack.RULE
    assert se.RULE_FLAG_CONTEXT is stack.RULE_CONTEXT
    payloads = stack.flag_payloads()
    assert se.FIXTURE_FLAGS == len(payloads) == 100
    assert se.FIXTURE_RULE_FLAGS == stack.RULE_FLAG_COUNT == 50
    assert [p["targeting_rules"] for p in payloads[:50]] == [stack.RULE] * 50


# ---------------------------------------------------------------------------
# The evaluate body check, pinned against the real evaluation (EMS-5)
# ---------------------------------------------------------------------------
def test_the_expected_evaluate_reasons_are_what_the_evaluation_answers():
    """A rule flag sent the scenario's matching context, and a plain flag sent
    none, through the real route, targeting adapter and flag service. The two
    flags are the harness's own payloads (scripts/scale_stack.py, the code
    that creates the fixtures). Only the flag lookup and the API key are served
    from memory."""
    from fastapi.testclient import TestClient

    from backend.app.api import deps
    from backend.app.main import app
    from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus

    stack = se.stack()
    payloads = stack.flag_payloads()
    rule_flag = next(p for p in payloads if "targeting_rules" in p)
    plain_flag = next(p for p in payloads if "targeting_rules" not in p)

    def flag(payload):
        assert payload["is_active"] is True
        return FeatureFlag(
            id=uuid.uuid4(),
            key=payload["key"],
            name=payload["name"],
            status=FeatureFlagStatus.ACTIVE.value,
            owner_id=uuid.uuid4(),
            rollout_percentage=payload["rollout_percentage"],
            targeting_rules=copy.deepcopy(payload.get("targeting_rules")),
            value_type="boolean",
        )

    flags = {payload["key"]: flag(payload) for payload in (rule_flag, plain_flag)}

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
        context = json.dumps(stack.RULE_CONTEXT)
        answers = {}
        for user in ("user-00001", "user-00002", "user-00003", "user-00004"):
            rule = client.get(
                f"/api/v1/feature-flags/evaluate/{rule_flag['key']}",
                params={"user_id": user, "context": context},
                headers={"X-API-Key": "in-memory"},
            )
            plain = client.get(
                f"/api/v1/feature-flags/evaluate/{plain_flag['key']}",
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
