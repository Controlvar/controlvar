"""Dashboard sign-in through the Cognito Hosted UI, as synthesised (#708).

One CDK context key turns it on, ``cognito_hosted_ui``, and it is off unless
it is set (``-c cognito_hosted_ui=true``). Asserted on a real ``app.synth()``:

* **Off** (the key unset, ``false`` or ``"false"``) the auth stack is what it
  was before the key existed: no Hosted UI domain; the app client's scopes,
  callback and sign-out URLs as they were; no ``PreventUserExistenceErrors``,
  no ``GenerateSecret``; no output or export but the pool's two. The API task
  is given no Cognito setting and imports nothing from the auth stack.
* **On** the pool gains a Hosted UI domain, managed login version 1, named for
  its environment. The client adds the ``aws.cognito.signin.user.admin``
  scope (GetUser requires it, and every request runs GetUser), the
  dashboard's callback and sign-out URLs on ``PUBLIC_BASE_URL``, and
  ``PreventUserExistenceErrors``, and still has no secret. The pool is
  unchanged and the client keeps its logical id, so nothing is replaced.
* **On** the API task is given ``AUTH_PROVIDER=cognito``, the pool, the
  client and the Hosted UI's domain name by reference to the auth stack, and
  ``AWS_REGION``.

The off case is checked by structure -- which resources, which properties --
rather than against a stored copy of the template, so an aws-cdk-lib upgrade
that renders the same resources differently does not turn it red.
"""

from __future__ import annotations

import json
from collections import Counter

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from .test_app_profiles import CDK_DIR, FAKE_REGION, _app_environment
from .test_dashboard_service import _synth

ENVIRONMENTS = ("dev", "staging", "prod", "demo")

#: The context key, spelled out rather than imported, so these tests also run
#: against a stack that does not know it (the off case must not depend on it).
KEY = "cognito_hosted_ui"
ON = {KEY: "true"}

#: Every way to leave it off: the key absent, and the two spellings of false.
OFF = {"unset": {}, "false": {KEY: "false"}, "False": {KEY: False}}

#: What the app client says when the key is off, as it did before the key.
OFF_SCOPES = ["email", "openid", "profile"]
OFF_CALLBACK_URLS = ["https://example.com/callback"]
OFF_LOGOUT_URLS = ["https://example.com/logout"]

#: The app client's property names when the key is off, as on main: names
#: only, so a library upgrade that renders a value differently is not a
#: failure, while any property added to the client (GenerateSecret above all,
#: whose change replaces the client) is.
OFF_CLIENT_PROPERTIES = {
    "AllowedOAuthFlows",
    "AllowedOAuthFlowsUserPoolClient",
    "AllowedOAuthScopes",
    "CallbackURLs",
    "ClientName",
    "ExplicitAuthFlows",
    "LogoutURLs",
    "SupportedIdentityProviders",
    "UserPoolId",
}

#: The settings the API task is given when the key is on.
API_SETTINGS = (
    "AUTH_PROVIDER",
    "COGNITO_USER_POOL_ID",
    "COGNITO_CLIENT_ID",
    "COGNITO_DOMAIN",
    "AWS_REGION",
)

POOL = "AWS::Cognito::UserPool"
CLIENT = "AWS::Cognito::UserPoolClient"
DOMAIN = "AWS::Cognito::UserPoolDomain"

_CACHE: dict = {}


def templates(environment: str, context: dict) -> dict[str, dict]:
    """Every stack's template, by stack name, for one synth (cached)."""
    key = (environment, json.dumps(context, sort_keys=True))
    if key not in _CACHE:
        assembly = _synth(environment, context)
        _CACHE[key] = {s.stack_name: s.template for s in assembly.stacks}
    return _CACHE[key]


def auth(environment: str, context: dict) -> dict:
    return templates(environment, context)[f"experimentation-auth-{environment}"]


def fargate(environment: str, context: dict) -> dict:
    return templates(environment, context)[f"experimentation-fargate-{environment}"]


def one(template: dict, rtype: str) -> tuple[str, dict]:
    """``(logical id, properties)`` of the one resource of ``rtype``."""
    found = [
        (lid, r.get("Properties", {}))
        for lid, r in template["Resources"].items()
        if r["Type"] == rtype
    ]
    assert len(found) == 1, f"expected one {rtype}, found {[lid for lid, _ in found]}"
    return found[0]


def api_environment(environment: str, context: dict) -> dict:
    """The API container's environment, name -> value as synthesised."""
    resources = fargate(environment, context)["Resources"]
    (task,) = [
        r
        for r in resources.values()
        if r["Type"] == "AWS::ECS::TaskDefinition"
        and r["Properties"].get("Family") == f"experimentation-backend-{environment}"
    ]
    (container,) = [
        c for c in task["Properties"]["ContainerDefinitions"] if c["Name"] == "backend"
    ]
    return {e["Name"]: e["Value"] for e in container.get("Environment", [])}


def export_value(environment: str, context: dict, imported: dict) -> object:
    """What an ``{"Fn::ImportValue": name}`` reads: the auth stack output
    exporting ``name``."""
    assert set(imported) == {"Fn::ImportValue"}, imported
    name = imported["Fn::ImportValue"]
    outputs = auth(environment, context).get("Outputs", {})
    (value,) = [
        o["Value"]
        for o in outputs.values()
        if (o.get("Export") or {}).get("Name") == name
    ]
    return value


# --- E1: off is what it was --------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
@pytest.mark.parametrize("off", sorted(OFF))
def test_off_the_auth_stack_has_no_hosted_ui(environment, off):
    template = auth(environment, OFF[off])
    kinds = Counter(r["Type"] for r in template["Resources"].values())
    assert kinds[DOMAIN] == 0, f"{environment} ({off}): a Hosted UI domain"
    assert (kinds[POOL], kinds[CLIENT]) == (1, 1), kinds
    _, client = one(template, CLIENT)
    assert "GenerateSecret" not in client, (
        f"the app client declares GenerateSecret: {client['GenerateSecret']!r}; "
        "CloudFormation replaces the client when that property changes, and "
        "this template has never had it"
    )
    assert "PreventUserExistenceErrors" not in client, (
        f"PreventUserExistenceErrors: {client['PreventUserExistenceErrors']!r}"
    )
    assert sorted(client["AllowedOAuthScopes"]) == OFF_SCOPES
    assert client["CallbackURLs"] == OFF_CALLBACK_URLS
    assert client["LogoutURLs"] == OFF_LOGOUT_URLS
    assert set(client) == OFF_CLIENT_PROPERTIES, (
        f"added {sorted(set(client) - OFF_CLIENT_PROPERTIES)}, "
        f"removed {sorted(OFF_CLIENT_PROPERTIES - set(client))}"
    )
    outputs = template.get("Outputs", {})
    assert set(outputs) == {"UserPoolId", "UserPoolArn"}, sorted(outputs)


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
@pytest.mark.parametrize("off", sorted(OFF))
def test_off_the_api_task_is_given_no_cognito_setting(environment, off):
    given = sorted(set(API_SETTINGS) & set(api_environment(environment, OFF[off])))
    assert given == [], f"{environment} ({off}): the API task is given {given}"
    imports = json.dumps(fargate(environment, OFF[off])).count(
        f'"Fn::ImportValue": "experimentation-auth-{environment}:'
    )
    assert imports == 0, f"{environment} ({off}): the API imports the auth stack"


@pytest.mark.parametrize("off", ["false", "False"])
def test_false_synthesises_exactly_what_unset_does(off):
    assert json.dumps(templates("dev", OFF[off]), sort_keys=True) == json.dumps(
        templates("dev", OFF["unset"]), sort_keys=True
    )


# --- E2: on ----------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_on_the_pool_gets_a_hosted_ui_domain_named_for_its_environment(environment):
    template = auth(environment, ON)
    pool_id, _ = one(template, POOL)
    _, domain = one(template, DOMAIN)
    assert domain == {
        "Domain": f"controlvar-{environment}",
        "ManagedLoginVersion": 1,
        "UserPoolId": {"Ref": pool_id},
    }


def test_on_each_environment_has_its_own_domain_prefix():
    prefixes = [one(auth(env, ON), DOMAIN)[1]["Domain"] for env in ENVIRONMENTS]
    assert len(set(prefixes)) == len(ENVIRONMENTS), prefixes


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_on_the_client_signs_in_through_the_dashboard_on_the_public_url(environment):
    public = api_environment(environment, ON)["PUBLIC_BASE_URL"]
    template = auth(environment, ON)
    _, client = one(template, CLIENT)
    assert client["CallbackURLs"] == [f"{public}/auth/callback"]
    assert client["LogoutURLs"] == [f"{public}/login"]
    assert sorted(client["AllowedOAuthScopes"]) == sorted(
        OFF_SCOPES + ["aws.cognito.signin.user.admin"]
    )
    assert client["AllowedOAuthFlows"] == ["code"]
    assert client["SupportedIdentityProviders"] == ["COGNITO"]
    assert client["PreventUserExistenceErrors"] == "ENABLED"
    assert "GenerateSecret" not in client, (
        f"the app client declares GenerateSecret: {client['GenerateSecret']!r}; "
        "CloudFormation replaces the client when that property changes, and "
        "this template has never had it"
    )
    text = json.dumps(template)
    for url in OFF_CALLBACK_URLS + OFF_LOGOUT_URLS:
        assert url not in text, url


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_on_only_the_client_changes_and_a_domain_is_added(environment):
    """No Replace: the pool is untouched, the client keeps its logical id and
    changes only properties CloudFormation updates in place, and nothing is
    removed."""
    off = auth(environment, OFF["unset"])["Resources"]
    on = auth(environment, ON)["Resources"]
    assert off.keys() <= on.keys(), sorted(off.keys() - on.keys())
    added = {on[lid]["Type"] for lid in on.keys() - off.keys()}
    assert added == {DOMAIN}, added
    pool_id, _ = one({"Resources": off}, POOL)
    assert on[pool_id] == off[pool_id]
    client_id, _ = one({"Resources": off}, CLIENT)
    before, after = off[client_id]["Properties"], on[client_id]["Properties"]
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    assert changed == {
        "AllowedOAuthScopes",
        "CallbackURLs",
        "LogoutURLs",
        "PreventUserExistenceErrors",
    }, changed
    for lid in off.keys() - {client_id}:
        assert on[lid] == off[lid], lid


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_on_the_outputs_name_the_domain_and_the_two_urls(environment):
    public = api_environment(environment, ON)["PUBLIC_BASE_URL"]
    template = auth(environment, ON)
    domain_id, _ = one(template, DOMAIN)
    outputs = template["Outputs"]
    assert outputs["HostedUiDomain"]["Value"] == {
        "Fn::Join": ["", [{"Ref": domain_id}, f".auth.{FAKE_REGION}.amazoncognito.com"]]
    }
    assert outputs["HostedUiCallbackUrl"]["Value"] == f"{public}/auth/callback"
    assert outputs["HostedUiLogoutUrl"]["Value"] == f"{public}/login"
    for name in ("HostedUiDomain", "HostedUiCallbackUrl", "HostedUiLogoutUrl"):
        assert "Export" not in outputs[name], name


# --- E3: sign-up stays off and there is no secret -----------------------------


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_on_sign_up_stays_off_and_the_client_has_no_secret(environment):
    template = auth(environment, ON)
    _, pool = one(template, POOL)
    config = pool.get("AdminCreateUserConfig", {})
    assert config.get("AllowAdminCreateUserOnly") is True, (
        f"{environment}: with the Hosted UI on, users can register themselves; "
        f"AdminCreateUserConfig is {config!r}"
    )
    _, client = one(template, CLIENT)
    assert "GenerateSecret" not in client, (
        f"the app client declares GenerateSecret: {client['GenerateSecret']!r}; "
        "CloudFormation replaces the client when that property changes, and "
        "this template has never had it"
    )


# --- E4: the API task is told ---------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_on_the_api_task_is_given_the_cognito_settings_by_reference(environment):
    template = auth(environment, ON)
    pool_id, _ = one(template, POOL)
    client_id, _ = one(template, CLIENT)
    domain_id, _ = one(template, DOMAIN)
    env = api_environment(environment, ON)
    missing = [name for name in API_SETTINGS if name not in env]
    assert not missing, f"{environment}: the API task is not given {missing}"
    assert env["AUTH_PROVIDER"] == "cognito"
    assert env["AWS_REGION"] == FAKE_REGION
    assert export_value(environment, ON, env["COGNITO_USER_POOL_ID"]) == {
        "Ref": pool_id
    }
    assert export_value(environment, ON, env["COGNITO_CLIENT_ID"]) == {"Ref": client_id}
    join = env["COGNITO_DOMAIN"]
    assert set(join) == {"Fn::Join"}, join
    separator, (prefix, suffix) = join["Fn::Join"]
    assert separator == ""
    assert export_value(environment, ON, prefix) == {"Ref": domain_id}
    assert suffix == f".auth.{FAKE_REGION}.amazoncognito.com"


# --- the key and the names it takes --------------------------------------------


def _auth_stack(context: dict, public_base_url="https://app.example.org", env="dev"):
    """The auth stack alone, built in an app with ``context``."""
    with _app_environment(CDK_DIR):
        from stacks.authentication_stack import AuthenticationStack

        app = cdk.App(context=context)
        return AuthenticationStack(
            app, f"auth-{env}", environment=env, public_base_url=public_base_url
        )


@pytest.mark.parametrize("value", ["yes", "1", "", "TRUE", "on", 1])
def test_a_value_other_than_true_or_false_is_refused(value):
    with pytest.raises(ValueError, match="cognito_hosted_ui"):
        _auth_stack({KEY: value})


@pytest.mark.parametrize("value", [True, "true"])
def test_true_in_either_spelling_turns_it_on(value):
    stack = _auth_stack({KEY: value})
    assert stack.hosted_ui is not None


@pytest.mark.parametrize("url", [None, "", "http://app.example.org"])
def test_on_without_an_https_public_url_is_refused(url):
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        _auth_stack(ON, public_base_url=url)


def test_a_trailing_slash_on_the_public_url_is_not_doubled():
    stack = _auth_stack(ON, public_base_url="https://app.example.org/")
    assert stack.hosted_ui.callback_url == "https://app.example.org/auth/callback"
    assert stack.hosted_ui.logout_url == "https://app.example.org/login"


def test_the_stack_offers_the_api_nothing_while_it_is_off():
    assert _auth_stack({}).hosted_ui is None


def test_the_domain_prefix_can_be_named_for_the_environment():
    stack = _auth_stack({**ON, "cognito_domain_prefix": "acme-sso-dev"})
    template = Template.from_stack(stack).to_json()
    assert one(template, DOMAIN)[1]["Domain"] == "acme-sso-dev"


@pytest.mark.parametrize(
    "prefix",
    [
        "acme-sso",  # not suffixed with the environment
        "acme-sso-staging",  # another environment's suffix
        "Acme-dev",  # upper case
        "acme_sso-dev",  # underscore
        "-dev",  # nothing before the suffix
        "my-aws-dev",  # Cognito refuses aws, amazon and cognito in a prefix
        "amazon-dev",
        "cognito-dev",
        "a" * 60 + "-dev",  # over 63 characters
    ],
)
def test_a_domain_prefix_cognito_or_the_environment_would_refuse_is_refused(prefix):
    with pytest.raises(ValueError, match="cognito_domain_prefix"):
        _auth_stack({**ON, "cognito_domain_prefix": prefix})


def test_a_domain_prefix_without_the_key_on_is_refused():
    """Otherwise it does nothing, silently."""
    with pytest.raises(ValueError, match="cognito_hosted_ui"):
        _auth_stack({"cognito_domain_prefix": "acme-sso-dev"})
