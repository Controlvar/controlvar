"""The shape ``CognitoAuthService.get_user_with_groups`` returns.

Not a test module. Tests that stub the Cognito boundary build their payload
with :func:`identity`; ``backend/tests/integration/auth/test_cognito_identity_shape.py``
checks it against what the real service returns through moto, so a stub
cannot drift from the real shape (a top-level ``sub``, an ``email`` of ``""``
where Cognito leaves the attribute out, groups under another key).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def identity(
    username: str,
    sub: Optional[str],
    email: Optional[str] = None,
    groups: Optional[List[str]] = None,
    *,
    email_verified: Optional[str] = "true",
    **extra_attributes: str,
) -> Dict[str, Any]:
    """What ``CognitoAuthService.get_user_with_groups`` returns for a user.

    ``sub`` and ``email`` are left out of ``attributes`` when ``None``, as
    Cognito leaves out an attribute the user does not have. A user with an
    email also has ``email_verified``, ``"true"`` unless given (the user
    "Adding a user" in ``docs/cognito_integration.md`` creates); ``None``
    leaves it out.
    """
    attributes: Dict[str, str] = dict(extra_attributes)
    if sub is not None:
        attributes["sub"] = sub
    if email is not None:
        attributes["email"] = email
        if email_verified is not None:
            attributes["email_verified"] = email_verified
    return {"username": username, "attributes": attributes, "groups": groups or []}
