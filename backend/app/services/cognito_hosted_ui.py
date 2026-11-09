"""The Cognito Hosted UI's authorization-code exchange (#708).

The dashboard sends the browser to the Hosted UI with a PKCE challenge, and
Cognito sends it back to the dashboard's callback with a code. The browser
does not ask Cognito's token endpoint for the tokens itself: the dashboard
image's Content-Security-Policy (``connect-src`` defaults to ``'self' ws:
wss:``) refuses a fetch to Cognito's origin. So the dashboard sends the code
to the API, ``POST /api/v1/auth/cognito/exchange``, which asks for the tokens
with :func:`exchange_code` and answers the access token alone.

What is asked, and of whom, is fixed here: ``https://<domain>/oauth2/token``,
a form body with ``grant_type=authorization_code``, the app client id, the
code, the redirect URI and the PKCE verifier (AWS's token endpoint
reference: https://docs.aws.amazon.com/cognito/latest/developerguide/token-endpoint.html).
No Authorization header is sent: the app client is meant to be a public one,
with no secret, as the CDK's is. A client with a secret is not supported (the
token endpoint wants the secret too). The caller passes the domain and client
id from the configuration.

Every failure raises one of three exceptions, each a :class:`CodeExchangeFailed`
standing for one fixed answer. None carries anything Cognito said: its
``error_description`` is text the caller must never repeat, and only the
``error`` code, when it is one of the OAuth 2.0 codes, is kept, for the log.
The id token and the refresh token in Cognito's answer are dropped here and
never returned, logged or stored.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

TOKEN_PATH = "/oauth2/token"

#: The scopes the dashboard asks the Hosted UI for. ``aws.cognito.signin.user.admin``
#: is what GetUser requires, and every request is checked with GetUser.
HOSTED_UI_SCOPES = ("openid", "email", "profile", "aws.cognito.signin.user.admin")

#: The ``error`` codes of RFC 6749 section 5.2, the only part of a refusal kept.
OAUTH_ERRORS = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
    }
)

#: The transport the exchange's HTTP client uses; ``None`` is httpx's own.
#: Tests put the fake token endpoint here, as they install a fake Cognito
#: client with ``CognitoAuthService.client``.
transport: Optional[httpx.BaseTransport] = None


class CodeExchangeFailed(Exception):
    """The exchange did not produce an access token. Carries no text of
    Cognito's: ``reason`` is one of a fixed set, and ``cognito_error`` is an
    OAuth 2.0 error code or ``None``."""

    reason = "failed"

    def __init__(self, cognito_error: Optional[str] = None) -> None:
        super().__init__(self.reason)
        self.cognito_error = cognito_error


class CodeExchangeRefused(CodeExchangeFailed):
    """Cognito answered 4xx: the code, verifier, redirect URI or client was
    not accepted (an expired or reused code is ``invalid_grant``)."""

    reason = "code_exchange_refused"


class CodeExchangeTimedOut(CodeExchangeFailed):
    """Cognito did not answer within the timeout."""

    reason = "code_exchange_timeout"


class CodeExchangeUnanswered(CodeExchangeFailed):
    """Cognito could not be reached, or answered with no usable access token:
    a 5xx or a redirect, a body that is not a JSON object, or one with no
    ``access_token``."""

    reason = "code_exchange_failed"


def _cognito_error(response: httpx.Response) -> Optional[str]:
    """The answer's OAuth ``error`` code, if it is one; never its other text."""
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    return error if error in OAUTH_ERRORS else None


def _lifetime(value: Any) -> int:
    """``expires_in`` as whole seconds; Cognito's default hour when absent or
    not a positive whole number."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return 3600
    return value


def exchange_code(
    *,
    domain: str,
    client_id: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    timeout: float,
) -> Dict[str, Any]:
    """Ask ``https://<domain>/oauth2/token`` for the tokens of ``code``.

    Returns ``{"access_token", "expires_in"}`` and nothing else. ``timeout``
    bounds each of httpx's connect, read, write and pool waits. Redirects are
    not followed. Raises :class:`CodeExchangeRefused`,
    :class:`CodeExchangeTimedOut` or :class:`CodeExchangeUnanswered`.
    """
    form = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
    }
    try:
        with httpx.Client(
            timeout=timeout, transport=transport, follow_redirects=False
        ) as http:
            response = http.post(
                f"https://{domain}{TOKEN_PATH}",
                data=form,
                headers={"Accept": "application/json"},
            )
    except httpx.TimeoutException:
        raise CodeExchangeTimedOut() from None
    except httpx.HTTPError:
        raise CodeExchangeUnanswered() from None

    if 400 <= response.status_code < 500:
        raise CodeExchangeRefused(_cognito_error(response))
    if response.status_code != 200:
        raise CodeExchangeUnanswered()
    try:
        body = response.json()
    except ValueError:
        raise CodeExchangeUnanswered() from None
    access_token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(access_token, str) or not access_token:
        raise CodeExchangeUnanswered()
    return {
        "access_token": access_token,
        "expires_in": _lifetime(body.get("expires_in")),
    }
