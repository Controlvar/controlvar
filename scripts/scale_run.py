#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""The scale harness's measurement: run, its samplers and the k6 adapter (#1608).

    python3 scripts/scale_run.py fixtures --work DIR
    python3 scripts/scale_run.py run --work DIR --scenario NAME --rep N --out DIR \\
        [--mode smoke] [--no-resource-sampling]
    python3 scripts/scale_run.py calibrate --work DIR --out DIR [--report-only]
    python3 scripts/scale_run.py tag
    python3 scripts/scale_run.py plan --scenarios "NAME ..." --repetitions N
    python3 scripts/scale_run.py provenance --base REV --head REV

``scripts/scale_stack.py up`` starts the harness's own Compose stack and
``fixtures`` here loads it with the method file's counts. ``run`` then
measures one request type once (one repetition), the way
``.github/workflows/scale-evidence.yml`` runs it in each job of its matrix:

* a warm-up that is not counted, then one k6 process per step, from the
  official image pinned by digest (:data:`K6_IMAGE`), on the project's network
  and pinned to the load generator's hardware thread;
* around each step, the samplers: every container's cgroup ``cpu.stat`` and
  every hardware thread's ``/proc/stat`` once a second, the database sampler
  (a ``psql`` on its own hardware thread, :data:`PG_SAMPLER_SQL` every 2 s), the
  API's ``/metrics`` at both ends, and the access lines nginx wrote;
* each step judged by ``tests/scale/scale_evidence.py``'s ``summarise_step``,
  from the generator's rows (read back from k6's CSV by :func:`read_records`)
  and the step's observation; it stops after the method's failing steps in a
  row or at the cap. A confirming step (one after a step that did not keep up)
  whose after-step ``/metrics`` scrape times out waits up to
  :data:`RECOVERY_BOUND_S` for the API, records ``scrape_recovery_bound_s`` and
  ``scrape_recovery_ms`` in the step, and is INVALID (``api-unresponsive``) and
  so failing; a timeout on any other step, or no recovery in the bound, ends
  the run incomplete (exit 1);
* one results file for the job, which ``scale_evidence.py merge`` combines.

The CPU layout is the override's and the PE's (tests/scale/compose.scale.yml):
the API on cpu0; PostgreSQL, nginx and Redis on cpu1; the database sampler and
this process on cpu2; k6 alone on cpu3. Every container's cpuset is recorded
from ``docker inspect`` and never copied from here, and ``run`` refuses a
layout in which part of the system under test shares the load generator's
physical core.

There is no option to aim it at anything but its own stack: the target is the
project's ``web`` service, found through ``docker compose``.

Standard library only. Exit status: 0 done (and, for ``run``, the run is
valid; in smoke mode every step is valid and every request did its work), 1
failed or written but not valid, 2 refused or misused.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime
import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
SCALE_EVIDENCE = ROOT / "tests" / "scale" / "scale_evidence.py"
K6_DIR = ROOT / "tests" / "scale" / "k6"
SCENARIOS_JS = K6_DIR / "scenarios.js"
COMPOSE_FILE = ROOT / "deploy" / "compose" / "compose.yml"
RESULTS_DIR = "docs/performance/scale-results"


def _load(name: str, path: Path) -> Any:
    """A standard-library-only repository script, loaded by path, once, and in
    ``sys.modules`` before it runs (its dataclasses look their module up)."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise SystemExit(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[name]
            raise
    return sys.modules[name]


#: The summariser: the method file, the step rules and the results schema.
se = _load("scale_evidence", SCALE_EVIDENCE)
#: The harness's stack: the Compose argv, the child environment, the fixtures'
#: shape. The module scale_evidence.py already loaded, not a second copy.
ss = se.stack()

Refused = se.Refused


class Failed(Exception):
    """Something the harness started did not succeed (exit 1)."""


# -- the generator ---------------------------------------------------------------

#: The official k6 image, pinned by digest (the manifest index); never copied
#: into this repository or any image (ruling 2).
K6_IMAGE = (
    "grafana/k6:2.3.0"
    "@sha256:2dd4756ec6e14e62778916e1eb850108e4b499304c02df56760fa0bdb0fba0b8"
)
K6_TOOL = "k6"
#: k6's exit status when a threshold failed; the script's only threshold is
#: "no dropped iteration".
K6_THRESHOLDS_FAILED = 99
#: The custom Trend scenarios.js adds one sample of per request.
RECORD_METRIC = "scale_request"
#: The metadata key that carries the record (tests/scale/k6/lib.js).
RECORD_KEY = "scale_row"
#: The record's fields, in lib.js's order (a test holds the two equal).
RECORD_FIELDS: Tuple[str, ...] = (
    "scenario",
    "iteration",
    "status",
    "error",
    "latency_us",
    "start_lag_us",
    "sent_offset_us",
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
    "scenario_start_ms",
)
#: The User-Agent every generator request carries (scenarios.js options), by
#: which the step's access lines are counted.
USER_AGENT = "controlvar-scale"
#: The containers this harness starts itself, beside the project's.
GENERATOR_NAME = f"{ss.PROJECT}-k6"
SAMPLER_NAME = f"{ss.PROJECT}-pg-sampler"
STUB_NAME = f"{ss.PROJECT}-stub"

# -- the layout (PE SMT ruling) ----------------------------------------------------

#: k6, alone on its hardware thread.
GENERATOR_THREAD = 3
#: The database sampler and this process: the generator's sibling on the
#: runner probed, never one of the system under test's threads.
SAMPLER_THREAD = 2
DRIVER_THREAD = 2
STACK_SERVICES: Tuple[str, ...] = ("api", "postgres", "web", "redis")
#: The address nginx listens on inside the project's network (compose.yml).
WEB_PORT = 8080

# -- the samplers --------------------------------------------------------------------

CPU_SAMPLE_S = 1
PG_SAMPLE_S = 2
#: After k6 exits: time for the last access lines to reach the log.
DRAIN_S = 2
#: How long the API gets to answer /metrics again after a CONFIRMING step (one
#: that follows a step that did not keep up) timed out its scrape. The run that
#: found this (run 41612833958) saw the API silent for more than 30 s after
#: ~201 rps of abandoned requests; the bound is ten times that, long enough for
#: the queue to drain, short enough that a wedged API still fails the run (and
#: well inside the job's timeout). The wait is counted from the failed scrape's
#: start, on the injected clock.
RECOVERY_BOUND_S = 300
#: Seconds between recovery attempts (each attempt itself waits up to 30 s).
RECOVERY_POLL_S = 5
#: curl's exit status for "operation timed out"; the scrape's only recoverable
#: failure, and only on a confirming step.
CURL_TIMED_OUT = 28
#: /proc/stat's per-CPU fields this reads, in order (guest time is already in
#: user and nice, so guest and guest_nice are left out of the total).
PROC_STAT_FIELDS: Tuple[str, ...] = (
    "user",
    "nice",
    "system",
    "idle",
    "iowait",
    "irq",
    "softirq",
    "steal",
)
#: The database sampler's application_name: it leaves its own connections out.
SAMPLER_APP = "scale-sampler"
#: What the database sampler runs every PG_SAMPLE_S seconds, in one session.
#: It counts the active client backends other than its own, and those of them
#: waiting on IO or on the write-ahead log, and stamps the sample with the
#: server's clock. It reads state, wait_event_type, wait_event, backend_type,
#: pid and application_name only: never ``query``, and never a row value, which
#: would select every column (PE-S22). A test pins the exact text.
PG_SAMPLER_SQL = (
    "SELECT 'scale-sample',\n"
    "       floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint,\n"
    "       count(*) FILTER (WHERE state = 'active'),\n"
    "       count(*) FILTER (WHERE state = 'active' AND (wait_event_type = 'IO'\n"
    "                        OR (wait_event_type = 'LWLock' AND wait_event LIKE 'WAL%')))\n"
    "FROM pg_stat_activity\n"
    "WHERE backend_type = 'client backend'\n"
    "  AND pid <> pg_backend_pid()\n"
    f"  AND coalesce(application_name, '') <> '{SAMPLER_APP}'\n"
    f"\\watch {PG_SAMPLE_S}\n"
)
_PG_SAMPLE = re.compile(r"scale-sample\|([0-9]{13})\|([0-9]{1,5})\|([0-9]{1,5})")
#: The table counts a run starts and ends with (V26, V21), and the
#: assignments of scale_ab around each step (V20).
COUNTS_SQL = (
    "SELECT concat_ws('|',"
    " (SELECT count(*) FROM {schema}.feature_flags WHERE key LIKE '{flags}%'),"
    " (SELECT count(*) FROM {schema}.feature_flags WHERE key LIKE '{flags}%'"
    " AND jsonb_typeof(targeting_rules -> 'groups') = 'array'),"
    " (SELECT count(*) FROM {schema}.assignments a JOIN {schema}.experiments e"
    " ON e.id = a.experiment_id WHERE e.key = '{experiment}'),"
    " (SELECT count(*) FROM {schema}.events),"
    " (SELECT count(*) FROM {schema}.raw_metrics))"
)
ASSIGNMENTS_SQL = (
    "SELECT count(*) FROM {schema}.assignments a JOIN {schema}.experiments e"
    " ON e.id = a.experiment_id WHERE e.key = '{experiment}'"
)
COUNT_KEYS: Tuple[str, ...] = (
    "flags",
    "rule_flags",
    "assignments",
    "events",
    "raw_metrics",
)
SETTINGS: Tuple[str, ...] = (
    "max_connections",
    "shared_buffers",
    "synchronous_commit",
    "wal_sync_method",
)
#: The start of the API's log line on a switch to per-process counting
#: (backend/app/middleware/rate_limiter.py FALLBACK_MESSAGE; a test holds the
#: two together).
FALLBACK_LOG = "rate limiter: Redis unavailable"
#: An access line in the web image's format: the method, the path without its
#: query, the status and the User-Agent are what is read.
_ACCESS_LINE = re.compile(
    r'\S{1,64} - \S{1,64} \[[^\]]{1,64}\] "([A-Z]{1,10}) (\S{1,2048}) '
    r'HTTP/[0-9.]{1,4}" ([0-9]{3}) [0-9]{1,15} "[^"]{0,2048}" "([^"]{0,512})" '
    r'"[^"]{0,512}"'
)

# -- the calibration stub (V18) ---------------------------------------------------

CALIBRATION_MILLI_RPS = 100_000
CALIBRATION_S = 60
CALIBRATION_STALL_S = 2
CALIBRATION_PERIOD_S = 10
CALIBRATION_VUS = 400
#: The p99 the calibration must reach: a stall of 2 s in every 10 s delays a
#: fifth of the requests by up to 2 s, so a generator that counts waiting
#: requests reports a p99 near 2 s, and one that does not reports the server's
#: own few milliseconds.
CALIBRATION_MIN_P99_US = 1_000_000
STUB_PORT = 8080
#: The stub: every request waits out a stall of CALIBRATION_STALL_S seconds at
#: the start of every CALIBRATION_PERIOD_S seconds, then answers 200.
STUB_CODE = f"""\
import http.server, time
T0 = time.monotonic()
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_GET(self):
        phase = (time.monotonic() - T0) % {CALIBRATION_PERIOD_S}
        if phase < {CALIBRATION_STALL_S}:
            time.sleep({CALIBRATION_STALL_S} - phase)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *args):
        pass
class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 1024
server = Server(("0.0.0.0", {STUB_PORT}), Handler)
print("ready", flush=True)
server.serve_forever()
"""


# ---------------------------------------------------------------------------
# The generator's records, read back from k6's CSV
# ---------------------------------------------------------------------------
def read_records(text: str) -> List[Dict[str, str]]:
    """Every scale_request sample's record, from k6's CSV output.

    k6 writes every metric's samples; this reads only RECORD_METRIC's, whose
    ``metadata`` column must be exactly ``scale_row=<the record>``. Anything
    else there (another metadata key, a field too many or too few) is refused:
    a record read wrongly would be judged as something it is not.
    """
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or header[:3] != ["metric_name", "timestamp", "metric_value"]:
        raise Refused("k6's CSV does not start with metric_name,timestamp,metric_value")
    if header[-2:] != ["extra_tags", "metadata"]:
        raise Refused("k6's CSV does not end with extra_tags,metadata")
    for column in ("url", "name", "error"):
        if column in header:
            raise Refused(f"k6's CSV has a {column} column: the system tags are wrong")
    records: List[Dict[str, str]] = []
    for number, line in enumerate(reader, 2):
        if len(line) != len(header):
            raise Refused(f"k6's CSV line {number}: not {len(header)} columns")
        if line[0] != RECORD_METRIC:
            continue
        key, equals, value = line[-1].partition("=")
        if key != RECORD_KEY or not equals or "&" in value:
            raise Refused(
                f"k6's CSV line {number}: a {RECORD_METRIC} sample's metadata is not "
                f"{RECORD_KEY}=<record> alone"
            )
        values = value.split("|")
        if len(values) != len(RECORD_FIELDS):
            raise Refused(
                f"k6's CSV line {number}: the record has {len(values)} fields, not "
                f"{len(RECORD_FIELDS)}"
            )
        records.append(dict(zip(RECORD_FIELDS, values)))
    return records


def scenario_start(records: Sequence[Mapping[str, str]]) -> int:
    """The k6 scenario's start time (ms since the epoch), which every record of
    one step carries alike."""
    starts = {record["scenario_start_ms"] for record in records}
    if len(starts) != 1 or not re.fullmatch(r"[0-9]{13}", next(iter(starts))):
        raise Refused(f"the step's records name {len(starts)} scenario start times")
    return int(next(iter(starts)))


def to_rows(records: Sequence[Mapping[str, str]]) -> List[Dict[str, str]]:
    """The summariser's rows (scale_evidence.ROW_FIELDS): each record's fields,
    and its endpoint from the fixed list."""
    rows = []
    for record in records:
        endpoint = se.ENDPOINTS.get(record["scenario"])
        if endpoint is None:
            raise Refused(f"a record of {record['scenario']!r}, which is not measured")
        row = {name: record.get(name, "") for name in se.ROW_FIELDS}
        row["endpoint"] = endpoint
        rows.append(row)
    return rows


def rows_text(rows: Sequence[Mapping[str, str]], lead: Sequence[str] = ()) -> str:
    """Rows as CSV, ROW_FIELDS after any ``lead`` columns."""
    out = io.StringIO()
    writer = csv.DictWriter(
        out, fieldnames=[*lead, *se.ROW_FIELDS], lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def k6_version(text: str) -> str:
    """The version ``k6 version`` reports, e.g. ``k6 v2.3.0 (commit/...)``."""
    match = re.search(r"\bk6 v([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4})\b", text)
    if not match:
        raise Refused(f"k6 did not report a version: {text.strip()[:120]!r}")
    return match.group(1)


def k6_summary(text: str) -> Dict[str, int]:
    """What k6 counted itself (lib.js summaryFacts)."""
    try:
        data = json.loads(text)
    except ValueError:
        raise Refused("k6's summary is not JSON") from None
    keys = ("requests", "measured", "dropped", "measured_p99_us")
    if not isinstance(data, dict) or set(data) != set(keys):
        raise Refused(
            f"k6's summary holds {sorted(data) if isinstance(data, dict) else data!r}"
        )
    for key in keys:
        if (
            not isinstance(data[key], int)
            or isinstance(data[key], bool)
            or data[key] < 0
        ):
            raise Refused(f"k6's summary: {key} is {data[key]!r}")
    return data


def measured_count(rows: Sequence[Mapping[str, str]], settle_s: int) -> int:
    """The rows sent after the settle: by ``sent_offset_us``, the same rule as
    ``summarise_step`` and the script (never by the iteration number, #1667)."""
    return sum(1 for row in rows if int(row["sent_offset_us"]) >= settle_s * 1_000_000)


def step_line(record: Mapping[str, Any], k6_p99_us: int) -> str:
    """One step's line. A step that measured nothing says so in words: it has
    no percentile and no failure count to print."""
    head = f"step {record['step']}: {se.rps(record['milli_rps'])}"
    if record["measured"] == 0:
        return (
            f"{head}: INVALID ({record['invalid_reason']}), no request was sent "
            "in the measure window, so no latency or failure count was measured"
        )
    verdict = "valid" if record["valid"] else f"INVALID ({record['invalid_reason']})"
    kept = (
        "kept up"
        if record["keeps_up"]
        else f"did not keep up ({record['fail_reason']})"
    )
    return (
        f"{head}: {verdict}, {kept}, p99 {se._ms(record['p99_us'])}, "
        f"{record['errors']} of {record['measured']} failed "
        f"(k6's own p99 {se._ms(k6_p99_us)})"
    )


def k6_counts_problems(
    summary: Mapping[str, int],
    records: int,
    measured: int,
    exit_code: int,
) -> List[str]:
    """Where k6's own counts and exit status disagree with what was read."""
    problems = []
    if summary["requests"] != records:
        problems.append(
            f"k6 counted {summary['requests']} requests; its CSV holds {records} "
            "records"
        )
    if summary["measured"] != measured:
        problems.append(
            f"k6 counted {summary['measured']} measured requests; the rows hold "
            f"{measured}"
        )
    if (exit_code == K6_THRESHOLDS_FAILED) != (summary["dropped"] > 0):
        problems.append(
            f"k6 exited {exit_code} with {summary['dropped']} dropped iterations: "
            f"it exits {K6_THRESHOLDS_FAILED} exactly when one was dropped"
        )
    return problems


# ---------------------------------------------------------------------------
# The CPU samplers: cgroup v2 cpu.stat and /proc/stat
# ---------------------------------------------------------------------------
def cgroup_dir(proc_cgroup: str) -> str:
    """A process's cgroup v2 directory, from its ``/proc/<pid>/cgroup``.

    On the runner probed that is ``/sys/fs/cgroup/system.slice/docker-<id>.scope``;
    it is read, never assumed. A process with no unified (``0::``) entry is not
    on cgroup v2, and is refused.
    """
    for line in proc_cgroup.splitlines():
        hierarchy, _, rest = line.strip().partition(":")
        controllers, _, path = rest.partition(":")
        if hierarchy == "0" and controllers == "":
            parts = path.split("/")
            if not path.startswith("/") or ".." in parts or "\x00" in path:
                raise Refused(f"the cgroup path {path!r} is not a plain path")
            return "/sys/fs/cgroup" + path.rstrip("/")
    raise Refused("the process has no cgroup v2 entry: the samplers need cgroup v2")


def cpu_usage_usec(cpu_stat: str) -> int:
    """``usage_usec`` from a cgroup v2 ``cpu.stat``."""
    for line in cpu_stat.splitlines():
        name, _, value = line.partition(" ")
        if name == "usage_usec" and re.fullmatch(r"[0-9]{1,20}", value.strip()):
            return int(value)
    raise Refused("cpu.stat has no usage_usec")


def proc_stat(text: str) -> Dict[str, Tuple[int, ...]]:
    """Each ``cpuN`` line of /proc/stat: PROC_STAT_FIELDS, in USER_HZ ticks."""
    threads: Dict[str, Tuple[int, ...]] = {}
    for line in text.splitlines():
        fields = line.split()
        if fields and re.fullmatch(r"cpu[0-9]{1,4}", fields[0]):
            values = fields[1 : 1 + len(PROC_STAT_FIELDS)]
            if len(values) != len(PROC_STAT_FIELDS) or not all(
                v.isdigit() for v in values
            ):
                raise Refused(f"/proc/stat's {fields[0]} line is not as expected")
            threads[fields[0]] = tuple(int(v) for v in values)
    return threads


def _permille(part: int, whole_: int) -> int:
    """``1000 x part / whole``, rounded half up; 0 of nothing."""
    if whole_ <= 0:
        return 0
    return (2000 * part + whole_) // (2 * whole_)


def thread_permille(before: Sequence[int], after: Sequence[int]) -> Dict[str, int]:
    """A hardware thread's use between two /proc/stat readings, in per-mille of
    the ticks between them: busy (user, nice, system, irq and softirq), steal,
    and irq and softirq on their own (PE condition 8: they are part of busy, and
    recorded apart so foreign CPU can be calibrated against them)."""
    delta = dict(zip(PROC_STAT_FIELDS, (b - a for a, b in zip(before, after))))
    if any(value < 0 for value in delta.values()):
        raise Refused("/proc/stat went backwards between two readings")
    total = sum(delta.values())
    busy = sum(delta[k] for k in ("user", "nice", "system", "irq", "softirq"))
    return {
        "busy_permille": _permille(busy, total),
        "steal_permille": _permille(delta["steal"], total),
        "irq_permille": _permille(delta["irq"], total),
        "softirq_permille": _permille(delta["softirq"], total),
    }


@dataclasses.dataclass(frozen=True)
class Reading:
    """One sampler tick: when (ms since the epoch), every hardware thread's
    counters, and each registered container's ``usage_usec`` (None when its
    cpu.stat could not be read)."""

    at_ms: int
    threads: Optional[Mapping[str, Tuple[int, ...]]]
    usage: Mapping[str, Optional[int]]


@dataclasses.dataclass(frozen=True)
class CpuWindow:
    sampled: bool
    samples: int
    cpu: Dict[str, int]
    threads: Dict[str, Dict[str, int]]


def _zero_threads() -> Dict[str, Dict[str, int]]:
    return {
        thread: dict.fromkeys(
            ("busy_permille", "steal_permille", "irq_permille", "softirq_permille"), 0
        )
        for thread in se.THREADS
    }


def cpu_window(
    readings: Sequence[Reading], start_ms: int, end_ms: int, containers: Sequence[str]
) -> CpuWindow:
    """Each container's CPU and each thread's use over ``[start_ms, end_ms]``,
    from the first and last readings inside it, and how many readings fell
    inside it (rule 7). A container or a thread not read at both ends makes the
    window not sampled: never a 0 that looks like an idle CPU."""
    inside = [r for r in readings if start_ms <= r.at_ms <= end_ms]
    zeros = CpuWindow(False, len(inside), dict.fromkeys(containers, 0), _zero_threads())
    if len(inside) < 2:
        return zeros
    first, last = inside[0], inside[-1]
    elapsed_us = (last.at_ms - first.at_ms) * 1000
    if elapsed_us <= 0 or first.threads is None or last.threads is None:
        return zeros
    cpu = {}
    for name in containers:
        before, after = first.usage.get(name), last.usage.get(name)
        if before is None or after is None or after < before:
            return zeros
        cpu[name] = _permille(after - before, elapsed_us)
    threads = {}
    for thread in se.THREADS:
        if thread not in first.threads or thread not in last.threads:
            return zeros
        threads[thread] = thread_permille(first.threads[thread], last.threads[thread])
    return CpuWindow(True, len(inside), cpu, threads)


# ---------------------------------------------------------------------------
# The database sampler, the access lines and the API's log
# ---------------------------------------------------------------------------
def pg_samples(text: str) -> List[Tuple[int, int, int]]:
    """The database sampler's samples: (ms since the epoch, active backends,
    of those waiting on IO or the WAL). Other output lines are not samples;
    too few samples in a window makes it not sampled."""
    found = []
    for line in text.splitlines():
        match = _PG_SAMPLE.fullmatch(line.strip())
        if match:
            found.append(tuple(int(group) for group in match.groups()))
    return found


@dataclasses.dataclass(frozen=True)
class PgWindow:
    samples: int
    by_count: List[int]
    io_wait_observations: int


def pg_window(
    samples: Sequence[Tuple[int, int, int]], start_ms: int, end_ms: int
) -> PgWindow:
    """The window's samples as ``active_backend_samples_by_count`` (index n:
    the samples with n active backends) and the IO-or-WAL waits summed."""
    inside = [s for s in samples if start_ms <= s[0] <= end_ms]
    highest = max((active for _, active, _ in inside), default=-1)
    by_count = [0] * (highest + 1)
    for _, active, _ in inside:
        by_count[active] += 1
    return PgWindow(len(inside), by_count, sum(waiting for _, _, waiting in inside))


def access_counts(text: str, user_agent: str = USER_AGENT) -> Tuple[int, int]:
    """How many requests from the generator nginx logged, and how many of them
    it logged as 499 (the client gave up first)."""
    total = gave_up = 0
    for line in text.splitlines():
        match = _ACCESS_LINE.fullmatch(line.strip())
        if match and match.group(4) == user_agent:
            total += 1
            gave_up += match.group(3) == "499"
    return total, gave_up


def fallback_lines(text: str) -> int:
    """The API's log lines that announce a switch to per-process counting."""
    return sum(FALLBACK_LOG in line for line in text.splitlines())


def web_workers(top: str) -> int:
    """The API processes serving requests, from ``docker top``'s ``ps -ef``
    table: uvicorn's spawned workers when it has any, otherwise uvicorn itself
    (one worker runs in the main process)."""
    lines = top.strip().splitlines()
    if not lines or lines[0].split()[-1:] != ["CMD"]:
        raise Refused("docker top did not answer with a ps -ef table")
    commands = [line.split(None, 7)[7] for line in lines[1:] if len(line.split()) >= 8]
    spawned = sum("multiprocessing.spawn" in command for command in commands)
    if spawned:
        return spawned
    return sum(
        any(os.path.basename(word) == "uvicorn" for word in command.split()[:2])
        for command in commands
    )


# ---------------------------------------------------------------------------
# The release measured (EMS-7, C3)
# ---------------------------------------------------------------------------
def compose_version(compose: str) -> str:
    """The release literal the shipped compose.yml names, once for the api and
    once for the web image."""
    found = re.findall(
        r"-([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4})[ \t]*#[ \t]*x-release-please-version",
        compose,
    )
    if len(found) != 2 or len(set(found)) != 1:
        raise Refused(
            f"compose.yml names the release {sorted(set(found)) or 'nowhere'}; the "
            "api and web images must name one release"
        )
    return found[0]


def image_release(reference: str, image: str) -> Tuple[str, str]:
    """(profile, version) from an app image reference, e.g.
    ``ghcr.io/controlvar/controlvar:core-0.39.0``."""
    repository = "controlvar" if image == "api" else "controlvar-web"
    match = re.fullmatch(
        rf"ghcr\.io/controlvar/{repository}:([a-z]{{1,10}})-"
        r"([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4})",
        reference,
    )
    if not match:
        raise Refused(f"the {image} container runs {reference!r}, not a release image")
    return match.group(1), match.group(2)


def release_problems(
    version: str,
    compose: bytes,
    tag_blob: bytes,
    tag_commit: str,
    revisions: Mapping[str, str],
) -> List[str]:
    """Why this checkout does not measure the release it names (C3): (i) its
    compose.yml must be byte for byte the tag's, and (ii) the api and web
    images' revision labels must be the tag's commit."""
    tag = f"v{version}"
    problems = []
    if compose != tag_blob:
        problems.append(
            f"compose.yml differs from the one {tag} shipped: refusing to publish"
        )
    for image in ("api", "web"):
        revision = revisions.get(image, "")
        if revision != tag_commit:
            problems.append(
                f"the {image} image's revision {revision[:12] or 'label'} is not "
                f"{tag}'s commit {tag_commit[:12]}: refusing to publish"
            )
    return problems


# ---------------------------------------------------------------------------
# The workflow's matrix, and the run's records
# ---------------------------------------------------------------------------
def plan_matrix(scenarios: str, repetitions: str) -> List[Dict[str, Any]]:
    """The measure jobs a dispatch asks for: each named request type times
    the repetitions, in the page's order."""
    names = scenarios.split()
    unknown = [name for name in names if name not in se.SCENARIOS]
    if not names or unknown:
        raise Refused(
            f"scenarios: {' '.join(unknown) or 'none given'}; name some of "
            f"{' '.join(se.SCENARIOS)}"
        )
    if len(set(names)) != len(names):
        raise Refused("scenarios: a request type is named twice")
    if not re.fullmatch(r"[1-9]", repetitions.strip()):
        raise Refused(f"repetitions: {repetitions!r} is not a whole number from 1 to 9")
    count = int(repetitions)
    return [
        {"scenario": name, "rep": rep}
        for name in se.SCENARIOS
        if name in names
        for rep in range(1, count + 1)
    ]


def utc(ms: int) -> str:
    """A time for ``docker logs --since/--until`` and the records."""
    moment = datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def run_record(environ: Mapping[str, str], dispatched_at: str) -> Dict[str, Any]:
    """The workflow run the results come from (GitHub Actions' variables)."""
    names = ("GITHUB_RUN_ID", "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_SERVER_URL")
    missing = [name for name in names if not environ.get(name)]
    if missing:
        raise Refused(
            f"{', '.join(missing)} not set: the results file records the workflow "
            "run it came from, so run it in .github/workflows/scale-evidence.yml "
            "or set them"
        )
    return {
        "run_id": int(environ["GITHUB_RUN_ID"]),
        "run_url": (
            f"{environ['GITHUB_SERVER_URL']}/{environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{environ['GITHUB_RUN_ID']}"
        ),
        "run_attempt": int(environ.get("GITHUB_RUN_ATTEMPT") or 1),
        "harness_commit": environ["GITHUB_SHA"],
        "dispatched_at": dispatched_at,
    }


def cpu_model(cpuinfo: str) -> str:
    for line in cpuinfo.splitlines():
        name, _, value = line.partition(":")
        if name.strip() == "model name" and value.strip():
            return value.strip()
    raise Refused("/proc/cpuinfo names no CPU model")


def runner_image(environ: Mapping[str, str]) -> str:
    """``ImageOS/ImageVersion``, as GitHub's runner image sets them."""
    image, version = environ.get("ImageOS", ""), environ.get("ImageVersion", "")
    if not image or not version:
        raise Refused(
            "ImageOS and ImageVersion are not set: not a GitHub-hosted runner"
        )
    return f"{image}/{version}"


def isolation_problems(
    split: Mapping[str, str], siblings: Mapping[str, str]
) -> List[str]:
    """The layout's refusals before anything is measured: every container on
    one sampled hardware thread, no part of the system under test on a thread
    of the load generator's physical core, and the sampler off its thread."""
    problems = se.split_problems(split)
    problems += se.siblings_problems(siblings)
    if problems:
        return problems
    generator = se._cpus(split["generator"])
    core = se._core(siblings, generator)
    for service in se.SYSTEM_UNDER_TEST:
        on = sorted(core & se._cpus(split[service]))
        if on:
            problems.append(
                f"{se.NAMES[service]} is on cpu{on[0]}, a hardware thread of the load "
                f"generator's core ({se._threads_text(core)}, as the VM reports it)"
            )
    if generator & se._cpus(split["pg_sampler"]):
        problems.append(
            "the database sampler is on the load generator's hardware thread"
        )
    return problems


def observation(
    *,
    scenario: str,
    step: int,
    rate: int,
    time_unit_ms: int,
    dropped: int,
    variants: Sequence[str],
    split: Mapping[str, str],
    cpu: CpuWindow,
    pg: PgWindow,
    pg_needed: int,
    fallback: int,
    server: Tuple[int, int],
    assignments: Tuple[int, int],
    recovery: Tuple[int, int] = (0, 0),
) -> Dict[str, Any]:
    """A step's observation, as scale_evidence.summarise_step reads it. The
    window is sampled only when the CPU samplers read it at both ends and the
    database sampler gave at least ``pg_needed`` samples in it."""
    sampled = cpu.sampled and pg.samples >= pg_needed
    return {
        "scenario": scenario,
        "step": step,
        "rate": rate,
        "time_unit_ms": time_unit_ms,
        "dropped": dropped,
        "variants": list(variants),
        "cpu_split": dict(split),
        "sampled": sampled,
        "samples": cpu.samples,
        "cpu_permille": {
            name: cpu.cpu.get(name, 0) if sampled else 0 for name in se.CONTAINERS
        },
        "threads": cpu.threads if sampled else _zero_threads(),
        "pg": {
            "active_backend_samples_by_count": list(pg.by_count) if sampled else [],
            "io_wait_observations": pg.io_wait_observations if sampled else 0,
        },
        "fallback_log_lines": fallback,
        "server_count": server[0],
        "server_499": server[1],
        "scrape_recovery_bound_s": recovery[0],
        "scrape_recovery_ms": recovery[1],
        "assignments_before": assignments[0],
        "assignments_after": assignments[1],
    }


def pg_needed(method: Mapping[str, Any]) -> int:
    """The database samples a step's window must hold: the method's
    min_sample_permille of the one every PG_SAMPLE_S seconds the window spans."""
    measure = se.ranges(method)["measure_s"]
    return -(
        -method["validity"]["min_sample_permille"] * measure // (1000 * PG_SAMPLE_S)
    )


def smoke_problems(steps: Sequence[Mapping[str, Any]]) -> List[str]:
    """In smoke mode every step must be VALID and every measured request must
    have done its work (no wrong status, body, timeout or connection error).
    Its latency and drops are recorded and printed, never judged here: no
    pull-request gate asserts a wall-clock timing (PLAN 3.9), and the mix's
    batch requests alone can put the p99 over the publication limit."""
    problems = []
    for step in steps:
        if not step["valid"]:
            problems.append(f"step {step['step']}: INVALID ({step['invalid_reason']})")
        elif step["errors"]:
            problems.append(
                f"step {step['step']}: {step['errors']} of {step['measured']} "
                "requests failed"
            )
    return problems


# ---------------------------------------------------------------------------
# The outside world, replaceable in tests
# ---------------------------------------------------------------------------
def capture(argv: Sequence[str], env: Mapping[str, str]) -> subprocess.CompletedProcess:
    """Run *argv* with exactly *env*, stdin from /dev/null, output captured."""
    return subprocess.run(
        list(argv),
        env=dict(env),
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=900,
    )


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _pin(cpu: int) -> None:
    os.sched_setaffinity(0, {cpu})


@dataclasses.dataclass
class Io:
    run: Callable[[Sequence[str], Mapping[str, str]], subprocess.CompletedProcess] = (
        capture
    )
    read: Callable[[str], str] = read_text
    http: Callable[..., tuple] = ss.http_request
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000)
    sleep: Callable[[float], None] = time.sleep
    environ: Mapping[str, str] = dataclasses.field(default_factory=lambda: os.environ)
    pin: Callable[[int], None] = _pin
    cpu_count: Callable[[], Optional[int]] = os.cpu_count
    uname: Callable[[], Any] = os.uname
    linux: bool = sys.platform.startswith("linux")
    out: Any = dataclasses.field(default_factory=lambda: sys.stdout)

    def say(self, text: str) -> None:
        print(text, file=self.out, flush=True)


class CpuSampler:
    """Every CPU_SAMPLE_S seconds: /proc/stat, and each registered container's
    cpu.stat, as one Reading. ``tick`` is one reading; ``start`` runs it on a
    thread of this process (which is pinned to DRIVER_THREAD)."""

    def __init__(self, io: Io) -> None:
        self.io = io
        self.paths: Dict[str, str] = {}
        self.readings: List[Reading] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def register(self, name: str, directory: str) -> None:
        with self._lock:
            self.paths[name] = directory

    def unregister(self, name: str) -> None:
        with self._lock:
            self.paths.pop(name, None)

    def tick(self) -> None:
        at = self.io.now_ms()
        try:
            threads: Optional[Dict[str, Tuple[int, ...]]] = proc_stat(
                self.io.read("/proc/stat")
            )
        except (OSError, Refused):
            threads = None
        with self._lock:
            paths = dict(self.paths)
        usage: Dict[str, Optional[int]] = {}
        for name, directory in paths.items():
            try:
                usage[name] = cpu_usage_usec(self.io.read(f"{directory}/cpu.stat"))
            except (OSError, Refused):
                usage[name] = None
        with self._lock:
            self.readings.append(Reading(at, threads, usage))

    def between(self, start_ms: int, end_ms: int) -> List[Reading]:
        with self._lock:
            return [r for r in self.readings if start_ms <= r.at_ms <= end_ms]

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                began = time.monotonic()
                self.tick()
                self._stop.wait(max(0.0, CPU_SAMPLE_S - (time.monotonic() - began)))

        self._thread = threading.Thread(target=loop, name="cpu-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# The stack, as Docker reports it
# ---------------------------------------------------------------------------
class Docker:
    """The docker calls ``run`` makes: each with the harness's child
    environment (scripts/scale_stack.py child_env), compose ones through its
    argv builder."""

    def __init__(self, work: Path, io: Io) -> None:
        self.work = work
        self.io = io

    def call(
        self, argv: Sequence[str], extra: Optional[Mapping[str, str]] = None, ok=(0,)
    ) -> str:
        result = self.io.run(list(argv), ss.child_env(extra))
        if result.returncode not in ok:
            error = (result.stderr or b"").decode("utf-8", "replace").strip()
            raise Failed(
                f"{' '.join(argv[:3])} ... exited {result.returncode}: {error[-300:]}"
            )
        return (result.stdout or b"").decode("utf-8", "replace")

    def output(self, argv: Sequence[str]) -> str:
        """stdout and stderr together (``docker logs`` splits the container's)."""
        result = self.io.run(list(argv), ss.child_env())
        if result.returncode != 0:
            raise Failed(f"{' '.join(argv[:3])} ... exited {result.returncode}")
        return (result.stdout or b"").decode("utf-8", "replace") + (
            result.stderr or b""
        ).decode("utf-8", "replace")

    def service(self, name: str) -> str:
        ids = self.call(ss.compose_argv(self.work, "ps", "-q", name)).split()
        if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{12,64}", ids[0]):
            raise Refused(
                f"the {ss.PROJECT} project has {len(ids)} {name} containers: run "
                f"scripts/scale_stack.py up --work {self.work} first"
            )
        return ids[0]

    def inspect(self, ref: str, kind: str = "container") -> Dict[str, Any]:
        argv = (
            ["docker", "image", "inspect", ref]
            if kind == "image"
            else [
                "docker",
                "inspect",
                ref,
            ]
        )
        data = json.loads(self.call(argv))
        if not isinstance(data, list) or len(data) != 1:
            raise Failed(f"docker {kind} inspect {ref} did not answer one object")
        return data[0]


def container_env(inspected: Mapping[str, Any]) -> Dict[str, str]:
    pairs = (item.partition("=") for item in inspected["Config"].get("Env") or [])
    return {name: value for name, _, value in pairs}


def cpuset(inspected: Mapping[str, Any], name: str) -> str:
    value = str(inspected["HostConfig"].get("CpusetCpus") or "")
    if not value:
        raise Refused(f"{name} is not pinned to a CPU (docker inspect shows no cpuset)")
    return value


def image_record(image: Mapping[str, Any], app: bool) -> Dict[str, Any]:
    """An image's digest, ID and, for the app images, revision and platform."""
    digests = [d.split("@", 1)[1] for d in image.get("RepoDigests") or [] if "@" in d]
    if len(set(digests)) != 1:
        raise Refused(f"the image {image.get('Id')} has {len(set(digests))} digests")
    out: Dict[str, Any] = {"digest": digests[0], "image_id": image["Id"]}
    if app:
        labels = image.get("Config", {}).get("Labels") or {}
        out["revision"] = labels.get("org.opencontainers.image.revision", "")
        out["platform"] = f"{image.get('Os')}/{image.get('Architecture')}"
    return out


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Stack:
    ids: Dict[str, str]
    split: Dict[str, str]
    network: str
    postgres_image: str
    api_image: str
    system_under_test: Dict[str, Any]
    environment: Dict[str, str]


class Measurement:
    """One repetition of one request type on the harness's own stack."""

    def __init__(
        self,
        work: Path,
        out: Path,
        method: Dict[str, Any],
        scenario: str,
        rep: int,
        io: Io,
        sampling: bool = True,
        sampler: Optional[CpuSampler] = None,
    ) -> None:
        self.work = work
        self.out = out
        self.method = method
        self.scenario = scenario
        self.rep = rep
        self.io = io
        self.sampling = sampling
        self.docker = Docker(work, io)
        self.sampler = sampler or CpuSampler(io)
        self.values = ss.own_env(work)
        self.fixtures = json.loads((work / "fixtures.json").read_text(encoding="utf-8"))
        self.variants = [v["name"] for v in self.fixtures["experiment"]["variants"]]
        self.started: List[str] = []

    # -- facts about the machine and the stack -----------------------------------
    def host(self, generator: str) -> Dict[str, Any]:
        siblings = {
            thread: self.io.read(
                f"/sys/devices/system/cpu/{thread}/topology/thread_siblings_list"
            ).strip()
            for thread in se.THREADS
        }
        uname = self.io.uname()
        return {
            "runner_image": runner_image(self.io.environ),
            "cpu_model": cpu_model(self.io.read("/proc/cpuinfo")),
            "kernel": uname.release,
            "nproc": self.io.cpu_count() or 0,
            "machine": uname.machine,
            "thread_siblings": siblings,
            "generator_core_exclusive": se._core(siblings, se._cpus(generator))
            == se._cpus(generator),
        }

    def git(self, *args: str) -> bytes:
        result = self.io.run(["git", "-C", str(ROOT), *args], ss.child_env())
        if result.returncode != 0:
            raise Refused(
                f"git {' '.join(args)} failed: fetch the measured release's tag first "
                "(git fetch --no-tags --depth=1 origin "
                "+refs/tags/vX.Y.Z:refs/tags/vX.Y.Z)"
            )
        return result.stdout or b""

    def stack(self) -> Stack:
        ids = {name: self.docker.service(name) for name in STACK_SERVICES}
        inspected = {name: self.docker.inspect(ids[name]) for name in STACK_SERVICES}
        split = {name: cpuset(inspected[name], name) for name in STACK_SERVICES}
        networks = sorted(inspected["web"]["NetworkSettings"]["Networks"])
        if len(networks) != 1:
            raise Refused(
                f"web is on {len(networks)} networks; expected the project's one"
            )
        images = {
            name: self.docker.inspect(inspected[name]["Image"], "image")
            for name in STACK_SERVICES
        }
        environment = container_env(inspected["api"])
        compose_bytes = COMPOSE_FILE.read_bytes()
        version = compose_version(compose_bytes.decode("utf-8"))
        profile, api_version = image_release(inspected["api"]["Config"]["Image"], "api")
        web_profile, web_version = image_release(
            inspected["web"]["Config"]["Image"], "web"
        )
        if {api_version, web_version} != {version} or web_profile != profile:
            raise Refused(
                f"the stack runs api {api_version} and web {web_version}; compose.yml "
                f"names {version}"
            )
        tag = f"v{version}"
        tag_commit = (
            self.git("rev-parse", "--verify", f"{tag}^{{commit}}").decode().strip()
        )
        tag_blob = self.git("show", f"{tag}:deploy/compose/compose.yml")
        app = {name: image_record(images[name], True) for name in ("api", "web")}
        sut = {
            "version": version,
            "profile": profile,
            "environment": environment.get("ENVIRONMENT", ""),
            "release_tag": tag,
            "tag_commit": tag_commit,
            "images": {
                "api": app["api"],
                "web": app["web"],
                "postgres": image_record(images["postgres"], False),
                "redis": image_record(images["redis"], False),
            },
            "compose_sha256": hashlib.sha256(compose_bytes).hexdigest(),
            "compose_tag_blob_sha256": hashlib.sha256(tag_blob).hexdigest(),
            "web_concurrency_expected": self.method["expected_web_concurrency"],
            "web_concurrency_observed": web_workers(
                self.docker.call(["docker", "top", ids["api"]])
            ),
            "sdk_rate_limit_per_minute": 0,
            "postgres_settings": {},
            "cpu_split": {},
        }
        problems = []
        if self.method["mode"] == "publication":
            problems += release_problems(
                version,
                compose_bytes,
                tag_blob,
                tag_commit,
                {name: app[name]["revision"] for name in ("api", "web")},
            )
        if sut["environment"] != "production":
            problems.append(
                f"environment is {sut['environment'] or 'unset'}: the stack must run as "
                "production (schedulers and rate limiter on)"
            )
        expected = self.method["expected_web_concurrency"]
        if sut["web_concurrency_observed"] != expected:
            problems.append(
                f"expected {expected} workers (config), observed "
                f"{sut['web_concurrency_observed']}"
            )
        if problems:
            raise Refused("; ".join(problems))
        return Stack(
            ids=ids,
            split=split,
            network=networks[0],
            postgres_image=inspected["postgres"]["Image"],
            api_image=inspected["api"]["Image"],
            system_under_test=sut,
            environment=environment,
        )

    # -- the database sampler --------------------------------------------------------
    def start_sampler(self, stack: Stack) -> Tuple[str, str]:
        """The database sampler: a psql in the stack's own postgres image, on
        SAMPLER_THREAD, on the project's network. Returns (id, cpuset)."""
        sql = self.work / "pg_sampler.sql"
        sql.write_text(PG_SAMPLER_SQL, encoding="utf-8")
        sql.chmod(0o644)
        postgres_env = container_env(self.docker.inspect(stack.ids["postgres"]))
        self.docker.call(["docker", "rm", "-f", SAMPLER_NAME], ok=(0, 1))
        self.docker.call(
            [
                "docker",
                "run",
                "-d",
                "--name",
                SAMPLER_NAME,
                "--cpuset-cpus",
                str(SAMPLER_THREAD),
                "--network",
                stack.network,
                "-e",
                "PGHOST=postgres",
                "-e",
                f"PGUSER={postgres_env['POSTGRES_USER']}",
                "-e",
                f"PGDATABASE={postgres_env['POSTGRES_DB']}",
                "-e",
                f"PGAPPNAME={SAMPLER_APP}",
                # The value comes from this call's own environment, not argv.
                "-e",
                "PGPASSWORD",
                "-v",
                f"{sql}:/sampler.sql:ro",
                "--entrypoint",
                "psql",
                stack.postgres_image,
                "-X",
                "-q",
                "-A",
                "-t",
                "-v",
                "ON_ERROR_STOP=1",
                "-f",
                "/sampler.sql",
            ],
            {"PGPASSWORD": postgres_env["POSTGRES_PASSWORD"]},
        )
        self.started.append(SAMPLER_NAME)
        inspected = self.docker.inspect(SAMPLER_NAME)
        return inspected["Id"], cpuset(inspected, "pg_sampler")

    def psql(self, sql: str) -> str:
        """One query through the sampler's container (its own hardware thread)."""
        return self.docker.call(
            [
                "docker",
                "exec",
                SAMPLER_NAME,
                "psql",
                "-X",
                "-A",
                "-t",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                sql,
            ]
        ).strip()

    def counts(self) -> Dict[str, int]:
        sql = COUNTS_SQL.format(
            schema=ss.SCHEMA,
            flags=ss.FLAG_PREFIX.replace("_", "\\_"),
            experiment=ss.EXPERIMENT_KEY,
        )
        values = self.psql(sql).split("|")
        if len(values) != len(COUNT_KEYS) or not all(v.isdigit() for v in values):
            raise Failed(f"the table counts came back as {values!r}")
        return dict(zip(COUNT_KEYS, (int(v) for v in values)))

    def assignments(self) -> int:
        """scale_ab's assignments, before and after each step (V20)."""
        sql = ASSIGNMENTS_SQL.format(schema=ss.SCHEMA, experiment=ss.EXPERIMENT_KEY)
        value = self.psql(sql)
        if not value.isdigit():
            raise Failed(f"the assignments count came back as {value!r}")
        return int(value)

    def settings(self) -> Dict[str, str]:
        sql = (
            "SELECT concat_ws('|', "
            + ", ".join(f"current_setting('{name}')" for name in SETTINGS)
            + ")"
        )
        values = self.psql(sql).split("|")
        if len(values) != len(SETTINGS):
            raise Failed(f"the PostgreSQL settings came back as {values!r}")
        return dict(zip(SETTINGS, values))

    def metrics(self, stack: Stack) -> str:
        """The API's /metrics, asked from inside its container with its own token."""
        return self.docker.call(
            [
                "docker",
                "exec",
                stack.ids["api"],
                "sh",
                "-c",
                'curl -fsS --max-time 30 -H "Authorization: Bearer $METRICS_TOKEN" '
                "http://localhost:8000/metrics",
            ]
        )

    def metrics_after_overload(self, stack: Stack, started: int) -> Tuple[str, int]:
        """The after-step scrape of a CONFIRMING step whose first scrape (begun
        at ``started``, ms) timed out: poll until the API answers again, for at
        most RECOVERY_BOUND_S from ``started``, and return the scrape and the
        milliseconds that took. Past the bound the run fails."""
        self.io.say(
            f"the API did not answer /metrics after a confirming step: waiting up "
            f"to {RECOVERY_BOUND_S} s for it"
        )
        while True:
            if self.io.now_ms() - started >= RECOVERY_BOUND_S * 1000:
                raise Failed(
                    f"the API did not answer /metrics within {RECOVERY_BOUND_S} s "
                    "of a confirming step"
                )
            try:
                text = self.metrics(stack)
            except Failed as error:
                # Only "still slow" is waited out. A refused connection (the
                # API crashed), a docker exec error or anything else is a
                # failure the poll must not absorb.
                if f"exited {CURL_TIMED_OUT}:" not in str(error):
                    raise
                self.io.sleep(RECOVERY_POLL_S)
                continue
            took = self.io.now_ms() - started
            if took > RECOVERY_BOUND_S * 1000:
                raise Failed(
                    f"the API answered /metrics after {took} ms, past the "
                    f"{RECOVERY_BOUND_S} s bound"
                )
            return text, max(took, 1)

    def ruleset_facts(self) -> Tuple[str, int]:
        """The ruleset's ETag (the ruleset scenario sends it back) and the
        rate limit the API states, from one request through nginx."""
        base, host = ss._base(self.values)
        status, headers, _ = self.io.http(
            "GET",
            base + "/api/v1/sdk/ruleset",
            host,
            None,
            {"X-API-Key": self.fixtures["api_keys"]["scale-server"]["key"]},
        )
        etag = headers.get("etag", "")
        limit = headers.get("x-ratelimit-limit", "")
        if status != 200 or not etag or not limit.isdigit():
            raise Refused(
                f"GET /api/v1/sdk/ruleset answered {status or 'nothing'} (ETag "
                f"{'present' if etag else 'absent'}, X-RateLimit-Limit "
                f"{limit or 'absent'}): the stack is not as measured"
            )
        return etag, int(limit)

    # -- one k6 process ------------------------------------------------------------
    def k6(
        self,
        stack: Stack,
        directory: Path,
        *,
        scenario: str,
        step: int,
        milli_rps: int,
        seconds: int,
        settle_s: int,
        etag: str,
        vus: Optional[int] = None,
        base: Optional[str] = None,
    ) -> Tuple[int, str]:
        """Start k6 for one step, detached. Returns (rate, time unit)."""
        rate, unit = se.rate_pair(milli_rps)
        directory.mkdir(parents=True, exist_ok=True)
        variables = {
            "SCALE_SCENARIO": scenario,
            "SCALE_RATE": str(rate),
            "SCALE_TIME_UNIT_MS": str(unit),
            "SCALE_DURATION_S": str(seconds),
            "SCALE_SETTLE_MS": str(settle_s * 1000),
            "SCALE_VUS": str(vus or se.vus(self.method, milli_rps)),
            "SCALE_TIMEOUT_MS": str(self.method["timeout_ms"]),
            "SCALE_STEP": str(step),
            "SCALE_BASE_URL": base or f"http://web:{WEB_PORT}",
            "SCALE_HOST": ss._base(self.values)[1],
            "SCALE_ETAG": etag,
            "SCALE_EVENT": ss.EVENT_NAME,
            "SCALE_MIX": json.dumps(self.method["smoke"]["mix"]),
            "SCALE_FIXTURES": "/fixtures.json",
            "SCALE_SUMMARY": "/out/summary.json",
        }
        self.docker.call(["docker", "rm", "-f", GENERATOR_NAME], ok=(0, 1))
        argv = [
            "docker",
            "run",
            "-d",
            "--name",
            GENERATOR_NAME,
            "--cpuset-cpus",
            str(GENERATOR_THREAD),
            "--network",
            stack.network,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{K6_DIR}:/scale:ro",
            "-v",
            f"{self.work / 'fixtures.json'}:/fixtures.json:ro",
            "-v",
            f"{directory}:/out",
            K6_IMAGE,
            "run",
            "--quiet",
            "--no-usage-report",
            "--out",
            "csv=/out/k6.csv",
        ]
        for name, value in variables.items():
            argv += ["-e", f"{name}={value}"]
        argv.append("/scale/scenarios.js")
        self.docker.call(argv)
        self.started.append(GENERATOR_NAME)
        return rate, unit

    def finish_k6(self, directory: Path) -> int:
        """Wait for k6, keep its log beside the step, and remove it."""
        code = self.docker.call(["docker", "wait", GENERATOR_NAME]).strip()
        (directory / "k6.log").write_text(
            self.docker.output(["docker", "logs", GENERATOR_NAME]), encoding="utf-8"
        )
        self.docker.call(["docker", "rm", "-f", GENERATOR_NAME])
        self.started.remove(GENERATOR_NAME)
        if not code.isdigit():
            raise Failed(f"docker wait answered {code!r}")
        return int(code)

    # -- one step ------------------------------------------------------------------
    def step(
        self,
        stack: Stack,
        number: int,
        milli_rps: int,
        etag: str,
        confirming: bool = False,
    ) -> Tuple[Dict[str, Any], List[Dict[str, str]], Dict[str, str]]:
        """One step. ``confirming`` is True for a step that follows one that did
        not keep up: it runs past saturation by design, so a timed-out
        after-step scrape gets the bounded recovery poll (and the step is
        recorded as failing). Anywhere else a timed-out scrape raises Failed."""
        window = se.ranges(self.method)
        directory = self.out / f"step-{number:02d}"
        before = self.metrics(stack)
        restarts_before = self.docker.inspect(stack.ids["api"])["RestartCount"]
        assignments_before = self.assignments()
        t0 = self.io.now_ms()
        rate, unit = self.k6(
            stack,
            directory,
            scenario=self.scenario,
            step=number,
            milli_rps=milli_rps,
            seconds=window["settle_s"] + window["measure_s"],
            settle_s=window["settle_s"],
            etag=etag,
        )
        generator = self.docker.inspect(GENERATOR_NAME)
        generator_split = cpuset(generator, "the load generator")
        pid = int(generator["State"].get("Pid") or 0)
        if self.sampling and pid:
            self.sampler.register(
                "generator", cgroup_dir(self.io.read(f"/proc/{pid}/cgroup"))
            )
        try:
            code = self.finish_k6(directory)
        finally:
            self.sampler.unregister("generator")
        self.io.sleep(DRAIN_S)
        t1 = self.io.now_ms()
        recovery = (0, 0)
        scrape_began = self.io.now_ms()
        try:
            after = self.metrics(stack)
        except Failed as error:
            if not confirming or f"exited {CURL_TIMED_OUT}:" not in str(error):
                raise
            after, took = self.metrics_after_overload(stack, scrape_began)
            recovery = (RECOVERY_BOUND_S, took)
            # the API answered again, but a container that restarted (it is
            # `restart: unless-stopped`) crashed under the load: not "slow"
            restarts = self.docker.inspect(stack.ids["api"])["RestartCount"]
            if restarts != restarts_before:
                raise Failed(
                    f"the API container restarted during step {number} "
                    f"(RestartCount {restarts_before} -> {restarts}): it crashed "
                    "rather than ran slow"
                )
        assignments_after = self.assignments()
        if code not in (0, K6_THRESHOLDS_FAILED):
            raise Failed(
                f"k6 exited {code} at step {number}: see {directory / 'k6.log'}"
            )
        k6_csv = directory / "k6.csv"
        records = read_records(k6_csv.read_text(encoding="utf-8"))
        k6_csv.unlink()
        rows = to_rows(records)
        start = scenario_start(records)
        summary = k6_summary((directory / "summary.json").read_text(encoding="utf-8"))
        measured = measured_count(rows, window["settle_s"])
        problems = k6_counts_problems(summary, len(records), measured, code)
        if problems:
            raise Failed(f"step {number}: " + "; ".join(problems))
        begin = start + window["settle_s"] * 1000
        end = start + (window["settle_s"] + window["measure_s"]) * 1000
        split = {**stack.split, "generator": generator_split}
        cpu = cpu_window(self.sampler.between(begin, end), begin, end, se.CONTAINERS)
        if not self.sampling:
            cpu = dataclasses.replace(cpu, sampled=False)
        pg = pg_window(
            pg_samples(self.docker.output(["docker", "logs", SAMPLER_NAME])), begin, end
        )
        since = ["--since", utc(t0), "--until", utc(t1)]
        web = self.docker.output(["docker", "logs", *since, stack.ids["web"]])
        api = self.docker.output(["docker", "logs", *since, stack.ids["api"]])
        seen = observation(
            scenario=self.scenario,
            step=number,
            rate=rate,
            time_unit_ms=unit,
            dropped=summary["dropped"],
            variants=self.variants,
            split=split,
            cpu=cpu,
            pg=pg,
            pg_needed=pg_needed(self.method),
            fallback=fallback_lines(api),
            server=access_counts(web),
            assignments=(assignments_before, assignments_after),
            recovery=recovery,
        )
        for name, text in (
            ("rows.csv", rows_text(rows)),
            ("observation.json", json.dumps(seen, indent=1) + "\n"),
            ("metrics-before.txt", before),
            ("metrics-after.txt", after),
        ):
            (directory / name).write_text(text, encoding="utf-8")
        record, messages = se.summarise_step(
            self.method, self.scenario, rows, seen, before, after
        )
        (directory / "step.json").write_text(
            json.dumps(record, indent=1) + "\n", "utf-8"
        )
        self.io.say(step_line(record, summary["measured_p99_us"]))
        for message in messages:
            self.io.say(f"  {message}")
        return record, rows, split

    # -- the whole repetition -----------------------------------------------------
    def warm_up(self, stack: Stack, milli_rps: int, etag: str) -> None:
        window = se.ranges(self.method)
        if not window["warmup_s"]:
            return
        directory = self.out / "warm-up"
        self.k6(
            stack,
            directory,
            scenario=self.scenario,
            step=0,
            milli_rps=milli_rps,
            seconds=window["warmup_s"],
            settle_s=0,
            etag=etag,
        )
        code = self.finish_k6(directory)
        if code not in (0, K6_THRESHOLDS_FAILED):
            raise Failed(f"the warm-up's k6 exited {code}: see {directory / 'k6.log'}")
        for name in ("k6.csv", "summary.json"):
            if (directory / name).exists():
                (directory / name).unlink()
        self.io.say(
            f"warm-up: {window['warmup_s']} s at {se.rps(milli_rps)}, not counted"
        )

    def measure(self) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
        """The repetition: its results file, and every step's rows (with their
        step number) for the job's rows file."""
        method = self.method
        dispatched = (
            self.io.environ.get("SCALE_DISPATCHED_AT")
            or utc(self.io.now_ms())[:19] + "Z"
        )
        run = run_record(self.io.environ, dispatched)
        stack = self.stack()
        intended = {
            **stack.split,
            "pg_sampler": str(SAMPLER_THREAD),
            "generator": str(GENERATOR_THREAD),
        }
        host = self.host(str(GENERATOR_THREAD))
        problems = isolation_problems(intended, host["thread_siblings"])
        if host["nproc"] != len(se.THREADS):
            problems.append(
                f"the VM reports {host['nproc']} vCPUs; the method's layout is for "
                f"{len(se.THREADS)}"
            )
        if problems:
            raise Refused("; ".join(problems))
        self.io.pin(DRIVER_THREAD)
        sampler_id, sampler_split = self.start_sampler(stack)
        stack.split["pg_sampler"] = sampler_split
        sut = stack.system_under_test
        sut["postgres_settings"] = self.settings()
        counts_before = self.counts()
        problems = se.precondition_problems(method, counts_before)
        if problems:
            raise Refused("; ".join(problems))
        etag, limit = self.ruleset_facts()
        sut["sdk_rate_limit_per_minute"] = limit
        self.docker.call(["docker", "pull", "--quiet", K6_IMAGE])
        version = k6_version(
            self.docker.call(["docker", "run", "--rm", K6_IMAGE, "version"])
        )
        api_machine = self.docker.call(
            ["docker", "exec", stack.ids["api"], "uname", "-m"]
        ).strip()
        if self.sampling:
            for name in (*STACK_SERVICES, "pg_sampler"):
                ident = sampler_id if name == "pg_sampler" else stack.ids[name]
                pid = int(self.docker.inspect(ident)["State"].get("Pid") or 0)
                self.sampler.register(
                    name, cgroup_dir(self.io.read(f"/proc/{pid}/cgroup"))
                )
            self.sampler.start()
        rates = se.step_rates(method, self.scenario)
        self.warm_up(stack, rates[0], etag)
        steps: List[Dict[str, Any]] = []
        all_rows: List[Dict[str, str]] = []
        status = "complete"
        failing = 0
        split: Optional[Dict[str, str]] = None
        try:
            for number, milli in enumerate(rates, 1):
                self.io.sleep(se.ranges(method)["gap_s"])
                record, rows, step_split = self.step(
                    stack, number, milli, etag, confirming=failing >= 1
                )
                if split is None:
                    split = step_split
                elif step_split != split:
                    raise Failed(
                        f"step {number} ran on the split {step_split}; step 1 on {split}"
                    )
                steps.append(record)
                all_rows += [{"step": str(number), **row} for row in rows]
                failing = 0 if record["keeps_up"] else failing + 1
                if failing >= method["stop_after_failing_steps"]:
                    break
        except (Failed, Refused, KeyboardInterrupt) as stopped:
            if not steps:
                raise
            status = "incomplete"
            self.io.say(f"stopped after step {len(steps)}: {stopped or 'interrupted'}")
        finally:
            self.sampler.stop()
        counts_after = self.counts()
        assert split is not None
        sut["cpu_split"] = dict(split)
        host["generator_core_exclusive"] = se._core(
            host["thread_siblings"], se._cpus(split["generator"])
        ) == se._cpus(split["generator"])
        rep = {
            "rep": self.rep,
            "host": host,
            "api_machine": api_machine,
            "counts_before": counts_before,
            "counts_after": counts_after,
            "steps": steps,
            **se.assess_rep(method, steps, split),
        }
        results = {
            "schema": se.SCHEMA,
            "status": status,
            "run": run,
            "system_under_test": sut,
            "generator": {
                "tool": K6_TOOL,
                "version": version,
                "image_digest": K6_IMAGE.split("@", 1)[1],
                "timeout_ms": method["timeout_ms"],
                "max_vus_per_rps": method["max_vus_per_rps"],
                "min_vus": method["min_vus"],
            },
            "data": {"preload": dict(method["preload"])},
            "method": method,
            "scenarios": [
                {
                    "name": self.scenario,
                    "reps": [rep],
                    "summary": se.summarise_scenario(method, [rep]),
                }
            ],
        }
        return results, all_rows

    def cleanup(self) -> None:
        for name in reversed(self.started):
            self.io.run(["docker", "rm", "-f", name], ss.child_env())
        self.started.clear()


def write_job(
    out: Path, results: Mapping[str, Any], rows: Sequence[Mapping[str, str]]
) -> None:
    """The job's two uploaded files: results.json, and rows.csv.gz (every
    step's rows, each with its step number)."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(se.dumps(results), encoding="utf-8")
    data = rows_text(rows, lead=("step",)).encode("utf-8")
    (out / "rows.csv.gz").write_bytes(gzip.compress(data, mtime=0))


def cmd_run(
    work: Path,
    out: Path,
    scenario: str,
    rep: int,
    mode: str,
    sampling: bool,
    io: Io,
    sampler: Optional[CpuSampler] = None,
) -> int:
    method = se.load_config(se.CONFIG, mode)
    if mode == "smoke" and scenario != se.SMOKE_ONLY:
        raise Refused(f"smoke mode runs {se.SMOKE_ONLY}, not {scenario!r}")
    if mode == "publication" and scenario not in se.SCENARIOS:
        raise Refused(f"{scenario!r} is not one of {' '.join(se.SCENARIOS)}")
    if not 1 <= rep <= 99:
        raise Refused(f"--rep {rep} is not from 1 to 99")
    if not io.linux:
        raise Refused("run measures on Linux only: it reads /proc and cgroup v2")
    measurement = Measurement(work, out, method, scenario, rep, io, sampling, sampler)
    try:
        results, rows = measurement.measure()
    finally:
        measurement.cleanup()
    problems = se.results_problems(results)
    write_job(out, results, rows)
    if problems:
        raise Failed("the results file is refused: " + "; ".join(problems[:10]))
    steps = results["scenarios"][0]["reps"][0]["steps"]
    if results["status"] != "complete":
        io.say("the run was interrupted: its results are incomplete")
        return 1
    if mode == "smoke":
        failed = smoke_problems(steps)
        for problem in failed:
            io.say(f"smoke: {problem}")
        return 1 if failed else 0
    rep_record = results["scenarios"][0]["reps"][0]
    io.say(
        f"{scenario} run {rep}: knee step {rep_record['knee_step']}, first failing "
        f"step {rep_record['first_failing_step']} ({rep_record['first_failing_reason']}), "
        f"bottleneck {rep_record['bottleneck']}"
    )
    return 0 if rep_record["valid"] else 1


# ---------------------------------------------------------------------------
# calibrate (V18)
# ---------------------------------------------------------------------------
def calibration_p99(records: Sequence[Mapping[str, str]]) -> Tuple[int, int, int]:
    """(p99 in us of the answered requests, how many answered, how many not)."""
    answered = sorted(
        int(r["latency_us"]) for r in records if r["status"] == "200" and not r["error"]
    )
    return se.percentile(answered, 99), len(answered), len(records) - len(answered)


def calibration_problems(p99_us: int, answered: int) -> List[str]:
    if answered == 0:
        return ["calibration: no request was answered"]
    if p99_us < CALIBRATION_MIN_P99_US:
        return [
            f"calibration: planted {CALIBRATION_STALL_S * 1000} ms stall over "
            f"{100 * CALIBRATION_STALL_S // CALIBRATION_PERIOD_S}% of the window, "
            f"measured p99 {se._ms(p99_us)}: this generator does not count waiting "
            "requests"
        ]
    return []


def calibration_exit(problems: Sequence[str], report_only: bool) -> int:
    """The calibration's exit status. A pull request only reads the result
    (report_only): a timing never fails a pull request (PLAN 3.9). A dispatch
    fails on a problem, and the measure workflow's ``merge`` needs that
    success, so a calibration that fails refuses to publish."""
    return 1 if problems and not report_only else 0


def cmd_calibrate(work: Path, out: Path, io: Io, report_only: bool = False) -> int:
    """k6 against a stub that stalls 2 s in every 10 s (V18): the p99 must
    show the stall, or the generator does not count waiting requests."""
    method = se.load_config(se.CONFIG, "smoke")
    measurement = Measurement(work, out, method, "calibration", 1, io, sampling=False)
    try:
        stack = measurement.stack()
        measurement.docker.call(["docker", "rm", "-f", STUB_NAME], ok=(0, 1))
        measurement.docker.call(
            [
                "docker",
                "run",
                "-d",
                "--name",
                STUB_NAME,
                "--cpuset-cpus",
                "0",
                "--network",
                stack.network,
                "--entrypoint",
                "python",
                stack.api_image,
                "-c",
                STUB_CODE,
            ]
        )
        measurement.started.append(STUB_NAME)
        for _ in range(30):
            if "ready" in measurement.docker.output(["docker", "logs", STUB_NAME]):
                break
            io.sleep(1)
        else:
            raise Failed("the calibration stub did not start")
        directory = out / "calibration"
        measurement.docker.call(["docker", "pull", "--quiet", K6_IMAGE])
        measurement.k6(
            stack,
            directory,
            scenario="calibration",
            step=0,
            milli_rps=CALIBRATION_MILLI_RPS,
            seconds=CALIBRATION_S,
            settle_s=0,
            etag="",
            vus=CALIBRATION_VUS,
            base=f"http://{STUB_NAME}:{STUB_PORT}",
        )
        code = measurement.finish_k6(directory)
    finally:
        measurement.cleanup()
    if code not in (0, K6_THRESHOLDS_FAILED):
        raise Failed(f"the calibration's k6 exited {code}: see {directory / 'k6.log'}")
    records = read_records((directory / "k6.csv").read_text(encoding="utf-8"))
    (directory / "k6.csv").unlink()
    p99, answered, other = calibration_p99(records)
    problems = calibration_problems(p99, answered)
    io.say(
        f"calibration: {answered} answered, {other} not, p99 {se._ms(p99)} with a "
        f"{CALIBRATION_STALL_S} s stall in every {CALIBRATION_PERIOD_S} s"
    )
    for problem in problems:
        io.say(problem)
    return calibration_exit(problems, report_only)


# ---------------------------------------------------------------------------
# fixtures, tag, plan, provenance
# ---------------------------------------------------------------------------
def cmd_fixtures(work: Path, io: Io) -> int:
    """scripts/scale_stack.py fixtures, with the method file's preload counts
    (config.json stays their only source)."""
    preload = se.load_config()["preload"]
    return ss.main(
        [
            "fixtures",
            "--work",
            str(work),
            "--users",
            str(preload["users"]),
            "--events",
            str(preload["events"]),
            "--raw-metrics",
            str(preload["raw_metrics"]),
        ]
    )


def cmd_provenance(base: str, head: str, io: Io) -> int:
    """Each results file the pull request adds or changes is its run's
    artifact (scale_evidence.py verify). A pull request that changes none has
    nothing to verify, and says so."""
    for rev in (base, head):
        if not re.fullmatch(r"[0-9A-Za-z^~_./-]{1,100}", rev):
            raise Refused(f"{rev!r} is not a revision")
    result = io.run(
        [
            "git",
            "-C",
            str(ROOT),
            "diff",
            "--name-only",
            "--diff-filter=AMR",
            base,
            head,
            "--",
            RESULTS_DIR,
        ],
        ss.child_env(),
    )
    if result.returncode != 0:
        raise Failed(f"git diff exited {result.returncode}")
    changed = [
        line
        for line in (result.stdout or b"").decode().splitlines()
        if line.endswith(".json")
    ]
    if not changed:
        io.say(
            f"no results file under {RESULTS_DIR} is added or changed: nothing to verify"
        )
        return 0
    repo = io.environ.get("GITHUB_REPOSITORY", "")
    problems: List[str] = []
    for name in changed:
        path = ROOT / name
        data = se.loads(path.read_text(encoding="utf-8"))
        run_id = data.get("run", {}).get("run_id") if isinstance(data, dict) else None
        if not isinstance(run_id, int):
            problems.append(f"{name}: names no run")
            continue
        found = se.verify(path, run_id, repo)
        problems += [f"{name}: {problem}" for problem in found]
        if not found:
            io.say(f"{name} is the artifact of run {run_id}")
    for problem in problems:
        io.say(problem)
    return 1 if problems else 0


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------
def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="scale_run.py", description="The scale harness's measurement (#1608)."
    )
    commands = top.add_subparsers(dest="command", required=True)
    for name, text in (
        ("fixtures", "scale_stack.py fixtures with the method file's counts"),
        ("run", "measure one request type once"),
        ("calibrate", "k6 against a stalling stub (V18)"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("--work", type=Path, required=True)
        if name != "fixtures":
            command.add_argument("--out", type=Path, required=True)
        if name == "calibrate":
            command.add_argument("--report-only", action="store_true")
        if name == "run":
            command.add_argument("--scenario", required=True)
            command.add_argument("--rep", type=int, required=True)
            command.add_argument(
                "--mode", choices=("publication", "smoke"), default="publication"
            )
            command.add_argument("--no-resource-sampling", action="store_true")
    commands.add_parser("tag", help="the measured release's tag, from compose.yml")
    plan = commands.add_parser(
        "plan", help="the measure jobs, as the workflow's matrix"
    )
    plan.add_argument("--scenarios", required=True)
    plan.add_argument("--repetitions", required=True)
    proof = commands.add_parser("provenance", help="verify the changed results files")
    proof.add_argument("--base", required=True)
    proof.add_argument("--head", required=True)
    return top


def _interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def main(argv: Optional[Sequence[str]] = None, io: Optional[Io] = None) -> int:
    args = parser().parse_args(argv)
    io = io or Io()
    try:
        if args.command == "tag":
            io.say(f"v{compose_version(COMPOSE_FILE.read_text(encoding='utf-8'))}")
            return 0
        if args.command == "plan":
            matrix = plan_matrix(args.scenarios, args.repetitions)
            include = json.dumps({"include": matrix}, separators=(",", ":"))
            io.say(f"matrix={include}")
            io.say(f"jobs={len(matrix)}")
            io.say(f"dispatched_at={utc(io.now_ms())[:19]}Z")
            return 0
        if args.command == "provenance":
            return cmd_provenance(args.base, args.head, io)
        work = ss.work_dir(args.work)
        if args.command == "fixtures":
            return cmd_fixtures(work, io)
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, _interrupt)
        if args.command == "calibrate":
            return cmd_calibrate(work, args.out.resolve(), io, args.report_only)
        return cmd_run(
            work,
            args.out.resolve(),
            args.scenario,
            args.rep,
            args.mode,
            not args.no_resource_sampling,
            io,
        )
    except (Refused, ss.Refused) as err:
        print(f"refusing: {err}", file=sys.stderr)
        return 2
    except (Failed, ss.Failed) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
