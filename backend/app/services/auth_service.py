"""
AWS Cognito authentication service.

This module provides a service for handling authentication operations using AWS Cognito.
"""

import logging
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

import boto3
import jwt
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger(__name__)

# The record of a refused Cognito sign-in. A stdlib logger (not loguru) so the
# refusal reason is a field on the record that operators and tests can read.
sign_in_logger = logging.getLogger("backend.app.auth.cognito_sign_in")

# A user pool ID is "<region>_<id>", for example "us-west-2_AbC123xyZ"; AWS caps
# it at 55 characters. The region is the part before the underscore.
_USER_POOL_ID_MAX_LENGTH = 55
_USER_POOL_ID = re.compile(r"([a-z]{2}(?:-[a-z]+){1,3}-[0-9]{1,2})_([0-9A-Za-z]{1,50})")

REASON_NOT_CONFIGURED = "not_configured"
REASON_WRONG_ISSUER = "wrong_issuer"


class CognitoTokenRefused(Exception):
    """An access token Cognito accepted, refused because it was not issued to
    this deployment's user pool and app client.

    ``reason`` is ``not_configured`` (``COGNITO_USER_POOL_ID`` or
    ``COGNITO_CLIENT_ID`` unset) or ``wrong_issuer`` (the pool ID is malformed,
    or the token's ``iss``, ``token_use`` or ``client_id`` does not match).
    Deliberately not a ``ValueError``: callers answer a refusal with the generic
    401 and log the reason, rather than returning the exception's text.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# The two answers to a sign-in Cognito met with a challenge instead of tokens.
NEW_PASSWORD_REQUIRED_DETAIL = (
    "This user must set a new password before signing in; an administrator "
    "sets one with admin-set-user-password --permanent."
)
UNSUPPORTED_CHALLENGE_DETAIL = (
    "This sign-in needs a step this API does not support ({challenge})."
)


class CognitoSignInChallenge(ValueError):
    """A sign-in Cognito answered with a challenge, or without an access token.

    ``challenge`` is the response's ``ChallengeName`` (one of Cognito's fixed
    set, for example ``NEW_PASSWORD_REQUIRED`` or ``SOFTWARE_TOKEN_MFA``), or
    ``"none"`` when the response carries neither a challenge nor an access
    token. A ``ValueError`` so ``POST /auth/token`` answers it with 401 and the
    message, which names the challenge and never the challenge's ``Session``.
    """

    def __init__(self, challenge: str) -> None:
        if challenge == "NEW_PASSWORD_REQUIRED":
            message = NEW_PASSWORD_REQUIRED_DETAIL
        else:
            message = UNSUPPORTED_CHALLENGE_DETAIL.format(challenge=challenge)
        super().__init__(message)
        self.challenge = challenge


def log_token_refused(refusal: CognitoTokenRefused, path: str) -> None:
    """Record a refused sign-in on the ``backend.app.auth.cognito_sign_in`` logger."""
    sign_in_logger.warning(
        "Cognito sign-in refused (%s) on %s",
        refusal.reason,
        path,
        extra={"reason": refusal.reason},
    )


def expected_issuer(user_pool_id: Optional[str]) -> Optional[str]:
    """The ``iss`` claim Cognito puts in tokens from ``user_pool_id``, or
    ``None`` when the pool ID is not of the form ``<region>_<id>``."""
    if not user_pool_id or len(user_pool_id) > _USER_POOL_ID_MAX_LENGTH:
        return None
    match = _USER_POOL_ID.fullmatch(user_pool_id)
    if match is None:
        return None
    return f"https://cognito-idp.{match.group(1)}.amazonaws.com/{user_pool_id}"


def validate_access_token_claims(
    claims: Mapping[str, Any],
    user_pool_id: Optional[str],
    client_id: Optional[str],
) -> None:
    """Require the token to be an access token from ``user_pool_id`` issued to
    ``client_id``; raise :class:`CognitoTokenRefused` otherwise."""
    if not user_pool_id or not client_id:
        raise CognitoTokenRefused(REASON_NOT_CONFIGURED)
    issuer = expected_issuer(user_pool_id)
    if (
        issuer is None
        or claims.get("iss") != issuer
        or claims.get("token_use") != "access"
        or claims.get("client_id") != client_id
    ):
        raise CognitoTokenRefused(REASON_WRONG_ISSUER)


def groups_from_claims(claims: Mapping[str, Any]) -> list:
    """The group names in a verified access token's ``cognito:groups`` claim.

    Absent (the user is in no group) or not a list of names: no groups.
    """
    value = claims.get("cognito:groups")
    if not isinstance(value, list):
        return []
    return [name for name in value if isinstance(name, str)]


def configured_pool_and_client() -> Tuple[Optional[str], Optional[str]]:
    """``COGNITO_USER_POOL_ID`` and ``COGNITO_CLIENT_ID``, from the process
    environment, where :class:`CognitoAuthService` reads them."""
    return os.environ.get("COGNITO_USER_POOL_ID"), os.environ.get("COGNITO_CLIENT_ID")


def unset_hosted_ui_settings(cognito_domain: Optional[str]) -> List[str]:
    """The settings sign-in through the Hosted UI needs that are unset, by
    name, in a fixed order: ``COGNITO_DOMAIN`` (given, from the settings),
    ``COGNITO_CLIENT_ID``, ``COGNITO_USER_POOL_ID``. Names only, never a value."""
    user_pool_id, client_id = configured_pool_and_client()
    values = (
        ("COGNITO_DOMAIN", cognito_domain),
        ("COGNITO_CLIENT_ID", client_id),
        ("COGNITO_USER_POOL_ID", user_pool_id),
    )
    return [name for name, value in values if not value]


class CognitoAuthService:
    """Service for AWS Cognito authentication operations."""

    def __init__(self):
        # Load configuration from environment variables
        self.user_pool_id, self.client_id = configured_pool_and_client()
        self.region = os.environ.get("AWS_REGION", "us-east-1")

        if not self.user_pool_id or not self.client_id:
            logger.warning("COGNITO_USER_POOL_ID or COGNITO_CLIENT_ID not set")

        self._client = None

    @property
    def client(self) -> Any:
        """The Cognito Identity Provider client, created on first use.

        Not in ``__init__``: a module-level instance of this class is built at
        import, and ``boto3.client`` validates the region then -- with
        ``AWS_REGION=''`` it raises "Invalid endpoint" -- which made importing
        ``backend.app.api.deps`` (and everything that imports the API, the
        modules' registration included) fail on a configuration detail
        that only matters once Cognito is actually called.
        """
        if self._client is None:
            self._client = boto3.client("cognito-idp", region_name=self.region)
        return self._client

    @client.setter
    def client(self, value: Any) -> None:  # tests install a fake client
        self._client = value

    def sign_up(
        self,
        username: str,
        password: str,
        email: str,
        given_name: str,
        family_name: str,
    ) -> Dict[str, Any]:
        """Register a new user in the Cognito User Pool."""
        try:
            # Format user attributes for Cognito
            user_attributes = [
                {"Name": "email", "Value": email},
                {"Name": "given_name", "Value": given_name},
                {"Name": "family_name", "Value": family_name},
            ]

            response = self.client.sign_up(
                ClientId=self.client_id,
                Username=username,
                Password=password,
                UserAttributes=user_attributes,
            )

            logger.info(f"User registered successfully: {username}")
            return {
                "user_id": response["UserSub"],
                "confirmed": False,
                "message": "User registration successful. Please check "
                "your email for verification code.",
            }

        except ClientError as e:
            logger.error(f"Sign-up error: {e!s}")
            raise ValueError(str(e))
        except Exception as e:
            logger.error(f"Unexpected error during sign-up: {e!s}")
            # Pass through the original error message
            raise ValueError(str(e))

    def confirm_sign_up(self, username: str, confirmation_code: str) -> Dict[str, Any]:
        """Confirm user registration with the code sent to their email."""
        try:
            self.client.confirm_sign_up(
                ClientId=self.client_id,
                Username=username,
                ConfirmationCode=confirmation_code,
            )

            logger.info(f"User confirmed successfully: {username}")
            return {
                "message": "Account confirmed successfully. You can now sign in.",
                "confirmed": True,
            }

        except ClientError as e:
            logger.error(f"Confirmation error: {e!s}")
            raise ValueError(str(e))
        except Exception as e:
            logger.error(f"Unexpected error during confirmation: {e!s}")
            # Pass through the original error message
            raise ValueError(str(e))

    def sign_in(self, username: str, password: str) -> Dict[str, Any]:
        """Authenticate a user and get tokens.

        Raises :class:`CognitoSignInChallenge` when Cognito answers with a
        challenge (``NEW_PASSWORD_REQUIRED``, an MFA step, ...) or with no
        access token, and ``ValueError`` when Cognito refuses the credentials.
        """
        try:
            response = self.client.initiate_auth(
                ClientId=self.client_id,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={
                    "USERNAME": username,
                    "PASSWORD": password,
                },
            )
        except ClientError as e:
            logger.error(f"Sign-in error: {e!s}")
            raise ValueError(str(e))
        except Exception as e:
            logger.error(f"Unexpected error during sign-in: {e!s}")
            # Pass through the original error message
            raise ValueError(str(e))

        # Checked outside the try above, so a challenge is not reported as an
        # unexpected error. The challenge's Session is never logged or returned.
        challenge = response.get("ChallengeName")
        auth_result = response.get("AuthenticationResult") or {}
        if challenge or not auth_result.get("AccessToken"):
            refusal = CognitoSignInChallenge(str(challenge) if challenge else "none")
            logger.warning(
                f"Sign-in for user {username} stopped at Cognito challenge "
                f"{refusal.challenge}"
            )
            raise refusal

        logger.info(f"Sign-in successful for user: {username}")

        return {
            "access_token": auth_result.get("AccessToken"),
            "id_token": auth_result.get("IdToken"),
            "refresh_token": auth_result.get("RefreshToken"),
            "expires_in": auth_result.get("ExpiresIn", 3600),
            "token_type": auth_result.get("TokenType", "Bearer"),
        }

    def forgot_password(self, username: str) -> Dict[str, Any]:
        """Initiate the forgot password flow."""
        try:
            self.client.forgot_password(ClientId=self.client_id, Username=username)

            logger.info(f"Forgot password flow initiated for user: {username}")
            return {"message": "Password reset code has been sent to your email."}

        except ClientError as e:
            logger.error(f"Forgot password error: {e!s}")
            raise ValueError(str(e))
        except Exception as e:
            logger.error(f"Unexpected error during forgot password: {e!s}")
            # Pass through the original error message
            raise ValueError(str(e))

    def confirm_forgot_password(
        self, username: str, confirmation_code: str, new_password: str
    ) -> Dict[str, Any]:
        """Complete the forgot password flow by setting a new password."""
        try:
            self.client.confirm_forgot_password(
                ClientId=self.client_id,
                Username=username,
                ConfirmationCode=confirmation_code,
                Password=new_password,
            )

            logger.info(f"Password reset successful for user: {username}")
            return {
                "message": "Password has been reset successfully. You can now sign in."
            }

        except ClientError as e:
            logger.error(f"Confirm forgot password error: {e!s}")
            raise ValueError(str(e))
        except Exception as e:
            logger.error(f"Unexpected error during password reset: {e!s}")
            # Pass through the original error message
            raise ValueError(str(e))

    def refresh_token(self, refresh_token: str) -> Dict[str, Any]:
        """Refresh the authentication tokens using a refresh token."""
        try:
            response = self.client.initiate_auth(
                ClientId=self.client_id,
                AuthFlow="REFRESH_TOKEN_AUTH",
                AuthParameters={
                    "REFRESH_TOKEN": refresh_token,
                },
            )

            auth_result = response.get("AuthenticationResult", {})

            logger.info("Token refreshed successfully")

            return {
                "access_token": auth_result.get("AccessToken"),
                "id_token": auth_result.get("IdToken"),
                "expires_in": auth_result.get("ExpiresIn", 3600),
                "token_type": auth_result.get("TokenType", "Bearer"),
            }

        except ClientError as e:
            logger.error(f"Token refresh error: {e!s}")
            raise ValueError(str(e))
        except Exception as e:
            logger.error(f"Unexpected error during token refresh: {e!s}")
            raise ValueError("An unexpected error occurred during token refresh")

    def _check_token_audience(self, access_token: str) -> Dict[str, Any]:
        """Refuse a token not issued to this deployment's pool and app client,
        and return its claims.

        Called only after ``GetUser`` has accepted the token, so the token is
        authentic and unexpired and its claims can be read without verifying
        the signature again.
        """
        if not self.user_pool_id or not self.client_id:
            raise CognitoTokenRefused(REASON_NOT_CONFIGURED)
        try:
            # GetUser has already accepted this access token; only its iss,
            # token_use and client_id claims are compared with configuration.
            claims = jwt.decode(access_token, options={"verify_signature": False})  # nosemgrep: python.jwt.security.unverified-jwt-decode.unverified-jwt-decode  # fmt: skip
        except jwt.PyJWTError:
            raise CognitoTokenRefused(REASON_WRONG_ISSUER) from None
        validate_access_token_claims(claims, self.user_pool_id, self.client_id)
        return claims

    def get_user(self, access_token: str) -> Dict[str, Any]:
        """Get user details from the access token.

        Raises :class:`CognitoTokenRefused` when the token was not issued to
        this deployment's user pool and app client, and ``ValueError`` when
        Cognito does not accept it.
        """
        try:
            response = self.client.get_user(AccessToken=access_token)
            self._check_token_audience(access_token)

            # Extract user attributes
            user_attributes = {
                attr["Name"]: attr["Value"]
                for attr in response.get("UserAttributes", [])
            }

            logger.info(
                f"User details retrieved for username: {response.get('Username')}"
            )

            return {"username": response.get("Username"), "attributes": user_attributes}

        except CognitoTokenRefused:
            raise
        except ClientError as e:
            logger.error(f"Get user error: {e!s}")
            raise ValueError(str(e))
        except Exception as e:
            logger.error(f"Unexpected error getting user details: {e!s}")
            raise ValueError("An unexpected error occurred retrieving user details")

    def global_sign_out(self, access_token: str) -> None:
        """Sign the token's user out of Cognito everywhere (GlobalSignOut).

        Cognito then refuses that user's access tokens on every token-authorised
        call, GetUser included, so each request with one is refused, and their
        refresh tokens stop working. Every other session of the user ends too.
        The call is authorised by the access token itself and needs no AWS
        credentials. Raises whatever the client raises; the caller decides
        what a failure means.
        """
        self.client.global_sign_out(AccessToken=access_token)

    def get_user_with_groups(self, access_token: str) -> Dict[str, Any]:
        """
        Get user details and group membership from access token.

        The groups are the token's ``cognito:groups`` claim, read once the
        token has passed the issuer and app-client check. An access token
        carries the claim only when the user is in at least one group, so an
        absent claim means no groups. A change of group therefore takes
        effect with the user's next access token.

        Args:
            access_token: JWT access token from Cognito

        Returns:
            Dict with user info and groups

        Raises:
            CognitoTokenRefused: the token was not issued to this deployment's
                user pool and app client.
            ValueError: Cognito did not accept the token.
        """
        try:
            # Get basic user details
            response = self.client.get_user(AccessToken=access_token)
            claims = self._check_token_audience(access_token)

            # Extract user attributes
            user_attributes = {
                attr["Name"]: attr["Value"]
                for attr in response.get("UserAttributes", [])
            }
            username = response.get("Username")
            groups = groups_from_claims(claims)

            logger.info(
                f"User details with groups retrieved for username: {username}, groups: {groups}"
            )

            return {
                "username": username,
                "attributes": user_attributes,
                "groups": groups,
            }
        except CognitoTokenRefused:
            raise
        except ClientError as e:
            logger.error(f"Get user with groups error: {e!s}")
            raise ValueError(str(e))
        except Exception as e:
            logger.error(f"Unexpected error getting user details with groups: {e!s}")
            raise ValueError("An unexpected error occurred retrieving user details")


# Create a single instance of the service
auth_service = CognitoAuthService()
