"""Request and response bodies of two-step sign-in (#1812).

The routes are ``POST /api/v1/auth/mfa/verify`` (the code step of a sign-in),
``POST /api/v1/auth/mfa/enroll`` and ``/auth/mfa/enroll/confirm`` (setting it
up during a sign-in, for an account whose role requires it),
``/api/v1/users/me/2fa`` and its four actions (an account's own two-step
sign-in) and ``DELETE /api/v1/admin/users/{user_id}/2fa`` (a superuser's
reset), all for ``AUTH_PROVIDER=local`` only.

A ``code`` or ``recovery_code`` longer than ``RAW_INPUT_MAX`` (64) characters
answers 422 before anything else runs, a bcrypt check included. Where both are
accepted, exactly one must be sent (422 otherwise). Within the limit, the
value is checked as ``two_factor_service`` describes: spaces, tabs and hyphens
are dropped, a recovery code is upper-cased, and anything that is then not six
digits (a code) or ten of ``A-Z`` and ``2-7`` (a recovery code) is a wrong
code, counted like any other.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from backend.app.schemas.auth import LoginResponse
from backend.app.services.two_factor_service import RAW_INPUT_MAX

#: The longest mfa_token looked at. A real one is about 300 characters.
MFA_TOKEN_MAX = 2048

_CODE_DESCRIPTION = (
    "The 6-digit code the authenticator app shows. Spaces and hyphens are ignored."
)
_RECOVERY_DESCRIPTION = (
    "One of the account's recovery codes (10 characters, A-Z and 2-7). Spaces, "
    "hyphens and letter case are ignored. Each works once."
)


def _exactly_one(code: Optional[str], recovery_code: Optional[str]) -> None:
    if (code is None) == (recovery_code is None):
        raise ValueError("send exactly one of code and recovery_code")


class MfaRequiredResponse(BaseModel):
    """``POST /auth/login`` and ``/auth/token`` for an account with two-step
    sign-in on, or whose role requires it: the password was right, and a
    second step is needed. There is no ``access_token``. With ``enrol: false``
    send ``mfa_token`` with a code to ``POST /api/v1/auth/mfa/verify``; with
    ``enrol: true`` set two-step sign-in up first, with
    ``POST /api/v1/auth/mfa/enroll`` and ``/auth/mfa/enroll/confirm``."""

    mfa_required: Literal[True] = True
    mfa_token: str = Field(
        ...,
        description="With enrol false, send it to POST /api/v1/auth/mfa/verify "
        "with a code; with enrol true, to POST /api/v1/auth/mfa/enroll and "
        "/auth/mfa/enroll/confirm",
    )
    enrol: bool = Field(
        False,
        description="True when the account's role requires two-step sign-in "
        "(LOCAL_AUTH_2FA_REQUIRED_ROLES) and the account has not set it up: it "
        "sets it up before it is signed in. False when the account already has "
        "an authenticator app set up",
    )
    expires_in: int = Field(
        ...,
        description="Seconds until mfa_token expires: 300, or 600 with enrol true",
    )


class MfaVerifyRequest(BaseModel):
    """``POST /api/v1/auth/mfa/verify``: the token from the password step, and
    a code or a recovery code (exactly one)."""

    mfa_token: str = Field(..., min_length=1, max_length=MFA_TOKEN_MAX)
    code: Optional[str] = Field(
        None, max_length=RAW_INPUT_MAX, description=_CODE_DESCRIPTION
    )
    recovery_code: Optional[str] = Field(
        None, max_length=RAW_INPUT_MAX, description=_RECOVERY_DESCRIPTION
    )

    @model_validator(mode="after")
    def _one_of_the_two(self) -> "MfaVerifyRequest":
        _exactly_one(self.code, self.recovery_code)
        return self


class MfaEnrolRequest(BaseModel):
    """``POST /api/v1/auth/mfa/enroll``: the token of a password step that
    answered ``enrol: true``."""

    mfa_token: str = Field(..., min_length=1, max_length=MFA_TOKEN_MAX)


class MfaEnrolConfirmRequest(BaseModel):
    """``POST /api/v1/auth/mfa/enroll/confirm``: the same token, and the first
    code of the new secret."""

    mfa_token: str = Field(..., min_length=1, max_length=MFA_TOKEN_MAX)
    code: str = Field(..., max_length=RAW_INPUT_MAX, description=_CODE_DESCRIPTION)


class MfaEnrolConfirmResponse(BaseModel):
    """``POST /api/v1/auth/mfa/enroll/confirm``: two-step sign-in is on, the
    recovery codes are shown this once, and the account is signed in."""

    recovery_codes: List[str] = Field(
        ..., description="10 codes of 10 characters; each signs in once"
    )
    login: LoginResponse = Field(
        ...,
        description="What POST /api/v1/auth/login answers an account without "
        "two-step sign-in: the access token and the profile",
    )


class TwoFactorStatusResponse(BaseModel):
    """``GET /api/v1/users/me/2fa``."""

    enabled: bool = Field(..., description="Two-step sign-in is on")
    required: bool = Field(
        False,
        description="Your role must use two-step sign-in "
        "(LOCAL_AUTH_2FA_REQUIRED_ROLES), so it cannot be turned off. Always "
        "false for an account that signs in without a password",
    )
    recovery_codes_remaining: int = Field(
        ..., description="Unused recovery codes; 0 while two-step sign-in is off"
    )


class TwoFactorEnrolResponse(BaseModel):
    """``POST /api/v1/users/me/2fa/enroll``: a new secret for the
    authenticator app, waiting for its first code."""

    secret: str = Field(..., description="The secret in base32, to type in")
    otpauth_uri: str = Field(
        ..., description="The same secret as an otpauth:// link, for a QR code"
    )


class TwoFactorCodeRequest(BaseModel):
    """A code from the authenticator app (``confirm``, ``recovery-codes``)."""

    code: str = Field(..., max_length=RAW_INPUT_MAX, description=_CODE_DESCRIPTION)


class TwoFactorDisableRequest(BaseModel):
    """``POST /api/v1/users/me/2fa/disable``: a code or a recovery code
    (exactly one)."""

    code: Optional[str] = Field(
        None, max_length=RAW_INPUT_MAX, description=_CODE_DESCRIPTION
    )
    recovery_code: Optional[str] = Field(
        None, max_length=RAW_INPUT_MAX, description=_RECOVERY_DESCRIPTION
    )

    @model_validator(mode="after")
    def _one_of_the_two(self) -> "TwoFactorDisableRequest":
        _exactly_one(self.code, self.recovery_code)
        return self


class RecoveryCodesResponse(BaseModel):
    """A new set of recovery codes, shown this once; any earlier set stops
    working."""

    recovery_codes: List[str] = Field(
        ..., description="10 codes of 10 characters; each signs in once"
    )
