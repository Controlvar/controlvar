"""``python -m backend.app.db.bootstrap`` answers ``--help`` without a database.

It used to ignore its arguments: ``--help`` ran the whole bootstrap, so an
operator who only wanted the usage line got "Using schema ..." and then a
connection error (or, against a reachable database, a migration). The
repository's argparse commands answer ``--help`` with exit status 0.

The no-argument call is the one the image's entrypoint, the migration task, the
Makefile and the CI jobs make; it must run :func:`bootstrap` exactly as before.

No database: the subprocesses are pointed at a socket this test listens on, and
the test checks that nothing connected to it.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from backend.app.db import bootstrap

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]


def _run(*args: str) -> tuple[subprocess.CompletedProcess, bool]:
    """Run the module with *args*; True when it connected to the database port.

    The listener closes every connection at once, so a run that does try to
    connect fails fast instead of waiting for a server that never answers.
    """
    connections: list[int] = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        listener.settimeout(0.2)
        stop = threading.Event()

        def accept() -> None:
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                except OSError:
                    continue
                connections.append(1)
                connection.close()

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        env = {
            **os.environ,
            "POSTGRES_SERVER": "127.0.0.1",
            "POSTGRES_PORT": str(listener.getsockname()[1]),
        }
        try:
            done = subprocess.run(
                [sys.executable, "-m", "backend.app.db.bootstrap", *args],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
        finally:
            stop.set()
            thread.join()
    return done, bool(connections)


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_the_usage_and_exits_0_without_connecting(flag):
    done, connected = _run(flag)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("usage: python -m backend.app.db.bootstrap")
    assert "Traceback" not in done.stderr
    assert not connected, "--help opened a database connection"


def test_an_unknown_argument_exits_2_without_connecting():
    done, connected = _run("--wrong")
    assert done.returncode == 2
    assert "unrecognized arguments: --wrong" in done.stderr
    assert not connected, "an unknown argument opened a database connection"


def test_an_abbreviated_flag_is_not_taken_for_help():
    done, connected = _run("--he")
    assert done.returncode == 2
    assert not connected


def test_with_no_argument_it_runs_the_bootstrap_and_says_so(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(bootstrap, "bootstrap", lambda: calls.append(()) or "created")
    monkeypatch.setattr(bootstrap, "schema_name", lambda: "the_schema")
    monkeypatch.setattr(bootstrap, "database_url", lambda: "user:pw@db.example:5432/x")
    bootstrap.main([])
    assert calls == [()]
    assert capsys.readouterr().out == (
        "Database bootstrap complete (created) — schema 'the_schema' at "
        "db.example:5432/x\n"
    )


def test_a_refused_first_administrator_still_exits_1(monkeypatch):
    def refuse():
        raise bootstrap.FirstSuperuserRefused("set FIRST_SUPERUSER_PASSWORD")

    monkeypatch.setattr(bootstrap, "bootstrap", refuse)
    with pytest.raises(SystemExit) as raised:
        bootstrap.main([])
    assert raised.value.code == 1
