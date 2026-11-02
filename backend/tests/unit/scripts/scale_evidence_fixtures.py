# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""Synthetic inputs for tests/scale/scale_evidence.py's tests.

Everything here is made up and deterministic (no clock, no randomness): a
healthy step's rows and observation, two /metrics expositions, a run's steps
built through the summariser's own rules, and the full-size results file the
Leak Guard proof rests on (EMS-15).

``python -m backend.tests.unit.scripts.scale_evidence_fixtures`` rewrites the
committed full-size fixture from ``full_size()``; a test holds the committed
file equal to it, so the file is reviewable as code and cannot drift.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[4]
MODULE_PATH = REPO_ROOT / "tests" / "scale" / "scale_evidence.py"
FIXTURES = Path(__file__).resolve().parent / "scale_fixtures"
FULL_SIZE = FIXTURES / "results_full.json"
VARIANTS = ["control", "treatment"]


def module() -> Any:
    """tests/scale/scale_evidence.py, loaded by path (it is not a package)."""
    name = "scale_evidence"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[name] = loaded
        spec.loader.exec_module(loaded)
    return sys.modules[name]


def method(**changes: Any) -> Dict[str, Any]:
    """The committed method file, with top-level keys replaced."""
    loaded = module().load_config()
    loaded.update(changes)
    return loaded


def digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def sha(label: str) -> str:
    return hashlib.sha1(label.encode()).hexdigest()


# ---------------------------------------------------------------------------
# One step's raw inputs
# ---------------------------------------------------------------------------
def body(scenario: str, index: int) -> Dict[str, str]:
    """The body facts of a request that did its work."""
    facts = dict.fromkeys(module().ROW_FIELDS, "")
    if scenario == "evaluate_get":
        rule = index % 2 == 0
        facts["flag_class"] = "rule" if rule else "plain"
        facts["reason"] = "targeting_rule" if rule else "rollout"
    elif scenario == "assign":
        facts.update(assigned="1", reason="assigned", variant=VARIANTS[index % 2])
    elif scenario == "assign_returning":
        facts.update(
            assigned="1",
            reason="assigned",
            user_index=str(index),
            variant=VARIANTS[index % 2],
        )
    elif scenario == "track":
        facts.update(id_present="1", variant_id_present="1")
    elif scenario == "batch":
        facts.update(success_count="100", failure_count="0")
    elif scenario == "ruleset":
        facts["flag_count"] = "100"
    return facts


def row(
    scenario: str,
    iteration: int,
    *,
    status: int = 200,
    latency_us: int = 4000,
    start_lag_us: int = 1000,
    rate_limit: str = "10000000",
    error: str = "",
    **facts: str,
) -> Dict[str, str]:
    se = module()
    out = body(scenario, iteration)
    out.update(
        scenario=scenario,
        endpoint=se.ENDPOINTS[scenario],
        iteration=str(iteration),
        status=str(0 if error else status),
        error=error,
        latency_us=str(latency_us),
        start_lag_us=str(start_lag_us),
        rate_limit="" if error else rate_limit,
    )
    out.update(facts)
    return out


def csv_text(rows: Sequence[Dict[str, str]]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=module().ROW_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def step_rows(
    meth: Dict[str, Any],
    scenario: str,
    number: int,
    *,
    dropped: int = 0,
    missing_tail: int = 0,
    latency_us: int = 4000,
) -> List[Dict[str, str]]:
    """Every scheduled request of a step answered, but for the dropped ones (no
    row and no iteration: k6 advances no counter for a drop) and a missing
    tail (rows lost after they were sent)."""
    se = module()
    rate, unit = se.rate_pair(se.step_rates(meth, scenario)[number - 1])
    window = se.ranges(meth)
    scheduled = se.scheduled_count(rate, unit, window["settle_s"] + window["measure_s"])
    count = scheduled - dropped - missing_tail
    # Latencies spread 1..2x so the percentiles are distinct.
    return [
        row(scenario, i, latency_us=latency_us + (i * 7919) % latency_us)
        for i in range(count)
    ]


def observation(
    meth: Dict[str, Any],
    scenario: str,
    number: int,
    *,
    rows: Optional[int] = None,
    **changes: Any,
) -> Dict[str, Any]:
    """A healthy step's observation: samplers in limits, the server's count
    equal to the client's."""
    se = module()
    rate, unit = se.rate_pair(se.step_rates(meth, scenario)[number - 1])
    window = se.ranges(meth)
    scheduled = se.scheduled_count(rate, unit, window["settle_s"] + window["measure_s"])
    out = {
        "scenario": scenario,
        "step": number,
        "rate": rate,
        "time_unit_ms": unit,
        "dropped": 0,
        "variants": list(VARIANTS),
        "sampled": True,
        "samples": window["measure_s"],
        "cpu_permille": {
            "api": 400,
            "postgres": 200,
            "web_redis": 150,
            "generator": 120,
        },
        "cores": {
            "cpu0": {"busy_permille": 420, "steal_permille": 5},
            "cpu3": {"busy_permille": 140, "steal_permille": 5},
        },
        "pg": {
            "active_backend_samples_by_count": [10, 15, 5],
            "io_wait_observations": 2,
        },
        "fallback_log_lines": 0,
        "server_count": scheduled if rows is None else rows,
        "server_499": 0,
        "assignments_before": 100000,
        "assignments_after": 100000,
    }
    out.update(changes)
    return out


def metrics(
    shape: str = "a", rejections: int = 0, fallbacks: int = 0, hits: int = 5
) -> str:
    """A /metrics exposition. The two shapes carry different label sets for the
    same series; read as sums they must agree."""
    if shape == "a":
        return (
            "# TYPE rate_limit_hits_total counter\n"
            f'rate_limit_hits_total{{endpoint="track"}} {hits}.0\n'
            f'rate_limit_hits_total{{endpoint="assign"}} {hits}.0\n'
            "# TYPE rate_limit_rejections_total counter\n"
            f'rate_limit_rejections_total{{endpoint="track"}} {rejections}.0\n'
            "# TYPE rate_limit_redis_fallbacks_total counter\n"
            f"rate_limit_redis_fallbacks_total {fallbacks}.0\n"
            "rate_limit_redis_fallbacks_created 1.7619e+09\n"
            "rate_limit_redis_fallback_active 0.0\n"
        )
    return (
        "# TYPE rate_limit_hits_total counter\n"
        f'rate_limit_hits_total{{endpoint="track",method="POST"}} {2 * hits}.0\n'
        "# TYPE rate_limit_rejections_total counter\n"
        f'rate_limit_rejections_total{{endpoint="track",method="POST"}} {rejections}.0\n'
        'rate_limit_rejections_total{endpoint="other",method="GET"} 0.0\n'
        "# TYPE rate_limit_redis_fallbacks_total counter\n"
        f"rate_limit_redis_fallbacks_total {fallbacks}.0\n"
        "rate_limit_redis_fallback_active 0.0\n"
    )


# ---------------------------------------------------------------------------
# Steps as facts (for runs and whole results files)
# ---------------------------------------------------------------------------
def step_facts(
    meth: Dict[str, Any], scenario: str, number: int, **changes: Any
) -> Dict[str, Any]:
    """A step record as the summariser writes it, built from facts (no rows):
    healthy unless ``changes`` say otherwise; the judgement is the rules'."""
    se = module()
    rate, unit = se.rate_pair(se.step_rates(meth, scenario)[number - 1])
    window = se.ranges(meth)
    scheduled = se.scheduled_count(rate, unit, window["settle_s"] + window["measure_s"])
    measured = scheduled - se.first_measured(rate, unit, window["settle_s"])
    step: Dict[str, Any] = {
        "step": number,
        "rate": rate,
        "time_unit_ms": unit,
        "milli_rps": 0,
        "scheduled": 0,
        "rows": scheduled,
        "dropped": 0,
        "measured": measured,
        "achieved_milli_rps": 0,
        "errors": 0,
        "timeouts": 0,
        "connection_errors": 0,
        "client_429": 0,
        "limit_mismatches": 0,
        "p50_us": 3000 + 150 * number,
        "p95_us": 9000 + 900 * number,
        "p99_us": 15000 + 2500 * number,
        "percentiles_are_lower_bounds": False,
        "start_lag_p99_us": 1000,
        "sampled": True,
        "samples": window["measure_s"],
        "api_core_permille": min(880, 120 + 45 * number),
        "postgres_core_permille": min(700, 60 + 25 * number),
        "web_redis_core_permille": min(500, 40 + 15 * number),
        "generator_cpu_permille": min(600, 30 + 20 * number),
        "cpu0_busy_permille": min(900, 130 + 45 * number),
        "cpu0_steal_permille": 4,
        "cpu0_foreign_permille": 0,
        "cpu3_busy_permille": min(640, 40 + 20 * number),
        "cpu3_steal_permille": 3,
        "cpu3_foreign_permille": 0,
        "active_backend_samples_by_count": [6, 20, 4],
        "active_backend_share_permille": 0,
        "max_active_backends": 0,
        "io_wait_observations": 1,
        "io_wait_share_permille": 0,
        "rejections_delta": 0,
        "fallback_delta": 0,
        "fallback_log_lines": 0,
        "server_count": scheduled,
        "server_499": 0,
        "assignments_delta": 0,
        "reasons": {},
        "not_modified": 0,
    }
    if scenario == "evaluate_get":
        step["reasons"] = {
            "targeting_rule": measured - measured // 2,
            "rollout": measured // 2,
        }
    if scenario == "ruleset":
        step["not_modified"] = measured - measured // 50
    step.update(changes)
    step.update(se._derived(step, meth))
    fields, _ = se.judge_step(meth, scenario, step)
    step.update(fields)
    return step


def failing_step(
    meth: Dict[str, Any], scenario: str, number: int, kind: str
) -> Dict[str, Any]:
    """A step that did not keep up, in one of the ways a real one fails."""
    if kind == "p99":
        return step_facts(
            meth,
            scenario,
            number,
            p99_us=1_000_000 + 37_000 * number,
            p95_us=600_000,
            api_core_permille=610,
            cpu0_busy_permille=630,
            active_backend_samples_by_count=[2, 27, 1],
        )
    if kind == "dropped":
        rate, unit = module().rate_pair(module().step_rates(meth, scenario)[number - 1])
        window = module().ranges(meth)
        scheduled = module().scheduled_count(
            rate, unit, window["settle_s"] + window["measure_s"]
        )
        drops = 37 + 11 * number
        return step_facts(
            meth,
            scenario,
            number,
            rows=scheduled - drops,
            server_count=scheduled - drops,
            dropped=drops,
            measured=scheduled
            - drops
            - module().first_measured(rate, unit, window["settle_s"]),
            p99_us=2_400_000,
            api_core_permille=960,
            cpu0_busy_permille=975,
        )
    if kind == "errors":
        return step_facts(meth, scenario, number, errors=12 + number)
    raise ValueError(kind)


def rep_steps(
    meth: Dict[str, Any], scenario: str, knee: int, kinds: Sequence[str]
) -> List[Dict[str, Any]]:
    """Steps 1..knee keep up; then one failing step per kind."""
    steps = [step_facts(meth, scenario, n) for n in range(1, knee + 1)]
    steps += [
        failing_step(meth, scenario, knee + 1 + i, kind) for i, kind in enumerate(kinds)
    ]
    return steps


def host(rep: int, exclusive: bool = True) -> Dict[str, Any]:
    return {
        "runner_image": f"ubuntu24/2026102{rep % 3}.1",
        "cpu_model": "AMD EPYC 7763 64-Core Processor",
        "kernel": "6.8.0-1036-azure",
        "nproc": 4,
        "machine": "x86_64",
        "thread_siblings": {
            "cpu0": "0",
            "cpu1": "1",
            "cpu2": "2" if exclusive else "2-3",
            "cpu3": "3" if exclusive else "2-3",
        },
        "generator_core_exclusive": exclusive,
    }


def counts(meth: Dict[str, Any], **changes: int) -> Dict[str, int]:
    out = {
        "flags": 100,
        "rule_flags": 50,
        "assignments": meth["preload"]["users"],
        "events": meth["preload"]["events"],
        "raw_metrics": meth["preload"]["raw_metrics"],
    }
    out.update(changes)
    return out


def rep(
    meth: Dict[str, Any], scenario: str, number: int, steps: List[Dict[str, Any]]
) -> Dict[str, Any]:
    se = module()
    after = counts(meth)
    if scenario == "assign":
        after["assignments"] += sum(step["rows"] for step in steps)
    out = {
        "rep": number,
        "host": host(number),
        "api_machine": "x86_64",
        "counts_before": counts(meth),
        "counts_after": after,
        "steps": steps,
    }
    out.update(se.assess_rep(meth, steps))
    return out


def results(
    meth: Dict[str, Any], reps: Dict[str, List[Dict[str, Any]]], label: str = "unit"
) -> Dict[str, Any]:
    """A whole results file around the given runs, every field realistic."""
    se = module()
    tag_commit = sha(f"{label}-tag")
    return {
        "schema": se.SCHEMA,
        "status": "complete",
        "run": {
            "run_id": 40999123456,
            "run_url": "https://github.com/Controlvar/controlvar/actions/runs/40999123456",
            "run_attempt": 1,
            "harness_commit": sha(f"{label}-harness"),
            "dispatched_at": "2026-11-20T14:03:11Z",
        },
        "system_under_test": {
            "version": "0.40.0",
            "profile": "core",
            "environment": "production",
            "release_tag": "v0.40.0",
            "tag_commit": tag_commit,
            "images": {
                "api": {
                    "digest": digest(f"{label}-api"),
                    "image_id": digest(f"{label}-api-id"),
                    "revision": tag_commit,
                    "platform": "linux/amd64",
                },
                "web": {
                    "digest": digest(f"{label}-web"),
                    "image_id": digest(f"{label}-web-id"),
                    "revision": tag_commit,
                    "platform": "linux/amd64",
                },
                "postgres": {
                    "digest": digest(f"{label}-postgres"),
                    "image_id": digest(f"{label}-postgres-id"),
                },
                "redis": {
                    "digest": digest(f"{label}-redis"),
                    "image_id": digest(f"{label}-redis-id"),
                },
            },
            "compose_sha256": digest(f"{label}-compose")[7:],
            "compose_tag_blob_sha256": digest(f"{label}-compose")[7:],
            "web_concurrency_expected": meth["expected_web_concurrency"],
            "web_concurrency_observed": meth["expected_web_concurrency"],
            "sdk_rate_limit_per_minute": meth["sdk_rate_limit_per_minute"],
            "postgres_settings": {
                "max_connections": "100",
                "shared_buffers": "128MB",
                "synchronous_commit": "on",
                "wal_sync_method": "fdatasync",
            },
            "cpu_split": {
                "api": "0",
                "postgres": "1",
                "web": "2",
                "redis": "2",
                "pg_sampler": "2",
                "generator": "3",
            },
        },
        "generator": {
            "tool": "k6",
            "version": "2.3.0",
            "image_digest": digest(f"{label}-k6"),
            "timeout_ms": meth["timeout_ms"],
            "max_vus_per_rps": meth["max_vus_per_rps"],
            "min_vus": meth["min_vus"],
        },
        "data": {"preload": dict(meth["preload"])},
        "method": meth,
        "scenarios": [
            {
                "name": name,
                "reps": runs,
                "summary": se.summarise_scenario(meth, runs),
            }
            for name, runs in reps.items()
        ],
    }


def small(meth: Optional[Dict[str, Any]] = None, **knees: int) -> Dict[str, Any]:
    """A publishable results file: every scenario, three runs, knees near step 4."""
    meth = meth or method()
    reps = {}
    for name in module().SCENARIOS:
        knee = knees.get(name, 4)
        reps[name] = [
            rep(meth, name, n, rep_steps(meth, name, knee + (n == 2), ["p99", "p99"]))
            for n in (1, 2, 3)
        ]
    return results(meth, reps)


def full_size() -> Dict[str, Any]:
    """18 jobs of about 16 steps: six scenarios, three runs each (EMS-15)."""
    meth = method()
    se = module()
    kinds = (["p99", "p99"], ["dropped", "dropped"], ["p99", "dropped"])
    reps = {}
    for offset, name in enumerate(se.SCENARIOS):
        knee = 13 + offset % 2
        reps[name] = [
            rep(meth, name, n, rep_steps(meth, name, knee + (n % 2), kinds[n - 1]))
            for n in (1, 2, 3)
        ]
    return results(meth, reps, label="full-size")


def main() -> int:
    FIXTURES.mkdir(exist_ok=True)
    FULL_SIZE.write_text(module().dumps(full_size()), encoding="utf-8")
    sys.stdout.write(f"wrote {FULL_SIZE.relative_to(REPO_ROOT)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
