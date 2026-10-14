"""What the load-test runner runs, against which API, and the API it starts.

``backend/tests/performance/run_load_tests.py`` is driven through ``main()``
with its three side effects replaced by recorders (starting the API, waiting
for it, running Locust), so each test reads what the runner asked for: which
locustfile, which host, and whether it stopped the server it started. The API
``--start-server`` launches is read in a fresh interpreter with the
environment the runner gives it; a development API is read the same way.

Locust is never imported into this process (it monkey-patches with gevent).
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import ModuleType

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
PERFORMANCE = REPO_ROOT / "backend" / "tests" / "performance"
RUNNER = PERFORMANCE / "run_load_tests.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "performance-tests.yml"


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_run_load_tests_cli", RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Server:
    """Stands in for the uvicorn process ``_start_server`` returns."""

    def __init__(self) -> None:
        self.stopped = False

    def terminate(self) -> None:
        self.stopped = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.stopped = True


def _drive(runner: ModuleType, monkeypatch, argv: list[str]) -> dict:
    """Run ``main(argv)``; Locust "fails" so the run ends after it."""
    seen: dict = {"server": _Server()}

    def start(port: int) -> _Server:
        seen["started_on"] = port
        return seen["server"]

    def wait(host: str, timeout_seconds: int = 30) -> bool:
        seen["waited_for"] = host
        return True

    def locust(**kwargs) -> int:
        seen["locust"] = kwargs
        return 1

    monkeypatch.setattr(runner, "_start_server", start)
    monkeypatch.setattr(runner, "_wait_for_server", wait)
    monkeypatch.setattr(runner, "_run_locust", locust)
    seen["exit"] = runner.main(argv)
    return seen


@pytest.mark.regression
def test_without_a_profile_the_runner_runs_the_baseline(runner, monkeypatch):
    seen = _drive(runner, monkeypatch, [])
    assert seen["locust"]["locustfile"] == str(
        PERFORMANCE / "locustfiles" / "api_load_test.py"
    )
    spike = _drive(runner, monkeypatch, ["--profile", "spike"])
    assert spike["locust"]["locustfile"] == str(
        PERFORMANCE / "locustfiles" / "spike_test.py"
    )


@pytest.mark.regression
def test_start_server_runs_against_the_api_it_started_and_stops_it(runner, monkeypatch):
    seen = _drive(runner, monkeypatch, ["--start-server", "--server-port", "8123"])
    assert seen["started_on"] == 8123
    assert seen["waited_for"] == "http://localhost:8123"
    assert seen["locust"]["host"] == "http://localhost:8123"
    assert seen["server"].stopped, "the runner left the API it started running"


@pytest.mark.regression
def test_with_host_it_runs_against_that_api_and_starts_none(runner, monkeypatch):
    seen = _drive(runner, monkeypatch, ["--host", "http://localhost:8000"])
    assert "started_on" not in seen
    assert seen["locust"]["host"] == "http://localhost:8000"


@pytest.mark.regression
def test_the_runner_runs_locust_headless(runner, monkeypatch):
    calls = []

    class Done:
        returncode = 0

    def run(cmd, **kwargs):
        calls.append(cmd)
        return Done()

    monkeypatch.setattr(runner.subprocess, "run", run)
    runner._run_locust(
        host="http://h",
        users=2,
        spawn_rate=1,
        duration="5s",
        locustfile="f.py",
        csv_prefix="/tmp/p",
    )
    ((cmd),) = calls
    assert cmd[1:3] == ["-m", "locust"]
    assert "--headless" in cmd


def _settings_in(env: dict) -> dict:
    """The API's environment, rate limiting and database, read in a fresh
    interpreter with *env*."""
    probe = (
        "import json\n"
        "import backend.app.main as main\n"
        "from backend.app.core.config import settings\n"
        "from backend.app.core.database_config import get_schema_name\n"
        "from backend.app.middleware.rate_limiter import RateLimitMiddleware\n"
        "(mounted,) = [m for m in main.app.user_middleware"
        " if m.cls is RateLimitMiddleware]\n"
        "print(json.dumps({'environment': settings.ENVIRONMENT,"
        " 'rate_limited': mounted.kwargs['enabled'],"
        " 'database': settings.POSTGRES_DB, 'schema': get_schema_name()}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def _reader_shell() -> dict:
    """A shell with none of the settings the page has a reader export."""
    drop = {"ENVIRONMENT", "APP_ENV", "TESTING", "POSTGRES_DB", "POSTGRES_SCHEMA"}
    return {k: v for k, v in os.environ.items() if k not in drop}


@pytest.mark.regression
def test_start_server_starts_the_api_from_the_checkout_in_the_test_environment(
    runner, monkeypatch
):
    started = []

    def popen(cmd, env=None, cwd=None, **kwargs):
        started.append((cmd, env, cwd))
        return object()

    with monkeypatch.context() as patched:
        patched.setattr(runner.subprocess, "Popen", popen)
        patched.setattr(os, "environ", _reader_shell())
        runner._start_server(8123)
    ((cmd, env, cwd),) = started
    assert cwd == str(REPO_ROOT)
    assert cmd[1:4] == ["-m", "uvicorn", "backend.app.main:app"]
    assert cmd[cmd.index("--port") + 1] == "8123"

    api = _settings_in(env)
    assert api == {
        "environment": "test",
        "rate_limited": False,
        "database": "experimentation_test",
        "schema": "test_experimentation",
    }
    named = _settings_in(
        {**env, "POSTGRES_DB": "experimentation", "POSTGRES_SCHEMA": "experimentation"}
    )
    assert (named["database"], named["schema"]) == (
        "experimentation",
        "experimentation",
    )


@pytest.mark.regression
def test_a_development_api_answers_429_past_6000_requests_a_minute_on_an_sdk_route():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from backend.app.middleware.rate_limiter import (
        RateLimitMiddleware,
        SlidingWindowRateLimiter,
        resolve_rate_limit,
    )

    development = _settings_in({**_reader_shell(), "ENVIRONMENT": "development"})
    assert (development["environment"], development["rate_limited"]) == (
        "development",
        True,
    )

    async def ok(request):
        return JSONResponse({})

    app = Starlette(routes=[Route("/api/v1/tracking/track", ok, methods=["POST"])])
    limited = RateLimitMiddleware(app, enabled=True)
    limited._limiter = SlidingWindowRateLimiter()  # in this process, not Redis
    assert limited._sdk_limit == 6000
    for path in ("/api/v1/tracking/track", "/api/v1/feature-flags/evaluate/f"):
        assert resolve_rate_limit(path, limited._sdk_limit) == (6000, 60), path
    client = TestClient(limited)
    codes = [client.post("/api/v1/tracking/track").status_code for _ in range(6001)]
    assert codes[:6000] == [200] * 6000
    assert codes[6000] == 429


@pytest.mark.regression
def test_a_429_is_a_failed_request():
    from backend.tests.performance.locustfiles.common import expect

    class Response:
        status_code = 429
        verdict = None

        def success(self) -> None:
            self.verdict = "success"

        def failure(self, message: str) -> None:
            self.verdict = message

    response = Response()
    assert expect(response, 200) is False
    assert response.verdict == "HTTP 429, expected 200"


@pytest.mark.regression
def test_locust_is_pinned_and_the_pinned_version_is_installed():
    text = (REPO_ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")
    (pinned,) = re.findall(r"^locust==([0-9][0-9.]*)\b", text, re.M)
    assert importlib.metadata.version("locust") == pinned


#: Option A's three commands, as the page gives them: no --headless, so
#: Locust serves its web UI instead of running. The spike test's shape class
#: sets the users and the ramp itself, so Locust serves the page with
#: hide_common_options true; the other two leave it unset.
WEB_UI_LOCUSTFILES = {
    "baseline": ("api_load_test.py", False),
    "spike": ("spike_test.py", True),
    "endurance": ("endurance_test.py", False),
}
WEB_UI = "http://127.0.0.1:8089"


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:
        assert response.status == 200, (url, response.status)
        return response.read().decode("utf-8")


@pytest.mark.parametrize("profile", sorted(WEB_UI_LOCUSTFILES))
def test_the_web_ui_is_on_8089_and_its_form_follows_the_locustfile(tmp_path, profile):
    """The interactive run serves Locust's web UI on port 8089. The pinned
    Locust's form has the Number of users and Ramp up fields and a Start
    button, and disables both fields when the page says hide_common_options,
    which it does for the spike test's shape and not for the other two. The
    form is never submitted, so nothing is sent to the host."""
    locustfile, shaped = WEB_UI_LOCUSTFILES[profile]
    locust = Path(sys.executable).with_name("locust")
    command = str(locust) if locust.is_file() else shutil.which("locust")
    assert command, "the locust command is not installed"
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex(("127.0.0.1", 8089)) != 0, (
            "something already listens on 8089; this test must start the UI there"
        )
    log = tmp_path / "locust.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            [
                command,
                "-f",
                f"backend/tests/performance/locustfiles/{locustfile}",
                "--host",
                "http://localhost:8000",
            ],
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            page = ""
            deadline = time.monotonic() + 90
            while not page and process.poll() is None and time.monotonic() < deadline:
                try:
                    page = _get(f"{WEB_UI}/")
                except OSError:
                    time.sleep(0.5)
            assert page, log.read_text()[-3000:]
            scripts = re.findall(r'<script[^>]* src="\./(assets/[^"]+\.js)"', page)
            assert len(scripts) == 1, page[:2000]
            bundle = _get(f"{WEB_UI}/{scripts[0]}")
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=30)
    assert "Starting web interface at http://0.0.0.0:8089" in log.read_text()

    (hidden,) = re.findall(r'"hide_common_options": *(true|false|null)\b', page)
    assert (hidden == "true") is shaped, (profile, hidden)

    # The form's own function: its hideCommonOptions parameter is what
    # disables the two fields.
    form = bundle.rindex("function ", 0, bundle.index("name:`userCount`"))
    params = re.match(r"function [A-Za-z_$]+\(\{([^}]{1,2000})\}", bundle[form:])
    assert params, bundle[form : form + 300]
    (flag,) = re.findall(r"\bhideCommonOptions:([A-Za-z_$]+)", params.group(1))
    for label, name in (
        ("Number of users (peak concurrency)", "userCount"),
        ("Ramp up (users started/second)", "spawnRate"),
    ):
        assert f"disabled:!!{flag},label:`{label}`,name:`{name}`" in bundle, name
    assert re.search(
        r"type:`submit`,variant:`contained`,children:[A-Za-z_$]+\?`Update`:`Start`\}",
        bundle,
    ), "the swarm form's submit button does not read Start"


@pytest.mark.regression
def test_the_framework_unit_tests_pass_with_no_database_or_api():
    """The spec and validator tests, with PostgreSQL, Redis and the API all at
    a port nothing listens on."""
    env = {
        **os.environ,
        "POSTGRES_SERVER": "127.0.0.1",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "1",
        "REDIS_HOST": "127.0.0.1",
        "REDIS_PORT": "1",
        "REDIS_URL": "redis://127.0.0.1:1",
        "DATABASE_URI": "postgresql://postgres:postgres@127.0.0.1:1/none",
    }
    env.pop("DATABASE_URL", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            str(PERFORMANCE / "test_specs.py"),
            str(PERFORMANCE / "test_validators.py"),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-3000:]
    summary = result.stdout.strip().splitlines()[-1]
    assert re.search(r"\b[1-9][0-9]* passed\b", summary), summary
    assert "skipped" not in summary and "error" not in summary, summary


# ---------------------------------------------------------------------------
# The weekly workflow
# ---------------------------------------------------------------------------


def _workflow() -> dict:
    if not WORKFLOW.is_file():
        pytest.skip("this tree has no .github/workflows")
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.mark.regression
def test_the_workflow_runs_the_baseline_every_monday_and_any_profile_on_dispatch(
    runner,
):
    workflow = _workflow()
    triggers = workflow[True]  # PyYAML reads the key `on` as True
    ((cron,),) = [[s["cron"] for s in triggers["schedule"]]]
    assert cron.split()[2:] == ["*", "*", "1"], cron
    options = triggers["workflow_dispatch"]["inputs"]["test_type"]["options"]
    assert triggers["workflow_dispatch"]["inputs"]["test_type"]["default"] == "baseline"

    steps = workflow["jobs"]["load-test"]["steps"]
    (pick,) = [s["run"] for s in steps if "case" in s.get("run", "")]
    files = dict(re.findall(r"^\s*([a-z_]+)\)\s+FILE=\"([^\"]+)\"", pick, re.M))
    runner_files = {
        Path(path).relative_to(REPO_ROOT).as_posix()
        for path in runner._PROFILE_LOCUSTFILES.values()
    }
    assert {files[o] for o in options} == runner_files
    assert len(options) == 6
    assert "${{ github.event.inputs.test_type || 'baseline' }}" in pick


@pytest.mark.regression
def test_the_workflow_uploads_the_csv_files_it_tells_locust_to_write():
    steps = _workflow()["jobs"]["load-test"]["steps"]
    (run,) = [s["run"] for s in steps if "run_load_tests.py" in s.get("run", "")]
    prefix = re.search(r"--csv-prefix (\S+)", run).group(1)
    uploads = [
        s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@")
    ]
    assert uploads, "nothing is uploaded"
    for upload in uploads:
        assert upload.get("if") == "always()", upload
    globs = [u["with"]["path"] for u in uploads]
    assert any(
        re.fullmatch(re.escape(prefix.rsplit("_", 1)[0]) + r"_\*\.csv", g)
        for g in globs
    ), (prefix, globs)


@pytest.mark.regression
def test_the_workflow_runs_its_api_in_the_test_environment():
    workflow = _workflow()
    job = workflow["jobs"]["load-test"]
    assert job["env"]["APP_ENV"] == "test"
    assert "ENVIRONMENT" not in job["env"]
    (start,) = [
        s for s in job["steps"] if "uvicorn backend.app.main:app" in s.get("run", "")
    ]
    assert "ENVIRONMENT" not in start.get("env", {})


@pytest.mark.regression
def test_report_mode_prints_each_percentile_and_rps_beside_its_target(
    runner, tmp_path, capsys
):
    stats = tmp_path / "locust_stats.csv"
    stats.write_text(
        "Type,Name,Request Count,Failure Count,50%,95%,99%\n"
        "GET,/health,600,0,12,40,90\n"
        "Aggregated,,600,0,12,40,90\n",
        encoding="utf-8",
    )
    locustfile = tmp_path / "locustfile.py"
    locustfile.write_text('TARGETS = ("health",)\n', encoding="utf-8")
    code = runner._judge(str(stats), str(locustfile), "60s", "report")
    out = capsys.readouterr().out
    target = runner.PERFORMANCE_TARGETS["health"]
    line = (
        f"[runner] reported, not gated: GET /health: 600 requests, "
        f"p50 12 ms (target {target.p50_ms:g}), p95 40 ms (target {target.p95_ms:g}), "
        f"p99 90 ms (target {target.p99_ms:g}), 10.0 rps (target {target.min_rps:g})"
    )
    assert line in out.splitlines(), out
    assert code == 0, out


@pytest.mark.regression
def test_the_seed_makes_the_keys_and_the_account_the_locustfiles_use():
    """Read in a fresh interpreter: importing the seeds sets POSTGRES_* defaults."""
    probe = (
        "import json\n"
        "from backend.scripts import seed_demo_data, seed_sdk_contract\n"
        "from backend.tests.performance.locustfiles import common\n"
        "(user,) = [u for u in seed_demo_data.DEMO_USERS"
        " if u['email'] == common.DEFAULT_EMAIL]\n"
        "print(json.dumps({'experiment': [common.EXPERIMENT_KEY,"
        " seed_sdk_contract.EXPERIMENT_KEY], 'flag': [common.FLAG_KEY,"
        " seed_sdk_contract.FLAG_KEY], 'email': common.DEFAULT_EMAIL,"
        " 'superuser': user['is_superuser'], 'role': user['role'].name,"
        " 'password': user['password'] == common.DEFAULT_PASSWORD,"
        " 'seeds_users': seed_sdk_contract.seed_users is seed_demo_data.seed_users}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    found = json.loads(result.stdout.strip().splitlines()[-1])
    assert found == {
        "experiment": ["sdk_contract_ab", "sdk_contract_ab"],
        "flag": ["sdk_contract_flag", "sdk_contract_flag"],
        "email": "dev@demo.com",
        "superuser": False,
        "role": "DEVELOPER",
        "password": True,
        "seeds_users": True,
    }
