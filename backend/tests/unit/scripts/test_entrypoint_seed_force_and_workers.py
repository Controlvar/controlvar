"""Two settings the API container's entrypoint reads (#1142).

``backend/docker-entrypoint.sh`` applies each seed in ``SEED`` once per
database: a seed the ``seed_markers`` table already records is skipped, and
``SEED_FORCE=true`` applies it again. It starts uvicorn with
``--workers $WEB_CONCURRENCY``. The real script runs here with ``python`` and
``uvicorn`` replaced by stubs that record their arguments, so no database or
server is needed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
ENTRYPOINT = REPO_ROOT / "backend" / "docker-entrypoint.sh"
BASH = shutil.which("bash")

#: Records every call; a marker check says the seed is already applied.
STUB = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$STUB_LOG"
exit 0
"""

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
