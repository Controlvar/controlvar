"""A moto user pool shaped like the reference one, and the documented onboarding.

``infrastructure/cdk/stacks/authentication_stack.py`` creates the reference
pool: administrator-created users only, email as a sign-in alias, email,
given name and family name required, and the password policy below.  moto
4.2.14 enforces none of the admin-only, required-attribute or alias rules
(the docs' steps for those are written from AWS's documentation), but the
shape is kept so a moto release that starts enforcing them is exercised.

:func:`run_documented_onboarding` makes the boto3 calls that the commands of
``docs/cognito_integration.md`` "Adding a user" make, read from the page by
``backend.tests.unit.docs.test_cognito_onboarding_docs.onboarding_commands``.
Call it only inside a moto fence.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.tests.unit.docs.test_cognito_onboarding_docs import (
    PASSWORD_PLACEHOLDER,
    POOL_PLACEHOLDER,
    onboarding_commands,
)

REGION = "us-east-1"
#: What the administrator types at the documented password prompt: it meets
#: the reference pool's policy.
TYPED_PASSWORD = "Choose-A-Password-1"


def create_reference_like_pool(idp: Any) -> Tuple[str, str]:
    """(user pool id, app client id) of a new pool shaped like the reference."""
    pool_id = idp.create_user_pool(
        PoolName="reference-like",
        AliasAttributes=["email"],
        Policies={
            "PasswordPolicy": {
                "MinimumLength": 8,
                "RequireUppercase": True,
                "RequireLowercase": True,
                "RequireNumbers": True,
                "RequireSymbols": True,
            }
        },
        AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
        Schema=[
            {
                "Name": name,
                "Required": True,
                "Mutable": True,
                "AttributeDataType": "String",
            }
            for name in ("email", "given_name", "family_name")
        ],
    )["UserPool"]["Id"]
    client_id = idp.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName="reference-like-client",
        GenerateSecret=False,
        ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
    )["UserPoolClient"]["ClientId"]
    return pool_id, client_id


def run_documented_onboarding(
    idp: Any,
    pool_id: str,
    edit: Optional[Callable[[List[Tuple[str, Dict[str, Any]]]], None]] = None,
) -> Dict[str, Any]:
    """Run the documented procedure against ``pool_id``.

    Returns the parameters of the ``admin_set_user_password`` step (the
    username and password the user then signs in with).  ``edit`` may change
    the calls before they run; the tamper tests use it.  ``$PASSWORD`` is what
    the administrator typed at the prompt, :data:`TYPED_PASSWORD`.
    """
    calls = onboarding_commands()
    if edit is not None:
        edit(calls)
    password_step: Dict[str, Any] = {}
    typed = {POOL_PLACEHOLDER: pool_id, PASSWORD_PLACEHOLDER: TYPED_PASSWORD}
    for operation, params in calls:
        concrete = {
            key: (typed[value] if isinstance(value, str) and value in typed else value)
            for key, value in params.items()
        }
        getattr(idp, operation)(**concrete)
        if operation == "admin_set_user_password":
            password_step = concrete
    assert password_step, "the procedure sets no password"
    return password_step
