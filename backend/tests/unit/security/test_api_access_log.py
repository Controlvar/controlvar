"""The API's server keeps uvicorn's access log, which records each request's address (#1545).

The visual script's install guide says that the Controlvar server's access
logs record each request's address. The dashboard's nginx side is pinned by
``test_dashboard_access_log.py``. The API side is uvicorn's own access log,
on unless it is started with ``--no-access-log`` (or ``access_log=False``):
this pins that neither the image's command nor the entrypoint, which fills
in uvicorn's arguments, turns it off.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SERVER_FILES = (
    REPO_ROOT / "backend" / "Dockerfile",
    REPO_ROOT / "backend" / "docker-entrypoint.sh",
    REPO_ROOT / "backend" / "app" / "main.py",
)
OFF = ("--no-access-log", "access_log=False", "access_log: false")


def access_log_switches(text: str) -> list:
    return [switch for switch in OFF if switch in text]


def test_the_api_keeps_uvicorns_access_log():
    for path in SERVER_FILES:
        assert path.is_file(), path
        assert access_log_switches(path.read_text(encoding="utf-8")) == [], path


def test_the_switch_is_found_where_it_would_be_written():
    assert access_log_switches(
        "set -- uvicorn backend.app.main:app --host 0.0.0.0 --no-access-log"
    ) == ["--no-access-log"]
