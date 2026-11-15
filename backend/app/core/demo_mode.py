"""Demo mode: a read-only switch for a public demo install.

With ``DEMO_MODE=true`` the API refuses every request that could create or
change something, except an exact allow-list of eight POSTs: signing in and
out, and the five calls an SDK makes to report assignments, events and client
errors. ``backend/app/middleware/demo_mode_middleware.py`` applies it to every
request, before routing, body, authentication or any database session, so a
route added later is refused until someone adds it here on purpose.

This module holds the contract and the start-up rules. It imports no settings
object (``backend.app.core.config`` imports it to validate the settings), so
every function here takes its inputs as arguments.

The contract
------------
* A request whose method is ``GET``, ``HEAD`` or ``OPTIONS`` passes.
* Any other method passes only when ``(method, path)`` is one of
  :func:`allowed_requests`, compared on ``scope["path"]``: the string the
  router matches, exactly. A trailing slash, a doubled slash, another letter
  case, a mounted prefix or a method-override header is not on the list and is
  refused.
* Everything else is answered 403 with :data:`REFUSAL_BODY`. The demo layer
  writes no log line of its own (the server's access log still has its one
  line per request, refused or not).
* Every websocket is refused.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

#: The machine-readable reason, in ``detail.code`` of every refusal.
DEMO_READ_ONLY_CODE = "demo_read_only"

#: The sentence a visitor reads. No URL in it, so a client can show it as it is.
DEMO_READ_ONLY_MESSAGE = (
    "This is a read-only demo: creating and changing things is turned off."
)

#: The body of every refusal. ``detail`` is an object, as elsewhere in the API
#: (``{"detail": {"code", "message"}}``), so a client reads ``detail.code``.
REFUSAL_BODY: Dict[str, Dict[str, str]] = {
    "detail": {"code": DEMO_READ_ONLY_CODE, "message": DEMO_READ_ONLY_MESSAGE}
}

#: Methods that never change anything and always pass.
SAFE_METHODS: FrozenSet[str] = frozenset({"GET", "HEAD", "OPTIONS"})

#: The eight POSTs demo mode allows, relative to ``API_V1_STR``. Literal paths:
#: no pattern, no prefix, no path parameter.
ALLOWED_POST_PATHS: Tuple[str, ...] = (
    # Signing in and out. /auth/token is the same sign-in for the API docs'
    # Authorize button; /auth/logout changes nothing under the local provider.
    "auth/login",
    "auth/token",
    "auth/logout",
    # What the SDKs and the demo apps send.
    "tracking/assign",
    "tracking/track",
    "tracking/batch",
    "tracking/errors",
    "tracking/errors/batch",
)


def allowed_requests(api_v1_str: str) -> FrozenSet[Tuple[str, str]]:
    """The ``(method, path)`` pairs a non-safe request must match, exactly."""
    prefix = api_v1_str.rstrip("/")
    return frozenset(("POST", f"{prefix}/{path}") for path in ALLOWED_POST_PATHS)


def is_refused(method: str, path: str, allowed: FrozenSet[Tuple[str, str]]) -> bool:
    """True when demo mode refuses an HTTP request with this method and path."""
    if method in SAFE_METHODS:
        return False
    return (method, path) not in allowed


# ---------------------------------------------------------------------------
# Start-up rules
# ---------------------------------------------------------------------------

#: Settings that let the API call a service outside the install: e-mail,
#: Slack and the scheduler's webhook. Under demo mode each must be empty
#: (false for the two switches), or the API does not start.
OUTBOUND_SETTINGS: Tuple[str, ...] = (
    "NOTIFICATION_WEBHOOK_URL",
    "SLACK_ENABLED",
    "SLACK_BOT_TOKEN",
    "EMAIL_ENABLED",
    "SENDGRID_API_KEY",
    "SMTP_HOST",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
)

#: Variables the LLM providers read from the process environment where they
#: call out (they are not settings): Anthropic's SDK takes either of its two,
#: OpenAI's its key, and the Gemini provider its key. Under demo mode each must
#: be unset or empty.
OUTBOUND_ENVIRONMENT_VARIABLES: Tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
)


#: The longest demo-account address and password the API publishes.
MAX_ACCOUNT_EMAIL_LENGTH = 254
MAX_ACCOUNT_PASSWORD_LENGTH = 256


def demo_account_published(demo_mode: bool, email: str, password: str) -> bool:
    """True when ``GET /auth/config`` publishes a demo account: demo mode on and
    both of the pair set. Its password is then public."""
    return demo_mode is True and _is_set(email) and _is_set(password)


def _is_set(value: Any) -> bool:
    """A setting counts as set when it is true or a non-empty value."""
    if isinstance(value, str):
        return value != ""
    return bool(value)


def demo_settings_refusals(
    *,
    demo_mode: bool,
    account_email: str,
    account_password: str,
    auth_provider: str,
    first_superuser: str,
    outbound_settings: Mapping[str, Any],
    environment: Mapping[str, str],
) -> List[str]:
    """Why these settings must not start the API; an empty list when they may.

    Each reason names the setting and never its value.

    * ``DEMO_ACCOUNT_EMAIL`` and ``DEMO_ACCOUNT_PASSWORD`` are set together or
      not at all.
    * They are set only with ``DEMO_MODE=true``, so a misspelt ``DEMO_MODE``
      (which the settings ignore) stops the API instead of serving writes.
    * ``DEMO_MODE=true`` runs only with ``AUTH_PROVIDER=local``: a visitor
      signs in with a local password, and the allow-list holds no other
      sign-in route (the Cognito code exchange among them). The account pair
      likewise needs the local provider.
    * The demo account is never ``FIRST_SUPERUSER``, compared without regard
      to letter case.
    * Its address, as published, contains ``@`` and is at most
      :data:`MAX_ACCOUNT_EMAIL_LENGTH` characters; its password is at most
      :data:`MAX_ACCOUNT_PASSWORD_LENGTH`. A client offers the one-click
      sign-in only for an account of that shape.
    * Under ``DEMO_MODE=true`` every outbound setting and variable is empty.
    """
    reasons: List[str] = []
    email_set = _is_set(account_email)
    password_set = _is_set(account_password)
    if email_set and (
        "@" not in account_email or len(account_email) > MAX_ACCOUNT_EMAIL_LENGTH
    ):
        reasons.append(
            "DEMO_ACCOUNT_EMAIL must be an e-mail address (with an @) of at most "
            f"{MAX_ACCOUNT_EMAIL_LENGTH} characters."
        )
    if password_set and len(account_password) > MAX_ACCOUNT_PASSWORD_LENGTH:
        reasons.append(
            "DEMO_ACCOUNT_PASSWORD must be 1 to "
            f"{MAX_ACCOUNT_PASSWORD_LENGTH} characters."
        )
    if email_set != password_set:
        reasons.append(
            "DEMO_ACCOUNT_EMAIL and DEMO_ACCOUNT_PASSWORD are set together or "
            "not at all; only one of them is set."
        )
    if (email_set or password_set) and demo_mode is not True:
        reasons.append(
            "DEMO_ACCOUNT_EMAIL or DEMO_ACCOUNT_PASSWORD is set but DEMO_MODE is "
            "not true. Set DEMO_MODE=true (check its spelling), or remove the "
            "demo account."
        )
    if demo_mode is True and auth_provider != "local":
        reasons.append(
            "DEMO_MODE=true needs AUTH_PROVIDER=local: a visitor signs in with a "
            "local password, and demo mode refuses every other sign-in route."
        )
    elif (email_set or password_set) and auth_provider != "local":
        reasons.append(
            "A demo account needs AUTH_PROVIDER=local; it is a local password."
        )
    if email_set and account_email.strip().lower() == (
        (first_superuser or "").strip().lower()
    ):
        reasons.append(
            "DEMO_ACCOUNT_EMAIL is FIRST_SUPERUSER. The demo account must be a "
            "viewer, never the first administrator."
        )
    if demo_mode is True:
        named = [n for n in OUTBOUND_SETTINGS if _is_set(outbound_settings.get(n))]
        named += [
            n for n in OUTBOUND_ENVIRONMENT_VARIABLES if _is_set(environment.get(n))
        ]
        if named:
            reasons.append(
                "DEMO_MODE=true refuses settings that call services outside "
                "this install; leave these empty: " + ", ".join(named) + "."
            )
    return reasons


def refusal_message(reasons: Iterable[str]) -> Optional[str]:
    """All the reasons as one message, or ``None`` when there are none."""
    joined = " ".join(reasons)
    return joined or None
