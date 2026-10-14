"""
Structured logging configuration using structlog.

* In production (``json_logs=True``) every log event is rendered as a
  single-line JSON object.  This is what CloudWatch Logs Insights expects.
* In development (``json_logs=False``) log events are rendered as
  human-readable colourised text via structlog's ``dev`` renderer.

Context variable support
------------------------
``bind_log_context`` and ``get_log_context`` let middleware or request
handlers attach fields (``request_id``, ``user_id``, …) that are then
automatically included in **every** log line produced during that request,
without callers having to pass a bound logger around.
"""

import contextvars
import logging
import os
import re
import sys
from typing import Any, Optional

import structlog

from backend.app.core.query_redaction import QUERY_VALUE_REDACTOR

# ---------------------------------------------------------------------------
# Per-request context variable
# ---------------------------------------------------------------------------

_log_context: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "log_context", default={}
)


def bind_log_context(**kwargs: Any) -> None:
    """Merge *kwargs* into the current async-task log context.

    Later calls to this function for the same key will overwrite earlier ones.
    """
    current = _log_context.get().copy()
    current.update(kwargs)
    _log_context.set(current)


def get_log_context() -> dict:
    """Return a copy of the current async-task log context."""
    return _log_context.get().copy()


#: The shape a request id must have to be shown to a user in a response body.
#: The same rule the SSO module applies to the ids it shows.
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def current_request_id() -> Optional[str]:
    """This request's id (the one on its ``X-Request-ID``), if it is safe to show.

    ``RequestIDMiddleware`` binds the id into the log context: the client's
    ``X-Request-ID`` when it sent one, otherwise a fresh UUID4.  Returns None
    when nothing is bound (no middleware), when the value is not a string, or
    when it does not match ``REQUEST_ID_PATTERN`` in full.
    """
    value = _log_context.get().get("request_id")
    if isinstance(value, str) and REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return None


def failure_detail(sentence: str) -> str:
    """A fixed failure message for a response body, with this request's id.

    ``failure_detail("Could not store the event")`` gives
    ``"Could not store the event (request ID: <id>)."`` when
    :func:`current_request_id` returns an id, and
    ``"Could not store the event."`` when it does not.  The id is the one on
    the response's ``X-Request-ID`` header and in the server log, so a
    failure the caller reports can be found there.
    """
    request_id = current_request_id()
    if request_id is None:
        return f"{sentence}."
    return f"{sentence} (request ID: {request_id})."


def unexpected_failure(
    exc: BaseException,
    operation: str,
    sentence: str,
    *,
    db: Any = None,
    logger: Any = None,
    status_code: int = 500,
) -> Any:
    """The ``HTTPException`` for a failure nobody planned for.

    ``raise unexpected_failure(e, "Flag toggle", "Could not toggle the
    feature flag", db=db, logger=logger)`` inside an ``except`` block:

    * rolls ``db`` back when one is given (a failed rollback is logged, not
      raised, so it cannot replace the original error);
    * logs ``"<operation> failed (<exception type>)"`` at ERROR with the full
      traceback, under this request's ID;
    * returns an ``HTTPException`` with ``status_code`` whose ``detail`` is
      :func:`failure_detail` of ``sentence`` -- never the error's own text.
    """
    # Imported here so this module stays importable without FastAPI.
    from fastapi import HTTPException

    log = logger if logger is not None else logging.getLogger(__name__)
    if db is not None:
        try:
            db.rollback()
        except Exception:
            log.warning("Rollback after a failed %s also failed", operation)
    # What logger.exception() logs, from outside the except block: ERROR,
    # with the traceback.
    log.error("%s failed (%s)", operation, type(exc).__name__, exc_info=exc)
    return HTTPException(status_code=status_code, detail=failure_detail(sentence))


# ---------------------------------------------------------------------------
# structlog context-variable processor
# ---------------------------------------------------------------------------


def _inject_context(
    logger: Any,
    method: str,
    event_dict: dict,
) -> dict:
    """structlog processor: inject per-request context into every log event."""
    event_dict.update(_log_context.get())
    return event_dict


# ---------------------------------------------------------------------------
# Public configuration helpers
# ---------------------------------------------------------------------------

#: The HTTP client's own loggers.  httpx writes one INFO line per request --
#: ``HTTP Request: POST https://<account>.snowflakecomputing.com/... "HTTP/1.1
#: 200 OK"`` -- with the full URL, so at the default ``LOG_LEVEL=INFO`` every
#: call to a warehouse put its host (the customer's account or project) in the
#: API log.  httpcore traces connections at DEBUG, host included.
HTTP_CLIENT_LOGGERS: tuple[str, ...] = ("httpx", "httpcore")


def quiet_http_client_loggers() -> None:
    """Hold the HTTP client's loggers at WARNING, whatever the root level is.

    A level on the named logger wins over the root's, so this applies to every
    handler the process installs.  WARNING and above (a failed request) still
    get through.  Called by both functions that configure logging
    (:func:`configure_logging` and ``backend.app.core.logging.setup_logging``).
    """
    for name in HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def configure_logging(
    log_level: str = "INFO",
    json_logs: bool = True,
    service_name: str = "experimentation-platform",
    stream: Any = None,
) -> None:
    """Configure structlog **and** the standard library for the application.

    Should be called once at startup (e.g. from ``main.py``). Safe to call
    multiple times — each call replaces the previous configuration.

    Both structlog loggers (``get_logger``) and plain ``logging.getLogger``
    loggers (the majority of the codebase, plus uvicorn, sqlalchemy, ...) are
    rendered by the same :class:`structlog.stdlib.ProcessorFormatter` on a
    single root handler, so with ``json_logs=True`` **every** line the process
    writes is one JSON object (``LOG_FORMAT=json`` in production/containers)
    and with ``json_logs=False`` every line is the coloured console format.

    Args:
        log_level: Python log-level name (``"DEBUG"``, ``"INFO"``, etc.).
        json_logs: ``True`` for JSON output (production/CloudWatch),
            ``False`` for colourised dev output.
        service_name: Value added to every log event under the ``service``
            key for log-aggregation filtering.
        stream: Destination stream (defaults to ``sys.stdout``).
    """
    numeric_level: int = getattr(logging, log_level.upper(), logging.INFO)
    stream = stream or sys.stdout

    def _add_service(logger: Any, method: str, event_dict: dict) -> dict:
        event_dict.setdefault("service", service_name)
        return event_dict

    # Processors shared by structlog events and foreign (stdlib) records.
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        _inject_context,
        _add_service,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if json_logs:
        renderer: Any = structlog.processors.JSONRenderer()
        # Turn exc_info into a string field ("exception") for JSON output.
        pre_render: list = [structlog.processors.format_exc_info]
    else:
        renderer = structlog.dev.ConsoleRenderer()
        pre_render = []

    structlog.configure(
        processors=shared_processors
        + [
            # Hand the event dict to the stdlib handler/formatter below.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # Processors applied to every record right before rendering.
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta]
        + pre_render
        + [renderer],
        # Processors applied to records that did NOT originate from structlog.
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(numeric_level)
    quiet_http_client_loggers()

    # uvicorn installs its own handlers (plain text, propagate=False). Route
    # them through the root handler so access/error lines share the format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        for existing in list(uv_logger.handlers):
            uv_logger.removeHandler(existing)
        uv_logger.propagate = True

    # uvicorn writes the request target, query string included, on its access
    # lines and its WebSocket handshake lines (uvicorn.error). The filter
    # replaces the values listed in backend/app/core/query_redaction.py. It is
    # a filter on the logger, not on the handler, so it applies to whichever
    # handler ends up writing the line.
    for name in ("uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addFilter(QUERY_VALUE_REDACTOR)


def log_format_from_env(default: str = "console") -> str:
    """Resolve ``LOG_FORMAT`` (``json`` | ``console``) from the environment."""
    value = os.environ.get("LOG_FORMAT", "").strip().lower()
    if value in ("json", "console", "text"):
        return "json" if value == "json" else "console"
    return default


def get_logger(name: str = __name__) -> structlog.BoundLogger:
    """Return a structlog BoundLogger bound to *name*.

    Args:
        name: Logger name, typically ``__name__`` of the calling module.

    Returns:
        A structlog ``BoundLogger`` instance.
    """
    return structlog.get_logger(name)
