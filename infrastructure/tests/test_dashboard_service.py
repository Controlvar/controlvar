"""The dashboard's ECS service and the ALB's path routing (#69; Stream C, C3).

One public origin (DECISIONS D5, D14). The HTTPS listener's default action is
the dashboard; `/api/*` and the API's four root routes go to the API's LIVE
target group. Everything below is asserted on a real ``app.synth()`` and as
exact values, because every property here fails silently when it is wrong:

* a path the rules miss reaches the dashboard's nginx, which answers 502 for
  `/api` (its upstream is deliberately nowhere) -- while every probe is green;
* the API rules pointing at the empty one of blue/green is a 503 on every API
  request, while `/` and the dashboard's probes are green (the black hole
  engineering-manager condition 8 is about);
* a missing half of the ALB -> dashboard security-group path is a dashboard
  target group that never goes healthy.

Which environment gets how many tasks is DECISIONS D13 (staging: dashboard 1,
API 2); the production dashboard count is the founder's to decide, so it is a
default in ``app.py`` and asserted as that default, not as a decision.
"""

from __future__ import annotations

import json
import re
import runpy

import aws_cdk as cdk
import pytest

from .test_app_profiles import CDK_DIR, REPO_ROOT, _app_environment

ALB_NGINX = REPO_ROOT / "tests" / "alb" / "nginx.conf"
GUARD_SCRIPT = REPO_ROOT / "scripts" / "check_live_target_group.py"


def _synth(environment: str = "dev", context: dict | None = None):
    """``app.py`` synthesised for ``environment``, with extra CDK context.

    Context is given as an ``App`` prop, the way the CLI's ``-c`` ultimately
    arrives (see ``test_pipeline_owns_the_image.pinned_assembly`` for why an
    environment variable cannot reach the jsii process from here).
    """
    original = cdk.App.__init__

    def with_context(self, **kwargs):
        merged = dict(kwargs.pop("context", None) or {})
        merged.update(context or {})
        original(self, context=merged, **kwargs)

    cdk.App.__init__ = with_context
    try:
        with _app_environment(CDK_DIR, ENVIRONMENT=environment):
            namespace = runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")
    finally:
        cdk.App.__init__ = original
    return namespace["app"].synth()


def _fargate(assembly) -> dict:
    (stack,) = [s for s in assembly.stacks if "-fargate-" in s.stack_name]
    return stack.template["Resources"]


def _by_type(resources: dict, rtype: str) -> dict:
    return {lid: r for lid, r in resources.items() if r["Type"] == rtype}


def _one_id(resources: dict, prefix: str, rtype: str) -> str:
    hits = [
        lid
        for lid, r in resources.items()
        if r["Type"] == rtype and re.fullmatch(rf"{prefix}[0-9A-F]{{8}}", lid)
    ]
    assert len(hits) == 1, f"expected one {rtype} {prefix}<hash>, found {hits}"
    return hits[0]


def _ref(value) -> str:
    """The logical id a ``{"Ref": ...}`` or ``{"Fn::GetAtt": [id, ...]}`` names."""
    if isinstance(value, dict) and "Ref" in value:
        return value["Ref"]
    if isinstance(value, dict) and "Fn::GetAtt" in value:
        return value["Fn::GetAtt"][0]
    raise AssertionError(f"not a reference to a resource in this stack: {value!r}")


@pytest.fixture(scope="module")
def dev():
    return _fargate(_synth("dev"))


@pytest.fixture(scope="module")
def ids(dev):
    lb = "AWS::ElasticLoadBalancingV2::"
    return {
        "blue": _one_id(dev, "BlueTargetGroup", lb + "TargetGroup"),
        "green": _one_id(dev, "GreenTargetGroup", lb + "TargetGroup"),
        "dashboard_tg": _one_id(dev, "DashboardTargetGroup", lb + "TargetGroup"),
        "https": _one_id(dev, "BackendALBHttpsListener", lb + "Listener"),
        "test": _one_id(dev, "BackendALBTestListener", lb + "Listener"),
        "alb_sg": _one_id(dev, "BackendALBSecurityGroup", "AWS::EC2::SecurityGroup"),
        "dashboard_sg": _one_id(
            dev, "DashboardSecurityGroup", "AWS::EC2::SecurityGroup"
        ),
        "dashboard_service": _one_id(dev, "DashboardService", "AWS::ECS::Service"),
        "api_service": _one_id(dev, "BackendService", "AWS::ECS::Service"),
    }


def _rules(resources: dict) -> list[tuple[str, int, list[list[str]], str]]:
    """(listener, priority, [path values per condition], target) for every rule."""
    out = []
    for rule in _by_type(
        resources, "AWS::ElasticLoadBalancingV2::ListenerRule"
    ).values():
        props = rule["Properties"]
        (action,) = props["Actions"]
        assert action["Type"] == "forward", action
        conditions = []
        for condition in props["Conditions"]:
            assert condition["Field"] == "path-pattern", condition
            conditions.append(condition["PathPatternConfig"]["Values"])
        out.append(
            (
                _ref(props["ListenerArn"]),
                props["Priority"],
                conditions,
                _ref(action["TargetGroupArn"]),
            )
        )
    return sorted(out, key=lambda r: r[1])


# --- the ALB's routing ---------------------------------------------------------


@pytest.mark.regression
def test_the_https_default_action_is_the_dashboard(dev, ids):
    actions = dev[ids["https"]]["Properties"]["DefaultActions"]
    assert actions == [
        {"TargetGroupArn": {"Ref": ids["dashboard_tg"]}, "Type": "forward"}
    ]


@pytest.mark.regression
def test_the_api_paths_go_to_the_live_target_group_exactly(dev, ids):
    """Exact rules: listener, priority, path values and target.

    `/health*` is NOT a shorter spelling of the second rule: it also matches
    `/healthz` (the dashboard's static liveness path) and anything else that
    starts with `/health`. ALB quotas: five condition values per rule, three
    match evaluations per condition; the second rule is at the three-value
    limit.
    """
    assert _rules(dev) == [
        (ids["https"], 10, [["/api/*"]], ids["blue"]),
        (ids["https"], 11, [["/health", "/health/*", "/metrics"]], ids["blue"]),
    ]


def test_no_condition_exceeds_the_alb_limits_or_uses_a_bare_prefix_wildcard(dev):
    for _, priority, conditions, _ in _rules(dev):
        for values in conditions:
            assert len(values) <= 3, (priority, values)
            for value in values:
                # A wildcard only after a slash: `/health*` matches `/healthz`.
                assert "*" not in value or value.endswith("/*"), (priority, value)
                assert value.count("*") <= 1, (priority, value)


def test_the_test_listener_is_unchanged(dev, ids):
    """CodeDeploy's test listener still sends everything to green."""
    actions = dev[ids["test"]]["Properties"]["DefaultActions"]
    assert actions == [{"TargetGroupArn": {"Ref": ids["green"]}, "Type": "forward"}]


# --- guard (b): the live target group is a context value, pinned -------------


@pytest.mark.regression
def test_the_context_moves_both_rules_to_green(ids):
    green = _fargate(_synth("dev", {"api_live_target_group": "green"}))
    assert [(p, t) for _, p, _, t in _rules(green)] == [
        (10, ids["green"]),
        (11, ids["green"]),
    ]
    # And nothing else about the listener moves with it.
    assert green[ids["https"]]["Properties"]["DefaultActions"][0]["TargetGroupArn"] == {
        "Ref": ids["dashboard_tg"]
    }


@pytest.mark.parametrize("value", ["Blue", "", "purple", 1])
def test_a_context_that_is_not_blue_or_green_is_refused(value):
    with pytest.raises(ValueError, match="api_live_target_group context must be"):
        _synth("dev", {"api_live_target_group": value})


# --- the dashboard service -----------------------------------------------------


@pytest.mark.regression
def test_the_app_has_exactly_two_ecs_services():
    assembly = _synth("dev")
    services = {
        r["Properties"]["ServiceName"]
        for s in assembly.stacks
        for r in s.template["Resources"].values()
        if r["Type"] == "AWS::ECS::Service"
    }
    assert services == {"experimentation-backend-dev", "experimentation-dashboard-dev"}


@pytest.mark.regression
def test_the_dashboard_rolls_with_the_circuit_breaker(dev, ids):
    props = dev[ids["dashboard_service"]]["Properties"]
    assert props["DeploymentController"] == {"Type": "ECS"}
    config = props["DeploymentConfiguration"]
    assert config["DeploymentCircuitBreaker"] == {"Enable": True, "Rollback": True}
    assert config["MinimumHealthyPercent"] == 100
    assert config["MaximumPercent"] == 200
    assert props["LoadBalancers"] == [
        {
            "ContainerName": "dashboard",
            "ContainerPort": 8080,
            "TargetGroupArn": {"Ref": ids["dashboard_tg"]},
        }
    ]
    assert props["NetworkConfiguration"]["AwsvpcConfiguration"]["AssignPublicIp"] == (
        "DISABLED"
    )
    assert props["NetworkConfiguration"]["AwsvpcConfiguration"]["SecurityGroups"] == [
        {"Fn::GetAtt": [ids["dashboard_sg"], "GroupId"]}
    ]


@pytest.mark.regression
def test_the_dashboard_container_never_proxies_the_api(dev):
    (task,) = [
        r
        for r in _by_type(dev, "AWS::ECS::TaskDefinition").values()
        if r["Properties"]["Family"] == "experimentation-dashboard-dev"
    ]
    (container,) = task["Properties"]["ContainerDefinitions"]
    assert container["Name"] == "dashboard"
    assert container["Environment"] == [
        {"Name": "API_UPSTREAM", "Value": "http://127.0.0.1:1"}
    ]
    assert container["PortMappings"] == [{"ContainerPort": 8080, "Protocol": "tcp"}]
    assert container["HealthCheck"]["Command"] == [
        "CMD-SHELL",
        "wget -qO- http://127.0.0.1:8080/healthz >/dev/null 2>&1 || exit 1",
    ]
    image = json.dumps(container["Image"])
    assert "/experimentation-platform/web:bootstrap" in image, image


def test_the_dashboard_target_group_probes_the_static_healthz(dev, ids):
    props = dev[ids["dashboard_tg"]]["Properties"]
    assert (props["Port"], props["Protocol"], props["TargetType"]) == (
        8080,
        "HTTP",
        "ip",
    )
    assert props["HealthCheckPath"] == "/healthz"
    assert props["Matcher"] == {"HttpCode": "200"}


@pytest.mark.parametrize("value", ["latest", ""])
def test_the_dashboard_image_tag_refuses_latest_and_empty(value):
    with pytest.raises(ValueError, match="dashboard_image_tag"):
        _synth("dev", {"dashboard_image_tag": value})


def _dashboard_image(resources: dict, environment: str = "dev") -> str:
    (task,) = [
        r
        for r in _by_type(resources, "AWS::ECS::TaskDefinition").values()
        if r["Properties"]["Family"] == f"experimentation-dashboard-{environment}"
    ]
    (container,) = task["Properties"]["ContainerDefinitions"]
    return json.dumps(container["Image"])


#: A digest of the shape `check_dashboard_image.py` prints.
PINNED_DIGEST = "sha256:" + "0123456789abcdef" * 4


@pytest.mark.regression
def test_a_digest_pin_names_the_image_by_digest():
    """`-c dashboard_image_tag=sha256:<hex>` is how a running dashboard is kept
    across a `cdk deploy` (C4b; principal-engineer C7).

    It must synthesise `web@sha256:<hex>`. Handled as a tag it would be
    `web:sha256:<hex>`, a reference to a tag, not to the running image (and
    not a tag ECR can hold: a tag cannot contain `:`).
    """
    image = _dashboard_image(
        _fargate(_synth("dev", {"dashboard_image_tag": PINNED_DIGEST}))
    )
    assert f"/experimentation-platform/web@{PINNED_DIGEST}" in image, image
    assert ":sha256:" not in image, image


def test_the_check_script_names_this_stack_s_dashboard(dev):
    """`check_dashboard_image.py` finds the service, container and repository by
    name; each must be what the stack synthesises."""
    check = runpy.run_path(str(REPO_ROOT / "scripts" / "check_dashboard_image.py"))
    services = {
        r["Properties"]["ServiceName"]
        for r in _by_type(dev, "AWS::ECS::Service").values()
    }
    assert check["SERVICE"].format(env="dev") in services
    (task,) = [
        r
        for r in _by_type(dev, "AWS::ECS::TaskDefinition").values()
        if r["Properties"]["Family"] == "experimentation-dashboard-dev"
    ]
    assert [c["Name"] for c in task["Properties"]["ContainerDefinitions"]] == [
        check["CONTAINER"]
    ]
    assert f"/{check['REPOSITORY']}:" in _dashboard_image(dev)
    names = runpy.run_path(str(CDK_DIR / "stacks" / "names.py"))
    assert check["CLUSTER"].format(env="dev") == names["ecs_cluster_name"]("dev")
    assert check["REPOSITORY"] == names["DASHBOARD_ECR_REPOSITORY"]


# --- the security-group path, both halves ---------------------------------------


@pytest.mark.regression
def test_the_alb_reaches_the_dashboard_on_8080_and_nothing_else_does(dev, ids):
    """Both sides of ALB -> dashboard tcp 8080, and only that way in.

    The ALB's group allows no outbound traffic by default, so the egress half
    is as necessary as the ingress half: without it the dashboard target group
    never goes healthy and `/` is a 503.
    """
    dashboard_sg = {"Fn::GetAtt": [ids["dashboard_sg"], "GroupId"]}
    alb_sg = {"Fn::GetAtt": [ids["alb_sg"], "GroupId"]}

    ingress = [
        r["Properties"]
        for r in _by_type(dev, "AWS::EC2::SecurityGroupIngress").values()
        if r["Properties"]["GroupId"] == dashboard_sg
    ]
    assert [
        (i["IpProtocol"], i["FromPort"], i["ToPort"], i.get("SourceSecurityGroupId"))
        for i in ingress
    ] == [("tcp", 8080, 8080, alb_sg)]
    # No inline ingress on the group itself either.
    assert not dev[ids["dashboard_sg"]]["Properties"].get("SecurityGroupIngress")

    egress = [
        r["Properties"]
        for r in _by_type(dev, "AWS::EC2::SecurityGroupEgress").values()
        if r["Properties"].get("DestinationSecurityGroupId") == dashboard_sg
    ]
    assert [
        (e["IpProtocol"], e["FromPort"], e["ToPort"], e["GroupId"]) for e in egress
    ] == [("tcp", 8080, 8080, alb_sg)]


# --- task counts, per environment (D13) -----------------------------------------

#: environment -> (API tasks and auto-scaling floor, dashboard tasks)
COUNTS = {
    "dev": (2, 1),
    "staging": (2, 1),  # D13
    "prod": (3, 2),  # API unchanged; dashboard 2 = team default, founder-pending
    "demo": (1, 1),
}


@pytest.mark.regression
@pytest.mark.parametrize("environment", sorted(COUNTS))
def test_task_counts_per_environment(environment):
    resources = _fargate(_synth(environment))
    services = {
        r["Properties"]["ServiceName"]: r["Properties"]
        for r in _by_type(resources, "AWS::ECS::Service").values()
    }
    api, dashboard = COUNTS[environment]
    assert services[f"experimentation-backend-{environment}"]["DesiredCount"] == api
    assert (
        services[f"experimentation-dashboard-{environment}"]["DesiredCount"]
        == dashboard
    )
    (scaling,) = _by_type(
        resources, "AWS::ApplicationAutoScaling::ScalableTarget"
    ).values()
    # A floor above the desired count would hold the service above it.
    assert scaling["Properties"]["MinCapacity"] == api
    assert scaling["Properties"]["MaxCapacity"] == 10


# --- the rehearsal and the guard script agree with the stack --------------------


def _nginx_location_paths() -> dict[str, str]:
    """tests/alb/nginx.conf's locations, as ALB path patterns -> upstream."""
    patterns = {}
    current = None
    for raw in ALB_NGINX.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("location "):
            current = line[len("location ") :].rstrip("{").strip()
        elif current and line.startswith("proxy_pass "):
            if current.startswith("= "):
                pattern = current[2:]  # exact match == an ALB value with no *
            elif current.endswith("/"):
                pattern = current + "*"  # prefix match == `<prefix>/*`
            else:
                raise AssertionError(f"a location the ALB cannot express: {current}")
            patterns[pattern] = line[len("proxy_pass ") :].rstrip(";").strip()
            current = None
    return patterns


@pytest.mark.regression
def test_the_alb_rehearsal_routes_the_same_paths_as_the_listener(dev):
    """tests/alb/nginx.conf copies the listener rules by hand; they must agree.

    `location /` is the default action (the dashboard); every other location is
    an API path and must be exactly the rules' path set, sent to the API.
    """
    locations = _nginx_location_paths()
    assert locations.pop("/*") == "http://frontend:8080"
    rule_paths = {v for _, _, conds, _ in _rules(dev) for vals in conds for v in vals}
    assert set(locations) == rule_paths, (
        f"only in nginx.conf: {sorted(set(locations) - rule_paths)}; "
        f"only in the listener rules: {sorted(rule_paths - set(locations))}"
    )
    assert set(locations.values()) == {"http://api:8000"}


def test_the_guard_script_finds_this_stack_s_resources(dev):
    """The script finds resources by logical-id prefix; each must match once."""
    guard = runpy.run_path(str(GUARD_SCRIPT))
    for key, (prefix, rtype) in guard["LOGICAL_ID_PREFIXES"].items():
        _one_id(dev, prefix, rtype)
    assert guard["API_PATH"] in {
        v for _, _, conds, _ in _rules(dev) for vals in conds for v in vals
    }


# --- what the CDK page says about the load balancer and the API service -----------


@pytest.mark.regression
def test_port_80_only_redirects_to_https_443(dev):
    """The AWS CDK Deployment page: the HTTP listener redirects to HTTPS, permanently."""
    listeners = [
        r["Properties"]
        for r in _by_type(dev, "AWS::ElasticLoadBalancingV2::Listener").values()
        if r["Properties"].get("Port") == 80
    ]
    assert len(listeners) == 1, listeners
    (http,) = listeners
    assert http["Protocol"] == "HTTP"
    assert http["DefaultActions"] == [
        {
            "Type": "redirect",
            "RedirectConfig": {
                "Port": "443",
                "Protocol": "HTTPS",
                "StatusCode": "HTTP_301",
            },
        }
    ], http["DefaultActions"]


@pytest.mark.regression
def test_the_api_service_is_under_codedeploy_and_the_dashboard_is_not(dev):
    """The API's new tasks come only from a CodeDeploy deployment; the
    dashboard's service is ECS's own rolling controller."""
    controllers = {
        r["Properties"]["ServiceName"]: r["Properties"].get(
            "DeploymentController", {"Type": "ECS"}
        )["Type"]
        for r in _by_type(dev, "AWS::ECS::Service").values()
    }
    assert controllers == {
        "experimentation-backend-dev": "CODE_DEPLOY",
        "experimentation-dashboard-dev": "ECS",
    }, controllers


@pytest.mark.regression
def test_the_api_and_dashboard_log_groups(dev):
    """The log groups the deployment guide and the runbook send an operator to
    (the migration task's is the migrations stack's)."""
    names = sorted(
        r["Properties"]["LogGroupName"]
        for r in _by_type(dev, "AWS::Logs::LogGroup").values()
    )
    assert names == [
        "/ecs/experimentation-backend-dev",
        "/ecs/experimentation-dashboard-dev",
    ], names


@pytest.mark.regression
def test_the_app_creates_no_dns_record():
    """The CDK page: the alias records for the hostname are added by hand after
    the first deploy, because no stack creates one."""
    records = [
        (stack.stack_name, lid)
        for stack in _synth("dev").stacks
        for lid, r in stack.template.get("Resources", {}).items()
        if r["Type"].startswith("AWS::Route53::")
    ]
    assert records == [], records
