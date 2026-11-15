"""
Health probes and the Prometheus scrape endpoint.

Routes (all unauthenticated, none in the OpenAPI schema):

``GET /health/live``
    Liveness: the process is up and the event loop answers. Never touches the
    database or Redis, so a dependency outage does not make orchestrators
    restart healthy processes. Used by the container ``HEALTHCHECK``. The only
    probe declared ``async def``: it runs on the event loop because that is
    what it reports on.

``GET /health/ready``
    Readiness: PostgreSQL must answer ``SELECT 1``. Redis is checked and
    reported but only fails readiness when ``REDIS_REQUIRED=true`` (the
    application degrades gracefully without Redis: rate limiting falls back to
    memory, caching is skipped). Disk space below 1 GB is reported as ``low``.
    The profile the process runs (``core`` or ``full``) is reported as
    ``profile`` and in ``checks.modules``, and never gates readiness. Returns
    200 when ready, 503 otherwise.

    A plain ``def`` route, so FastAPI runs it in the thread pool: the checks
    are blocking calls, and a slow database or Redis holds one worker thread
    instead of the event loop every other request is answered on (#810).

``GET /health``
    Alias of ``/health/ready`` kept for the existing ALB/ECS/CDK wiring.

``GET /metrics``
    Prometheus text exposition. When ``METRICS_TOKEN`` is set the request must
    carry it (``Authorization: Bearer <token>`` or ``?token=<token>``);
    when it is not set the endpoint is open in development/test only and
    returns 403 elsewhere. ``METRICS_ENABLED=false`` disables it (404).

All switches (``METRICS_TOKEN``, ``METRICS_ENABLED``, ``REDIS_REQUIRED``,
``ENVIRONMENT``) are read from ``settings`` (``backend/app/core/config.py``),
which is the single source of truth for the environment.
"""

from __future__ import annotations

import hmac
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(route_class=OffLoopRoute)


# ---------------------------------------------------------------------------
# Settings helpers (tolerant of the pre-/post-canonicalisation config)
# ---------------------------------------------------------------------------


#: ``scheme://user:secret@host`` -- a DSN or a broker URL in an exception
#: message.  The userinfo is the credential; the rest is the diagnosis.
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[A-Za-z][\w+.-]*://)[^\s/@]*:[^\s/@]*@")
#: The account id in an ARN -- botocore puts the whole ARN in its messages.
#: The service and the resource are the diagnosis; the account is not.
_ARN_ACCOUNT = re.compile(r"(arn:[a-z0-9-]*:[a-z0-9-]*:[a-z0-9-]*:)\d{6,}")
#: ``client_secret=abc`` / ``"api-key": "abc"`` -- a named credential with its
#: value attached.  The *name* is worth keeping, the value never is.
#: A ``name = value`` / ``"name": "value"`` pair whose *name* looks secret.
#
# Deliberately dull. An earlier version allowed the name to be surrounded by
# `[\w.\[\]-]*` on both sides and the value to be `'...'|"..."|\S+`, which
# (a) missed the JSON and quoted forms this docstring gives as its own
# examples, because the closing quote sits between the name and the colon, and
# (b) backtracked quadratically: 12.9 s on a 5.5 kB line, minutes on a longer
# one, in a synchronous call inside an async handler on a one-worker
# container -- an unauthenticated readiness probe could hold the event loop.
# The name is a single bounded run, the value is "the rest of the token", and
# nothing nests.
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (["']?)                                  # optional opening quote
    ([\w.\[\]-]{0,40}?
       (?:secret|token|api[-_]?key|credential|passwd|pwd|authorization)
     [\w.\[\]-]{0,40}?)
    \1                                       # its closing quote, if any
    \s*[=:]\s*
    (["']?)[^\s,;}\]]{0,200}\3              # the value, quoted or bare
    """
)


def _scrub(text: str) -> str:
    """One line of *text*, at most 200 characters, carrying no credential.

    The modules check's failure string goes through this before it reaches a
    response body: the readiness probe is unauthenticated, and outside
    production it reports that string.  (The other checks report only an
    error's type -- ``_check_failed``.)  Exceptions from this layer are rich in
    credentials -- psycopg2 quotes the whole DSN, botocore the ARN, authlib
    the client secret it was given -- and none of that is diagnosis.

    Truncation alone is not redaction (the credential is usually in the first
    eighty characters), so the two shapes a secret arrives in are removed
    first, and a message that still mentions a password is dropped whole: the
    common one, ``password authentication failed for user "x"``, says what an
    operator needs without the first line at all.
    """
    first = text.splitlines()[0] if text else ""
    if "password" in first.lower():
        return "redacted: the message carried a credential"
    # Truncate BEFORE substituting, not after. The patterns run in time
    # proportional to what they are given, and this string comes from an
    # arbitrary exception; the cap is the only bound there is. A credential
    # lives in the first characters of these messages anyway -- psycopg2 opens
    # with the DSN, authlib with the parameter it was handed -- so nothing
    # diagnostic is lost, and 200 characters is what the caller gets regardless.
    first = first[:200]
    first = _URL_CREDENTIALS.sub(r"\g<scheme>***:***@", first)
    first = _ARN_ACCOUNT.sub(r"\1***", first)
    first = _SECRET_ASSIGNMENT.sub(r"\1\2\1=***", first)
    return first


def _check_failed(check: str, exc: BaseException) -> Dict[str, Any]:
    """The result of a check that raised: the error's type, never its text.

    The text of a database, Redis or OS error names hosts, ports, users and
    paths, and the readiness probe is unauthenticated, so the response
    carries only the type; the full error goes to the server log.
    """
    logger.warning(
        "Readiness check %s failed (%s)", check, type(exc).__name__, exc_info=exc
    )
    return {"status": "unhealthy", "error": type(exc).__name__}


# Environment and feature switches come from ``settings`` only: pydantic-
# settings already reads the process environment, and ``config.py`` is the
# single place that canonicalises ENVIRONMENT.


def environment_name() -> str:
    """Canonical environment name (``development``, ``test``, ``staging``, ``production``)."""
    return str(settings.ENVIRONMENT)


def is_production() -> bool:
    return bool(settings.is_production)


def is_development_or_test() -> bool:
    return bool(settings.is_development or settings.is_test)


def metrics_token() -> Optional[str]:
    token = settings.METRICS_TOKEN
    return str(token) if token else None


def metrics_enabled() -> bool:
    return bool(settings.METRICS_ENABLED)


def redis_required() -> bool:
    return bool(settings.REDIS_REQUIRED)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_database() -> Dict[str, Any]:
    """``SELECT 1`` on a session from the application pool."""
    try:
        from sqlalchemy import text

        from backend.app.db.session import SessionLocal

        t0 = time.perf_counter()
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
        return {
            "status": "healthy",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
        }
    except Exception as exc:
        return _check_failed("database", exc)


def check_redis() -> Dict[str, Any]:
    """``PING`` Redis using the connection settings.

    One connect attempt, with 1 s connect and socket timeouts. redis-py
    retries ten times with backoff by default, which made one failed ping
    take seconds with Redis refused and longer than the 5 s probe timeouts
    with Redis unreachable (#810).
    """
    try:
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        from backend.app.core.redis_client import create_redis_client

        t0 = time.perf_counter()
        client = create_redis_client(
            socket_connect_timeout=1,
            socket_timeout=1,
            # No client retries: see the docstring.
            retry=Retry(NoBackoff(), 0),
        )
        try:
            client.ping()
        finally:
            try:
                client.close()
            except Exception:  # pragma: no cover - best effort
                pass
        return {
            "status": "healthy",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
        }
    except Exception as exc:
        return _check_failed("redis", exc)


def check_modules() -> Dict[str, Any]:
    """Which profile the process runs, and whether anyone chose it.

    ``healthy`` covers the two intended states: a core build with no
    ``modules`` package (``profile: "core"``) and a full-profile deployment
    whose registration succeeded (``profile: "full"``).  ``unhealthy`` is the
    third state the loader distinguishes -- the package was found and did not
    install -- in which the process serves less than it was deployed as
    without anyone having asked for it.  ``profile`` still says how far it
    got: ``core`` when the registration itself failed (module routes 404 and
    compliance audit events are written unsigned), ``full`` when the
    registration stands and only its routers are missing.

    Reported, not gated: readiness answers "may this instance take traffic",
    and a degraded profile still serves every core route.  Outside development
    and test the state cannot reach a probe at all, because ``main.py`` refuses
    to start on it (``modules_loader.abort_if_modules_broken``); reporting it
    is for the developer running a half-finished module, who would otherwise
    have to infer it from a 404.

    The profile is read, never loaded: ``modules_active()`` answers from the
    loader's cache, where ``load_modules()`` could re-run a whole registration
    (~3.5 s of endpoint imports, under the loader's lock) from inside an
    unauthenticated probe.
    """
    from backend.app.modules_loader import modules_active, modules_failure

    profile = "full" if modules_active() else "core"
    failure = modules_failure()
    if failure is None:
        return {"status": "healthy", "profile": profile}
    # Scrubbed like every other check's error: the loader builds this string
    # out of an arbitrary exception from module code (a psycopg2 DSN, a
    # botocore ARN, an authlib client secret) and this response is
    # unauthenticated.
    return {"status": "unhealthy", "profile": profile, "error": _scrub(failure)}


def check_disk(path: str = "/") -> Dict[str, Any]:
    """Free space on *path*; ``low`` below 1 GB."""
    try:
        usage = shutil.disk_usage(path)
        free_gb = round(usage.free / (1024**3), 2)
        return {"status": "healthy" if free_gb > 1.0 else "low", "free_gb": free_gb}
    except Exception as exc:
        return _check_failed("disk", exc)


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def liveness_payload() -> Dict[str, Any]:
    return {
        "status": "alive",
        "timestamp": _timestamp(),
        "version": str(getattr(settings, "VERSION", "")),
    }


def readiness_payload() -> tuple[Dict[str, Any], int]:
    """Run the readiness checks. Returns ``(body, http_status)``."""
    checks: Dict[str, Dict[str, Any]] = {
        "database": check_database(),
        "redis": check_redis(),
        "disk": check_disk(),
        "modules": check_modules(),
    }

    # The modules check is deliberately not part of ``ready``: see
    # :func:`check_modules`.
    ready = checks["database"]["status"] == "healthy"
    if checks["redis"]["status"] != "healthy" and redis_required():
        ready = False
    if checks["disk"]["status"] == "unhealthy":
        ready = False

    body: Dict[str, Any] = {
        "status": "healthy" if ready else "unhealthy",
        "timestamp": _timestamp(),
        # Which profile is serving, in every environment: an operator reading
        # a production probe should not have to call a route to find out.
        "profile": checks["modules"]["profile"],
    }

    if not is_production():
        # Details (errors, latencies, environment) only outside production.
        body["version"] = str(settings.VERSION)
        body["environment"] = environment_name()
        body["checks"] = checks
    else:
        body["checks"] = {
            name: {"status": data.get("status", "unknown")}
            for name, data in checks.items()
        }

    return body, (200 if ready else 503)


# ---------------------------------------------------------------------------
# /metrics access control
# ---------------------------------------------------------------------------


def _presented_token(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.query_params.get("token") or None


def metrics_access(request: Request) -> Optional[JSONResponse]:
    """Return an error response when the scrape is not allowed, else ``None``."""
    if not metrics_enabled():
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    expected = metrics_token()
    if expected:
        presented = _presented_token(request)
        # Compared as bytes: Starlette decodes headers as latin-1, so a scrape
        # with a non-ASCII byte in its Authorization header hands us a `str`
        # that `hmac.compare_digest` refuses (TypeError -> 500 from an
        # unauthenticated endpoint).  Encoding both sides keeps the comparison
        # constant-time and makes a bad token a 401, which is what it is.
        if not presented or not hmac.compare_digest(
            presented.encode("utf-8"), expected.encode("utf-8")
        ):
            return JSONResponse(
                {"detail": "Invalid or missing metrics token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None

    if is_development_or_test():
        return None

    return JSONResponse(
        {"detail": "The /metrics endpoint requires METRICS_TOKEN outside development"},
        status_code=403,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/health/live", include_in_schema=False)
async def health_live() -> JSONResponse:
    """Liveness probe: process up. Never touches external dependencies."""
    return JSONResponse(content=liveness_payload(), status_code=200)


# The two readiness routes are ``def``, not ``async def``: FastAPI runs them in
# the thread pool, so the blocking checks in ``readiness_payload`` never hold
# the event loop. ``backend/tests/unit/core/test_health_endpoints.py``
# (TestReadinessRunsOffTheEventLoop) drives /health/live while /health is
# blocked.
@router.get("/health/ready", include_in_schema=False)
def health_ready() -> JSONResponse:
    """Readiness probe: database (and Redis when required) reachable."""
    body, status_code = readiness_payload()
    return JSONResponse(content=body, status_code=status_code)


@router.get("/health", include_in_schema=False)
def health_check() -> JSONResponse:
    """Compatibility alias of ``/health/ready``."""
    body, status_code = readiness_payload()
    return JSONResponse(content=body, status_code=status_code)


@router.get("/metrics", include_in_schema=False, response_model=None)
async def prometheus_metrics(request: Request) -> Response:
    """Prometheus text exposition, guarded by ``METRICS_TOKEN`` (see module docstring)."""
    denied = metrics_access(request)
    if denied is not None:
        return denied
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        return PlainTextResponse(
            content=generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("Failed to generate Prometheus metrics: %s", exc)
        return PlainTextResponse(content="# metrics unavailable\n", status_code=503)


__all__ = [
    "check_database",
    "check_disk",
    "check_modules",
    "check_redis",
    "environment_name",
    "is_development_or_test",
    "is_production",
    "liveness_payload",
    "metrics_access",
    "readiness_payload",
    "router",
]
