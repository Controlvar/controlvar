"""
Authentication endpoints for the API.

Two providers share this router (selected by ``settings.AUTH_PROVIDER``):

* ``local`` (the default) — e-mail + password against the
  ``users`` table, HS256 JWTs issued by ``core.security``.  Endpoints:
  ``POST /login``, ``GET /me``, ``POST /logout`` and the OAuth2 form
  ``POST /token`` (``username`` = e-mail) so Swagger's *Authorize* works.
* ``cognito`` — AWS Cognito.  The legacy endpoints (signup, confirm, token,
  forgot/reset password, refresh, me) forward to the user pool; ``/login``
  answers 404 because Cognito has its own flows.  The dashboard signs in
  through the Cognito Hosted UI: ``POST /cognito/exchange`` turns the code
  the Hosted UI sends back into an access token, and ``POST /logout`` signs
  the user out of Cognito (GlobalSignOut).

``GET /config`` (no credentials, either provider) tells the dashboard which
of the two it is and, under Cognito, where the Hosted UI is.

Six endpoints exist only for Cognito: ``POST /signup``, ``/confirm``,
``/forgot-password``, ``/reset-password``, ``/refresh`` and
``/cognito/exchange``.  Under any other provider they answer 404 with
``COGNITO_ONLY_DETAIL`` before the request body is validated and before any
Cognito client is created.

Under ``cognito``, ``/signup`` and ``/confirm`` (self sign-up) also need
``COGNITO_SELF_SIGNUP_ENABLED=true``; without it they answer 404 with
``SELF_SIGNUP_DISABLED_DETAIL``, at the same point and with no Cognito call,
and an administrator creates users in the user pool.  The other four are
unaffected by that setting.
"""

import logging
from typing import Any, List, Optional, Union

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token, oauth2_scheme
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.user import User
from backend.app.schemas.auth import (
    AuthConfigResponse,
    CognitoCodeExchangeRequest,
    CognitoCodeExchangeResponse,
    ConfirmForgotPasswordRequest,
    ConfirmForgotPasswordResponse,
    ConfirmSignUpRequest,
    ConfirmSignUpResponse,
    DemoAccount,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    LoginResponse,
    RefreshTokenRequest,
    SignUpRequest,
    SignUpResponse,
    TokenResponse,
    UserInfoResponse,
    UserMe,
)
from backend.app.services import cognito_hosted_ui
from backend.app.services.audit_service import AuditService
from backend.app.services.auth_service import (
    CognitoAuthService,
    CognitoTokenRefused,
    configured_pool_and_client,
    log_token_refused,
    sign_in_logger,
    unset_hosted_ui_settings,
)
from backend.app.services.cognito_accounts import cognito_external_id
from backend.app.services.local_auth_service import (
    AccountLockedError,
    InvalidCredentialsError,
    local_auth_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(route_class=OffLoopRoute)

INVALID_CREDENTIALS_DETAIL = "Invalid email or password"

COGNITO_ONLY_DETAIL = (
    "Endpoint not available: AUTH_PROVIDER is not 'cognito'. "
    "Sign in with POST /api/v1/auth/login; "
    "an administrator creates accounts and resets passwords."
)

SELF_SIGNUP_DISABLED_DETAIL = (
    "Endpoint not available: self sign-up is turned off "
    "(COGNITO_SELF_SIGNUP_ENABLED is not true). "
    "An administrator creates users in the Cognito user pool."
)

CREDENTIALS_REFUSED_DETAIL = "Could not validate credentials"

# The code exchange's answers. Fixed sentences: nothing Cognito says is
# repeated, and the dashboard shows its own copy for each status.
CODE_EXCHANGE_REFUSED_DETAIL = (
    "Cognito did not accept the sign-in code. Start the sign-in again."
)
CODE_EXCHANGE_FAILED_DETAIL = "Cognito did not complete the sign-in. Try again."
CODE_EXCHANGE_TIMEOUT_DETAIL = "Cognito did not answer in time. Try again."
HOSTED_UI_NOT_SET_UP_DETAIL = (
    "Sign-in through the Cognito Hosted UI is not set up on this server: "
    "COGNITO_DOMAIN, COGNITO_CLIENT_ID and COGNITO_USER_POOL_ID must all be set."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_local_provider() -> bool:
    return settings.AUTH_PROVIDER == "local"


def _require_local_provider() -> None:
    """404 for endpoints that only exist on the local provider."""
    if not _is_local_provider():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Endpoint not available: AUTH_PROVIDER is not 'local'",
        )


def _require_cognito_provider() -> None:
    """404 for endpoints that only exist on the Cognito provider.

    Used as a route dependency, so it runs before the body is validated and
    before the endpoint creates a ``CognitoAuthService``.  Any provider other
    than ``cognito`` is refused.
    """
    if settings.AUTH_PROVIDER != "cognito":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=COGNITO_ONLY_DETAIL,
        )


def _require_cognito_self_signup(
    _provider: None = Depends(_require_cognito_provider),
) -> None:
    """404 for self sign-up (``/signup``, ``/confirm``) unless it is turned on.

    The provider check runs first as a sub-dependency, so under any provider
    other than ``cognito`` the answer is ``COGNITO_ONLY_DETAIL`` exactly as
    for the other Cognito-only routes.  Under ``cognito`` the route is refused
    unless ``COGNITO_SELF_SIGNUP_ENABLED`` is true.  Like the provider check it
    runs before the body is validated and before any ``CognitoAuthService``
    is created.
    """
    if not settings.COGNITO_SELF_SIGNUP_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=SELF_SIGNUP_DISABLED_DETAIL,
        )


def user_to_me(user: User) -> UserMe:
    """Serialise a ``User`` row into the ``UserMe`` contract shape."""
    return UserMe(
        id=user.id,
        email=user.email,
        username=user.username,
        full_name=user.full_name,
        role=user.role,
        is_superuser=bool(user.is_superuser),
        is_active=bool(user.is_active),
        auth_provider=settings.AUTH_PROVIDER,
    )


def _issue_local_login(db: Session, email: str, password: str) -> LoginResponse:
    """Authenticate with the local provider and build the token response."""
    try:
        user = local_auth_service.authenticate(db, email, password)
    except AccountLockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=(
                "Too many failed login attempts; account temporarily locked. "
                f"Retry in {exc.retry_after_seconds} seconds."
            ),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    except InvalidCredentialsError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=INVALID_CREDENTIALS_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_local_access_token(user)
    response = LoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=settings.LOCAL_AUTH_TOKEN_TTL_MINUTES * 60,
        user=user_to_me(user),
    )
    # After the sign-in has succeeded; a failed audit write does not refuse
    # it. The entry records the provider only.
    AuditService.record_after_commit(
        db,
        actor=user,
        action=ActionType.USER_LOGIN,
        entity_type=EntityType.USER,
        entity_id=user.id,
        entity_name=user.username or str(user.id),
        after={"provider": "local"},
    )
    return response


def _cognito_account(
    db: Session, auth_service: CognitoAuthService, access_token: Any
) -> Optional[User]:
    """The account linked to the Cognito user an access token was issued to,
    found as ``resolve_cognito_user`` finds it (``external_id =
    "cognito:<sub>"``), or ``None``. Nothing is created or changed.

    Cognito issued the token in answer to this request's own sign-in call, so
    its claims are read as ``get_user_with_groups`` reads them once
    ``GetUser`` has accepted a token: the issuer and app client are compared
    with configuration, and the signature is not verified again. A token that
    fails that comparison is refused on every request, and has no account.
    """
    try:
        claims = auth_service._check_token_audience(access_token)
    except CognitoTokenRefused:
        return None
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        return None
    return (
        db.query(User)
        .filter(User.external_id == cognito_external_id(sub.strip()))
        .first()
    )


def _record_cognito_login(
    db: Session, auth_service: CognitoAuthService, access_token: Any
) -> None:
    """Write ``user_login`` for a Cognito sign-in whose account exists.

    Called only after Cognito has accepted the credentials. A sign-in with no
    linked account writes nothing: the account is created by the first
    request made with the token, which writes ``user_create``. As for the
    local sign-in, the entry is written after the sign-in has succeeded and
    records the provider only; if the account cannot be looked up or the entry
    cannot be written, one ERROR line is logged and the sign-in still succeeds.
    """
    try:
        user = _cognito_account(db, auth_service, access_token)
    except Exception as exc:
        AuditService._write_failed(
            db, ActionType.USER_LOGIN, EntityType.USER, None, exc
        )
        return
    if user is None:
        return
    AuditService.record_after_commit(
        db,
        actor=user,
        action=ActionType.USER_LOGIN,
        entity_type=EntityType.USER,
        entity_id=user.id,
        entity_name=user.username or str(user.id),
        after={"provider": "cognito"},
    )


def _credentials_refused(detail: str = CREDENTIALS_REFUSED_DETAIL) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _hosted_ui_missing() -> List[str]:
    """The names of the unset settings sign-in through the Hosted UI needs."""
    return unset_hosted_ui_settings(settings.COGNITO_DOMAIN)


def _log_code_exchange_failed(failure: cognito_hosted_ui.CodeExchangeFailed) -> None:
    """Record why a code exchange failed: the reason and, for a refusal,
    Cognito's OAuth error code. Nothing else Cognito said is logged."""
    sign_in_logger.warning(
        "Cognito code exchange failed (%s, %s)",
        failure.reason,
        failure.cognito_error,
        extra={"reason": failure.reason, "cognito_error": failure.cognito_error},
    )


def _log_sign_out_failed(exc: Exception) -> None:
    """Record a GlobalSignOut that failed: the exception's type and, from
    Cognito, its error code. Never the message or the token."""
    code = None
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
    logger.error(
        "Cognito GlobalSignOut failed on /auth/logout (%s, %s); the user's "
        "tokens were not revoked",
        type(exc).__name__,
        code,
    )


def _sign_out_of_cognito(token: Optional[str]) -> None:
    """Check the bearer as every request's is, then GlobalSignOut with it.

    The check is GetUser and the issuer and app-client comparison, as
    ``deps.get_current_user`` makes them (no account is looked up or made):
    401 without a token or with one refused. GlobalSignOut is best effort: a
    failure is logged and the caller still answers 204.
    """
    if not token:
        raise _credentials_refused("Not authenticated")
    auth_service = CognitoAuthService()
    try:
        auth_service.get_user(access_token=token)
    except CognitoTokenRefused as refusal:
        log_token_refused(refusal, "/auth/logout")
        raise _credentials_refused()
    except ValueError:
        raise _credentials_refused()
    try:
        auth_service.global_sign_out(access_token=token)
    except Exception as exc:
        _log_sign_out_failed(exc)


# ---------------------------------------------------------------------------
# Local provider endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/login",
    response_model=LoginResponse,
    responses={
        401: {"description": INVALID_CREDENTIALS_DETAIL},
        423: {"description": "Account temporarily locked after repeated failures"},
    },
)
def login_local(body: LoginRequest, db: Session = Depends(deps.get_db)) -> Any:
    """
    Sign in with e-mail and password (``AUTH_PROVIDER=local``).

    Returns a bearer token plus the user's profile.  The same 401 message is
    used for an unknown address, a wrong password and an inactive account so
    the endpoint cannot be used to enumerate users.  After
    ``LOCAL_AUTH_MAX_FAILED_ATTEMPTS`` failures within
    ``LOCAL_AUTH_LOCKOUT_MINUTES`` the address answers 423 until the window
    expires.  The email address is matched whatever its letter case (A–Z),
    and every casing of it counts toward the same failed attempts.
    """
    _require_local_provider()
    return _issue_local_login(db, body.email, body.password)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {
            "description": (
                "AUTH_PROVIDER=cognito: no bearer token, or one Cognito does not accept"
            )
        }
    },
)
# The function keeps its name so the operation keeps its operationId.
def logout_local(token: Optional[str] = Depends(oauth2_scheme)) -> Response:
    """
    Sign out.

    * ``AUTH_PROVIDER=local`` (or the development sign-in bypass): local
      tokens are stateless, so this endpoint exists for the client's benefit
      (it discards the token); nothing is revoked server-side, and no token is
      needed.
    * ``AUTH_PROVIDER=cognito``: the bearer token is checked as every
      request's is (401 without one, or with one Cognito does not accept),
      then Cognito is asked to sign the user out everywhere (GlobalSignOut):
      this token, the user's other access tokens and their refresh tokens stop
      working, so the user's other sessions end too. If Cognito fails, the
      answer is still 204 and the failure is logged.
    """
    if _is_local_provider() or deps.dev_auth_bypass_active():
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if settings.AUTH_PROVIDER != "cognito":
        _require_local_provider()
    _sign_out_of_cognito(token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Cognito-only endpoints + provider-aware /token and /me
# ---------------------------------------------------------------------------


@router.post(
    "/signup",
    response_model=SignUpResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_cognito_self_signup)],
    responses={
        404: {
            "description": (
                "Self sign-up is not available: AUTH_PROVIDER is not "
                "'cognito', or COGNITO_SELF_SIGNUP_ENABLED is not true"
            )
        }
    },
)
def signup(signup_data: SignUpRequest) -> Any:
    """
    Register a new user in the Cognito user pool.

    Answers 404 unless ``AUTH_PROVIDER=cognito`` and
    ``COGNITO_SELF_SIGNUP_ENABLED=true``; the user pool must also allow self
    sign-up.  When it is off, an administrator creates users in the user pool.
    """
    auth_service = CognitoAuthService()
    try:
        response = auth_service.sign_up(
            username=signup_data.username,
            password=signup_data.password,
            email=signup_data.email,
            given_name=signup_data.given_name,
            family_name=signup_data.family_name,
        )
        return response
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post(
    "/confirm",
    response_model=ConfirmSignUpResponse,
    dependencies=[Depends(_require_cognito_self_signup)],
    responses={
        404: {
            "description": (
                "Self sign-up is not available: AUTH_PROVIDER is not "
                "'cognito', or COGNITO_SELF_SIGNUP_ENABLED is not true"
            )
        }
    },
)
def confirm_signup(confirm_data: ConfirmSignUpRequest) -> Any:
    """
    Confirm a self sign-up with the verification code Cognito sent.

    Answers 404 unless ``AUTH_PROVIDER=cognito`` and
    ``COGNITO_SELF_SIGNUP_ENABLED=true``.
    """
    auth_service = CognitoAuthService()
    try:
        response = auth_service.confirm_sign_up(
            username=confirm_data.username,
            confirmation_code=confirm_data.confirmation_code,
        )
        return response
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post("/token", response_model=Union[LoginResponse, TokenResponse])
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(deps.get_db),
) -> Any:
    """
    OAuth2 compatible token login, get an access token for future requests.

    With ``AUTH_PROVIDER=local`` the form's ``username`` field is the user's
    e-mail address and the response has the same shape as ``/login`` (this is
    what makes Swagger's *Authorize* button work).  With ``cognito`` the
    credentials are forwarded to the user pool; a successful sign-in of a
    user who already has an account writes ``user_login`` with the provider
    ``cognito``.
    """
    if _is_local_provider():
        return _issue_local_login(db, form_data.username, form_data.password)

    auth_service = CognitoAuthService()
    try:
        response = auth_service.sign_in(
            username=form_data.username,
            password=form_data.password,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )
    _record_cognito_login(db, auth_service, response.get("access_token"))
    return response


@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
    dependencies=[Depends(_require_cognito_provider)],
)
def forgot_password(forgot_password_data: ForgotPasswordRequest) -> Any:
    """
    Initiate the forgot password flow.
    """
    auth_service = CognitoAuthService()
    try:
        response = auth_service.forgot_password(
            username=forgot_password_data.username,
        )
        return response
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post(
    "/reset-password",
    response_model=ConfirmForgotPasswordResponse,
    dependencies=[Depends(_require_cognito_provider)],
)
def reset_password(reset_data: ConfirmForgotPasswordRequest) -> Any:
    """
    Complete the forgot password flow by setting a new password.
    """
    auth_service = CognitoAuthService()
    try:
        response = auth_service.confirm_forgot_password(
            username=reset_data.username,
            confirmation_code=reset_data.confirmation_code,
            new_password=reset_data.new_password,
        )
        return response
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    dependencies=[Depends(_require_cognito_provider)],
)
def refresh_token(refresh_data: RefreshTokenRequest) -> Any:
    """
    Refresh the access token using a refresh token.
    """
    auth_service = CognitoAuthService()
    try:
        response = auth_service.refresh_token(
            refresh_token=refresh_data.refresh_token,
        )
        return response
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.get("/me", response_model=Union[UserMe, UserInfoResponse])
def get_user_info(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(deps.get_db),
) -> Any:
    """
    Get current user information.

    * ``AUTH_PROVIDER=local`` (or the dev-auth bypass): the ``UserMe`` profile
      of the bearer's user — ``{id, email, username, full_name, role,
      is_superuser, is_active, auth_provider}``.
    * ``AUTH_PROVIDER=cognito``: the Cognito ``{username, attributes}`` record.
    """
    if _is_local_provider() or deps.dev_auth_bypass_active():
        user = deps.require_active_user(deps.get_current_user(token=token, db=db))
        return user_to_me(user)

    cognito_token = deps.get_token(request)
    auth_service = CognitoAuthService()
    try:
        response = auth_service.get_user(access_token=cognito_token)
        return response
    except CognitoTokenRefused as refusal:
        log_token_refused(refusal, "/auth/me")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# Dashboard sign-in: the configuration, and the Cognito Hosted UI (#708)
# ---------------------------------------------------------------------------


@router.get(
    "/config",
    response_model=AuthConfigResponse,
    response_model_exclude_none=True,
    openapi_extra={"x-stability": "beta"},
)
def get_auth_config() -> Any:
    """
    How the dashboard signs in. Needs no credentials.

    * ``AUTH_PROVIDER=local``, or the development sign-in bypass:
      ``{"provider": "local"}``.
    * ``AUTH_PROVIDER=cognito`` with ``COGNITO_DOMAIN``, ``COGNITO_CLIENT_ID``
      and ``COGNITO_USER_POOL_ID`` set: ``{"provider": "cognito",
      "hosted_ui_domain", "client_id", "scopes"}``, what the dashboard needs
      to send the browser to the Hosted UI. The callback is not served: it is
      the dashboard's own origin followed by ``/auth/callback``, which must be
      an allowed callback URL of the app client.
    * ``AUTH_PROVIDER=cognito`` with any of the three unset:
      ``{"provider": "cognito", "configured": false, "missing": [...]}``,
      naming the unset settings.
    * ``DEMO_MODE=true`` adds ``"demo_mode": true`` to any of these, and
      ``"demo_account": {"email", "password"}`` when ``DEMO_ACCOUNT_EMAIL`` and
      ``DEMO_ACCOUNT_PASSWORD`` are set: the shared account a demo visitor
      signs in with, whose password is public by design.

    Every value comes from the configuration, none from the request.
    """
    response = _sign_in_config()
    if settings.DEMO_MODE:
        response.demo_mode = True
        if settings.demo_account_published:
            response.demo_account = DemoAccount(
                email=settings.DEMO_ACCOUNT_EMAIL,
                password=settings.DEMO_ACCOUNT_PASSWORD,
            )
    return response


def _sign_in_config() -> AuthConfigResponse:
    """``GET /auth/config`` without the demo fields."""
    if settings.AUTH_PROVIDER != "cognito" or deps.dev_auth_bypass_active():
        return AuthConfigResponse(provider="local")
    missing = _hosted_ui_missing()
    if missing:
        return AuthConfigResponse(provider="cognito", configured=False, missing=missing)
    _, client_id = configured_pool_and_client()
    return AuthConfigResponse(
        provider="cognito",
        hosted_ui_domain=settings.COGNITO_DOMAIN,
        client_id=client_id,
        scopes=list(cognito_hosted_ui.HOSTED_UI_SCOPES),
    )


@router.post(
    "/cognito/exchange",
    response_model=CognitoCodeExchangeResponse,
    dependencies=[Depends(_require_cognito_provider)],
    openapi_extra={"x-stability": "beta"},
    responses={
        400: {"description": CODE_EXCHANGE_REFUSED_DETAIL},
        404: {"description": "AUTH_PROVIDER is not 'cognito'"},
        502: {"description": CODE_EXCHANGE_FAILED_DETAIL},
        503: {"description": HOSTED_UI_NOT_SET_UP_DETAIL},
        504: {"description": CODE_EXCHANGE_TIMEOUT_DETAIL},
    },
)
def exchange_cognito_code(
    body: CognitoCodeExchangeRequest, db: Session = Depends(deps.get_db)
) -> Any:
    """
    Exchange the code the Cognito Hosted UI sent to the dashboard for an
    access token (``AUTH_PROVIDER=cognito`` only; 404 otherwise).

    The body is what the dashboard's callback received and kept: the
    ``code``, the PKCE ``code_verifier`` and the ``redirect_uri`` the sign-in
    started with. The API asks the token endpoint of ``COGNITO_DOMAIN`` for
    the app client ``COGNITO_CLIENT_ID``; neither is read from the request.
    The answer is the access token and its lifetime in seconds, which the
    dashboard then sends as a bearer token like any other; the id and refresh
    tokens are not returned.

    Refused: a code, verifier or redirect URI Cognito does not accept (an
    expired or reused code, say) answers 400; Cognito unreachable or
    answering with no access token, 502; no answer within
    ``COGNITO_TOKEN_TIMEOUT_SECONDS``, 504; the Hosted UI settings unset, 503.
    Each with a fixed sentence: nothing Cognito says is repeated. Limited to
    10 requests a minute per client address. A sign-in by a user who already
    has an account writes ``user_login`` with the provider ``cognito``.
    """
    if _hosted_ui_missing():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=HOSTED_UI_NOT_SET_UP_DETAIL,
        )
    auth_service = CognitoAuthService()
    try:
        tokens = cognito_hosted_ui.exchange_code(
            domain=settings.COGNITO_DOMAIN,
            client_id=auth_service.client_id,
            code=body.code,
            code_verifier=body.code_verifier,
            redirect_uri=body.redirect_uri,
            timeout=settings.COGNITO_TOKEN_TIMEOUT_SECONDS,
        )
    except cognito_hosted_ui.CodeExchangeRefused as failure:
        _log_code_exchange_failed(failure)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=CODE_EXCHANGE_REFUSED_DETAIL,
        )
    except cognito_hosted_ui.CodeExchangeTimedOut as failure:
        _log_code_exchange_failed(failure)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=CODE_EXCHANGE_TIMEOUT_DETAIL,
        )
    except cognito_hosted_ui.CodeExchangeUnanswered as failure:
        _log_code_exchange_failed(failure)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=CODE_EXCHANGE_FAILED_DETAIL,
        )
    _record_cognito_login(db, auth_service, tokens["access_token"])
    return CognitoCodeExchangeResponse(**tokens)
