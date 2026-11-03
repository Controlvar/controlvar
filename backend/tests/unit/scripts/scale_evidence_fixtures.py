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

#: The layout the override applies (#1614): the API on cpu0; PostgreSQL, nginx
#: and Redis on cpu1, the API's sibling; the database sampler on cpu2; k6 alone
#: on cpu3.
SPLIT = {
    "api": "0",
    "postgres": "1",
    "web": "1",
    "redis": "1",
    "pg_sampler": "2",
    "generator": "3",
}
#: The first layout, before the runner was probed: web and Redis on k6's core.
OLD_SPLIT = {
    "api": "0",
    "postgres": "1",
    "web": "2",
    "redis": "2",
    "pg_sampler": "2",
    "generator": "3",
}
#: The runner the probe measured: 4 vCPUs, 2 cores of 2 hardware threads.
SMT = {"cpu0": "0-1", "cpu1": "0-1", "cpu2": "2-3", "cpu3": "2-3"}
#: Siblings paired across the numbering: cpu0 with cpu2, cpu1 with cpu3.
INTERLEAVED = {"cpu0": "0,2", "cpu1": "1,3", "cpu2": "0,2", "cpu3": "1,3"}
#: No SMT: every hardware thread a core of its own.
NO_SMT = {"cpu0": "0", "cpu1": "1", "cpu2": "2", "cpu3": "3"}


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
    sent_offset_us: int = 0,
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
        sent_offset_us=str(sent_offset_us),
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
    # Nothing is dropped before the tail, so each request goes out in its slot:
    # iteration i is due i x time_unit_ms / rate ms into the step.
    return [
        row(
            scenario,
            i,
            latency_us=latency_us + (i * 7919) % latency_us,
            sent_offset_us=i * unit * 1000 // rate,
        )
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
        "cpu_split": dict(SPLIT),
        "sampled": True,
        "samples": window["measure_s"],
        "cpu_permille": {
            "api": 400,
            "postgres": 200,
            "web": 80,
            "redis": 70,
            "pg_sampler": 20,
            "generator": 120,
        },
        "threads": {
            thread: {
                "busy_permille": busy,
                "steal_permille": 5,
                "irq_permille": 1,
                "softirq_permille": 4,
            }
            for thread, busy in zip(se.THREADS, (420, 360, 50, 140))
        },
        "pg": {
            "active_backend_samples_by_count": [10, 15, 5],
            "io_wait_observations": 2,
        },
        "fallback_log_lines": 0,
        "server_count": scheduled if rows is None else rows,
        "server_499": 0,
        "scrape_recovery_bound_s": 0,
        "scrape_recovery_ms": 0,
        "assignments_before": 100000,
        "assignments_after": 100000,
    }
    out.update(changes)
    return out


def tick_lines(state: Dict[str, Dict[str, Any]]) -> str:
    """The two scheduler tick families as the API's prometheus_client writes
    them, from cumulative state ``{scheduler: {outcome: ticks, "seconds":
    "1.25"}}``: the ticks, and the timed ticks' count and summed duration (every
    outcome but skipped is timed). A scheduler or an outcome not in ``state``
    has no sample, as on an API where it has not ticked yet; ``{}`` is both
    families with no sample at all."""
    se = module()
    lines = [
        "# HELP scheduler_ticks_total Background scheduler ticks by outcome",
        "# TYPE scheduler_ticks_total counter",
    ]
    for name, counts in state.items():
        for status in se.TICK_STATUSES:
            if status in counts:
                lines.append(
                    f'scheduler_ticks_total{{name="{name}",status="{status}"}} '
                    f"{counts[status]}.0"
                )
    lines += [
        "# HELP scheduler_ticks_created Background scheduler ticks by outcome",
        "# TYPE scheduler_ticks_created gauge",
        "# HELP scheduler_tick_duration_seconds Wall-clock duration of ticks",
        "# TYPE scheduler_tick_duration_seconds histogram",
    ]
    for name, counts in state.items():
        timed = sum(
            counts.get(status, 0) for status in ("success", "partial", "failed")
        )
        if not timed:
            continue
        lines += [
            f'scheduler_tick_duration_seconds_bucket{{le="1.0",name="{name}"}} 0.0',
            f'scheduler_tick_duration_seconds_bucket{{le="+Inf",name="{name}"}} {timed}.0',
            f'scheduler_tick_duration_seconds_count{{name="{name}"}} {timed}.0',
            f'scheduler_tick_duration_seconds_sum{{name="{name}"}} {counts["seconds"]}',
            f'scheduler_tick_duration_seconds_created{{name="{name}"}} 1.7936e+09',
        ]
    return "\n".join(lines) + "\n"


def ticks(**ran: Dict[str, int]) -> Dict[str, Dict[str, int]]:
    """A step record's ``scheduler_ticks``: every scheduler at zero but those
    given, e.g. ``ticks(safety={"success": 1, "duration_count": 1})``."""
    se = module()
    out = {
        name: {
            **dict.fromkeys(se.TICK_STATUSES, 0),
            "duration_count": 0,
            "duration_sum_us": 0,
        }
        for name in se.SCHEDULERS
    }
    for name, counts in ran.items():
        out[name].update(counts)
    return out


def metrics(
    shape: str = "a",
    rejections: int = 0,
    fallbacks: int = 0,
    hits: int = 5,
    tick_state: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """A /metrics exposition. The two shapes carry different label sets for the
    same series; read as sums they must agree. With ``tick_state``
    (``tick_lines``') it also exports the scheduler tick families; without, it
    is an API that does not export them."""
    extra = "" if tick_state is None else tick_lines(tick_state)
    if shape == "a":
        return extra + (
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
    return extra + (
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
    meth: Dict[str, Any],
    scenario: str,
    number: int,
    split: Optional[Dict[str, str]] = None,
    **changes: Any,
) -> Dict[str, Any]:
    """A step record as the summariser writes it, built from facts (no rows):
    healthy unless ``changes`` say otherwise; the judgement is the rules'.

    A hardware thread's busy time, unless ``changes`` give it, is what the
    containers pinned to it used plus 10 per-mille of other work."""
    se = module()
    split = split or SPLIT
    rate, unit = se.rate_pair(se.step_rates(meth, scenario)[number - 1])
    window = se.ranges(meth)
    scheduled = se.scheduled_count(rate, unit, window["settle_s"] + window["measure_s"])
    measured = scheduled - se.first_measured(rate, unit, window["settle_s"])
    cpu = {
        "api": min(880, 120 + 45 * number),
        "postgres": min(450, 40 + 20 * number),
        "web": min(150, 15 + 6 * number),
        "redis": min(150, 10 + 5 * number),
        "pg_sampler": 20,
        "generator": min(600, 30 + 20 * number),
    }
    threads: Dict[str, int] = {}
    for thread, steal in zip(se.THREADS, (4, 4, 3, 3)):
        threads.update(
            {
                f"{thread}_busy_permille": 0,
                f"{thread}_steal_permille": steal,
                f"{thread}_irq_permille": 1,
                f"{thread}_softirq_permille": 2 + steal,
                f"{thread}_foreign_permille": 0,
            }
        )
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
        **{f"{name}_cpu_permille": value for name, value in cpu.items()},
        **threads,
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
        "scrape_recovery_bound_s": 0,
        "scrape_recovery_ms": 0,
        "assignments_delta": 0,
        "reasons": {},
        "not_modified": 0,
        "scheduler_metrics": "present",
        "scheduler_ticks": ticks(),
    }
    if scenario == "evaluate_get":
        step["reasons"] = {
            "targeting_rule": measured - measured // 2,
            "rollout": measured // 2,
        }
    if scenario == "ruleset":
        step["not_modified"] = measured - measured // 50
    step.update(changes)
    pinned = se._pinned(split)
    for index, thread in enumerate(se.THREADS):
        key = f"{thread}_busy_permille"
        if key not in changes:
            used = sum(step[f"{name}_cpu_permille"] for name in pinned.get(index, []))
            step[key] = min(1000, used + 10)
    step.update(se._derived(step, meth, split))
    fields, _ = se.judge_step(meth, scenario, step, split)
    step.update(fields)
    return step


def failing_step(
    meth: Dict[str, Any],
    scenario: str,
    number: int,
    kind: str,
    split: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """A step that did not keep up, in one of the ways a real one fails."""
    if kind == "p99":
        return step_facts(
            meth,
            scenario,
            number,
            split,
            p99_us=1_000_000 + 37_000 * number,
            p95_us=600_000,
            api_cpu_permille=610,
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
            split,
            rows=scheduled - drops,
            server_count=scheduled - drops,
            dropped=drops,
            measured=scheduled
            - drops
            - module().first_measured(rate, unit, window["settle_s"]),
            p99_us=2_400_000,
            api_cpu_permille=960,
        )
    if kind == "errors":
        return step_facts(meth, scenario, number, split, errors=12 + number)
    raise ValueError(kind)


def rep_steps(
    meth: Dict[str, Any],
    scenario: str,
    knee: int,
    kinds: Sequence[str],
    split: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Steps 1..knee keep up; then one failing step per kind."""
    steps = [step_facts(meth, scenario, n, split) for n in range(1, knee + 1)]
    steps += [
        failing_step(meth, scenario, knee + 1 + i, kind, split)
        for i, kind in enumerate(kinds)
    ]
    return steps


def host(
    rep: int,
    siblings: Optional[Dict[str, str]] = None,
    split: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """A rep's runner as it recorded itself; by default the measured topology."""
    se = module()
    siblings = dict(siblings or SMT)
    generator = se._cpus((split or SPLIT)["generator"])
    return {
        "runner_image": f"ubuntu24/2026102{rep % 3}.1",
        "cpu_model": "AMD EPYC 7763 64-Core Processor",
        "kernel": "6.8.0-1036-azure",
        "nproc": 4,
        "machine": "x86_64",
        "thread_siblings": siblings,
        "generator_core_exclusive": se._core(siblings, generator) == generator,
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
    meth: Dict[str, Any],
    scenario: str,
    number: int,
    steps: List[Dict[str, Any]],
    split: Optional[Dict[str, str]] = None,
    siblings: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    se = module()
    after = counts(meth)
    if scenario == "assign":
        after["assignments"] += sum(step["rows"] for step in steps)
    out = {
        "rep": number,
        "host": host(number, siblings, split),
        "api_machine": "x86_64",
        "counts_before": counts(meth),
        "counts_after": after,
        "steps": steps,
    }
    out.update(se.assess_rep(meth, steps, split or SPLIT))
    return out


def results(
    meth: Dict[str, Any],
    reps: Dict[str, List[Dict[str, Any]]],
    label: str = "unit",
    split: Optional[Dict[str, str]] = None,
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
            "cpu_split": dict(split or SPLIT),
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


def small(
    meth: Optional[Dict[str, Any]] = None,
    split: Optional[Dict[str, str]] = None,
    siblings: Optional[Dict[str, str]] = None,
    **knees: int,
) -> Dict[str, Any]:
    """A results file of every scenario, three runs, knees near step 4:
    publishable with the default split and the measured topology."""
    meth = meth or method()
    reps = {}
    for name in module().SCENARIOS:
        knee = knees.get(name, 4)
        reps[name] = [
            rep(
                meth,
                name,
                n,
                rep_steps(meth, name, knee + (n == 2), ["p99", "p99"], split),
                split,
                siblings,
            )
            for n in (1, 2, 3)
        ]
    return results(meth, reps, split=split)


def full_size() -> Dict[str, Any]:
    """18 jobs of about 16 steps: six scenarios, three runs each (EMS-15), on
    the measured topology (siblings 0-1, 0-1, 2-3, 2-3) and the override's
    split."""
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
