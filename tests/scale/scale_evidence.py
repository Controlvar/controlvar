#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""The scale evidence: the method file, the summariser and the render (#1608).

Standard library only. It imports nothing from ``backend`` or ``modules``; the
one repository file it reads as code is ``scripts/doc_claims.py`` (also
standard library only), for the unit splitter the claims registries use.

Commands (``python tests/scale/scale_evidence.py <command> --help``):

``summarise``
    One step of one run: the generator's per-request rows (CSV, ``ROW_FIELDS``)
    and the step's observation (JSON: the rate pair, the drops, the samplers'
    readings) and the API's ``/metrics`` before and after, judged by the method
    in ``config.json``. Writes the step's record. Exit 0 when the step is
    VALID (whether or not it kept up), 1 when it is INVALID (the record is
    still written, with its reason), 2 on a refusal (nothing written).
``merge``
    The per-job results files of one run (one scenario, one repetition each)
    into the one results file, with each scenario's summary computed from its
    steps. Exit 0 when every scenario is publishable, 1 when the file was
    written but is not, 2 on a refusal.
``render``
    The results file into the page's generated block (between ``BEGIN`` and
    ``END``) AND that block's entries in the page's claims registry. Refuses a
    file that is not publishable. The page and registry paths are arguments.
``check``
    A results file: the closed schema, integers only, and every judgement and
    summary recomputed from the step data. With ``--page`` and ``--registry``,
    also that the page's block and its registry entries are what ``render``
    writes.
``verify``
    The committed results file against the artifact of the run it names
    (``gh run download``): byte for byte, the same commit, and an artifact that
    still exists. An expired artifact fails; it is never skipped.

The harness commands (``up``, ``down``, ``fixtures``, ``run``) are not in this
file. ``HARNESS_MODULE`` beside it, when present, adds them (see
``_harness_commands``).

Every number is an integer, in units the key names (``_us``, ``_ms``, ``_s``,
``_milli_rps``, ``_permille``, ``_milli``, or a bare count). The results file
refuses any float: a JSON number with a fraction or an exponent.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import decimal
import hashlib
import importlib.util
import io
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import tomllib
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
CONFIG = HERE / "config.json"
DOC_CLAIMS = REPO_ROOT / "scripts" / "doc_claims.py"
HARNESS_MODULE = "scale_harness"

SCHEMA = "controlvar-scale-results/1"

#: The six published requests, in the page's order (EMS-2). ``mix`` is the
#: smoke workload only and is never a published row.
SCENARIOS: Tuple[str, ...] = (
    "evaluate_get",
    "assign",
    "assign_returning",
    "track",
    "batch",
    "ruleset",
)
SMOKE_ONLY = "mix"

#: The fixed endpoint list: a row's endpoint is one of these, never a URL.
ENDPOINTS: Dict[str, str] = {
    "evaluate_get": "GET /api/v1/feature-flags/evaluate/{key}",
    "assign": "POST /api/v1/tracking/assign",
    "assign_returning": "POST /api/v1/tracking/assign",
    "track": "POST /api/v1/tracking/track",
    "batch": "POST /api/v1/tracking/batch",
    "ruleset": "GET /api/v1/sdk/ruleset",
}

#: How the page and the messages name each request.
LABELS: Dict[str, str] = {
    "evaluate_get": "GET /api/v1/feature-flags/evaluate/{key}",
    "assign": "POST /api/v1/tracking/assign (first assignment)",
    "assign_returning": "POST /api/v1/tracking/assign (returning user)",
    "track": "POST /api/v1/tracking/track",
    "batch": "POST /api/v1/tracking/batch (100 events each)",
    "ruleset": "GET /api/v1/sdk/ruleset",
}

# The fixture design the scenarios rest on (PLAN 3.4): 100 ACTIVE flags, half
# of them with one 3-condition dashboard-shape rule, every flag at a 50% rollout,
# and scale_ab's two variants. The evaluate scenario sends RULE_FLAG_CONTEXT to
# a rule flag and no context to a plain one, so each class has one expected
# reason, which the method file states and an in-process test pins (EMS-5).
FIXTURE_FLAGS = 100
FIXTURE_RULE_FLAGS = 50
FIXTURE_ROLLOUT_PERCENTAGE = 50
RULE_FLAG_RULES: Dict[str, Any] = {
    "logical_operator": "AND",
    "rollout_percentage": 50,
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "plan", "operator": "equals", "value": "scale"},
                {
                    "attribute": "app_version",
                    "operator": "semver_gte",
                    "value": "2.0.0",
                },
                {"attribute": "country", "operator": "in", "value": ["GB", "US", "CA"]},
            ],
        }
    ],
}
RULE_FLAG_CONTEXT: Dict[str, str] = {
    "plan": "scale",
    "app_version": "2.3.1",
    "country": "GB",
}
#: One batch request carries this many events.
BATCH_EVENTS = 100
#: Connections one API process may hold: pool_size 20 + max_overflow 20
#: (backend/app/db/session.py; a test holds the two equal).
POOL_CONNECTIONS_PER_WORKER = 40

EVALUATE_REASONS: Tuple[str, ...] = ("targeting_rule", "rollout", "inactive", "error")
FLAG_CLASSES: Tuple[str, ...] = ("rule", "plain")
ROW_ERRORS: Tuple[str, ...] = ("timeout", "connection")

#: Why a step is INVALID, in the order the rules are checked (A3).
INVALID_REASONS: Tuple[str, ...] = (
    "none",
    "sent-identity",
    "start-lag",
    "not-sampled",
    "foreign-cpu",
    "generator-cpu",
    "limiter-header",
    "limiter-rejected",
    "redis-fallback",
    "samples",
    "client-server",
    "assignments-grew",
)
#: Why a step did not keep up.
FAIL_REASONS: Tuple[str, ...] = ("none", "invalid", "dropped", "errors", "p99")
#: What stopped a run (A3), plus "not-reached" for a run that never failed.
BOTTLENECKS: Tuple[str, ...] = (
    "cpu:api",
    "cpu:postgres",
    "cpu:web-redis",
    "postgres-io",
    "db-pool",
    "serialised",
    "none-at-limit",
    "invalid",
    "not-reached",
)
#: Why a scenario cannot be published.
NOT_PUBLISHABLE: Tuple[str, ...] = (
    "reps",
    "invalid-rep",
    "below-the-series",
    "above-the-cap",
    "unstable",
    "preconditions",
)
TRACKING_ISSUE = "https://github.com/Controlvar/controlvar/issues/823"
BEGIN = "<!-- scale:begin -->"
END = "<!-- scale:end -->"
#: The claims target every unit of the generated block names. PR-5 registers
#: the mechanism in scripts/doc_claims.py's MECHANISMS.
MECHANISM = "mechanism:scale-results"
#: The workflow's artifact holding the merged results, and the file in it.
RESULTS_ARTIFACT = "scale-results"
RESULTS_FILE = "results.json"
GENERATOR_LICENCES = {"k6": "AGPL-3.0", "vegeta": "MIT"}

#: The normalised per-request record the generator's adapter writes. It holds
#: no URL, no query and no error text: the endpoint comes from ENDPOINTS, and
#: the body columns are the few facts ``judge`` reads (empty where they do not
#: apply). The generator records facts; this file decides what passed.
ROW_FIELDS: Tuple[str, ...] = (
    "scenario",
    "endpoint",
    "iteration",
    "status",
    "error",
    "latency_us",
    "start_lag_us",
    "rate_limit",
    "flag_class",
    "reason",
    "assigned",
    "variant",
    "user_index",
    "success_count",
    "failure_count",
    "flag_count",
    "id_present",
    "variant_id_present",
)


class Refused(Exception):
    """A refusal: the message names the cause and, where there is one, the fix."""


# ---------------------------------------------------------------------------
# Strict JSON: integers only, no duplicate keys
# ---------------------------------------------------------------------------
class _Float(str):
    """A JSON number with a fraction or an exponent, kept as its text."""


def _pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise Refused(f"duplicate key {key!r}")
        out[key] = value
    return out


def _floats(value: Any, path: str) -> List[str]:
    if isinstance(value, _Float):
        return [f"float {value} at {path or 'the top level'}"]
    found: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += _floats(item, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += _floats(item, f"{path}[{index}]")
    return found


def loads(text: str) -> Any:
    """JSON with every number an integer; a float is refused with its path."""
    try:
        data = json.loads(
            text,
            parse_float=_Float,
            parse_constant=_Float,
            object_pairs_hook=_pairs,
        )
    except json.JSONDecodeError as error:
        raise Refused(f"not JSON: {error}") from None
    found = _floats(data, "")
    if found:
        raise Refused("; ".join(found[:10]) + ": every number is an integer")
    return data


def dumps(data: Any) -> str:
    """The layout of every results file this writes: one step per line."""

    def emit(value: Any, indent: int, path: Tuple[str, ...]) -> str:
        pad = "  " * indent
        if path and path[-1] == "steps" and isinstance(value, list):
            inner = [
                pad + "  " + json.dumps(item, separators=(", ", ": ")) for item in value
            ]
            return "[\n" + ",\n".join(inner) + "\n" + pad + "]" if inner else "[]"
        if isinstance(value, dict) and value:
            items = [
                f"{pad}  {json.dumps(key)}: {emit(item, indent + 1, (*path, key))}"
                for key, item in value.items()
            ]
            return "{\n" + ",\n".join(items) + "\n" + pad + "}"
        if isinstance(value, list) and value and isinstance(value[0], dict):
            items = [pad + "  " + emit(item, indent + 1, path) for item in value]
            return "[\n" + ",\n".join(items) + "\n" + pad + "]"
        return json.dumps(value, separators=(", ", ": "))

    return emit(data, 0, ()) + "\n"


# ---------------------------------------------------------------------------
# A closed schema, as data
# ---------------------------------------------------------------------------
class Spec:
    def check(self, value: Any, path: str, problems: List[str]) -> None:
        raise NotImplementedError


@dataclasses.dataclass(frozen=True)
class Int(Spec):
    low: int = 0
    high: Optional[int] = None

    def check(self, value: Any, path: str, problems: List[str]) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            problems.append(f"{path}: must be an integer, got {value!r}")
        elif value < self.low:
            problems.append(f"{path}: must be at least {self.low}, got {value}")
        elif self.high is not None and value > self.high:
            problems.append(f"{path}: must be at most {self.high}, got {value}")


@dataclasses.dataclass(frozen=True)
class Bool(Spec):
    def check(self, value: Any, path: str, problems: List[str]) -> None:
        if not isinstance(value, bool):
            problems.append(f"{path}: must be true or false, got {value!r}")


@dataclasses.dataclass(frozen=True)
class Str(Spec):
    pattern: str
    what: str

    def check(self, value: Any, path: str, problems: List[str]) -> None:
        if not isinstance(value, str) or not re.fullmatch(self.pattern, value):
            problems.append(f"{path} must be {self.what}, got {value!r}")


@dataclasses.dataclass(frozen=True)
class Enum(Spec):
    values: Tuple[str, ...]

    def check(self, value: Any, path: str, problems: List[str]) -> None:
        if value not in self.values or not isinstance(value, str):
            problems.append(
                f"{path}: must be one of {list(self.values)}, got {value!r}"
            )


@dataclasses.dataclass(frozen=True)
class Obj(Spec):
    fields: Tuple[Tuple[str, Spec], ...]

    def check(self, value: Any, path: str, problems: List[str]) -> None:
        if not isinstance(value, dict):
            problems.append(f"{path or 'the top level'}: must be an object")
            return
        names = [name for name, _ in self.fields]
        for key in value:
            if key not in names:
                problems.append(f"{_join(path, key)}: unknown key")
        for name, spec in self.fields:
            if name not in value:
                problems.append(f"{_join(path, name)}: missing key")
            else:
                spec.check(value[name], _join(path, name), problems)


@dataclasses.dataclass(frozen=True)
class ListOf(Spec):
    item: Spec
    low: int = 0
    high: int = 10_000

    def check(self, value: Any, path: str, problems: List[str]) -> None:
        if not isinstance(value, list):
            problems.append(f"{path}: must be a list")
            return
        if not self.low <= len(value) <= self.high:
            problems.append(f"{path}: must hold {self.low} to {self.high} items")
        for index, item in enumerate(value):
            self.item.check(item, f"{path}[{index}]", problems)


@dataclasses.dataclass(frozen=True)
class MapOf(Spec):
    keys: Tuple[str, ...]
    item: Spec

    def check(self, value: Any, path: str, problems: List[str]) -> None:
        if not isinstance(value, dict):
            problems.append(f"{path}: must be an object")
            return
        for key, item in value.items():
            if key not in self.keys:
                problems.append(f"{_join(path, key)}: unknown key")
            else:
                self.item.check(item, _join(path, key), problems)


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def obj(**fields: Spec) -> Obj:
    return Obj(tuple(fields.items()))


PERMILLE = Int(0, 1000)
HEX40 = Str(r"[0-9a-f]{40}", "40 hex characters")
HEX64 = Str(r"[0-9a-f]{64}", "64 hex characters")
DIGEST = Str(r"sha256:[0-9a-f]{64}", "sha256: and 64 hex characters")
VERSION = Str(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}", "a version such as 0.40.0")
TAG = Str(r"v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}", "a tag such as v0.40.0")
CPULIST = Str(r"[0-9]{1,3}(?:[-,][0-9]{1,3}){0,15}", "a CPU list such as 0 or 2-3")
WORD = Str(r"[A-Za-z0-9_.]{1,32}", "a setting value")

CONFIG_SPEC = obj(
    mode=Enum(("publication", "smoke")),
    scenarios=ListOf(Str(r"[a-z_]{1,40}", "a scenario name"), 1, 20),
    step_multiplier_milli=Int(1001),
    step_cap_milli_rps=Int(1),
    start_milli_rps=obj(**{name: Int(1) for name in SCENARIOS}),
    settle_s=Int(1),
    measure_s=Int(1),
    gap_s=Int(0),
    warmup_s=Int(0),
    stop_after_failing_steps=Int(1),
    timeout_ms=Int(1),
    max_vus_per_rps=Int(1),
    min_vus=Int(1),
    keeps_up=obj(max_errors_per_mille=PERMILLE, max_p99_ms=Int(1), max_dropped=Int(0)),
    validity=obj(
        max_generator_cpu_permille=PERMILLE,
        max_start_lag_p99_us=Int(2000),
        max_foreign_cpu_permille=PERMILLE,
        min_sample_permille=PERMILLE,
        sent_identity_tolerance=Int(0),
    ),
    bottleneck=obj(
        cpu_permille=PERMILLE,
        io_wait_permille=PERMILLE,
        pool_permille=PERMILLE,
        serialised=obj(
            max_cpu_permille=PERMILLE,
            max_active_backends=Int(0, 1000),
            min_share_permille=PERMILLE,
        ),
    ),
    repetitions=Int(1),
    max_knee_spread_steps=Int(0),
    sdk_rate_limit_per_minute=Int(1),
    expected_web_concurrency=Int(1),
    preload=obj(users=Int(0), events=Int(0), raw_metrics=Int(0)),
    evaluate_expected_reason=obj(
        rule_flag=Enum(EVALUATE_REASONS), plain_flag=Enum(EVALUATE_REASONS)
    ),
    smoke=obj(
        settle_s=Int(1),
        measure_s=Int(1),
        gap_s=Int(0),
        warmup_s=Int(0),
        rates_milli_rps=ListOf(Int(1), 1, 20),
        mix=MapOf(SCENARIOS, Int(1)),
    ),
)

COUNTS = obj(
    flags=Int(0),
    rule_flags=Int(0),
    assignments=Int(0),
    events=Int(0),
    raw_metrics=Int(0),
)
STEP_SPEC = obj(
    step=Int(1),
    rate=Int(1),
    time_unit_ms=Int(1),
    milli_rps=Int(1),
    scheduled=Int(0),
    rows=Int(0),
    dropped=Int(0),
    measured=Int(0),
    achieved_milli_rps=Int(0),
    errors=Int(0),
    timeouts=Int(0),
    connection_errors=Int(0),
    client_429=Int(0),
    limit_mismatches=Int(0),
    p50_us=Int(0),
    p95_us=Int(0),
    p99_us=Int(0),
    percentiles_are_lower_bounds=Bool(),
    start_lag_p99_us=Int(0),
    sampled=Bool(),
    samples=Int(0),
    api_core_permille=Int(0),
    postgres_core_permille=Int(0),
    web_redis_core_permille=Int(0),
    generator_cpu_permille=Int(0),
    cpu0_busy_permille=PERMILLE,
    cpu0_steal_permille=PERMILLE,
    cpu0_foreign_permille=PERMILLE,
    cpu3_busy_permille=PERMILLE,
    cpu3_steal_permille=PERMILLE,
    cpu3_foreign_permille=PERMILLE,
    active_backend_samples_by_count=ListOf(Int(0), 0, 1000),
    active_backend_share_permille=PERMILLE,
    max_active_backends=Int(0),
    io_wait_observations=Int(0),
    io_wait_share_permille=PERMILLE,
    rejections_delta=Int(0),
    fallback_delta=Int(0),
    fallback_log_lines=Int(0),
    server_count=Int(0),
    server_499=Int(0),
    assignments_delta=Int(0),
    reasons=MapOf((*EVALUATE_REASONS, "other"), Int(0)),
    not_modified=Int(0),
    valid=Bool(),
    invalid_reason=Enum(INVALID_REASONS),
    keeps_up=Bool(),
    fail_reason=Enum(FAIL_REASONS),
)
HOST_SPEC = obj(
    runner_image=Str(r"[a-z0-9][a-z0-9._-]{0,40}/[0-9][0-9.]{0,30}", "a runner image"),
    cpu_model=Str(r"[A-Za-z0-9][A-Za-z0-9 ()@.,_+-]{0,120}", "a CPU model name"),
    kernel=Str(r"[0-9][A-Za-z0-9._+-]{0,60}", "a kernel release"),
    nproc=Int(1),
    machine=Enum(("x86_64", "aarch64")),
    thread_siblings=obj(cpu0=CPULIST, cpu1=CPULIST, cpu2=CPULIST, cpu3=CPULIST),
    generator_core_exclusive=Bool(),
)
REP_SPEC = obj(
    rep=Int(1),
    host=HOST_SPEC,
    api_machine=Enum(("x86_64", "aarch64")),
    counts_before=COUNTS,
    counts_after=COUNTS,
    steps=ListOf(STEP_SPEC, 1, 200),
    valid=Bool(),
    knee_step=Int(0),
    first_failing_step=Int(0),
    first_failing_reason=Enum(FAIL_REASONS),
    bottleneck=Enum(BOTTLENECKS),
)
SUMMARY_STEP_SPEC = obj(
    step=Int(1),
    milli_rps=Int(1),
    reps=Int(1),
    kept_up=Int(0),
    p50_us=Int(0),
    p95_us=Int(0),
    p99_us=Int(0),
    percentiles_are_lower_bounds=Bool(),
    errors=Int(0),
    measured=Int(0),
    achieved_milli_rps=Int(0),
)
SUMMARY_SPEC = obj(
    reps=Int(1),
    valid_reps=Int(0),
    knee_median_step=Int(0),
    knee_median_milli_rps=Int(0),
    knee_min_milli_rps=Int(0),
    knee_max_milli_rps=Int(0),
    knee_spread_steps=Int(0),
    first_failing_min_milli_rps=Int(0),
    first_failing_max_milli_rps=Int(0),
    first_failing_reasons=ListOf(Enum(FAIL_REASONS), 1, 100),
    bottleneck=Enum((*BOTTLENECKS, "mixed")),
    unstable=Bool(),
    publishable=Bool(),
    not_publishable=ListOf(Enum(NOT_PUBLISHABLE), 0, len(NOT_PUBLISHABLE)),
    steps=ListOf(SUMMARY_STEP_SPEC, 1, 200),
)
APP_IMAGE = obj(
    digest=DIGEST,
    image_id=DIGEST,
    revision=HEX40,
    platform=Enum(("linux/amd64", "linux/arm64")),
)
DEP_IMAGE = obj(digest=DIGEST, image_id=DIGEST)
RESULTS_SPEC = obj(
    schema=Enum((SCHEMA,)),
    status=Enum(("complete", "incomplete")),
    run=obj(
        run_id=Int(1),
        run_url=Str(
            r"https://github\.com/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}"
            r"/actions/runs/[0-9]{1,20}",
            "a GitHub Actions run URL",
        ),
        run_attempt=Int(1),
        harness_commit=HEX40,
        dispatched_at=Str(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
            "a UTC time such as 2026-11-20T14:03:11Z",
        ),
    ),
    system_under_test=obj(
        version=VERSION,
        profile=Enum(("core", "full")),
        environment=Enum(("production", "staging", "development", "test")),
        release_tag=TAG,
        tag_commit=HEX40,
        images=obj(api=APP_IMAGE, web=APP_IMAGE, postgres=DEP_IMAGE, redis=DEP_IMAGE),
        compose_sha256=HEX64,
        compose_tag_blob_sha256=HEX64,
        web_concurrency_expected=Int(1),
        web_concurrency_observed=Int(0),
        sdk_rate_limit_per_minute=Int(0),
        postgres_settings=obj(
            max_connections=WORD,
            shared_buffers=WORD,
            synchronous_commit=WORD,
            wal_sync_method=WORD,
        ),
        cpu_split=obj(
            api=CPULIST,
            postgres=CPULIST,
            web=CPULIST,
            redis=CPULIST,
            pg_sampler=CPULIST,
            generator=CPULIST,
        ),
    ),
    generator=obj(
        tool=Enum(tuple(GENERATOR_LICENCES)),
        version=VERSION,
        image_digest=DIGEST,
        timeout_ms=Int(1),
        max_vus_per_rps=Int(1),
        min_vus=Int(1),
    ),
    data=obj(preload=obj(users=Int(0), events=Int(0), raw_metrics=Int(0))),
    method=obj(**dict(CONFIG_SPEC.fields)),
    scenarios=ListOf(
        obj(
            name=Enum((*SCENARIOS, SMOKE_ONLY)),
            reps=ListOf(REP_SPEC, 1, 100),
            summary=SUMMARY_SPEC,
        ),
        1,
        len(SCENARIOS) + 1,
    ),
)


# ---------------------------------------------------------------------------
# The method file
# ---------------------------------------------------------------------------
def config_problems(config: Any) -> List[str]:
    """Everything wrong with a method file (A1), each naming its key."""
    problems: List[str] = []
    CONFIG_SPEC.check(config, "", problems)
    if problems:
        return problems
    names = config["scenarios"]
    if SMOKE_ONLY in names:
        problems.append("scenarios: 'mix' is smoke only: refusing a mix row")
    for name in names:
        if name not in SCENARIOS and name != SMOKE_ONLY:
            problems.append(f"scenarios: unknown scenario {name!r}")
    for name in SCENARIOS:
        if names.count(name) != 1:
            problems.append(
                f"scenarios: {name!r} appears {names.count(name)} times; "
                "the method measures each of the six once"
            )
    if config["mode"] == "publication" and config["measure_s"] < 30:
        problems.append(
            f"measure_s: {config['measure_s']} s is a smoke window; publication "
            "mode measures at least 30 s"
        )
    lag = config["validity"]["max_start_lag_p99_us"]
    if lag % 1000:
        problems.append(
            f"validity.max_start_lag_p99_us: {lag} is not a whole number of ms "
            "(the start-lag clock has 1 ms resolution)"
        )
    cap = config["step_cap_milli_rps"]
    for name, start in config["start_milli_rps"].items():
        if start > cap:
            problems.append(f"start_milli_rps.{name}: {start} is above the cap {cap}")
    weights = config["smoke"]["mix"]
    if sum(weights.values()) != 100:
        problems.append(
            f"smoke.mix: the weights sum to {sum(weights.values())}, not 100"
        )
    return problems


def load_config(source: Any = None, mode: Optional[str] = None) -> Dict[str, Any]:
    """The method file, checked; ``mode`` overrides its ``mode`` (the PR smoke).

    ``source`` is a path, the file's text, or an already parsed dict; None reads
    ``tests/scale/config.json``.
    """
    if source is None:
        source = CONFIG
    if isinstance(source, pathlib.Path):
        source = source.read_text(encoding="utf-8")
    config = (
        loads(source) if isinstance(source, str) else json.loads(json.dumps(source))
    )
    if mode is not None:
        if mode not in ("publication", "smoke"):
            raise Refused(f"mode must be publication or smoke, got {mode!r}")
        if isinstance(config, dict):
            config["mode"] = mode
    problems = config_problems(config)
    if problems:
        raise Refused("the method file is refused: " + "; ".join(problems))
    return config


def ranges(method: Mapping[str, Any]) -> Dict[str, int]:
    """The windows the mode runs: the smoke block's only in smoke mode."""
    source = method["smoke"] if method["mode"] == "smoke" else method
    return {key: source[key] for key in ("settle_s", "measure_s", "gap_s", "warmup_s")}


def step_rates(method: Mapping[str, Any], scenario: str) -> List[int]:
    """Each step's rate in milli-rps: start, x multiplier (rounded half up), to the cap."""
    if method["mode"] == "smoke":
        return list(method["smoke"]["rates_milli_rps"])
    if scenario not in SCENARIOS:
        raise Refused(f"{scenario!r} is not one of the six published scenarios")
    rate = method["start_milli_rps"][scenario]
    cap = method["step_cap_milli_rps"]
    multiplier = method["step_multiplier_milli"]
    rates: List[int] = []
    while rate <= cap:
        rates.append(rate)
        rate = max(rate + 1, (rate * multiplier + 500) // 1000)
    return rates


def rate_pair(milli_rps: int) -> Tuple[int, int]:
    """The exact k6 pair for a rate: ``rate`` iterations per ``time_unit_ms``."""
    if milli_rps < 1:
        raise Refused(f"a rate must be at least 1 milli-rps, got {milli_rps}")
    common = math.gcd(milli_rps, 1_000_000)
    return milli_rps // common, 1_000_000 // common


def milli_rps_of(rate: int, time_unit_ms: int) -> int:
    """``rate x 1,000,000 / time_unit_ms``, refused unless it is exact."""
    if rate < 1 or time_unit_ms < 1:
        raise Refused(f"the rate pair {rate}/{time_unit_ms} ms is not a rate")
    if (rate * 1_000_000) % time_unit_ms:
        raise Refused(
            f"the rate pair {rate} per {time_unit_ms} ms is not a whole number of "
            "milli-requests a second; use rate_pair()"
        )
    return rate * 1_000_000 // time_unit_ms


def scheduled_count(rate: int, time_unit_ms: int, seconds: int) -> int:
    """How many requests k6 schedules in ``seconds``: ``floor(rate x s x 1000 / tu)``."""
    return rate * seconds * 1000 // time_unit_ms


def first_measured(rate: int, time_unit_ms: int, settle_s: int) -> int:
    """The first iteration due at or after the settle ends: iteration ``i`` is due
    at ``i x time_unit_ms / rate`` ms, so ``ceil(settle_s x 1000 x rate / tu)``."""
    return -(-settle_s * 1000 * rate // time_unit_ms)


def vus(method: Mapping[str, Any], milli_rps: int) -> int:
    """``max(min_vus, ceil(max_vus_per_rps x R))``, every VU allocated before the step."""
    wanted = -(-method["max_vus_per_rps"] * milli_rps // 1000)
    return max(method["min_vus"], wanted)


# ---------------------------------------------------------------------------
# /metrics, read as sums only
# ---------------------------------------------------------------------------
_METRIC_NAME = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]{0,200}")


def metric_sums(text: str) -> Dict[str, decimal.Decimal]:
    """Each series name and the sum of its samples' values.

    A sample's labels are read past and dropped: nothing here keeps, compares or
    records a label value, so two expositions whose label sets differ give the
    same sums when their totals agree.
    """
    sums: Dict[str, decimal.Decimal] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "{" in line:
            name, _, rest = line.partition("{")
            rest = rest[rest.rfind("}") + 1 :] if "}" in rest else ""
        else:
            name, _, rest = line.partition(" ")
        fields = rest.split()
        if not _METRIC_NAME.fullmatch(name) or not 1 <= len(fields) <= 2:
            raise Refused(f"metrics line {number} is not a sample")
        try:
            value = decimal.Decimal(fields[0])
        except decimal.InvalidOperation:
            raise Refused(f"metrics line {number}: {fields[0][:40]!r} is not a number")
        sums[name] = sums.get(name, decimal.Decimal(0)) + value
    return sums


def counter_delta(
    before: Mapping[str, decimal.Decimal],
    after: Mapping[str, decimal.Decimal],
    name: str,
) -> int:
    """A counter's growth over the step, as a whole number."""
    first = before.get(name, decimal.Decimal(0))
    last = after.get(name, decimal.Decimal(0))
    delta = last - first
    if not delta.is_finite() or delta != delta.to_integral_value() or delta < 0:
        raise Refused(
            f"metrics: {name} went from {first} to {last}; a counter only grows by "
            "whole numbers (did the API restart during the step?)"
        )
    return int(delta)


# ---------------------------------------------------------------------------
# The per-request rows, and what passed
# ---------------------------------------------------------------------------
_VARIANT = re.compile(r"[A-Za-z0-9_.-]{0,64}")
_REASON = re.compile(r"[a-z_]{0,40}")


def read_rows(text: str) -> List[Dict[str, str]]:
    """The normalised per-request rows; the header must be ROW_FIELDS exactly."""
    reader = csv.DictReader(io.StringIO(text))
    header = tuple(reader.fieldnames or ())
    if header != ROW_FIELDS:
        raise Refused(f"rows: the header is {list(header)}, not {list(ROW_FIELDS)}")
    rows: List[Dict[str, str]] = []
    for number, row in enumerate(reader, 2):
        if None in row or any(value is None for value in row.values()):
            raise Refused(f"rows line {number}: not {len(ROW_FIELDS)} columns")
        if not _VARIANT.fullmatch(row["variant"]) or not _REASON.fullmatch(
            row["reason"]
        ):
            raise Refused(f"rows line {number}: a body fact is not a code")
        rows.append(row)
    return rows


def _number(row: Mapping[str, str], key: str) -> int:
    text = row[key]
    if not re.fullmatch(r"[0-9]{1,15}", text):
        raise Refused(f"rows: {key} must be a whole number, got {text!r}")
    return int(text)


def _count(row: Mapping[str, str], key: str) -> Optional[int]:
    text = row[key]
    return int(text) if re.fullmatch(r"[0-9]{1,15}", text) else None


def judge(scenario: str, row: Mapping[str, str], expect: Mapping[str, Any]) -> str:
    """'' when the request did the work it stands for, otherwise why not:
    ``status`` (a wrong status, a 429 included), ``body`` (a 2xx that did not do
    the work), ``timeout`` or ``connection``.

    ``expect``: ``reasons`` (flag class -> expected evaluate reason),
    ``variants`` (scale_ab's variant names), ``flags`` (the ruleset's size).
    """
    error = row["error"]
    if error:
        if error not in ROW_ERRORS:
            raise Refused(
                f"rows: error must be one of {list(ROW_ERRORS)}, got {error!r}"
            )
        return error
    status = _number(row, "status")
    if scenario == "ruleset":
        if status == 304:
            return ""
        if status != 200:
            return "status"
        return "" if _count(row, "flag_count") == expect["flags"] else "body"
    if status != 200:
        return "status"
    if scenario == "evaluate_get":
        wanted = expect["reasons"].get(row["flag_class"])
        return "" if wanted is not None and row["reason"] == wanted else "body"
    if scenario in ("assign", "assign_returning"):
        variants = list(expect["variants"])
        if row["assigned"] != "1" or row["variant"] not in variants:
            return "body"
        if scenario == "assign":
            return "" if row["reason"] == "assigned" else "body"
        index = _count(row, "user_index")
        if index is None:
            return "body"
        return "" if row["variant"] == variants[index % len(variants)] else "body"
    if scenario == "track":
        ok = row["id_present"] == "1" and row["variant_id_present"] == "1"
        return "" if ok else "body"
    if scenario == "batch":
        ok = (
            _count(row, "success_count") == BATCH_EVENTS
            and _count(row, "failure_count") == 0
        )
        return "" if ok else "body"
    raise Refused(f"rows: no request is called {scenario!r}")


def expectations(method: Mapping[str, Any], variants: Sequence[str]) -> Dict[str, Any]:
    """What ``judge`` compares a body with, from the method and the fixtures."""
    reasons = method["evaluate_expected_reason"]
    return {
        "reasons": {"rule": reasons["rule_flag"], "plain": reasons["plain_flag"]},
        "variants": sorted(variants),
        "flags": FIXTURE_FLAGS,
    }


def percentile(ordered: Sequence[int], p: int) -> int:
    """The nearest-rank percentile of sorted values (0 for none)."""
    if not ordered:
        return 0
    rank = max(1, (p * len(ordered) + 99) // 100)
    return ordered[rank - 1]


def _median(values: Sequence[int]) -> int:
    """The median; of an even count, the mean of the middle two rounded half up."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle] + 1) // 2


# ---------------------------------------------------------------------------
# One step: the rules (A3)
# ---------------------------------------------------------------------------
def judge_step(
    method: Mapping[str, Any], scenario: str, step: Mapping[str, Any]
) -> Tuple[Dict[str, Any], List[str]]:
    """VALID, KEEPS UP and their reasons, from a step's recorded facts.

    Returns the four judgement fields and the messages for every rule that
    failed (the first one is the step's ``invalid_reason``).
    """
    number = step["step"]
    validity = method["validity"]
    keeps = method["keeps_up"]
    window = ranges(method)
    failed: List[Tuple[str, str]] = []

    tolerance = validity["sent_identity_tolerance"]
    if abs(step["rows"] + step["dropped"] - step["scheduled"]) > tolerance:
        failed.append(
            (
                "sent-identity",
                f"step {number}: rows {step['rows']} + dropped {step['dropped']} "
                f"!= scheduled {step['scheduled']}",
            )
        )
    lag = validity["max_start_lag_p99_us"]
    if step["dropped"] == 0 and step["start_lag_p99_us"] > lag:
        failed.append(
            (
                "start-lag",
                f"step {number}: p99 start lag {_ms(step['start_lag_p99_us'])} over "
                f"the limit of {_ms(lag)}: this step measures the generator",
            )
        )
    if not step["sampled"]:
        failed.append(
            (
                "not-sampled",
                f"step {number}: no resource samples, so the generator and the "
                "bottleneck cannot be judged",
            )
        )
    else:
        foreign = validity["max_foreign_cpu_permille"]
        if step["cpu3_foreign_permille"] > foreign:
            failed.append(
                (
                    "foreign-cpu",
                    f"step {number}: other work used {step['cpu3_foreign_permille']} "
                    f"per-mille of the generator's core (limit {foreign})",
                )
            )
        ceiling = validity["max_generator_cpu_permille"]
        if step["generator_cpu_permille"] >= ceiling:
            failed.append(
                (
                    "generator-cpu",
                    f"step {number}: the generator used "
                    f"{step['generator_cpu_permille']} per-mille of its core "
                    f"(limit below {ceiling}): this step measures the generator",
                )
            )
    limit = method["sdk_rate_limit_per_minute"]
    if step["limit_mismatches"]:
        failed.append(
            (
                "limiter-header",
                f"step {number}: rate limiter not shown at {limit:,}: "
                f"{step['limit_mismatches']} answers without X-RateLimit-Limit {limit}",
            )
        )
    if step["client_429"] or step["rejections_delta"]:
        failed.append(
            (
                "limiter-rejected",
                f"step {number}: 429 on {step['client_429']} of {step['rows']}; "
                f"the limiter counted {step['rejections_delta']} rejections",
            )
        )
    if step["fallback_delta"] or step["fallback_log_lines"]:
        failed.append(
            (
                "redis-fallback",
                f"limiter fell back to per-process counting during step {number}",
            )
        )
    minimum = validity["min_sample_permille"]
    if step["sampled"] and 1000 * step["samples"] < minimum * window["measure_s"]:
        failed.append(
            (
                "samples",
                f"step {number}: {step['samples']} resource samples in a "
                f"{window['measure_s']} s window",
            )
        )
    client = step["rows"] - step["connection_errors"]
    if client != step["server_count"] or step["timeouts"] != step["server_499"]:
        failed.append(
            (
                "client-server",
                f"step {number}: client {client}, server {step['server_count']}: "
                f"{abs(client - step['server_count'])} requests unaccounted "
                f"({step['timeouts']} client timeouts, {step['server_499']} logged 499)",
            )
        )
    if scenario == "assign_returning" and step["assignments_delta"]:
        failed.append(
            (
                "assignments-grew",
                f"assign_returning: assignments grew by {step['assignments_delta']} "
                f"during step {number}",
            )
        )

    valid = not failed
    if not valid:
        fail = "invalid"
    elif step["dropped"] > keeps["max_dropped"]:
        fail = "dropped"
    elif 1000 * step["errors"] > step["measured"] * keeps["max_errors_per_mille"]:
        fail = "errors"
    elif step["p99_us"] > keeps["max_p99_ms"] * 1000:
        fail = "p99"
    else:
        fail = "none"
    messages = [message for _, message in failed]
    if fail == "dropped":
        messages.append(
            f"step {number}: {step['dropped']} scheduled requests never sent; "
            "the server did not keep up"
        )
    elif fail == "errors":
        messages.append(f"step {number}: {step['errors']} of {step['measured']} failed")
    elif fail == "p99":
        messages.append(
            f"step {number}: p99 {_ms(step['p99_us'])} over {keeps['max_p99_ms']} ms"
        )
    fields = {
        "valid": valid,
        "invalid_reason": failed[0][0] if failed else "none",
        "keeps_up": fail == "none",
        "fail_reason": fail,
    }
    return fields, messages


def _derived(step: Mapping[str, Any], method: Mapping[str, Any]) -> Dict[str, Any]:
    """The step fields computed from its other fields."""
    window = ranges(method)
    histogram = step["active_backend_samples_by_count"]
    samples = sum(histogram)
    within = sum(
        histogram[: method["bottleneck"]["serialised"]["max_active_backends"] + 1]
    )
    observations = sum(count * active for active, count in enumerate(histogram))
    highest = max((i for i, count in enumerate(histogram) if count), default=0)
    return {
        "milli_rps": milli_rps_of(step["rate"], step["time_unit_ms"]),
        "scheduled": scheduled_count(
            step["rate"], step["time_unit_ms"], window["settle_s"] + window["measure_s"]
        ),
        "achieved_milli_rps": step["measured"] * 1000 // window["measure_s"],
        "cpu0_foreign_permille": max(
            0, step["cpu0_busy_permille"] - step["api_core_permille"]
        ),
        "cpu3_foreign_permille": max(
            0, step["cpu3_busy_permille"] - step["generator_cpu_permille"]
        ),
        "active_backend_share_permille": 1000 * within // samples if samples else 0,
        "max_active_backends": highest,
        "io_wait_share_permille": (
            1000 * step["io_wait_observations"] // observations if observations else 0
        ),
        "percentiles_are_lower_bounds": step["dropped"] > 0 or step["timeouts"] > 0,
    }


def summarise_step(
    method: Mapping[str, Any],
    scenario: str,
    rows: Sequence[Mapping[str, str]],
    observation: Mapping[str, Any],
    metrics_before: str,
    metrics_after: str,
) -> Tuple[Dict[str, Any], List[str]]:
    """One step's record from its rows, its observation and its metrics."""
    if scenario == SMOKE_ONLY and method["mode"] != "smoke":
        raise Refused("mix is smoke only: refusing a mix row")
    if scenario not in SCENARIOS and scenario != SMOKE_ONLY:
        raise Refused(f"no scenario is called {scenario!r}")
    problems: List[str] = []
    OBSERVATION_SPEC.check(observation, "observation", problems)
    if problems:
        raise Refused("; ".join(problems))
    if observation["scenario"] != scenario:
        raise Refused(
            f"the observation is of {observation['scenario']!r}, not {scenario!r}"
        )
    number = observation["step"]
    rate, unit = observation["rate"], observation["time_unit_ms"]
    milli = milli_rps_of(rate, unit)
    rates = step_rates(method, scenario)
    if number > len(rates) or rates[number - 1] != milli:
        planned = rates[number - 1] if number <= len(rates) else "none"
        raise Refused(
            f"step {number} ran at {milli} milli-rps; the method's step {number} "
            f"is {planned}"
        )
    window = ranges(method)
    scheduled = scheduled_count(rate, unit, window["settle_s"] + window["measure_s"])
    first = first_measured(rate, unit, window["settle_s"])
    allowed = set(method["smoke"]["mix"]) if scenario == SMOKE_ONLY else {scenario}
    expect = expectations(method, observation["variants"])
    seen: set = set()
    measured: List[Tuple[Mapping[str, str], str]] = []
    timeouts = connection_errors = client_429 = mismatches = 0
    limit = str(method["sdk_rate_limit_per_minute"])
    for row in rows:
        kind = row["scenario"]
        if kind not in allowed:
            raise Refused(f"step {number} of {scenario} holds a {kind!r} row")
        if row["endpoint"] != ENDPOINTS[kind]:
            raise Refused(f"rows: endpoint {row['endpoint']!r} is not {kind}'s")
        iteration = _number(row, "iteration")
        if iteration in seen:
            raise Refused(f"rows: iteration {iteration} appears twice")
        if iteration >= scheduled + method["validity"]["sent_identity_tolerance"]:
            raise Refused(
                f"rows: iteration {iteration} is beyond the {scheduled} scheduled"
            )
        seen.add(iteration)
        verdict = judge(kind, row, expect)
        timeouts += row["error"] == "timeout"
        connection_errors += row["error"] == "connection"
        status = _count(row, "status") or 0
        client_429 += status == 429
        if 200 <= status < 400 and row["rate_limit"] != limit:
            mismatches += 1
        if iteration >= first:
            measured.append((row, verdict))
    latencies = sorted(_number(row, "latency_us") for row, _ in measured)
    lags = sorted(_number(row, "start_lag_us") for row, _ in measured)
    reasons: Dict[str, int] = {}
    if scenario == "evaluate_get":
        for row, _ in measured:
            code = row["reason"] if row["reason"] in EVALUATE_REASONS else "other"
            reasons[code] = reasons.get(code, 0) + 1
    sums_before = metric_sums(metrics_before)
    sums_after = metric_sums(metrics_after)
    sampled = observation["sampled"]
    cpu = observation["cpu_permille"]
    cores = observation["cores"]
    pg = observation["pg"]
    delta = observation["assignments_after"] - observation["assignments_before"]
    if delta < 0:
        raise Refused(f"step {number}: the assignments count went down by {-delta}")
    step: Dict[str, Any] = {
        "step": number,
        "rate": rate,
        "time_unit_ms": unit,
        "milli_rps": milli,
        "scheduled": scheduled,
        "rows": len(rows),
        "dropped": observation["dropped"],
        "measured": len(measured),
        "achieved_milli_rps": 0,
        "errors": sum(1 for _, verdict in measured if verdict),
        "timeouts": timeouts,
        "connection_errors": connection_errors,
        "client_429": client_429,
        "limit_mismatches": mismatches,
        "p50_us": percentile(latencies, 50),
        "p95_us": percentile(latencies, 95),
        "p99_us": percentile(latencies, 99),
        "percentiles_are_lower_bounds": False,
        "start_lag_p99_us": percentile(lags, 99),
        "sampled": sampled,
        "samples": observation["samples"] if sampled else 0,
        "api_core_permille": cpu["api"] if sampled else 0,
        "postgres_core_permille": cpu["postgres"] if sampled else 0,
        "web_redis_core_permille": cpu["web_redis"] if sampled else 0,
        "generator_cpu_permille": cpu["generator"] if sampled else 0,
        "cpu0_busy_permille": cores["cpu0"]["busy_permille"] if sampled else 0,
        "cpu0_steal_permille": cores["cpu0"]["steal_permille"] if sampled else 0,
        "cpu0_foreign_permille": 0,
        "cpu3_busy_permille": cores["cpu3"]["busy_permille"] if sampled else 0,
        "cpu3_steal_permille": cores["cpu3"]["steal_permille"] if sampled else 0,
        "cpu3_foreign_permille": 0,
        "active_backend_samples_by_count": (
            list(pg["active_backend_samples_by_count"]) if sampled else []
        ),
        "active_backend_share_permille": 0,
        "max_active_backends": 0,
        "io_wait_observations": pg["io_wait_observations"] if sampled else 0,
        "io_wait_share_permille": 0,
        "rejections_delta": counter_delta(
            sums_before, sums_after, "rate_limit_rejections_total"
        ),
        "fallback_delta": counter_delta(
            sums_before, sums_after, "rate_limit_redis_fallbacks_total"
        ),
        "fallback_log_lines": observation["fallback_log_lines"],
        "server_count": observation["server_count"],
        "server_499": observation["server_499"],
        "assignments_delta": delta,
        "reasons": reasons,
        "not_modified": sum(1 for row, _ in measured if row["status"] == "304"),
    }
    step.update(_derived(step, method))
    fields, messages = judge_step(method, scenario, step)
    step.update(fields)
    return step, messages


OBSERVATION_SPEC = obj(
    scenario=Str(r"[a-z_]{1,40}", "a scenario name"),
    step=Int(1),
    rate=Int(1),
    time_unit_ms=Int(1),
    dropped=Int(0),
    variants=ListOf(Str(r"[A-Za-z0-9_.-]{1,64}", "a variant name"), 2, 2),
    sampled=Bool(),
    samples=Int(0),
    cpu_permille=obj(api=Int(0), postgres=Int(0), web_redis=Int(0), generator=Int(0)),
    cores=obj(
        cpu0=obj(busy_permille=PERMILLE, steal_permille=PERMILLE),
        cpu3=obj(busy_permille=PERMILLE, steal_permille=PERMILLE),
    ),
    pg=obj(
        active_backend_samples_by_count=ListOf(Int(0), 0, 1000),
        io_wait_observations=Int(0),
    ),
    fallback_log_lines=Int(0),
    server_count=Int(0),
    server_499=Int(0),
    assignments_before=Int(0),
    assignments_after=Int(0),
)


# ---------------------------------------------------------------------------
# A repetition, and what stopped it
# ---------------------------------------------------------------------------
def attribute(method: Mapping[str, Any], step: Mapping[str, Any]) -> str:
    """What stopped a run, from its first failing step; first match wins (A3)."""
    if not step["valid"]:
        return "invalid"
    rule = method["bottleneck"]
    groups = (
        ("cpu:api", step["api_core_permille"]),
        ("cpu:postgres", step["postgres_core_permille"]),
        ("cpu:web-redis", step["web_redis_core_permille"]),
    )
    hot = [group for group in groups if group[1] >= rule["cpu_permille"]]
    if hot:
        return max(hot, key=lambda group: group[1])[0]
    histogram = step["active_backend_samples_by_count"]
    observations = sum(count * active for active, count in enumerate(histogram))
    if observations and (
        1000 * step["io_wait_observations"] >= rule["io_wait_permille"] * observations
    ):
        return "postgres-io"
    pool = POOL_CONNECTIONS_PER_WORKER * method["expected_web_concurrency"]
    if 1000 * step["max_active_backends"] >= rule["pool_permille"] * pool:
        return "db-pool"
    serial = rule["serialised"]
    samples = sum(histogram)
    within = sum(histogram[: serial["max_active_backends"] + 1])
    starved = step["cpu0_steal_permille"] + step["cpu0_foreign_permille"]
    on_latency = (
        step["p99_us"] > method["keeps_up"]["max_p99_ms"] * 1000 or step["dropped"] > 0
    )
    if (
        step["api_core_permille"] < serial["max_cpu_permille"]
        and starved <= method["validity"]["max_foreign_cpu_permille"]
        and samples
        and 1000 * within >= serial["min_share_permille"] * samples
        and on_latency
    ):
        return "serialised"
    return "none-at-limit"


def assess_rep(
    method: Mapping[str, Any], steps: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    """A run's knee, its first failing step and what stopped it."""
    numbers = [step["step"] for step in steps]
    if numbers != list(range(1, len(steps) + 1)):
        raise Refused(f"a run's steps must be 1..n in order, got {numbers}")
    failing = next((step for step in steps if not step["keeps_up"]), None)
    if failing is None:
        return {
            "valid": all(step["valid"] for step in steps),
            "knee_step": len(steps),
            "first_failing_step": 0,
            "first_failing_reason": "none",
            "bottleneck": "not-reached",
        }
    return {
        "valid": bool(failing["valid"]),
        "knee_step": failing["step"] - 1,
        "first_failing_step": failing["step"],
        "first_failing_reason": failing["fail_reason"],
        "bottleneck": attribute(method, failing),
    }


def precondition_problems(
    method: Mapping[str, Any], counts: Mapping[str, int]
) -> List[str]:
    """The data the run started from is the method's (V26)."""
    wanted = {
        "flags": FIXTURE_FLAGS,
        "rule_flags": FIXTURE_RULE_FLAGS,
        "assignments": method["preload"]["users"],
        "events": method["preload"]["events"],
        "raw_metrics": method["preload"]["raw_metrics"],
    }
    return [
        f"precondition: expected {value:,} {key.replace('_', ' ')}, found {counts[key]:,}"
        for key, value in wanted.items()
        if counts[key] != value
    ]


def summarise_scenario(
    method: Mapping[str, Any], reps: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    """A scenario's summary, from its runs' steps alone."""
    knees = [rep["knee_step"] for rep in reps]
    fails = [rep["first_failing_step"] for rep in reps]

    def rate_at(rep: Mapping[str, Any], number: int) -> int:
        return rep["steps"][number - 1]["milli_rps"] if number else 0

    knee_rates = [rate_at(rep, rep["knee_step"]) for rep in reps]
    fail_rates = [rate_at(rep, rep["first_failing_step"]) for rep in reps]
    median_step = _median(knees)
    holder = next((rep for rep in reps if len(rep["steps"]) >= median_step), None)
    median_rate = rate_at(holder, median_step) if holder and median_step else 0
    bottlenecks = {rep["bottleneck"] for rep in reps}
    spread = max(knees) - min(knees)
    unstable = spread > method["max_knee_spread_steps"]
    blocked: List[str] = []
    if len(reps) < method["repetitions"]:
        blocked.append("reps")
    if not all(rep["valid"] for rep in reps):
        blocked.append("invalid-rep")
    if 0 in knees:
        blocked.append("below-the-series")
    if 0 in fails:
        blocked.append("above-the-cap")
    if unstable:
        blocked.append("unstable")
    if any(precondition_problems(method, rep["counts_before"]) for rep in reps):
        blocked.append("preconditions")
    steps: List[Dict[str, Any]] = []
    for number in range(1, max(len(rep["steps"]) for rep in reps) + 1):
        present = [
            rep["steps"][number - 1] for rep in reps if len(rep["steps"]) >= number
        ]
        steps.append(
            {
                "step": number,
                "milli_rps": present[0]["milli_rps"],
                "reps": len(present),
                "kept_up": sum(1 for step in present if step["keeps_up"]),
                "p50_us": _median([step["p50_us"] for step in present]),
                "p95_us": _median([step["p95_us"] for step in present]),
                "p99_us": _median([step["p99_us"] for step in present]),
                "percentiles_are_lower_bounds": any(
                    step["percentiles_are_lower_bounds"] for step in present
                ),
                "errors": sum(step["errors"] for step in present),
                "measured": sum(step["measured"] for step in present),
                "achieved_milli_rps": _median(
                    [step["achieved_milli_rps"] for step in present]
                ),
            }
        )
    return {
        "reps": len(reps),
        "valid_reps": sum(1 for rep in reps if rep["valid"]),
        "knee_median_step": median_step,
        "knee_median_milli_rps": median_rate,
        "knee_min_milli_rps": min(knee_rates),
        "knee_max_milli_rps": max(knee_rates),
        "knee_spread_steps": spread,
        "first_failing_min_milli_rps": min(fail_rates),
        "first_failing_max_milli_rps": max(fail_rates),
        "first_failing_reasons": [rep["first_failing_reason"] for rep in reps],
        "bottleneck": bottlenecks.pop() if len(bottlenecks) == 1 else "mixed",
        "unstable": unstable,
        "publishable": not blocked,
        "not_publishable": blocked,
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# A results file: schema, recomputation, publication
# ---------------------------------------------------------------------------
def recompute_problems(results: Mapping[str, Any]) -> List[str]:
    """Every judgement and summary, recomputed from the step data it rests on."""
    method = results["method"]
    problems: List[str] = []
    for scenario in results["scenarios"]:
        name = scenario["name"]
        for rep in scenario["reps"]:
            where = f"{name} run {rep['rep']}"
            for step in rep["steps"]:
                try:
                    derived = _derived(step, method)
                except Refused as error:
                    problems.append(f"{where} step {step['step']}: {error}")
                    continue
                expected = dict(derived)
                expected.update(judge_step(method, name, {**step, **derived})[0])
                for key, value in expected.items():
                    if step[key] != value:
                        problems.append(
                            f"{where} step {step['step']}: {key} {step[key]!r} != "
                            f"{value!r} recomputed from its facts"
                        )
            try:
                assessed = assess_rep(method, rep["steps"])
            except Refused as error:
                problems.append(f"{where}: {error}")
                continue
            for key, value in assessed.items():
                if rep[key] != value:
                    problems.append(
                        f"{where}: {key} {rep[key]!r} != {value!r} recomputed from "
                        "its steps"
                    )
        if problems:
            continue
        summary = summarise_scenario(method, scenario["reps"])
        stored = scenario["summary"]
        for key, value in summary.items():
            if key == "steps":
                continue
            if stored[key] != value:
                problems.append(
                    f"{name}: summary {key} {stored[key]!r} != {value!r} recomputed "
                    "from its runs"
                )
        if len(stored["steps"]) != len(summary["steps"]):
            problems.append(
                f"{name}: summary has {len(stored['steps'])} steps; its runs have "
                f"{len(summary['steps'])}"
            )
            continue
        for kept, fresh in zip(stored["steps"], summary["steps"]):
            for key, value in fresh.items():
                if kept[key] != value:
                    values = [
                        rep["steps"][fresh["step"] - 1][key]
                        for rep in scenario["reps"]
                        if len(rep["steps"]) >= fresh["step"] and key in rep["steps"][0]
                    ]
                    problems.append(
                        f"{name}: aggregate step {fresh['step']} {key} {kept[key]!r} "
                        f"!= {value!r}, from runs {values}"
                    )
    return problems


def results_problems(results: Any) -> List[str]:
    """The closed schema, the method file, and the recomputation."""
    problems: List[str] = []
    RESULTS_SPEC.check(results, "", problems)
    if problems:
        return problems
    problems += [f"method: {problem}" for problem in config_problems(results["method"])]
    if problems:
        return problems
    for scenario in results["scenarios"]:
        for rep in scenario["reps"]:
            siblings = _cpus(rep["host"]["thread_siblings"]["cpu3"])
            if rep["host"]["generator_core_exclusive"] != (siblings == {3}):
                problems.append(
                    f"{scenario['name']} run {rep['rep']}: generator_core_exclusive "
                    f"disagrees with cpu3's siblings {sorted(siblings)}"
                )
    return problems + recompute_problems(results)


def load_results(text: str) -> Dict[str, Any]:
    """A results file, refused unless it is whole and consistent."""
    results = loads(text)
    problems = results_problems(results)
    if problems:
        shown = problems[:20] + (
            [f"... {len(problems) - 20} more"] if len(problems) > 20 else []
        )
        raise Refused("the results file is refused: " + "; ".join(shown))
    return results


def _cpus(cpulist: str) -> set:
    found: set = set()
    for part in cpulist.split(","):
        low, _, high = part.partition("-")
        found.update(range(int(low), int(high or low) + 1))
    return found


PLATFORMS = {"x86_64": "linux/amd64", "aarch64": "linux/arm64"}


def publication_problems(results: Mapping[str, Any]) -> List[str]:
    """Why this file may not be published, if anything (render refuses on any)."""
    problems: List[str] = []
    method = results["method"]
    sut = results["system_under_test"]
    if results["status"] != "complete":
        problems.append(
            "the run was interrupted, so the results are incomplete: re-run"
        )
    if method["mode"] == "smoke":
        problems.append("smoke results cannot be rendered")
    environment = sut["environment"]
    if environment != "production":
        why = ": schedulers and rate limiter are off" if environment == "test" else ""
        problems.append(f"environment is {environment}{why}")
    expected = method["expected_web_concurrency"]
    if sut["web_concurrency_observed"] != expected:
        problems.append(
            f"expected {expected} workers (config), observed "
            f"{sut['web_concurrency_observed']}"
        )
    if sut["web_concurrency_expected"] != expected:
        problems.append(
            f"web_concurrency_expected {sut['web_concurrency_expected']} is not the "
            f"method's {expected}"
        )
    if sut["sdk_rate_limit_per_minute"] != method["sdk_rate_limit_per_minute"]:
        problems.append(
            f"the API answered with a rate limit of {sut['sdk_rate_limit_per_minute']:,}"
            f"; the method says {method['sdk_rate_limit_per_minute']:,}"
        )
    tag = sut["release_tag"]
    if tag != f"v{sut['version']}":
        problems.append(f"release_tag {tag} is not v{sut['version']}")
    if sut["compose_sha256"] != sut["compose_tag_blob_sha256"]:
        problems.append(
            f"compose.yml differs from the one {tag} shipped: refusing to publish"
        )
    for image in ("api", "web"):
        revision = sut["images"][image]["revision"]
        if revision != sut["tag_commit"]:
            problems.append(
                f"the {image} image's revision {revision[:12]} is not {tag}'s commit "
                f"{sut['tag_commit'][:12]}"
            )
    if results["data"]["preload"] != method["preload"]:
        problems.append("data.preload is not the method's preload")
    if results["generator"]["timeout_ms"] != method["timeout_ms"]:
        problems.append("the generator's timeout is not the method's")
    split = sut["cpu_split"]
    generator = _cpus(split["generator"])
    for service in ("api", "postgres", "web", "redis", "pg_sampler"):
        shared = generator & _cpus(split[service])
        if shared:
            problems.append(f"the generator shares cpu {sorted(shared)} with {service}")
    names = [scenario["name"] for scenario in results["scenarios"]]
    if SMOKE_ONLY in names:
        problems.append("mix is smoke only: refusing a mix row")
    for name in set(names):
        if names.count(name) > 1:
            problems.append(f"{name} is measured {names.count(name)} times")
    missing = [name for name in SCENARIOS if name not in names]
    if missing:
        problems.append(
            f"measured {len(SCENARIOS) - len(missing)} of the six: missing "
            + ", ".join(LABELS[name] for name in missing)
        )
    for scenario in results["scenarios"]:
        if scenario["name"] not in SCENARIOS:
            continue
        label = LABELS[scenario["name"]]
        summary = scenario["summary"]
        if summary["reps"] < method["repetitions"]:
            problems.append(
                f"{label}: {summary['reps']} runs; the method says {method['repetitions']}"
            )
        if not summary["publishable"]:
            problems.append(
                f"{label}: not publishable ({', '.join(summary['not_publishable'])})"
            )
        for rep in scenario["reps"]:
            host = rep["host"]["machine"]
            platform = sut["images"]["api"]["platform"]
            if PLATFORMS[host] != platform or rep["api_machine"] != host:
                problems.append(
                    f"API runs {platform} emulated on {host}: refusing to publish"
                )
            problems += [
                f"{label} run {rep['rep']}: {problem}"
                for problem in precondition_problems(method, rep["counts_before"])
            ]
    return list(dict.fromkeys(problems))


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------
def merge(jobs: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The per-job results files of one run, as one results file."""
    if not jobs:
        raise Refused("no job results to merge")
    for job in jobs:
        if job["status"] != "complete":
            raise Refused(
                "a job's results are incomplete (the run was interrupted): re-run it"
            )
        if job["method"]["mode"] == "smoke":
            raise Refused("smoke results cannot be merged or rendered")
    first = jobs[0]
    for job in jobs[1:]:
        for key in ("schema", "run", "generator", "data", "method"):
            if job[key] != first[key]:
                raise Refused(f"the jobs disagree on {key}")
        if (
            job["system_under_test"]["images"]["api"]
            != first["system_under_test"]["images"]["api"]
        ):
            raise Refused("reps used different api images")
        for key, value in first["system_under_test"].items():
            if job["system_under_test"][key] != value:
                raise Refused(f"the jobs disagree on system_under_test.{key}")
    reps: Dict[str, Dict[int, Mapping[str, Any]]] = {}
    for job in jobs:
        for scenario in job["scenarios"]:
            name = scenario["name"]
            if name == SMOKE_ONLY:
                raise Refused("mix is smoke only: refusing a mix row")
            for rep in scenario["reps"]:
                if rep["rep"] in reps.setdefault(name, {}):
                    raise Refused(f"{name} run {rep['rep']} appears twice")
                reps[name][rep["rep"]] = rep
    method = first["method"]
    merged = {key: first[key] for key in first if key != "scenarios"}
    merged["status"] = "complete"
    merged["scenarios"] = []
    for name in SCENARIOS:
        if name not in reps:
            continue
        ordered = [reps[name][number] for number in sorted(reps[name])]
        merged["scenarios"].append(
            {
                "name": name,
                "reps": ordered,
                "summary": summarise_scenario(method, ordered),
            }
        )
    return merged


# ---------------------------------------------------------------------------
# render: the page block and its registry entries
# ---------------------------------------------------------------------------
def three_figures(milli: int) -> str:
    """A rate in requests a second to three significant figures, from milli-rps."""
    if milli < 0:
        raise Refused(f"a rate cannot be negative, got {milli}")
    digits = len(str(milli))
    if digits > 3:
        unit = 10 ** (digits - 3)
        milli = (milli + unit // 2) // unit * unit
    decimals = min(3, max(0, 6 - len(str(milli))))
    whole, fraction = divmod(milli, 1000)
    if not decimals:
        return f"{whole:,}"
    return f"{whole:,}.{fraction:03d}"[: len(f"{whole:,}") + 1 + decimals]


def rps(milli: int) -> str:
    return f"{three_figures(milli)} requests a second"


def _ms(us: int) -> str:
    """Whole milliseconds from microseconds, rounded half up."""
    if us < 1000:
        return "under 1 ms"
    return f"{(us + 500) // 1000:,} ms"


def _percent(permille: int) -> str:
    return f"{(permille + 5) // 10}%"


def _multiplier(milli: int) -> str:
    whole, fraction = divmod(milli, 1000)
    return f"{whole}.{fraction:03d}".rstrip("0").rstrip(".")


def _percentile(us: int, lower_bound: bool) -> str:
    return f"at least {_ms(us)}" if lower_bound else _ms(us)


FAIL_WORDS = {
    "dropped": "requests it could not send",
    "errors": "too many errors",
    "p99": "the 99th percentile over the limit",
    "invalid": "an invalid step",
    "none": "it did not fail",
}


def _stopped(method: Mapping[str, Any], code: str) -> str:
    """What was measured at the first step that did not keep up, for one code."""
    rule = method["bottleneck"]
    serial = rule["serialised"]
    pool = POOL_CONNECTIONS_PER_WORKER * method["expected_web_concurrency"]
    cpu = _percent(rule["cpu_permille"])
    words = {
        "cpu:api": f"the API's core was busy at least {cpu} of the time",
        "cpu:postgres": f"PostgreSQL's core was busy at least {cpu} of the time",
        "cpu:web-redis": f"the core nginx and Redis share was busy at least {cpu} "
        "of the time",
        "postgres-io": f"at least {_percent(rule['io_wait_permille'])} of the active "
        "database connections the sampler saw were waiting on disk or on the "
        "write-ahead log",
        "db-pool": "the active database connections reached "
        f"{_percent(rule['pool_permille'])} of the {pool} the API may open",
        "serialised": f"the API's core was below {_percent(serial['max_cpu_permille'])} "
        f"busy, at most {serial['max_active_backends']} database connection was "
        f"active in at least {_percent(serial['min_share_permille'])} of the "
        "database samples, and the step failed on latency",
        "none-at-limit": "no resource the samplers measure had reached its limit",
    }
    if code not in words:
        raise Refused(f"no words for the bottleneck {code!r}: it is not publishable")
    return words[code]


#: Said once, and only where a run's bottleneck is ``serialised`` (PE-S9): what
#: the measurement names, and the issue that tracks the database calls. It says
#: nothing about what a fix would change.
SERIALISED_NOTE = (
    "The request path makes synchronous calls that block the API's event loop: the "
    "rate limiter's call to Redis and the database calls. The database ones are "
    f"tracked in [#823]({TRACKING_ISSUE})."
)


def _runs(numbers: Sequence[int]) -> str:
    if len(numbers) == 1:
        return f"run {numbers[0]}"
    return "runs " + ", ".join(map(str, numbers[:-1])) + f" and {numbers[-1]}"


def _stopped_line(method: Mapping[str, Any], scenario: Mapping[str, Any]) -> str:
    """One request type's line in "What stopped it"."""
    label = LABELS[scenario["name"]]
    by_code: Dict[str, List[int]] = {}
    for rep in scenario["reps"]:
        by_code.setdefault(rep["bottleneck"], []).append(rep["rep"])
    first = "at the first step that did not keep up"
    if len(by_code) == 1:
        (code,) = by_code
        text = f"{first[0].upper()}{first[1:]}, {_stopped(method, code)}."
    else:
        parts = [
            f"in {_runs(numbers)}, {_stopped(method, code)}"
            for code, numbers in by_code.items()
        ]
        text = f"The runs differed {first}: " + "; ".join(parts) + "."
    if "serialised" in by_code:
        text += " " + SERIALISED_NOTE
    return f"- {label}: {text}"


def _starts(method: Mapping[str, Any]) -> str:
    by_rate: Dict[int, List[str]] = {}
    for name in SCENARIOS:
        by_rate.setdefault(method["start_milli_rps"][name], []).append(name)
    common = max(by_rate, key=lambda rate: len(by_rate[rate]))
    others = [
        f"{three_figures(rate)} for {', '.join(LABELS[n] for n in names)}"
        for rate, names in sorted(by_rate.items())
        if rate != common
    ]
    text = rps(common)
    return f"{text} ({'; '.join(others)})" if others else text


def render_block(results: Mapping[str, Any], results_link: str) -> str:
    """The page's generated block (the text between BEGIN and END)."""
    problems = publication_problems(results)
    if problems:
        raise Refused("refusing to render: " + "; ".join(problems))
    method = results["method"]
    sut = results["system_under_test"]
    run = results["run"]
    keeps = method["keeps_up"]
    window = ranges(method)
    name = results_link.rsplit("/", 1)[-1]
    reps = method["repetitions"]
    lines: List[str] = [
        "",
        f"Measured as of v{sut['version']} on {run['dispatched_at'][:10]}, on the "
        f"release images of commit {sut['tag_commit'][:7]}, from the results file "
        f"[{name}]({results_link}).",
        "",
        "## What it served",
        "",
        f"| Request | Kept up with | Over {reps} runs | First failed at | Why | p50 | "
        "p95 | p99 | Errors |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    scenarios = {scenario["name"]: scenario for scenario in results["scenarios"]}
    for scenario_name in SCENARIOS:
        scenario = scenarios[scenario_name]
        summary = scenario["summary"]
        at = summary["steps"][summary["knee_median_step"] - 1]
        kept = rps(summary["knee_median_milli_rps"])
        if scenario_name == "batch":
            events = summary["knee_median_milli_rps"] * BATCH_EVENTS
            kept += f" ({three_figures(events)} events a second)"
        low, high = summary["knee_min_milli_rps"], summary["knee_max_milli_rps"]
        spread = (
            three_figures(low)
            if low == high
            else f"{three_figures(low)} to {three_figures(high)}"
        )
        f_low = summary["first_failing_min_milli_rps"]
        f_high = summary["first_failing_max_milli_rps"]
        failed = (
            rps(f_low)
            if f_low == f_high
            else f"{three_figures(f_low)} to {rps(f_high)}"
        )
        why = "; ".join(
            dict.fromkeys(FAIL_WORDS[code] for code in summary["first_failing_reasons"])
        )
        bound = at["percentiles_are_lower_bounds"]
        lines.append(
            f"| {LABELS[scenario_name]} | {kept} | {spread} | {failed} | {why} | "
            f"{_percentile(at['p50_us'], bound)} | {_percentile(at['p95_us'], bound)} | "
            f"{_percentile(at['p99_us'], bound)} | {at['errors']:,} of {at['measured']:,} |"
        )
    reasons = method["evaluate_expected_reason"]
    lines += [
        "",
        f"A step kept up when every scheduled request was sent, at most "
        f"{keeps['max_errors_per_mille']} in 1,000 requests failed, and the 99th "
        f"percentile was at most {keeps['max_p99_ms']:,} ms. A request failed when its "
        f"status or its body was wrong, when it timed out after "
        f"{method['timeout_ms']:,} ms, or when its connection failed. An evaluation "
        f"passed only with the reason its flag's class gives: {reasons['rule_flag']} "
        f"for a flag with a rule, sent a context the rule matches, and "
        f"{reasons['plain_flag']} for a flag without one.",
        "",
        f"Each step offered one rate for {window['measure_s']} s, after "
        f"{window['settle_s']} s that are not counted and before a {window['gap_s']} s "
        f"gap. The first step offered {_starts(method)}; each step offered "
        f"{_multiplier(method['step_multiplier_milli'])} times the one before; a run "
        f"stopped after {method['stop_after_failing_steps']} failing steps in a row or "
        f"at {rps(method['step_cap_milli_rps'])}. Each request type ran {reps} times, "
        f"each on a fresh runner and a fresh stack, after a {window['warmup_s']} s "
        "warm-up that is not counted. The figures above are the median run's; the "
        "percentiles and errors are at the step it kept up with.",
        "",
        "Each request's time runs from when it started to its last byte, including "
        "any wait for a connection. Each request's start lag was recorded: a step "
        "with no unsent requests whose 99th percentile start lag was over "
        f"{_ms(method['validity']['max_start_lag_p99_us'])} is not published, and a "
        "step in which any scheduled request could not be sent counts as not kept up.",
        "",
        '??? note "Every step"',
        "",
        "    A percentile marked at least comes from a step with requests that were "
        "not sent or timed out: the true value is higher.",
    ]
    for scenario_name in SCENARIOS:
        summary = scenarios[scenario_name]["summary"]
        lines += [
            "",
            f"    {LABELS[scenario_name]}:",
            "",
            "    | Step | Offered | Served | Kept up | p50 | p95 | p99 | Errors |",
            "    |---|---|---|---|---|---|---|---|",
        ]
        for step in summary["steps"]:
            bound = step["percentiles_are_lower_bounds"]
            lines.append(
                f"    | {step['step']} | {rps(step['milli_rps'])} | "
                f"{rps(step['achieved_milli_rps'])} | {step['kept_up']} of "
                f"{step['reps']} runs | {_percentile(step['p50_us'], bound)} | "
                f"{_percentile(step['p95_us'], bound)} | "
                f"{_percentile(step['p99_us'], bound)} | {step['errors']:,} of "
                f"{step['measured']:,} |"
            )
    lines += ["", "## What stopped it", ""]
    lines += [_stopped_line(method, scenarios[name]) for name in SCENARIOS]
    hosts = [
        rep["host"] for scenario in results["scenarios"] for rep in scenario["reps"]
    ]
    siblings = sorted(
        set().union(*(_cpus(host["thread_siblings"]["cpu3"]) for host in hosts)) - {3}
    )
    topology = (
        "alone on its core"
        if all(host["generator_core_exclusive"] for host in hosts)
        else "sharing a physical core with cpu " + ", ".join(map(str, siblings))
    )
    split = sut["cpu_split"]
    settings = sut["postgres_settings"]
    preload = method["preload"]
    generator = results["generator"]
    lines += [
        "",
        "## The setup",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| Release | v{sut['version']} (commit {sut['tag_commit'][:7]}), the "
        f"published images, {sut['profile']} profile |",
        f"| API image | {sut['images']['api']['digest']} |",
        f"| Web image | {sut['images']['web']['digest']} |",
        f"| Machine | a GitHub-hosted runner, image "
        f"{', '.join(sorted({h['runner_image'] for h in hosts}))}; "
        f"{', '.join(sorted({str(h['nproc']) for h in hosts}))} vCPUs; "
        f"{', '.join(sorted({h['cpu_model'] for h in hosts}))} |",
        f"| CPU split | the API on cpu {split['api']}; PostgreSQL on cpu "
        f"{split['postgres']}; nginx, Redis and the samplers on cpu {split['web']}; "
        f"the load generator on cpu {split['generator']} |",
        f"| The load generator's core | {topology} |",
        f"| API processes | {sut['web_concurrency_observed']} |",
        f"| Database connections | up to {POOL_CONNECTIONS_PER_WORKER} per API process |",
        f"| PostgreSQL | max_connections {settings['max_connections']}, shared_buffers "
        f"{settings['shared_buffers']}, synchronous_commit "
        f"{settings['synchronous_commit']}, wal_sync_method "
        f"{settings['wal_sync_method']} |",
        f"| SDK rate limit | {method['sdk_rate_limit_per_minute']:,} a minute per address "
        "per path (a measurement setting, not advice) |",
        f"| Load generator | {generator['tool']} {generator['version']} "
        f"({GENERATOR_LICENCES[generator['tool']]}), the official image, unmodified |",
        f"| Request timeout | {method['timeout_ms']:,} ms |",
        f"| Data | {preload['users']:,} users, {preload['events']:,} events and "
        f"{preload['raw_metrics']:,} metric rows, loaded before each run |",
        f"| Runs | {reps} of each request type |",
        f"| Method | the harness at commit {run['harness_commit']}: check out that "
        "commit to run the same method |",
        f"| Workflow run | [{run['run_id']}]({run['run_url']}) |",
        "",
    ]
    return "\n".join(lines)


def _doc_claims() -> Any:
    """scripts/doc_claims.py, loaded by path (it is standard library only)."""
    name = "_scale_evidence_doc_claims"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, DOC_CLAIMS)
        if spec is None or spec.loader is None:
            raise Refused(f"cannot load {DOC_CLAIMS}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def split_page(page: str) -> Tuple[str, str, str]:
    """The page around its block, and the block: (before, block, after)."""
    lines = page.split("\n")
    begins = [i for i, line in enumerate(lines) if line.strip() == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if len(begins) != 1 or len(ends) != 1 or begins[0] > ends[0]:
        raise Refused(
            f"the page must hold exactly one {BEGIN} line followed by one {END} line"
        )
    start, stop = begins[0], ends[0]
    return (
        "\n".join(lines[: start + 1]),
        "\n".join(lines[start + 1 : stop]),
        "\n".join(lines[stop:]),
    )


def block_units(block: str) -> List[str]:
    """The block's units, as scripts/doc_claims.py splits them."""
    return [unit.text for unit in _doc_claims().units(block)]


def _entries(
    registry: str, document: Optional[str]
) -> Tuple[str, List[Dict[str, Any]]]:
    data = tomllib.loads(registry)
    claims = _doc_claims()
    path = data.get("document")
    entries = data.get("unit", [])
    if not isinstance(path, str) or not isinstance(entries, list):
        raise Refused("the registry has no document or no [[unit]] entries")
    if document is not None and path != document:
        raise Refused(f"the registry is {path!r}'s, not {document!r}'s")
    if claims.registry_text(path, entries) != registry:
        raise Refused(
            "the registry is not in the layout scripts/doc_claims.py writes "
            "(registry_text): render would change entries outside the block"
        )
    return path, entries


def apply_render(
    page: str, registry: str, block: str, mechanism: str = MECHANISM
) -> Tuple[str, str]:
    """The page with ``block`` in place, and the registry with the block's entries
    replaced by one per unit of the new block, each held by ``mechanism``.
    Entries for units outside the block are kept as they are, in place."""
    before, old, after = split_page(page)
    new_page = before + "\n\n" + block.strip("\n") + "\n\n" + after
    claims = _doc_claims()
    outside = [unit.text for unit in claims.units(before + "\n\n" + after)]
    inside = block_units(block)
    together = [unit.text for unit in claims.units(new_page)]
    if sorted(together) != sorted(outside + inside):
        raise Refused(
            "the block's units depend on the text around it: leave a blank line "
            "on each side of the markers"
        )
    shared = sorted(set(inside) & set(outside))
    if shared:
        raise Refused(
            f"{shared[0]!r} is both inside and outside the generated block: reword "
            "the one outside"
        )
    path, entries = _entries(registry, None)
    replaced = set(block_units(old)) | set(inside)
    positions = [i for i, entry in enumerate(entries) if entry["text"] in replaced]
    kept = [entry for entry in entries if entry["text"] not in replaced]
    if positions:
        at = positions[0]
    else:
        earlier = {unit.text for unit in claims.units(before)}
        at = max((i + 1 for i, e in enumerate(kept) if e["text"] in earlier), default=0)
    counts: Dict[str, int] = {}
    for text in inside:
        counts[text] = counts.get(text, 0) + 1
    fresh: List[Dict[str, Any]] = []
    for text, count in counts.items():
        entry: Dict[str, Any] = {"text": text, "by": [mechanism]}
        if count > 1:
            entry["count"] = count
        fresh.append(entry)
    return new_page, claims.registry_text(path, kept[:at] + fresh + kept[at:])


def check_page(
    results: Mapping[str, Any],
    page: str,
    registry: str,
    results_link: str,
    mechanism: str = MECHANISM,
) -> List[str]:
    """The page's block and the block's registry entries against render."""
    problems: List[str] = []
    block = render_block(results, results_link)
    _, old, _ = split_page(page)
    want = block.strip("\n").split("\n")
    have = old.strip("\n").split("\n")
    if want != have:
        for number, (seen, wanted) in enumerate(zip(have, want), 1):
            if seen != wanted:
                problems.append(
                    f"block line {number}: the page has {seen!r}; render gives {wanted!r}"
                )
                break
        else:
            problems.append(
                f"the block has {len(have)} lines; render gives {len(want)}"
            )
    _, entries = _entries(registry, None)
    by_text = {entry["text"]: entry for entry in entries}
    counts: Dict[str, int] = {}
    for text in block_units(block):
        counts[text] = counts.get(text, 0) + 1
    for number, (text, count) in enumerate(counts.items(), 1):
        entry = by_text.get(text)
        wanted = {
            "text": text,
            "by": [mechanism],
            **({"count": count} if count > 1 else {}),
        }
        if entry != wanted:
            problems.append(
                f"registry entry for unit {number} ({text[:60]!r}) differs from render"
            )
    return problems


#: k6's tags that carry the request URL (``url``; ``name`` defaults to the URL
#: with its query; ``error`` embeds it). The script's options must leave all
#: three out (PE-S2, V45).
URL_TAGS: Tuple[str, ...] = ("url", "name", "error")
_SYSTEM_TAGS = re.compile(r"\bsystemTags[ \t]{0,8}:[ \t\n]{0,8}\[([^\]]{0,2000})\]")
_TAG = re.compile(r"""["']([A-Za-z_]{1,40})["']""")


def system_tags_problems(script: str) -> List[str]:
    """Why a k6 script's options could write a URL into its output, if they could.

    The setting must be present, once and explicit: without it k6 writes its
    default tags, ``url`` and ``name`` among them.
    """
    found = _SYSTEM_TAGS.findall(script)
    if len(found) != 1:
        return [
            f"the script sets systemTags {len(found)} times: set it once, explicitly "
            "(k6's default tags carry the URL)"
        ]
    tags = _TAG.findall(found[0])
    return [f"system tags include {tag!r}" for tag in URL_TAGS if tag in tags]


_VERSION_FILE = re.compile(r"([0-9]{1,4})\.([0-9]{1,4})\.([0-9]{1,4})\.json")


def newest_results(directory: pathlib.Path) -> pathlib.Path:
    """The results file of the highest version in ``directory``."""
    found = []
    for path in directory.iterdir():
        match = _VERSION_FILE.fullmatch(path.name)
        if match:
            found.append((tuple(int(part) for part in match.groups()), path))
    if not found:
        raise Refused(f"no <version>.json results file in {directory}")
    return max(found)[1]


# ---------------------------------------------------------------------------
# verify: the committed file is the run's artifact
# ---------------------------------------------------------------------------
Runner = Callable[[List[str]], subprocess.CompletedProcess]


def _gh(argv: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *argv],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=600,
    )


def verify(
    committed: pathlib.Path, run_id: int, repo: str, runner: Runner = _gh
) -> List[str]:
    """Why the committed file is not run ``run_id``'s artifact, if it is not."""
    data = committed.read_bytes()
    results = loads(data.decode("utf-8"))
    problems: List[str] = []
    if results.get("run", {}).get("run_id") != run_id:
        problems.append(
            f"the file names run {results.get('run', {}).get('run_id')}, not {run_id}"
        )
    view = runner(["run", "view", str(run_id), "--repo", repo, "--json", "headSha"])
    if view.returncode != 0:
        return problems + [f"run {run_id} cannot be read: {view.stderr.strip()[:200]}"]
    built = json.loads(view.stdout).get("headSha", "")
    stated = results.get("run", {}).get("harness_commit")
    if built != stated:
        problems.append(f"run {run_id} built {built}, the file says {stated}")
    with tempfile.TemporaryDirectory() as directory:
        download = runner(
            [
                "run",
                "download",
                str(run_id),
                "--repo",
                repo,
                "--name",
                RESULTS_ARTIFACT,
                "--dir",
                directory,
            ]
        )
        artifact = pathlib.Path(directory) / RESULTS_FILE
        if download.returncode != 0 or not artifact.is_file():
            return problems + [f"artifact of run {run_id} is gone: re-run"]
        theirs = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if theirs != hashlib.sha256(data).hexdigest():
            problems.append(f"results file differs from the artifact of run {run_id}")
    return problems


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------
def _read(path: str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


def _write(path: str, text: str) -> None:
    pathlib.Path(path).write_text(text, encoding="utf-8")


def _say(lines: Sequence[str]) -> None:
    """The command's findings, on standard error."""
    for line in lines:
        sys.stderr.write(f"{line}\n")


def _cmd_summarise(args: argparse.Namespace) -> int:
    method = load_config(pathlib.Path(args.method), args.mode)
    observation = loads(_read(args.observation))
    step, messages = summarise_step(
        method,
        args.scenario,
        read_rows(_read(args.rows)),
        observation,
        _read(args.metrics_before),
        _read(args.metrics_after),
    )
    _write(args.out, json.dumps(step, indent=1) + "\n")
    _say(messages)
    return 0 if step["valid"] else 1


def _cmd_merge(args: argparse.Namespace) -> int:
    jobs = [load_results(_read(path)) for path in args.jobs]
    merged = merge(jobs)
    _write(args.out, dumps(merged))
    problems = publication_problems(merged)
    _say(problems)
    return 1 if problems else 0


def _link(results: str, page: str) -> str:
    return os.path.relpath(
        os.path.abspath(results), os.path.dirname(os.path.abspath(page))
    ).replace(os.sep, "/")


def _cmd_render(args: argparse.Namespace) -> int:
    results = load_results(_read(args.results))
    block = render_block(results, _link(args.results, args.page))
    page, registry = apply_render(
        _read(args.page), _read(args.registry), block, args.mechanism
    )
    _write(args.page, page)
    _write(args.registry, registry)
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    results = load_results(_read(args.results))
    problems: List[str] = []
    if args.publishable:
        problems += publication_problems(results)
    if args.page or args.registry:
        if not (args.page and args.registry):
            raise Refused("--page and --registry go together")
        problems += check_page(
            results,
            _read(args.page),
            _read(args.registry),
            _link(args.results, args.page),
            args.mechanism,
        )
    _say(problems)
    return 1 if problems else 0


def _cmd_verify(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", args.repo):
        raise Refused("--repo OWNER/NAME (or GITHUB_REPOSITORY) names the repository")
    problems = verify(pathlib.Path(args.results), args.run, args.repo)
    _say(problems)
    return 1 if problems else 0


def _harness_commands(commands: Any) -> None:
    """The extension point for the harness commands (up, down, fixtures, run).

    They live in their own module, ``HARNESS_MODULE``.py beside this file. When
    it exists and defines ``add_commands(subparsers)``, that adds them here, each
    with ``set_defaults(handler=...)`` like the commands below. Nothing else in
    this file knows about them.
    """
    path = HERE / f"{HARNESS_MODULE}.py"
    if not path.is_file():
        return
    spec = importlib.util.spec_from_file_location(HARNESS_MODULE, path)
    if spec is None or spec.loader is None:
        raise Refused(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[HARNESS_MODULE] = module
    spec.loader.exec_module(module)
    add = getattr(module, "add_commands", None)
    if add is not None:
        add(commands)


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = top.add_subparsers(dest="command", required=True)

    one = commands.add_parser("summarise", help="judge one step")
    one.add_argument("--method", default=str(CONFIG))
    one.add_argument("--mode", choices=("publication", "smoke"))
    one.add_argument("--scenario", required=True)
    one.add_argument("--rows", required=True)
    one.add_argument("--observation", required=True)
    one.add_argument("--metrics-before", required=True)
    one.add_argument("--metrics-after", required=True)
    one.add_argument("--out", required=True)
    one.set_defaults(handler=_cmd_summarise)

    many = commands.add_parser("merge", help="the run's jobs into one results file")
    many.add_argument("jobs", nargs="+")
    many.add_argument("--out", required=True)
    many.set_defaults(handler=_cmd_merge)

    page = commands.add_parser("render", help="write the page block and its registry")
    page.add_argument("--results", required=True)
    page.add_argument("--page", required=True)
    page.add_argument("--registry", required=True)
    page.add_argument("--mechanism", default=MECHANISM)
    page.set_defaults(handler=_cmd_render)

    check = commands.add_parser("check", help="check a results file (and a page)")
    check.add_argument("--results", required=True)
    check.add_argument("--publishable", action="store_true")
    check.add_argument("--page")
    check.add_argument("--registry")
    check.add_argument("--mechanism", default=MECHANISM)
    check.set_defaults(handler=_cmd_check)

    proof = commands.add_parser(
        "verify", help="the committed file is the run's artifact"
    )
    proof.add_argument("--results", required=True)
    proof.add_argument("--run", required=True, type=int)
    proof.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    proof.set_defaults(handler=_cmd_verify)

    _harness_commands(commands)
    return top


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except Refused as error:
        _say([f"scale-evidence: {error}"])
        return 2


if __name__ == "__main__":
    sys.exit(main())
