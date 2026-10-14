"""The rules a change to this repository follows, as the tools enforce them.

Each test runs or reads the thing a contributor relies on, not a description
of it:

* ``make -n`` prints what a target would run: ``make format`` formats and
  fixes with ruff, ``make lint`` runs every command of the ``lint`` job,
  ``make db`` starts PostgreSQL and Redis only, ``make core-build`` runs
  ``scripts/core_build.sh``;
* ruff, with this repository's configuration, sorts imports;
* the ``conventional-title`` job's own script, run with a title;
* the pull-request steps that run the backend suites, and their marker filters;
* pytest with ``--strict-markers`` against the markers ``pyproject.toml``
  declares, and the hook that skips a ``modules`` test in a core build.
"""

from __future__ import annotations

import configparser
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

if not (WORKFLOWS / "lint.yml").is_file():
    pytest.skip("this tree has no .github/workflows", allow_module_level=True)

needs_make = pytest.mark.skipif(shutil.which("make") is None, reason="no make")


def _make_n(target: str) -> str:
    """What ``make <target>`` would run, printed and not run."""
    result = subprocess.run(
        ["make", "-n", target],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


#: Flags that change how a tool reports, not what it checks.
_REPORTING = {"-s", "-color", "--output-format=github"}
_PREFIXES = ("../../venv/bin/", "venv/bin/")
#: The Makefile's optional path: modules/ when the directory exists.
_MODULES_PATH = re.compile(r"\$\(test -d modules && [a-z]{1,10} modules/\)")


def _invocations(script: str) -> set[tuple[str, str, frozenset]]:
    """(directory, command, path arguments) for every command of a script.

    Continuation lines are joined, ``cd X &&`` sets the directory of the
    commands after it on that line, a leading ``NAME=value`` is dropped, the
    Makefile's ``$(test -d modules && ... modules/)`` is the path it prints in a
    full checkout, and what runs only when a check fails or a tool is missing
    (``|| { ...; }``, ``else ... fi``) is not a check.
    """
    script = script.replace("\\\n", " ")
    script = _MODULES_PATH.sub("modules/", script)
    script = re.sub(r"\|\|\s*\{[^}]*\}", "", script)
    script = re.sub(r"\belse\b.*?\bfi\b", "fi", script)
    found = set()
    for line in script.splitlines():
        directory = "."
        for part in re.split(r"&&|\|\||;|\bthen\b|\belse\b", line):
            part = re.sub(r"\b[A-Z_]+='[^']*'\s*", "", part)
            words = [w.strip("\"'") for w in part.split()]
            while words and re.fullmatch(r"[A-Z_]+=.*", words[0]):
                words = words[1:]
            if not words or words[0] in {"if", "fi", "exit"}:
                continue
            if words[0] == "cd":
                directory = words[1]
                continue
            if words[0] == "command":
                continue
            first = words[0]
            for prefix in _PREFIXES:
                first = first.removeprefix(prefix)
            words = [first, *words[1:]]
            if words[:2] == ["python", "-m"] or words[0] == "python":
                words = (
                    words[1:] if words[0] == "python" and words[1] != "-m" else words
                )
            paths = frozenset(w for w in words if w.endswith("/") or "Dockerfile" in w)
            command = " ".join(
                w for w in words if w not in paths and w not in _REPORTING
            )
            found.add((directory, command, paths))
    return found


@needs_make
@pytest.mark.regression
def test_make_lint_runs_every_command_the_lint_job_runs():
    workflow = yaml.safe_load((WORKFLOWS / "lint.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["lint"]["steps"]
    checks = [
        s["run"]
        for s in steps
        if "run" in s and not s.get("name", "").startswith(("Install", "Set up"))
    ]
    wanted = set().union(*(_invocations(run) for run in checks))
    assert len(wanted) >= 10, sorted(wanted)
    made = _invocations(_make_n("lint"))
    missing = sorted(wanted - made, key=str)
    assert not missing, f"the lint job runs these and make lint does not: {missing}"


@needs_make
@pytest.mark.regression
def test_make_format_formats_and_fixes_the_python_in_place():
    lines = [
        _MODULES_PATH.sub("modules/", line)
        for line in _make_n("format").splitlines()
        if line.strip()
    ]
    paths = "backend/ scripts/ tests/acceptance/ modules/"
    assert lines == [
        f"venv/bin/ruff format {paths}",
        f"venv/bin/ruff check {paths} --fix",
    ], lines


@needs_make
@pytest.mark.regression
def test_make_db_starts_postgresql_and_redis_only():
    (line,) = [line for line in _make_n("db").splitlines() if line.strip()]
    assert re.fullmatch(r"docker compose up -d --wait postgres redis", line), line


@needs_make
@pytest.mark.regression
def test_make_core_build_runs_the_core_build_script():
    lines = [line for line in _make_n("core-build").splitlines() if line.strip()]
    assert lines[-1] == "scripts/core_build.sh", lines
    assert all("pip install" in line for line in lines[:-1]), lines


@pytest.mark.regression
def test_ruff_formats_lints_and_sorts_imports_from_the_pyproject_configuration():
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "ruff" in config["tool"], "pyproject.toml has no [tool.ruff]"
    unsorted = "import sys\nimport os\n\nprint(os, sys)\n"
    ruff = [sys.executable, "-m", "ruff"]
    settings = ["--config", str(REPO_ROOT / "pyproject.toml")]
    check = subprocess.run(
        [*ruff, "check", *settings, "--stdin-filename", "probe.py", "-"],
        input=unsorted,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert check.returncode == 1 and "I001" in check.stdout, check.stdout + check.stderr
    fixed = subprocess.run(
        [*ruff, "check", *settings, "--fix", "--stdin-filename", "probe.py", "-"],
        input=unsorted,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert fixed.stdout.startswith("import os\nimport sys\n"), fixed.stdout
    formatted = subprocess.run(
        [*ruff, "format", *settings, "--stdin-filename", "probe.py", "-"],
        input="x = {  'a':1 }\n",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert formatted.stdout == 'x = {"a": 1}\n', formatted.stdout


def _title_check(title: str) -> int:
    workflow = yaml.safe_load(
        (WORKFLOWS / "conventional-title.yml").read_text(encoding="utf-8")
    )
    (step,) = workflow["jobs"]["conventional-title"]["steps"]
    bash = shutil.which("bash")
    result = subprocess.run(
        [bash, "-c", step["run"]],
        env={"PATH": os.environ.get("PATH", ""), "TITLE": title},
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode


TYPES = "feat fix perf deps docs build ci refactor test chore revert style".split()


@pytest.mark.regression
@pytest.mark.parametrize("kind", TYPES)
def test_the_conventional_title_check_accepts_each_type(kind):
    assert _title_check(f"{kind}: a change") == 0
    assert _title_check(f"{kind}(scope): a change") == 0


@pytest.mark.regression
@pytest.mark.parametrize(
    "title",
    [
        "Add a thing",
        "feature: a change",
        "Feat: a change",
        "fix:no space",
        "fix(): empty scope",
        "docs a change",
    ],
)
def test_the_conventional_title_check_refuses_any_other_title(title):
    assert _title_check(title) == 1


# ---------------------------------------------------------------------------
# The backend suites, as the pull-request checks run them
# ---------------------------------------------------------------------------

#: Options that run part of a suite, however they are written: a marker or
#: keyword expression (``-m EXPR``, ``-mEXPR``, ``-m=EXPR``, or bundled as in
#: ``-qm EXPR``), a deselection, an ignored path, or only the last failures.
_NARROWING = re.compile(
    r"^(?:-(?!-)[A-Za-z]{0,10}[mk]|--deselect|--ignore|--lf\b|--last-failed"
    r"|--sw\b|--stepwise)"
)
#: Where PYTEST_ADDOPTS is given a value in a step's script, including one
#: written to $GITHUB_ENV for the steps after it.
_ADDOPTS_SET = re.compile(
    r"PYTEST_ADDOPTS=(?:\"([^\"\n]{0,500})\"|'([^'\n]{0,500})'|(\S{1,500}))"
)


def _workflow(workflow: str) -> dict:
    return yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))


def _pytest_steps(workflow: str, job: str) -> list[list[str]]:
    runs = []
    for step in _workflow(workflow)["jobs"][job]["steps"]:
        for line in step.get("run", "").replace("\\\n", " ").splitlines():
            if not line.strip().startswith("python -m pytest"):
                continue
            # As the shell splits it: a quoted -m expression is one argument.
            runs.append(shlex.split(line, comments=True)[3:])
    return runs


def _suite_runs(workflow: str, job: str, suite: str) -> list[list[str]]:
    return [
        words
        for words in _pytest_steps(workflow, job)
        if any(w.rstrip("/") == f"backend/tests/{suite}" for w in words)
    ]


def _added_options(workflow: str, job: str) -> list[str]:
    """Every value PYTEST_ADDOPTS is given for *job*: the workflow's, the job's
    and each step's ``env``, and any the job's scripts set."""
    data = _workflow(workflow)
    found = []
    scopes = [data, data["jobs"][job], *data["jobs"][job]["steps"]]
    for scope in scopes:
        value = (scope.get("env") or {}).get("PYTEST_ADDOPTS")
        if value is not None:
            found.append(str(value))
    for step in data["jobs"][job]["steps"]:
        for match in _ADDOPTS_SET.finditer(step.get("run", "")):
            found.append(next(g for g in match.groups() if g is not None))
    return found


def _config_options() -> list[str]:
    """``addopts`` from every file pytest reads its configuration from."""
    found = []
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    found.append(pyproject["tool"]["pytest"]["ini_options"].get("addopts", ""))
    for name, section in (
        ("pytest.ini", "pytest"),
        ("tox.ini", "pytest"),
        ("setup.cfg", "tool:pytest"),
    ):
        path = REPO_ROOT / name
        if path.is_file():
            parser = configparser.ConfigParser()
            parser.read(path, encoding="utf-8")
            if parser.has_option(section, "addopts"):
                found.append(parser.get(section, "addopts"))
    return found


def _words(value: str) -> list[str]:
    """*value* split as the shell would, or on white space when it does not
    parse (a value cut from a quoted string in a script)."""
    try:
        return shlex.split(value)
    except ValueError:
        return value.split()


def _narrowing(words: list[str]) -> list[str]:
    """The narrowing options in *words*, an ``-o addopts=...`` override's too."""
    found = [w for w in words if _NARROWING.match(w)]
    for word in words:
        if word.startswith("addopts="):
            found += _narrowing(_words(word[len("addopts=") :]))
    return found


@pytest.mark.regression
@pytest.mark.parametrize(
    "workflow, job, suite",
    [
        ("pr-qa-gate.yml", "unit-tests", "unit"),
        ("integration-tests.yml", "shard", "integration"),
        ("pr-qa-gate.yml", "smoke-tests", "smoke"),
    ],
)
def test_the_pull_request_checks_run_unit_integration_and_smoke_with_no_marker_filter(
    workflow, job, suite
):
    runs = _suite_runs(workflow, job, suite)
    assert runs, f"{workflow} {job} runs no backend/tests/{suite}"
    for words in runs:
        assert not _narrowing(words), (suite, words)
    for value in _added_options(workflow, job) + _config_options():
        assert not _narrowing(_words(value)), (suite, value)


@pytest.mark.parametrize(
    "words, narrowed",
    [
        (["-m", "unit"], True),
        (["-munit"], True),
        (["-m=unit"], True),
        (["-qm", "unit"], True),
        (["-k", "slow"], True),
        (["--deselect", "x"], True),
        (["--ignore=backend/tests/unit/core"], True),
        (["--lf"], True),
        (["-o", "addopts=-m unit"], True),
        (["-p", "no:cov", "-q", "--tb=short", "-x", "--junitxml=u.xml"], False),
        (["--maxfail=1"], False),
    ],
)
def test_the_narrowing_options_are_recognised(words, narrowed):
    assert bool(_narrowing(words)) is narrowed


def _marker_expressions(words: list[str]) -> list[str]:
    found = []
    for i in range(len(words)):
        word = words[i]
        if word == "-m":
            found.append(words[i + 1])
        elif word.startswith("-m="):
            found.append(word[3:])
        elif re.match(r"^-(?!-)[A-Za-z]{0,10}m", word):
            rest = word.split("m", 1)[1]
            found.append(rest if rest else words[i + 1])
    return found


@pytest.mark.regression
@pytest.mark.parametrize("suite", ["e2e", "contract"])
def test_the_e2e_and_contract_suites_run_with_their_marker(suite):
    (words,) = _suite_runs("integration-tests.yml", "shard", suite)
    assert _marker_expressions(words) == [suite], words


@pytest.mark.regression
def test_markers_are_declared_in_pyproject_and_pytest_reads_them_there(tmp_path):
    probe = tmp_path / "test_probe_markers.py"
    probe.write_text(
        textwrap.dedent(
            """
            import pytest

            @pytest.mark.regression
            @pytest.mark.modules
            @pytest.mark.unit
            def test_declared():
                pass
            """
        )
    )
    undeclared = tmp_path / "test_probe_undeclared.py"
    undeclared.write_text(
        "import pytest\n\n@pytest.mark.not_a_declared_marker\ndef test_x():\n    pass\n"
    )

    def collect(path: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-c",
                str(REPO_ROOT / "pyproject.toml"),
                "--rootdir",
                str(tmp_path),
                "-p",
                "no:cacheprovider",
                "--strict-markers",
                "--collect-only",
                "-q",
                str(path),
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
        )

    declared = collect(probe)
    assert declared.returncode == 0, declared.stdout + declared.stderr
    refused = collect(undeclared)
    assert refused.returncode != 0
    assert "not_a_declared_marker" in refused.stdout + refused.stderr


@pytest.mark.regression
def test_a_modules_test_is_skipped_when_the_modules_package_is_absent(monkeypatch):
    """The hook in backend/tests/conftest.py, with the modules absent (no
    package, so no failure): the ``modules`` test is skipped, the other is not."""
    import backend.app.modules_loader as loader
    from backend.tests import conftest

    monkeypatch.setattr(loader, "load_modules", lambda *a, **k: False)
    monkeypatch.setattr(loader, "modules_failure", lambda: None)

    class Item:
        def __init__(self, *marks: str) -> None:
            self.keywords = dict.fromkeys(marks, True)
            self.added: list = []

        def add_marker(self, mark) -> None:
            self.added.append(mark)

    module_test, core_test = Item("modules", "unit"), Item("unit")
    conftest.pytest_collection_modifyitems(None, [module_test, core_test])
    assert [m.name for m in module_test.added] == ["skip"]
    assert module_test.added[0].kwargs["reason"].endswith("(core build)")
    assert core_test.added == []
