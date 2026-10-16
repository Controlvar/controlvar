"""The container's start-up check: every missing or unusable setting, at once.

``backend/docker-entrypoint.sh`` runs ``python -m backend.app.core.preflight``
before anything else -- before it waits for the database, before the
bootstrap, before the server.  It exits 0 when the environment can start, and
otherwise prints one list naming every problem and exits 78 (``EX_CONFIG``).

Without it a staging or production start refused its settings one at a time:
the settings class stops at the first model it cannot build, the modules'
settings are only built after the core ones, and a database URL the image does
not read cost a 120-second wait on ``localhost`` before anything was said.

What it refuses:

* always -- neither ``ENVIRONMENT`` nor the legacy ``APP_ENV`` set, an
  environment name the settings do not know, or ``test``, which is the test
  runner's environment and not one the image runs in.  The image requires
  ENVIRONMENT to be set.  Also an ``ALLOWED_HOSTS`` entry that can never match
  (``*example.com``), which the settings refuse in every environment.
* in staging and production -- ``TESTING`` set (the test runner's flag; one
  line, and nothing else is listed, as the settings refuse it outright);
  ``SECRET_KEY``, ``FIRST_SUPERUSER_PASSWORD``
  and (full profile) ``AUDIT_HMAC_KEY`` missing, too short or a published
  placeholder; (full profile) ``WAREHOUSE_CREDENTIALS_KEYS`` set but not a
  list of Fernet keys, or holding a placeholder (it may be absent);
  ``PUBLIC_BASE_URL`` missing (unless ``ALLOWED_HOSTS`` names the hosts) or
  malformed; ``ALLOWED_HOSTS=*``; ``POSTGRES_SERVER`` (or
  ``POSTGRES_HOST``) not set; ``DATABASE_URL`` or ``DATABASE_URI`` set.

The secret rules are ``settings_rules``', the ones ``config.py`` and the
modules' settings apply, so this refuses what they would refuse -- all of it in
one pass -- plus the database-location mistakes they cannot see.  It never
prints a secret's value.

Standard library plus python-dotenv (a pydantic-settings dependency) only, and
it never imports ``backend.app.core.config``: that module builds the settings
when imported, and raises on the first bad one.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

from backend.app.core.settings_rules import (
    CANONICAL_ENVIRONMENTS,
    CREDENTIAL_KEYS_SETTING,
    ENV_FILES,
    HARDENED_ENVIRONMENTS,
    MIN_SECRET_KEY_LENGTH,
    MIN_SUPERUSER_PASSWORD_LENGTH,
    PUBLIC_BASE_URL_EXAMPLE,
    allowed_host_pattern_error,
    canonical_environment_quiet,
    credential_keys_error,
    parse_allowed_hosts,
    public_base_url_error,
    secret_is_weak,
    superuser_password_is_weak,
    testing_refusal,
)

#: ``EX_CONFIG`` from sysexits.h: "something was found in an unconfigured or
#: misconfigured state".
EX_CONFIG = 78

PREFIX = "[preflight]"

GENERATE_SECRET = "Generate one: openssl rand -hex 32"
GENERATE_PASSWORD = "Generate one: openssl rand -base64 18"

#: What to set instead, shared by the one-line environment refusals.
_ENVIRONMENT_ADVICE = (
    "set ENVIRONMENT=production (or staging; development only for a local trial)."
)

#: The one line printed when no environment is named at all.
ENVIRONMENT_UNSET = (
    f"ENVIRONMENT is not set. This image requires it: {_ENVIRONMENT_ADVICE}"
)

#: The environments this image runs in: every canonical one but ``test``,
#: which belongs to the test runner (pytest, from source).
IMAGE_ENVIRONMENTS = tuple(e for e in CANONICAL_ENVIRONMENTS if e != "test")


def environment_is_test(name: str) -> str:
    """The one line printed when *name* (ENVIRONMENT or APP_ENV) says ``test``."""
    return f"{name}=test is for the test runner, not this image: {_ENVIRONMENT_ADVICE}"


#: Refusals printed as a bare line rather than as a one-item list.
_ONE_LINE_REFUSALS = (
    ENVIRONMENT_UNSET,
    environment_is_test("ENVIRONMENT"),
    environment_is_test("APP_ENV"),
    *(testing_refusal(environment, "true") for environment in HARDENED_ENVIRONMENTS),
)

_POSTGRES_SETTINGS = (
    "POSTGRES_SERVER, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD and POSTGRES_DB"
)


def _modules_present() -> bool:
    """The full profile: the ``modules`` package the loader registers is here.

    ``find_spec``, not an import -- importing the package runs its code, and
    the question is only whether this image carries it (the same test the
    image checks in CI).
    """
    try:
        return importlib.util.find_spec("modules") is not None
    except (ImportError, ValueError):
        return False


def _dotenv(environment: str, root: Path) -> Dict[str, str]:
    """The dotenv file the settings class for *environment* reads, if present.

    The settings read ``.env.prod`` (staging, production) relative to the
    working directory, so a value supplied there is a value supplied; the
    process environment wins over it, as it does for pydantic-settings.
    """
    path = root / ENV_FILES.get(environment, ENV_FILES["development"])
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover - a pydantic-settings dependency
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def _truncate(value: str, limit: int = 200) -> str:
    return value if len(value) <= limit else value[:limit] + "..."


def check(
    environ: Mapping[str, str],
    *,
    full_profile: Optional[bool] = None,
    root: Optional[Path] = None,
) -> List[str]:
    """Every problem with *environ*, one message each; empty when it can start.

    *full_profile* defaults to whether the modules package is importable, and
    *root* (where the dotenv file is looked for) to the working directory.
    """
    # Truthiness of the raw values, as resolve_environment_from_process_env()
    # tests them: a blank-but-set ENVIRONMENT is a bad name, not an absent one.
    raw_environment = environ.get("ENVIRONMENT") or ""
    legacy = environ.get("APP_ENV") or ""
    if not raw_environment and not legacy:
        return [ENVIRONMENT_UNSET]
    name = "ENVIRONMENT" if raw_environment else "APP_ENV"
    environment = canonical_environment_quiet(raw_environment or legacy)
    if environment == "test":
        return [environment_is_test(name)]
    if environment not in IMAGE_ENVIRONMENTS:
        return [
            f"{name}={_truncate(raw_environment or legacy)!r} is not an environment "
            f"this image knows. Use one of: {', '.join(IMAGE_ENVIRONMENTS)}."
        ]
    # TESTING with staging/production: the settings refuse it outright
    # (settings_rules.testing_refusal, the process environment only, as the
    # settings read it), so nothing else is worth listing.
    refusal = testing_refusal(environment, environ.get("TESTING"))
    if refusal:
        return [refusal]
    from_file = _dotenv(environment, root if root is not None else Path.cwd())

    def value(key: str) -> str:
        if key in environ:
            return environ[key]
        return from_file.get(key, "")

    problems: List[str] = []

    # ALLOWED_HOSTS entries the settings refuse in EVERY environment: a
    # pattern that parses but can never match (`*example.com`).
    raw_hosts = value("ALLOWED_HOSTS")
    try:
        hosts = parse_allowed_hosts(raw_hosts)
    except ValueError:
        hosts = []
        problems.append(
            "ALLOWED_HOSTS starts with '[' but is not a JSON array. Write it "
            'comma-separated (a.example.com,b.example.com) or as ["a.example.com"].'
        )
    for pattern in hosts:
        error = allowed_host_pattern_error(pattern)
        if error:
            problems.append(error)

    if environment not in HARDENED_ENVIRONMENTS:
        return problems

    if full_profile is None:
        full_profile = _modules_present()

    def secret(
        key: str, missing: str, weak: Callable[[str], bool], rule: str, how: str
    ) -> None:
        current = value(key)
        if not current.strip():
            problems.append(f"{key} is not set. {missing} {how}")
        elif weak(current):
            problems.append(f"{key} is {rule}. {how}")

    secret(
        "SECRET_KEY",
        "It signs every session token.",
        secret_is_weak,
        f"shorter than {MIN_SECRET_KEY_LENGTH} characters or a published placeholder",
        GENERATE_SECRET,
    )
    secret(
        "FIRST_SUPERUSER_PASSWORD",
        "It is the password of the first administrator, created on the first start.",
        superuser_password_is_weak,
        f"shorter than {MIN_SUPERUSER_PASSWORD_LENGTH} characters or a well-known default",
        GENERATE_PASSWORD,
    )

    public_base_url = value("PUBLIC_BASE_URL").strip()
    if public_base_url:
        error = public_base_url_error(public_base_url)
        if error:
            problems.append(_truncate(error, 400))
    elif not hosts or hosts == ["*"]:
        problems.append(
            "PUBLIC_BASE_URL is not set. Set it to the URL people type to open the "
            f"dashboard, e.g. {PUBLIC_BASE_URL_EXAMPLE} (the API is served under "
            "/api on the same origin)."
        )
    if "*" in hosts:
        problems.append(
            "ALLOWED_HOSTS=* is refused in staging and production. Name the "
            "hostnames, or remove ALLOWED_HOSTS and let PUBLIC_BASE_URL decide."
        )

    if full_profile:
        secret(
            "AUDIT_HMAC_KEY",
            "The full profile requires it: it signs the compliance audit log.",
            secret_is_weak,
            f"shorter than {MIN_SECRET_KEY_LENGTH} characters or a published placeholder",
            GENERATE_SECRET,
        )
        # Optional, but when it is set it must be usable: the modules'
        # settings refuse the same values (settings_rules).
        keys_error = credential_keys_error(value(CREDENTIAL_KEYS_SETTING))
        if keys_error:
            problems.append(keys_error)

    # The entrypoint's database wait reads the process environment only, and
    # falls back from POSTGRES_SERVER to POSTGRES_HOST to localhost.
    if not (
        environ.get("POSTGRES_SERVER") or environ.get("POSTGRES_HOST") or ""
    ).strip():
        problems.append(
            "POSTGRES_SERVER is not set, so the API would connect to localhost:5432 "
            "inside its own container. Set it to your database host."
        )
    for key in ("DATABASE_URL", "DATABASE_URI"):
        if value(key).strip():
            problems.append(
                f"{key} is set but this image does not connect with it: the database "
                f"wait and the migrations read {_POSTGRES_SETTINGS}. Set those "
                f"instead, and remove {key}."
            )
    return problems


def render(problems: List[str]) -> str:
    """The text printed for *problems* (non-empty)."""
    if len(problems) == 1 and problems[0] in _ONE_LINE_REFUSALS:
        return f"{PREFIX} {problems[0]}"
    noun = "setting needs" if len(problems) == 1 else "settings need"
    lines = [f"{PREFIX} Controlvar cannot start: {len(problems)} {noun} attention."]
    lines.extend(f"  - {problem}" for problem in problems)
    return "\n".join(lines)


def main(environ: Optional[Mapping[str, str]] = None) -> int:
    problems = check(os.environ if environ is None else environ)
    if not problems:
        return 0
    sys.stderr.write(render(problems) + "\n")
    sys.stderr.flush()
    return EX_CONFIG


if __name__ == "__main__":
    sys.exit(main())
