"""The reference Cognito user pool and app client, as synthesised.

``stacks/authentication_stack.py``, on a real synth of each environment:

* the pool requires ``given_name`` and ``family_name`` (and ``email``);
* its password policy: at least 8 characters, with upper case, lower case,
  a digit and a symbol;
* it creates no group: the role groups are created by hand;
* the app client sets no token lifetime, so its access tokens last the
  60 minutes Cognito gives a client that sets none.
"""

from __future__ import annotations

import pytest

from .test_dashboard_service import _synth

ENVIRONMENTS = ("dev", "staging", "prod", "demo")
LIFETIMES = (
    "AccessTokenValidity",
    "IdTokenValidity",
    "RefreshTokenValidity",
    "TokenValidityUnits",
)


@pytest.fixture(scope="module", params=ENVIRONMENTS)
def auth(request) -> dict:
    environment = request.param
    (stack,) = [
        s
        for s in _synth(environment).stacks
        if s.stack_name == f"experimentation-auth-{environment}"
    ]
    return stack.template["Resources"]


def _one(resources: dict, kind: str) -> dict:
    (found,) = [r["Properties"] for r in resources.values() if r["Type"] == kind]
    return found


@pytest.mark.regression
def test_the_pool_requires_a_given_and_a_family_name(auth):
    pool = _one(auth, "AWS::Cognito::UserPool")
    required = sorted(a["Name"] for a in pool["Schema"] if a.get("Required"))
    assert required == ["email", "family_name", "given_name"], required


@pytest.mark.regression
def test_the_password_policy(auth):
    policy = _one(auth, "AWS::Cognito::UserPool")["Policies"]["PasswordPolicy"]
    assert policy["MinimumLength"] == 8
    for rule in (
        "RequireUppercase",
        "RequireLowercase",
        "RequireNumbers",
        "RequireSymbols",
    ):
        assert policy[rule] is True, rule


@pytest.mark.regression
def test_the_pool_creates_no_group(auth):
    kinds = {r["Type"] for r in auth.values()}
    assert "AWS::Cognito::UserPool" in kinds
    assert not {
        k
        for k in kinds
        if k
        in (
            "AWS::Cognito::UserPoolGroup",
            "AWS::Cognito::UserPoolUserToGroupAttachment",
        )
    }, kinds


@pytest.mark.regression
def test_the_app_client_sets_no_token_lifetime(auth):
    client = _one(auth, "AWS::Cognito::UserPoolClient")
    assert client["ClientName"].startswith("experimentation-platform-client-")
    for key in LIFETIMES:
        assert key not in client, (key, client.get(key))
