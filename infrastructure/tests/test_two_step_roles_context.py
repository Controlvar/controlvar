"""The CDK app can require two-step sign-in for chosen roles (#1812).

One CDK context key, ``local_auth_2fa_required_roles``
(``-c local_auth_2fa_required_roles=ADMIN,DEVELOPER``), gives the API task
``LOCAL_AUTH_2FA_REQUIRED_ROLES``. Asserted on a real ``app.synth()``:

* **Set** the API container's environment holds the value exactly as given,
  for every environment, and no other container of any stack holds the
  setting.
* **Absent** the API container has no such variable at all (not an empty
  one), and a synth with the key set differs from one without it in that one
  variable and nothing else.
* **Refused** an empty or non-string value, and any value together with
  ``cognito_hosted_ui=true``, where the API signs in through Cognito and the
  setting, which is about local accounts, would do nothing.

The role names are the API's rule, applied when it starts
(``parse_two_factor_roles``); the CDK app passes the value through.
"""

from __future__ import annotations

import copy
import json

import pytest

from .test_cognito_hosted_ui import api_environment, templates
from .test_dashboard_service import _synth

ENVIRONMENTS = ("dev", "staging", "prod", "demo")
KEY = "local_auth_2fa_required_roles"
SETTING = "LOCAL_AUTH_2FA_REQUIRED_ROLES"

#: As the operator types them: passed through untouched, never normalised.
#: The padded and the lower-case value are the point: the CDK app neither
#: trims nor changes case, and the API alone decides whether a value is valid
#: (it refuses "admin" when it starts). This synth never loads the API.
VALUES = ("ADMIN", "ADMIN,DEVELOPER", "ANALYST, VIEWER", " ADMIN,DEVELOPER ", "admin")


def _containers(template: dict):
    for resource in template["Resources"].values():
        if resource["Type"] == "AWS::ECS::TaskDefinition":
            yield from resource["Properties"].get("ContainerDefinitions", [])


def _without_setting(template: dict) -> dict:
    """*template* with the setting taken out of every container."""
    stripped = copy.deepcopy(template)
    for container in _containers(stripped):
        container["Environment"] = [
            e for e in container.get("Environment", []) if e["Name"] != SETTING
        ]
    return stripped


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
@pytest.mark.parametrize("value", VALUES)
def test_set_the_api_task_has_exactly_that_value(environment, value):
    assert api_environment(environment, {KEY: value})[SETTING] == value


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_absent_the_api_task_has_no_such_variable(environment):
    env = api_environment(environment, {})
    assert SETTING not in env, f"{SETTING}={env[SETTING]!r}"


@pytest.mark.regression
@pytest.mark.parametrize("environment", ENVIRONMENTS)
@pytest.mark.parametrize("value", VALUES)
def test_only_the_api_container_gets_it(environment, value):
    holders = [
        (stack, c["Name"])
        for stack, template in templates(environment, {KEY: value}).items()
        for c in _containers(template)
        if any(e["Name"] == SETTING for e in c.get("Environment", []))
    ]
    assert holders == [(f"experimentation-fargate-{environment}", "backend")], holders


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_the_key_changes_that_one_variable_and_nothing_else(environment):
    absent = templates(environment, {})
    given = templates(environment, {KEY: "ADMIN,DEVELOPER"})
    assert set(given) == set(absent)
    for name in absent:
        assert json.dumps(_without_setting(given[name]), sort_keys=True) == json.dumps(
            absent[name], sort_keys=True
        ), f"{name} differs beyond {SETTING}"
    assert given != absent, "the key changed nothing"


@pytest.mark.parametrize("value", ["", "   ", True, 5, ["ADMIN"]])
def test_an_empty_or_non_string_value_is_refused(value):
    with pytest.raises(ValueError, match=KEY):
        _synth("dev", {KEY: value})


def test_with_the_hosted_ui_on_it_is_refused_rather_than_ignored():
    with pytest.raises(ValueError, match="cognito_hosted_ui"):
        _synth("dev", {KEY: "ADMIN", "cognito_hosted_ui": "true"})
