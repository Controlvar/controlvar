"""The reference Cognito user pool accepts administrator-created users only.

``self_sign_up_enabled=False`` in ``stacks/authentication_stack.py``
synthesises to ``AdminCreateUserConfig.AllowAdminCreateUserOnly: true`` on the
``AWS::Cognito::UserPool``. Asserted on a real ``app.synth()`` of each
environment, because the template property is what Cognito acts on, not the
construct argument.
"""

from __future__ import annotations

import pytest

from .test_dashboard_service import _synth


@pytest.mark.parametrize("environment", ["dev", "staging", "prod"])
def test_the_user_pool_allows_admin_create_user_only(environment: str):
    assembly = _synth(environment)
    (stack,) = [
        s
        for s in assembly.stacks
        if s.stack_name == f"experimentation-auth-{environment}"
    ]
    pools = [
        r["Properties"]
        for r in stack.template["Resources"].values()
        if r["Type"] == "AWS::Cognito::UserPool"
    ]
    assert len(pools) == 1, f"{environment}: {len(pools)} user pools"
    config = pools[0].get("AdminCreateUserConfig", {})
    assert config.get("AllowAdminCreateUserOnly") is True, (
        f"{environment}: the user pool lets users register themselves; "
        f"AdminCreateUserConfig is {config!r}"
    )


@pytest.mark.regression
def test_a_user_signs_in_with_a_username_or_an_email_address_and_has_one():
    """The Cognito page: the reference pool lets people sign in with their
    email address as well as their username (so Cognito refuses a username in
    email form), and requires an email address."""
    assembly = _synth("dev")
    (stack,) = [
        s for s in assembly.stacks if s.stack_name == "experimentation-auth-dev"
    ]
    (pool,) = [
        r["Properties"]
        for r in stack.template["Resources"].values()
        if r["Type"] == "AWS::Cognito::UserPool"
    ]
    assert pool.get("AliasAttributes") == ["email"], pool.get("AliasAttributes")
    assert "UsernameAttributes" not in pool
    (email,) = [a for a in pool["Schema"] if a["Name"] == "email"]
    assert email["Required"] is True
