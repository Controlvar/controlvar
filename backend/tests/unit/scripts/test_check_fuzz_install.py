"""Every pull request installs the fuzzer's pins and runs ``pip check`` (#1128).

The fuzzer's venv used to be built only by ``fuzz.yml`` (through
``_platform.yml``'s fuzz arm), on a schedule or by dispatch. So a pull request
that moved ``graphql-core`` to 3.3.0, which ``hypothesis-graphql`` refuses,
passed every check and merged, and every scheduled run then failed at the
install step. The install is now ``scripts/check_fuzz_install.sh``, called by
the fuzz arm, by ``make fuzz`` and by the ``Base Requirements Only`` job of
``pr-qa-gate.yml`` -- a required check on every pull request.

Pinned here:

* the pull request job calls the script, unconditionally, on the Python the
  fuzz arm uses, and that job is a required check that reports on a pull
  request;
* no workflow or script holds a second copy of the install;
* the script itself, run with a stand-in for the interpreter (so no network):
  the exact pip commands, run from the repository root whatever the caller's
  directory, a non-zero exit when ``pip check`` fails, a refusal of a venv
  directory that exists, and a throwaway venv removed on success and failure.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from backend.tests.unit.infrastructure import _workflow_graph as _wg
from backend.tests.unit.infrastructure.test_docs_only_gate import _load

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
GATE = WORKFLOWS / "pr-qa-gate.yml"
PLATFORM = WORKFLOWS / "_platform.yml"
SCRIPTS = REPO_ROOT / "scripts"
SCRIPT = SCRIPTS / "check_fuzz_install.sh"

JOB = "base-requirements-only"
JOB_NAME = "Base Requirements Only"
STEP_NAME = "The fuzzer's pins install and pass pip check"

#: The two commands, as the fuzz run has always run them.
INSTALL_ARGS = (
    "install --quiet --disable-pip-version-check --no-deps"
    " -r tests/fuzz/requirements.txt -r tests/fuzz/constraints.txt"
)
CHECK_ARGS = "check --disable-pip-version-check"

#: A pip install of the fuzzer's requirements file, in whatever quoting.
INSTALLS_THE_PINS = re.compile(r"pip\"? install[^\n]*tests/fuzz/requirements\.txt")

needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is not installed"
)


def _workflows_present() -> bool:
    return GATE.is_file() and PLATFORM.is_file()


needs_workflows = pytest.mark.skipif(
    not _workflows_present(), reason="this tree has no .github/workflows"
)


# ---------------------------------------------------------------------------
# The pull request runs it
# ---------------------------------------------------------------------------


def _job_steps() -> List[Dict[str, object]]:
    return _load(GATE)["jobs"][JOB]["steps"]


def _python_version(steps: List[Dict[str, object]]) -> Optional[str]:
    versions = [
        str(s["with"]["python-version"])  # type: ignore[index]
        for s in steps
        if str(s.get("uses", "")).startswith("actions/setup-python@")
    ]
    assert len(versions) == 1, versions
    return versions[0]


@needs_workflows
def test_a_pull_request_runs_the_fuzzer_install():
    steps = [s for s in _job_steps() if s.get("name") == STEP_NAME]
    assert len(steps) == 1, "the pull request job has no step that installs the fuzzer"
    step = steps[0]
    assert str(step["run"]).strip() == "bash scripts/check_fuzz_install.sh"
    # A condition would let the step be skipped under a green job.
    assert "if" not in step
    assert "continue-on-error" not in step


@needs_workflows
def test_the_job_that_runs_it_is_a_required_check_on_pull_requests():
    produced = list(_wg.check_names(GATE, "pull_request"))
    assert (JOB, JOB_NAME) in produced
    assert JOB_NAME in _wg.required_checks()


@needs_workflows
def test_the_pull_request_job_uses_the_fuzz_arms_python():
    arm_steps = _load(PLATFORM)["jobs"]
    (arm,) = arm_steps.values()
    assert _python_version(_job_steps()) == _python_version(arm["steps"])


@needs_workflows
def test_the_fuzz_arm_calls_the_script_for_the_venv_it_then_uses():
    step = next(
        s
        for s in next(iter(_load(PLATFORM)["jobs"].values()))["steps"]
        if s.get("name") == "Install the fuzzer"
    )
    assert str(step["run"]).strip() == (
        'bash scripts/check_fuzz_install.sh "$RUNNER_TEMP/fuzz-venv"'
    )
    assert "$RUNNER_TEMP/fuzz-venv/bin/schemathesis" in PLATFORM.read_text()


@needs_workflows
def test_no_workflow_or_script_holds_a_second_copy_of_the_install():
    holders = []
    for path in [*sorted(WORKFLOWS.glob("*.y*ml")), *sorted(SCRIPTS.glob("*.sh"))]:
        text = path.read_text(encoding="utf-8")
        if INSTALLS_THE_PINS.search(text):
            holders.append(path.name)
    assert holders == [SCRIPT.name]


# ---------------------------------------------------------------------------
# The script, with a stand-in interpreter
# ---------------------------------------------------------------------------

#: What PYTHON points at: `python -m venv DIR` makes DIR/bin/{pip,python}, and
#: the pip records its arguments and the directory it ran in.
FAKE_PYTHON = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    if [ "$1" != -m ] || [ "$2" != venv ] || [ "$#" -ne 3 ]; then
      echo "fake python: unexpected arguments: $*" >&2
      exit 97
    fi
    mkdir -p "$3/bin"
    printf '%s\\n' '#!/usr/bin/env bash' \\
      'echo "pip $*" >> "$FAKE_LOG"' \\
      'echo "cwd $PWD" >> "$FAKE_LOG"' \\
      'if [ "$1" = check ]; then exit "${FAKE_CHECK_EXIT:-0}"; fi' > "$3/bin/pip"
    printf '%s\\n' '#!/usr/bin/env bash' 'echo "Python 3.11.0"' > "$3/bin/python"
    chmod +x "$3/bin/pip" "$3/bin/python"
    """
)


@pytest.fixture
def harness(tmp_path):
    fake = tmp_path / "fake-python"
    fake.write_text(FAKE_PYTHON)
    fake.chmod(0o755)
    temp = tmp_path / "tmp"
    temp.mkdir()
    log = tmp_path / "pip.log"
    work = tmp_path / "cwd"
    work.mkdir()

    def run(*args: str, check_exit: int = 0, cwd: Optional[Path] = None):
        env = dict(os.environ)
        env.pop("RUNNER_TEMP", None)
        env.update(
            PYTHON=str(fake),
            TMPDIR=str(temp),
            FAKE_LOG=str(log),
            FAKE_CHECK_EXIT=str(check_exit),
        )
        proc = subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=cwd or work,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        lines = log.read_text().splitlines() if log.exists() else []
        return proc, lines

    run.temp = temp
    run.work = work
    return run


@needs_bash
def test_it_runs_the_two_commands_from_the_repository_root(harness, tmp_path):
    venv = tmp_path / "venv"
    proc, lines = harness(str(venv))
    assert proc.returncode == 0, proc.stderr
    assert lines == [
        f"pip {INSTALL_ARGS}",
        f"cwd {REPO_ROOT}",
        f"pip {CHECK_ARGS}",
        f"cwd {REPO_ROOT}",
    ]
    assert "pass pip check (Python 3.11.0)" in proc.stdout
    # A venv the caller named is the caller's: the fuzz run goes on to use it.
    assert (venv / "bin" / "pip").is_file()


@needs_bash
def test_it_fails_when_pip_check_fails(harness, tmp_path):
    proc, lines = harness(str(tmp_path / "venv"), check_exit=1)
    assert proc.returncode == 1
    assert lines[-2] == f"pip {CHECK_ARGS}"
    assert "pass pip check" not in proc.stdout


@needs_bash
def test_a_relative_venv_directory_is_the_callers_not_the_repositorys(harness):
    name = "relative-fuzz-venv"
    assert not (REPO_ROOT / name).exists()
    proc, _ = harness(name)
    assert proc.returncode == 0, proc.stderr
    assert (harness.work / name / "bin" / "pip").is_file()
    assert not (REPO_ROOT / name).exists()


@needs_bash
def test_it_refuses_a_venv_directory_that_exists(harness, tmp_path):
    venv = tmp_path / "venv"
    venv.mkdir()
    proc, lines = harness(str(venv))
    assert proc.returncode == 2
    assert "already exists" in proc.stderr
    assert lines == []


@needs_bash
def test_it_refuses_two_arguments(harness, tmp_path):
    proc, lines = harness(str(tmp_path / "a"), str(tmp_path / "b"))
    assert proc.returncode == 2
    assert "usage:" in proc.stderr
    assert lines == []


@needs_bash
@pytest.mark.parametrize("check_exit", [0, 1])
def test_without_an_argument_the_venv_is_thrown_away(harness, check_exit):
    proc, lines = harness(check_exit=check_exit)
    assert proc.returncode == check_exit
    assert lines, "the commands did not run"
    assert list(harness.temp.iterdir()) == []


@needs_bash
def test_it_stops_at_the_first_failing_command(tmp_path):
    # An interpreter that cannot make a venv ends the script before any pip.
    broken = tmp_path / "broken-python"
    broken.write_text("#!/usr/bin/env bash\nexit 3\n")
    broken.chmod(0o755)
    env = dict(os.environ, PYTHON=str(broken), TMPDIR=str(tmp_path))
    proc = subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path / "venv")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 3
    assert "pass pip check" not in proc.stdout
