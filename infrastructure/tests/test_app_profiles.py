"""`infrastructure/cdk/app.py` — which stacks each profile builds.

The three module stacks (the real-time counters table, the analytics data lake
and the Glue ETL jobs) were gated on ``ENABLE_MODULE_STACKS``, a variable
nothing in the repository ever set.  ``cdk deploy --all`` from a full checkout
therefore built none of them, while the API went on reporting ``counters`` and
``etl`` installed, the bandit scheduler retried DynamoDB every tick, and the
core monitoring stack kept an alarm on a Kinesis stream nothing had created.

The gate is now the presence of ``modules/infrastructure/cdk/stacks`` — the
same rule ``backend/app/modules_loader.py`` applies to the application — with
``EXPERIMENTLY_PROFILE`` as the explicit override.

``app.py`` is run the way ``cdk synth`` runs it, with ``App.synth`` stubbed out
so the assertions below are about which stacks the app *contains*.

That stub is also why a two-year-old defect shipped.  ``app.synth()`` raised
``DependencyCycle`` between ``experimentation-compute-dev`` and
``experimentation-fargate-dev`` from the day EP-019 added the Fargate stack,
and every test here passed throughout, because none of them ever called it: a
dependency cycle is an app-level, synth-time failure, invisible to
``Template.from_stack`` on one stack at a time.  ``TestTheAppActuallySynthesises``
at the bottom of this file is the test that would have caught it, and is the
regression test for the fix.
"""

from __future__ import annotations

import os
import runpy
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

REPO_ROOT = Path(__file__).resolve().parents[2]
CDK_DIR = REPO_ROOT / "infrastructure" / "cdk"
MODULE_STACKS = {
    "experimentation-dynamodb-counters-dev",
    "experimentation-analytics-dev",
    "experimentation-glue-etl-dev",
}
CORE_STACKS = {
    "experimentation-vpc-dev",
    "experimentation-database-dev",
    "experimentation-fargate-dev",
    "experimentation-monitoring-dev",
}


#: The ALARM_EMAIL every synth here gets (``_app_environment``).
TEST_ALARM_EMAIL = "alerts@controlvar.test"

#: The account and region every synth here deploys to. The account is one of
#: the documented placeholders the repository's account check accepts, and
#: never a real one.
FAKE_ACCOUNT = "111111111111"
FAKE_REGION = "us-west-2"

#: The synth inputs that carry the task definitions' secrets by complete ARN
#: (stacks/secret_arns.py, #636), and the name part each must point at.
SECRET_ARN_INPUTS = {
    "JWT_SECRET_ARN": "jwt-secret",
    "FIRST_SUPERUSER_PASSWORD_SECRET_ARN": "first-superuser-password",
    "AUDIT_HMAC_KEY_SECRET_ARN": "audit-hmac-key",
}

#: The fake six-character suffix each fixture ARN ends in. Letters only, and
#: distinct per secret, so a value in the wrong slot is told apart.
FAKE_SUFFIX = {
    "JWT_SECRET_ARN": "JwtFak",
    "FIRST_SUPERUSER_PASSWORD_SECRET_ARN": "SupFak",
    "AUDIT_HMAC_KEY_SECRET_ARN": "AudFak",
}


def fake_secret_arn(
    variable: str,
    environment: str,
    *,
    account: str = FAKE_ACCOUNT,
    region: str = FAKE_REGION,
) -> str:
    """A complete, fake ARN for ``variable``'s secret in ``environment``."""
    return (
        f"arn:aws:secretsmanager:{region}:{account}:secret:"
        f"/{environment}/experimentation/{SECRET_ARN_INPUTS[variable]}"
        f"-{FAKE_SUFFIX[variable]}"
    )


@contextmanager
def _app_environment(cdk_dir: Path, **overrides: str | None):
    saved_env = dict(os.environ)
    saved_path = list(sys.path)
    saved_modules = {
        k: v for k, v in sys.modules.items() if k.split(".")[0] == "stacks"
    }
    os.environ.update(
        CDK_DEFAULT_ACCOUNT=FAKE_ACCOUNT,
        CDK_DEFAULT_REGION=FAKE_REGION,
        ENVIRONMENT="dev",
        # Required at synth, not at deploy: both ALB listeners are HTTPS and
        # CDK validates that at the end of synthesis, so FargateServiceStack
        # refuses to build without one. Never resolved -- no AWS call is made
        # for an ACM ARN until a real deployment.
        CERTIFICATE_ARN=(
            f"arn:aws:acm:{FAKE_REGION}:{FAKE_ACCOUNT}:certificate/"
            "00000000-0000-0000-0000-000000000000"
        ),
        # Required at synth for the same class of reason as the certificate:
        # FargateServiceStack refuses to build without knowing the origin users
        # reach the service at, because the application refuses to START
        # without it in staging/production (#220). Never resolved at synth.
        PUBLIC_BASE_URL="https://api.example.com",
        # Required at synth in staging and prod (DECISIONS D21): the address
        # every alarm emails. Not an example.* domain, which is refused; `.test`
        # is reserved (RFC 2606) and delivers nowhere, but the rule accepts it.
        # Nothing is sent at synth.
        ALARM_EMAIL=TEST_ALARM_EMAIL,
    )
    os.environ.pop("EXPERIMENTLY_PROFILE", None)
    for name in SECRET_ARN_INPUTS:
        os.environ.pop(name, None)
    # An override of None removes the variable (e.g. ALARM_EMAIL=None: unset).
    for name, value in overrides.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    # Required at synth in EVERY environment (#636): the complete ARN of each
    # secret the API and migration task definitions read. Built for the
    # environment, account and region this synth ends up with, so a staging
    # synth gets staging ARNs. Never resolved: no AWS call is made at synth.
    for name in SECRET_ARN_INPUTS:
        if name not in overrides:
            os.environ[name] = fake_secret_arn(
                name,
                os.environ["ENVIRONMENT"],
                account=os.environ.get("CDK_DEFAULT_ACCOUNT", FAKE_ACCOUNT),
                region=os.environ.get("CDK_DEFAULT_REGION", FAKE_REGION),
            )
    sys.path.insert(0, str(cdk_dir))
    for name in list(sys.modules):
        if name.split(".")[0] == "stacks":
            del sys.modules[name]
    try:
        yield
    finally:
        sys.path[:] = saved_path
        os.environ.clear()
        os.environ.update(saved_env)
        for name in list(sys.modules):
            if name.split(".")[0] == "stacks":
                del sys.modules[name]
        sys.modules.update(saved_modules)


def build(cdk_dir: Path, **env: str) -> tuple[set[str], str]:
    """Run ``app.py`` without synthesising; return ``(stack ids, stdout)``."""
    synth = cdk.App.synth
    cdk.App.synth = lambda self, **kwargs: None  # type: ignore[method-assign]
    try:
        with _app_environment(cdk_dir, **env):
            namespace = runpy.run_path(str(cdk_dir / "app.py"), run_name="__main__")
    finally:
        cdk.App.synth = synth  # type: ignore[method-assign]
    app = namespace["app"]
    profile = "full" if namespace["ENABLE_MODULE_STACKS"] else "core"
    return {
        child.node.id for child in app.node.children if isinstance(child, cdk.Stack)
    }, profile


@pytest.fixture(scope="module")
def core_cdk_dir(tmp_path_factory) -> Path:
    """A copy of the CDK app with no ``modules/`` beside it — a core checkout."""
    root = tmp_path_factory.mktemp("core-checkout")
    shutil.copytree(CDK_DIR, root / "infrastructure" / "cdk")
    return root / "infrastructure" / "cdk"


modules_present = pytest.mark.skipif(
    not (REPO_ROOT / "modules" / "infrastructure" / "cdk" / "stacks").is_dir(),
    reason="core checkout: no modules/infrastructure/cdk/stacks",
)


class TestProfileSelection:
    @pytest.mark.regression
    @modules_present
    def test_a_full_checkout_builds_the_module_stacks(self):
        """`cdk deploy --all` on a full checkout must include all three.

        They were gated on ENABLE_MODULE_STACKS, which nothing set, so every
        deployment silently left out the counters table, the Kinesis/OpenSearch
        data lake and the Glue ETL jobs.
        """
        stacks, profile = build(CDK_DIR)
        assert profile == "full"
        assert MODULE_STACKS <= stacks, sorted(stacks)

    def test_a_core_checkout_builds_none_of_them(self, core_cdk_dir):
        stacks, profile = build(core_cdk_dir)
        assert profile == "core"
        assert not MODULE_STACKS & stacks, sorted(stacks)
        assert CORE_STACKS <= stacks, sorted(stacks)

    @modules_present
    def test_the_profile_variable_overrides_a_full_checkout(self):
        stacks, profile = build(CDK_DIR, EXPERIMENTLY_PROFILE="core")
        assert profile == "core"
        assert not MODULE_STACKS & stacks, sorted(stacks)
        assert CORE_STACKS <= stacks, sorted(stacks)

    def test_asking_for_full_on_a_core_checkout_fails_loudly(self, core_cdk_dir):
        """Not a silent drop to the core set."""
        with pytest.raises(SystemExit) as excinfo:
            build(core_cdk_dir, EXPERIMENTLY_PROFILE="full")
        assert "core checkout" in str(excinfo.value)

    def test_an_unknown_profile_is_rejected(self, core_cdk_dir):
        with pytest.raises(SystemExit) as excinfo:
            build(core_cdk_dir, EXPERIMENTLY_PROFILE="enterprise")
        assert "EXPERIMENTLY_PROFILE must be" in str(excinfo.value)


class TestMonitoringFollowsTheProfile:
    """The Kinesis alarm may only exist where the Kinesis stream does."""

    @staticmethod
    def _classes():
        sys.path.insert(0, str(CDK_DIR))
        try:
            from stacks.monitoring_stack import MonitoringStack
            from stacks.vpc_stack import VpcStack
        finally:
            sys.path.remove(str(CDK_DIR))
        return MonitoringStack, VpcStack

    @classmethod
    def _template(cls, events_stream_name: str | None) -> dict:
        MonitoringStack, VpcStack = cls._classes()
        app = cdk.App()
        vpc_stack = VpcStack(app, "TestVpc")
        stack = MonitoringStack(
            app,
            "TestMonitoring",
            vpc=vpc_stack.vpc,
            # Required (#390): the per-node Redis CPU alarms are named from it.
            redis_replication_group_id="experimentation-redis-dev-redis",
            events_stream_name=events_stream_name,
        )
        return Template.from_stack(stack).to_json()

    @pytest.mark.regression
    @pytest.mark.parametrize("value", ["missing", "", "token"])
    def test_the_redis_replication_group_id_is_required(self, value):
        """No default: the CPU alarms named the literal ``Redis`` for exactly that reason (#390).

        A token (the replication group's ``Ref``) is refused too: it would be a
        cross-stack import, and the id is a literal the Redis stack already has.
        """
        MonitoringStack, VpcStack = self._classes()
        app = cdk.App()
        vpc_stack = VpcStack(app, "TestVpc")
        kwargs = {}
        if value == "":
            kwargs["redis_replication_group_id"] = ""
        elif value == "token":
            kwargs["redis_replication_group_id"] = cdk.Fn.ref("RedisCluster")
        with pytest.raises((TypeError, ValueError)) as excinfo:
            MonitoringStack(app, "TestMonitoring", vpc=vpc_stack.vpc, **kwargs)
        assert "redis_replication_group_id" in str(excinfo.value)

    @staticmethod
    def _kinesis_alarms(template: dict) -> list[dict]:
        return [
            resource
            for resource in template["Resources"].values()
            if resource["Type"] == "AWS::CloudWatch::Alarm"
            and resource["Properties"].get("Namespace") == "AWS/Kinesis"
        ]

    @pytest.mark.regression
    def test_the_core_profile_has_no_alarm_on_a_stream_it_never_creates(self):
        """A core deployment has no Kinesis stream; the alarm watched one anyway.

        `experimentation-events` is a name nothing ever created -- the
        analytics stack calls its stream `exp-events-<id>` -- so the alarm sat
        in INSUFFICIENT_DATA and the dashboard graphed an empty widget.
        """
        template = self._template(None)
        assert [
            resource
            for resource in template["Resources"].values()
            if resource["Type"] == "AWS::CloudWatch::Alarm"
        ], "the monitoring stack lost all of its alarms"
        assert not self._kinesis_alarms(template)
        assert "experimentation-events" not in str(template)

    def test_the_full_profile_keeps_it_on_the_real_stream(self):
        template = self._template("exp-events-abcd1234")
        alarms = self._kinesis_alarms(template)
        assert len(alarms) == 1, sorted(template["Resources"])
        assert alarms[0]["Properties"]["Dimensions"] == [
            {"Name": "StreamName", "Value": "exp-events-abcd1234"}
        ]
        assert "experimentation-events" not in str(template)


class TestTheAppActuallySynthesises:
    """A real ``app.synth()``, which nothing in this repository did before.

    Everything else in this file stubs ``App.synth`` so it can assert on the
    app's *contents*.  That is a different question from "does this app
    synthesise", and the difference is not academic: cross-stack references,
    the stack dependency graph, export generation and the CDK validation
    plugins all run only inside ``synth()``.  ``cdk synth`` -- and therefore
    ``cdk deploy`` -- was impossible for either profile from 2026-03-01 until
    this test was written, and CI was green the whole time.
    """

    @pytest.mark.parametrize("profile", ["core", "full"])
    def test_synth_completes(self, profile: str, core_cdk_dir: Path):
        """`app.synth()` succeeds and yields a template for every stack.

        Asserted on the returned ``CloudAssembly``, not on files: the jsii Node
        process fixes its working directory and environment when it starts, so
        by the time this test runs neither ``CDK_OUTDIR`` nor an ``os.chdir``
        can move where a synth writes. The assembly object has everything worth
        checking and no such constraint.

        The dummy account, region and certificate come from
        ``_app_environment``. Nothing here calls ``from_lookup``, so no
        credentials and no context lookups are needed; the certificate is
        required at *synth* because both ALB listeners are HTTPS, and an ACM
        ARN is never resolved until a deployment.
        """
        cdk_dir = core_cdk_dir if profile == "core" else CDK_DIR

        with _app_environment(cdk_dir):
            # No `App.synth` stub -- calling it for real is the entire point.
            namespace = runpy.run_path(str(cdk_dir / "app.py"), run_name="__main__")

        assembly = namespace["app"].synth()
        produced = {stack.stack_name for stack in assembly.stacks}
        assert produced, f"{profile}: synth produced no stacks"

        # The stack set is pinned EXACTLY, in both directions. It used to be
        # `expected <= produced`, which catches a stack going missing -- the
        # defect the ENABLE_MODULE_STACKS gate produced -- but says nothing
        # about a stack that should not be there. An app that quietly grows a
        # deploy target is the same class of problem: `experimentation-api-dev`
        # built an API Gateway, a second Cognito user pool and three
        # placeholder Lambdas for months, referenced by nothing (#178).
        expected = CORE_STACKS | {
            "experimentation-auth-dev",
            "experimentation-compute-dev",
            "experimentation-redis-dev",
            "experimentation-dynamodb-dev",
            "experimentation-migrations-dev",
        }
        if profile == "full":
            expected |= MODULE_STACKS

        assert produced == expected, (
            f"{profile}: missing {sorted(expected - produced)}, "
            f"unexpected {sorted(produced - expected)}"
        )

        # Every stack carries a real template -- synth having "succeeded" with
        # an empty one would mean nothing.
        for stack in assembly.stacks:
            assert stack.template.get("Resources"), (
                f"{profile}: {stack.stack_name} synthesised no resources"
            )

    @pytest.mark.regression
    def test_the_load_balancer_can_still_reach_the_tasks(self):
        """The ALB -> task ingress survives, and is written by the fargate stack.

        This is the half of the cycle fix that fails *silently*.  Importing the
        compute stack's security group with ``mutable=False`` is what breaks the
        cycle, and it also makes CDK decline -- with no warning and no
        annotation -- to write the rule that
        ``attach_to_application_target_group`` would otherwise have added.  The
        explicit ``CfnSecurityGroupIngress`` in ``fargate_service_stack.py``
        puts it back.

        Delete that construct and everything still synthesises and every
        other test here still passes, but nothing lets the load balancer in:
        ``compute_stack.py`` no longer opens 0.0.0.0/0 on 8000, so every
        ``/api/v1`` and ``/health`` request through the load balancer fails
        while the dashboard at ``/`` keeps working.  So the rule is asserted
        here rather than left to be inferred from a green synth.  Its egress
        half, on the load balancer's group, is pinned for every environment,
        profile and live colour by ``test_alb_egress_to_tasks.py`` (#801).
        """
        with _app_environment(CDK_DIR):
            namespace = runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")
        assembly = namespace["app"].synth()

        fargate = next(
            s for s in assembly.stacks if s.stack_name == "experimentation-fargate-dev"
        )
        ingress = [
            props
            for res in fargate.template["Resources"].values()
            if res["Type"] == "AWS::EC2::SecurityGroupIngress"
            for props in [res.get("Properties", {})]
            if props.get("FromPort") == 8000 and props.get("ToPort") == 8000
        ]
        assert len(ingress) == 1, (
            "expected exactly one ALB->task ingress rule on port 8000 in "
            f"experimentation-fargate-dev, found {len(ingress)}"
        )

        rule = ingress[0]
        assert rule.get("IpProtocol") == "tcp", rule
        # Sourced from a security group, never a CIDR: a rule that widened to
        # 0.0.0.0/0 here would pass a "the rule exists" check and be wrong.
        assert "SourceSecurityGroupId" in rule, rule
        assert "CidrIp" not in rule, rule
        # And it targets the compute stack's group, which is why the rule has
        # to be written from this side at all.
        assert "GroupId" in rule, rule

    @pytest.mark.regression
    def test_two_synths_of_the_same_source_agree(self):
        """Synthesising twice produces byte-identical templates.

        ``analytics_stack.py`` built every physical name it had from
        ``random.choices(...)`` evaluated at synth time, so this assertion
        failed on six resources: the Kinesis stream, the S3 data lake, the
        Firehose delivery stream, the OpenSearch domain, and a Glue database and
        crawler that duplicated ``glue_etl_stack.py``'s pair.

        What that cost is not the template churn but what CloudFormation does
        with it: changing a physical name is a **replacement**, so every
        ``cdk deploy`` -- including a deploy of no change at all -- tore down the
        data lake and built a new one, and ``RemovalPolicy.RETAIN`` on the bucket
        meant the old one was kept rather than deleted, with nothing pointing at
        it. ``exp-data-hdv7dl4h-us-west-2`` in this account is one of those.

        The full profile is the one asserted because the affected stacks are all
        module stacks; the core profile has no analytics at all. Compared as
        parsed JSON, not as text, so key order cannot make this flap.
        """
        templates = []
        for _ in range(2):
            with _app_environment(CDK_DIR):
                namespace = runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")
            assembly = namespace["app"].synth()
            templates.append({s.stack_name: s.template for s in assembly.stacks})

        first, second = templates
        assert first.keys() == second.keys(), "the two synths built different stacks"

        differing = sorted(name for name in first if first[name] != second[name])
        assert not differing, (
            "these stacks are not deterministic across synths, so every "
            f"`cdk deploy` replaces resources in them: {differing}"
        )

    def test_the_glue_catalog_is_defined_once(self):
        """One Glue database and one crawler across the whole app, not two.

        ``analytics_stack`` and ``glue_etl_stack`` each created a database and a
        crawler, both crawling ``s3://<the same data lake>/raw/events/`` on a
        schedule -- the analytics one hourly. Only ``glue_etl_stack``'s pair is
        referenced, by its two ETL jobs through ``GLUE_DATABASE_NAME``, so the
        other maintained a catalog nothing queried and billed per DPU-hour to
        do it.
        """
        with _app_environment(CDK_DIR):
            namespace = runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")
        assembly = namespace["app"].synth()

        found = {"AWS::Glue::Database": [], "AWS::Glue::Crawler": []}
        for stack in assembly.stacks:
            for logical_id, resource in stack.template.get("Resources", {}).items():
                if resource["Type"] in found:
                    found[resource["Type"]].append(f"{stack.stack_name}/{logical_id}")

        for resource_type, instances in found.items():
            assert len(instances) == 1, (
                f"expected exactly one {resource_type} in the app, found {instances}"
            )
