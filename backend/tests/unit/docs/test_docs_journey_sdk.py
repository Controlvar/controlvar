"""The docs-journey runner's sdk step: a published SDK installed and run as its page says.

The sdk-javascript and sdk-python journeys (#939; QA plan v1.3, D3c) install
each SDK from its public registry with the page's own install command and run
the page's Quick start block against the compose stack (``docs_runner/sdk.py``).
What decides whether that check can fail is tested here, in the unit job:

* the install command and the block are read from the real pages, and the
  program that is run is the page's block with only its quoted placeholders
  replaced (the block is held equal to the page);
* an install that fails, or installs another version, fails the step, and
  never leaves it NOT RUN;
* the API key reaches only the block's process, and a block that prints it
  fails the step with the key taken out of everything kept;
* each answer is held to the oracle with its JSON type (``true`` is not ``1``);
* the loader refuses a step that replaces anything but a placeholder, or names
  a stub or a variable the block does not, and a journey whose users all get
  the same flag answer.

The commands go through a fake ``execute``, so nothing is installed; one test
runs a generated Python program with this interpreter. Temporary files only;
no network, no browser, no git.
"""

from __future__ import annotations

import copy
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import guide as guides
from docs_runner import loader, log, oracles, redaction, report, sdk, traffic
from docs_runner.model import Journey, Step

TODAY = datetime.date(2026, 10, 7)
KEY = "eptk_Fake0KeyForTheUnitJob9876543210abcdef"
#: What execute.py gives a block: the key and the address under both names.
SECRETS_ENV = {
    "CONTROLVAR_API_URL": "http://api",
    "CONTROLVAR_API_KEY": KEY,
    "EXPERIMENTLY_API_URL": "http://api",
    "EXPERIMENTLY_API_KEY": KEY,
}

PAGE = """# Sample SDK

## Installation

```bash
npm install @getexperimently/js-sdk@0.1.0
```

To build it yourself:

```bash
npm install /path/to/a.tgz
```

## Quick start

```typescript
import { ExperimentationClient } from '@getexperimently/js-sdk';

const client = new ExperimentationClient({
  apiUrl: 'http://localhost:8000',
  apiKey: process.env.EXPERIMENTLY_API_KEY!,
});
const user = { userId: 'user-123' };
const variant = await client.getVariant('checkout_flow', user);
const enabled = await client.isFeatureEnabled('new_search', user);
if (enabled) { useNewSearch(); }
```

## Other

```bash
ls
```
"""

INVENTORY: Dict[str, Any] = {
    "pages": {
        "sdk/sample.md": {
            "class": "journey",
            "journey": "sample",
            "reason": "walked",
        },
    },
    "flow": [],
}


def _users_on_and_off(flag: str, rollout: int) -> List[str]:
    """One user the documented rollout hash gives the flag, then one it does not."""
    on = next(
        f"u-{n}" for n in range(1, 500) if traffic.flag_bucket(f"u-{n}", flag) < rollout
    )
    off = next(
        f"u-{n}"
        for n in range(1, 500)
        if traffic.flag_bucket(f"u-{n}", flag) >= rollout
    )
    return [on, off]


ON, OFF = _users_on_and_off("flag_key", 50)


def _sdk(user: str) -> Dict[str, Any]:
    return {
        "language": "typescript",
        "install": "installation",
        "snippet": "quick-start",
        "key": "sdk-key",
        "experiment": "experiment-id",
        "as": "admin",
        "flag": "flag_key",
        "rollout": 50,
        "user": user,
        "replace": {
            "http://localhost:8000": "{{api-url}}",
            "checkout_flow": "{{experiment}}",
            "new_search": "{{flag}}",
            "user-123": "{{user}}",
        },
        "stubs": ["useNewSearch"],
        "answers": {
            "variant": ["variant"],
            "enabled": ["enabled", "called.useNewSearch"],
        },
    }


GOOD: Dict[str, Any] = {
    "guide": "sdk/sample.md",
    "stack": "compose-dev",
    "profile": "core",
    "video": False,
    "written": TODAY,
    "steps": [
        {
            "id": "create-experiment",
            "doc": "quick-start",
            "do": {
                "api": {"method": "POST", "path": "/api/v1/experiments/", "as": "admin"}
            },
            "expect": {"status": 201},
            "save": {"experiment-id": {"path": "id"}},
            "fail": "x",
        },
        {
            "id": "api-key",
            "doc": "quick-start",
            "do": {
                "api": {"method": "POST", "path": "/api/v1/api-keys", "as": "admin"}
            },
            "expect": {"status": 201},
            "save": {"sdk-key": {"path": "key", "secret": True}},
            "fail": "x",
        },
        {"id": "on", "doc": "quick-start", "do": {"sdk": _sdk(ON)}, "fail": "x"},
        {"id": "off", "doc": "quick-start", "do": {"sdk": _sdk(OFF)}, "fail": "x"},
    ],
}


def _context(tmp_path: Path, page: str = PAGE) -> loader.Context:
    docs = tmp_path / "docs"
    (docs / "sdk").mkdir(parents=True, exist_ok=True)
    (docs / "sdk" / "sample.md").write_text(page, encoding="utf-8")
    return loader.Context(
        docs_root=docs,
        inventory=INVENTORY,
        doc_examples={},
        oracles=oracles.ORACLES,
        today=TODAY,
    )


def _load(tmp_path: Path, data: Dict[str, Any], page: str = PAGE) -> Journey:
    path = tmp_path / "journeys" / "sample.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return loader.load(path, _context(tmp_path, page))


def _with(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    data = copy.deepcopy(GOOD)
    change(data)
    return data


def _on(data: Dict[str, Any]) -> Dict[str, Any]:
    return data["steps"][2]["do"]["sdk"]


def _both(change: Callable[[Dict[str, Any]], None]) -> Callable:
    def apply(data: Dict[str, Any]) -> None:
        change(data["steps"][2]["do"]["sdk"])
        change(data["steps"][3]["do"]["sdk"])

    return apply


# ---------------------------------------------------------------------------
# The real pages and journeys
# ---------------------------------------------------------------------------
def _real_sdk_steps():
    context = loader.context_for(REPO_ROOT)
    for path in sorted((RUNNER_ROOT / "journeys").glob("sdk-*.yaml")):
        journey = loader.load(path, context)
        page = guides.load(REPO_ROOT / "docs", journey.guide)
        for step in journey.steps:
            if step.do.sdk is not None:
                yield journey, page, step


def test_every_sdk_journey_exists_and_holds_two_users():
    seen = {}
    for journey, _, step in _real_sdk_steps():
        seen.setdefault(journey.guide, []).append(step.do.sdk.user)
    assert sorted(seen) == ["sdk/go.md", "sdk/javascript.md", "sdk/python.md"]
    assert all(len(users) == 2 for users in seen.values()), seen


@pytest.mark.parametrize(
    "page, language, tool, package, version",
    [
        ("sdk/javascript.md", "typescript", "npm", "@getexperimently/js-sdk", "0.1.0"),
        ("sdk/python.md", "python", "pip", "experimently", "0.1.0"),
        ("sdk/go.md", "go", "go", "github.com/getexperimently/experimently/sdk/go", ""),
    ],
)
def test_the_pages_install_command_is_read_as_written(
    page, language, tool, package, version
):
    guide = guides.load(REPO_ROOT / "docs", page)
    install = sdk.install_command(guide.section("installation"), language)
    assert (install.tool, install.package, install.version) == (tool, package, version)
    assert install.line in guide.text


def test_the_program_run_is_the_pages_block_with_only_its_placeholders_replaced():
    """The block is held equal to the page: the program holds it, line for line,
    with each change a quoted placeholder put in place of another."""
    for journey, page, step in _real_sdk_steps():
        plan = step.do.sdk
        block = sdk.snippet(page.section(plan.snippet), plan.language)
        assert block.strip() and block in page.text
        values = {
            "{{api-url}}": "http://127.0.0.1:28000",
            "{{experiment}}": "exp_key",
            "{{flag}}": plan.flag,
            "{{user}}": plan.user,
        }
        replaced = sdk.substitute(
            block, {lit: values[v] for lit, v in plan.replace.items()}
        )
        expressions = [e for _, exprs in plan.answers.items() for e in exprs]
        program = sdk.program(plan.language, replaced, plan.stubs, expressions)
        assert replaced.rstrip("\n") in program
        before, after = block.splitlines(), replaced.splitlines()
        assert len(before) == len(after)
        for old, new in zip(before, after):
            if old == new:
                continue
            undone = new
            for literal, placeholder in plan.replace.items():
                for quote in ("'", '"'):
                    undone = undone.replace(
                        f"{quote}{values[placeholder]}{quote}",
                        f"{quote}{literal}{quote}",
                    )
            assert undone == old, (journey.guide, old, new)
        # The key is never in the program: the block reads it from the environment.
        assert "EXPERIMENTLY_API_KEY" in block
        assert "eptk_" not in program


# ---------------------------------------------------------------------------
# Reading a page
# ---------------------------------------------------------------------------
def test_blocks_are_read_with_their_language():
    found = sdk.blocks(
        "text\n```bash\nls\n```\n~~~~{.bash exec}\npwd\n~~~~\n```\nplain\n```\n"
    )
    assert [(b.language, b.text) for b in found] == [
        ("bash", "ls\n"),
        ("bash", "pwd\n"),
        ("", "plain\n"),
    ]


@pytest.mark.parametrize(
    "section, language, message",
    [
        ("no block here\n", "typescript", "has no code block"),
        ("```python\nprint()\n```\n", "typescript", "not a shell block"),
        (
            "```bash\nnpm install a@1.0.0\nnpm install b@1.0.0\n```\n",
            "typescript",
            "holds 2 commands",
        ),
        ("```bash\nnpm install a\n```\n", "typescript", "a package at one version"),
        ("```bash\nnpm i a@1.0.0\n```\n", "typescript", "a package at one version"),
        ("```bash\npip install a==1.0.0\n```\n", "typescript", "not npm install"),
        ("```bash\npip install a>=1.0.0\n```\n", "python", "not pip install"),
        ("```bash\nnpm install a@1.0.0 && rm -rf x\n```\n", "typescript", "npm"),
    ],
)
def test_an_install_command_that_is_not_one_pinned_install_is_refused(
    section, language, message
):
    with pytest.raises(sdk.SdkError, match=re.escape(message)):
        sdk.install_command(section, language)


def test_the_snippet_must_be_in_the_steps_language():
    with pytest.raises(sdk.SdkError, match="first block is python, not typescript"):
        sdk.snippet(
            "```python\nx = 1\n```\n```typescript\nconst x = 1;\n```\n", "typescript"
        )


def test_only_a_whole_quoted_string_is_replaced():
    text = "f('user-123'); g(\"user-123\"); h(user-123); i('user-1234')\n"
    assert sdk.substitute(text, {"user-123": "u-1"}) == (
        "f('u-1'); g(\"u-1\"); h(user-123); i('user-1234')\n"
    )


@pytest.mark.parametrize("value", ["a'b", 'a"b', "a b", "a\\b", "", "x;y"])
def test_a_replacement_is_an_address_or_an_id(value):
    with pytest.raises(sdk.SdkError, match="cannot stand in"):
        sdk.substitute("f('user-123')\n", {"user-123": value})


def test_a_placeholder_the_block_does_not_have_is_an_error():
    with pytest.raises(sdk.SdkError, match="no quoted 'nope'"):
        sdk.substitute("f('user-123')\n", {"nope": "x"})


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------
def test_the_good_journey_loads(tmp_path):
    journey = _load(tmp_path, GOOD)
    assert [s.do.kind for s in journey.steps] == ["api", "api", "sdk", "sdk"]
    assert journey.has_secrets


def _drop(name: str) -> Callable:
    def change(plan: Dict[str, Any]) -> None:
        del plan[name]

    return change


def _set(name: str, value: Any) -> Callable:
    def change(plan: Dict[str, Any]) -> None:
        plan[name] = copy.deepcopy(value)

    return change


def _rename(old: str, new: str) -> Callable:
    """Replace *new* (a part of a string, not a whole one) instead of *old*."""

    def change(plan: Dict[str, Any]) -> None:
        plan["replace"][new] = plan["replace"].pop(old)

    return change


PLANTS = [
    pytest.param(
        lambda d: d.update(stack="docs-local"),
        None,
        "an sdk step needs the compose stack's API",
        id="sdk-on-a-stack-with-no-api",
    ),
    pytest.param(
        lambda d: d["steps"][2].update(expect={"status": 200}),
        None,
        "it has no expect",
        id="sdk-with-an-expect",
    ),
    pytest.param(
        lambda d: d["steps"][2].update(
            snapshot=False, snapshot_reason="no screen here at all"
        ),
        None,
        "an sdk step has no screen to snapshot",
        id="sdk-with-a-snapshot-setting",
    ),
    pytest.param(
        _both(_set("install", "nowhere")),
        None,
        "sdk.install: the guide has no anchor #nowhere",
        id="install-anchor-missing",
    ),
    pytest.param(
        None,
        PAGE.replace(
            "npm install @getexperimently/js-sdk@0.1.0",
            "npm install @getexperimently/js-sdk",
        ),
        "a package at one version",
        id="install-without-a-version",
    ),
    pytest.param(
        None,
        PAGE.replace("```typescript", "```javascript"),
        "first block is javascript, not typescript",
        id="snippet-in-another-language",
    ),
    pytest.param(
        _both(_rename("http://localhost:8000", "localhost:8000")),
        None,
        "is not a quoted string of the block",
        id="replace-something-not-a-placeholder",
    ),
    pytest.param(
        _both(lambda p: p["replace"].update({"http://localhost:8000": "http://x"})),
        None,
        "is not one of the values allowed here",
        id="replace-with-anything-but-a-placeholder",
    ),
    pytest.param(
        _both(lambda p: p["replace"].pop("user-123")),
        None,
        "replace puts nothing in place of {{user}}",
        id="the-user-not-replaced",
    ),
    pytest.param(
        _both(lambda p: p["replace"].update({"http://localhost:8000": "{{user}}"})),
        None,
        "replace puts {{user}} in place of two strings",
        id="one-placeholder-for-two-strings",
    ),
    pytest.param(
        _both(_set("stubs", ["useNewSearch", "neverCalled"])),
        None,
        "the block never calls neverCalled()",
        id="a-stub-the-block-never-calls",
    ),
    pytest.param(
        _both(lambda p: p["answers"].update(variant=["assignment.variantName"])),
        None,
        "the block names no assignment",
        id="an-answer-the-block-does-not-name",
    ),
    pytest.param(
        _both(lambda p: p["answers"].update(enabled=["called.somethingElse"])),
        None,
        "called.<name> names one of the stubs",
        id="called-names-no-stub",
    ),
    pytest.param(
        _both(lambda p: p["answers"].pop("variant")),
        None,
        "variant",
        id="no-variant-answer",
    ),
    pytest.param(
        lambda d: d["steps"][3]["do"]["sdk"].update(user=ON),
        None,
        "the sdk steps' users all get the same answer for flag_key (on)",
        id="every-user-gets-the-flag",
    ),
    pytest.param(
        lambda d: d["steps"].pop(3),
        None,
        "the sdk steps' users all get the same answer for flag_key",
        id="one-user-only",
    ),
    pytest.param(
        lambda d: d["steps"][1]["save"]["sdk-key"].update(secret=False),
        None,
        "the key 'sdk-key' is not a secret",
        id="the-key-not-saved-as-a-secret",
    ),
    pytest.param(
        lambda d: d["steps"][0]["save"]["experiment-id"].update(secret=True),
        None,
        "'experiment-id' is a secret",
        id="the-experiment-saved-as-a-secret",
    ),
    pytest.param(
        lambda d: d["steps"].insert(0, d["steps"].pop(2)),
        None,
        "'experiment-id' is used before any step saves it",
        id="sdk-before-the-experiment-is-saved",
    ),
]


@pytest.mark.parametrize("change, page, expected", PLANTS)
def test_a_planted_sdk_defect_is_refused(tmp_path, change, page, expected):
    data = _with(change) if change is not None else copy.deepcopy(GOOD)
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, data, page or PAGE)
    assert any(expected in problem for problem in refused.value.problems), (
        refused.value.problems
    )


# ---------------------------------------------------------------------------
# Running it, with a fake execute
# ---------------------------------------------------------------------------
class FakeCommands:
    """Records each command; answers as an npm or a pip install and a block would."""

    def __init__(
        self,
        *,
        install_status: int = 0,
        installed: str = "0.1.0",
        block_status: int = 0,
        block_stdout: Optional[str] = None,
        block_stderr: str = "",
        block_error: str = "",
    ):
        self.install_status = install_status
        self.installed = installed
        self.block_status = block_status
        self.block_stdout = block_stdout
        self.block_stderr = block_stderr
        self.block_error = block_error
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, argv, cwd, env, timeout) -> sdk.Result:
        argv = list(argv)
        self.calls.append({"argv": argv, "cwd": Path(cwd), "env": dict(env)})
        if argv[1:3] == ["-m", "venv"]:
            return sdk.Result(status=0)
        if argv[0] == "npm" or argv[0].endswith("/pip"):
            if self.install_status == 0 and argv[0] == "npm":
                manifest = Path(cwd) / "node_modules" / argv[2].rsplit("@", 1)[0]
                manifest.mkdir(parents=True)
                (manifest / "package.json").write_text(
                    json.dumps({"version": self.installed})
                )
            out = "added 1 package\n" if self.install_status == 0 else ""
            err = (
                ""
                if self.install_status == 0
                else "npm error code ETARGET\n"
                "npm error notarget No matching version found for a@0.0.404.\n"
                "npm error notarget In most cases you are requesting\n"
                "npm error A complete log of this run can be found in: /tmp/x.log\n"
            )
            return sdk.Result(status=self.install_status, stdout=out, stderr=err)
        if argv[1:2] == ["-c"]:
            return sdk.Result(status=0, stdout=self.installed + "\n")
        stdout = self.block_stdout
        if stdout is None:
            stdout = sdk.MARKER + json.dumps({"variant": "control"}) + "\n"
        return sdk.Result(
            status=self.block_status if not self.block_error else None,
            stdout=stdout,
            stderr=self.block_stderr,
            error=self.block_error,
        )


NPM = sdk.Install(
    line="npm install @getexperimently/js-sdk@0.1.0",
    tool="npm",
    package="@getexperimently/js-sdk",
    version="0.1.0",
)
PIP = sdk.Install(
    line="pip install experimently==0.1.0",
    tool="pip",
    package="experimently",
    version="0.1.0",
)


def _run(fake: FakeCommands, install: sdk.Install = NPM) -> sdk.Outcome:
    redactor = redaction.Redactor()
    redactor.add(KEY)
    return sdk.run(
        language="typescript" if install.tool == "npm" else "python",
        install=install,
        source="the program\n",
        secrets_env=SECRETS_ENV,
        redact=redactor.redact,
        run_command=fake,
        python="/usr/bin/python3",
    )


def test_a_good_run_installs_as_written_and_reads_the_answers():
    fake = FakeCommands()
    outcome = _run(fake)
    assert outcome.problems == []
    assert outcome.installed == "0.1.0"
    assert outcome.answers == {"variant": "control"}
    assert [c["argv"][:3] for c in fake.calls] == [
        ["npm", "install", "@getexperimently/js-sdk@0.1.0"],
        ["node", "--experimental-strip-types", "--disable-warning=ExperimentalWarning"],
    ]
    assert outcome.commands[0]["run"] == NPM.line


def test_a_python_run_makes_a_fresh_environment_and_installs_into_it():
    fake = FakeCommands()
    outcome = _run(fake, PIP)
    assert outcome.problems == []
    venv = fake.calls[0]["argv"][3]
    assert fake.calls[0]["argv"][:3] == ["/usr/bin/python3", "-m", "venv"]
    assert fake.calls[1]["argv"] == [
        f"{venv}/bin/pip",
        "install",
        "experimently==0.1.0",
    ]
    assert fake.calls[2]["argv"][:2] == [f"{venv}/bin/python", "-c"]
    assert fake.calls[3]["argv"] == [f"{venv}/bin/python", "quick_start.py"]
    assert outcome.commands[1]["run"] == PIP.line


def test_a_failed_install_fails_the_step_and_runs_nothing_more():
    fake = FakeCommands(install_status=1)
    outcome = _run(fake)
    assert outcome.problems == [
        "the page's install, `npm install @getexperimently/js-sdk@0.1.0`, exited 1:"
        " npm error code ETARGET / npm error notarget No matching version found for"
        " a@0.0.404."
    ]
    assert len(fake.calls) == 1


def test_another_version_installed_fails_the_step():
    outcome = _run(FakeCommands(installed="0.1.1"))
    assert outcome.problems == [
        "`npm install @getexperimently/js-sdk@0.1.0` installed"
        " @getexperimently/js-sdk 0.1.1, not 0.1.0"
    ]
    outcome = _run(FakeCommands(installed="0.2.0"), PIP)
    assert "installed experimently 0.2.0, not 0.1.0" in outcome.problems[0]


def test_only_the_block_is_given_the_key_and_nothing_else_is_inherited(monkeypatch):
    monkeypatch.setenv("DOCS_JOURNEY_SENTINEL", "inherited")
    monkeypatch.setenv("NPM_CONFIG_REGISTRY", "https://mirror.invalid/")
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://mirror.invalid/simple")
    fake = FakeCommands()
    _run(fake, PIP)
    for call in fake.calls:
        env = call["env"]
        assert "DOCS_JOURNEY_SENTINEL" not in env
        assert "NPM_CONFIG_REGISTRY" not in env and "PIP_EXTRA_INDEX_URL" not in env
        assert env["npm_config_registry"] == sdk.NPM_REGISTRY
        assert env["PIP_INDEX_URL"] == sdk.PYPI_INDEX
        assert env["PIP_CONFIG_FILE"] == os.devnull
        assert Path(env["HOME"]) == call["cwd"].parent
    keyed = [c for c in fake.calls if set(SECRETS_ENV) & set(c["env"])]
    assert [c["argv"][-1] for c in keyed] == ["quick_start.py"]
    for name, value in SECRETS_ENV.items():
        assert keyed[0]["env"][name] == value, name


def test_the_directory_is_removed_when_the_step_ends():
    fake = FakeCommands()
    _run(fake)
    assert not fake.calls[0]["cwd"].exists()
    fake = FakeCommands(install_status=1)
    _run(fake)
    assert not fake.calls[0]["cwd"].parent.exists()


def test_a_block_that_prints_the_key_fails_and_the_key_is_kept_nowhere():
    printed = f"the key is {KEY}\n" + sdk.MARKER + json.dumps({"variant": "control"})
    outcome = _run(FakeCommands(block_stdout=printed, block_stderr=f"also {KEY}\n"))
    assert outcome.printed_a_secret
    assert outcome.problems[0].startswith("a command printed a value the run keeps")
    kept = json.dumps(outcome.commands) + json.dumps(outcome.answers)
    assert KEY not in kept
    assert "the key is (redacted)" in outcome.commands[-1]["stdout"]
    assert outcome.answers == {"variant": "control"}


@pytest.mark.parametrize(
    "fake, problem",
    [
        (
            FakeCommands(block_status=1, block_stderr="file:///q.mts:3\n    at x\n"),
            "the block exited 1: at x",
        ),
        (
            FakeCommands(
                block_status=1,
                block_stderr="Traceback (most recent call last):\n  File x\nKeyError: 'K'\n",
            ),
            "the block exited 1: KeyError: 'K'",
        ),
        (
            FakeCommands(block_error="stopped after 120 s"),
            "the block stopped after 120 s",
        ),
        (FakeCommands(block_stdout="nothing\n"), "printed 0 times, not once"),
        (
            FakeCommands(block_stdout=f"{sdk.MARKER}{{}}\n{sdk.MARKER}{{}}\n"),
            "printed 2 times, not once",
        ),
        (FakeCommands(block_stdout=f"{sdk.MARKER}not json\n"), "are not JSON"),
    ],
)
def test_a_block_that_does_not_end_with_its_answers_fails(fake, problem):
    outcome = _run(fake)
    assert any(problem in p for p in outcome.problems), outcome.problems


def test_a_command_that_cannot_start_is_a_problem_not_an_error(tmp_path):
    result = sdk.execute([str(tmp_path / "no-such-program")], tmp_path, {"PATH": ""}, 5)
    assert result.status is None
    assert "could not be started" in result.error


def test_a_command_that_runs_too_long_is_stopped(tmp_path):
    result = sdk.execute(
        [sys.executable, "-c", "import time; print('x', flush=True); time.sleep(30)"],
        tmp_path,
        {"PATH": os.environ.get("PATH", "")},
        1,
    )
    assert result.status is None
    assert result.error == "stopped after 1 s"


# ---------------------------------------------------------------------------
# The answers against the oracle
# ---------------------------------------------------------------------------
def test_the_oracle_is_the_documented_hashes():
    split = [("control", 50), ("treatment", 50)]
    wanted = sdk.expected("user-123", "my-flag", split, "my-flag", 80)
    # user-123 / my-flag: assignment bucket 69 (treatment), rollout bucket 79.
    assert wanted == {"variant": "treatment", "enabled": True, "reason": "rollout"}
    assert sdk.expected("user-123", "my-flag", split, "my-flag", 79)["enabled"] is False


def test_each_answer_is_held_to_the_oracle_with_its_type():
    spec = {"variant": ["variant", "a.v"], "enabled": ["enabled", "called.s"]}
    wanted = {"variant": "treatment", "enabled": True, "reason": "rollout"}
    good = {
        "variant": "treatment",
        "a.v": "treatment",
        "enabled": True,
        "called.s": True,
    }
    assert sdk.compare(spec, good, wanted) == []
    bad = {**good, "a.v": "control", "enabled": 1}
    del bad["called.s"]
    assert sdk.compare(spec, bad, wanted) == [
        'a.v is "control"; the documented assignment hash gives "treatment"',
        "enabled is 1; the documented rollout hash gives true",
        'called.s is {"absent": true}; the documented rollout hash gives true',
    ]


def test_a_generated_python_program_reports_its_answers_and_its_stubs(tmp_path):
    block = (
        "class A:\n    variant_name = 'control'\n"
        "assignment = A()\nflags = {'f': True}\nvariant = 'control'\n"
        "if flags['f']:\n    use_it()\n"
    )
    source = sdk.program(
        "python",
        block,
        ["use_it", "never"],
        [
            "variant",
            "assignment.variant_name",
            "flags.f",
            "called.use_it",
            "called.never",
            "nope",
        ],
    )
    (tmp_path / "p.py").write_text(source)
    done = subprocess.run(
        [sys.executable, str(tmp_path / "p.py")],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    answers = sdk.read_answers(done.stdout)
    assert answers == {
        "variant": "control",
        "assignment.variant_name": "control",
        "flags.f": True,
        "called.use_it": True,
        "called.never": False,
        "nope": {"error": "NameError: name 'nope' is not defined"},
    }


def test_a_typescript_program_wraps_the_block_and_reads_each_answer():
    source = sdk.program(
        "typescript",
        "const variant = 'x';\n",
        ["useIt"],
        ["variant", "a.b", "called.useIt"],
    )
    lines = source.splitlines()
    assert lines[0] == "const __docsJourneyCalled = new Set();"
    assert lines[1].startswith("function useIt(")
    assert lines[2] == "const variant = 'x';"
    assert '"a.b": __docsJourneyRead(() => a?.["b"]),' in source
    assert (
        '"called.useIt": __docsJourneyRead(() => __docsJourneyCalled.has("useIt")),'
        in source
    )
    assert f"console.log({json.dumps(sdk.MARKER)}" in source


# ---------------------------------------------------------------------------
# The step in the runner
# ---------------------------------------------------------------------------
@pytest.fixture
def execute_module(monkeypatch):
    """docs_runner.execute with a stand-in for Playwright, which the unit job lacks."""
    import importlib
    import types

    sync_api = types.ModuleType("playwright.sync_api")

    class _Expect:
        def __call__(self, *args, **kwargs):
            raise AssertionError("no browser here")

        def set_options(self, **kwargs):
            return None

    sync_api.Error = type("Error", (Exception,), {})
    sync_api.Browser = sync_api.Page = sync_api.Playwright = object
    sync_api.expect = _Expect()
    package = types.ModuleType("playwright")
    package.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.delitem(sys.modules, "docs_runner.execute", raising=False)
    module = importlib.import_module("docs_runner.execute")
    yield module
    sys.modules.pop("docs_runner.execute", None)


class _Answer:
    def __init__(self, status: int, body: Any):
        self.status = status
        self._body = body

    def json(self):
        return self._body


class _ExperimentApi:
    def get(self, path, headers):
        variants = [
            {"name": "control", "traffic_allocation": 50},
            {"name": "treatment", "traffic_allocation": 50},
        ]
        return _Answer(200, {"key": "exp_key", "variants": variants})


def _step_runner(execute_module, tmp_path: Path, monkeypatch, answers: Dict[str, Any]):
    from docs_runner.stacks import Running

    runner = execute_module.JourneyRunner(
        None,
        None,
        log.Log(tmp_path / "results.jsonl"),
        execute_module.Settings(run_dir=tmp_path, run_id="r", sha="s"),
    )
    runner.guide = guides.parse("sdk/sample.md", PAGE)
    runner.values = {"experiment-id": "e1"}
    runner.secrets = {"sdk-key": KEY}
    runner.redactor.add(KEY)
    running = Running(name="compose-dev", base_url="http://dash/", api_url="http://api")
    runner.tokens[(running.api_url, "admin")] = "already-signed-in-token"
    stdout = sdk.MARKER + json.dumps(answers) + "\n"
    fake = FakeCommands(block_stdout=stdout)
    real_run = sdk.run

    def run(**kwargs):
        return real_run(**kwargs, run_command=fake)

    monkeypatch.setattr(execute_module.sdk, "run", run)
    return runner, running, fake


def _sdk_step(user: str) -> Step:
    return Step.model_validate(
        {"id": "s", "doc": "quick-start", "do": {"sdk": _sdk(user)}, "fail": "x"}
    )


def _oracle_answers(user: str) -> Dict[str, Any]:
    wanted = sdk.expected(
        user, "exp_key", [("control", 50), ("treatment", 50)], "flag_key", 50
    )
    return {
        "variant": wanted["variant"],
        "enabled": wanted["enabled"],
        "called.useNewSearch": wanted["enabled"],
    }


def test_the_step_passes_when_every_answer_is_the_oracles(
    tmp_path, execute_module, monkeypatch
):
    runner, running, fake = _step_runner(
        execute_module, tmp_path, monkeypatch, _oracle_answers(ON)
    )
    observed, snapshot = runner._sdk_step(
        _sdk_step(ON), running, _ExperimentApi(), "j", 3
    )
    assert observed.startswith(
        "installed @getexperimently/js-sdk 0.1.0 from https://registry.npmjs.org/;"
    )
    assert "flag on (rollout), as the documented hashes give" in observed
    written = json.loads((tmp_path / snapshot).read_text())
    assert written["problems"] == []
    assert "'exp_key'" in written["program"] and f"'{ON}'" in written["program"]
    assert "'http://api'" in written["program"] and "user-123" not in written["program"]
    block_env = fake.calls[-1]["env"]
    # Both names: the pages under test are the deployed site's, which may
    # predate the rename of these variables.
    for name in ("CONTROLVAR_API_KEY", "EXPERIMENTLY_API_KEY"):
        assert block_env[name] == KEY, name
    for name in ("CONTROLVAR_API_URL", "EXPERIMENTLY_API_URL"):
        assert block_env[name] == "http://api", name


def test_a_swapped_variant_fails_the_step_and_both_snapshots_are_written(
    tmp_path, execute_module, monkeypatch
):
    """The record of a FAIL names its expected and its observed file, so the
    report shows them side by side, and neither holds the key."""
    answers = _oracle_answers(OFF)
    answers["variant"] = "treatment" if answers["variant"] == "control" else "control"
    runner, running, _ = _step_runner(execute_module, tmp_path, monkeypatch, answers)
    journey = Journey.model_validate(
        {**GOOD, "steps": GOOD["steps"][:2] + [GOOD["steps"][3]]}
    )
    record = runner._step(
        journey,
        "j",
        3,
        journey.steps[2],
        running,
        None,
        _ExperimentApi(),
        None,
        runner.guide.anchors,
        False,
    )
    assert record.result == "FAIL"
    assert "variant is" in record.observed
    assert "the documented assignment hash gives" in record.observed
    assert record.expected_snapshot == "j/03-off.expected.json"
    assert record.snapshot == "j/03-off.sdk.json"
    for name in (record.expected_snapshot, record.snapshot):
        assert KEY not in (tmp_path / name).read_text()
    outcome = report.GuideRun(
        journey="j",
        guide="sdk/sample.md",
        title="Sample SDK",
        stack="compose-dev",
        records=(record,),
    )
    rendered = report.guide_markdown(outcome, tmp_path, date="2026-10-07", sha="s")
    assert "03-off.sdk.json" in rendered


def test_a_step_whose_experiment_cannot_be_read_fails_with_its_file(
    tmp_path, execute_module, monkeypatch
):
    runner, running, fake = _step_runner(execute_module, tmp_path, monkeypatch, {})

    class _Refusing:
        def get(self, path, headers):
            return _Answer(404, {"detail": "Not found"})

    with pytest.raises(execute_module._Failed) as failed:
        runner._sdk_step(_sdk_step(ON), running, _Refusing(), "j", 3)
    assert failed.value.observed == "reading the experiment answered 404"
    assert (tmp_path / failed.value.snapshot).is_file()
    assert fake.calls == []


def test_the_step_hands_its_runs_redactor_to_the_block(
    tmp_path, execute_module, monkeypatch
):
    """The step passes the run's own redactor to the block, so a quick start
    that prints the key fails the step: the end-of-run scan would only clean
    the files, not fail the guide."""
    runner, running, fake = _step_runner(
        execute_module, tmp_path, monkeypatch, _oracle_answers(ON)
    )
    fake.block_stdout = (
        f"my key: {KEY}\n" + sdk.MARKER + json.dumps(_oracle_answers(ON)) + "\n"
    )
    with pytest.raises(execute_module._Failed) as failed:
        runner._sdk_step(_sdk_step(ON), running, _ExperimentApi(), "j", 3)
    assert "printed a value the run keeps out of its files" in failed.value.observed
    assert KEY not in failed.value.observed
    assert KEY not in (tmp_path / failed.value.snapshot).read_text()


def test_the_page_install_and_block_are_read_at_the_step(
    tmp_path, execute_module, monkeypatch
):
    """A page whose install line changed after the load is what the step runs."""
    runner, running, fake = _step_runner(
        execute_module, tmp_path, monkeypatch, _oracle_answers(ON)
    )
    runner.guide = guides.parse(
        "sdk/sample.md", PAGE.replace("js-sdk@0.1.0", "js-sdk@0.0.404")
    )
    fake.install_status = 1
    with pytest.raises(execute_module._Failed) as failed:
        runner._sdk_step(_sdk_step(ON), running, _ExperimentApi(), "j", 3)
    assert failed.value.observed.startswith(
        "the page's install, `npm install @getexperimently/js-sdk@0.0.404`, exited 1"
    )
    assert fake.calls[0]["argv"] == [
        "npm",
        "install",
        "@getexperimently/js-sdk@0.0.404",
    ]
