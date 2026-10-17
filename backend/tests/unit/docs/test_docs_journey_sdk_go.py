"""The docs-journey runner's sdk step for Go: docs/sdk/go.md followed as written.

The sdk-go journey (#1075) runs the page's own ``go mod init`` and ``go get``
in the page's order, holds the toolchain to the line sdk/go/go.mod names,
records what ``go get`` resolved and compares it with this checkout's sdk/go,
and runs the page's complete Quick Start program, which prints one
``key=value`` answer line (``docs_runner/sdk.py``). What decides whether that
check can fail is tested here, in the unit job:

* the page requires the Go version sdk/go/go.mod declares, and the loader
  refuses a Go step on a page that says otherwise;
* the page's install commands are run as written and in its order, and the
  runner writes no go.mod, so a page without ``go mod init`` fails at
  ``go get`` with the go command's own words;
* ``go version`` must be the line of go.mod's directive;
* a ``replace`` of the module is refused, and the resolved version is a
  pseudo-version or a semver tag; on a branch run a pseudo-version must be
  this checkout's sdk/go, while a tag run or a tagged module only records the
  difference;
* the program's one answer line is parsed exactly, and held to the oracle.

The go commands go through a fake ``execute``; no Go toolchain, no network,
no browser, no git. Temporary files only.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import guide as guides
from docs_runner import loader, log, oracles, redaction, sdk, traffic
from docs_runner.model import Step

from backend.tests.unit.docs.test_docs_journey_sdk import (
    KEY,
    SECRETS_ENV,
    TODAY,
    _ExperimentApi,
    execute_module,
)

MODULE = "github.com/getexperimently/experimently/sdk/go"
PSEUDO = "v0.0.0-20261007170856-0ee808b07642"
GO_PAGE = REPO_ROOT / "docs" / "sdk" / "go.md"
GO_MOD = REPO_ROOT / "sdk" / "go" / "go.mod"
#: sdk/go/go.mod's `go` version and its line: the fixtures below follow it, so
#: only the tests that hold the real page to it fail when one moves alone.
DIRECTIVE = sdk.go_directive(GO_MOD.read_text(encoding="utf-8"))
LINE = ".".join(DIRECTIVE.split(".")[:2])
OTHER = f"{LINE.split('.')[0]}.{int(LINE.split('.')[1]) + 1}.12"
TOOLCHAIN = f"go version go{LINE}.13 linux/amd64"

PAGE = """# Sample Go SDK

Requires Go GO_DIRECTIVE+.

## Installation

```bash
go mod init example.com/quickstart
go get github.com/getexperimently/experimently/sdk/go
```

## Quick Start

```go
package main

import (
	"fmt"
	"os"

	exp "github.com/getexperimently/experimently/sdk/go"
)

func main() {
	client, _ := exp.New(exp.WithBaseURL("http://localhost:8000"), exp.WithAPIKey(os.Getenv("EXPERIMENTLY_API_KEY")))
	user := &exp.User{ID: "user-123"}
	a, _ := client.GetAssignment(nil, "checkout_flow", user)
	flag, _ := client.EvaluateFlag(nil, "new_search", user)
	fmt.Printf("variant=%s flag=%t\\n", a.VariantName, flag.Enabled)
}
```
""".replace("GO_DIRECTIVE", DIRECTIVE)

INVENTORY: Dict[str, Any] = {
    "pages": {
        "sdk/sample.md": {"class": "journey", "journey": "sample", "reason": "walked"},
    },
    "flow": [],
}


def _users_on_and_off(flag: str, rollout: int) -> List[str]:
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
        "language": "go",
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
        "answers": {"variant": ["variant"], "enabled": ["flag"]},
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


# ---------------------------------------------------------------------------
# The real page, the real go.mod, the real journey
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_the_page_requires_the_go_version_its_module_declares():
    """docs/sdk/go.md's "Requires Go X+" is sdk/go/go.mod's ``go`` directive.

    setup-go installs the directive's line for the walk, so a page that names
    another version would be walked with a toolchain it does not promise."""
    page = GO_PAGE.read_text(encoding="utf-8")
    directive = sdk.go_directive(GO_MOD.read_text(encoding="utf-8"))
    assert sdk.go_requirement(page) == directive
    assert sdk.go_requirement_problem(page, GO_MOD.read_text(encoding="utf-8")) is None


@pytest.mark.regression
def test_the_pages_install_makes_a_module_before_go_get():
    """The page as it was ran `go get` outside a module, which the go command
    refuses ("go.mod file not found"): the reader needs `go mod init` first."""
    page = guides.load(REPO_ROOT / "docs", "sdk/go.md")
    install = sdk.install_command(page.section("installation"), "go")
    assert install.commands == (
        "go mod init example.com/quickstart",
        f"go get {MODULE}",
    )
    assert (install.tool, install.package, install.version) == ("go", MODULE, "")


@pytest.mark.regression
def test_the_quick_start_is_a_complete_program_that_prints_one_answer_line():
    """The page's Quick Start was a fragment that did not compile (a variable
    declared and not used) and swallowed every error, so it exited 0 against
    an API that was not there. It is now a `package main` that stops on an
    error and prints the line the walk reads."""
    page = guides.load(REPO_ROOT / "docs", "sdk/go.md")
    block = sdk.snippet(page.section("quick-start"), "go")
    assert block.startswith("package main\n")
    assert "func main() {" in block
    assert block.count("fmt.Printf(") == 1
    assert 'fmt.Printf("variant=%s flag=%t\\n", a.VariantName, flag.Enabled)' in block
    assert "_ =" not in block and ", _ :=" not in block
    for call in ("GetAssignment", "EvaluateFlag", "Track"):
        assert f"client.{call}(" in block
    assert block.count("if err != nil {") == 5
    assert "variant=treatment flag=true" in page.section("quick-start")


def test_the_go_journey_holds_one_user_in_each_variant_and_flag_answer():
    context = loader.context_for(REPO_ROOT)
    journey = loader.load(RUNNER_ROOT / "journeys" / "sdk-go.yaml", context)
    assert journey.guide == "sdk/go.md"
    plans = [step.do.sdk for step in journey.steps if step.do.sdk is not None]
    assert [plan.language for plan in plans] == ["go", "go"]
    split = [("control", 50), ("treatment", 50)]
    seen = {
        (
            traffic.variant_for(plan.user, "docs_journey_sdk_go", split),
            traffic.expected_answer(plan.user, plan.flag, "rollout", plan.rollout)[0],
        )
        for plan in plans
    }
    assert seen == {("treatment", True), ("control", False)}
    inventory = loader.read_inventory(RUNNER_ROOT / "inventory.toml")
    assert inventory["pages"]["sdk/go.md"]["class"] == "journey"
    assert inventory["pages"]["sdk/go.md"]["journey"] == "sdk-go"
    flow = next(f for f in inventory["flow"] if f["text"] == "the SDK quick starts")
    assert "sdk-go" in flow["journeys"] and "sdk/go.md" not in flow["pages"]


# ---------------------------------------------------------------------------
# Reading the page
# ---------------------------------------------------------------------------
def test_a_page_without_go_mod_init_is_read_so_its_walk_fails_as_its_reader_does():
    install = sdk.install_command(f"```bash\ngo get {MODULE}\n```\n", "go")
    assert install.commands == (f"go get {MODULE}",)


def test_the_commands_keep_the_pages_order():
    install = sdk.install_command(
        f"```bash\ngo get {MODULE}@v0.1.0\ngo mod init example.com/x\n```\n", "go"
    )
    assert install.commands == (f"go get {MODULE}@v0.1.0", "go mod init example.com/x")
    assert install.version == "v0.1.0"


@pytest.mark.parametrize(
    "block, problem",
    [
        (f"go get -u {MODULE}", "is not `go mod init <path>` or `go get"),
        (f"go install {MODULE}@latest", "is not `go mod init <path>`"),
        (
            f"go mod init x.com/y\ngo mod edit -replace={MODULE}=../go\ngo get {MODULE}",
            "is not `go mod init <path>`",
        ),
        (f"go get {MODULE} && rm -rf ~", "is not `go mod init <path>`"),
        (f"go get {MODULE}@latest", "is not `go mod init <path>`"),
        ("go mod init x.com/y", "holds 0 go get commands, not one"),
        (f"go get {MODULE}\ngo get {MODULE}", "holds 2 go get commands"),
        (
            f"go mod init x.com/y\ngo mod init x.com/z\ngo get {MODULE}",
            "holds 2 go mod init commands",
        ),
        (f"npm install {MODULE}@1.0.0", "is not `go mod init <path>`"),
    ],
)
def test_an_install_block_that_is_not_the_pages_go_commands_is_refused(block, problem):
    with pytest.raises(sdk.SdkError, match=problem.replace("(", r"\(")):
        sdk.install_command(f"```bash\n{block}\n```\n", "go")


@pytest.mark.parametrize(
    "page, go_mod, problem",
    [
        ("Requires Go 1.21+.", "module m\n\ngo 1.21\n", None),
        (
            "Requires Go 1.21+.",
            "module m\n\ngo 1.22\n",
            'the page says "Requires Go 1.21+", but sdk/go/go.mod says go 1.22',
        ),
        ("Go 1.21 or later.", "module m\n\ngo 1.21\n", '"Requires Go X+" 0 times'),
        (
            "Requires Go 1.21+. Requires Go 1.22+.",
            "module m\n\ngo 1.21\n",
            '"Requires Go X+" 2 times',
        ),
        ("Requires Go 1.21+.", "module m\n", "has 0 go directives"),
    ],
)
def test_the_pages_go_requirement_must_be_the_modules_directive(page, go_mod, problem):
    found = sdk.go_requirement_problem(page, go_mod)
    if problem is None:
        assert found is None
    else:
        assert problem in found


@pytest.mark.parametrize(
    "printed, directive, problem",
    [
        ("go version go1.21.13 linux/amd64\n", "1.21", None),
        ("go version go1.21.0 darwin/arm64\n", "1.21", None),
        ("go version go1.21.13 linux/amd64\n", "1.21.0", None),
        ("go version go1.22.12 linux/amd64\n", "1.21", "is go1.22.12, not go1.21.x"),
        ("go version go1.2.2 linux/amd64\n", "1.21", "is go1.2.2, not go1.21.x"),
        ("go version go1.210 linux/amd64\n", "1.21", "is go1.210, not go1.21.x"),
        ("bash: go: command not found\n", "1.21", "not a Go version"),
    ],
)
def test_the_toolchain_must_be_the_line_go_mod_names(printed, directive, problem):
    found = sdk.go_version_problem(printed, directive)
    if problem is None:
        assert found is None
    else:
        assert problem in found


# ---------------------------------------------------------------------------
# What go get resolved
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "version, kind",
    [
        (PSEUDO, "pseudo-version"),
        ("v0.1.1-0.20261007170856-0ee808b07642", "pseudo-version"),
        ("v0.1.0-rc.1.0.20261007170856-0ee808b07642", "pseudo-version"),
        ("v0.1.0", "tag"),
        ("v1.2.3-rc.1", "tag"),
    ],
)
def test_the_resolved_version_is_a_pseudo_version_or_a_tag(version, kind):
    assert sdk.classify(version) == kind


@pytest.mark.parametrize(
    "version", ["", "(devel)", "latest", "main", "0.1.0", "v1.2", "v1.2.3+build"]
)
def test_any_other_resolved_version_is_refused(version):
    with pytest.raises(sdk.SdkError, match="neither a pseudo-version nor a semver tag"):
        sdk.classify(version)


@pytest.mark.parametrize(
    "kind, ref, rule",
    [
        ("pseudo-version", "refs/heads/main", "compare"),
        ("pseudo-version", "refs/heads/test/b1-go-sdk-journey", "compare"),
        ("pseudo-version", "", "compare"),
        ("pseudo-version", "refs/tags/v0.27.0", "record"),
        ("tag", "refs/heads/main", "record"),
        ("tag", "refs/tags/v0.27.0", "record"),
    ],
)
def test_a_branch_run_of_the_head_of_main_compares_and_a_tag_records(kind, ref, rule):
    assert sdk.comparison_rule(kind, ref) == rule


@pytest.mark.parametrize(
    "listed, go_mod, problem",
    [
        ({"Path": MODULE, "Version": PSEUDO}, "module x\n\ngo 1.21\n", None),
        (
            {"Path": MODULE, "Version": PSEUDO, "Replace": {"Path": "../go"}},
            "module x\n",
            f"replaces {MODULE} with ../go",
        ),
        (
            {"Path": MODULE, "Version": PSEUDO},
            f"module x\n\nreplace {MODULE} => ../go\n",
            "has a replace directive",
        ),
        (
            {"Path": MODULE, "Version": PSEUDO},
            f"module x\n\nreplace (\n\t{MODULE} => example.com/fork v1.0.0\n)\n",
            "has a replace directive",
        ),
    ],
)
def test_a_replace_of_the_module_is_refused(listed, go_mod, problem):
    found = sdk.replace_problem(listed, go_mod)
    if problem is None:
        assert found is None
    else:
        assert problem in found


def _tree(root: Path, files: Mapping[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


SOURCE = {
    "go.mod": "module m\n\ngo 1.21\n",
    "client.go": "package m\n",
    "x/y.go": "y\n",
}


def test_the_module_is_compared_with_the_checkout_file_by_file(tmp_path):
    ours = _tree(tmp_path / "ours", SOURCE)
    assert sdk.differences(_tree(tmp_path / "same", SOURCE), ours) == []
    changed = {**SOURCE, "client.go": "package m // changed\n", "new.go": "n\n"}
    del changed["x/y.go"]
    assert sdk.differences(_tree(tmp_path / "theirs", changed), ours) == [
        "client.go: differs",
        "new.go: only in the module go get resolved",
        "x/y.go: only in this checkout",
    ]


# ---------------------------------------------------------------------------
# The answer line
# ---------------------------------------------------------------------------
def test_the_answer_line_is_read_with_its_types():
    assert sdk.read_line("variant=treatment flag=true\n", ["variant", "flag"]) == {
        "variant": "treatment",
        "flag": True,
    }
    assert sdk.read_line("\nflag=false variant=control\n\n", ["variant", "flag"]) == {
        "variant": "control",
        "flag": False,
    }
    # "1" is not true: the oracle compares JSON types.
    answers = sdk.read_line("variant=control flag=1\n", ["variant", "flag"])
    wanted = {"variant": "control", "enabled": True, "reason": "rollout"}
    assert sdk.compare({"enabled": ["flag"]}, answers, wanted) == [
        'flag is "1"; the documented rollout hash gives true'
    ]


@pytest.mark.parametrize(
    "stdout, problem",
    [
        ("", "printed 0 lines on stdout"),
        ("starting\nvariant=control flag=true\n", "printed 2 lines on stdout"),
        ("variant=control  flag=true\n", "is not key=value pairs"),
        ("variant=control flag=true \n", "is not key=value pairs"),
        ("variant=control flag\n", "is not key=value pairs"),
        ("variant=control variant=treatment\n", "is not key=value pairs"),
        ("variant= flag=true\n", "is not key=value pairs"),
        ("variant=control\n", "names variant, not flag, variant"),
        ("variant=control flag=true extra=1\n", "names extra, flag, variant"),
    ],
)
def test_an_answer_line_that_is_not_exactly_the_steps_keys_is_refused(stdout, problem):
    with pytest.raises(sdk.SdkError, match=problem):
        sdk.read_line(stdout, ["variant", "flag"])


# ---------------------------------------------------------------------------
# Running it, with a fake go command
# ---------------------------------------------------------------------------
NOT_A_MODULE = (
    "go: go.mod file not found in current directory or any parent directory.\n"
    "\t'go get' is no longer supported outside a module.\n"
    "\tTo build and install a command, use 'go install' with a version,\n"
    "\tlike 'go install example.com/cmd@latest'\n"
    "\tFor more information, see https://golang.org/doc/go-get-install-deprecation\n"
    "\tor run 'go help get' or 'go help install'.\n"
)


class FakeGo:
    """Answers each go command of a Go step as the go command would."""

    def __init__(
        self,
        source: Path,
        *,
        version: str = TOOLCHAIN,
        resolved: str = PSEUDO,
        replace: Optional[Dict[str, str]] = None,
        module_files: Optional[Mapping[str, str]] = None,
        run_status: int = 0,
        run_stdout: str = "variant=treatment flag=true\n",
        run_stderr: str = "",
    ):
        self.source = source
        self.version = version
        self.resolved = resolved
        self.replace = replace
        self.module_files = module_files
        self.run_status = run_status
        self.run_stdout = run_stdout
        self.run_stderr = run_stderr
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, argv, cwd, env, timeout) -> sdk.Result:
        argv = list(argv)
        cwd = Path(cwd)
        self.calls.append({"argv": argv, "cwd": cwd, "env": dict(env)})
        go_mod = cwd / "go.mod"
        if argv[:2] == ["go", "version"]:
            return sdk.Result(status=0, stdout=self.version + "\n")
        if argv[:3] == ["go", "mod", "init"]:
            go_mod.write_text(f"module {argv[3]}\n\ngo 1.21.13\n")
            return sdk.Result(
                status=0, stderr=f"go: creating new go.mod: module {argv[3]}\n"
            )
        if argv[:2] == ["go", "get"]:
            if not go_mod.is_file():
                return sdk.Result(status=1, stderr=NOT_A_MODULE)
            with go_mod.open("a") as handle:
                handle.write(f"\nrequire {MODULE} {self.resolved} // indirect\n")
            return sdk.Result(
                status=0,
                stderr=f"go: downloading {MODULE} {self.resolved}\n"
                f"go: added {MODULE} {self.resolved}\n",
            )
        if argv[:4] == ["go", "list", "-m", "-json"]:
            listed: Dict[str, Any] = {"Path": argv[4], "Version": self.resolved}
            if self.replace:
                listed["Replace"] = self.replace
            return sdk.Result(status=0, stdout=json.dumps(listed, indent="\t"))
        if argv[:4] == ["go", "mod", "download", "-json"]:
            module = Path(env["GOMODCACHE"]) / argv[4].replace("/", "_")
            if self.module_files is not None:
                _tree(module, self.module_files)
            else:
                shutil.copytree(self.source, module)
            # The go command keeps its module cache read-only.
            for root, dirs, files in os.walk(module, topdown=False):
                for name in files:
                    os.chmod(os.path.join(root, name), stat.S_IRUSR)
                os.chmod(root, stat.S_IRUSR | stat.S_IXUSR)
            os.chmod(module, stat.S_IRUSR | stat.S_IXUSR)
            return sdk.Result(status=0, stdout=json.dumps({"Dir": str(module)}))
        if argv[:3] == ["go", "run", "."]:
            assert (cwd / "main.go").is_file()
            return sdk.Result(
                status=self.run_status, stdout=self.run_stdout, stderr=self.run_stderr
            )
        raise AssertionError(f"unexpected command {argv}")


INSTALL = sdk.install_command(
    f"```bash\ngo mod init example.com/quickstart\ngo get {MODULE}\n```\n", "go"
)


def _run(
    fake: FakeGo,
    checkout: Path,
    *,
    install: sdk.Install = INSTALL,
    ref: str = "refs/heads/main",
) -> sdk.Outcome:
    redactor = redaction.Redactor()
    redactor.add(KEY)
    return sdk.run(
        language="go",
        install=install,
        source="package main\n",
        secrets_env=SECRETS_ENV,
        redact=redactor.redact,
        run_command=fake,
        go=sdk.GoCheck(directive=DIRECTIVE, checkout=checkout, ref=ref),
        answer_keys=["variant", "flag"],
    )


@pytest.fixture
def checkout(tmp_path) -> Path:
    return _tree(tmp_path / "checkout" / "sdk" / "go", SOURCE)


def test_a_good_run_follows_the_page_and_reads_the_answer_line(checkout):
    fake = FakeGo(checkout)
    outcome = _run(fake, checkout)
    assert outcome.problems == []
    assert outcome.answers == {"variant": "treatment", "flag": True}
    assert outcome.installed == PSEUDO
    assert outcome.toolchain == TOOLCHAIN
    assert outcome.module == {
        "version": PSEUDO,
        "kind": "pseudo-version",
        "ref": "refs/heads/main",
        "rule": "compare",
        "compared_with": "sdk/go of this checkout",
        "differences": [],
    }
    assert [call["argv"] for call in fake.calls] == [
        ["go", "version"],
        ["go", "mod", "init", "example.com/quickstart"],
        ["go", "get", MODULE],
        ["go", "list", "-m", "-json", MODULE],
        ["go", "mod", "download", "-json", f"{MODULE}@{PSEUDO}"],
        ["go", "run", "."],
    ]
    assert [c["run"] for c in outcome.commands][1:3] == list(INSTALL.commands)


def test_the_go_commands_use_the_public_proxy_and_nothing_inherited(
    checkout, monkeypatch
):
    monkeypatch.setenv("GOPROXY", "https://mirror.invalid")
    monkeypatch.setenv("GOFLAGS", "-mod=mod")
    monkeypatch.setenv("GOTOOLCHAIN", "auto")
    monkeypatch.setenv("GONOSUMDB", "github.com")
    fake = FakeGo(checkout)
    _run(fake, checkout)
    for call in fake.calls:
        env = call["env"]
        assert env["GOPROXY"] == "https://proxy.golang.org,direct"
        assert env["GOSUMDB"] == "sum.golang.org"
        assert env["GOTOOLCHAIN"] == "local"
        assert env["GOFLAGS"] == "" and env["GONOSUMDB"] == ""
        assert env["GOENV"] == "off" and env["GOWORK"] == "off"
        home = call["cwd"].parent
        assert Path(env["HOME"]) == home
        assert Path(env["GOMODCACHE"]).is_relative_to(home)
        assert Path(env["GOCACHE"]).is_relative_to(home)
    keyed = [c for c in fake.calls if set(SECRETS_ENV) & set(c["env"])]
    assert [c["argv"] for c in keyed] == [["go", "run", "."]]
    for name, value in SECRETS_ENV.items():
        assert keyed[0]["env"][name] == value, name


def test_the_directory_and_its_read_only_module_cache_are_removed(checkout):
    fake = FakeGo(checkout)
    _run(fake, checkout)
    home = fake.calls[0]["cwd"].parent
    assert not home.exists()


def test_a_page_without_go_mod_init_fails_at_go_get_with_the_go_commands_words(
    checkout,
):
    """The runner writes no go.mod: the reader's failure is the step's."""
    fake = FakeGo(checkout)
    install = sdk.install_command(f"```bash\ngo get {MODULE}\n```\n", "go")
    outcome = _run(fake, checkout, install=install)
    assert outcome.problems == [
        f"the page's install, `go get {MODULE}`, exited 1: go: go.mod file not found"
        " in current directory or any parent directory."
    ]
    assert [call["argv"][:2] for call in fake.calls] == [
        ["go", "version"],
        ["go", "get"],
    ]


def test_another_go_line_fails_before_anything_is_installed(checkout):
    fake = FakeGo(checkout, version=f"go version go{OTHER} linux/amd64")
    outcome = _run(fake, checkout)
    assert len(outcome.problems) == 1
    assert f"`go version` is go{OTHER}, not go{LINE}.x" in outcome.problems[0]
    assert len(fake.calls) == 1


def test_a_replaced_module_is_refused(checkout):
    fake = FakeGo(checkout, replace={"Path": "../sdk/go"})
    outcome = _run(fake, checkout)
    assert outcome.problems == [
        f"the project's go.mod replaces {MODULE} with ../sdk/go; the module built"
        " must be the one go get resolved"
    ]
    assert fake.calls[-1]["argv"][:3] == ["go", "list", "-m"]


def test_a_branch_run_whose_module_is_not_this_checkout_fails(checkout):
    changed = {**SOURCE, "client.go": "package m // a later commit\n"}
    for ref in ("refs/heads/main", "refs/heads/some-branch", ""):
        fake = FakeGo(checkout, module_files=changed)
        outcome = _run(fake, checkout, ref=ref)
        assert len(outcome.problems) == 1, ref
        assert outcome.problems[0].startswith(
            f"the module go get resolved, {MODULE} {PSEUDO}, is not this checkout's"
            " sdk/go (client.go: differs)"
        )
        assert "the module proxy's cache window" in outcome.problems[0]
        assert outcome.module["differences"] == ["client.go: differs"]
        # The program still runs, so the file shows what it answered too.
        assert outcome.answers == {"variant": "treatment", "flag": True}


def test_a_tag_run_or_a_tagged_module_records_the_difference(checkout):
    changed = {**SOURCE, "client.go": "package m // main moved on\n"}
    outcome = _run(
        FakeGo(checkout, module_files=changed), checkout, ref="refs/tags/v0.27.0"
    )
    assert outcome.problems == []
    assert outcome.module["rule"] == "record"
    assert outcome.module["differences"] == ["client.go: differs"]
    outcome = _run(
        FakeGo(checkout, module_files=changed, resolved="v0.1.0"),
        checkout,
        ref="refs/heads/main",
    )
    assert outcome.problems == []
    assert (outcome.module["kind"], outcome.module["rule"]) == ("tag", "record")


def test_a_version_the_page_pins_must_be_the_one_resolved(checkout):
    install = sdk.install_command(
        f"```bash\ngo mod init x.com/y\ngo get {MODULE}@v0.1.0\n```\n", "go"
    )
    outcome = _run(FakeGo(checkout, resolved="v0.1.1"), checkout, install=install)
    assert outcome.problems == [
        f"`go get {MODULE}@v0.1.0` resolved {MODULE} v0.1.1, not v0.1.0"
    ]


def test_an_unclassified_resolution_fails(checkout):
    outcome = _run(FakeGo(checkout, resolved="(devel)"), checkout)
    assert outcome.problems == [
        "go get resolved (devel), neither a pseudo-version nor a semver tag"
    ]


def test_a_program_that_stops_on_an_error_fails_the_step_with_its_words(checkout):
    """Against an API that does not answer, the page's program stops: `go run`
    exits 1 and its own "exit status 1" line is not what is reported."""
    fake = FakeGo(
        checkout,
        run_status=1,
        run_stdout="",
        run_stderr='2026/10/07 11:09:09 assignment: assign experiment "x": http'
        " request failed: dial tcp 127.0.0.1:1: connect: connection refused\n"
        "exit status 1\n",
    )
    outcome = _run(fake, checkout)
    assert outcome.problems == [
        'the block exited 1: 2026/10/07 11:09:09 assignment: assign experiment "x":'
        " http request failed: dial tcp 127.0.0.1:1: connect: connection refused"
    ]


def test_a_program_that_prints_the_key_fails_and_the_key_is_kept_nowhere(checkout):
    fake = FakeGo(checkout, run_stdout=f"key {KEY}\nvariant=control flag=false\n")
    outcome = _run(fake, checkout)
    assert outcome.printed_a_secret
    assert outcome.problems[0].startswith("a command printed a value the run keeps")
    assert "printed 2 lines on stdout" in outcome.problems[1]
    assert KEY not in json.dumps(outcome.commands)


def test_a_go_run_needs_what_it_is_held_to():
    with pytest.raises(sdk.SdkError, match="needs the toolchain line"):
        sdk.run(
            language="go",
            install=INSTALL,
            source="",
            secrets_env={},
            redact=lambda text: text,
            run_command=lambda *a: sdk.Result(status=0),
        )


def test_a_go_block_is_run_as_it_is_and_takes_no_stubs():
    assert sdk.program("go", "package main\n", [], ["variant"]) == "package main\n"
    with pytest.raises(sdk.SdkError, match="takes no stubs"):
        sdk.program("go", "package main\n", ["useIt"], ["variant"])


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------
def _context(tmp_path: Path, page: str, go_mod: Optional[str]) -> loader.Context:
    docs = tmp_path / "docs"
    (docs / "sdk").mkdir(parents=True, exist_ok=True)
    (docs / "sdk" / "sample.md").write_text(page, encoding="utf-8")
    if go_mod is not None:
        (tmp_path / "sdk" / "go").mkdir(parents=True, exist_ok=True)
        (tmp_path / "sdk" / "go" / "go.mod").write_text(go_mod, encoding="utf-8")
    return loader.Context(
        docs_root=docs,
        inventory=INVENTORY,
        doc_examples={},
        oracles=oracles.ORACLES,
        today=TODAY,
    )


def _load(tmp_path: Path, data: Dict[str, Any], page: str = PAGE, go_mod=None):
    import yaml

    path = tmp_path / "journeys" / "sample.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    go_mod = f"module m\n\ngo {DIRECTIVE}\n" if go_mod is None else go_mod
    return loader.load(path, _context(tmp_path, page, go_mod or None))


def _both(change) -> Any:
    def apply(data: Dict[str, Any]) -> None:
        change(data["steps"][2]["do"]["sdk"])
        change(data["steps"][3]["do"]["sdk"])

    data = copy.deepcopy(GOOD)
    apply(data)
    return data


def test_the_good_go_journey_loads(tmp_path):
    journey = _load(tmp_path, copy.deepcopy(GOOD))
    assert [s.do.sdk.language for s in journey.steps if s.do.sdk] == ["go", "go"]


PLANTS = [
    pytest.param(
        _both(lambda p: p.update(stubs=["useIt"])),
        PAGE,
        None,
        "a go block is a complete program; it takes no stubs",
        id="stubs-on-go",
    ),
    pytest.param(
        _both(lambda p: p["answers"].update(variant=["a.VariantName"])),
        PAGE,
        None,
        "a go block's answers are keys of the one key=value line",
        id="a-path-as-a-go-answer",
    ),
    pytest.param(
        _both(lambda p: p["answers"].update(variant=["arm"])),
        PAGE,
        None,
        "sdk.answers: arm: the block prints no arm=",
        id="a-key-the-block-does-not-print",
    ),
    pytest.param(
        _both(lambda p: p["replace"].pop("http://localhost:8000")),
        PAGE,
        None,
        "replace puts nothing in place of {{api-url}}",
        marks=pytest.mark.regression,
        id="the-api-address-not-replaced",
    ),
    pytest.param(
        copy.deepcopy(GOOD),
        PAGE,
        "module m\n\ngo 1.99\n",
        f'the page says "Requires Go {DIRECTIVE}+", but sdk/go/go.mod says go 1.99',
        id="go-mod-moved-the-page-did-not",
    ),
    pytest.param(
        copy.deepcopy(GOOD),
        PAGE,
        "",
        "sdk/go/go.mod is missing",
        id="no-go-mod",
    ),
    pytest.param(
        copy.deepcopy(GOOD),
        PAGE.replace("go mod init example.com/quickstart", "go mod tidy"),
        None,
        "is not `go mod init <path>` or `go get <module>[@<version>]`",
        id="another-command-in-the-install",
    ),
    pytest.param(
        copy.deepcopy(GOOD),
        PAGE.replace("```go", "```golang"),
        None,
        "first block is golang, not go",
        id="snippet-not-go",
    ),
]


@pytest.mark.parametrize("data, page, go_mod, expected", PLANTS)
def test_a_planted_go_defect_is_refused(tmp_path, data, page, go_mod, expected):
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, data, page, go_mod)
    assert any(expected in problem for problem in refused.value.problems), (
        refused.value.problems
    )


# ---------------------------------------------------------------------------
# The step in the runner
# ---------------------------------------------------------------------------
def _go_step_runner(execute_module, tmp_path: Path, monkeypatch, fake: FakeGo):
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
    real_run = sdk.run

    def run(**kwargs):
        return real_run(**kwargs, run_command=fake)

    monkeypatch.setattr(execute_module.sdk, "run", run)
    return runner, running


def _go_step(user: str) -> Step:
    return Step.model_validate(
        {"id": "s", "doc": "quick-start", "do": {"sdk": _sdk(user)}, "fail": "x"}
    )


def _line_for(user: str) -> str:
    wanted = sdk.expected(
        user, "exp_key", [("control", 50), ("treatment", 50)], "flag_key", 50
    )
    flag = "true" if wanted["enabled"] else "false"
    return f"variant={wanted['variant']} flag={flag}\n"


def test_the_go_step_holds_the_real_go_mod_and_compares_the_real_checkout(
    tmp_path, execute_module, monkeypatch
):
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    fake = FakeGo(sdk.REPO_ROOT / "sdk" / "go", run_stdout=_line_for(ON))
    runner, running = _go_step_runner(execute_module, tmp_path, monkeypatch, fake)
    observed, snapshot = runner._sdk_step(
        _go_step(ON), running, _ExperimentApi(), "j", 3
    )
    assert observed.startswith(
        f"installed {MODULE} {PSEUDO} (pseudo-version; identical to this checkout's"
        " sdk/go) from https://proxy.golang.org,direct;"
    )
    written = json.loads((tmp_path / snapshot).read_text())
    assert written["problems"] == []
    assert written["install_commands"] == list(INSTALL.commands)
    assert written["toolchain"] == TOOLCHAIN
    assert written["module"]["ref"] == "refs/heads/main"
    assert written["module"]["rule"] == "compare"
    assert '"exp_key"' in written["program"] and f'"{ON}"' in written["program"]
    assert '"http://api"' in written["program"]
    assert KEY not in (tmp_path / snapshot).read_text()
    run_env = fake.calls[-1]["env"]
    for name in ("CONTROLVAR_API_KEY", "EXPERIMENTLY_API_KEY"):
        assert run_env[name] == KEY, name


def test_a_go_step_on_a_page_that_requires_another_version_stops_before_running(
    tmp_path, execute_module, monkeypatch
):
    fake = FakeGo(sdk.REPO_ROOT / "sdk" / "go")
    runner, running = _go_step_runner(execute_module, tmp_path, monkeypatch, fake)
    runner.guide = guides.parse(
        "sdk/sample.md", PAGE.replace(f"Requires Go {DIRECTIVE}+", "Requires Go 0.1+")
    )
    with pytest.raises(execute_module._Failed) as failed:
        runner._sdk_step(_go_step(ON), running, _ExperimentApi(), "j", 3)
    assert failed.value.observed.startswith(
        f'the page says "Requires Go 0.1+", but sdk/go/go.mod says go {DIRECTIVE}'
    )
    assert fake.calls == []


def test_a_go_step_answering_for_the_other_user_fails(
    tmp_path, execute_module, monkeypatch
):
    """An SDK that answers control and off for everyone (the old snippet
    against an API that was not there) fails for the user in treatment."""
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    fake = FakeGo(sdk.REPO_ROOT / "sdk" / "go", run_stdout=_line_for(OFF))
    runner, running = _go_step_runner(execute_module, tmp_path, monkeypatch, fake)
    assert _line_for(ON) != _line_for(OFF)
    with pytest.raises(execute_module._Failed) as failed:
        runner._sdk_step(_go_step(ON), running, _ExperimentApi(), "j", 3)
    assert "the documented" in failed.value.observed
