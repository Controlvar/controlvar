"""
Main FastAPI application module.

This module initializes the FastAPI application with all necessary middleware,
routers, and configuration.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi

# Import routers and settings
from backend.app.api.api import api_router, tags_metadata
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.core.bandit_scheduler import bandit_scheduler_runner
from backend.app.core.config import cors_start_up_messages, settings
from backend.app.core.finite_json import refuse_non_finite_json
from backend.app.core.health import is_development_or_test
from backend.app.core.health import router as health_router
from backend.app.core.logger import configure_logging, log_format_from_env
from backend.app.core.metrics_scheduler import metrics_scheduler
from backend.app.core.rollout_scheduler import rollout_scheduler
from backend.app.core.safety_scheduler import safety_scheduler
from backend.app.core.scheduler import experiment_scheduler
from backend.app.core.settings_rules import ENVIRONMENT_NOT_SET_MESSAGE
from backend.app.core.validation_errors import request_validation_error_handler
from backend.app.middleware.demo_mode_middleware import DemoModeMiddleware
from backend.app.middleware.prometheus_metrics_middleware import (
    PrometheusMetricsMiddleware,
)
from backend.app.middleware.rate_limiter import RateLimitMiddleware
from backend.app.middleware.relative_redirect_middleware import (
    RelativeSlashRedirectMiddleware,
)
from backend.app.middleware.request_body_limit_middleware import (
    RequestBodyLimitMiddleware,
)
from backend.app.middleware.request_id_middleware import RequestIDMiddleware
from backend.app.middleware.security_middleware import SecurityHeadersMiddleware
from backend.app.middleware.trusted_host_middleware import TrustedHostMiddleware
from backend.app.middleware.unhandled_error_middleware import UnhandledErrorMiddleware
from backend.app.modules_loader import abort_if_modules_broken, load_modules

# ---------------------------------------------------------------------------
# Structured logging — initialise before anything else so that startup
# log messages are captured in the correct format.
#
# LOG_FORMAT=json  -> every line (structlog, stdlib, uvicorn) is one JSON object
# LOG_FORMAT=console -> coloured console output
# unset            -> console in development/test, JSON everywhere else
# ---------------------------------------------------------------------------
_log_format: str = str(
    getattr(settings, "LOG_FORMAT", "") or ""
).lower() or log_format_from_env(
    default="console" if is_development_or_test() else "json"
)
_json_logs: bool = _log_format == "json"
configure_logging(
    log_level=str(settings.LOG_LEVEL or "INFO").upper(),
    json_logs=_json_logs,
    service_name="experimentation-platform",
)

# The logger and the two monitoring middleware (EP-013) are imported plainly, so
# in every environment a module that cannot be imported stops the API from
# starting (#1041) rather than leaving it serving with no request IDs or metrics.

# Standard-library logger (used by the existing schedulers etc.)
logger = logging.getLogger(__name__)

# The seam: install the modules' hooks (routers, models, tags, audit signer).
# Importing backend.app.api.api above already triggered this while it built
# the v1 router; the call is repeated here (it is idempotent) so that main.py
# — the process entry point — names the seam explicitly.  A core build has no
# `modules` package: nothing loads and every hook keeps its default.
if load_modules():
    logger.info("Modules hooks installed")
# Nothing to load is the core profile and starts cleanly.  A package that *was*
# found and did not install -- the registration raised, or its routers would
# not mount -- is a full-profile deployment that would serve less than it was
# deployed as (module routes 404, and on a failed registration compliance
# events unsigned), so outside development/test this raises and the process
# never starts.  Unconditional, because a routers-only failure leaves
# `load_modules()` True: it is `modules_failure()` that decides, not the
# return value.
abort_if_modules_broken()

if settings.dev_auth_bypass_active:
    logger.warning(
        "DEV_AUTH_BYPASS is active (ENVIRONMENT=%s): every request is served as the "
        "synthetic dev-admin superuser without credentials. Never expose this instance.",
        settings.ENVIRONMENT,
    )

# The largest request body the API reads: 5 MiB. A larger one is answered 413
# on every route (RequestBodyLimitMiddleware, registered below). It admits the
# largest documented request, 10,000 segment member IDs of 255 characters
# (2,590,009 bytes as JSON), and it equals the dashboard proxy's
# `client_max_body_size 5m` (frontend/nginx.conf). A constant, not a setting.
MAX_REQUEST_BODY_SIZE: int = 5 * 1024 * 1024


class EnvironmentNotSet(RuntimeError):
    """The API was started without ENVIRONMENT (see :func:`lifespan`)."""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage background scheduler lifecycle for the FastAPI app.

    Under the test profile the schedulers are not started: a ``TestClient``
    used as a context manager would otherwise run real ticks against the test
    database in the middle of unrelated tests (they patch the same service
    functions, and the ticks open and drop connections). Scheduler behaviour
    is covered directly in ``backend/tests/unit/core/test_scheduler_*``.

    First, it refuses to serve when ENVIRONMENT was never set: the settings
    then default to development, whose relaxed rules a deployment must choose
    rather than fall into. Importing the application (the OpenAPI dump, the
    release's version check) is unaffected; only serving it is refused.
    """
    if not settings.environment_explicit:
        logger.error(ENVIRONMENT_NOT_SET_MESSAGE)
        raise EnvironmentNotSet(ENVIRONMENT_NOT_SET_MESSAGE)

    if settings.is_test:
        logger.info("Test environment: background schedulers are not started")
        yield
        return

    logger.info("Starting experiment scheduler")
    # The singleton is built at import with the class default; the configured
    # cadence is applied here, before its loop starts (#484). It stays the one
    # instance, because POST /experiments/schedules/process also uses it.
    experiment_scheduler.interval_minutes = (
        settings.EXPERIMENT_SCHEDULER_INTERVAL_MINUTES
    )
    await experiment_scheduler.start()

    logger.info("Starting rollout scheduler")
    await rollout_scheduler.start()

    logger.info("Starting metrics scheduler")
    await metrics_scheduler.start()

    logger.info("Starting safety monitoring scheduler")
    await safety_scheduler.start()

    logger.info("Starting bandit scheduler")
    await bandit_scheduler_runner.start()

    yield

    logger.info("Stopping experiment scheduler")
    await experiment_scheduler.stop()

    logger.info("Stopping rollout scheduler")
    await rollout_scheduler.stop()

    logger.info("Stopping metrics scheduler")
    await metrics_scheduler.stop()

    logger.info("Stopping safety monitoring scheduler")
    await safety_scheduler.stop()

    logger.info("Stopping bandit scheduler")
    await bandit_scheduler_runner.stop()


# Create FastAPI application
app = FastAPI(
    title=settings.PROJECT_NAME,
    description=settings.PROJECT_DESCRIPTION,
    version=settings.VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    # Core tag descriptions plus whatever the modules' registration
    # contributed through hooks.register_tags(); computed at router build, so
    # it is read here after `api_router` has been imported above.
    openapi_tags=tags_metadata,
    lifespan=lifespan,
    # A JSON request body holding NaN, Infinity or a number too large for a
    # double is answered 422 `finite_number` (#1630). Passed here, to the
    # constructor, so every router included below runs it; a dependency
    # added to the application after the routers are included is not run.
    dependencies=[Depends(refuse_non_finite_json)],
)
# Routes declared on the app itself use the route class every router uses.
app.router.route_class = OffLoopRoute

# A request that fails validation is answered with each error's type, loc, msg
# and ctx, and without the submitted values (#500). Every route included below,
# core and module alike, gets it; see backend/app/core/validation_errors.py.
app.add_exception_handler(RequestValidationError, request_validation_error_handler)

# ---------------------------------------------------------------------------
# Middleware stack
#
# `add_middleware` inserts at the FRONT of `app.user_middleware`, and Starlette
# builds the stack by wrapping in reverse of that list -- so the LAST
# registered call is the outermost layer, not the first. (Measured, not read:
# with CORS registered first and the rate limiter second, `user_middleware`
# prints the rate limiter first, and a 429 it returned never reached CORS.)
#
# CORS is therefore registered last, so every response a middleware below it
# returns -- a 429 from the rate limiter especially -- carries the
# `Access-Control-*` headers a browser needs before it will let the page read
# the status. Without that, a rate-limited login surfaced in the dashboard as
# "Can't reach the API" (#85).
# ---------------------------------------------------------------------------

# CORS origins — resolved here, registered LAST (below), because the
# registration order is what decides the nesting.
# BACKEND_CORS_ORIGINS, else CORS_ORIGINS, else the localhost defaults in
# development and test and nothing in staging/production (#130), plus any
# DASHBOARD_ORIGINS; every entry normalised to the form a browser's Origin
# header takes (#126).
cors_origins = settings.cors_allowed_origins
for _level, _message in cors_start_up_messages(settings):
    logger.log(_level, _message)

# An unhandled route exception becomes a 500 the browser can read (#72).
# Registered FIRST, i.e. innermost of all the layers below, so its 500 passes
# back out through every one of them -- CORS, the request id, the security
# headers -- instead of being sent by Starlette's outermost ServerErrorMiddleware
# with none of them. Its position is asserted in test_unhandled_error_cors.py.
app.add_middleware(UnhandledErrorMiddleware)

# A request body larger than MAX_REQUEST_BODY_SIZE is answered 413, on every
# route: at once when its Content-Length says so, otherwise when the bytes read
# pass the limit. Registered directly after the error layer, so it is inside
# CORS, the request id and the response headers layer, and its 413 carries
# their headers like any other response. It must also stay inside every
# BaseHTTPMiddleware layer: registered outside one, the route reading the body
# is handed an ExceptionGroup instead of the HTTPException, and FastAPI answers
# a chunked body over the limit with 400 (measured). No layer outside it reads
# the body.
app.add_middleware(RequestBodyLimitMiddleware, max_body_size=MAX_REQUEST_BODY_SIZE)

# Trailing-slash redirects keep the client's own origin (#86). Registered
# next, so it sits just outside the two layers above (neither changes a
# redirect) and sees the router's redirect before anything else can act on it.
# (Starlette still inserts its own ExceptionMiddleware between these and the
# router.)
app.add_middleware(RelativeSlashRedirectMiddleware)

# Rate limiter — disabled during tests to avoid interfering with test assertions
_rate_limit_enabled = not settings.is_test
app.add_middleware(RateLimitMiddleware, enabled=_rate_limit_enabled)

# Demo mode (DEMO_MODE=true): every write except an exact allow-list of eight
# POSTs is answered 403 `demo_read_only`, and every websocket is refused, before
# routing, the body, authentication or a database session
# (backend/app/core/demo_mode.py). Off, it passes everything through.
# Registered after the rate limiter and before the trusted-host check: outside
# the limiter, so a refusal costs no bucket; inside TrustedHost, so a forged
# `Host` still gets its 400 first; and inside CORS, the request id and the
# security headers, so the 403 carries them and a dashboard can read it.
# test_demo_mode_middleware.py pins the order.
app.add_middleware(DemoModeMiddleware)

# Trusted Host — refuse a request whose `Host` is not one of ours (#220).
# `Host` is attacker-controlled on any path that reaches the app, and absolute
# URLs built from it make a crafted one dangerous; `PUBLIC_BASE_URL` already
# removed the OIDC `redirect_uri` from that category, and this covers every
# handler that has not been audited and every one not yet written.
#
# `effective_allowed_hosts`, not `ALLOWED_HOSTS`: with the latter unset it
# derives from PUBLIC_BASE_URL's host, which is the URL users actually reach
# the service at. The earlier attempt at this defaulted to the load balancer's
# own DNS name and would have refused 100% of user traffic while every health
# check stayed green.
#
# Registered AFTER the rate limiter and BEFORE the security headers, putting it
# outside the limiter and inside SecurityHeadersMiddleware. Both halves matter:
#
#   outside the limiter  a forged `Host` costs a header comparison, not a
#                        rate-limit bucket and a database session.
#   inside the headers   its 400 is a response a hostile client is by
#                        construction most likely to see, so it carries the
#                        same headers as everything else -- exactly why the
#                        429 below is positioned the way it is.
#
# The probes are exempt inside the middleware: the ALB health-checks `/health`
# with the TARGET'S OWN IP as `Host`, so without that a correct allow-list
# fails every probe and rolls the deployment back with the app itself healthy.
app.add_middleware(
    TrustedHostMiddleware, allowed_hosts=settings.effective_allowed_hosts
)

# Security headers — lightweight and stateless. Registered AFTER the rate
# limiter, i.e. outside it, for the same reason CORS is registered outside
# everything: the limiter returns its 429 without calling `call_next`, so any
# layer nested inside it never runs. With this the wrong way round the 429 —
# the one response on this path a hostile client is most likely to see — went
# out with no `X-Content-Type-Options`, no `X-Frame-Options`, no CSP and, in
# production, no HSTS.
app.add_middleware(SecurityHeadersMiddleware)

# Prometheus latency / request-count middleware (EP-013)
app.add_middleware(PrometheusMetricsMiddleware)
# X-Request-ID on every response + request_id/path/method bound into the
# log context. Registered after the middlewares it should wrap, so every
# log line from them already carries the id.
app.add_middleware(RequestIDMiddleware)

# CORS last, i.e. outermost -- see the note at the top of this block.
#
# A preflight is answered here and goes no further. That is also why it no
# longer spends a request from the login limit, and it is a deliberate
# trade: a preflight response now carries no security headers, no
# `X-Request-ID`, and appears in no Prometheus histogram. A CORS preflight
# has no body and reaches no route, so what is lost is observability of the
# handshake, not of the request it precedes.
#
# `expose_headers` is what a browser will let the page READ. Without
# `Retry-After` in it, a rate-limited login can be seen but not explained:
# the dashboard knows it was refused and not for how long.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    # No first-party client sends credentials cross-origin: the dashboard
    # authenticates with a bearer header, and SSO's state cookie travels on a
    # top-level navigation. Off, a `*` entry can never be combined with them.
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    expose_headers=[
        "X-Request-ID",
        "Retry-After",
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Window",
        # The audit log export's entry count, which the dashboard compares
        # with the rows it received before it saves the file.
        "X-Total-Count",
    ],
)

# Include API router
app.include_router(api_router, prefix=settings.API_V1_STR)

# Health probes (/health/live, /health/ready, /health) and Prometheus /metrics
app.include_router(health_router)


# ---------------------------------------------------------------------------
# OpenAPI documentation routes
# ---------------------------------------------------------------------------


@app.get("/api/v1/openapi.json", include_in_schema=False)
async def get_openapi_schema():
    return get_openapi(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        description=settings.PROJECT_DESCRIPTION,
        routes=app.routes,
    )


@app.get("/api/v1/docs", include_in_schema=False)
async def get_swagger_docs():
    return get_swagger_ui_html(
        openapi_url="/api/v1/openapi.json",
        title=f"{settings.PROJECT_NAME} - Swagger UI",
    )


@app.get("/api/v1/redoc", include_in_schema=False)
async def get_redoc_docs():
    return get_redoc_html(
        openapi_url="/api/v1/openapi.json",
        title=f"{settings.PROJECT_NAME} - ReDoc",
    )


# This allows other modules to import the application instance
# without creating circular imports
__all__ = ["app"]
