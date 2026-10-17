"""
Application settings and configuration.

This module defines settings for the application based on environment variables
and sensible defaults.
"""

import logging
import os
import warnings
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple, Union
from urllib.parse import urlparse

from pydantic import (
    EmailStr,
    Field,
    PostgresDsn,
    PrivateAttr,
    RedisDsn,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from backend.app.core.settings_rules import (
    CANONICAL_ENVIRONMENTS,
    ENV_FILES,
    HARDENED_ENVIRONMENTS,
    LEGACY_ENVIRONMENT_ALIASES,
    MIN_SECRET_KEY_LENGTH,
    PUBLIC_BASE_URL_EXAMPLE,
    allowed_host_pattern_error,
    parse_allowed_hosts,
    public_base_url_error,
    secret_is_placeholder,
    superuser_password_is_weak,
    testing_refusal,
)
from backend.app.core.settings_rules import (
    canonical_environment_quiet as _canonical_environment_quiet,
)
from backend.app.core.version import get_version
from backend.app.db.url import postgres_url

logger = logging.getLogger(__name__)

#: The allow-list when neither CORS setting is given, in development and test
#: only. Staging and production allow no other origin unless one is configured
#: (#130): their dashboard is served from the API's own origin and needs none.
DEFAULT_CORS_ORIGINS = (
    "http://localhost:3100",
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:3200",  # ShopLab demo storefront
    "http://localhost:3300",  # StreamPulse demo app
)

_DEFAULT_PORTS = {"http": 80, "https": 443}

#: The dashboard origins a *development* API accepts as an SSO `return_to`
#: without configuration: `npm run dev` (3100) and the dashboard container (3000).
#: Never added in any other environment.
DEVELOPMENT_DASHBOARD_ORIGINS = ("http://localhost:3000", "http://localhost:3100")


def normalise_origin(value: Any) -> Optional[str]:
    """An origin as a browser sends it in ``Origin``, or ``None`` if it is not one.

    ``scheme://host[:port]``: scheme and host lower-cased, the scheme's default
    port dropped, no trailing slash. ``*`` is returned as it is. Refused
    (``None``): anything that is not http(s) with a host, or that carries
    userinfo, a path other than ``/``, parameters, a query or a fragment, or a
    port that does not parse. A browser never sends any of those, so such an
    entry could match no request.
    """
    if not isinstance(value, str):
        value = str(value) if value is not None else ""
    candidate = value.strip()
    if candidate == "*":
        return "*"
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if (
        scheme not in _DEFAULT_PORTS
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
        or any(c.isspace() for c in candidate)
    ):
        return None
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


# Minimum acceptable length for SECRET_KEY in non-test environments (the rule
# itself lives in settings_rules, which the container's start-up check shares).
_MIN_SECRET_KEY_LENGTH = MIN_SECRET_KEY_LENGTH

# ---------------------------------------------------------------------------
# Environment name canonicalisation
# ---------------------------------------------------------------------------
# ``ENVIRONMENT`` is the single source of truth for "which environment am I
# running in".  The canonical values are below; the legacy spellings ``dev``
# and ``prod`` (and the legacy ``APP_ENV`` variable) are still accepted but
# emit a DeprecationWarning so operators migrate their env files.

EnvironmentName = Literal["development", "test", "staging", "production"]

# Legacy spelling -> canonical spelling (CANONICAL_ENVIRONMENTS and this table
# are defined in settings_rules).
_LEGACY_ENVIRONMENT_ALIASES: Dict[str, str] = LEGACY_ENVIRONMENT_ALIASES

# Canonical spelling -> legacy ``APP_ENV`` spelling (mirrored back into the
# process environment for the handful of modules that still read APP_ENV).
_CANONICAL_TO_APP_ENV: Dict[str, str] = {
    "development": "dev",
    "test": "test",
    "staging": "staging",
    "production": "prod",
}

# Environments in which the dev-admin auth bypass may run at all.
BYPASS_ALLOWED_ENVIRONMENTS: tuple = ("development", "test")

# Environments that must not run with placeholder secrets or demo passwords:
# HARDENED_ENVIRONMENTS, from settings_rules.

# The dotenv file each environment's settings class reads.  One table: the
# three ``model_config`` blocks below and ``env_file_for_environment()`` (which
# the modules' settings call, so that a setting that moved off the core class
# is still read from the same file) all take their answer from ENV_FILES, which
# settings_rules defines so that the container's start-up check reads the same
# file.

#: The file an unrecognised environment name falls back to.  Never a hardened
#: environment's file: an unknown name must not pick up production secrets.
DEFAULT_ENV_FILE: str = ENV_FILES["development"]


def _secret_is_placeholder(value: str) -> bool:
    """True when *value* is a committed/well-known placeholder secret."""
    return secret_is_placeholder(value)


def _hardening_required(info: ValidationInfo) -> bool:
    """Secrets must be real in staging/production.

    ``TESTING`` does not relax this: ``Settings`` refuses ``TESTING`` together
    with a staging or production environment before any field is validated
    (``refuse_testing_in_a_hardened_environment``).
    """
    environment = (info.data or {}).get("ENVIRONMENT", "development")
    return environment in HARDENED_ENVIRONMENTS


def canonical_environment(value: Any) -> Any:
    """
    Map a raw environment string to its canonical spelling.

    ``dev`` -> ``development`` and ``prod`` -> ``production`` are accepted with
    a DeprecationWarning.  Unknown values are returned (lower-cased) untouched
    so the ``Literal`` validation on the field produces the error message.
    Non-string values are returned as-is for the same reason.
    """
    if not isinstance(value, str):
        return value
    normalised = value.strip().lower()
    if normalised in _LEGACY_ENVIRONMENT_ALIASES:
        canonical = _LEGACY_ENVIRONMENT_ALIASES[normalised]
        message = f"ENVIRONMENT={value!r} is deprecated; use ENVIRONMENT={canonical!r} instead."
        warnings.warn(message, DeprecationWarning, stacklevel=3)
        logger.warning(message)
        return canonical
    return normalised


def canonical_environment_quiet(value: str) -> str:
    """Like :func:`canonical_environment` but never warns (for comparisons)."""
    return _canonical_environment_quiet(value)


def env_file_for_environment(environment: Any) -> str:
    """The dotenv file the settings class for *environment* reads.

    ``ModulesSettings`` (``modules/backend/app/settings.py``) asks for this so
    that every setting which moved off the core ``Settings`` class is still
    read from the same ``.env.dev`` / ``.env.test`` / ``.env.prod`` the core
    class reads -- the mechanism ``docs/getting-started/environment-setup.md``
    documents.  Legacy ``dev``/``prod`` spellings are accepted (quietly: the
    caller has already had its deprecation warning), and an unrecognised name
    falls back to the development file rather than a hardened one.
    """
    if not isinstance(environment, str):
        return DEFAULT_ENV_FILE
    return ENV_FILES.get(canonical_environment_quiet(environment), DEFAULT_ENV_FILE)


def resolve_environment_from_process_env() -> str:
    """
    Determine the canonical environment name from the process environment.

    ``ENVIRONMENT`` wins.  The legacy ``APP_ENV`` variable is honoured as a
    fallback (with a DeprecationWarning).  When neither is set the platform
    runs as ``development``.
    """
    raw = os.environ.get("ENVIRONMENT")
    legacy = os.environ.get("APP_ENV")
    if raw:
        canonical = canonical_environment(raw)
        if legacy and canonical_environment_quiet(legacy) != canonical:
            logger.warning(
                "ENVIRONMENT=%r and APP_ENV=%r disagree; ENVIRONMENT wins. "
                "Remove APP_ENV from your configuration.",
                raw,
                legacy,
            )
        return canonical
    if legacy:
        message = (
            "APP_ENV is deprecated; set ENVIRONMENT="
            f"{canonical_environment_quiet(legacy)!r} instead."
        )
        warnings.warn(message, DeprecationWarning, stacklevel=2)
        logger.warning(message)
        return canonical_environment(legacy)
    return "development"


def environment_is_set_in_process_env() -> bool:
    """Whether the process environment names the environment at all.

    ``ENVIRONMENT`` or the legacy ``APP_ENV``, non-empty. When neither is set
    :func:`resolve_environment_from_process_env` falls back to development,
    and this is how that fallback is told apart from a deliberate choice.
    """
    return bool(os.environ.get("ENVIRONMENT") or os.environ.get("APP_ENV"))


def _postgres_url(data: Dict[str, Any]) -> str:
    """The database URL from the POSTGRES_* fields, password percent-encoded.

    ``PostgresDsn.build`` does not encode, so a password containing ``#``,
    ``?``, ``%``, ``:`` or a space either broke the URL or refused to validate
    (#146). ``backend/app/db/url.py`` explains the encoding.
    """
    return postgres_url(
        user=data.get("POSTGRES_USER") or "",
        password=data.get("POSTGRES_PASSWORD") or "",
        host=data.get("POSTGRES_SERVER"),
        port=int(data.get("POSTGRES_PORT", 5432)),
        database=data.get("POSTGRES_DB", ""),
    )


class Settings(BaseSettings):
    """Base settings class."""

    PROJECT_NAME: str = "Controlvar"
    # Resolved from the repository-root ``VERSION`` file, which release-please
    # maintains and pyproject.toml and the image labels read too; see
    # backend/app/core/version.py.
    #
    # The alias is the point.  As a plain field this read the bare ``VERSION``
    # environment variable -- about the most generic name there is, exported by
    # build scripts, base images and CI steps everywhere, including six steps
    # of this repository's own release workflow.  Anything that set it silently
    # changed what ``/health``, ``GET /api/v1/modules`` and the OpenAPI
    # ``info.version`` reported, and would have turned
    # ``scripts/check_version_sources.py`` from a gate into a tautology: the
    # release workflow would have been comparing the tag against itself.
    # ``EXPERIMENTLY_VERSION`` is still there for a deployment that genuinely
    # wants to report something else, but now it has to mean it.
    VERSION: str = Field(
        default_factory=get_version, validation_alias="EXPERIMENTLY_VERSION"
    )
    API_V1_STR: str = "/api/v1"

    # The Claude model the platform's own features call (AI experiment
    # design). Not the models a *user* selects for an LLM experiment --
    # those come from the experiment record and go through llm_proxy_service.
    #
    # A setting rather than a literal because the three call sites that used to
    # hard-code `claude-sonnet-4-6` were still on it a generation later, with
    # no way for an operator to move them without editing code. Sonnet 5 is
    # both newer and cheaper than 4.6 ($2/$10 per MTok against $3/$15).
    ANTHROPIC_MODEL: str = "claude-sonnet-5"
    # Canonical environment name. Legacy ``dev``/``prod`` spellings and the
    # legacy ``APP_ENV`` variable are accepted (see module docstring above).
    ENVIRONMENT: EnvironmentName = "development"
    DEBUG: bool = False
    SECRET_KEY: str = "default-secret-key-for-testing"  # Default for testing only
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60  # 1 hour

    # ------------------------------------------------------------------
    # Authentication provider (P0 open-core)
    # ------------------------------------------------------------------
    # ``local``  - email/password against ``users.hashed_password`` (bcrypt)
    #              issuing HS256 JWTs signed with SECRET_KEY.  The default;
    #              no AWS required.
    # ``cognito`` - AWS Cognito user pool (needs COGNITO_USER_POOL_ID and
    #              COGNITO_CLIENT_ID in the process environment).
    AUTH_PROVIDER: Literal["local", "cognito"] = "local"

    # Dev-admin bypass: every request is served as a synthetic ADMIN user
    # without credentials.  Only honoured when ENVIRONMENT is development or
    # test (see ``forbid_dev_auth_bypass_outside_dev`` below and
    # ``backend.app.api.deps.dev_auth_bypass_active``).  Never enable this on
    # a network-reachable deployment.
    DEV_AUTH_BYPASS: bool = False

    # Local-provider token and lockout policy.
    LOCAL_AUTH_TOKEN_TTL_MINUTES: int = 720  # 12 hours
    LOCAL_AUTH_MAX_FAILED_ATTEMPTS: int = 10
    LOCAL_AUTH_LOCKOUT_MINUTES: int = 15

    # The canonical absolute base URL this service is reached at, e.g.
    # `https://api.example.com`. Every absolute URL the app EMITS is built from
    # this rather than from the request, so no handler asks the request for the
    # application's own identity.
    #
    # `_get_redirect_uri` in the SSO endpoints is the one that mattered (#220):
    # it built the OIDC `redirect_uri` -- the address an authorization code is
    # returned to -- from `request.base_url`, which is the `Host` header and a
    # scheme that `X-Forwarded-Proto` can set (#237). Both halves are
    # client-controlled, so a crafted request steered the code elsewhere.
    #
    # Optional, and unset is the development default. It is also the fix for
    # the ordinary case behind a TLS-terminating proxy, where the request's own
    # scheme is `http` and every absolute URL built from it is wrong in a way
    # no test that talks to the app directly would show.
    PUBLIC_BASE_URL: Optional[str] = None

    # The hostnames this application answers to. A request whose `Host` is not
    # one of them is refused with 400 before a handler sees it (#220).
    #
    # USUALLY LEAVE THIS UNSET. When it is empty it derives from
    # PUBLIC_BASE_URL's host -- see `effective_allowed_hosts` -- which is the
    # right default precisely because PUBLIC_BASE_URL is, by definition, the
    # URL users reach the service at. Set it explicitly only when the service
    # legitimately answers on more than one name.
    #
    # Deriving is not a convenience. Two independently-set values that must
    # agree is a defect waiting to happen: the earlier attempt at this fix
    # defaulted the allow-list to the load balancer's own DNS name, which would
    # have refused 100% of user traffic while every health check stayed green.
    # A default taken from the URL we publish cannot have that shape.
    ALLOWED_HOSTS: Annotated[List[str], NoDecode] = []

    # Plain strings, normalised by `cors_allowed_origins`: as `AnyHttpUrl`
    # every entry rendered with a trailing slash that no browser `Origin`
    # carries, so this setting matched nothing (#126), and one malformed entry
    # failed settings validation and stopped the API starting.
    BACKEND_CORS_ORIGINS: Annotated[List[str], NoDecode] = []
    # CORS_ORIGINS is a plain-string list version of BACKEND_CORS_ORIGINS that
    # can also be set via env var as a comma-separated string. NoDecode stops
    # pydantic-settings from JSON-decoding the raw value so the "before"
    # validator below receives it as-is.
    CORS_ORIGINS: Annotated[List[str], NoDecode] = []

    # The dashboard's origin(s), when it is not served from PUBLIC_BASE_URL:
    # where an SSO sign-in started from the dashboard may send the browser
    # back to (`dashboard_origins`). Comma-separated or a JSON array; the
    # first entry is the primary one. REQUIRED when PUBLIC_BASE_URL is the
    # API's own origin rather than the dashboard's (an `api.` host), or every
    # dashboard SSO sign-in is refused with 400. Not the CORS list: that one
    # also names the demo apps and any site running an SDK.
    DASHBOARD_ORIGINS: Annotated[List[str], NoDecode] = []

    # Database settings
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "experimentation"
    POSTGRES_PORT: str = "5432"
    POSTGRES_SCHEMA: Optional[str] = None
    DATABASE_URI: Optional[PostgresDsn] = None
    SQLALCHEMY_DATABASE_URI: Optional[PostgresDsn] = None

    # Redis settings
    REDIS_HOST: str = "localhost"
    REDIS_PORT: str = "6379"
    REDIS_PASSWORD: Optional[str] = None
    REDIS_DB: int = 0
    # Connect to Redis over TLS. The deployed ElastiCache replication group has
    # in-transit encryption on, so it refuses a plaintext connection; the ECS
    # task sets this to true (#147). Off by default, for the local and CI
    # `redis:7` containers, which speak plaintext only. Every Redis client is
    # built by backend/app/core/redis_client.py, which passes REDIS_HOST, _PORT,
    # _PASSWORD, _DB and _SSL; backend/tests/unit/core/test_redis_connection.py
    # fails on a client built anywhere else (#236).
    REDIS_SSL: bool = False
    REDIS_URI: Optional[RedisDsn] = None

    # User settings
    FIRST_SUPERUSER: EmailStr = "admin@example.com"
    FIRST_SUPERUSER_PASSWORD: str = "admin"

    # Scheduler notification and health settings
    NOTIFICATION_WEBHOOK_URL: str = ""
    SCHEDULER_MAX_RETRIES: int = 3
    SCHEDULER_RETRY_DELAY_SECONDS: int = 60
    SCHEDULER_HISTORY_RETENTION_DAYS: int = 30

    # Slack notification settings
    SLACK_BOT_TOKEN: str = ""
    SLACK_DEFAULT_CHANNEL: str = "#platform-alerts"
    SLACK_ENABLED: bool = False

    # Multi-armed bandit weight refresh cadence (Issue #22). The in-app
    # BanditSchedulerRunner recomputes BanditState weights this often.
    BANDIT_UPDATE_INTERVAL_MINUTES: int = 5

    # Background scheduler cadences (minutes). SafetyScheduler checks flag
    # error-rate/latency thresholds and auto-rolls back; RolloutScheduler
    # advances time-based rollout stages. Demos set both to 1.
    SAFETY_CHECK_INTERVAL_MINUTES: int = 5
    ROLLOUT_CHECK_INTERVAL_MINUTES: int = 15
    # ExperimentScheduler starts and completes experiments whose scheduled
    # start/end date has passed. The lifespan passes this to it (#484). A
    # value below 1 is refused: 0 would make the loop spin without sleeping.
    EXPERIMENT_SCHEDULER_INTERVAL_MINUTES: int = Field(default=15, ge=1)
    # The live results stream (websocket_results.py) sends each connected
    # client a fresh snapshot this often, whatever the client sends (#1193).
    # Read when a socket connects. Above zero: 0 would send without pause.
    RESULTS_STREAM_INTERVAL_SECONDS: float = Field(default=30, gt=0)

    # Per-IP ceiling for SDK-facing endpoints (/tracking/*, flag evaluation).
    # Far above the 300/min default because one server-side SDK or NAT egress
    # can legitimately fan out thousands of assignments a minute.
    SDK_RATE_LIMIT_PER_MINUTE: int = 6000

    # Observability (read by backend/app/core/health.py, logger.py, main.py)
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Optional[Literal["json", "console"]] = (
        None  # None -> json in staging/production, console otherwise
    )
    # Days of `analysis_snapshots` and `bandit_state_history` to keep. The
    # request path writes at most one snapshot per experiment, kind and day,
    # but the bandit scheduler records every tick, so the tables still grow
    # without a purge. 0 disables the purge.
    ANALYSIS_HISTORY_RETENTION_DAYS: int = 90

    METRICS_ENABLED: bool = True
    METRICS_TOKEN: Optional[str] = (
        None  # required to read /metrics outside development/test
    )
    REDIS_REQUIRED: bool = False  # when true, /health/ready fails without Redis

    # Process model (documented: one worker per container; scheduler ticks are advisory-locked)
    WEB_CONCURRENCY: int = 1
    PORT: int = 8000

    # AWS region
    AWS_REGION: str = "us-east-1"

    # Compliance audit retention (EP-033).  Read by the core AuditLogService
    # when it stamps an event's retention date; the HMAC signing key that
    # makes the events tamper-evident is the compliance module's and lives in
    # modules/backend/app/settings.py (issue #91).
    AUDIT_RETENTION_DAYS_SOC2: int = 365  # 12 months
    AUDIT_RETENTION_DAYS_ISO27001: int = 730  # 24 months

    # Email / notification settings (EP-030)
    EMAIL_ENABLED: bool = False
    SENDGRID_API_KEY: str = ""
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    EMAIL_FROM_ADDRESS: str = "platform@example.com"
    EMAIL_FROM_NAME: str = "Controlvar"
    NOTIFICATION_ADMIN_EMAILS: List[str] = []

    # EP-046: LLM/AI Model Evaluation. There are no LLM_*_API_KEY settings:
    # each provider reads its own variable (ANTHROPIC_API_KEY, OPENAI_API_KEY,
    # GEMINI_API_KEY) where it calls out, and the LLM_* keys that were here
    # were read by nothing (#256).
    # The LLM-as-judge default. Was `claude-3-5-sonnet-20241022` -- a 2024
    # snapshot -- here and, separately, in the schema and the service, so the
    # three agreed with each other while all three aged. Sonnet 5 is current
    # and cheaper ($2/$10 per MTok); the other two now read this.
    LLM_DEFAULT_JUDGE_MODEL: str = "claude-sonnet-5"
    LLM_MAX_TOKENS_DEFAULT: int = 1000
    LLM_TEMPERATURE_DEFAULT: float = 0.7

    # The modules' settings (the audit signing key, HIPAA, SSO, the warehouse
    # connectors, ETL, real-time counters) are NOT here: they live on
    # ``ModulesSettings`` in modules/backend/app/settings.py and are validated
    # by the modules' registration, so a core deployment never has to supply
    # a module's secret to start (issue #91).

    # Cognito settings
    COGNITO_GROUP_ROLE_MAPPING: Dict[str, str] = {
        "Admins": "admin",
        "Developers": "developer",
        "Analysts": "analyst",
        "Viewers": "viewer",
    }
    COGNITO_ADMIN_GROUPS: List[str] = ["Admins", "SuperUsers"]
    SYNC_ROLES_ON_LOGIN: bool = True
    # Under AUTH_PROVIDER=cognito, POST /api/v1/auth/signup and /confirm
    # answer 404 unless this is true; an administrator creates users in the
    # user pool instead (docs/cognito_integration.md, "Adding a user").
    # Ignored under ``local``, where both routes always answer 404.  Read at
    # request time, so a test can flip it on the singleton.
    COGNITO_SELF_SIGNUP_ENABLED: bool = False

    model_config = SettingsConfigDict(
        case_sensitive=True,
        extra="ignore",  # Ignore unknown fields to catch typos
        # A refused configuration says which setting and why, never the
        # values it was given.
        hide_input_in_errors=True,
    )

    #: Whether ENVIRONMENT was chosen rather than defaulted, when the builder
    #: of this instance knows better than ``model_fields_set`` does (the
    #: module-level ``settings`` always passes the resolved name, so it
    #: records what the process environment said). ``None``: ask
    #: ``model_fields_set``.
    _environment_explicit: Optional[bool] = PrivateAttr(default=None)

    # ------------------------------------------------------------------
    # Environment helpers
    # ------------------------------------------------------------------
    @property
    def environment_explicit(self) -> bool:
        """True when ENVIRONMENT was set, False when it is the class default.

        Set means the ENVIRONMENT variable (or, for the module-level
        ``settings``, the legacy APP_ENV), a dotenv entry, or a keyword to the
        constructor. The API refuses to serve when this is False
        (``backend.app.main.lifespan``), and the bootstrap creates a first
        administrator with a weak password only when this is True and the
        environment is development or test.
        """
        if self._environment_explicit is not None:
            return self._environment_explicit
        return "ENVIRONMENT" in self.model_fields_set

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def is_staging(self) -> bool:
        return self.ENVIRONMENT == "staging"

    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT == "development"

    @property
    def is_test(self) -> bool:
        return self.ENVIRONMENT == "test"

    @property
    def cors_allowed_origins(self) -> List[str]:
        """The origins CORSMiddleware allows, normalised.

        `BACKEND_CORS_ORIGINS` if it has a usable entry, else `CORS_ORIGINS`,
        else -- in development and test only -- `DEFAULT_CORS_ORIGINS`; a
        staging or production API with neither set allows no other origin
        (#130). Every configured `DASHBOARD_ORIGINS` entry is then added, so a
        dashboard on its own host is named once. An entry that is not an
        origin (see `normalise_origin`) is dropped with an ERROR log, never an
        exception: a bad entry matched no request before, and must not stop
        the API starting now.
        """
        origins = self._configured_cors_origins()
        if origins is None:
            if self.ENVIRONMENT in HARDENED_ENVIRONMENTS:
                origins = []
            else:
                origins = list(DEFAULT_CORS_ORIGINS)
        for origin in self._configured_dashboard_origins():
            if origin not in origins:
                origins.append(origin)
        return origins

    def _configured_cors_origins(self) -> Optional[List[str]]:
        """The first of `BACKEND_CORS_ORIGINS`/`CORS_ORIGINS` with a usable
        entry, normalised; None when neither has one."""
        for name in ("BACKEND_CORS_ORIGINS", "CORS_ORIGINS"):
            kept: List[str] = []
            for entry in getattr(self, name) or []:
                if entry is None or not str(entry).strip():
                    continue
                origin = normalise_origin(entry)
                if origin is None:
                    logger.error(
                        "%s entry %r is not an origin (scheme://host[:port]); ignoring it",
                        name,
                        str(entry)[:200],
                    )
                elif origin not in kept:
                    kept.append(origin)
            if kept:
                return kept
        return None

    def _configured_dashboard_origins(self) -> List[str]:
        """`DASHBOARD_ORIGINS`, normalised; an entry that is not an origin is
        dropped with an ERROR log, and so is `*` -- a wildcard here would let
        a sign-in hand its result to any site."""
        kept: List[str] = []
        for entry in self.DASHBOARD_ORIGINS or []:
            if entry is None or not str(entry).strip():
                continue
            origin = normalise_origin(entry)
            if origin is None or origin == "*":
                logger.error(
                    "DASHBOARD_ORIGINS entry %r is not an origin (scheme://host[:port]); ignoring it",
                    str(entry)[:200],
                )
            elif origin not in kept:
                kept.append(origin)
        return kept

    @property
    def dashboard_origins(self) -> List[str]:
        """The origins an SSO sign-in may return the browser to, primary first.

        `DASHBOARD_ORIGINS`, then `PUBLIC_BASE_URL`'s origin, then -- in
        development only -- `DEVELOPMENT_DASHBOARD_ORIGINS`; each normalised by
        `normalise_origin`, no duplicates. Deliberately not the CORS list.
        """
        origins = self._configured_dashboard_origins()
        if self.PUBLIC_BASE_URL:
            origin = normalise_origin(self.PUBLIC_BASE_URL)
            if origin and origin != "*" and origin not in origins:
                origins.append(origin)
        if self.is_development:
            for origin in DEVELOPMENT_DASHBOARD_ORIGINS:
                if origin not in origins:
                    origins.append(origin)
        return origins

    @property
    def primary_dashboard_origin(self) -> Optional[str]:
        """Where a sign-in that cannot say where it came from is sent back to.

        The first `DASHBOARD_ORIGINS` entry if there is one, else
        `PUBLIC_BASE_URL`'s origin, else `None` (a development API with
        neither set; the development defaults are never primary).
        """
        configured = self._configured_dashboard_origins()
        if configured:
            return configured[0]
        if self.PUBLIC_BASE_URL:
            origin = normalise_origin(self.PUBLIC_BASE_URL)
            if origin and origin != "*":
                return origin
        return None

    @property
    def effective_allowed_hosts(self) -> List[str]:
        """The hostnames the app answers to, explicit or derived.

        ALLOWED_HOSTS when it is set; otherwise PUBLIC_BASE_URL's host, which
        is the URL users actually reach the service at and therefore a host we
        answer to by definition. Empty means the check is off, which is the
        development default and which a hardened environment cannot reach --
        `require_a_host_allow_list_when_hardened` below refuses to construct.

        A property rather than a mutated field so that `settings.ALLOWED_HOSTS`
        keeps meaning "what the operator set", which is what an operator
        debugging this will look at first.
        """
        if self.ALLOWED_HOSTS:
            return list(self.ALLOWED_HOSTS)
        if self.PUBLIC_BASE_URL:
            host = urlparse(self.PUBLIC_BASE_URL).hostname
            if host:
                return [host]
        return []

    @property
    def dev_auth_bypass_active(self) -> bool:
        """True only when the dev-admin bypass is both enabled and permitted."""
        return (
            self.DEV_AUTH_BYPASS is True
            and self.ENVIRONMENT in BYPASS_ALLOWED_ENVIRONMENTS
        )

    @property
    def dev_fallbacks_allowed(self) -> bool:
        """True where a degraded development-only fallback may run at all.

        The companion of :attr:`dev_auth_bypass_active` for the fallbacks that
        have no switch to enable -- code that stands in for something a real
        deployment must have:

        * the SAML stub parser that accepts an assertion without checking its
          signature when ``python3-saml`` is not installed
          (``modules/backend/app/services/sso_service.py``);
        * continuing on the core profile after the modules package was found
          but failed to register (``backend/app/modules_loader.py``).

        Both are conveniences for a developer and security failures anywhere
        else, so they share the dev-admin bypass's allow-list: they run in
        ``development`` and ``test`` and nowhere else.  Call sites ask this
        rather than comparing ``ENVIRONMENT`` themselves, so that staging is
        hardened with production and an unrecognised environment name fails
        closed instead of matching no ``== "production"`` test.
        """
        return self.ENVIRONMENT in BYPASS_ALLOWED_ENVIRONMENTS

    @field_validator("ENVIRONMENT", mode="before")
    @classmethod
    def canonicalise_environment(cls, v: Any) -> Any:
        """Accept legacy ``dev``/``prod`` spellings (with a deprecation warning)."""
        return canonical_environment(v)

    @model_validator(mode="before")
    @classmethod
    def refuse_testing_in_a_hardened_environment(cls, data: Any) -> Any:
        """``TESTING`` belongs to the test runner, never to staging or production.

        The test suite sets ``TESTING=true`` (with ``APP_ENV=test``).  In a
        staging or production process it is always a configuration mistake,
        so it is refused outright rather than allowed to change what those
        environments require.  A ``before`` validator so that this is the
        message, not whichever setting it would otherwise have affected.
        """
        environment: Any = None
        if isinstance(data, dict):
            environment = data.get("ENVIRONMENT")
        if environment is None:
            environment = cls.model_fields["ENVIRONMENT"].default
        if isinstance(environment, str):
            environment = canonical_environment_quiet(environment)
        # The rule and its message are settings_rules', which the container's
        # start-up check applies too.
        refusal = testing_refusal(environment, os.environ.get("TESTING"))
        if refusal:
            raise ValueError(refusal)
        return data

    @model_validator(mode="after")
    def forbid_dev_auth_bypass_outside_dev(self) -> "Settings":
        """
        Refuse to start with DEV_AUTH_BYPASS enabled in staging/production.

        The bypass serves every request as an ADMIN without credentials; the
        only environments where that is acceptable are ``development`` and
        ``test``.  This check is deliberately NOT relaxed by ``TESTING``.
        """
        if self.DEV_AUTH_BYPASS and self.ENVIRONMENT not in BYPASS_ALLOWED_ENVIRONMENTS:
            raise ValueError(
                f"DEV_AUTH_BYPASS=true is not permitted when ENVIRONMENT={self.ENVIRONMENT!r}. "
                "The dev-admin bypass may only be enabled in development or test."
            )
        return self

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> List[str]:
        """JSON array or comma-separated; entries are checked by `cors_allowed_origins`."""
        if isinstance(v, str) and v.strip().startswith("["):
            import json

            try:
                v = json.loads(v)
            except ValueError:
                logger.error("BACKEND_CORS_ORIGINS is not valid JSON; ignoring it")
                return []
        if isinstance(v, str):
            return [i.strip() for i in v.split(",") if i.strip()]
        if isinstance(v, list):
            return [str(i) for i in v]
        return []

    @field_validator("PUBLIC_BASE_URL")
    @classmethod
    def validate_public_base_url(cls, v: Optional[str]) -> Optional[str]:
        """A scheme and a host, no path, no trailing slash.

        Rejecting a path matters: this is concatenated with
        `/api/v1/auth/sso/oidc/{provider}/callback`, and an OIDC `redirect_uri`
        must match the IdP's registration EXACTLY -- so a stray path or slash
        is not cosmetic, it is a callback the IdP refuses.
        """
        if v is None or not v.strip():
            return None
        error = public_base_url_error(v)
        if error:
            raise ValueError(error)
        return v.strip().rstrip("/")

    @field_validator("ALLOWED_HOSTS", mode="before")
    @classmethod
    def assemble_allowed_hosts(cls, v: Union[str, List[str]]) -> List[str]:
        """Comma-separated or JSON, the two spellings CORS_ORIGINS also takes."""
        return parse_allowed_hosts(v)

    @field_validator("ALLOWED_HOSTS")
    @classmethod
    def validate_allowed_host_patterns(cls, v: List[str]) -> List[str]:
        """Refuse patterns that parse but can never match.

        This is the silent-outage class and it has already been shipped once:
        a wildcard written `*example.com` (no dot) is not a wildcard. It is a
        literal hostname containing an asterisk, it matches nothing, and a
        deployment configured with it refuses every request while every health
        check stays green -- because the probes are exempt. An outage that
        monitoring calls fine is the most expensive kind, so the typo is
        refused here rather than at 3am.

        Accepted: `example.com`, `*.example.com`. Nothing else. `*` passes
        here and is refused in staging/production below. The rule is
        settings_rules.allowed_host_pattern_error, which the container's
        start-up check applies too.
        """
        for pattern in v:
            error = allowed_host_pattern_error(pattern)
            if error:
                raise ValueError(error)
        return v

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins_plain(cls, v: Union[str, List[str]]) -> List[str]:
        """Parse plain CORS origins list from comma-separated string or list."""
        if isinstance(v, str) and v.strip().startswith("["):
            import json

            v = json.loads(v)
        if isinstance(v, str) and v:
            return [i.strip() for i in v.split(",") if i.strip()]
        if isinstance(v, list):
            return v
        return []

    @field_validator("DASHBOARD_ORIGINS", mode="before")
    @classmethod
    def assemble_dashboard_origins(cls, v: Union[str, List[str], None]) -> List[str]:
        """Comma-separated or a JSON array; entries are checked by `dashboard_origins`.

        Never raises: a malformed value logs an ERROR and is ignored, so it
        cannot stop the API starting.
        """
        if isinstance(v, str) and v.strip().startswith("["):
            import json

            try:
                v = json.loads(v)
            except ValueError:
                logger.error("DASHBOARD_ORIGINS is not valid JSON; ignoring it")
                return []
        if isinstance(v, str):
            return [i.strip() for i in v.split(",") if i.strip()]
        if isinstance(v, list):
            return [str(i) for i in v]
        return []

    @model_validator(mode="after")
    def warn_when_the_dashboard_origin_is_unknown(self) -> "Settings":
        """WARNING at startup for the `api.` hedge with no DASHBOARD_ORIGINS.

        A staging or production PUBLIC_BASE_URL on an `api.` host is the API's
        own origin, not the dashboard's, and with DASHBOARD_ORIGINS empty no
        dashboard origin is known: every SSO sign-in from the dashboard is
        refused with 400. A warning, not a refusal to start -- password
        sign-in still works, and the deployment may not use SSO at all.
        """
        message = dashboard_origins_warning(self)
        if message:
            logger.warning(message)
        return self

    @model_validator(mode="after")
    def require_a_host_allow_list_when_hardened(self) -> "Settings":
        """Staging and production must know what hostname they answer on.

        Fail-closed, and there is exactly ONE thing to set to satisfy it:
        PUBLIC_BASE_URL, which such a deployment needs anyway or the OIDC
        callback cannot be registered. ALLOWED_HOSTS is for the rarer case of
        several names.

        Harsher than a warning on purpose. An unset value in production is
        indistinguishable at runtime from one considered and deliberately
        opened, and the first is overwhelmingly more likely -- so this says so
        at deploy time rather than leaving a silent hole. Both task definitions
        carry PUBLIC_BASE_URL for this reason, which
        `backend/tests/unit/infrastructure/test_deployment_secrets.py` boots
        the real settings against.
        """
        environment = getattr(self, "ENVIRONMENT", "development")
        if environment not in HARDENED_ENVIRONMENTS:
            return self
        if "*" in self.ALLOWED_HOSTS:
            raise ValueError(
                "ALLOWED_HOSTS=* defeats the check it configures and is refused "
                "in staging/production. Name the hostnames instead, or set "
                "PUBLIC_BASE_URL and let them be derived."
            )
        if not self.effective_allowed_hosts:
            raise ValueError(
                "PUBLIC_BASE_URL is not set, so this deployment does not know "
                "what hostname it answers on, and the Host header is "
                "attacker-controlled. Set it to the URL people type to open the "
                f"dashboard, e.g. {PUBLIC_BASE_URL_EXAMPLE} (the API is served "
                "under /api on the same origin) -- or set ALLOWED_HOSTS "
                "explicitly if the service answers on several names."
            )
        return self

    @field_validator("SECRET_KEY")
    @classmethod
    def validate_secret_key(cls, v: str, info: ValidationInfo) -> str:
        """
        Ensure the SECRET_KEY is sufficiently long in non-test environments.

        A short or default key in production is a critical security vulnerability.
        Tests are explicitly excluded so the test suite can run without real keys.
        """
        if _hardening_required(info):
            if _secret_is_placeholder(v) or len(v) < _MIN_SECRET_KEY_LENGTH:
                raise ValueError(
                    f"SECRET_KEY must be at least {_MIN_SECRET_KEY_LENGTH} characters "
                    "and must not be a placeholder from the repository (docker-compose.yml, "
                    ".env.example or the class default) in staging/production. "
                    'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
                )
        return v

    @field_validator("FIRST_SUPERUSER_PASSWORD")
    @classmethod
    def validate_superuser_password(cls, v: str, info: ValidationInfo) -> str:
        """Reject weak or repository-known superuser passwords in staging/production."""
        if _hardening_required(info):
            # ``Demo1234!`` is the seeded demo password and appears in the
            # public docs and docker-compose.yml; it is never acceptable
            # for a real deployment's first administrator
            # (settings_rules.WEAK_SUPERUSER_PASSWORDS).
            if superuser_password_is_weak(v):
                raise ValueError(
                    "FIRST_SUPERUSER_PASSWORD must be at least 8 characters "
                    "and must not be a well-known or demo default in staging/production."
                )
        return v

    @field_validator("DATABASE_URI", mode="before")
    @classmethod
    def assemble_database_connection(
        cls, v: Optional[str], info: ValidationInfo
    ) -> Any:
        """Assemble database connection string if not provided directly."""
        if isinstance(v, str):
            # Supplied whole, so used as given: it must already be encoded.
            return v
        return _postgres_url(info.data)

    @field_validator("SQLALCHEMY_DATABASE_URI", mode="before")
    @classmethod
    def assemble_db_connection(cls, v: Optional[str], info: ValidationInfo) -> Any:
        """Assemble database connection string if not provided directly."""
        if isinstance(v, str):
            return v
        # If DATABASE_URI is set, use that
        database_uri = info.data.get("DATABASE_URI")
        if database_uri:
            return database_uri
        # Otherwise build from components
        return _postgres_url(info.data)

    @field_validator("REDIS_URI", mode="before")
    @classmethod
    def assemble_redis_connection(cls, v: Optional[str], info: ValidationInfo) -> Any:
        """Assemble Redis connection string if not provided directly."""
        if isinstance(v, str):
            return v

        # Build Redis URI components
        host = info.data.get("REDIS_HOST", "localhost")
        port = int(info.data.get("REDIS_PORT", 6379))
        password = info.data.get("REDIS_PASSWORD")

        # Construct the Redis URI
        if password:
            return f"redis://:{password}@{host}:{port}"
        return f"redis://{host}:{port}"


def dashboard_origins_warning(config: "Settings") -> Optional[str]:
    """The startup warning `Settings` logs for an unknown dashboard origin, or None.

    Only in staging/production, only with DASHBOARD_ORIGINS empty, and only
    when PUBLIC_BASE_URL's host starts with `api.`.
    """
    if config.ENVIRONMENT not in HARDENED_ENVIRONMENTS:
        return None
    if config._configured_dashboard_origins() or not config.PUBLIC_BASE_URL:
        return None
    host = (urlparse(config.PUBLIC_BASE_URL).hostname or "").lower()
    if not host.startswith("api."):
        return None
    return (
        f"PUBLIC_BASE_URL={config.PUBLIC_BASE_URL} looks like the API's own origin "
        "and DASHBOARD_ORIGINS is empty, so no dashboard origin is known: SSO "
        "sign-in from the dashboard will be refused (400), and a browser refuses "
        "the dashboard's API calls unless its origin is in CORS_ORIGINS. Set "
        "DASHBOARD_ORIGINS to the dashboard's origin, e.g. https://app.example.com "
        "(it is added to the CORS allow-list too), or at least add that origin "
        "to CORS_ORIGINS."
    )


def cors_start_up_messages(config: "Settings") -> List[Tuple[int, str]]:
    """The `(level, message)` lines main.py logs once at start-up about CORS.

    Staging and production only: one INFO line naming the effective allow-list
    (or saying there is none), and a WARNING when it contains `*`. Nothing is
    refused: credentials are never allowed cross-origin (main.py), so a `*`
    entry is reported rather than stopping an upgraded deployment starting.
    """
    if config.ENVIRONMENT not in HARDENED_ENVIRONMENTS:
        return []
    origins = config.cors_allowed_origins
    listed = ", ".join(origins) if origins else "none: same-origin only"
    messages: List[Tuple[int, str]] = [
        (
            logging.INFO,
            f"CORS allow-list ({config.ENVIRONMENT}): {listed}. To let a site on "
            "another origin call this API from a browser, add its origin to "
            "CORS_ORIGINS.",
        )
    ]
    if "*" in origins:
        messages.append(
            (
                logging.WARNING,
                "The CORS allow-list contains '*', so every website can call this "
                "API from a browser (without credentials, which are never allowed "
                "cross-origin). List the sites that need access in CORS_ORIGINS "
                "instead.",
            )
        )
    return messages


class DevSettings(Settings):
    """Development environment settings."""

    ENVIRONMENT: EnvironmentName = "development"
    LOG_LEVEL: str = (
        "INFO"  # set LOG_LEVEL=DEBUG explicitly; DEBUG makes every library chatty
    )
    CORS_ORIGINS: Annotated[List[str], NoDecode] = [
        "http://localhost:3100",
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3200",  # ShopLab demo storefront
        "http://localhost:3300",  # StreamPulse demo app
        "http://localhost:8000",
    ]
    CACHE_ENABLED: bool = False
    CACHE_CONTROL: Dict[str, Any] = {"enabled": False, "redis": None, "ttl": 3600}
    PROJECT_NAME: str = "Controlvar (Development)"
    PROJECT_DESCRIPTION: str = "A platform for managing experiments and feature flags (Development Environment)"
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "experimentation"

    model_config = SettingsConfigDict(
        env_file=ENV_FILES["development"],
        case_sensitive=True,
        extra="ignore",
        hide_input_in_errors=True,
    )


class TestSettings(Settings):
    """Test environment settings."""

    ENVIRONMENT: EnvironmentName = "test"
    ENV: str = "test"
    TESTING: bool = True
    LOG_LEVEL: str = (
        "INFO"  # set LOG_LEVEL=DEBUG explicitly; DEBUG makes every library chatty
    )
    PROJECT_NAME: str = "Controlvar"
    PROJECT_DESCRIPTION: str = (
        "API for managing experiments and feature flags in test environment"
    )
    DEBUG: bool = True
    POSTGRES_SERVER: str = "localhost"  # Use localhost for testing
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "experimentation_test"
    POSTGRES_SCHEMA: str = "test_experimentation"
    CACHE_ENABLED: bool = False
    CACHE_CONTROL: Dict[str, Any] = {"enabled": False, "redis": None, "ttl": 3600}

    model_config = SettingsConfigDict(
        env_file=ENV_FILES["test"],
        case_sensitive=True,
        extra="ignore",
        hide_input_in_errors=True,
    )


class ProdSettings(Settings):
    """Production/staging environment settings."""

    ENVIRONMENT: EnvironmentName = "production"
    PROJECT_NAME: str = "Controlvar"
    PROJECT_DESCRIPTION: str = "A platform for managing experiments and feature flags"
    # Off by default, as in development and test. It affects the experiment
    # and admin routes only: the feature-flag routes always read the database
    # (#630). CACHE_CONTROL is read by no route.
    CACHE_ENABLED: bool = False
    CACHE_CONTROL: Dict[str, Any] = {"enabled": False, "redis": None, "ttl": 3600}

    model_config = SettingsConfigDict(
        env_file=ENV_FILES["production"],
        case_sensitive=True,
        extra="ignore",
        hide_input_in_errors=True,
    )


# ---------------------------------------------------------------------------
# Select the settings class from the canonical environment name
# ---------------------------------------------------------------------------
# ``ENVIRONMENT`` (canonical) wins; legacy ``APP_ENV`` is honoured with a
# deprecation warning.  The resolved value is passed explicitly so the chosen
# class always reports the canonical name (``APP_ENV=prod`` -> ``production``).
_resolved_environment = resolve_environment_from_process_env()
# Read before the APP_ENV mirror below writes to the process environment, after
# which it would always look set.
_environment_explicit = environment_is_set_in_process_env()

# Mirror the legacy spelling back into APP_ENV for modules that still read it
# directly (schema selection, rate-limiter enablement, bandit scheduler ...).
os.environ.setdefault(
    "APP_ENV", _CANONICAL_TO_APP_ENV.get(_resolved_environment, _resolved_environment)
)

if _resolved_environment in ("production", "staging"):
    settings = ProdSettings(ENVIRONMENT=_resolved_environment)
elif _resolved_environment == "test":
    settings = TestSettings(ENVIRONMENT=_resolved_environment)
else:
    settings = DevSettings(ENVIRONMENT=_resolved_environment)
# The resolved name is always passed, so that the class reports the canonical
# spelling; whether it was chosen or defaulted is what the process said.
settings._environment_explicit = _environment_explicit

# Make settings accessible at module level
__all__ = [
    "BYPASS_ALLOWED_ENVIRONMENTS",
    "CANONICAL_ENVIRONMENTS",
    "DEFAULT_ENV_FILE",
    "ENV_FILES",
    "DevSettings",
    "EnvironmentName",
    "ProdSettings",
    "Settings",
    "TestSettings",
    "canonical_environment",
    "env_file_for_environment",
    "environment_is_set_in_process_env",
    "resolve_environment_from_process_env",
    "settings",
]
