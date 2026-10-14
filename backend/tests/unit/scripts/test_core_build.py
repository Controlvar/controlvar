"""`scripts/core_build.sh` — argument handling, and what a stopped step reports.

The script is the proof that a profile stands on its own, so the one thing it
must never do is report success without having run anything.  These tests
drive the real script.  Most stop at argument parsing, well before the copy
step; the ones about a step that does not finish run it past preflight with
stand-in tools.  None needs a database, the build's own tools or the network.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "core_build.sh"

#: Every step name the script's own ALL_STEPS list carries.  `modules_tests`
#: is deliberately absent: it is only valid with --profile full.
_CORE_STEPS = [
    "copy",
    "strip",
    "reuse",
    "secrets",
    "import",
    "bootstrap",
    "unit",
    "integration",
    "smoke",
    "lambda",
    "frontend",
    "vectors",
    "licences",
]


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(_SCRIPT), *args], capture_output=True, text=True, timeout=60
    )


class TestStepValidation:
    @pytest.mark.regression
    @pytest.mark.parametrize("typo", ["unti", "units", "front-end", "licenses", ""])
    def test_unknown_step_is_a_usage_error(self, typo):
        """A step name nothing matches must not skip the build and exit 0.

        `--steps <typo>` used to select no step at all: every step reported
        "skipped" and the script still printed `core-build: OK`, so a typo in
        CI would have proved precisely nothing while looking green.
        """
        result = run("--steps", typo or "nope")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "unknown step" in result.stderr
        # The error names the alternatives, so the typo is fixable from it.
        assert "known steps:" in result.stderr

    @pytest.mark.parametrize("step", _CORE_STEPS)
    def test_known_step_names_are_accepted(self, step):
        """Every name in the script's own step list passes validation.

        Validation happens before preflight, so a valid name gets as far as
        the tool check -- never exit 2.
        """
        result = run("--steps", step, "--help")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_modules_tests_needs_the_full_profile(self):
        """`modules_tests` only exists in the full sequence."""
        assert run("--steps", "modules_tests").returncode == 2
        assert (
            run("--profile", "full", "--steps", "modules_tests", "--help").returncode
            == 0
        )

    def test_unknown_profile_is_still_a_usage_error(self):
        result = run("--profile", "community")
        assert result.returncode == 2
        assert "--profile must be core or full" in result.stderr


def listed(*args: str) -> list[str]:
    """The steps ``--list-steps`` says this invocation would run."""
    result = run(*args, "--list-steps")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == "", result.stderr
    return result.stdout.split()


class TestStepSelection:
    """What `--steps` actually *selects*, not just what it accepts.

    Validation and selection used to read the filter differently: the
    validator stripped whitespace into a throwaway local while `want_step`
    matched the raw string.
    """

    def test_the_full_sequence_runs_without_a_filter(self):
        core = listed()
        assert core[0] == "copy" and core[1] == "strip"
        assert "modules_tests" not in core
        assert {
            "unit",
            "integration",
            "smoke",
            "lambda",
            "frontend",
            "licences",
        } <= set(core)
        assert "modules_tests" in listed("--profile", "full")

    @pytest.mark.regression
    @pytest.mark.parametrize(
        "spelling",
        ["unit,frontend", "unit, frontend", " unit , frontend ", "unit,\tfrontend"],
    )
    def test_whitespace_around_a_name_does_not_drop_it(self, spelling):
        """`--steps "unit, frontend"` selected `unit` and silently skipped ` frontend`.

        It validated (the check stripped whitespace), ran half of what was
        asked for, and exited 0 with `core-build: OK` -- a build that proved
        less than the command said.
        """
        assert listed("--steps", spelling) == ["copy", "strip", "unit", "frontend"]

    @pytest.mark.regression
    def test_the_environment_variable_is_normalised_too(self):
        result = subprocess.run(
            [str(_SCRIPT), "--list-steps"],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "CORE_BUILD_STEPS": "unit, frontend"},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.split() == ["copy", "strip", "unit", "frontend"]

    @pytest.mark.parametrize("filter_", [" , ", ",", "   "])
    def test_a_filter_that_names_nothing_is_a_usage_error(self, filter_):
        """Not "no filter, run everything", and not "nothing ran, OK"."""
        result = run("--steps", filter_)
        assert result.returncode == 2, result.stdout + result.stderr
        assert "names no step" in result.stderr

    def test_a_repeated_name_is_listed_once(self):
        assert listed("--steps", "unit,unit, unit") == ["copy", "strip", "unit"]

    def test_copy_and_strip_are_added_when_a_filter_omits_them(self):
        """A filtered run still needs a tree; asking for copy does not duplicate it."""
        assert listed("--steps", "licences") == ["copy", "strip", "licences"]
        assert listed("--steps", "copy,strip") == ["copy", "strip"]


# ---------------------------------------------------------------------------
# A step that does not finish (#1220)
# ---------------------------------------------------------------------------
#
# These tests run the real script past preflight with stand-in tools: `--dir`
# names a directory that already looks like a copy (it has
# backend/app/main.py), so copy and strip are not run and nothing is copied,
# and PYTHON is a shell script.  Every preflight probe it is given passes, and
# every pytest session passes -- except the suite named by FAKE_SUITE, which
# does what FAKE_ACTION says:
#
#   die    the step's process sends SIGTERM to itself;
#   wait   it creates FAKE_MARKER, then waits until FAKE_RELEASE exists, so the
#          test can signal the script while the step is running.
#
# No database, no git repository and no network: the tests run the same in
# core_build.sh's own copy, which has no .git.

_FAKE_PYTHON = """#!/bin/sh
case " $* " in
    *" -m pytest $FAKE_SUITE "*) ;;
    *) exit 0 ;;
esac
case "$FAKE_ACTION" in
    die)
        kill -TERM $$
        sleep 5
        ;;
    wait)
        : > "$FAKE_MARKER"
        i=0
        while [ ! -e "$FAKE_RELEASE" ] && [ "$i" -lt 600 ]; do
            sleep 0.1
            i=$((i + 1))
        done
        ;;
esac
exit 0
"""


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _default_sigint() -> None:
    # A shell cannot trap a signal it was started with ignored (POSIX), and a
    # pytest started in the background inherits SIGINT ignored.
    signal.signal(signal.SIGINT, signal.SIG_DFL)


class _Build:
    """The script, started against a stand-in copy and stand-in tools."""

    def __init__(self, tmp_path: Path) -> None:
        self.copy = tmp_path / "copy"
        (self.copy / "backend" / "app").mkdir(parents=True)
        (self.copy / "backend" / "app" / "main.py").write_text("")
        tools = tmp_path / "bin"
        tools.mkdir()
        for tool in ("git", "node", "npm", "gitleaks"):
            _executable(tools / tool, "#!/bin/sh\nexit 0\n")
        _executable(tools / "fake-python", _FAKE_PYTHON)
        self.marker = tmp_path / "step-started"
        self.release = tmp_path / "step-may-return"
        self.env = {
            **os.environ,
            "PATH": f"{tools}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHON": str(tools / "fake-python"),
            # So the script does not activate a ./venv beside it.
            "VIRTUAL_ENV": str(tmp_path),
            "FAKE_MARKER": str(self.marker),
            "FAKE_RELEASE": str(self.release),
            "FAKE_SUITE": "backend/tests/unit",
            "FAKE_ACTION": "none",
        }
        for name in ("CORE_BUILD_STEPS", "CORE_BUILD_DIR"):
            self.env.pop(name, None)
        self.proc: subprocess.Popen | None = None

    def start(self, steps: str, action: str) -> subprocess.Popen:
        self.proc = subprocess.Popen(
            [str(_SCRIPT), "--dir", str(self.copy), "--steps", steps],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**self.env, "FAKE_ACTION": action},
            # Its own process group, so a signal to the group reaches the
            # script and its step and nothing else.
            start_new_session=True,
            preexec_fn=_default_sigint,
        )
        return self.proc

    def wait_until_the_step_runs(self) -> None:
        deadline = time.monotonic() + 30
        while not self.marker.exists():
            assert self.proc is not None and self.proc.poll() is None, (
                "the script ended before the step started:\n" + self.finish()[1]
            )
            assert time.monotonic() < deadline, "the step never started"
            time.sleep(0.05)

    def finish(self) -> tuple[int, str]:
        assert self.proc is not None
        self.release.touch()
        _, err = self.proc.communicate(timeout=60)
        return self.proc.returncode, err

    def cleanup(self) -> None:
        self.release.touch()
        if self.proc is not None and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.communicate(timeout=10)


@pytest.fixture
def build(tmp_path):
    started = _Build(tmp_path)
    yield started
    started.cleanup()


def summary(err: str) -> dict[str, str]:
    """Step name -> outcome, from the summary the script prints last."""
    assert "summary (--profile core):" in err, err
    lines = err.split("summary (--profile core):", 1)[1].splitlines()
    outcomes = {}
    for line in lines[1:]:
        if not line.startswith("  "):
            break
        name, _, outcome = line.strip().partition(" ")
        outcomes[name] = outcome.strip()
    return outcomes


class TestAStepThatDoesNotFinish:
    """A gate that says OK after one of its steps was stopped passes for the
    wrong reason (#1220)."""

    def test_the_stand_ins_pass_when_nothing_goes_wrong(self, build):
        """The control: the same harness, no signal, every step ok."""
        build.start("unit,integration", action="none")
        status, err = build.finish()
        assert status == 0, err
        assert "core-build: OK" in err
        outcomes = summary(err)
        assert outcomes["unit"].startswith("ok"), outcomes
        assert outcomes["integration"].startswith("ok"), outcomes
        assert outcomes["smoke"] == "skipped", outcomes

    @pytest.mark.regression
    def test_a_step_whose_process_is_killed_fails_and_names_every_step(self, build):
        """The step's own process dies of SIGTERM partway through.

        The script stops at that step, and the summary still has a line for
        the step it never reached: it used to list only the steps that had
        run, so `integration` vanished from it.
        """
        build.start("unit,integration", action="die")
        status, err = build.finish()
        assert status != 0, err
        assert "core-build: OK" not in err
        assert "core-build: FAILED at step unit" in err
        outcomes = summary(err)
        assert outcomes["unit"].startswith("FAILED"), outcomes
        assert outcomes["integration"] == "not run", outcomes
        assert "asked for but not run: integration" in err

    @pytest.mark.regression
    @pytest.mark.parametrize("target", ["script", "group"])
    @pytest.mark.parametrize(
        "signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP], ids=lambda s: s.name
    )
    def test_a_signal_to_the_script_mid_step_ends_it_non_zero(
        self, build, signum, target
    ):
        """Stopping the script while a step runs is a failure at that step.

        SIGTERM used to run the summary with $? = 0 -- `core-build: OK`,
        the running step missing -- and delete the copy under the step;
        SIGINT to the script alone was ignored.  `group` is what Ctrl-C and
        most outside time limits do; `script` is a `kill <pid>`.
        """
        proc = build.start("unit,integration", action="wait")
        build.wait_until_the_step_runs()
        if target == "group":
            os.killpg(proc.pid, signum)
        else:
            os.kill(proc.pid, signum)
        status, err = build.finish()
        assert status == 128 + signum, err
        assert "core-build: OK" not in err
        assert "core-build: FAILED at step unit" in err
        assert f"stopped by {signum.name}" in err
        outcomes = summary(err)
        assert outcomes["unit"].startswith("FAILED"), outcomes
        assert outcomes["integration"] == "not run", outcomes

    @pytest.mark.regression
    def test_a_signal_with_no_trap_is_still_not_ok(self, build):
        """SIGUSR1 ends bash after the summary runs with $? = 0.

        The script cannot name the signal, but a run that ended inside a
        step must not report OK.
        """
        proc = build.start("unit", action="wait")
        build.wait_until_the_step_runs()
        os.kill(proc.pid, signal.SIGUSR1)
        status, err = build.finish()
        assert status != 0, err
        assert "core-build: OK" not in err
        assert "core-build: FAILED at step unit" in err
        assert summary(err)["unit"].startswith("FAILED"), err
