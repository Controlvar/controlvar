"""Two settings the API container's entrypoint reads (#1142).

``backend/docker-entrypoint.sh`` applies each seed in ``SEED`` once per
database: a seed the ``seed_markers`` table already records is skipped, and
``SEED_FORCE=true`` applies it again. It starts uvicorn with
``--workers $WEB_CONCURRENCY``. The real script runs here with ``python`` and
``uvicorn`` replaced by stubs that record their arguments, so no database or
server is needed. ``python -m backend.app.core.proxy_trust``, the list of
proxies uvicorn is started with, is answered by the real module, run by the
interpreter running these tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
ENTRYPOINT = REPO_ROOT / "backend" / "docker-entrypoint.sh"
BASH = shutil.which("bash")

#: Records every call; a marker check says the seed is already applied. The
#: trusted-proxy list is the real module's (STUB_ENV says where it is).
STUB = """#!/usr/bin/env bash
if [ "$*" = "-m backend.app.core.proxy_trust" ]; then
    PYTHONPATH="$STUB_REPO_ROOT" exec "$STUB_REAL_PYTHON" -m backend.app.core.proxy_trust
fi
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$STUB_LOG"
exit 0
"""

#: What STUB needs to run the real ``backend.app.core.proxy_trust``.
STUB_ENV = {"STUB_REAL_PYTHON": sys.executable, "STUB_REPO_ROOT": str(REPO_ROOT)}

pytestmark.append(
    pytest.mark.skipif(
        BASH is None or not ENTRYPOINT.is_file(),
        reason="needs bash and backend/docker-entrypoint.sh",
    )
)


def run(tmp_path: Path, *command: str, **env_vars: str) -> Tuple[int, str, str]:
    """The entrypoint with *command*, ``cd /app`` sent to *tmp_path*."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("python", "uvicorn"):
        stub = bin_dir / name
        stub.write_text(STUB)
        stub.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "STUB_LOG": str(log),
        "APP_DIR_FOR_TEST": str(tmp_path),
        "WAIT_FOR_DB": "false",
        "RUN_MIGRATIONS": "false",
        "OPENSSL_armcap": "0",
        "ENVIRONMENT": "development",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        **STUB_ENV,
        **env_vars,
    }
    driver = (
        'cd() { builtin cd "$APP_DIR_FOR_TEST"; }; export -f cd; exec bash "$0" "$@"'
    )
    result = subprocess.run(
        [BASH, "-c", driver, str(ENTRYPOINT), *command],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode, result.stderr, log.read_text()


def test_a_recorded_seed_is_skipped_and_seed_force_applies_it_again(tmp_path):
    code, stderr, calls = run(tmp_path / "once", "true", SEED="demo")
    assert code == 0, stderr
    assert "python -m backend.scripts.seed_markers check demo" in calls
    assert "seed_demo_data.py" not in calls
    assert "seed 'demo' already applied; skipping" in stderr

    code, stderr, calls = run(
        tmp_path / "forced", "true", SEED="demo", SEED_FORCE="true"
    )
    assert code == 0, stderr
    assert "python backend/scripts/seed_demo_data.py" in calls
    assert "python -m backend.scripts.seed_markers mark demo" in calls


@pytest.mark.parametrize("workers", ["1", "3"])
def test_web_concurrency_is_the_number_of_uvicorn_workers(tmp_path, workers):
    code, stderr, calls = run(
        tmp_path, "uvicorn", "backend.app.main:app", WEB_CONCURRENCY=workers
    )
    assert code == 0, stderr
    started = [line for line in calls.splitlines() if line.startswith("uvicorn ")]
    assert len(started) == 1, calls
    assert f" --workers {workers}" in started[0] + " ", started[0]


#: Records every call, and fails the bootstrap when BOOTSTRAP_FAILS is set.
FAILING_BOOTSTRAP = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$STUB_LOG"
if [ -n "${BOOTSTRAP_FAILS:-}" ] && [ "$*" = "-m backend.app.db.bootstrap" ]; then
    exit 1
fi
exit 0
"""


def _started(calls: str) -> list[str]:
    """The bootstrap and server lines, in the order the entrypoint ran them."""
    return [
        line
        for line in calls.splitlines()
        if line == "python -m backend.app.db.bootstrap" or line.startswith("uvicorn ")
    ]


@pytest.mark.regression
def test_by_default_the_bootstrap_runs_on_every_start_before_the_api_serves(tmp_path):
    """RUN_MIGRATIONS unset (empty here) is the image's default, true: the
    bootstrap, then uvicorn; the Dockerfile does not change that default."""
    for attempt in ("first", "second"):
        code, stderr, calls = run(
            tmp_path / attempt, "uvicorn", "backend.app.main:app", RUN_MIGRATIONS=""
        )
        assert code == 0, stderr
        started = _started(calls)
        assert started[0] == "python -m backend.app.db.bootstrap", calls
        assert len(started) == 2 and started[1].startswith("uvicorn "), calls
    dockerfile = (REPO_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    assert "RUN_MIGRATIONS" not in dockerfile.replace(
        "schema when RUN_MIGRATIONS=true", ""
    ), "the Dockerfile sets RUN_MIGRATIONS"
    assert 'ENTRYPOINT ["/app/backend/docker-entrypoint.sh"]' in dockerfile


@pytest.mark.regression
def test_the_api_does_not_start_when_the_bootstrap_fails(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("python", FAILING_BOOTSTRAP), ("uvicorn", STUB)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "STUB_LOG": str(log),
        "APP_DIR_FOR_TEST": str(tmp_path),
        "WAIT_FOR_DB": "false",
        "RUN_MIGRATIONS": "true",
        "BOOTSTRAP_FAILS": "1",
        "OPENSSL_armcap": "0",
        "ENVIRONMENT": "development",
    }
    driver = (
        'cd() { builtin cd "$APP_DIR_FOR_TEST"; }; export -f cd; exec bash "$0" "$@"'
    )
    result = subprocess.run(
        [BASH, "-c", driver, str(ENTRYPOINT), "uvicorn", "backend.app.main:app"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, result.stderr
    assert _started(log.read_text()) == ["python -m backend.app.db.bootstrap"]


@pytest.mark.regression
def test_run_migrations_false_skips_the_bootstrap(tmp_path):
    code, stderr, calls = run(
        tmp_path, "uvicorn", "backend.app.main:app", RUN_MIGRATIONS="false"
    )
    assert code == 0, stderr
    started = _started(calls)
    assert len(started) == 1 and started[0].startswith("uvicorn "), calls
    assert "RUN_MIGRATIONS=false: skipping bootstrap" in stderr
