"""The rules the settings enforce, with no settings object behind them.

``backend.app.core.config`` builds its settings when it is imported, so
importing it in a deployment with a bad secret raises before the importer can
do anything else.  The container's start-up check
(``backend.app.core.preflight``) has to apply the *same* rules to an
environment that may be that deployment, so the rules live here, in a module
whose import does nothing but define them.  ``config.py`` and
``modules/backend/app/settings.py`` use these definitions, so the start-up
check and the settings cannot disagree about what a placeholder is.

Standard library only: this is imported by a process that has not yet
decided whether the environment is fit to start.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

#: Minimum length of SECRET_KEY and AUDIT_HMAC_KEY in staging/production.
MIN_SECRET_KEY_LENGTH = 32

#: Minimum length of FIRST_SUPERUSER_PASSWORD in staging/production.
MIN_SUPERUSER_PASSWORD_LENGTH = 8

#: Environments that must not run with placeholder secrets or demo passwords.
HARDENED_ENVIRONMENTS: tuple = ("staging", "production")

#: The canonical environment names.
CANONICAL_ENVIRONMENTS: tuple = ("development", "test", "staging", "production")

#: Environments in which a weak first-administrator password is accepted, and
#: then only when ENVIRONMENT was set rather than defaulted.
WEAK_SUPERUSER_PASSWORD_ENVIRONMENTS: tuple = ("development", "test")

#: What the API says, and then stops, when ENVIRONMENT was never set.
ENVIRONMENT_NOT_SET_MESSAGE = (
    "ENVIRONMENT is not set: set it to development, test, staging or "
    "production before starting the API."
)

#: What the bootstrap says when it will not create the first administrator.
WEAK_FIRST_SUPERUSER_PASSWORD_MESSAGE = (
    "FIRST_SUPERUSER_PASSWORD is a well-known default or shorter than "
    f"{MIN_SUPERUSER_PASSWORD_LENGTH} characters, which is accepted only with "
    "ENVIRONMENT=development or ENVIRONMENT=test. Set a stronger "
    "FIRST_SUPERUSER_PASSWORD, or set ENVIRONMENT."
)

#: The most bcrypt will hash: it refuses a longer password outright.
MAX_PASSWORD_BYTES = 72

#: What the bootstrap says when the first administrator's password is too long
#: to hash. Checked only where that administrator is created, never on the
#: settings: a running deployment whose users table is populated ignores the
#: setting and must keep starting whatever it holds.
LONG_FIRST_SUPERUSER_PASSWORD_MESSAGE = (
    "FIRST_SUPERUSER_PASSWORD is too long: it must be at most "
    f"{MAX_PASSWORD_BYTES} bytes when encoded as UTF-8 (a character outside "
    "ASCII takes two to four). Set a shorter FIRST_SUPERUSER_PASSWORD."
)

#: The same, for a value that has no UTF-8 encoding at all. Fixed text: the
#: value itself is never repeated.
UNENCODABLE_FIRST_SUPERUSER_PASSWORD_MESSAGE = (
    "FIRST_SUPERUSER_PASSWORD cannot be encoded as UTF-8. Set a "
    "FIRST_SUPERUSER_PASSWORD of at most "
    f"{MAX_PASSWORD_BYTES} bytes when encoded as UTF-8."
)

#: Legacy spelling -> canonical spelling.
LEGACY_ENVIRONMENT_ALIASES: Dict[str, str] = {
    "dev": "development",
    "prod": "production",
    # The AWS demo stack (`ENVIRONMENT=demo cdk deploy`, infrastructure/cdk/
    # app.py) runs with APP_ENV=demo: a public, seeded development-grade
    # deployment.
    "demo": "development",
}

#: The dotenv file each environment's settings class reads.
ENV_FILES: Dict[str, str] = {
    "development": ".env.dev",
    "test": ".env.test",
    "staging": ".env.prod",
    "production": ".env.prod",
}

# Placeholder secrets that ship in the repository (class defaults,
# docker-compose.yml, .env.example) or are otherwise well known.  Any secret
# equal to one of these, or starting with one of the prefixes, is refused in
# HARDENED_ENVIRONMENTS regardless of its length.
PLACEHOLDER_SECRETS: frozenset = frozenset(
    {
        "",
        "secret",
        "changeme",
        "change-me",
        "password",
        "supersecret",
        "default-secret-key-for-testing",
        "development_secret_key_change_in_production",
        "dev-audit-key-change-in-production",
    }
)
PLACEHOLDER_SECRET_PREFIXES: tuple = (
    "dev-only-",
    "dev-audit-",
    "development_secret",
    "default-secret",
    "ci-only-",
    "test-",
)

#: First-administrator passwords refused in staging/production whatever their
#: length.  ``Demo1234!`` is the seeded demo password and appears in the
#: public docs and docker-compose.yml.  Compared lower-cased.
WEAK_SUPERUSER_PASSWORDS: frozenset = frozenset(
    {"admin", "password", "changeme", "admin123", "", "demo1234!"}
)

#: The example PUBLIC_BASE_URL every message gives: the dashboard's URL, which
#: is also where the API is served (under /api).  Never an ``api.`` host.
PUBLIC_BASE_URL_EXAMPLE = "https://controlvar.example.com"


#: The strings pydantic's ``bool`` reads as False (case-insensitive, exact).
#: backend/tests/unit/core/test_settings_rules_testing_flag.py pins this
#: against pydantic itself.
_FALSE_WORDS: frozenset = frozenset({"0", "off", "f", "false", "n", "no"})


def testing_flag_is_set(value: Optional[str]) -> bool:
    """Whether a raw ``TESTING`` value turns the test runner's flag on.

    Unset or empty is off, and so is anything pydantic's ``bool`` reads as
    False (``0``, ``off``, ``f``, ``false``, ``n``, ``no``, in any case).
    Everything else is on: what pydantic reads as True (``1``, ``true``,
    ``on``, ``t``, ``y``, ``yes``, in any case) and, failing closed, any other
    non-empty value, such as ``" true"`` or ``enabled``.
    """
    if value is None or value == "":
        return False
    return value.lower() not in _FALSE_WORDS


def testing_refusal(environment: Any, testing: Optional[str]) -> Optional[str]:
    """The refusal for ``TESTING`` with a staging/production environment, or None.

    *environment* is the canonical name; *testing* the raw ``TESTING`` value
    from the process environment. The settings (``config.Settings``) raise
    with this message and the container's start-up check prints it, so the
    two cannot disagree.
    """
    if environment not in HARDENED_ENVIRONMENTS or not testing_flag_is_set(testing):
        return None
    return (
        f"TESTING is for the test runner and cannot be combined with "
        f"ENVIRONMENT={environment}. Remove TESTING from this deployment's "
        "configuration."
    )


def secret_is_placeholder(value: Optional[str]) -> bool:
    """True when *value* is a committed/well-known placeholder secret."""
    normalised = (value or "").strip().lower()
    if normalised in PLACEHOLDER_SECRETS:
        return True
    return any(normalised.startswith(prefix) for prefix in PLACEHOLDER_SECRET_PREFIXES)


#: The setting that holds the keys stored credentials are encrypted with.
CREDENTIAL_KEYS_SETTING = "WAREHOUSE_CREDENTIALS_KEYS"

#: How to make one key, given in every message about the setting.
GENERATE_FERNET_KEY = (
    "Generate one with: python -c "
    '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
)


def split_key_list(value: Optional[str]) -> List[str]:
    """The entries of a comma-separated key list, stripped; ``[]`` when blank."""
    if value is None or not value.strip():
        return []
    return [entry.strip() for entry in value.split(",")]


def fernet_key_is_well_formed(key: str) -> bool:
    """Whether *key* is 32 bytes in URL-safe base64, as Fernet requires.

    Stricter than ``Fernet()`` itself, which discards characters outside the
    alphabet before decoding: anything this accepts, ``Fernet()`` accepts.
    """
    try:
        decoded = base64.b64decode(key.encode("ascii"), altchars=b"-_", validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return False
    return len(decoded) == 32


def credential_keys_error(value: Optional[str]) -> Optional[str]:
    """Why *value* is not a usable ``WAREHOUSE_CREDENTIALS_KEYS``, or ``None``.

    Blank or absent is not an error here: the setting is optional, and a
    feature that needs it refuses at use.  A value that is set must be a
    comma-separated list of Fernet keys with no empty entry and no published
    placeholder.  The message names the setting and the position of the bad
    entry, never a key.
    """
    entries = split_key_list(value)
    if not entries:
        return None
    for position, entry in enumerate(entries, start=1):
        if not entry:
            return (
                f"{CREDENTIAL_KEYS_SETTING} has an empty entry (key {position}). "
                "It is a comma-separated list of Fernet keys, newest first."
            )
        if secret_is_placeholder(entry):
            return (
                f"{CREDENTIAL_KEYS_SETTING} key {position} is a published placeholder. "
                f"{GENERATE_FERNET_KEY}"
            )
        if not fernet_key_is_well_formed(entry):
            return (
                f"{CREDENTIAL_KEYS_SETTING} key {position} is not a Fernet key "
                f"(32 bytes, URL-safe base64). {GENERATE_FERNET_KEY}"
            )
    return None


def secret_is_weak(value: Optional[str]) -> bool:
    """A SECRET_KEY or AUDIT_HMAC_KEY that staging/production refuse."""
    return secret_is_placeholder(value) or len(value or "") < MIN_SECRET_KEY_LENGTH


def superuser_password_is_weak(value: Optional[str]) -> bool:
    """A FIRST_SUPERUSER_PASSWORD that staging/production refuse."""
    value = value or ""
    return (
        value.lower() in WEAK_SUPERUSER_PASSWORDS
        or len(value) < MIN_SUPERUSER_PASSWORD_LENGTH
    )


def first_superuser_password_length_refusal(value: Optional[str]) -> Optional[str]:
    """Why ``value`` cannot be hashed as the first administrator's password, or None.

    Length is measured in UTF-8 bytes, which is what bcrypt counts. Call this
    only where the first administrator is about to be created; see
    :data:`LONG_FIRST_SUPERUSER_PASSWORD_MESSAGE` for why it is not a setting
    rule.
    """
    try:
        encoded = (value or "").encode("utf-8")
    except UnicodeEncodeError:
        return UNENCODABLE_FIRST_SUPERUSER_PASSWORD_MESSAGE
    if len(encoded) > MAX_PASSWORD_BYTES:
        return LONG_FIRST_SUPERUSER_PASSWORD_MESSAGE
    return None


def canonical_environment_quiet(value: str) -> str:
    """The canonical spelling of an environment name, without a warning."""
    normalised = value.strip().lower()
    return LEGACY_ENVIRONMENT_ALIASES.get(normalised, normalised)


def public_base_url_error(value: str) -> Optional[str]:
    """Why *value* is not a usable PUBLIC_BASE_URL, or ``None`` if it is.

    A scheme and a host, no path.  *value* is non-empty; the caller decides
    what an empty one means.
    """
    candidate = value.strip().rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return (
            "PUBLIC_BASE_URL must be an absolute http(s) URL with a host, "
            f"e.g. {PUBLIC_BASE_URL_EXAMPLE} (the dashboard's URL) -- got {value!r}"
        )
    if parsed.path:
        return (
            "PUBLIC_BASE_URL must not carry a path; it is the origin the "
            f"service is reached at -- got {value!r}"
        )
    return None


def parse_allowed_hosts(value: Any) -> List[str]:
    """ALLOWED_HOSTS as a list: comma-separated or a JSON array.

    The spelling CORS_ORIGINS also takes. Blank entries are dropped. Raises
    ``ValueError`` (``json.JSONDecodeError``) on a value that starts like a
    JSON array but is not one.
    """
    if isinstance(value, str) and value.strip().startswith("["):
        value = json.loads(value)
    if isinstance(value, str) and value:
        return [i.strip() for i in value.split(",") if i.strip()]
    if isinstance(value, list):
        return [str(i).strip() for i in value if str(i).strip()]
    return []


def allowed_host_pattern_error(pattern: str) -> Optional[str]:
    """Why an ALLOWED_HOSTS entry can never match, or ``None`` if it can.

    A wildcard written `*example.com` (no dot) is not a wildcard: it is a
    literal hostname containing an asterisk, it matches nothing, and a
    deployment configured with it refuses every request while every health
    check stays green -- because the probes are exempt.

    Accepted: `example.com`, `*.example.com`, and `*` (meaningful, "allow
    anything"; staging and production refuse it separately). Refused in every
    environment: any other `*`, and a leading dot.
    """
    if pattern == "*":
        return None
    if pattern.startswith("*."):
        rest = pattern[2:]
        if rest and "*" not in rest:
            return None
    elif "*" not in pattern and not pattern.startswith("."):
        return None
    return (
        f"ALLOWED_HOSTS entry {pattern[:200]!r} is not a hostname or a `*.` "
        "wildcard and would match nothing, refusing every request while "
        "the health probes -- which are exempt -- stayed green. Write "
        "`example.com` or `*.example.com`."
    )
