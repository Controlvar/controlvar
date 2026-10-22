"""The harness's fake ``aws``: what every installer test relies on it to do (PE 3).

The fake is a specification of the CLI, so its own behaviour is pinned here: the
region model, the three fault modes, the CLI's output (pagination, JSON, text,
timestamps), the shape check and the logs. That it prints what the real CLI prints
is gate H6 (scripts/check_installer_cli.py, CDK Stack Tests).
"""

from __future__ import annotations

import concurrent.futures
import json

import pytest

from backend.tests.installer_sweeps.harness import accounts, fake_aws

pytestmark = pytest.mark.unit

ACCOUNT = accounts.ACCOUNT


@pytest.fixture
def world(make_world):
    return make_world(accounts.new_account())


# --- the region model ---------------------------------------------------------------


def _vpc_in(region: str):
    def change(state):
        state["regions"][region]["vpcs"].append(
            accounts.Builder().vpc(
                "vpc-0eeeeeeeeeeeeeeee", "10.9.0.0/16", default=False
            )
        )

    return change


def _vpcs(world, *argv, **env) -> list[str]:
    done = world.aws(
        "ec2",
        "describe-vpcs",
        "--query",
        "Vpcs[].VpcId",
        "--output",
        "json",
        *argv,
        **env,
    )
    assert done.code == 0, done.err
    return json.loads(done.out)


def test_each_region_has_its_own_resources(world):
    world.edit(_vpc_in("us-east-1"))
    assert "vpc-0eeeeeeeeeeeeeeee" in _vpcs(world, "--region", "us-east-1")
    assert "vpc-0eeeeeeeeeeeeeeee" not in _vpcs(world, "--region", "us-west-2")


def test_the_region_is_the_flag_then_aws_region_then_the_default_then_the_profile(
    world, tmp_path
):
    world.edit(_vpc_in("us-east-1"))
    east = "vpc-0eeeeeeeeeeeeeeee"
    assert east in _vpcs(world, AWS_REGION="us-east-1")
    assert east not in _vpcs(world, "--region", "us-west-2", AWS_REGION="us-east-1")
    assert east in _vpcs(world, AWS_REGION="", AWS_DEFAULT_REGION="us-east-1")
    config = tmp_path / "config"
    config.write_text("[profile reader]\nregion = us-east-1\n")
    assert east in _vpcs(
        world, AWS_REGION="", AWS_CONFIG_FILE=str(config), AWS_PROFILE="reader"
    )


def test_no_region_is_the_clis_refusal(world):
    done = world.aws("ec2", "describe-vpcs", "--output", "json", AWS_REGION="")
    assert done.code == 253, done
    assert done.err.startswith("You must specify a region.")


def test_a_region_the_state_does_not_hold_is_not_modelled(world):
    done = world.aws(
        "ec2", "describe-vpcs", "--region", "eu-west-1", "--output", "json"
    )
    assert done.code == 99
    assert "region eu-west-1 is not in the state" in world.unknown
    world.clear_logs()


def test_global_services_answer_in_any_region(world):
    for region in ("us-west-2", "us-east-1", "eu-west-1"):
        done = world.aws(
            "iam",
            "list-roles",
            "--region",
            region,
            "--query",
            "Roles[].RoleName",
            "--output",
            "json",
        )
        assert done.code == 0, done.err
        assert "OrganizationAccountAccessRole" in json.loads(done.out)


# --- the three fault modes ----------------------------------------------------------------


def test_access_denied_is_the_services_own_code(world):
    world.fault(mode="AccessDenied")
    expected = {
        ("ec2", "describe-vpcs"): ("UnauthorizedOperation", "DescribeVpcs"),
        ("cloudformation", "list-stacks"): ("AccessDenied", "ListStacks"),
        ("ecs", "list-clusters"): ("AccessDeniedException", "ListClusters"),
        ("sns", "list-topics"): ("AuthorizationError", "ListTopics"),
        ("rds", "describe-db-clusters"): ("AccessDenied", "DescribeDBClusters"),
    }
    for (service, name), (code, operation) in expected.items():
        done = world.aws(service, name, "--output", "json")
        assert done.code == 254, (service, done)
        assert done.out == ""
        assert done.err.startswith(
            f"\naws: [ERROR]: An error occurred ({code}) when calling the {operation}"
            " operation: User: "
        ), done.err


def test_an_error_prints_in_either_form_clis_print(world):
    """aws-cli 2.38.1 (the CI runner's) prefixes a service's error with
    "aws: [ERROR]: "; 2.28.24 (the run's) does not. The fake prints either, so a
    guard can be tested against both."""
    world.fault(mode="AccessDenied", service="sts")
    prefixed = world.aws("sts", "get-caller-identity", "--output", "json")
    world.edit(lambda s: s.update(error_style="plain"))
    plain = world.aws("sts", "get-caller-identity", "--output", "json")
    assert prefixed.err == plain.err.replace(
        "\nAn error", "\naws: [ERROR]: An error", 1
    )
    assert plain.err.startswith(
        "\nAn error occurred (AccessDenied) when calling the GetCallerIdentity operation: "
    )
    assert prefixed.code == plain.code == 254


def test_empty_is_exit_0_with_nothing_printed(world):
    world.fault(mode="EMPTY", service="iam", op="list-roles")
    done = world.aws(
        "iam", "list-roles", "--query", "Roles[].RoleName", "--output", "json"
    )
    assert (done.code, done.out, done.err) == (0, "", "")
    other = world.aws(
        "iam", "list-users", "--query", "Users[].UserName", "--output", "json"
    )
    assert other.out == "[]\n"


def test_noquery_prints_the_whole_response(world):
    world.fault(mode="NOQUERY", service="sts")
    done = world.aws(
        "sts", "get-caller-identity", "--query", "Account", "--output", "text"
    )
    assert done.code == 0
    # The text form of a structure: its scalars in key order on one line.
    account, arn, user = done.out.rstrip("\n").split("\t")
    assert account == ACCOUNT and arn.startswith(
        f"arn:aws:sts::{ACCOUNT}:assumed-role/"
    )
    assert user.endswith(":reader")
    full = world.aws(
        "sts", "get-caller-identity", "--query", "Account", "--output", "json"
    )
    assert set(json.loads(full.out)) == {"UserId", "Account", "Arn"}


def test_a_fault_can_name_one_call_by_number_or_by_match(world):
    world.fault(mode="AccessDenied", call=2)
    world.fault(mode="EMPTY", service="iam", op="list-users", nth=2)
    first = world.aws("iam", "list-users", "--output", "json")
    second = world.aws("iam", "list-users", "--output", "json")  # call 2: denied
    third = world.aws("iam", "list-users", "--output", "json")  # 2nd match left: empty
    fourth = world.aws("iam", "list-users", "--output", "json")
    assert [first.code, second.code, third.code, fourth.code] == [0, 254, 0, 0]
    assert third.out == "" and first.out and fourth.out
    assert [c["n"] for c in world.calls] == [1, 2, 3, 4]
    assert [c["fault"] for c in world.calls] == [None, "AccessDenied", "EMPTY", None]


def test_an_error_fault_can_carry_the_clis_own_words(world):
    words = '\nCould not connect to the endpoint URL: "https://sts.us-west-2.amazonaws.com/"\n'
    world.fault(mode="error", service="sts", stderr=words, exit=255)
    done = world.aws("sts", "get-caller-identity", "--output", "json")
    assert (done.code, done.out, done.err) == (255, "", words)
    world.edit(
        lambda s: s.update(
            faults=[{"mode": "error", "code": "ExpiredToken", "message": "expired"}]
        )
    )
    expired = world.aws("sts", "get-caller-identity", "--output", "json")
    assert expired.code == 254
    assert (
        "(ExpiredToken) when calling the GetCallerIdentity operation: expired"
        in expired.err
    )


# --- the CLI's output ------------------------------------------------------------------


def test_text_prints_a_row_per_line_and_none_for_null(world):
    done = world.aws(
        "kms",
        "list-aliases",
        "--region",
        "us-west-2",
        "--query",
        "Aliases[].[AliasName,TargetKeyId]",
        "--output",
        "text",
    )
    assert done.code == 0, done.err
    lines = done.out.splitlines()
    assert len(lines) == len(accounts.NEW_ACCOUNT_ALIASES)
    assert "alias/aws/s3\tNone" in lines  # a row per line; null is None


def test_json_keeps_null_and_true_text_prints_true(world):
    json_out = world.aws(
        "ec2",
        "describe-vpcs",
        "--query",
        "Vpcs[].[VpcId,IsDefault,Tags]",
        "--output",
        "json",
    )
    assert [row[1:] for row in json.loads(json_out.out)] == [[True, None]]
    text_out = world.aws(
        "ec2",
        "describe-vpcs",
        "--query",
        "Vpcs[].[VpcId,IsDefault,Tags]",
        "--output",
        "text",
    )
    assert text_out.out.endswith("\tTrue\tNone\n")


def test_a_paging_option_given_by_hand_prints_the_page_as_sent(make_world):
    """--max-results is ListUserPools' limit key: the CLI makes one request and
    prints the page as the service sent it, next-page token included (measured
    with aws-cli 2.28.24), where a paged call prints its result keys only."""
    world = make_world(accounts.installed_account())
    world.edit(lambda s: s["regions"]["us-west-2"].update(user_pools_next_token="t2"))
    argv = (
        "cognito-idp",
        "list-user-pools",
        "--region",
        "us-west-2",
        "--max-results",
        "60",
    )
    page = json.loads(world.aws(*argv, "--output", "json").out)
    assert page["NextToken"] == "t2" and page["UserPools"]
    guard = world.aws(
        *argv,
        "--query",
        "NextToken && 'more than one page' || UserPools[].Name",
        "--output",
        "json",
    )
    assert guard.out == '"more than one page"\n'
    world.edit(lambda s: s["regions"]["us-west-2"].pop("user_pools_next_token"))
    names = world.aws(
        *argv,
        "--query",
        "NextToken && 'more than one page' || UserPools[].Name",
        "--output",
        "json",
    )
    assert json.loads(names.out) == ["experimentation-users-staging"]


def test_a_paginated_operation_prints_its_result_keys_only(world):
    done = world.aws("route53", "list-hosted-zones", "--output", "json")
    assert list(json.loads(done.out)) == [
        "HostedZones"
    ]  # no Marker, IsTruncated, MaxItems
    marker = world.aws(
        "route53", "list-hosted-zones", "--query", "IsTruncated", "--output", "json"
    )
    assert marker.out == "null\n"


def test_an_empty_object_prints_nothing_and_an_absent_list_prints_none_as_text(world):
    # CloudFront leaves Items out of an empty list, so the full result is {}.
    whole = world.aws("cloudfront", "list-distributions", "--output", "json")
    assert (whole.code, whole.out) == (0, "")
    ids = world.aws(
        "cloudfront",
        "list-distributions",
        "--query",
        "DistributionList.Items[].Id",
        "--output",
        "json",
    )
    assert ids.out == "null\n"
    text = world.aws(
        "cloudfront",
        "list-distributions",
        "--query",
        "DistributionList.Items[].Id",
        "--output",
        "text",
    )
    assert text.out == "None\n"


def test_an_epoch_timestamp_prints_in_local_time_and_an_iso_one_in_utc(world):
    world.edit(
        lambda s: (
            s["regions"]["us-west-2"]
            .setdefault("secrets", [])
            .append(
                {
                    "Name": "old",
                    "ARN": "arn:x",
                    "DeletedDate": "2026-10-21T05:41:07.512Z",
                }
            )
        )
    )
    argv = (
        "secretsmanager",
        "list-secrets",
        "--include-planned-deletion",
        "--query",
        "SecretList[].DeletedDate",
        "--output",
        "text",
    )
    assert world.aws(*argv, TZ="UTC").out == "2026-10-21T05:41:07.512000+00:00\n"
    assert (
        world.aws(*argv, TZ="Asia/Kolkata").out == "2026-10-21T11:11:07.512000+05:30\n"
    )
    roles = world.aws(
        "iam",
        "list-roles",
        "--query",
        "Roles[0].CreateDate",
        "--output",
        "text",
        TZ="Asia/Kolkata",
    )
    assert roles.out == "2026-09-29T17:02:41+00:00\n"  # IAM is XML: always UTC


def test_family_prefix_takes_a_full_family_name(make_world):
    """N2, measured by the AWS guide's run: "experimentation-" matched nothing."""
    world = make_world(accounts.installed_account())
    argv = ("ecs", "list-task-definitions", "--region", "us-west-2", "--output", "json")
    prefix = world.aws(*argv, "--family-prefix", "experimentation-")
    assert json.loads(prefix.out) == {"taskDefinitionArns": []}
    family = world.aws(*argv, "--family-prefix", "experimentation-migrate-staging")
    assert [
        a.rsplit(":", 1)[1] for a in json.loads(family.out)["taskDefinitionArns"]
    ] == ["1", "2"]


def test_list_certificates_without_includes_lists_rsa_2048_only(make_world):
    world = make_world(accounts.installed_account())
    world.edit(
        lambda s: s["regions"]["us-west-2"]["certificates"].append(
            {
                "CertificateArn": "arn:ec",
                "DomainName": "ec.example.org",
                "Status": "ISSUED",
                "KeyAlgorithm": "EC_prime256v1",
            }
        )
    )
    query = ("--query", "CertificateSummaryList[].DomainName", "--output", "json")
    plain = world.aws("acm", "list-certificates", "--region", "us-west-2", *query)
    every = world.aws(
        "acm",
        "list-certificates",
        "--region",
        "us-west-2",
        "--includes",
        "keyTypes=RSA_2048,EC_prime256v1",
        *query,
    )
    assert json.loads(plain.out) == ["install-test.example.org"]
    assert json.loads(every.out) == ["install-test.example.org", "ec.example.org"]


# --- what it refuses ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, why",
    [
        (("ec2", "delete-vpc", "--vpc-id", "vpc-1"), "operation not modelled"),
        (("ec2", "describe-vpcs", "--filters", "Name=x,Values=y"), "option --filters"),
        (("ec2", "describe-vpcs", "--output", "table"), "--output table"),
        (("iam", "list-policies"), "list-policies without --scope Local"),
    ],
)
def test_anything_unmodelled_exits_99_into_unknown_log(world, argv, why):
    done = world.aws(*argv)
    assert done.code == 99, done
    assert why in world.unknown
    world.clear_logs()


def test_a_response_the_model_refuses_is_never_printed(world):
    world.edit(lambda s: s["regions"]["us-west-2"]["vpcs"][0].update(VpcIdd="vpc-typo"))
    done = world.aws("ec2", "describe-vpcs", "--output", "json")
    assert (done.code, done.out) == (99, "")
    assert "VpcIdd: not a member of Vpc" in world.unknown
    world.clear_logs()
    world.state = accounts.new_account()
    world.edit(lambda s: s["regions"]["us-west-2"]["vpcs"][0].update(IsDefault="true"))
    wrong = world.aws("ec2", "describe-vpcs", "--output", "json")
    assert (wrong.code, wrong.out) == (99, "")
    assert "is not a boolean" in world.unknown
    world.clear_logs()


def test_every_modelled_operation_answers_for_an_installed_account(make_world):
    """Each handler's response passes the model's check, in both regions."""
    world = make_world(accounts.installed_account())
    needs = {
        ("ecs", "list-services"): ("--cluster", "experimentation-staging"),
        ("kms", "describe-key"): ("--key-id", accounts._key_id("database-key")),
        ("cognito-idp", "list-user-pools"): ("--max-results", "60"),
        ("application-autoscaling", "describe-scalable-targets"): (
            "--service-namespace",
            "ecs",
        ),
        ("iam", "list-policies"): ("--scope", "Local"),
    }
    for service, name in fake_aws.modelled_operations():
        if (service, name) == ("backup", "list-recovery-points-by-backup-vault"):
            continue  # the installed account has no vault; its error is asserted below
        done = world.aws(
            service, name, *needs.get((service, name), ()), "--output", "json"
        )
        assert done.code == 0, (service, name, done.err)
    vault = world.aws(
        "backup", "list-recovery-points-by-backup-vault", "--backup-vault-name", "x"
    )
    assert vault.code == 254 and "(ResourceNotFoundException)" in vault.err


# --- the logs and the shim ------------------------------------------------------------------


def test_calls_log_records_variable_names_and_never_values(world):
    world.aws(
        "sts",
        "get-caller-identity",
        "--output",
        "json",
        PLANTED_VALUE_NAME="s3cr3t-value",
    )
    (call,) = world.calls
    assert "PLANTED_VALUE_NAME" in call["env"]
    assert "s3cr3t-value" not in (world.fake_dir / "calls.log").read_text()
    assert {"service", "op", "region", "argv", "exit", "fault"} <= set(call)
    assert not [name for name in call["env"] if name.startswith("FAKE_")]


def test_concurrent_calls_share_the_state_safely(world):
    """The deploy's watcher calls aws while other steps do."""
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        done = list(
            pool.map(
                lambda _: world.aws("sts", "get-caller-identity", "--output", "json"),
                range(24),
            )
        )
    assert {d.code for d in done} == {0}
    assert sorted(c["n"] for c in world.calls) == list(range(1, 25))
    assert world.state["calls"] == 24
