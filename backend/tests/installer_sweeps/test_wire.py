"""The stub's wire format, read back by botocore's own parsers (H6's offline half).

scripts/check_installer_cli.py serves the fake's response to the real CLI in the
protocol of the CLI's request (harness/wire.py). If that serialization were wrong,
the CLI would print something else and H6 would blame the fake. So here every
modelled operation's response for an installed account is written in its
service's protocol, CloudWatch in all three it has, and parsed with botocore's
parser for that protocol and botocore's full model: what comes back must be the
response itself.
"""

from __future__ import annotations

import datetime

import pytest
from botocore import parsers
from botocore.session import get_session

from backend.tests.installer_sweeps.harness import accounts, fake_aws, shapes, wire

pytestmark = pytest.mark.unit

ARGS = {
    ("ecs", "list-services"): ["--cluster", "experimentation-staging"],
    ("kms", "describe-key"): ["--key-id", accounts._key_id("database-key")],
    ("cognito-idp", "list-user-pools"): ["--max-results", "60"],
    ("application-autoscaling", "describe-scalable-targets"): [
        "--service-namespace",
        "ecs",
    ],
    ("iam", "list-policies"): ["--scope", "Local"],
}
#: CloudWatch's protocols, newest first: a CLI uses the first it knows.
EVERY_PROTOCOL = {"cloudwatch": ("smithy-rpc-v2-cbor", "json", "query")}


@pytest.fixture(scope="module")
def doc():
    return shapes.extract(fake_aws.modelled_operations())


def _utc(value):
    """Parsed timestamps (datetimes, in local time for an epoch) as UTC text."""
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.timezone.utc).isoformat()
    if isinstance(value, dict):
        return {k: _utc(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_utc(v) for v in value]
    return value


def _cases():
    state = accounts.installed_account()
    # A pending secret, so a timestamp crosses every JSON-protocol path.
    state["regions"]["us-west-2"]["secrets"].append(
        {"ARN": "arn:x", "Name": "old", "DeletedDate": "2026-10-21T05:41:07.512Z"}
    )
    for service, name in fake_aws.modelled_operations():
        if (service, name) == ("backup", "list-recovery-points-by-backup-vault"):
            continue
        argv = [service, name, *ARGS.get((service, name), [])]
        yield (
            service,
            name,
            fake_aws.raw_response(state, argv, {"AWS_REGION": "us-west-2"}),
        )


@pytest.mark.parametrize(
    "service, name, response",
    list(_cases()),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_botocore_reads_back_what_the_stub_writes(doc, service, name, response):
    spec = doc[service]
    operation = spec["operations"][name]
    model = get_session().get_service_model(shapes.model_name(service))
    output = model.operation_model(operation["name"]).output_shape
    expected = _utc(
        fake_aws.render_timestamps(
            response, operation["output"], spec["shapes"], epoch=False
        )
    )
    for protocol in EVERY_PROTOCOL.get(service, (spec["metadata"]["protocol"],)):
        status, headers, body = wire.serialize(protocol, spec, operation, response)
        parsed = parsers.create_parser(protocol).parse(
            {"status_code": status, "headers": headers, "body": body}, output
        )
        parsed.pop("ResponseMetadata", None)
        assert _utc(parsed) == expected, (protocol, body[:400])


@pytest.mark.parametrize(
    "protocol", ["json", "rest-json", "smithy-rpc-v2-cbor", "query", "ec2", "rest-xml"]
)
def test_botocore_reads_back_an_error_code_and_message(doc, protocol):
    service = {
        "json": "ecs",
        "rest-json": "lambda",
        "smithy-rpc-v2-cbor": "cloudwatch",
        "query": "cloudformation",
        "ec2": "ec2",
        "rest-xml": "route53",
    }[protocol]
    spec = doc[service]
    status, headers, body = wire.serialize_error(
        protocol, spec, "AccessDenied", "no, not you", 403
    )
    parsed = parsers.create_parser(protocol).parse(
        {"status_code": status, "headers": headers, "body": body}, None
    )
    assert parsed["Error"]["Code"] == "AccessDenied"
    assert parsed["Error"]["Message"] == "no, not you"
