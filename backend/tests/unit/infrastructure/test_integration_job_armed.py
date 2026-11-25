"""The integration-tests job keeps the Redis-backed tests armed.

The Redis-backed tests under ``backend/tests/integration/`` (the experiment
cache, and the flag reads after every writer, #630) skip when no Redis answers, unless ``EXPERIMENTLY_REQUIRE_REDIS=1`` turns that
skip into a failure. The ``integration-tests`` workflow provides the ``redis``
service and sets the variable on the step that runs the suite, and nothing
pinned either: deleting the env line, the service, or narrowing the run with
``-m``/``-k`` would leave every cache test skipping green.

The same pattern as ``test_the_integration_job_is_armed`` in
``backend/tests/integration/database/test_core_tree_isolation.py``, which pins
the editable install for the core-tree tests. The workflow is read with the
duplicate-key-refusing loader from ``test_docs_only_gate.py``, so a second
``env:`` or ``run:`` key cannot hide the one GitHub reads.

The suite runs in four shards (the ``shard`` job, #869) behind a summary job
that keeps the required check name ``integration-tests``. The second half of
this file pins that shape, because each way it can go wrong is green:

* the shard variables in the job's ``env`` also slice the e2e and contract
  runs in shard 1, dropping three quarters of them (F6);
* an exit-5 swallow or ``continue-on-error`` makes an empty shard green (V3);
* a step that runs on some shards only gives them different environments (V10);
* ``fail-fast`` cancels the other shards when one fails (V15);
* a shared artifact name, or a download pattern that matches no upload, loses
  reports (V13);
* a summary whose checker allows more than the benchmark skip, or whose
  results loop accepts more than ``success`` (V4).

``core-build`` in ``pr-qa-gate.yml`` runs the same suite against the core
tree through ``scripts/core_build.sh``, in the shards of ``core-build-shard``
(#2126), and is armed the same way (#899): the ``redis`` service, the
password-protected Redis started by hand, and both ``EXPERIMENTLY_REQUIRE_*``
variables on the step that runs the script.

``integration-tests`` in ``nightly-qa.yml`` runs the suite once a night, in
one session, and is armed the same way (#902): its ``redis`` service and its
password-protected Redis step are compared with the shard job's, so the two
workflows cannot drift apart, and its suite step sets both variables.

The summary's two ``run:`` scripts are executed here, over reports written by
real pytest running the shard plugin, the way test_dashboard_deploy_wiring.py
runs a deploy step's script.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.tests.unit.infrastructure import _workflow_graph as wg

pytestmark = [pytest.mark.unit, pytest.mark.regression]

WORKFLOW = wg.WORKFLOWS / "integration-tests.yml"
#: The job that runs the suite, as a matrix of shards.
JOB = "shard"
#: The job that reports the required check `integration-tests`.
SUMMARY = "integration-tests"
SUITE = "backend/tests/integration/"
SHARDS = [1, 2, 3, 4]
SHARD_ENV = ("EXPERIMENTLY_TEST_SHARD", "EXPERIMENTLY_TEST_SHARD_REPORT")
SHARD_SPEC = "${{ matrix.shard }}/${{ strategy.job-total }}"
REPORT = "${{ runner.temp }}/shard-integration-${{ matrix.shard }}.json"
REPORT_ARTIFACT = "shard-report-integration-${{ matrix.shard }}"
#: The only steps that run on some shards and not others.
SHARD_ONE_STEPS = {"Run E2E workflow tests", "Run contract tests"}

REPO_ROOT = wg.REPO_ROOT


def _jobs() -> Dict[str, Any]:
    return wg.load(WORKFLOW).get("jobs") or {}


def _job() -> Dict[str, Any]:
    jobs = _jobs()
    assert JOB in jobs, f"{WORKFLOW.name} has no `{JOB}` job: {sorted(jobs)}"
    return jobs[JOB]


def _summary() -> Dict[str, Any]:
    jobs = _jobs()
    assert SUMMARY in jobs, f"{WORKFLOW.name} has no `{SUMMARY}` job: {sorted(jobs)}"
    return jobs[SUMMARY]


def _tokens(run: str) -> List[str]:
    """The shell words of a ``run:`` block, comments and continuations removed."""
    return shlex.split(run.replace("\\\n", " "), comments=True)


def _pytest_args(tokens: List[str]) -> List[str]:
    """The words after ``pytest`` up to the end of that command."""
    assert "pytest" in tokens, tokens
    args: List[str] = []
    for token in tokens[tokens.index("pytest") + 1 :]:
        if token in {";", "&&", "||", "|"}:
            break
        args.append(token)
    return args


def _narrowing_flags(tokens: List[str]) -> List[str]:
    """Tokens that select a subset by marker or keyword: ``-m``, ``-k``, also
    attached (``-munit``) or bundled with other short flags (``-vk``)."""
    return [
        token
        for token in tokens
        if token.startswith("-")
        and not token.startswith("--")
        and ("m" in token[1:] or "k" in token[1:])
    ]


def _suite_steps(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        step
        for step in job.get("steps") or []
        if "run" in step
        and "pytest" in _tokens(step["run"])
        and any(t.rstrip("/") + "/" == SUITE for t in _tokens(step["run"]))
    ]


def _suite_step() -> Dict[str, Any]:
    steps = _suite_steps(_job())
    assert len(steps) == 1, (
        f"expected exactly one step that runs pytest on {SUITE}, found "
        f"{[s.get('name') for s in steps]}"
    )
    return steps[0]


def test_the_job_has_the_redis_service():
    services = _job().get("services") or {}
    assert "redis" in services, (
        f"the `{JOB}` job lost its `redis` service; with "
        "EXPERIMENTLY_REQUIRE_REDIS=1 every cache test fails, without it they "
        f"all skip: services are {sorted(services)}"
    )
    redis = services["redis"]
    assert str(redis.get("image", "")).startswith("redis:"), redis
    assert "6379:6379" in [str(p) for p in redis.get("ports") or []], redis


def test_the_suite_step_requires_redis():
    env = _suite_step().get("env") or {}
    assert env.get("EXPERIMENTLY_REQUIRE_REDIS") == "1", (
        "the step that runs the integration suite must set "
        'EXPERIMENTLY_REQUIRE_REDIS: "1", otherwise a missing Redis makes the '
        f"cache tests skip green; env is {env}"
    )


def test_the_suite_step_runs_the_whole_suite():
    step = _suite_step()
    tokens = _pytest_args(_tokens(step["run"]))
    assert SUITE in tokens, f"the step must run `{SUITE}` itself: {tokens}"
    assert not _narrowing_flags(tokens), (
        f"the step selects a subset with {_narrowing_flags(tokens)}; the "
        "Redis-backed tests carry no marker and would be deselected"
    )
    addopts = (step.get("env") or {}).get("PYTEST_ADDOPTS")
    if addopts is not None:
        assert not _narrowing_flags(shlex.split(str(addopts))), addopts


@pytest.mark.parametrize(
    "tokens, narrowed",
    [
        (["-v", "--tb=short"], False),
        (["--junit-xml=x.xml", "-x"], False),
        (["-m", "unit"], True),
        (["-munit"], True),
        (["-k", "x"], True),
        (["-vk", "x"], True),
    ],
)
def test_narrowing_flags_are_recognised(tokens, narrowed):
    assert bool(_narrowing_flags(tokens)) is narrowed


# ---------------------------------------------------------------------------
# core-build: the same suite against the core tree (#899)
# ---------------------------------------------------------------------------

GATE = wg.WORKFLOWS / "pr-qa-gate.yml"
#: The core profile's build runs in this matrix job's shards; `core-build`
#: is their summary (test_core_build_shards.py).
CORE_JOB = "core-build-shard"
CORE_STEP = "core build"
AUTH_STEP = "Start a password-protected Redis"
REQUIRE = ("EXPERIMENTLY_REQUIRE_REDIS", "EXPERIMENTLY_REQUIRE_REDIS_AUTH")


def _core_job() -> Dict[str, Any]:
    jobs = wg.load(GATE).get("jobs") or {}
    assert CORE_JOB in jobs, f"{GATE.name} has no `{CORE_JOB}` job: {sorted(jobs)}"
    return jobs[CORE_JOB]


def _core_step(name: str) -> Dict[str, Any]:
    steps = [s for s in _steps(_core_job()) if s.get("name") == name]
    assert len(steps) == 1, [s.get("name") for s in _steps(_core_job())]
    return steps[0]


def test_core_build_has_the_redis_service():
    """core_build.sh runs backend/tests/integration in the core copy; without
    the service the 57 cache and flag-read tests skipped there, unseen."""
    services = _core_job().get("services") or {}
    assert "redis" in services, (
        f"`{CORE_JOB}` lost its `redis` service: services are {sorted(services)}"
    )
    redis = services["redis"]
    assert str(redis.get("image", "")).startswith("redis:"), redis
    assert "6379:6379" in [str(p) for p in redis.get("ports") or []], redis


def test_core_build_starts_the_password_protected_redis_first():
    """The seven tests in test_redis_password_auth.py read
    REDIS_AUTH_TEST_PORT/_PASSWORD, which this step writes to GITHUB_ENV, so
    it has to run before the build step and under the same condition."""
    names = [s.get("name") for s in _steps(_core_job())]
    assert names.index(AUTH_STEP) < names.index(CORE_STEP), names
    auth, build = _core_step(AUTH_STEP), _core_step(CORE_STEP)
    assert auth.get("if") == build.get("if"), (auth.get("if"), build.get("if"))
    run = auth["run"]
    assert "--requirepass" in run, run
    assert "-p 6380:6379" in run, run
    for line in ("REDIS_AUTH_TEST_PORT=6380", "REDIS_AUTH_TEST_PASSWORD=${password}"):
        assert line in run, (line, run)
    assert '>> "$GITHUB_ENV"' in run, run


def test_core_build_requires_both_redis_servers():
    """On the step that runs core_build.sh, whose pytest sessions inherit its
    environment, and on no wider scope. Not EXPERIMENTLY_REQUIRE_EDITABLE:
    the job has no editable install, so test_the_integration_job_is_armed
    (test_core_tree_isolation.py) would fail."""
    build = _core_step(CORE_STEP)
    env = build.get("env") or {}
    assert {k: env.get(k) for k in REQUIRE} == dict.fromkeys(REQUIRE, "1"), env
    assert "EXPERIMENTLY_REQUIRE_EDITABLE" not in env, env
    # A shard of the default step list, which includes `integration` in every
    # shard; a `--steps` that left it out would leave the variables nothing
    # to arm, and the script refuses `--steps` beside `--shard`.
    assert _tokens(build["run"]) == _tokens(
        "scripts/core_build.sh --profile core"
        " --shard ${{ matrix.shard }}/${{ strategy.job-total }}"
        ' --shard-reports "$RUNNER_TEMP/core-build-shard-reports"'
    )
    workflow = wg.load(GATE)
    for where, scope in (
        ("the workflow", workflow.get("env") or {}),
        (f"job {CORE_JOB}", _core_job().get("env") or {}),
    ):
        assert not set(REQUIRE) & set(scope), f"{where} sets {sorted(scope)}"
        assert "EXPERIMENTLY_REQUIRE_EDITABLE" not in scope, where


def test_core_build_s_pytest_sessions_inherit_the_environment():
    """The variables reach the copy only because pytest_session runs pytest
    with the caller's environment; an `env -i` there would drop them and the
    Redis tests would skip again."""
    script = (REPO_ROOT / "scripts" / "core_build.sh").read_text(encoding="utf-8")
    body = re.search(r"^pytest_session\(\) \{\n(.*?)^\}", script, re.S | re.M)
    assert body, "scripts/core_build.sh has no pytest_session()"
    code = "\n".join(
        line for line in body.group(1).splitlines() if not line.strip().startswith("#")
    )
    assert '"$PYTHON" -m pytest' in code, code
    # `env -i`, `env -u NAME`, or `env - ...`: each starts the pytest without
    # some or all of the caller's variables.
    assert not re.search(r"(^|[\s;&|(])env\s+-", code), code


def test_core_build_selects_its_steps_by_no_variable():
    """scripts/core_build.sh reads CORE_BUILD_STEPS as `--steps`: set to every
    step but `integration`, it would leave the Redis variables nothing to arm
    while the command line above still looks like the default list."""
    workflow = wg.load(GATE)
    for where, scope in (
        ("the workflow", workflow.get("env") or {}),
        (f"job {CORE_JOB}", _core_job().get("env") or {}),
        (f"step {CORE_STEP!r}", _core_step(CORE_STEP).get("env") or {}),
    ):
        assert "CORE_BUILD_STEPS" not in scope, f"{where} sets CORE_BUILD_STEPS"


#: pytest options that run fewer tests than the paths given select, beyond the
#: `-m`/`-k` that `_narrowing_flags` finds. Matched as the whole token or as
#: `--opt=value`.
NARROWING_LONG_OPTIONS = (
    "--deselect",
    "--ignore",
    "--ignore-glob",
    "--lf",
    "--last-failed",
    "--sw",
    "--stepwise",
    "--sw-skip",
    "--stepwise-skip",
    "--co",
    "--collect-only",
    "--collectonly",
    "--keyword",
)


def _script_narrowing(tokens: List[str]) -> List[str]:
    long = [
        token
        for token in tokens
        if any(
            token == option or token.startswith(option + "=")
            for option in NARROWING_LONG_OPTIONS
        )
    ]
    return _narrowing_flags(tokens) + long


def _shell_function(name: str) -> List[str]:
    """The words of a function in scripts/core_build.sh, comments removed."""
    script = (REPO_ROOT / "scripts" / "core_build.sh").read_text(encoding="utf-8")
    body = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)^\}}", script, re.S | re.M)
    assert body, f"scripts/core_build.sh has no {name}()"
    # A subshell's closing `)` sticks to the last word (`--lf)`).
    return [t.rstrip(")") for t in _tokens(body.group(1))]


@pytest.mark.parametrize(
    "function", ["step_integration", "shard_session", "pytest_session"]
)
def test_core_build_runs_the_whole_integration_suite(function):
    """The integration step hands the suite through shard_session (which cuts
    it to a shard's slice under --shard, by backend/tests/shard.py) to
    pytest_session, and nothing that selects a subset; neither function adds
    anything that does either: a `-k "not redis"` would deselect the tests
    this job arms."""
    tokens = _shell_function(function)
    if function == "step_integration":
        assert tokens[:3] == [
            "shard_session",
            "integration",
            "backend/tests/integration",
        ], tokens
    elif function == "shard_session":
        calls = [i for i, t in enumerate(tokens) if t == "pytest_session"]
        assert len(calls) == 2, tokens
        assert all(tokens[i + 1] == "$suite" for i in calls), tokens
    else:
        # The words after `pytest`: the `-m` of `python -m pytest` is not one.
        tokens = _pytest_args(tokens)
        assert tokens[0] == "$@", tokens
    assert not _script_narrowing(tokens), (
        f"{function}() narrows the run with {_script_narrowing(tokens)}: {tokens}"
    )


@pytest.mark.parametrize(
    "tokens, narrowed",
    [
        (["-p", "no:cov", "-q", "--tb=short"], False),
        (["-k", "not redis"], True),
        (["--deselect", "a.py::t"], True),
        (["--ignore=backend/tests/integration/api"], True),
        (["--ignore-glob", "*redis*"], True),
        (["--lf"], True),
        (["--collect-only"], True),
    ],
)
def test_script_narrowing_is_recognised(tokens, narrowed):
    assert bool(_script_narrowing(tokens)) is narrowed


# ---------------------------------------------------------------------------
# nightly-qa: the same suite once a night (#902)
# ---------------------------------------------------------------------------

NIGHTLY = wg.WORKFLOWS / "nightly-qa.yml"
#: The matrix job that runs the suite; `integration-tests` there is the summary.
NIGHTLY_JOB = "integration-shard"


def _nightly_job() -> Dict[str, Any]:
    jobs = wg.load(NIGHTLY).get("jobs") or {}
    assert NIGHTLY_JOB in jobs, (
        f"{NIGHTLY.name} has no `{NIGHTLY_JOB}` job: {sorted(jobs)}"
    )
    return jobs[NIGHTLY_JOB]


def _nightly_suite_step() -> Dict[str, Any]:
    steps = _suite_steps(_nightly_job())
    assert len(steps) == 1, (
        f"expected exactly one step in {NIGHTLY.name} that runs pytest on {SUITE}, "
        f"found {[s.get('name') for s in steps]}"
    )
    return steps[0]


def _named_step(job: Dict[str, Any], name: str) -> Dict[str, Any]:
    steps = [s for s in _steps(job) if s.get("name") == name]
    assert len(steps) == 1, [s.get("name") for s in _steps(job)]
    return steps[0]


def test_the_nightly_job_has_the_shard_job_s_redis_service():
    """Without it the Redis-backed tests skipped every night (#902). Compared
    whole with the shard job's, so a change to one is a change to both."""
    services = _nightly_job().get("services") or {}
    assert "redis" in services, (
        f"`{NIGHTLY_JOB}` in {NIGHTLY.name} lost its `redis` service: services "
        f"are {sorted(services)}"
    )
    assert services["redis"] == (_job().get("services") or {}).get("redis"), (
        services["redis"],
        (_job().get("services") or {}).get("redis"),
    )


def test_the_nightly_job_starts_the_shard_job_s_password_protected_redis_first():
    """test_redis_password_auth.py reads REDIS_AUTH_TEST_PORT/_PASSWORD, which
    this step writes to GITHUB_ENV: the same script as the shard job's, before
    the suite step and under the same condition."""
    job = _nightly_job()
    suite = _nightly_suite_step()
    names = [s.get("name") for s in _steps(job)]
    auth = _named_step(job, AUTH_STEP)
    assert names.index(AUTH_STEP) < names.index(suite.get("name")), names
    assert auth.get("if") == suite.get("if"), (auth.get("if"), suite.get("if"))
    assert auth.get("run") == _named_step(_job(), AUTH_STEP).get("run"), (
        f"the {AUTH_STEP!r} step differs between {NIGHTLY.name} and {WORKFLOW.name}"
    )


def test_the_nightly_suite_step_requires_both_redis_servers():
    env = _nightly_suite_step().get("env") or {}
    assert {k: env.get(k) for k in REQUIRE} == dict.fromkeys(REQUIRE, "1"), (
        f"the step that runs {SUITE} in {NIGHTLY.name} must set both "
        f'{REQUIRE} to "1", otherwise a missing Redis makes its tests skip '
        f"green; env is {env}"
    )


def test_the_nightly_suite_step_runs_the_whole_suite():
    step = _nightly_suite_step()
    tokens = _pytest_args(_tokens(step["run"]))
    assert SUITE in tokens, f"the step must run `{SUITE}` itself: {tokens}"
    assert not _script_narrowing(tokens), (
        f"the step narrows the run with {_script_narrowing(tokens)}: {tokens}"
    )
    assert "PYTEST_ADDOPTS" not in (step.get("env") or {}), step.get("env")


# ---------------------------------------------------------------------------
# The shards
# ---------------------------------------------------------------------------


def _steps(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(job.get("steps") or [])


def test_the_shards_are_a_static_matrix_that_does_not_fail_fast():
    """V15, V7: `fail-fast: false`, and a typed list of shard numbers 1..n
    with nothing else in the matrix, so every check name is known before the
    run and the total GitHub renders is the length of this list."""
    strategy = _job().get("strategy") or {}
    assert strategy.get("fail-fast") is False, strategy
    assert strategy.get("matrix") == {"shard": SHARDS}, strategy
    assert SHARDS == list(range(1, len(SHARDS) + 1))
    assert "if" not in _job(), _job().get("if")


def test_a_shard_gets_45_minutes():
    """Each shard job has `timeout-minutes: 45`, the value of the nightly's
    `integration-shard` job. At 30, shard 3 was cancelled five times out of 28
    finished attempts between 2026-11-24 11:49Z and 2026-11-25 05:00Z, at 30.2
    to 30.3 minutes, while the 23 that finished took 19.6 to 29.8 minutes."""
    assert _job().get("timeout-minutes") == 45, _job().get("timeout-minutes")


def test_the_shard_env_is_on_the_suite_step_only():
    """F6: the shard variables on the integration suite step and nowhere else.
    In the job's or the workflow's env they would also slice the e2e and
    contract runs in shard 1 and drop three quarters of them."""
    workflow = wg.load(WORKFLOW)
    for where, env in (
        ("the workflow", workflow.get("env") or {}),
        (f"job {JOB}", _job().get("env") or {}),
        (f"job {SUMMARY}", _summary().get("env") or {}),
    ):
        assert not set(SHARD_ENV) & set(env), f"{where} sets {sorted(env)}"
    suite = _suite_step()
    holders = [
        step.get("name")
        for step in _steps(_job()) + _steps(_summary())
        if set(SHARD_ENV) & set(step.get("env") or {})
    ]
    assert holders == [suite.get("name")], holders
    env = suite["env"]
    assert env["EXPERIMENTLY_TEST_SHARD"] == SHARD_SPEC, env
    assert env["EXPERIMENTLY_TEST_SHARD_REPORT"] == REPORT, env


def test_the_report_is_outside_the_checkout_and_named_for_the_checker():
    """An absolute path (the plugin refuses a relative one), unique to the
    shard, and a name the checker's directory search finds."""
    report = _suite_step()["env"]["EXPERIMENTLY_TEST_SHARD_REPORT"]
    assert report.startswith("${{ runner.temp }}/"), report
    assert "${{ matrix.shard }}" in report
    assert fnmatch.fnmatch(
        Path(report.replace("${{ matrix.shard }}", "3")).name, "shard-*.json"
    )
    # The junit beside it, named for the shard: the one-time V17 comparison
    # of `ran` with pytest's own record reads it.
    args = _pytest_args(_tokens(_suite_step()["run"]))
    assert "--junit-xml=test-results/integration-${{ matrix.shard }}.xml" in args, args


def test_an_empty_shard_cannot_pass():
    """V3: pytest exits 5 when a shard selects nothing (and the plugin exits
    4); neither may be swallowed."""
    for step in _steps(_job()):
        assert "continue-on-error" not in step, step.get("name")
    assert "continue-on-error" not in _job()
    run = _suite_step()["run"]
    for swallow in ("||", "suppress-no-test-exit-code", "$?", "set +e"):
        assert swallow not in run, (swallow, run)


def test_only_the_e2e_and_contract_steps_depend_on_the_shard():
    """V10, C5: every shard runs the same setup and the same suite step. The
    e2e and contract runs stay whole, in shard 1, with no shard env."""
    conditioned = {}
    for step in _steps(_job()):
        text = json.dumps(step)
        if "matrix." in text and step.get("name") not in (
            _suite_step().get("name"),
            "Upload test results",
            "Upload the shard report",
        ):
            conditioned[step.get("name")] = step.get("if")
    assert conditioned == dict.fromkeys(SHARD_ONE_STEPS, "matrix.shard == 1")


def _uploads(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        s
        for s in _steps(job)
        if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    ]


def test_every_upload_is_unique_to_its_shard_and_overwritable():
    """V13: artifacts are immutable per run, so a shared name fails the second
    shard's upload; `overwrite`, so re-running one shard can upload again.
    Uploaded even when the suite failed."""
    uploads = _uploads(_job())
    assert len(uploads) == 2, uploads
    for step in uploads:
        with_ = step.get("with") or {}
        assert "${{ matrix.shard }}" in str(with_.get("name")), with_
        assert with_.get("overwrite") is True, with_
        assert step.get("if") == "always()", step
    report = [s for s in uploads if s["with"]["name"] == REPORT_ARTIFACT]
    assert len(report) == 1, uploads
    assert report[0]["with"]["path"] == REPORT


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------


def _summary_step(name: str) -> Dict[str, Any]:
    steps = [s for s in _steps(_summary()) if s.get("name") == name]
    assert len(steps) == 1, [s.get("name") for s in _steps(_summary())]
    return steps[0]


RESULTS_STEP = "Every shard passed"
CHECKER_STEP = "Every test ran in exactly one shard"


def test_the_summary_reports_the_required_name_after_any_shard_result():
    """The required check under its id, with no `name:`; it needs the shards
    and runs on exactly `always()` (test_ci_aggregators.py pins the same
    through R1/R2; this names the job)."""
    summary = _summary()
    assert "name" not in summary, summary.get("name")
    assert wg.needs_of(summary) == [JOB]
    assert summary.get("if") == "always()"
    assert "strategy" not in summary


def test_the_summary_downloads_what_the_shards_upload():
    """V13: the download pattern matches the report artifact of every shard
    and nothing else the shards upload; the checker reads where it lands."""
    downloads = [
        s
        for s in _steps(_summary())
        if str(s.get("uses", "")).startswith("actions/download-artifact@")
    ]
    assert len(downloads) == 1, downloads
    with_ = downloads[0]["with"]
    names = {
        str(u["with"]["name"]).replace("${{ matrix.shard }}", str(i))
        for u in _uploads(_job())
        for i in SHARDS
    }
    matched = {n for n in names if fnmatch.fnmatchcase(n, with_["pattern"])}
    assert matched == {
        REPORT_ARTIFACT.replace("${{ matrix.shard }}", str(i)) for i in SHARDS
    }
    assert with_["path"] == "${{ runner.temp }}/shard-reports", with_
    assert "merge-multiple" not in with_
    assert '"$RUNNER_TEMP/shard-reports"' in _summary_step(CHECKER_STEP)["run"]


def _checker_args() -> List[str]:
    tokens = _tokens(_summary_step(CHECKER_STEP)["run"])
    assert tokens[:2] == ["python3", "scripts/check_test_shards.py"], tokens
    return tokens[2:]


def test_the_checker_allows_the_benchmark_skip_and_nothing_else():
    """C2: one suite, one exact skip reason, no blanket allowance and no
    disabled plugin (the suite step passes no `-p`)."""
    args = _checker_args()
    assert args == [
        "--suite",
        "backend/tests/integration",
        "--allow-skip",
        f"backend/tests/integration={_benchmark_reason()}",
        "$RUNNER_TEMP/shard-reports",
    ], args
    assert "-p" not in _pytest_args(_tokens(_suite_step()["run"]))
    assert _summary_step(CHECKER_STEP).get("if") == "always()"


def _benchmark_reason() -> str:
    """The reason the benchmark gate gives, read from a real skip."""
    from backend.tests import benchmark_gate

    class _Item:
        def __init__(self) -> None:
            self.marks: List[Any] = []

        def get_closest_marker(self, name: str) -> Any:
            return object() if name == "benchmark" else None

        def add_marker(self, mark: Any) -> None:
            self.marks.append(mark)

    class _Config:
        def getoption(self, *_: Any) -> str:
            return ""

    item = _Item()
    saved = os.environ.pop(benchmark_gate.ENV_VAR, None)
    try:
        benchmark_gate.pytest_collection_modifyitems(_Config(), [item])
    finally:
        if saved is not None:
            os.environ[benchmark_gate.ENV_VAR] = saved
    (mark,) = item.marks
    return mark.kwargs["reason"]


# The summary's two scripts, run the way the runner runs them (`bash -e`),
# over real `toJSON(needs)` shapes and real shard reports.


def _run_step(
    step: Dict[str, Any], cwd: Path, env: Dict[str, str]
) -> subprocess.CompletedProcess:
    script = cwd / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    full = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("EXPERIMENTLY_TEST_SHARD")
    }
    full.update(env)
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", str(script)],
        cwd=cwd,
        env=full,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _needs(result: str) -> str:
    return json.dumps({"shard": {"result": result, "outputs": {}}})


@pytest.mark.parametrize(
    "results, ok",
    [
        (_needs("success"), True),
        (_needs("failure"), False),
        (_needs("cancelled"), False),  # a hand cancel, and a timed-out shard
        (_needs("skipped"), False),
        (
            json.dumps({"shard": {"result": "success"}, "x": {"result": "neutral"}}),
            False,
        ),
        ("{}", False),
    ],
    ids=["success", "failure", "cancelled", "skipped", "other", "empty"],
)
def test_the_results_step_accepts_only_success(tmp_path, results, ok):
    """V4: Release Gate Summary's loop over `toJSON(needs)`; anything but
    `success`, and no results at all, is red."""
    done = _run_step(_summary_step(RESULTS_STEP), tmp_path, {"RESULTS": results})
    assert (done.returncode == 0) is ok, done.stdout + done.stderr


TREE = {
    "conftest.py": """
        pytest_plugins = ["backend.tests.benchmark_gate"]
        """,
    "test_a.py": """
        import os

        import pytest

        @pytest.mark.parametrize("n", range(24))
        def test_plain(n):
            pass

        @pytest.mark.benchmark
        def test_timing():
            pass

        @pytest.mark.skipif(os.environ.get("FIXTURE_SKIP") == "1", reason="no server")
        @pytest.mark.parametrize("n", range(8))
        def test_needs_a_server(n):
            pass
        """,
}


def _shard_reports(tmp_path: Path, env: Dict[str, str] | None = None) -> Path:
    """Four real shard sessions over a tree at backend/tests/integration, with
    the reports laid out as download-artifact writes them: one directory per
    artifact under $RUNNER_TEMP/shard-reports. No `-p no:` flag, as in CI."""
    root = tmp_path / "tree"
    suite = root / SUITE
    suite.mkdir(parents=True)
    for name, body in TREE.items():
        (suite / name).write_text(textwrap.dedent(body), encoding="utf-8")
    temp = tmp_path / "runner-temp"
    temp.mkdir()
    procs = []
    for i in SHARDS:
        report = temp / f"shard-integration-{i}.json"
        shard_env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("EXPERIMENTLY_TEST_SHARD")
            and k not in ("PYTEST_ADDOPTS", "RUN_BENCHMARKS")
        }
        shard_env.update(
            PYTHONPATH=str(REPO_ROOT),
            EXPERIMENTLY_TEST_SHARD=f"{i}/{len(SHARDS)}",
            EXPERIMENTLY_TEST_SHARD_REPORT=str(report),
            **(env or {}),
        )
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "backend.tests.shard",
            "-c",
            os.devnull,
            "--rootdir",
            str(root),
            "-o",
            f"cache_dir={tmp_path / 'cache'}",
            "-q",
            SUITE,
        ]
        procs.append(
            (
                i,
                report,
                subprocess.Popen(
                    cmd,
                    cwd=root,
                    env=shard_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                ),
            )
        )
    for i, report, proc in procs:
        out, _ = proc.communicate(timeout=120)
        assert proc.returncode == 0, out
        artifact = temp / "shard-reports" / f"shard-report-integration-{i}"
        artifact.mkdir(parents=True)
        report.rename(artifact / report.name)
    return temp


def _run_checker(tmp_path: Path, temp: Path) -> subprocess.CompletedProcess:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "scripts").mkdir(exist_ok=True)
    link = work / "scripts" / "check_test_shards.py"
    if not link.exists():
        link.symlink_to(REPO_ROOT / "scripts" / "check_test_shards.py")
    return _run_step(_summary_step(CHECKER_STEP), work, {"RUNNER_TEMP": str(temp)})


def test_the_checker_step_passes_four_real_shards(tmp_path):
    done = _run_checker(tmp_path, _shard_reports(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr
    assert (
        "backend/tests/integration: 4 shards, 33 collected, each ran exactly once"
        in done.stdout
    )


def test_the_checker_step_refuses_a_missing_shard(tmp_path):
    temp = _shard_reports(tmp_path)
    for path in (temp / "shard-reports" / "shard-report-integration-3").iterdir():
        path.unlink()
    done = _run_checker(tmp_path, temp)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "shard(s) [3] of 4 have no report" in done.stdout


def test_the_checker_step_refuses_any_other_skip(tmp_path):
    """C2: a Redis or Postgres test that skips instead of running is red."""
    done = _run_checker(tmp_path, _shard_reports(tmp_path, {"FIXTURE_SKIP": "1"}))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "its reason is not allowed: 'no server'" in done.stdout


def test_the_checker_step_refuses_a_narrowed_run(tmp_path):
    """C3: `-m`/`-k` from the job's environment, which no YAML pin sees."""
    done = _run_checker(
        tmp_path, _shard_reports(tmp_path, {"PYTEST_ADDOPTS": "-k plain"})
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert "narrowed by keyword='plain'" in done.stdout


def test_the_checker_step_refuses_no_reports(tmp_path):
    temp = tmp_path / "runner-temp"
    (temp / "shard-reports").mkdir(parents=True)
    done = _run_checker(tmp_path, temp)
    assert done.returncode != 0, done.stdout + done.stderr
