"""
Pydantic schemas for authentication.

This module defines the request and response schemas for authentication operations.
"""

from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# ---------------------------------------------------------------------------
# Local provider (the default) — /auth/login, /auth/me, /auth/logout
# ---------------------------------------------------------------------------


RoleName = Literal["ADMIN", "DEVELOPER", "ANALYST", "VIEWER"]


class LoginRequest(BaseModel):
    """Body for ``POST /api/v1/auth/login`` (local provider)."""

    # Plain ``str`` (not EmailStr): a malformed address is simply an unknown
    # user (401), and local accounts such as ``admin@localhost`` stay usable.
    email: str = Field(..., min_length=1, max_length=320)
    password: str = Field(..., min_length=1, max_length=256)

    @field_validator("email", mode="before")
    @classmethod
    def _strip_email(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v


class UserMe(BaseModel):
    """
    The authenticated user's own profile, as returned by ``GET /auth/me`` and
    embedded in the login response.  ``role`` is the upper-case name of the
    ``UserRole`` enum (matches the frontend ``UserRole`` type).
    """

    id: UUID
    email: str
    username: str
    full_name: Optional[str] = None
    role: RoleName
    is_superuser: bool
    is_active: bool
    auth_provider: Literal["local", "cognito"]

    model_config = ConfigDict(from_attributes=True)

    @field_validator("role", mode="before")
    @classmethod
    def _role_to_name(cls, v: Any) -> Any:
        """Accept a ``UserRole`` enum member, its name, or its lower-case value."""
        if v is None:
            return "VIEWER"
        name = getattr(v, "name", None)
        if isinstance(name, str):
            return name.upper()
        if isinstance(v, str):
            return v.upper()
        return v


class LoginResponse(BaseModel):
    """Token response for the local provider."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(..., description="Seconds until the token expires")
    user: UserMe


class SignUpRequest(BaseModel):
    """Schema for user registration request."""

    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8, max_length=100)
    email: EmailStr
    given_name: str = Field(..., min_length=1, max_length=50)
    family_name: str = Field(..., min_length=1, max_length=50)

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        """Enforce password strength: must contain uppercase, lowercase, and digit."""
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


class SignUpResponse(BaseModel):
    """Schema for user registration response."""

    user_id: str
    confirmed: bool
    message: str


class ConfirmSignUpRequest(BaseModel):
    """Schema for confirming user registration."""

    username: str
    confirmation_code: str


class ConfirmSignUpResponse(BaseModel):
    """Schema for confirmation response."""

    message: str
    confirmed: bool


class TokenResponse(BaseModel):
    """Schema for authentication token response."""

    access_token: str
    id_token: str
    refresh_token: Optional[str] = None
    expires_in: int
    token_type: str


class ForgotPasswordRequest(BaseModel):
    """Schema for forgot password request."""

    username: str


class ForgotPasswordResponse(BaseModel):
    """Schema for forgot password response."""

    message: str


class ConfirmForgotPasswordRequest(BaseModel):
    """Schema for confirming forgot password."""

    username: str
    confirmation_code: str
    new_password: str = Field(..., min_length=8, max_length=100)

    @field_validator("new_password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        """Enforce password strength: must contain uppercase, lowercase, and digit."""
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


class ConfirmForgotPasswordResponse(BaseModel):
    """Schema for confirm forgot password response."""

    message: str


class RefreshTokenRequest(BaseModel):
    """Schema for refresh token request."""

    refresh_token: str


class UserInfoResponse(BaseModel):
    """Schema for user information response."""

    username: str
    attributes: Dict[str, Any]


# ---------------------------------------------------------------------------
# Dashboard sign-in configuration and the Cognito Hosted UI (#708)
# ---------------------------------------------------------------------------


class DemoAccount(BaseModel):
    """The shared account a demo visitor signs in with (``DEMO_MODE=true``)."""

    email: str = Field(..., description="The demo account's e-mail address")
    password: str = Field(..., description="The demo account's published password")


class AuthConfigResponse(BaseModel):
    """``GET /api/v1/auth/config``: how the dashboard signs in.

    ``{"provider": "local"}`` under the local provider. Under Cognito, either
    the Hosted UI's ``hosted_ui_domain``, the app ``client_id`` and the
    ``scopes`` to ask for, or ``configured: false`` with the names of the
    unset settings in ``missing``. With ``DEMO_MODE=true``, ``demo_mode:
    true`` as well, and ``demo_account`` when a demo account is configured.
    A field that does not apply is absent.
    """

    provider: Literal["local", "cognito"]
    configured: Optional[Literal[False]] = Field(
        default=None,
        description="Present, and false, when Cognito sign-in is not set up",
    )
    missing: Optional[List[str]] = Field(
        default=None,
        description="The names of the unset settings, when configured is false",
    )
    hosted_ui_domain: Optional[str] = Field(
        default=None,
        description="The Cognito Hosted UI's domain name, without a scheme",
    )
    client_id: Optional[str] = Field(
        default=None, description="The Cognito app client the dashboard signs in to"
    )
    scopes: Optional[List[str]] = Field(
        default=None, description="The OAuth scopes to ask the Hosted UI for"
    )
    demo_mode: Optional[Literal[True]] = Field(
        default=None,
        description=(
            "Present, and true, when this is a read-only demo install: every "
            "write but signing in and the SDK's tracking calls is refused"
        ),
    )
    demo_account: Optional[DemoAccount] = Field(
        default=None,
        description="The shared demo account, when demo_mode is true and one is set",
    )


class CognitoCodeExchangeRequest(BaseModel):
    """``POST /api/v1/auth/cognito/exchange``: what the Hosted UI's callback
    received, and the PKCE verifier the dashboard kept.

    Nothing else is read from the request: the domain asked and the app client
    named come from the configuration.
    """

    code: str = Field(..., min_length=1, max_length=2048)
    # RFC 7636 section 4.1: 43 to 128 unreserved characters.
    code_verifier: str = Field(
        ..., min_length=43, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$"
    )
    redirect_uri: str = Field(..., min_length=1, max_length=2048)


class CognitoCodeExchangeResponse(BaseModel):
    """The access token Cognito issued, and its lifetime. Nothing else: the id
    token and the refresh token are not returned."""

    access_token: str
    expires_in: int = Field(..., description="Seconds until the token expires")
