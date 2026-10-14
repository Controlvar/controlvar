"""What a log line is: structlog, configured once, and the fields it carries.

The API configures logging when ``backend.app.main`` is imported, through
``configure_logging`` in ``backend/app/core/logger.py``. These tests read what
that produces:

* in a fresh interpreter, ``backend.app.main`` imported in each environment:
  one root handler, structlog's formatter, JSON in production and the
  colourised console renderer in development, and the level production logs at;
* in this process, ``configure_logging(json_logs=True)`` rendering lines into a
  buffer: the fields every line carries, and a keyword argument written as it
  was given;
* the application's middleware: what it binds to a request's log lines, and
  that the request-body masking helper is on no request's path;
* the HTTP client's own request lines, which are not written at any level.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

import pytest

from backend.tests.smoke.modules_manifest import MODULES_DIR

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]

# ---------------------------------------------------------------------------
# The configuration main.py makes, in a fresh interpreter per environment
# ---------------------------------------------------------------------------

#: Imports the application, then reports the root logger's handlers and the
#: renderer of their formatter, renders one error line with a traceback through
#: the root handler into a buffer, and writes the result to the file named.
_PROBE = textwrap.dedent(
    """
    import io, json, logging, sys
    import structlog
    import backend.app.main  # noqa: F401  (configures logging on import)

    root = logging.getLogger()
    handlers = list(root.handlers)
    formatter = handlers[0].formatter if handlers else None
    processors = list(getattr(formatter, "processors", []))
    buffer = io.StringIO()
    if handlers:
        handlers[0].setStream(buffer)
    try:
        raise ValueError("probe failure")
    except ValueError:
        logging.getLogger("probe.error").exception("probe line")
    report = {
        "handlers": len(handlers),
        "formatter": type(formatter).__name__,
        "renderer": type(processors[-1]).__name__ if processors else None,
        "configured": structlog.is_configured(),
        "level": logging.getLevelName(root.level),
        "rendered": buffer.getvalue(),
    }
    with open(sys.argv[1], "w", encoding="utf-8") as out:
        json.dump(report, out)
    """
)


def _environment(name: str) -> dict:
    """What the API process sees in *name*; LOG_FORMAT and LOG_LEVEL unset."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"LOG_FORMAT", "LOG_LEVEL", "APP_ENV", "TESTING"}
    }
    env["ENVIRONMENT"] = name
    env["PYTHONPATH"] = str(REPO_ROOT)
    if name == "production":
        env.update(
            SECRET_KEY=secrets.token_urlsafe(48),
            FIRST_SUPERUSER_PASSWORD=secrets.token_urlsafe(24),
            AUDIT_HMAC_KEY=secrets.token_urlsafe(48),
            SSO_STATE_SECRET=secrets.token_urlsafe(48),
            ALLOWED_HOSTS="127.0.0.1",
            PUBLIC_BASE_URL="https://api.example.com",
        )
        for name_ in ("DATABASE_URI", "DATABASE_URL", "REDIS_URI", "REDIS_URL"):
            env.pop(name_, None)
    return env


@pytest.fixture(scope="module")
def started(tmp_path_factory) -> dict:
    """The probe's report, for the development and production environments."""
    reports = {}
    for name in ("development", "production"):
        out = tmp_path_factory.mktemp(name) / "report.json"
        result = subprocess.run(
            [sys.executable, "-c", _PROBE, str(out)],
            cwd=REPO_ROOT,
            env=_environment(name),
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, f"{name}:\n{result.stderr[-3000:]}"
        reports[name] = json.loads(out.read_text(encoding="utf-8"))
    return reports


@pytest.mark.regression
def test_the_api_configures_structlog_once_when_it_starts(started):
    for name, report in started.items():
        assert report["configured"], f"{name}: structlog is not configured"
        assert report["handlers"] == 1, (name, report["handlers"])
        assert report["formatter"] == "ProcessorFormatter", (name, report)


def test_configuring_again_replaces_the_handler_rather_than_adding_one():
    from backend.app.core.logger import configure_logging

    with _configured(json_logs=True) as _:
        configure_logging(log_level="INFO", json_logs=True, stream=io.StringIO())
        assert len(logging.getLogger().handlers) == 1


@pytest.mark.regression
def test_production_writes_one_json_line_and_development_colourised_text(started):
    production, development = started["production"], started["development"]
    assert production["renderer"] == "JSONRenderer", production
    lines = [line for line in production["rendered"].splitlines() if line]
    assert len(lines) == 1, production["rendered"]
    line = json.loads(lines[0])
    assert line["event"] == "probe line"
    assert "ValueError: probe failure" in line["exception"]

    assert development["renderer"] == "ConsoleRenderer", development
    assert "\x1b[" in development["rendered"], "the console line is not colourised"
    with pytest.raises(json.JSONDecodeError):
        json.loads(development["rendered"].splitlines()[0])


@pytest.mark.regression
def test_production_logs_at_info_so_debug_lines_are_not_written(started):
    assert started["production"]["level"] == "INFO"
    with _configured(json_logs=True, log_level=started["production"]["level"]) as out:
        logging.getLogger("probe.level").debug("debug line")
        logging.getLogger("probe.level").info("info line")
    events = [line["event"] for line in out.lines()]
    assert events == ["info line"], events


# ---------------------------------------------------------------------------
# Fields on every line, rendered as production renders them
# ---------------------------------------------------------------------------


_HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")


class _Rendered:
    def __init__(self, buffer: io.StringIO) -> None:
        self.buffer = buffer

    def lines(self) -> list[dict]:
        return [
            json.loads(text) for text in self.buffer.getvalue().splitlines() if text
        ]

    def one(self) -> dict:
        (line,) = self.lines()
        return line


@contextlib.contextmanager
def _configured(json_logs: bool = True, log_level: str = "INFO", **kwargs):
    """``configure_logging`` into a buffer; the previous configuration restored."""
    import structlog

    from backend.app.core.logger import configure_logging

    root = logging.root
    handlers, root_level = list(root.handlers), root.level
    client_levels = {n: logging.getLogger(n).level for n in _HTTP_CLIENT_LOGGERS}
    # What is in force when the API starts: nothing holds the HTTP client back.
    # Importing the application has already quieted these loggers (both
    # configuring functions do), which would hide a call that was removed.
    for name in _HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)
    saved = structlog.get_config()
    buffer = io.StringIO()
    configure_logging(log_level=log_level, json_logs=json_logs, stream=buffer, **kwargs)
    try:
        yield _Rendered(buffer)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(root_level)
        for name, level in client_levels.items():
            logging.getLogger(name).setLevel(level)
        structlog.configure(**saved)


def _structlog(name: str = "probe.fields"):
    from backend.app.core.logger import get_logger

    return get_logger(name)


@pytest.mark.regression
def test_every_keyword_is_its_own_json_field():
    with _configured() as out:
        _structlog().info("experiment assigned", experiment_key="k1", variant="control")
    line = out.one()
    assert line["variant"] == "control"
    assert line["experiment_key"] == "k1"
    assert line["event"] == "experiment assigned"


@pytest.mark.regression
def test_the_timestamp_is_iso_8601():
    with _configured() as out:
        _structlog().info("probe line")
    stamp = out.one()["timestamp"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z", stamp), (
        stamp
    )
    datetime.fromisoformat(stamp.replace("Z", "+00:00"))


@pytest.mark.regression
def test_the_message_is_the_event_field_not_message():
    with _configured() as out:
        _structlog().info("structlog line")
        logging.getLogger("probe.stdlib").info("stdlib line")
    lines = out.lines()
    assert [line["event"] for line in lines] == ["structlog line", "stdlib line"]
    assert all("message" not in line for line in lines), lines


@pytest.mark.regression
def test_the_logger_field_is_the_loggers_name():
    with _configured() as out:
        _structlog("probe.named.structlog").info("one")
        logging.getLogger("probe.named.stdlib").info("two")
    assert [line["logger"] for line in out.lines()] == [
        "probe.named.structlog",
        "probe.named.stdlib",
    ]


@pytest.mark.regression
def test_the_service_field_is_the_configured_service_name():
    with _configured(service_name="probe-service") as out:
        _structlog().info("one")
        logging.getLogger("probe.service").info("two")
    assert [line["service"] for line in out.lines()] == ["probe-service"] * 2


@pytest.mark.regression
def test_the_exception_field_is_json_mode_only_and_needs_exc_info():
    with _configured() as out:
        try:
            raise ValueError("probe failure")
        except ValueError:
            _structlog().error("with exc_info", exc_info=True)
            _structlog().error("without exc_info")
    with_info, without = out.lines()
    assert "Traceback" in with_info["exception"]
    assert "ValueError: probe failure" in with_info["exception"]
    assert "exception" not in without, without

    import structlog

    with _configured(json_logs=False):
        (handler,) = logging.getLogger().handlers
        chain = handler.formatter.processors
    assert structlog.processors.format_exc_info not in chain, chain


@pytest.mark.regression
def test_a_keyword_is_written_as_it_was_given():
    """Nothing in the chain redacts a value: what is passed is what is stored."""
    payload = {"email": "bob@acme.com", "attributes": {"plan": "pro"}}
    with _configured() as out:
        _structlog().info("tracked", email="bob@acme.com", payload=payload)
    line = out.one()
    assert line["email"] == "bob@acme.com"
    assert line["payload"] == payload


# ---------------------------------------------------------------------------
# What the application's middleware does to a request's log lines
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_no_middleware_binds_the_users_id_to_a_log_line():
    """Run every middleware the application mounts around a probe route, with a
    bearer token, and read the log context the route sees: the request's id,
    path and method, and nothing else."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from backend.app.core.logger import get_log_context
    from backend.app.main import app

    async def probe(request):
        return JSONResponse(get_log_context())

    assert app.user_middleware, "the application mounts no middleware"
    wrapped = Starlette(routes=[Route("/probe", probe)], middleware=app.user_middleware)
    with TestClient(wrapped) as client:
        response = client.get(
            "/probe",
            headers={"Authorization": "Bearer probe-token", "X-Request-ID": "probe-1"},
        )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "request_id": "probe-1",
        "path": "/probe",
        "method": "GET",
    }


@pytest.mark.regression
def test_the_application_does_not_mount_the_logging_middleware():
    from backend.app.main import app
    from backend.app.middleware.logging_middleware import (
        LoggingMiddleware,
        RequestLoggingMiddleware,
    )

    mounted = [m.cls for m in app.user_middleware]
    assert mounted, "the application mounts no middleware"
    assert LoggingMiddleware not in mounted, mounted
    assert RequestLoggingMiddleware not in mounted, mounted


def _application_sources() -> list[Path]:
    roots = [REPO_ROOT / "backend" / "app", REPO_ROOT / MODULES_DIR / "backend" / "app"]
    return sorted(p for root in roots if root.is_dir() for p in root.rglob("*.py"))


@pytest.mark.regression
def test_only_the_logging_middleware_names_the_masking_helper():
    """Found by the module's name in the text, so every import form counts:
    ``import``, ``from ... import``, a relative import or a dotted string."""
    helper = REPO_ROOT / "backend" / "app" / "utils" / "masking.py"
    middleware = REPO_ROOT / "backend" / "app" / "middleware" / "logging_middleware.py"
    sources = _application_sources()
    assert helper in sources and middleware in sources
    naming = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in sources
        if p != helper and re.search(r"\bmasking\b", p.read_text(encoding="utf-8"))
    ]
    assert naming == ["backend/app/middleware/logging_middleware.py"], naming


# ---------------------------------------------------------------------------
# Where one user's records are
# ---------------------------------------------------------------------------


def test_the_assignment_event_and_error_tables_keep_the_users_id():
    from backend.app.models.assignment import Assignment
    from backend.app.models.event import Event
    from backend.app.models.metrics.metric import ErrorLog

    tables = {m.__table__.name: m.__table__ for m in (Assignment, Event, ErrorLog)}
    assert sorted(tables) == ["assignments", "error_logs", "events"]
    for name, table in tables.items():
        assert "user_id" in table.columns, name


# ---------------------------------------------------------------------------
# The HTTP client's request lines
# ---------------------------------------------------------------------------

#: What a warehouse call looks like to the HTTP client: the host names the
#: customer's Snowflake account, and httpx writes it, with the query string, on
#: one INFO line per request.
_WAREHOUSE_URL = (
    "https://acme-xy12345.snowflakecomputing.com/api/v2/statements?requestId=r-1"
)


def _post_through_httpx(url: str = _WAREHOUSE_URL) -> None:
    """One request through an httpx client, as the warehouse adapters make it."""
    import httpx

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    with httpx.Client(transport=transport) as client:
        client.post(url)


def _http_client_records(lines: list[dict]) -> list[dict]:
    return [
        line
        for line in lines
        if line["logger"] in _HTTP_CLIENT_LOGGERS
        or line["logger"].startswith(tuple(f"{n}." for n in _HTTP_CLIENT_LOGGERS))
    ]


@pytest.mark.regression
@pytest.mark.parametrize("log_level", ["INFO", "DEBUG"])
def test_the_http_clients_request_lines_are_not_written_at_any_log_level(log_level):
    """``HTTP Request: POST https://<host>/...`` is the line httpx writes at INFO
    for every request, and httpcore's connection trace (``connect_tcp.started
    host='<host>'``) is DEBUG.  Neither is in the log, however low ``LOG_LEVEL``
    is, and neither is the host."""
    with _configured(log_level=log_level) as out:
        _post_through_httpx()
        # What httpcore traces on a real connection (its own logger names).
        logging.getLogger("httpcore.connection").debug(
            "connect_tcp.started host='acme-xy12345.snowflakecomputing.com' port=443"
        )
        logging.getLogger("httpcore.http11").info("send_request_headers.started")
        logging.getLogger("probe.kept").info("kept line")
    lines = out.lines()
    assert "kept line" in [line["event"] for line in lines], "INFO is not written"
    assert _http_client_records(lines) == []
    assert "snowflakecomputing" not in out.buffer.getvalue()
    assert "HTTP Request" not in out.buffer.getvalue()


def test_a_warning_from_the_http_client_is_still_written():
    """The level is WARNING, not off: a failure the client reports gets through.
    Passes before the fix too; it guards against quieting more than the lines."""
    with _configured(log_level="INFO") as out:
        logging.getLogger("httpx").warning("httpx probe warning")
        logging.getLogger("httpcore.connection").error("httpcore probe error")
    assert [line["event"] for line in _http_client_records(out.lines())] == [
        "httpx probe warning",
        "httpcore probe error",
    ]


@pytest.mark.regression
def test_the_older_setup_function_holds_the_http_client_at_warning_too(tmp_path):
    """``backend.app.core.logging.setup_logging`` is the second place the
    application configures logging, and it runs when that module is imported."""
    from backend.app.core.logging import setup_logging

    log_file = tmp_path / "api.log"
    client_levels = {n: logging.getLogger(n).level for n in _HTTP_CLIENT_LOGGERS}
    try:
        # What is in force when the module is imported: nothing quiets httpx.
        for name in _HTTP_CLIENT_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)
        setup_logging(
            level=logging.INFO, format="%(name)s %(message)s", log_file=str(log_file)
        )
        _post_through_httpx()
        logging.getLogger("probe.kept").info("kept line")
    finally:
        for handler in logging.getLogger().handlers:
            handler.close()
        for name, level in client_levels.items():
            logging.getLogger(name).setLevel(level)
    written = log_file.read_text(encoding="utf-8")
    assert "probe.kept kept line" in written, written
    assert "snowflakecomputing" not in written, written
    assert "HTTP Request" not in written, written


#: Imports the application with LOG_LEVEL=INFO, makes one request through an
#: httpx client (MockTransport) and writes what the root handler rendered to the
#: file named by argv[1].  argv[2] is the URL.
_HTTP_PROBE = textwrap.dedent(
    """
    import io, logging, sys
    import httpx
    import backend.app.main  # noqa: F401  (configures logging on import)

    (handler,) = logging.getLogger().handlers
    buffer = io.StringIO()
    handler.setStream(buffer)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    with httpx.Client(transport=transport) as client:
        client.post(sys.argv[2])
    logging.getLogger("probe.kept").info("kept line")
    with open(sys.argv[1], "w", encoding="utf-8") as out:
        out.write(buffer.getvalue())
    """
)


@pytest.mark.regression
def test_the_api_started_at_info_does_not_log_the_hosts_it_calls(tmp_path):
    """The whole start-up path: ``backend.app.main`` in a fresh interpreter, with
    ``LOG_LEVEL=INFO`` set, calls a host through httpx."""
    out = tmp_path / "rendered.txt"
    result = subprocess.run(
        [sys.executable, "-c", _HTTP_PROBE, str(out), _WAREHOUSE_URL],
        cwd=REPO_ROOT,
        env={**_environment("development"), "LOG_LEVEL": "INFO"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    rendered = out.read_text(encoding="utf-8")
    assert "kept line" in rendered, rendered
    assert "snowflakecomputing" not in rendered, rendered
    assert "HTTP Request" not in rendered, rendered
