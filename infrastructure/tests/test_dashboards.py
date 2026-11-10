"""Every dashboard widget graphs a metric something deployed publishes (#424).

The two dashboards, ``experimentation-platform-<env>`` and
``experimentation-application-metrics-<env>``, graphed an API Gateway, three
Lambdas and two DynamoDB tables no stack deploys, Aurora and Redis under
identifiers no resource has (``AuroraCluster``, ``Redis``), and two
namespaces nothing publishes to (``ExperimentationPlatform`` and
``ExperimentationPlatform/Prometheus``). A widget like that renders empty and
says nothing is wrong.

Everything here parses each ``DashboardBody`` of a real ``app.synth()`` --
every environment, both profiles, both live colours -- and holds every metric
to an explicit table of publishers (no prefix match), and every dimension to a
resource the same synth creates:

* Aurora: the AuroraHighCPU alarm's dimensions (the SSM parameter's Ref and
  ``Role=WRITER``);
* Redis: exactly the replication group's nodes, which the CPU alarms watch;
* Kinesis: the stream the iterator-age alarm watches (full profile only);
* ECS: the synthesised cluster's and API service's names;
* ``Experimently/<env>``: a metric one of the fargate stack's metric filters
  publishes.
"""

from __future__ import annotations

import json
import re

import pytest

from .test_standing_alarms import (
    CASES,
    _alarms_by_name,
    _ids,
    _of_type,
    _stack,
    _synth,
)

#: (namespace, metric) pairs a deployed resource publishes, per namespace.
#: ``Experimently/<env>`` is checked against the metric filters instead.
PUBLISHED = {
    "AWS/Kinesis": {"IncomingRecords", "IncomingBytes", "GetRecords.IteratorAgeMilliseconds"},
    "AWS/RDS": {
        "CPUUtilization",
        "DatabaseConnections",
        "FreeableMemory",
        "ReadLatency",
        "WriteLatency",
    },
    "AWS/ElastiCache": {"EngineCPUUtilization", "CurrConnections", "CacheHits", "CacheMisses"},
    "AWS/ECS": {"CPUUtilization", "MemoryUtilization"},
    "ECS/ContainerInsights": {"RunningTaskCount"},
}

_MARKER = "@@token{}@@"
_MARKER_RE = re.compile(r"@@token(\d{1,4})@@")


@pytest.fixture(scope="module")
def synths():
    return {case: _synth(*case) for case in CASES}


def parse_body(body) -> tuple[dict, list]:
    """A DashboardBody as ``(json, tokens)``.

    The body is an ``Fn::Join`` of strings and CloudFormation tokens (``Ref``,
    ``Fn::ImportValue``). Each token becomes the marker ``@@token<n>@@`` --
    always inside a JSON string, so the text parses -- and ``tokens[n]`` is the
    token itself.
    """
    if isinstance(body, str):
        return json.loads(body), []
    separator, parts = body["Fn::Join"]
    assert separator == ""
    tokens, text = [], []
    for part in parts:
        if isinstance(part, str):
            text.append(part)
        else:
            text.append(_MARKER.format(len(tokens)))
            tokens.append(part)
    return json.loads("".join(text)), tokens


def _resolve(value, tokens):
    """A marker back to its token; any other value unchanged."""
    if isinstance(value, str):
        match = _MARKER_RE.fullmatch(value)
        if match:
            return tokens[int(match.group(1))]
        assert not _MARKER_RE.search(value), f"a token inside a longer string: {value!r}"
    return value


def widgets(template: dict, dashboard_name: str) -> list[tuple[str, list[dict]]]:
    """``[(title, [metric, ...])]`` of one dashboard, in order.

    Each metric is ``{"namespace", "name", "dimensions": {name: value}}`` with
    tokens resolved. A widget that is not a metric graph, a metric array that
    is not the full ``[namespace, name, dim, value, ..., {options}]`` form, and
    a math expression are all refused: none of them can be checked here.
    """
    (resource,) = [
        r
        for r in _of_type(template, "AWS::CloudWatch::Dashboard").values()
        if r["Properties"]["DashboardName"] == dashboard_name
    ]
    body, tokens = parse_body(resource["Properties"]["DashboardBody"])
    found = []
    for widget in body["widgets"]:
        assert widget["type"] == "metric", widget
        properties = widget["properties"]
        metrics = []
        for array in properties["metrics"]:
            options = array[-1] if isinstance(array[-1], dict) else {}
            assert "expression" not in options, array
            values = array[:-1] if isinstance(array[-1], dict) else array
            assert "." not in values, f"a shorthand metric array: {array}"
            assert len(values) >= 2 and len(values) % 2 == 0, array
            namespace, name, *pairs = (_resolve(v, tokens) for v in values)
            metrics.append(
                {
                    "namespace": namespace,
                    "name": name,
                    "dimensions": dict(zip(pairs[::2], pairs[1::2])),
                }
            )
        found.append((properties["title"], metrics))
    return found


def _redis_members(synth: dict, env: str) -> list[str]:
    (group,) = _of_type(_stack(synth, "redis", env), "AWS::ElastiCache::ReplicationGroup").values()
    group_id = group["Properties"]["ReplicationGroupId"]
    return [f"{group_id}-{i:03d}" for i in range(1, group["Properties"]["NumCacheClusters"] + 1)]


def _expected_titles(env: str, profile: str) -> dict[str, list[str]]:
    platform = ["Aurora writer", "Redis nodes"]
    if profile == "full":
        platform.insert(0, "Kinesis event stream")
    return {
        f"experimentation-platform-{env}": platform,
        f"experimentation-application-metrics-{env}": [
            "API service: CPU and memory",
            "API tasks running",
            "API log lines at level error or critical",
            "Audit-log entries not written",
        ],
    }


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_every_widget_graphs_a_published_metric_of_a_deployed_resource(synths, case):
    """#424: no widget graphs a namespace, metric or resource nothing publishes."""
    env, profile, _ = case
    synth = synths[case]
    monitoring = _stack(synth, "monitoring", env)
    fargate = _stack(synth, "fargate", env)
    alarms = _alarms_by_name(monitoring)

    # What the dimensions must name, from the same synth.
    _, aurora_alarm = alarms[f"AuroraHighCPU-{env}"]
    aurora = {d["Name"]: d["Value"] for d in aurora_alarm["Dimensions"]}
    assert set(aurora) == {"DBClusterIdentifier", "Role"}, aurora
    members = _redis_members(synth, env)
    redis_watched = {
        props["Dimensions"][0]["Value"]
        for _, props in alarms.values()
        if props.get("Namespace") == "AWS/ElastiCache"
    }
    assert redis_watched == set(members), (redis_watched, members)
    stream = None
    if profile == "full":
        _, kinesis_alarm = alarms[f"KinesisProcessingDelay-{env}"]
        (stream_dimension,) = kinesis_alarm["Dimensions"]
        stream = stream_dimension["Value"]
    (cluster,) = _of_type(_stack(synth, "compute", env), "AWS::ECS::Cluster").values()
    (service,) = [
        s["Properties"]["ServiceName"]
        for s in _of_type(fargate, "AWS::ECS::Service").values()
        if s["Properties"]["ServiceName"].startswith("experimentation-backend-")
    ]
    api_service = {
        "ClusterName": cluster["Properties"]["ClusterName"],
        "ServiceName": service,
    }
    filtered = {
        (t["MetricNamespace"], t["MetricName"])
        for template in synth["templates"].values()
        for r in _of_type(template, "AWS::Logs::MetricFilter").values()
        for t in r["Properties"]["MetricTransformations"]
    }

    expected = {
        "Kinesis event stream": 3,
        "Aurora writer": 5,
        "Redis nodes": 4 * len(members),
        "API service: CPU and memory": 2,
        "API tasks running": 1,
        "API log lines at level error or critical": 1,
        "Audit-log entries not written": 1,
    }
    graphed_redis = set()
    for dashboard in _expected_titles(env, profile):
        for title, metrics in widgets(monitoring, dashboard):
            # The exact number of metric arrays parsed: a parser that finds
            # nothing would otherwise pass every check below.
            assert len(metrics) == expected[title], (dashboard, title, metrics)
            for metric in metrics:
                namespace, name, dims = (
                    metric["namespace"],
                    metric["name"],
                    metric["dimensions"],
                )
                where = (dashboard, title, namespace, name, dims)
                if namespace == f"Experimently/{env}":
                    assert (namespace, name) in filtered, ("no filter publishes", where)
                    assert dims == {}, where
                    continue
                assert name in PUBLISHED.get(namespace, ()), ("not published", where)
                if namespace == "AWS/RDS":
                    assert dims == aurora, where
                elif namespace == "AWS/ElastiCache":
                    assert set(dims) == {"CacheClusterId"}, where
                    assert dims["CacheClusterId"] in members, where
                    graphed_redis.add(dims["CacheClusterId"])
                elif namespace == "AWS/Kinesis":
                    assert dims == {"StreamName": stream}, where
                elif namespace in ("AWS/ECS", "ECS/ContainerInsights"):
                    assert dims == api_service, where
                else:  # pragma: no cover - PUBLISHED lists no other namespace
                    raise AssertionError(where)
    # Every node is graphed, not only the first.
    assert graphed_redis == set(members), (graphed_redis, members)


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_the_dashboards_have_exactly_these_widgets(synths, case):
    """The titles name the resource; core has no Kinesis widget."""
    env, profile, _ = case
    monitoring = _stack(synths[case], "monitoring", env)
    for dashboard, titles in _expected_titles(env, profile).items():
        assert [title for title, _ in widgets(monitoring, dashboard)] == titles, dashboard
    names = sorted(
        r["Properties"]["DashboardName"]
        for template in synths[case]["templates"].values()
        for r in _of_type(template, "AWS::CloudWatch::Dashboard").values()
    )
    assert names == sorted(_expected_titles(env, profile)), names


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_the_api_service_keeps_its_name(synths, case):
    """``api_service_name`` is the literal the service always had: no replacement."""
    env, _, _ = case
    fargate = _stack(synths[case], "fargate", env)
    names = sorted(
        s["Properties"]["ServiceName"] for s in _of_type(fargate, "AWS::ECS::Service").values()
    )
    assert f"experimentation-backend-{env}" in names, names


def test_the_parser_reads_every_token_and_refuses_what_it_cannot_check():
    body = {
        "Fn::Join": [
            "",
            [
                '{"widgets":[{"type":"metric","properties":{"title":"t","metrics":'
                '[["AWS/RDS","CPUUtilization","DBClusterIdentifier","',
                {"Ref": "P"},
                '",{"period":60}],["AWS/ECS","CPUUtilization"]]}}]}',
            ],
        ]
    }
    template = {
        "Resources": {
            "D": {
                "Type": "AWS::CloudWatch::Dashboard",
                "Properties": {"DashboardName": "d", "DashboardBody": body},
            }
        }
    }
    assert widgets(template, "d") == [
        (
            "t",
            [
                {
                    "namespace": "AWS/RDS",
                    "name": "CPUUtilization",
                    "dimensions": {"DBClusterIdentifier": {"Ref": "P"}},
                },
                {"namespace": "AWS/ECS", "name": "CPUUtilization", "dimensions": {}},
            ],
        )
    ]
    for refused in (
        '[[".","CPUUtilization"]]',
        '[[{"expression":"m1*2"}]]',
        '[["AWS/RDS","CPUUtilization","DBClusterIdentifier"]]',
    ):
        template["Resources"]["D"]["Properties"]["DashboardBody"] = (
            '{"widgets":[{"type":"metric","properties":{"title":"t","metrics":'
            + refused
            + "}}]}"
        )
        with pytest.raises(AssertionError):
            widgets(template, "d")
