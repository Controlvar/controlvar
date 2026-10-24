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

from backend.tests.installer_sweeps.harness import (
    accounts,
    fake_aws,
    fake_ops_install,
    fake_ops_uninstall,
    shapes,
    wire,
)
from backend.tests.installer_sweeps.harness.install_world import (
    install_snapshots,
    replayable,
)

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
    """The sweep's reads, for an installed account. Install's own operations are read
    back from a clean install's calls (test_botocore_reads_back_installs_calls)."""
    state = accounts.installed_account()
    # A pending secret, so a timestamp crosses every JSON-protocol path.
    state["regions"]["us-west-2"]["secrets"].append(
        {"ARN": "arn:x", "Name": "old", "DeletedDate": "2026-10-21T05:41:07.512Z"}
    )
    for service, name in fake_aws.modelled_operations():
        if (service, name) == ("backup", "list-recovery-points-by-backup-vault"):
            continue
        if (
            service,
            name,
        ) in fake_ops_install.REGISTERED | fake_ops_uninstall.REGISTERED:
            continue
        argv = [service, name, *ARGS.get((service, name), [])]
        yield (
            service,
            name,
            fake_aws.raw_response(state, argv, {"AWS_REGION": "us-west-2"}),
        )


@pytest.fixture(scope="module")
def install_calls(tmp_path_factory, shapes_file):
    """Every AWS call of a clean install, with the response the stub would serve it."""
    root = tmp_path_factory.mktemp("wire-install")
    snapshots, _ = install_snapshots(root / "w", shapes_file)
    found = []
    for snap in snapshots:
        argv = replayable(snap["argv"], root, snap.get("files"))
        if (argv[0], argv[1]) in fake_aws.CUSTOMIZATIONS:
            continue  # the CLI's own command: check_installer_cli.py serves its operation
        try:
            response = fake_aws.raw_response(snap["state"], argv, snap["env"])
        except fake_aws.ServiceError:
            continue  # an error answer (a role not there yet): errors are DENIALS' cases
        found.append((argv[0], argv[1], response))
    return found


def test_botocore_reads_back_installs_calls(doc, install_calls):
    """Each response a clean install's and uninstall's calls get, written in its service's
    protocol and parsed back by botocore, is the response itself (the writes and deletes
    included)."""
    assert len(install_calls) > 50, len(install_calls)
    covered = {(service, name) for service, name, _ in install_calls}
    missing = sorted(
        (fake_ops_install.REGISTERED | fake_ops_uninstall.REGISTERED)
        - covered
        - UNREACHED
    )
    assert missing == [], f"install or uninstall operations no run calls: {missing}"
    for service, name, response in install_calls:
        if doc[service]["operations"][name]["output"] is None:
            # No output (a delete): the stub sends an empty success, and there is
            # nothing for a parser to read back.
            assert response == {}, (service, name, response)
            continue
        test_botocore_reads_back_what_the_stub_writes(doc, service, name, response)


#: Install's operations a clean install never calls (the deploy's failure path, the
#: watcher's reads while a stack is still being created, AWS's default quota where the
#: account has none applied).
UNREACHED = {
    ("cloudformation", "describe-stack-events"),
    ("ecs", "list-tasks"),
    ("ecs", "describe-services"),
    ("service-quotas", "get-aws-default-service-quota"),
}


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
