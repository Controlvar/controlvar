"""The request-ID and metrics middleware are required, in every environment (#1041).

``backend/app/main.py`` used to wrap their imports in ``try/except Exception``.
A broken module then left the API serving with no ``X-Request-ID``, no
Prometheus histogram and no structured logging, and nothing said so. The import
is now plain: a module that cannot be imported stops ``import backend.app.main``.

Each case runs ``import backend.app.main`` in a fresh interpreter with
``sys.modules[M] = None`` (Python then raises ``ModuleNotFoundError`` for ``M``).
The working directory is the repository root taken from this file, so the test
needs no git and runs in ``scripts/core_build.sh``'s copy (no ``.git``, no
``modules/``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

MIDDLEWARE_MODULES = (
    "backend.app.middleware.request_id_middleware",
    "backend.app.middleware.prometheus_metrics_middleware",
)

_BLOCK = (
    "import sys\n"
    "for m in sys.argv[1:]:\n"
    "    sys.modules[m] = None\n"
    "import backend.app.main as main\n"
    "print('CLASSES=' + ','.join(m.cls.__name__ for m in main.app.user_middleware))\n"
)


def _run(blocked: tuple[str, ...], environment: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        {
            "ENVIRONMENT": environment,
            "APP_ENV": environment,
            "TESTING": "true",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return subprocess.run(
        [sys.executable, "-B", "-c", _BLOCK, *blocked],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.regression
@pytest.mark.parametrize("environment", ["development", "test"])
@pytest.mark.parametrize("module", MIDDLEWARE_MODULES)
def test_api_does_not_start_when_a_monitoring_middleware_cannot_be_imported(
    module: str, environment: str
) -> None:
    result = _run((module,), environment)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "ModuleNotFoundError" in result.stderr
    assert module in result.stderr


def test_api_starts_with_both_monitoring_middleware_when_nothing_is_blocked() -> None:
    """The positive control: without the block the same subprocess exits 0."""
    result = _run((), "test")
    assert result.returncode == 0, result.stderr
    line = next((x for x in result.stdout.splitlines() if x.startswith("CLASSES=")), "")
    classes = line.removeprefix("CLASSES=").split(",")
    assert "RequestIDMiddleware" in classes
    assert "PrometheusMetricsMiddleware" in classes
