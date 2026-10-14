"""The only way in from the internet is the load balancer's HTTPS listener.

``docs/architecture/overview.md`` says the Application Load Balancer is the
only entry point the CDK creates, HTTPS with HTTP redirected to HTTPS, and that
no stack creates CloudFront or API Gateway. Both are asserted here on a real
``app.synth()`` of each profile, over every stack's template:

* exactly one load balancer, internet-facing; its port-80 listener does
  nothing but a permanent redirect to HTTPS on 443, and every other listener
  is HTTPS;
* the ECS services take no public address, and no Lambda function has a URL;
* no resource of CloudFront, API Gateway (REST) or API Gateway V2 (HTTP and
  WebSocket APIs).
"""

from __future__ import annotations

import runpy

import pytest

from .test_app_profiles import CDK_DIR, REPO_ROOT, _app_environment

#: Resource-type prefixes that would be a second way in.
OTHER_FRONT_DOORS = ("AWS::CloudFront::", "AWS::ApiGateway::", "AWS::ApiGatewayV2::")

LISTENER = "AWS::ElasticLoadBalancingV2::Listener"
LOAD_BALANCER = "AWS::ElasticLoadBalancingV2::LoadBalancer"


def _profiles() -> list:
    full = pytest.param(
        "full",
        marks=pytest.mark.skipif(
            not (REPO_ROOT / "modules").is_dir(), reason="a core tree has no full profile"
        ),
    )
    return ["core", full]


@pytest.fixture(scope="module", params=_profiles())
def resources(request) -> list[tuple[str, str, dict]]:
    """(stack, logical id, resource) for every resource the profile synthesises."""
    with _app_environment(CDK_DIR, EXPERIMENTLY_PROFILE=request.param):
        namespace = runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")
    assembly = namespace["app"].synth()
    found = [
        (stack.stack_name, logical_id, resource)
        for stack in assembly.stacks
        for logical_id, resource in stack.template.get("Resources", {}).items()
    ]
    assert found, f"{request.param}: the synth produced no resources"
    return found


def _of_type(resources, rtype: str) -> list[tuple[str, str, dict]]:
    return [r for r in resources if r[2]["Type"] == rtype]


@pytest.mark.regression
def test_the_only_entry_point_is_https_with_http_redirected_to_https(resources):
    balancers = _of_type(resources, LOAD_BALANCER)
    assert len(balancers) == 1, [lid for _, lid, _ in balancers]
    ((_, alb_id, alb),) = balancers
    assert alb["Properties"]["Scheme"] == "internet-facing"
    assert alb["Properties"]["Type"] == "application"

    listeners = _of_type(resources, LISTENER)
    assert listeners, "the load balancer has no listener"
    by_port = {}
    for _, lid, listener in listeners:
        props = listener["Properties"]
        assert props["LoadBalancerArn"] == {"Ref": alb_id}, lid
        by_port[props["Port"]] = props

    http = by_port.pop(80)
    assert http["Protocol"] == "HTTP"
    assert http["DefaultActions"] == [
        {
            "Type": "redirect",
            "RedirectConfig": {"Port": "443", "Protocol": "HTTPS", "StatusCode": "HTTP_301"},
        }
    ]
    assert 443 in by_port, sorted(by_port)
    for port, props in by_port.items():
        assert props["Protocol"] == "HTTPS", (port, props["Protocol"])

    for _, lid, service in _of_type(resources, "AWS::ECS::Service"):
        network = service["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]
        assert network["AssignPublicIp"] == "DISABLED", lid
    assert not _of_type(resources, "AWS::Lambda::Url")


@pytest.mark.regression
def test_no_stack_creates_cloudfront_or_api_gateway(resources):
    found = sorted(
        f"{stack}/{lid} {resource['Type']}"
        for stack, lid, resource in resources
        if resource["Type"].startswith(OTHER_FRONT_DOORS)
    )
    assert not found, found
