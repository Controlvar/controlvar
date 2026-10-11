"""The dashboard is deployed after the API and rolled back with it, by digest (#69).

`deploy.yml` builds the dashboard image with the API's, before anything is
migrated; after the API is serving and answering it registers the dashboard's
task definition by digest, checks the API is STILL serving this run's revision
(the race guard), rolls the dashboard out with `scripts/ecs_rolling_rollout.sh
--expect-primary <what it was serving>`, smoke-tests it, and checks the API
once more as the last assertion. `rollback.yml` takes an optional
`dashboard_task_definition_arn`, validated before anything changes by the same
digest predicate the deploy summary prints it by.

Most of these tests RUN the workflow's own `run:` scripts, taken from the YAML
as written, under `bash --noprofile --norc -eo pipefail` (what the runner runs),
with a fake `aws` and `curl` first on PATH and the step's `env:` resolved from
the YAML, so a change to a step's text or its env wiring is what gets tested.
No test here reaches AWS: the fakes answer from a scenario, the subprocesses get
no credentials, and the repository's conftest puts a refusing `aws` behind them.

Nothing measures elapsed time: every poll interval is 0.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from backend.tests.unit.infrastructure.test_ecs_rolling_rollout import (
    DONE,
    IN_PROGRESS,
    UPDATED,
    breaker_read,
    dep,
    svc,
)
from backend.tests.unit.infrastructure.test_ecs_rolling_rollout import (
    NEW as DASH_NEW,
)
from backend.tests.unit.infrastructure.test_ecs_rolling_rollout import (
    OLD as DASH_OLD,
)
from backend.tests.unit.infrastructure.test_ecs_rolling_rollout import (
    THIRD as DASH_THIRD,
)
from backend.tests.unit.infrastructure.test_forward_deploy_completes import (
    AFTER as API_AFTER,
)
from backend.tests.unit.infrastructure.test_forward_deploy_completes import (
    BEFORE as API_BEFORE,
)
from backend.tests.unit.infrastructure.test_forward_deploy_completes import (
    NEW as API_NEW,
)
from backend.tests.unit.infrastructure.test_forward_deploy_completes import (
    RULES_BLUE,
    RULES_GREEN,
    _fixture,
    _split,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DEPLOY = WORKFLOWS / "deploy.yml"
ROLLBACK = WORKFLOWS / "rollback.yml"
SCRIPTS = REPO_ROOT / "scripts"

pytestmark = pytest.mark.skipif(
    not WORKFLOWS.is_dir(), reason="this tree has no .github/workflows"
)

ENV = "staging"
REGISTRY = "123456789012.dkr.ecr.us-west-2.amazonaws.com"
WEB_DIGEST = "sha256:" + "a" * 64
OLD_WEB_DIGEST = "sha256:" + "b" * 64
WEB_IMAGE = f"{REGISTRY}/experimentation-platform/web@{WEB_DIGEST}"
OLD_WEB_IMAGE = f"{REGISTRY}/experimentation-platform/web@{OLD_WEB_DIGEST}"
TAG_WEB_IMAGE = f"{REGISTRY}/experimentation-platform/web:v1.2.3-full"
BOOTSTRAP_WEB_IMAGE = f"{REGISTRY}/experimentation-platform/web:bootstrap"
API_FAMILY_REVISION = (
    "arn:aws:ecs:us-west-2:123456789012:task-definition/"
    "experimentation-backend-staging:42"
)


# --- the workflow, as data ------------------------------------------------------


def _load(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["on"] = document.pop(True, document.get("on"))
    return document


def _job(path: Path) -> dict[str, Any]:
    (job,) = [j for j in _load(path)["jobs"].values() if "environment" in j]
    return job


def _steps(path: Path) -> list[dict[str, Any]]:
    return _job(path)["steps"]


def _step(path: Path, key: str) -> dict[str, Any]:
    (step,) = [s for s in _steps(path) if key in (s.get("id"), s.get("name"))]
    return step


def _index(path: Path, key: str) -> int:
    (index,) = [
        i for i, s in enumerate(_steps(path)) if key in (s.get("id"), s.get("name"))
    ]
    return index


def _code(script: str) -> str:
    return "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )


# --- a fake aws, a fake curl, and a step runner -------------------------------------

#: The fake `aws`: logs every call, answers from the scenario's rules.
#:
#: Before the rules, `deploy create-deployment` with the alarm override has to
#: meet a contract (#777). The fake used to accept any value, and the staging
#: rollback rehearsal (run 37139163352) was the first thing to see CodeDeploy
#: refuse the shorthand `enabled=false` -- which the CLI sends as
#: `{"enabled": false}`, with no `alarms` key -- with "InvalidAlarmConfigException
#: ... Alarm list cannot be null". Three layers, refused with the CLI's
#: service-error status, 254:
#:
#: 1. the value must be JSON. A policy of this harness, not an API rule: it does
#:    not reimplement the CLI's shorthand parser, the notation that hid #777;
#: 2. botocore's ParamValidator against the model's AlarmConfiguration shape
#:    (types: `"enabled":"nope"`, `"alarms":null`). The model marks no member
#:    required, so this alone accepts `{"enabled": false}`;
#: 3. the server rules the model's InvalidAlarmConfigException documentation
#:    lists: the alarm list is present and a list; every alarm is an object with
#:    a name of 1-255 characters; no two alarms share a name; an enabled
#:    configuration has a non-empty list.
#:
#: Layer 3 is a copy of documented rules, not an oracle: CodeDeploy may refuse
#: something it lets through. Whether CodeDeploy accepts `"alarms": []` with
#: `"enabled": false` is shown only by a staging run.
#:
#: CodeDeploy's own revert (#783), only when the scenario writes `revert.json`
#: ({"d", "length", "latency", "id"}): a virtual clock in `vclock` that every
#: call advances by `latency` (the call lands half-way through it) and the
#: test's `sleep` by its argument. `stop-deployment` starts a revert `d`
#: seconds after it lands, active for `length` seconds (`d` null: no revert).
#: After the stop, `list-deployments` answers from that state, not from the
#: rules, and `create-deployment` is refused, with the staging text, while the
#: revert is active. The refusal is keyed on the revert, not on what the
#: workflow last listed, so a workflow that never lists again still meets it.
#:
#: A rule made with `query=True` (#816) answers a describe-services document,
#: and the fake evaluates the call's `--query` on it with jmespath, as the CLI
#: does, so the workflow's own query is what is tested. Every other rule's
#: answer is printed as given. It prints the result the way aws-cli 2.28.24
#: was measured to print it (test_the_fakes_query_mode_prints_what_the_cli_prints):
#: `--output text` tab-joins a flat list, a None is `None`, a float keeps its
#: `.0`; `--output json` is indented by four. A jmespath error exits 255 with
#: its message on stderr, as the CLI does. Anything else -- a nested list, a
#: dict, an empty list, another --output -- exits 98: the CLI's text output
#: for those was not measured (it reorders nested lists), so the fake refuses
#: rather than guess.
#:
#: Each `deploy stop-deployment` call first copies the step's GITHUB_OUTPUT as
#: it stands to `snap.<n>` in the state directory (`Runner.snapshots()`), so a
#: test can tell an output written before the call from one written after it
#: (#759 gate 2a): the final file cannot.
FAKE_AWS = r"""#!{python}
import json, os, shutil, sys
args = sys.argv[1:]
state = os.environ["FAKE_AWS_STATE"]
with open(os.path.join(state, "calls.log"), "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:2] == ["deploy", "stop-deployment"] and os.environ.get("GITHUB_OUTPUT"):
    taken = len([f for f in os.listdir(state) if f.startswith("snap.")])
    shutil.copyfile(os.environ["GITHUB_OUTPUT"], os.path.join(state, "snap.%d" % taken))

REVERT = os.path.join(state, "revert.json")
revert = json.load(open(REVERT)) if os.path.exists(REVERT) else None
revert_active = False
if revert is not None:
    clock = os.path.join(state, "vclock")
    now = float(open(clock).read()) if os.path.exists(clock) else 0.0
    open(clock, "w").write(repr(now + revert["latency"]))
    now += revert["latency"] / 2
    if args[:2] == ["deploy", "stop-deployment"] and "stop" not in revert:
        revert["stop"] = now
        json.dump(revert, open(REVERT, "w"))
    if "stop" in revert and revert["d"] is not None:
        start = revert["stop"] + revert["d"]
        revert_active = start <= now < start + revert["length"]
    if args[:2] == ["deploy", "list-deployments"] and "stop" in revert:
        print(revert["id"] if revert_active else "")
        sys.exit(0)


def refuse_alarm_config(reason):
    print(
        "An error occurred (InvalidAlarmConfigException) when calling the "
        "CreateDeployment operation: " + reason,
        file=sys.stderr,
    )
    sys.exit(254)


OVERRIDE = "--override-alarm-configuration"
if args[:2] == ["deploy", "create-deployment"] and OVERRIDE in args:
    position = args.index(OVERRIDE) + 1
    raw = args[position] if position < len(args) else ""
    try:
        value = json.loads(raw)
    except ValueError:
        refuse_alarm_config("fake: the override value is not JSON: %r" % raw)
    from botocore.session import get_session
    from botocore.validate import ParamValidator

    shape = (
        get_session()
        .get_service_model("codedeploy")
        .operation_model("CreateDeployment")
        .input_shape.members["overrideAlarmConfiguration"]
    )
    report = ParamValidator().validate(value, shape)
    if report.has_errors():
        refuse_alarm_config("fake: " + report.generate_report())
    alarms = value.get("alarms")
    if not isinstance(alarms, list):
        refuse_alarm_config("Alarm list cannot be null.")
    names = []
    for alarm in alarms:
        if not isinstance(alarm, dict):
            refuse_alarm_config("The alarm object is null.")
        name = alarm.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= 255:
            refuse_alarm_config(
                "The alarm name is empty or null or exceeds the limit of 255 "
                "characters."
            )
        if name in names:
            refuse_alarm_config("Two alarms with the same name have been specified.")
        names.append(name)
    if value.get("enabled") is True and not alarms:
        refuse_alarm_config(
            "The alarm configuration is enabled, but the alarm list is empty."
        )

if args[:2] == ["deploy", "create-deployment"] and revert_active:
    print(
        "An error occurred (DeploymentLimitExceededException) when calling the "
        "CreateDeployment operation: The Deployment Group 'experimentation-staging "
        "(id=fake)' already has an active Deployment '%s'" % revert["id"],
        file=sys.stderr,
    )
    sys.exit(254)

def query_mode(document):
    def refuse(why):
        print("fake aws: query mode " + why, file=sys.stderr)
        sys.exit(98)

    if "--query" not in args:
        refuse("needs --query")
    expression = args[args.index("--query") + 1]
    output = args[args.index("--output") + 1] if "--output" in args else "json"
    import jmespath
    from jmespath.exceptions import JMESPathError

    try:
        result = jmespath.search(expression, document)
    except JMESPathError as error:
        print("\n" + str(error), file=sys.stderr)
        sys.exit(255)

    def scalar(value):
        if isinstance(value, bool):
            return False  # not measured
        return value is None or isinstance(value, (str, int, float))

    flat = scalar(result) or (
        isinstance(result, list) and result and all(scalar(v) for v in result)
    )
    if not flat:
        refuse("does not print this result (not measured): %r" % (result,))
    if output == "json":
        print(json.dumps(result, indent=4))
    elif output == "text":
        values = result if isinstance(result, list) else [result]
        print("\t".join("None" if v is None else str(v) for v in values))
    else:
        refuse("does not print --output %s" % output)
    sys.exit(0)


rules = json.load(open(os.path.join(state, "rules.json")))
line = " ".join(args)
for i, rule in enumerate(rules):
    if all(m in line for m in rule["match"]):
        path = os.path.join(state, "rule%d.count" % i)
        n = int(open(path).read()) if os.path.exists(path) else 0
        open(path, "w").write(str(n + 1))
        answers = rule["answers"]
        answer = answers[min(n, len(answers) - 1)]
        if isinstance(answer, dict) and set(answer) == {"error"}:
            print(answer["error"], file=sys.stderr)
            sys.exit(254)
        if rule.get("query"):
            query_mode(answer)
        print(answer if isinstance(answer, str) else json.dumps(answer))
        sys.exit(0)
print("fake aws: unexpected call " + line, file=sys.stderr)
sys.exit(99)
"""

#: `curl -sS -o FILE -w FORMAT --max-time N URL`: writes the canned body to
#: FILE and prints the canned `-w` answer ("200 text/html" or "401").
FAKE_CURL = r"""#!{python}
import os, sys
args = sys.argv[1:]
state = os.environ["FAKE_AWS_STATE"]
with open(os.path.join(state, "curl.log"), "a") as log:
    log.write(" ".join(args) + "\n")
body = open(os.path.join(state, "curl.body")).read()
out = args[args.index("-o") + 1]
open(out, "w").write(body)
sys.stdout.write(open(os.path.join(state, "curl.w")).read())
"""


def rule(*match: str, answers: list, query: bool = False) -> dict:
    """``query=True``: each answer is a document the call's --query is
    evaluated on (FAKE_AWS's query mode), not the printed answer."""
    made: dict[str, Any] = {"match": list(match), "answers": answers}
    if query:
        made["query"] = True
    return made


def _task_definition(arn: str, image: str, family: str | None = None) -> dict:
    name = arn.rsplit("/", 1)[-1]
    return {
        "taskDefinition": {
            "taskDefinitionArn": arn,
            "family": family or name.split(":")[0],
            "revision": int(name.split(":")[1]),
            "status": "ACTIVE",
            "containerDefinitions": [{"name": "dashboard", "image": image}],
        }
    }


def api_rules(services: dict | list = API_AFTER, rules: dict = RULES_GREEN) -> list:
    """What scripts/api_serving.py reads. Serving `API_NEW` by default."""
    return [
        rule(
            "ecs describe-services",
            "experimentation-backend-staging",
            answers=services if isinstance(services, list) else [services],
        ),
        rule(
            "cloudformation describe-stack-resources",
            answers=[_fixture("stack-resources.json")],
        ),
        rule(
            "elbv2 describe-listeners",
            answers=[_fixture("listener-default-dashboard.json")],
        ),
        rule("elbv2 describe-rules", answers=[rules]),
    ]


def dashboard_rules(
    polls: list,
    before: dict | None = None,
    update: dict | str | None = None,
    serving_image: str = OLD_WEB_IMAGE,
) -> list:
    """What the rollout script and the register script read and write."""
    update_answer: Any = update or UPDATED
    return [
        rule(
            "ecs describe-services",
            "experimentation-dashboard-staging",
            answers=[
                {"services": [s], "failures": []}
                for s in [before or svc(dep(DASH_OLD, "ecs-svc/6666")), *polls]
            ],
        ),
        rule("ecs update-service", answers=[update_answer]),
        rule("ecs register-task-definition", answers=[DASH_NEW]),
        rule(
            "ecs describe-task-definition",
            "containerDefinitions[?name=='dashboard']",
            answers=[WEB_IMAGE],
        ),
        rule(
            "ecs describe-task-definition",
            "containerDefinitions[0].image",
            answers=[serving_image],
        ),
        rule(
            "ecs describe-task-definition",
            "experimentation-dashboard-staging --query taskDefinition --output json",
            answers=[_task_definition(DASH_OLD, OLD_WEB_IMAGE)["taskDefinition"]],
        ),
        rule("ecs list-tasks", answers=["None"]),
    ]


def _no_aws_credentials(env: dict) -> dict:
    env = {k: v for k, v in env.items() if not k.startswith("AWS_")}
    env.update(
        AWS_CONFIG_FILE="/nonexistent/aws-config",
        AWS_SHARED_CREDENTIALS_FILE="/nonexistent/aws-credentials",
        AWS_EC2_METADATA_DISABLED="true",
    )
    return env


_EXPR = re.compile(r"\$\{\{\s*([^}]*?)\s*\}\}")


class Runner:
    """Runs a workflow step's `run:` script the way the runner does.

    ``shell_flags`` are bash's flags. The default, ``-eo pipefail``, is
    stricter than GitHub's ``bash -e {0}``; a test that must match the runner
    sets ``("-e",)``. ``raw_outputs`` keeps each step's GITHUB_OUTPUT text as
    written, keyed by step id (or name), because ``run()``'s dict keeps only
    the last value of a repeated key."""

    def __init__(self, tmp_path: Path):
        self.shell_flags: tuple[str, ...] = ("-eo", "pipefail")
        self.raw_outputs: dict[str, str] = {}
        self.root = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, text in (("aws", FAKE_AWS), ("curl", FAKE_CURL)):
            fake = self.bin / name
            fake.write_text(text.replace("{python}", sys.executable))
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        # `python3` in a step is this interpreter.
        (self.bin / "python3").symlink_to(sys.executable)
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.work = tmp_path / "work"
        self.work.mkdir()
        (self.work / "scripts").symlink_to(SCRIPTS)
        self.curl("200 text/html; charset=utf-8", "<html>dashboard</html>")

    def scenario(self, rules: list) -> None:
        (self.state / "rules.json").write_text(json.dumps(rules))
        for stale in [*self.state.glob("rule*.count"), *self.state.glob("snap.*")]:
            stale.unlink()
        (self.state / "calls.log").unlink(missing_ok=True)

    def curl(self, w: str, body: str = "") -> None:
        (self.state / "curl.w").write_text(w)
        (self.state / "curl.body").write_text(body)

    def calls(self) -> list[list[str]]:
        log = self.state / "calls.log"
        if not log.exists():
            return []
        return [json.loads(x) for x in log.read_text().splitlines()]

    def snapshots(self) -> list[str]:
        """GITHUB_OUTPUT's text at each `deploy stop-deployment` call, in order."""
        snaps = sorted(
            self.state.glob("snap.*"), key=lambda p: int(p.name.split(".")[1])
        )
        return [p.read_text() for p in snaps]

    def updates(self) -> list[list[str]]:
        return [c for c in self.calls() if c[:2] == ["ecs", "update-service"]]

    def env_for(
        self,
        path: Path,
        step: dict,
        outputs: dict[str, dict[str, str]],
        inputs: dict[str, str],
        overrides: dict[str, str],
    ) -> dict[str, str]:
        document = _load(path)
        values: dict[str, str] = {}

        def resolve(value: Any) -> str:
            def one(match: re.Match) -> str:
                expr = match.group(1)
                if expr == "inputs.environment":
                    return ENV
                if expr.startswith("inputs."):
                    return inputs.get(expr.split(".", 1)[1], "")
                if expr.startswith("vars."):
                    return inputs.get(expr, "")
                if expr == "job.status":
                    return inputs.get("job.status", "success")
                m = re.fullmatch(r"steps\.([\w-]+)\.(outputs\.([\w-]+)|outcome)", expr)
                if m:
                    if m.group(3):
                        return outputs.get(m.group(1), {}).get(m.group(3), "")
                    return outputs.get(m.group(1), {}).get("__outcome__", "")
                if expr.startswith(("needs.", "env.")):
                    return inputs.get(expr, "")
                # A job env that says whether a secret is set (SLACK_ON):
                # 'true' only when the test passes `secrets.NAME` itself.
                m = re.fullmatch(r"secrets\.(\w+) != ''", expr)
                if m:
                    return "true" if inputs.get(expr.split(" ", 1)[0]) else "false"
                raise AssertionError(
                    f"the test runner cannot resolve ${{{{ {expr} }}}}"
                )

            return _EXPR.sub(one, str(value))

        for scope in (document.get("env", {}), _job(path).get("env", {})):
            for key, value in scope.items():
                values[key] = resolve(value)
        for key, value in (step.get("env") or {}).items():
            values[key] = resolve(value)
        values.update(overrides)
        return values

    def run(
        self,
        path: Path,
        step: dict,
        outputs: dict[str, dict[str, str]] | None = None,
        inputs: dict[str, str] | None = None,
        **overrides: str,
    ) -> tuple[int, str, dict[str, str], str]:
        """(exit, stdout+stderr, GITHUB_OUTPUT values, GITHUB_STEP_SUMMARY)."""
        outputs = outputs or {}
        inputs = {"vars.PUBLIC_BASE_URL": "https://app.example.com", **(inputs or {})}
        env = self.env_for(path, step, outputs, inputs, overrides)
        script = self.root / "step.sh"
        script.write_text(step["run"])
        output = self.root / "github_output"
        output.write_text("")
        summary = self.root / "github_step_summary"
        summary.write_text("")
        base = _no_aws_credentials(dict(os.environ))
        base.update(env)
        base.update(
            PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            FAKE_AWS_STATE=str(self.state),
            GITHUB_OUTPUT=str(output),
            GITHUB_STEP_SUMMARY=str(summary),
            GITHUB_ENV=str(self.root / "github_env"),
        )
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", *self.shell_flags, str(script)],
            cwd=self.work,
            env=base,
            capture_output=True,
            text=True,
            timeout=60,
        )
        raw = output.read_text()
        self.raw_outputs[step.get("id") or step["name"]] = raw
        written = {}
        for line in raw.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                written[key] = value
        return (
            result.returncode,
            result.stdout + result.stderr,
            written,
            summary.read_text(),
        )


@pytest.fixture
def runner(tmp_path):
    return Runner(tmp_path)


#: Fast polling for every rollout a test runs (the workflow's own values are
#: 15 s and 1200 s): the verdict, not the wait, is under test.
FAST = {
    "DASHBOARD_ROLLOUT_POLL_SECONDS": "0",
    "DASHBOARD_ROLLOUT_DEADLINE_SECONDS": "3",
}

#: The outputs of the steps before the dashboard's, as a successful run has them.
BEFORE_DASHBOARD = {
    "api-td": {"arn": API_NEW, "__outcome__": "success"},
    "api-serving": {"__outcome__": "success", "result": "serving"},
    "codedeploy": {"id": "d-ABCDEF123", "__outcome__": "success"},
    "previous": {"arn": API_FAMILY_REVISION, "__outcome__": "success"},
    "previous-dashboard": {
        "arn": DASH_OLD,
        "image": OLD_WEB_IMAGE,
        "release_arn": DASH_OLD,
        "digest": OLD_WEB_DIGEST,
        "__outcome__": "success",
    },
    "web-image": {
        "image": WEB_IMAGE,
        "digest": WEB_DIGEST,
        "tag": "v1.2.3-full",
        "reused": "false",
        "__outcome__": "success",
    },
    "dashboard-td": {"arn": DASH_NEW, "__outcome__": "success"},
}


def _rollout(runner: Runner, api: list, dashboard: list, **outputs_changes):
    runner.scenario(api + dashboard)
    outputs = {k: dict(v) for k, v in BEFORE_DASHBOARD.items()}
    for key, value in outputs_changes.items():
        outputs[key.replace("_", "-")].update(value)
    return runner.run(DEPLOY, _step(DEPLOY, "dashboard-rollout"), outputs, **FAST)


# --- R10: the API half of the race guard -----------------------------------------------


@pytest.mark.regression
def test_r1_through_the_step_the_dashboard_rolls_out_when_the_api_is_serving(runner):
    code, out, written, _ = _rollout(
        runner, api_rules(), dashboard_rules([IN_PROGRESS, DONE])
    )
    assert code == 0, out
    assert written["api_guard"] == "0"
    assert written["outcome"] == "completed"
    (update,) = runner.updates()
    assert update[update.index("--task-definition") + 1] == DASH_NEW


@pytest.mark.regression
@pytest.mark.parametrize(
    "api, expected, words",
    [
        (api_rules(API_BEFORE, RULES_BLUE), "1", "another revision is PRIMARY"),
        (api_rules(API_AFTER, _split(50, 50)), "1", "a traffic shift is in progress"),
        (
            api_rules()[:-1]
            + [
                rule("elbv2 describe-rules", answers=[{"error": "ThrottlingException"}])
            ],
            "2",
            "could not tell whether the API is serving",
        ),
        (api_rules(API_AFTER, RULES_BLUE), "3", "route and its tasks disagree"),
    ],
    ids=["exit1-other-primary", "exit1-shifting", "exit2-unknown", "exit3-wrong"],
)
def test_r10_the_dashboard_is_not_touched_unless_the_api_serves_this_revision(
    runner, api, expected, words
):
    """R10 (EM v1 condition 2; PE spec-delta 3 and 8): api_serving.py exits 1,
    2 or 3 and there is no update-service call at all; the step prints the
    script's own line and a wording that claims no cause the code lacks."""
    code, out, written, _ = _rollout(runner, api, dashboard_rules([IN_PROGRESS, DONE]))
    assert code == 1, out
    assert runner.updates() == [], "the dashboard was changed after a refused guard"
    assert written["api_guard"] == expected
    assert words in out
    assert "The dashboard was not changed: it is still on " in out
    assert "experimentation-dashboard-staging:6" in out
    # api_serving.py's own line is printed and handed on to the summary.
    label = {"1": "NOT YET:", "2": "UNKNOWN:", "3": "WRONG:"}[expected]
    assert label in out and written["api_line"].startswith(label)


@pytest.mark.regression
def test_r11_the_dashboard_is_not_touched_when_it_changed_since_it_was_read(runner):
    """R11: something else put a third revision on the dashboard since this
    run recorded it. --expect-primary refuses before update-service."""
    moved = svc(dep(DASH_THIRD, "ecs-svc/8888"))
    code, out, written, _ = _rollout(
        runner, api_rules(), dashboard_rules([DONE], before=moved)
    )
    assert code == 1, out
    assert runner.updates() == []
    assert written["outcome"] == "changed by another run"


@pytest.mark.regression
def test_the_expect_primary_value_is_the_revision_arn_not_the_digest(runner):
    """PE spec-delta 4: (c) outputs the ARN and the digest under different
    names. Wired to the digest, the rollout refuses it as not an ARN."""
    step = _step(DEPLOY, "dashboard-rollout")
    assert step["env"]["PREVIOUS_DASHBOARD_TD"] == (
        "${{ steps.previous-dashboard.outputs.arn }}"
    )
    runner.scenario(api_rules() + dashboard_rules([DONE]))
    outputs = {k: dict(v) for k, v in BEFORE_DASHBOARD.items()}
    outputs["previous-dashboard"]["arn"] = OLD_WEB_DIGEST  # the planted miswiring
    code, out, _, _ = runner.run(DEPLOY, step, outputs, **FAST)
    assert code == 1
    assert (
        "--expect-primary 'sha256:" in out and "is not a registered revision ARN" in out
    )
    assert runner.updates() == []


@pytest.mark.regression
def test_the_guard_calls_the_serving_predicate_with_the_real_names():
    """PE spec-delta 1: the API service, and this run's api-td ARN."""
    for key in ("dashboard-rollout", "api-recheck"):
        step = _step(DEPLOY, key)
        code = _code(step["run"])
        calls = re.findall(
            r"\$\(python3 scripts/api_serving\.py ([^)\n]*?) 2>&1\)", code
        )
        assert calls == ['"$ECS_CLUSTER" "$ECS_BACKEND_SERVICE" "$API_TD_ARN"'], (
            key,
            calls,
        )
        assert step["env"]["API_TD_ARN"] == "${{ steps.api-td.outputs.arn }}", key
    job_env = _job(DEPLOY)["env"]
    assert job_env["ECS_BACKEND_SERVICE"] == (
        "experimentation-backend-${{ inputs.environment }}"
    )


@pytest.mark.regression
def test_the_guard_and_the_rollout_are_one_step_and_nothing_swallows_a_status():
    """PE spec-delta 3-5: no `if:` between guard and mutation, and no
    `|| true`, pipe or continue-on-error around a guard."""
    step = _step(DEPLOY, "dashboard-rollout")
    code = _code(step["run"])
    assert code.index("scripts/api_serving.py") < code.index(
        "scripts/ecs_rolling_rollout.sh"
    )
    assert "if" not in step, "the guard step must run whenever the steps before passed"
    guards = [
        (DEPLOY, "previous-dashboard"),
        (DEPLOY, "dashboard-td"),
        (DEPLOY, "dashboard-rollout"),
        (DEPLOY, "api-recheck"),
        (ROLLBACK, "dashboard-target"),
        (ROLLBACK, "dashboard-rollback"),
    ]
    for path, key in guards:
        step = _step(path, key)
        text = _code(step["run"])
        assert "continue-on-error" not in step, key
        assert not re.search(r"\|\|\s*true\b", text), f"{key}: || true"
        assert not re.search(r"(?<!\|)\|(?!\|)", text), (
            f"{key}: a pipe replaces a status with the last command's"
        )
        assert "set -euo pipefail" in text, key


# --- the post-(g) re-check: the last assertion (PE spec-delta 5) ----------------------


def _tail_of_the_job(runner: Runner, rules: list, drop: str | None = None):
    """Run every step from the dashboard's registration to the end of the job,
    with the runner's `if:` rules; return (job status, outputs, log)."""
    runner.scenario(rules)
    steps = _steps(DEPLOY)
    start = _index(DEPLOY, "dashboard-td")
    outputs = {k: dict(v) for k, v in BEFORE_DASHBOARD.items() if k != "dashboard-td"}
    failed = False
    log = []
    for step in steps[start:]:
        if drop and drop in (step.get("id"), step.get("name")):
            continue
        if "run" not in step:
            continue  # the Slack action
        if not _if(step.get("if", "success()"), outputs, failed):
            continue
        code, out, written, summary = runner.run(
            DEPLOY,
            step,
            outputs,
            {"job.status": "failure" if failed else "success"},
            **FAST,
        )
        log.append((step.get("id") or step["name"], code, out, summary))
        outputs[step.get("id") or step["name"]] = {
            **written,
            "__outcome__": "success" if code == 0 else "failure",
        }
        failed = failed or code != 0
    return ("failure" if failed else "success"), outputs, log


def _if(expression: str, outputs: dict, failed: bool, env: dict | None = None) -> bool:
    """The `if:` forms these workflows use: &&-joined status functions, step
    comparisons and job-env comparisons (`env.SLACK_ON == 'true'`; an env
    name not given reads as unset, as with no SLACK_BOT_TOKEN). Anything
    else fails the test rather than guessing."""
    result = True
    for term in expression.split(" && "):
        term = term.strip()
        env_term = re.fullmatch(r"env\.(\w+) (==|!=) '([^']*)'", term)
        if env_term:
            actual = (env or {}).get(env_term.group(1), "")
            value = (actual == env_term.group(3)) == (env_term.group(2) == "==")
        elif term == "always()":
            value = True
        elif term == "failure()":
            value = failed
        elif term == "success()":
            value = not failed
        else:
            m = re.fullmatch(
                r"steps\.([\w-]+)\.(outcome|outputs\.([\w-]+)) (==|!=) '([^']*)'", term
            )
            assert m, f"the test cannot evaluate if: {term!r}"
            step = outputs.get(m.group(1), {})
            actual = step.get(m.group(3)) if m.group(3) else step.get("__outcome__")
            actual = actual or ""
            value = (actual == m.group(5)) == (m.group(4) == "==")
        result = result and value
    return result


@pytest.mark.regression
def test_a_rollback_during_the_rollout_turns_the_run_red(runner):
    """The API serves this revision at the guard, and not by the re-check (a
    Rollback run stopped this run's deployment in between). The run must end
    red, the summary must say the API changed, and step (i) must not claim
    the API is serving."""
    # api_serving.py reads describe-services twice per call (its own read,
    # then check_live_target_group.py's): serving at the guard, not after.
    api = api_rules([API_AFTER, API_AFTER, API_BEFORE], RULES_GREEN)
    status, outputs, log = _tail_of_the_job(
        runner, api + dashboard_rules([IN_PROGRESS, DONE])
    )
    assert status == "failure", [(k, c) for k, c, _, _ in log]
    assert outputs["api-recheck"]["api"] == "1"
    (summary,) = [s for k, _, _, s in log if k == "summary"]
    assert "| Dashboard rollout | completed |" in summary
    assert "API changed: after this run's traffic shift" in summary
    (i,) = [o for k, _, o, _ in log if k == "after-shift"]
    assert "The API is serving" not in i
    assert "no longer serving this run's revision" in i


@pytest.mark.regression
def test_the_recheck_is_the_last_assertion():
    steps = _steps(DEPLOY)
    recheck = _index(DEPLOY, "api-recheck")
    smoke = _index(DEPLOY, "Smoke test the dashboard through the public URL")
    rollout = _index(DEPLOY, "dashboard-rollout")
    assert rollout < smoke < recheck
    # Only reporting after it: steps that run on failure or always.
    for step in steps[recheck + 1 :]:
        assert str(step.get("if", "")).startswith(("failure()", "always()")), step.get(
            "name"
        )


@pytest.mark.regression
def test_a_whole_successful_tail_is_green_and_says_so(runner):
    status, outputs, log = _tail_of_the_job(
        runner, api_rules() + dashboard_rules([IN_PROGRESS, DONE])
    )
    assert status == "success", [(k, c, o[-400:]) for k, c, o, _ in log if c]
    (summary,) = [s for k, _, _, s in log if k == "summary"]
    assert "| Dashboard rollout | completed |" in summary
    # family:revision: the ARN names the account, and the summary is public.
    assert f"| New dashboard revision | `{DASH_NEW.rsplit('/', 1)[-1]}` |" in summary
    assert (
        "Rollback: Actions → Rollback → environment=staging, "
        "task_definition_arn=experimentation-backend-staging:42, "
        "dashboard_task_definition_arn=experimentation-dashboard-staging:6"
    ) in summary
    # EM v1 condition 4: the pin is the running DIGEST, never <tag>-<profile>.
    assert f"-c dashboard_image_tag={WEB_DIGEST}" in summary
    assert "dashboard_image_tag=v1.2.3" not in summary
    assert "API changed" not in summary


# --- outcomes, as the summary names them (UX W2/W4, UX-6) ------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "polls, update, cell",
    [
        ([IN_PROGRESS, DONE], None, "completed"),
        (
            # The breaker's rollback as captured on staging (2026-10-10).
            [
                IN_PROGRESS,
                breaker_read(DASH_OLD, "ecs-svc/6666", DASH_NEW, "ecs-svc/7777"),
            ],
            None,
            "rolled back by ECS's circuit breaker; serving experimentation-dashboard-staging:6",
        ),
        (
            [IN_PROGRESS, svc(dep(DASH_THIRD, "ecs-svc/8888"))],
            None,
            "changed by another run",
        ),
        ([IN_PROGRESS], None, "did not finish in 3s"),
        (
            [DONE],
            {
                "error": "An error occurred (AccessDeniedException) when calling UpdateService"
            },
            "not started: permission denied",
        ),
    ],
    ids=["completed", "circuit-breaker", "third-revision", "deadline", "access-denied"],
)
def test_each_rollout_outcome_has_its_own_summary_cell(runner, polls, update, cell):
    code, out, written, _ = _rollout(
        runner, api_rules(), dashboard_rules(polls, update=update)
    )
    assert written["outcome"] == cell, out
    assert (code == 0) == (cell == "completed")


# --- step (i) and the migration warnings (PE spec-delta 6, 7, 10) -----------------------


def _i() -> dict:
    return _step(DEPLOY, "If the API was deployed and the dashboard was not")


def _warnings() -> list[dict]:
    return [
        s for s in _steps(DEPLOY) if "steps.migrate.outcome" in str(s.get("if", ""))
    ]


@pytest.mark.regression
@pytest.mark.parametrize(
    "api_outcome, approved",
    [
        ("success", ""),  # the console-approval case: serving, no `approved`
        ("success", "true"),
        ("failure", ""),
        ("failure", "true"),
    ],
)
def test_the_three_failure_messages_are_mutually_exclusive(api_outcome, approved):
    """PE spec-delta 6 / F3: at most one of the two migration warnings and
    step (i) runs, and when the shift step succeeded it is step (i)."""
    outputs = {
        "migrate": {"__outcome__": "success"},
        "api-serving": {"__outcome__": api_outcome, "approved": approved},
    }
    firing = [
        s["name"] for s in [*_warnings(), _i()] if _if(s["if"], outputs, failed=True)
    ]
    assert len(firing) == 1, firing
    if api_outcome == "success":
        assert firing == [_i()["name"]]


@pytest.mark.regression
@pytest.mark.parametrize(
    "env, says_serving, must",
    [
        (
            {
                "GUARD": "0",
                "ROLLOUT": "rolled back by ECS's circuit breaker; serving x:6",
            },
            True,
            "The API is serving v1.2.3",
        ),
        (
            {"GUARD": "1", "GUARD_LINE": "NOT YET: the PRIMARY task set runs x:41"},
            False,
            "no longer serving this run's revision experimentation-backend-staging:43: NOT YET",
        ),
        (
            {
                "GUARD": "0",
                "ROLLOUT": "completed",
                "RECHECK": "3",
                "RECHECK_LINE": "WRONG: z",
            },
            False,
            "no longer serving this run's revision",
        ),
        (
            {"GUARD": "2", "GUARD_LINE": "UNKNOWN: could not tell: throttled"},
            False,
            "could not be told: UNKNOWN",
        ),
        ({}, False, "the run failed before it checked the API again"),
    ],
    ids=["guard-passed", "guard-exit1", "recheck-exit3", "guard-exit2", "not-reached"],
)
def test_step_i_says_the_api_is_serving_only_when_the_check_passed(
    runner, env, says_serving, must
):
    """PE spec-delta 7, 8 and 10."""
    runner.scenario([])
    base = {
        "GUARD": "",
        "GUARD_LINE": "",
        "RECHECK": "",
        "RECHECK_LINE": "",
        "ROLLOUT": "",
        "VERSION": "v1.2.3",
    }
    code, out, _, _ = runner.run(
        DEPLOY,
        _i(),
        {k: dict(v) for k, v in BEFORE_DASHBOARD.items()},
        **{**base, **env},
    )
    assert code == 0, out
    assert ("The API is serving" in out) is says_serving, out
    assert must in out, out
    assert "Deploy refuses a re-run until CodeDeploy deployment d-ABCDEF123" in out
    assert "puts back the API as well as the dashboard" in out
    assert "deployment/rollback-runbook.md Method 2" in out


@pytest.mark.regression
def test_no_copy_says_re_run_deploy_without_the_hour():
    """PE spec-delta 10: a re-run is refused for about an hour after a shift."""
    texts = [
        DEPLOY.read_text(),
        ROLLBACK.read_text(),
        (SCRIPTS / "ecs_rolling_rollout.sh").read_text(),
    ]
    for text in texts:
        assert not re.search(r"re-?run (the )?deploy", text, re.I), (
            "a bare Re-run Deploy"
        )


@pytest.mark.regression
@pytest.mark.parametrize("code, printed", [("1", True), ("3", True), ("2", False)])
def test_the_summary_says_the_api_changed_only_for_exits_1_and_3(runner, code, printed):
    runner.scenario([])
    outputs = {k: dict(v) for k, v in BEFORE_DASHBOARD.items()}
    outputs["dashboard-rollout"] = {
        "api_guard": code,
        "api_line": "X: y",
        "__outcome__": "failure",
    }
    rc, out, _, summary = runner.run(
        DEPLOY, _step(DEPLOY, "Run summary"), outputs, {"job.status": "failure"}
    )
    assert rc == 0, out
    assert ("API changed:" in summary) is printed
    assert f"not started: the API check before it failed (exit {code})" in summary


# --- the timeout budget (PE spec-delta 11) ------------------------------------------------

#: Two cold image builds (the backend's and the dashboard's; neither build in
#: deploy.yml uses a cache) and their pushes, checkouts, OIDC and every
#: describe/register call. An ASSUMPTION, not a measurement: no uncached build
#: of either image has been timed on a runner.
BUILD_ALLOWANCE_SECONDS = 25 * 60
MARGIN_SECONDS = 5 * 60


def _budget(env: dict[str, str]) -> int:
    migration_poll = int(
        re.search(
            r"^\s*sleep (\d+)\s*$",
            (SCRIPTS / "run_migration_task.sh").read_text(),
            re.M,
        ).group(1)
    )
    n = {k: int(v) for k, v in env.items() if str(v).isdigit()}
    return (
        n["SNAPSHOT_DEADLINE_SECONDS"]
        + n["MIGRATION_TIMEOUT_SECONDS"]
        + migration_poll
        + n["CODEDEPLOY_DEADLINE_SECONDS"]
        + n["CODEDEPLOY_POLL_SECONDS"]
        + n["DASHBOARD_ROLLOUT_DEADLINE_SECONDS"]
        # the rollout script's two pre-mutation retries and its last sleep
        + 3 * n["DASHBOARD_ROLLOUT_POLL_SECONDS"]
        # two smoke tests
        + 2
        * n["SMOKE_ATTEMPTS"]
        * (n["SMOKE_MAX_TIME_SECONDS"] + n["SMOKE_SLEEP_SECONDS"])
        + BUILD_ALLOWANCE_SECONDS
        + MARGIN_SECONDS
    )


@pytest.mark.regression
def test_the_job_timeout_covers_every_named_wait():
    """Replaces B3b's one-term check. Every wait is a named workflow value,
    and each step uses the name, not a literal."""
    env = _load(DEPLOY)["env"]
    timeout = _job(DEPLOY)["timeout-minutes"]
    assert timeout * 60 >= _budget(env), (timeout * 60, _budget(env))
    code = "\n".join(_code(s["run"]) for s in _steps(DEPLOY) if "run" in s)
    assert 'timeout "$SNAPSHOT_DEADLINE_SECONDS"' in code
    assert "aws rds wait db-cluster-snapshot-available" in code
    assert '--deadline "$DASHBOARD_ROLLOUT_DEADLINE_SECONDS"' in code
    assert '--interval "$DASHBOARD_ROLLOUT_POLL_SECONDS"' in code
    assert '--deadline-seconds "$CODEDEPLOY_DEADLINE_SECONDS"' in code
    assert code.count('$(seq 1 "$SMOKE_ATTEMPTS")') == 2
    assert code.count('--max-time "$SMOKE_MAX_TIME_SECONDS"') == 2
    assert code.count('sleep "$SMOKE_SLEEP_SECONDS"') == 2
    assert not re.search(r"--max-time \d", code)
    # run_migration_task.sh reads the workflow's value by this name.
    assert (
        "MIGRATION_TIMEOUT_SECONDS" in (SCRIPTS / "run_migration_task.sh").read_text()
    )


def test_the_budget_is_not_vacuous():
    """The sum is sensitive to the deadline it guards: 120 minutes is short."""
    env = dict(_load(DEPLOY)["env"])
    assert _budget(env) > 120 * 60
    env["DASHBOARD_ROLLOUT_DEADLINE_SECONDS"] = "3000"
    assert _budget(env) > _job(DEPLOY)["timeout-minutes"] * 60


# --- the rollback job's timeout budget (#816, PE condition 5) ---------------------------

#: Per AWS read in a loop bounded by a COUNT of reads (the stop step's two
#: waits, the API verify step): the sleeps are named, the reads' own time is
#: not. An ASSUMPTION, not a measurement.
ROLLBACK_READ_ALLOWANCE_SECONDS = 2
#: Checkout, OIDC, the target checks, the stop step's first reads, the
#: create and the summary's own calls.
ROLLBACK_STEPS_ALLOWANCE_SECONDS = 5 * 60
#: The summary's list of the deployment group's active deployments, once,
#: after its last read of what is serving (#759).
ROLLBACK_SUMMARY_LIST_READS = 1


def _stop_step_value(name: str) -> int:
    code = _step(ROLLBACK, "Stop any deployment already in flight")["run"]
    (value,) = re.findall(rf"^\s*{name}=(\d+)\s*$", code, re.M)
    return int(value)


def _rollback_budget(env: dict[str, str]) -> int:
    n = {k: int(v) for k, v in env.items() if str(v).isdigit()}
    read = ROLLBACK_READ_ALLOWANCE_SECONDS
    stop_polls = _stop_step_value("STOP_WAIT_POLLS")
    idle_polls = _stop_step_value("GROUP_IDLE_POLLS")
    verify_reads = (
        n["VERIFY_TASKS_DEADLINE_SECONDS"] // n["VERIFY_TASKS_POLL_SECONDS"] + 1
    )
    summary = _step(ROLLBACK, "Run summary")["run"]
    (summary_deadline,) = re.findall(
        r"\$\{SUMMARY_SERVING_DEADLINE_SECONDS:-(\d+)\}", summary
    )
    return (
        # the stop step: a stopped deployment's final status, 5 s apart ...
        stop_polls * (5 + read)
        # ... then the group idle, GROUP_IDLE_POLL_SECONDS apart
        + idle_polls * (_stop_step_value("GROUP_IDLE_POLL_SECONDS") + read)
        # the shift (a clock deadline) and its last sleep and poll
        + n["ROLLBACK_TIMEOUT_SECONDS"]
        + 10
        + 3 * read
        # the API verify step: count-bounded
        + (verify_reads - 1) * n["VERIFY_TASKS_POLL_SECONDS"]
        + verify_reads * read
        # the dashboard's rollout: its deadline, two pre-mutation retries and
        # its last sleep
        + n["DASHBOARD_ROLLOUT_DEADLINE_SECONDS"]
        + 3 * n["DASHBOARD_ROLLOUT_POLL_SECONDS"]
        # the summary's read of what is serving, and one read past it
        + int(summary_deadline)
        + 4 * read
        # ... and its list of what is active
        + ROLLBACK_SUMMARY_LIST_READS * read
        + ROLLBACK_STEPS_ALLOWANCE_SECONDS
        + MARGIN_SECONDS
    )


@pytest.mark.regression
def test_the_rollback_job_timeout_covers_every_named_wait():
    """deploy.yml's check, for rollback.yml: every wait is a named value, each
    step uses the name, and their sum fits inside timeout-minutes."""
    env = _load(ROLLBACK)["env"]
    timeout = _job(ROLLBACK)["timeout-minutes"]
    assert timeout * 60 >= _rollback_budget(env), (timeout * 60, _rollback_budget(env))
    code = "\n".join(_code(s["run"]) for s in _steps(ROLLBACK) if "run" in s)
    assert "deadline=$(( $(date +%s) + ROLLBACK_TIMEOUT_SECONDS ))" in code
    assert '--deadline "$DASHBOARD_ROLLOUT_DEADLINE_SECONDS"' in code
    assert '--interval "$DASHBOARD_ROLLOUT_POLL_SECONDS"' in code
    assert (
        "reads=$(( 10#$VERIFY_TASKS_DEADLINE_SECONDS / "
        "10#$VERIFY_TASKS_POLL_SECONDS + 1 ))" in code
    )
    assert 'sleep "$VERIFY_TASKS_POLL_SECONDS"' in code
    assert 'for _ in $(seq 1 "$STOP_WAIT_POLLS"); do' in code
    assert 'for n in $(seq 1 "$GROUP_IDLE_POLLS"); do' in code
    assert 'sleep "$GROUP_IDLE_POLL_SECONDS"' in code
    summary = _code(_step(ROLLBACK, "Run summary")["run"])
    assert summary.count("aws deploy list-deployments") == ROLLBACK_SUMMARY_LIST_READS


def test_the_rollback_budget_is_not_vacuous():
    """The sum is sensitive to each wait it guards: 60 minutes is short, and
    a verify wait of 15 minutes no longer fits."""
    env = dict(_load(ROLLBACK)["env"])
    assert _rollback_budget(env) > 60 * 60
    env["VERIFY_TASKS_DEADLINE_SECONDS"] = "900"
    assert _rollback_budget(env) > _job(ROLLBACK)["timeout-minutes"] * 60


# --- the dashboard image (QA 1a-1e, UX-3) ----------------------------------------------


@pytest.mark.regression
def test_the_dashboard_image_is_built_like_the_apis_before_anything_is_migrated():
    step = _step(DEPLOY, "web-image")
    code = _code(step["run"])
    assert 'IMAGE_TAG="${VERSION}-${PROFILE}"' in code
    assert 'REF="$ECR_REGISTRY/$ECR_DASHBOARD_REPO:$IMAGE_TAG"' in code
    assert [ln.strip() for ln in code.splitlines() if "docker push" in ln] == [
        'docker push "$REF"'
    ]
    assert re.findall(r"^\s*-t\s+(\S+)", code, re.M) == ['"$REF"']
    assert '--build-arg EXPERIMENTLY_PROFILE="$PROFILE"' in code
    assert "--target" not in code, "the frontend's profile is a build-arg, not a stage"
    assert "-f release/frontend/Dockerfile" in code
    assert re.search(r"^\s*release\s*$", code, re.M)
    assert '--build-arg GIT_COMMIT="$RELEASE_COMMIT"' in code
    assert 'RELEASE_COMMIT="$(git -C release rev-parse HEAD)"' in code
    # The label check runs on both the reuse and the build path.
    assert code.count("check_labels ") == 2
    assert (
        "io.experimently.profile" in code
        and "org.opencontainers.image.revision" in code
    )
    # A re-run in the same environment reuses its tag.
    assert "aws ecr describe-images" in code and "ImageNotFoundException" in code
    assert 'echo "image=$ECR_REGISTRY/$ECR_DASHBOARD_REPO@$digest"' in code
    assert "^sha256:[0-9a-f]{64}$" in code
    # UX-3: before the snapshot, after the API's image.
    assert (
        _index(DEPLOY, "image")
        < _index(DEPLOY, "web-image")
        < _index(DEPLOY, "snapshot")
    )
    assert _load(DEPLOY)["env"]["ECR_DASHBOARD_REPO"] == "experimentation-platform/web"


@pytest.mark.regression
def test_the_dashboard_steps_follow_the_api_smoke_with_no_if():
    """UX-4: registered and rolled out only after the API answered."""
    smoke = _index(DEPLOY, "Smoke test through the public URL")
    for key in ("dashboard-td", "dashboard-rollout", "api-recheck"):
        assert _index(DEPLOY, key) > smoke, key
        assert "if" not in _step(DEPLOY, key), key
    assert "if" not in _step(DEPLOY, "Smoke test the dashboard through the public URL")


@pytest.mark.regression
def test_the_dashboard_is_registered_by_digest_with_its_own_container():
    step = _step(DEPLOY, "dashboard-td")
    assert step["env"]["IMAGE"] == "${{ steps.web-image.outputs.image }}"
    assert (
        'bash scripts/register_task_definition.sh "$ECS_DASHBOARD_TASK_FAMILY" "$IMAGE" dashboard'
        in step["run"]
    )


@pytest.mark.regression
def test_no_services_stable_and_no_forced_deployment():
    """QA 2c/3c, scoped to the workflows and scripts/ (PE v1 row 2c).

    The commands the deployment pages give an operator are held to the same
    ban in test_deploy_docs.py (#789), which runs on a docs-only change.
    """
    for path in [
        DEPLOY,
        ROLLBACK,
        *sorted(SCRIPTS.glob("*.sh")),
        *sorted(SCRIPTS.glob("*.py")),
    ]:
        code = _code(path.read_text()) if path.suffix != ".py" else path.read_text()
        assert "--force-new-deployment" not in code, path.name
        if path.suffix != ".py":
            assert "wait services-stable" not in code, path.name


@pytest.mark.regression
def test_no_aws_name_is_built_in_shell():
    """QA 4d: names come from the job env, which the CDK names test checks."""
    pattern = re.compile(r"experimentation-[a-z-]+-\$\{?TARGET_ENV")
    for path in (DEPLOY, ROLLBACK):
        for step in _steps(path):
            if isinstance(step.get("run"), str):
                assert not pattern.search(_code(step["run"])), step.get("name")


# --- the dashboard refusals and the record (W1, EM v1 condition 4, PE C9) ----------------


def _serving(
    runner: Runner, services: list, family_td: dict | None = None, image=OLD_WEB_IMAGE
):
    runner.scenario(
        [
            rule(
                "ecs describe-services",
                "experimentation-dashboard-staging",
                answers=services,
            ),
            rule(
                "ecs describe-task-definition",
                "--task-definition experimentation-dashboard-staging --output json",
                answers=[family_td or _task_definition(DASH_OLD, image)],
            ),
            rule(
                "ecs describe-task-definition",
                answers=[_task_definition(DASH_OLD, image)],
            ),
        ]
    )
    return runner.run(DEPLOY, _step(DEPLOY, "previous-dashboard"))


def _services_answer(*services: dict) -> dict:
    return {"services": list(services), "failures": []}


@pytest.mark.regression
def test_the_record_step_outputs_the_arn_and_the_release_value(runner):
    code, out, written, _ = _serving(
        runner, [_services_answer(svc(dep(DASH_OLD, "d")))]
    )
    assert code == 0, out
    assert written == {
        "arn": DASH_OLD,
        "image": OLD_WEB_IMAGE,
        "release_arn": DASH_OLD,
        "digest": OLD_WEB_DIGEST,
    }
    assert "::warning" not in out


@pytest.mark.regression
@pytest.mark.parametrize(
    "image, warns",
    [(BOOTSTRAP_WEB_IMAGE, False), (TAG_WEB_IMAGE, True)],
    ids=["bootstrap", "tag-after-cdk-deploy"],
)
def test_a_serving_image_that_is_not_a_release_is_recorded_without_a_rollback_value(
    runner, image, warns
):
    """EM v1 condition 4: a tag CloudFormation registered is named."""
    code, out, written, _ = _serving(
        runner, [_services_answer(svc(dep(DASH_OLD, "d")))], image=image
    )
    assert code == 0, out
    assert written["arn"] == DASH_OLD and written["release_arn"] == ""
    assert ("::warning title=Dashboard not on a release::" in out) is warns


@pytest.mark.regression
@pytest.mark.parametrize(
    "services, words",
    [
        (
            {"services": [], "failures": [{"reason": "MISSING"}]},
            "these stacks predate the dashboard service (v0.4.0). Deploy "
            "experimentation-fargate-staging with the pins in docs/deployment/"
            "deployment-guide.md section 1.6 first. Nothing has been "
            "built or changed.",
        ),
        (
            _services_answer(svc(dep(DASH_OLD, "d"), controller="CODE_DEPLOY")),
            "not the ECS rolling controller",
        ),
        (_services_answer(svc(dep(DASH_OLD, "d"), desired=0)), "wants 0 tasks"),
        (
            _services_answer(
                svc(
                    dep(DASH_NEW, "n", state="IN_PROGRESS"),
                    dep(DASH_OLD, "d", status="ACTIVE"),
                )
            ),
            "a dashboard deployment is in progress",
        ),
    ],
    ids=["missing", "controller", "desired-0", "mid-rollout"],
)
def test_the_dashboard_refusals(runner, services, words):
    code, out, _, _ = _serving(runner, [services])
    assert code == 1, out
    assert words in out
    assert "::error::" in out
    # PE v1 C9: never send an operator to `cdk deploy --all` on a running one.
    assert "deploy --all" not in out


@pytest.mark.regression
def test_a_family_without_one_dashboard_container_is_refused(runner):
    wrong = {
        "taskDefinition": {"containerDefinitions": [{"name": "backend", "image": "x"}]}
    }
    code, out, _, _ = _serving(
        runner, [_services_answer(svc(dep(DASH_OLD, "d")))], family_td=wrong
    )
    assert code == 1
    assert "0 containers named 'dashboard' (it has: backend)" in out


@pytest.mark.regression
def test_the_dashboard_refusals_come_before_the_first_mutation():
    """Duplicated here in the dashboard's terms; test_deploy_workflows.py's
    REFUSALS carries the same markers."""
    steps = _steps(DEPLOY)
    from backend.tests.unit.infrastructure.test_deploy_workflows import MUTATIONS

    first = min(
        i
        for i, s in enumerate(steps)
        if any(m in _code(s.get("run", "")) for m in MUTATIONS)
    )
    assert _index(DEPLOY, "previous-dashboard") < first
    assert _index(DEPLOY, "ECR repositories exist") < first
    assert (
        'for repo in "$ECR_BACKEND_REPO" "$ECR_DASHBOARD_REPO"; do'
        in _step(DEPLOY, "ECR repositories exist")["run"]
    )


# --- one digest predicate (PE v1 C6, EM v1 condition 5 / F6) ----------------------------


def _module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "dashboard_revision", SCRIPTS / "dashboard_revision.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.regression
@pytest.mark.parametrize(
    "image, release",
    [
        (WEB_IMAGE, True),
        (BOOTSTRAP_WEB_IMAGE, False),
        (TAG_WEB_IMAGE, False),
        (f"{REGISTRY}/experimentation-platform/web:v1.2.3-full@{WEB_DIGEST}", False),
        (f"{REGISTRY}/experimentation-platform/backend@{WEB_DIGEST}", False),
        (f"{REGISTRY}/experimentation-platform/web@sha256:abc", False),
        ("", False),
    ],
)
def test_the_release_predicate(image, release):
    assert _module().is_release_image(image) is release


def _target(runner: Runner, revision: str, td: dict, serving: dict | None = None):
    runner.scenario(
        [
            rule("ecs describe-task-definition", answers=[td]),
            rule(
                "ecs describe-services",
                "experimentation-dashboard-staging",
                answers=[_services_answer(serving or svc(dep(DASH_NEW, "n")))],
            ),
        ]
    )
    return runner.run(
        ROLLBACK,
        _step(ROLLBACK, "dashboard-target"),
        inputs={"dashboard_task_definition_arn": revision},
    )


@pytest.mark.regression
@pytest.mark.parametrize(
    "image, accepted",
    [(OLD_WEB_IMAGE, True), (TAG_WEB_IMAGE, False), (BOOTSTRAP_WEB_IMAGE, False)],
    ids=["digest", "cfn-tag", "bootstrap"],
)
def test_the_summary_prints_exactly_the_values_rollback_accepts(
    runner, image, accepted
):
    """F6 round trip: the deploy records the serving dashboard; its summary
    prints a rollback value only for a release; rollback.yml accepts exactly
    that value, and refuses the revision the summary left out."""
    code, out, recorded, _ = _serving(
        runner, [_services_answer(svc(dep(DASH_OLD, "d")))], image=image
    )
    assert code == 0, out
    outputs = {k: dict(v) for k, v in BEFORE_DASHBOARD.items()}
    outputs["previous-dashboard"] = {**recorded, "__outcome__": "success"}
    runner.scenario([])
    _, _, _, summary = runner.run(
        DEPLOY, _step(DEPLOY, "Run summary"), outputs, {"job.status": "success"}
    )
    printed = re.search(r"dashboard_task_definition_arn=(\S+)", summary)
    assert bool(printed) is accepted, summary
    if not accepted:
        assert (
            "not a release: there is no released dashboard to roll back to" in summary
        )
    revision = printed.group(1) if printed else DASH_OLD.rsplit("/", 1)[-1]
    rc, out, written, _ = _target(runner, revision, _task_definition(DASH_OLD, image))
    assert (rc == 0) is accepted, out
    if accepted:
        assert written["arn"] == DASH_OLD and written["expect"] == DASH_NEW


# --- rollback.yml (EM v1 conditions 1, 2; PE spec-delta 9; UX W5) ------------------------


@pytest.mark.regression
def test_the_dashboard_input_is_optional_and_the_api_input_still_required():
    inputs = _load(ROLLBACK)["on"]["workflow_dispatch"]["inputs"]
    assert inputs["task_definition_arn"]["required"] is True
    dashboard = inputs["dashboard_task_definition_arn"]
    assert dashboard["required"] is False and dashboard["default"] == ""


@pytest.mark.regression
def test_rollback_validates_both_before_it_stops_anything_and_stops_unconditionally():
    """EM v1 condition 1: no dashboard-only path; the stop step always runs.

    The two CodeDeploy steps are skipped on exactly one condition: the stop
    step found the API already on the target with nothing in flight (#148),
    when there is nothing for a deployment to do. Pinned exactly."""
    stop = _index(ROLLBACK, "Stop any deployment already in flight")
    assert _index(ROLLBACK, "target") < stop
    assert _index(ROLLBACK, "dashboard-target") < stop
    assert "if" not in _step(ROLLBACK, "Stop any deployment already in flight")
    assert "if" not in _step(ROLLBACK, "dashboard-target")
    for key in ("codedeploy", "Shift traffic and wait for it to land"):
        assert _step(ROLLBACK, key)["if"] == (
            "steps.stop.outputs.already_serving != 'true'"
        ), key
    assert "if" not in _step(ROLLBACK, "Verify what is serving traffic")
    assert _index(ROLLBACK, "Verify what is serving traffic") < _index(
        ROLLBACK, "dashboard-rollback"
    )


@pytest.mark.regression
@pytest.mark.parametrize(
    "revision, td, words",
    [
        (
            "experimentation-dashboard-staging",
            _task_definition(DASH_OLD, OLD_WEB_IMAGE),
            "names a family with no revision",
        ),
        (
            "experimentation-backend-staging:42",
            _task_definition(
                API_FAMILY_REVISION,
                OLD_WEB_IMAGE,
                family="experimentation-backend-staging",
            ),
            "experimentation-backend-staging:42 is an API revision; it goes in task_definition_arn, not dashboard_task_definition_arn.",
        ),
        (
            "experimentation-dashboard-prod:3",
            _task_definition(DASH_OLD.replace("staging", "prod"), OLD_WEB_IMAGE),
            "is a prod dashboard revision. Re-run with environment=prod.",
        ),
        (
            "experimentation-dashboard-staging:6",
            {
                "taskDefinition": {
                    **_task_definition(DASH_OLD, OLD_WEB_IMAGE)["taskDefinition"],
                    "status": "INACTIVE",
                }
            },
            "is INACTIVE, not ACTIVE",
        ),
        (
            "experimentation-dashboard-staging:6",
            {
                "taskDefinition": {
                    **_task_definition(DASH_OLD, OLD_WEB_IMAGE)["taskDefinition"],
                    "containerDefinitions": [{"name": "web", "image": OLD_WEB_IMAGE}],
                }
            },
            "has no container named 'dashboard' (it has: web)",
        ),
        (
            "experimentation-dashboard-staging:6",
            _task_definition(DASH_OLD, BOOTSTRAP_WEB_IMAGE),
            "which is not a release",
        ),
    ],
    ids=[
        "bare-family",
        "swapped-api",
        "other-env",
        "inactive",
        "no-container",
        "bootstrap",
    ],
)
def test_the_dashboard_target_refusals(runner, revision, td, words):
    code, out, _, _ = _target(runner, revision, td)
    assert code == 1, out
    assert words in out
    assert "Nothing has been rolled back." in out
    assert not [
        c
        for c in runner.calls()
        if c[1] not in ("describe-task-definition", "describe-services")
    ]


@pytest.mark.regression
def test_a_dashboard_revision_in_the_api_field_is_refused_by_name():
    code = _code(_step(ROLLBACK, "target")["run"])
    assert (
        "${ARN##*/} is a dashboard revision; it goes in dashboard_task_definition_arn, "
        "not task_definition_arn." in code
    )


def _rollback_dashboard(
    runner: Runner, expect: str, polls: list, before: dict | None = None
):
    runner.scenario(dashboard_rules(polls, before=before))
    outputs = {
        "dashboard-target": {"arn": DASH_OLD, "expect": expect, "image": OLD_WEB_IMAGE},
        "target": {"arn": API_FAMILY_REVISION},
        "codedeploy": {"deployment-id": "d-ROLLBACK1"},
    }
    return runner.run(
        ROLLBACK,
        _step(ROLLBACK, "dashboard-rollback"),
        outputs,
        {"dashboard_task_definition_arn": "experimentation-dashboard-staging:6"},
        **FAST,
    )


ROLLED_BACK_UPDATE = {
    "service": svc(
        dep(DASH_OLD, "ecs-svc/6000", state="IN_PROGRESS", running=0),
        dep(DASH_NEW, "ecs-svc/7777", status="ACTIVE"),
    )
}


@pytest.mark.regression
def test_rollback_rolls_the_dashboard_back(runner):
    runner.scenario([])
    rules = dashboard_rules(
        [svc(dep(DASH_OLD, "ecs-svc/6000"))],
        before=svc(dep(DASH_NEW, "ecs-svc/7777")),
        update=ROLLED_BACK_UPDATE,
    )
    runner.scenario(rules)
    outputs = {
        "dashboard-target": {"arn": DASH_OLD, "expect": DASH_NEW},
        "target": {"arn": API_FAMILY_REVISION},
        "codedeploy": {"deployment-id": "d-ROLLBACK1"},
    }
    code, out, written, _ = runner.run(
        ROLLBACK,
        _step(ROLLBACK, "dashboard-rollback"),
        outputs,
        {"dashboard_task_definition_arn": "experimentation-dashboard-staging:6"},
        **FAST,
    )
    assert code == 0, out
    assert written["result"] == "rolled back to experimentation-dashboard-staging:6"
    (update,) = runner.updates()
    assert update[update.index("--task-definition") + 1] == DASH_OLD


@pytest.mark.regression
def test_rollback_does_not_touch_a_dashboard_that_changed_since_it_was_validated(
    runner,
):
    """EM v1 condition 2 (rollback half) and PE spec-delta 9: the refusal
    says the API rollback has happened and what the dashboard is now."""
    moved = svc(dep(DASH_THIRD, "ecs-svc/8888"))
    runner.scenario(
        dashboard_rules([moved], before=moved)
        + [
            rule(
                "ecs describe-task-definition",
                answers=[_task_definition(DASH_THIRD, WEB_IMAGE)],
            )
        ]
    )
    outputs = {
        "dashboard-target": {"arn": DASH_OLD, "expect": DASH_NEW},
        "target": {"arn": API_FAMILY_REVISION},
        "codedeploy": {"deployment-id": "d-ROLLBACK1"},
    }
    code, out, written, _ = runner.run(
        ROLLBACK,
        _step(ROLLBACK, "dashboard-rollback"),
        outputs,
        {"dashboard_task_definition_arn": "experimentation-dashboard-staging:6"},
        **FAST,
    )
    assert code == 1, out
    assert runner.updates() == []
    assert (
        "The API is on experimentation-backend-staging:42 (CodeDeploy "
        "deployment d-ROLLBACK1" in out
    )
    assert (
        "(it is no longer experimentation-dashboard-staging:7), so it was not touched"
        in out
    )
    assert "experimentation-dashboard-staging:8" in out  # what it is now
    assert written["result"] == "not changed: it changed after this run validated it"


@pytest.mark.regression
def test_rollback_confirms_a_dashboard_already_on_the_target_without_redeploying(
    runner,
):
    already = svc(dep(DASH_OLD, "ecs-svc/6000"))
    runner.scenario(dashboard_rules([already], before=already))
    outputs = {
        "dashboard-target": {"arn": DASH_OLD, "expect": DASH_OLD},
        "target": {"arn": API_FAMILY_REVISION},
        "codedeploy": {"deployment-id": "d-ROLLBACK1"},
    }
    code, out, written, _ = runner.run(
        ROLLBACK,
        _step(ROLLBACK, "dashboard-rollback"),
        outputs,
        {"dashboard_task_definition_arn": "experimentation-dashboard-staging:6"},
        **FAST,
    )
    assert code == 0, out
    assert runner.updates() == []
    # Not "rolled back to": nothing was changed (code review suggestion c).
    assert written["result"] == (
        "already serving experimentation-dashboard-staging:6; nothing changed"
    )


@pytest.mark.regression
@pytest.mark.parametrize("given", ["", "experimentation-dashboard-staging:6"])
def test_the_rollback_summary_says_what_happened_to_the_dashboard(runner, given):
    """UX-10 / QA 4b: "Only the API was rolled back." is not unconditional,
    and an API-only rollback says what the dashboard is serving (PE 9)."""
    runner.scenario([])
    # A verified rollback (#759: ROLLED BACK is the only verdict that exits 0).
    outputs = {
        "target": {"arn": API_FAMILY_REVISION},
        "codedeploy": {"deployment-id": "d-ROLLBACK1"},
        "api-verify": {"__outcome__": "success"},
        "dashboard-target": (
            {"serving_arn": DASH_NEW, "serving_image": WEB_IMAGE}
            if not given
            else {"arn": DASH_OLD, "expect": DASH_NEW}
        ),
        "dashboard-rollback": {
            "result": "rolled back to experimentation-dashboard-staging:6",
            "__outcome__": "success" if given else "skipped",
        },
    }
    code, out, _, summary = runner.run(
        ROLLBACK,
        _step(ROLLBACK, "Run summary"),
        outputs,
        {"dashboard_task_definition_arn": given, "job.status": "success"},
    )
    assert code == 0, out
    assert "Only the API was rolled back" not in summary
    if given:
        assert (
            "| Dashboard result | rolled back to experimentation-dashboard-staging:6 |"
            in summary
        )
    else:
        # The image without its registry host, which names the account.
        assert (
            "Dashboard not rolled back: it is serving experimentation-dashboard-staging:7 "
            f"({WEB_IMAGE.split('/', 1)[1]})." in summary
        )


@pytest.mark.regression
def test_an_empty_dashboard_input_records_what_is_serving_under_other_names(runner):
    runner.scenario(
        [
            rule(
                "ecs describe-services",
                "experimentation-dashboard-staging",
                answers=[_services_answer(svc(dep(DASH_NEW, "n")))],
            ),
            rule(
                "ecs describe-task-definition",
                answers=[_task_definition(DASH_NEW, WEB_IMAGE)],
            ),
        ]
    )
    code, out, written, _ = runner.run(
        ROLLBACK,
        _step(ROLLBACK, "dashboard-target"),
        inputs={"dashboard_task_definition_arn": ""},
    )
    assert code == 0, out
    assert written["serving_arn"] == DASH_NEW and "arn" not in written


# --- the rollback-order ruling (EM c4-rollback-order conditions 1-4) --------------


@pytest.mark.regression
def test_the_dashboard_rollback_keeps_the_default_success_gate():
    """EM ruling C1: the refusal text says "the API is on <target>", which is
    true only because this step runs after the API verify step succeeded."""
    step = _step(ROLLBACK, "dashboard-rollback")
    assert step["if"] == "inputs.dashboard_task_definition_arn != ''"
    assert _index(ROLLBACK, "api-verify") < _index(ROLLBACK, "dashboard-rollback")


ROLLED_TO_OLD = {
    "service": svc(
        dep(DASH_OLD, "ecs-svc/6000", state="IN_PROGRESS", running=0),
        dep(DASH_NEW, "ecs-svc/7777", status="ACTIVE"),
    )
}


@pytest.mark.regression
@pytest.mark.parametrize(
    "polls, update, title",
    [
        ([svc(dep(DASH_THIRD, "ecs-svc/8888"))], None, "Dashboard not rolled back"),
        (
            # The breaker's rollback as captured on staging (2026-10-10), with
            # the rollback's revision as the one that failed to start.
            [breaker_read(DASH_NEW, "ecs-svc/7777", DASH_OLD, "ecs-svc/6000")],
            ROLLED_TO_OLD,
            "Dashboard rollback did not take",
        ),
        (
            [
                svc(
                    dep(DASH_OLD, "ecs-svc/6000", state="IN_PROGRESS", running=0),
                    dep(DASH_NEW, "ecs-svc/7777", status="ACTIVE"),
                )
            ],
            ROLLED_TO_OLD,
            "Dashboard rollback did not finish",
        ),
        (
            [],
            {"error": "An error occurred (InvalidParameterException)"},
            "Dashboard not rolled back",
        ),
    ],
    ids=["refused", "circuit-breaker", "deadline", "generic"],
)
def test_every_dashboard_rollback_failure_names_the_state_and_the_way_out(
    runner, polls, update, title
):
    """EM ruling C3: each branch says the API is on the target, what the
    dashboard is serving, that this is the newer-dashboard/older-API state,
    points at Method 2, and warns against dispatching Rollback again."""
    before = (
        svc(dep(DASH_THIRD, "ecs-svc/8888"))
        if title == "Dashboard not rolled back" and not update
        else svc(dep(DASH_NEW, "ecs-svc/7777"))
    )
    runner.scenario(
        dashboard_rules(polls, before=before, update=update)
        + [
            rule(
                "ecs describe-task-definition",
                answers=[_task_definition(DASH_NEW, WEB_IMAGE)],
            )
        ]
    )
    outputs = {
        "dashboard-target": {"arn": DASH_OLD, "expect": DASH_NEW},
        "target": {"arn": API_FAMILY_REVISION},
        "codedeploy": {"deployment-id": "d-ROLLBACK1"},
    }
    code, out, written, _ = runner.run(
        ROLLBACK,
        _step(ROLLBACK, "dashboard-rollback"),
        outputs,
        {"dashboard_task_definition_arn": "experimentation-dashboard-staging:6"},
        **FAST,
    )
    assert code != 0, out
    (line,) = [
        ln for ln in out.splitlines() if ln.startswith(f"::error title={title}::")
    ]
    assert "The API is on experimentation-backend-staging:42" in line
    assert "The dashboard was not rolled back with it: " in line
    assert " is serving " in line or "could not read" in line
    assert "the newer-dashboard, older-API state" in line
    assert "rollback-runbook.md Method 2, the dashboard block" in line
    assert (
        "Do not dispatch Rollback again while CodeDeploy deployment d-ROLLBACK1 is active"
        in line
    )
    assert written["result"]


def _summary(
    runner: Runner,
    api_verify: str,
    dashboard_outcome: str,
    given: str,
    result: str = "",
    **overrides: str,
):
    outputs = {
        "target": {"arn": API_FAMILY_REVISION},
        "codedeploy": {"deployment-id": "d-ROLLBACK1"},
        "api-verify": {"__outcome__": api_verify},
        "dashboard-target": {"arn": DASH_OLD, "expect": DASH_NEW} if given else {},
        "dashboard-rollback": {"__outcome__": dashboard_outcome, "result": result},
    }
    return runner.run(
        ROLLBACK,
        _step(ROLLBACK, "Run summary"),
        outputs,
        {
            "dashboard_task_definition_arn": given,
            "job.status": "success"
            if api_verify == "success" and dashboard_outcome in ("success", "")
            else "failure",
        },
        # The read of what is serving (#755) polls; the verdict, not the
        # wait, is under test.
        **{"SUMMARY_SERVING_POLL_SECONDS": "0", **overrides},
    )


#: The per-id sentence (#759, T32) for this run's deployment.
T32_ROLLBACK1 = (
    "Do not dispatch Rollback again while deployment d-ROLLBACK1 is active: a "
    "new Rollback stops any deployment that is not CodeDeploy's own rollback, "
    "with auto-rollback, which reverts what it shifted, and it refuses while "
    "CodeDeploy's own rollback is active."
)

#: scripts/api_serving.py's answer when the bad release is still PRIMARY.
NOT_YET_43 = (
    "NOT YET: the PRIMARY task set runs experimentation-backend-staging:43, "
    "not experimentation-backend-staging:42"
)


@pytest.mark.regression
@pytest.mark.parametrize(
    "api_verify, dashboard_outcome, given, result, slack",
    [
        (
            "success",
            "",
            "",
            "",
            "staging: ROLLED BACK: API rolled back to experimentation-backend-staging:42 "
            "(CodeDeploy deployment d-ROLLBACK1, verified); dashboard left as it is. "
            f"{T32_ROLLBACK1}",
        ),
        (
            "success",
            "success",
            "experimentation-dashboard-staging:6",
            "rolled back to experimentation-dashboard-staging:6",
            "staging: ROLLED BACK: API rolled back to experimentation-backend-staging:42 "
            "(CodeDeploy deployment d-ROLLBACK1, verified); dashboard rolled back to "
            f"experimentation-dashboard-staging:6. {T32_ROLLBACK1}",
        ),
        (
            "success",
            "failure",
            "experimentation-dashboard-staging:6",
            "rejected by ECS's circuit breaker; serving experimentation-dashboard-staging:7",
            "staging: API BACK, DASHBOARD NOT: API rolled back to "
            "experimentation-backend-staging:42 (CodeDeploy deployment d-ROLLBACK1, "
            "verified); dashboard NOT rolled back (rejected by ECS's circuit breaker; "
            "serving experimentation-dashboard-staging:7), so the API and dashboard "
            "are on different releases: put the dashboard back with docs/deployment/"
            "rollback-runbook.md Method 2, the dashboard block. "
            f"{T32_ROLLBACK1}",
        ),
        (
            "failure",
            "",
            "",
            "",
            # Verify failed, the read says the bad release is PRIMARY, and this
            # run's deployment is still active: STILL MOVING, quoting the
            # read, never the absolute "NOT rolled back" (#755, #759).
            "staging: STILL MOVING: the API is not yet on "
            "experimentation-backend-staging:42 (scripts/api_serving.py exit 1: "
            f"{NOT_YET_43}) while deployment d-ROLLBACK1 is active; dashboard left "
            f"as it is. {T32_ROLLBACK1}",
        ),
    ],
    ids=["api-only-ok", "both-ok", "api-ok-dashboard-failed", "api-failed"],
)
def test_the_result_line_keys_on_the_api_verify_step(
    runner, api_verify, dashboard_outcome, given, result, slack
):
    """EM ruling C2 (the code review's CRITICAL): never "NOT rolled back" for
    an API that was rolled back and verified. Every row supplies the reads
    of what is serving; only the row whose verify step failed makes them."""
    runner.scenario(
        [
            *api_rules(API_AFTER, RULES_GREEN),
            rule("deploy list-deployments", answers=["d-ROLLBACK1"]),
        ]
    )
    code, out, written, summary = _summary(
        runner,
        api_verify,
        dashboard_outcome,
        given,
        result,
        SUMMARY_SERVING_POLLS="3",
    )
    # Exit 0 only for ROLLED BACK (#759).
    assert code == (0 if slack.startswith("staging: ROLLED BACK: ") else 1), out
    assert written["slack"] == slack
    assert summary.startswith(f"## Rollback of {slack}\n")
    reads = [c for c in runner.calls() if c[:2] == ["ecs", "describe-services"]]
    if api_verify == "success":
        assert reads == [], reads
        assert "| API at the end of the run |" not in summary
    else:
        # Polled to the limit on 1 and decided on the last answer.
        assert len(reads) == 3, reads
        assert (
            f"| API at the end of the run | {NOT_YET_43} "
            "(scripts/api_serving.py exit 1) |" in summary
        ), summary


@pytest.mark.regression
def test_slack_says_what_the_summary_says():
    step = _step(ROLLBACK, "Notify rollback result")
    message = step["with"]["slack-message"]
    assert "steps.summary.outputs.slack" in message
    assert "is NOT rolled back" not in message
    assert "job.status == 'success' &&" not in message


@pytest.mark.regression
def test_a_dashboard_half_never_reached_is_read_and_named(runner):
    """EM ruling C4: the API half failed with a dashboard revision given; the
    summary reads (read-only) and prints what the dashboard is serving."""
    runner.scenario(
        [
            rule(
                "ecs describe-services",
                "experimentation-dashboard-staging",
                answers=[_services_answer(svc(dep(DASH_NEW, "n")))],
            ),
            rule(
                "ecs describe-task-definition",
                answers=[_task_definition(DASH_NEW, WEB_IMAGE)],
            ),
            *api_rules(API_AFTER, RULES_GREEN),
            rule("deploy list-deployments", answers=["d-ROLLBACK1"]),
        ]
    )
    code, out, written, summary = _summary(
        runner,
        "failure",
        "skipped",
        "experimentation-dashboard-staging:6",
        SUMMARY_SERVING_POLLS="1",
    )
    assert code == 1, out
    assert written["slack"] == (
        "staging: STILL MOVING: the API is not yet on "
        "experimentation-backend-staging:42 (scripts/api_serving.py exit 1: "
        f"{NOT_YET_43}) while deployment d-ROLLBACK1 is active; dashboard NOT "
        f"rolled back (not reached). {T32_ROLLBACK1}"
    )
    assert "(scripts/api_serving.py exit 1) |" in summary
    assert (
        "Dashboard not rolled back (the API half failed): experimentation-dashboard-staging "
        "is serving experimentation-dashboard-staging:7" in summary
    )
    assert "newer-dashboard, older-API state" in summary
    assert {tuple(c[:2]) for c in runner.calls()} == {
        ("ecs", "describe-services"),
        ("ecs", "describe-task-definition"),
        ("deploy", "list-deployments"),
    }


@pytest.mark.regression
def test_another_environments_dashboard_revision_in_the_api_field_says_which(runner):
    """Code review suggestion b: the same "Re-run with environment=" as the
    dashboard field's check, run through the step's own script."""
    prod = DASH_OLD.replace("staging", "prod")
    runner.scenario(
        [
            rule(
                "ecs describe-task-definition",
                answers=[_task_definition(prod, OLD_WEB_IMAGE)["taskDefinition"]],
            )
        ]
    )
    code, out, _, _ = runner.run(
        ROLLBACK,
        _step(ROLLBACK, "target"),
        inputs={"task_definition_arn": "experimentation-dashboard-prod:6"},
    )
    assert code == 1, out
    assert (
        "You chose environment=staging but experimentation-dashboard-prod:6 is a prod "
        "dashboard revision; it goes in dashboard_task_definition_arn, not "
        "task_definition_arn. Re-run with environment=prod." in out
    )
