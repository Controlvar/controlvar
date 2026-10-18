"""chart-kind's N-1 -> N leg runs on every event and pulls N-1 anonymously.

While the GHCR packages were private, ``scripts/chart_kind.sh previous`` printed
"not run: images private until D19" on a pull request from a fork and the
workflow skipped the leg. The packages are public now (#227), so the leg runs
everywhere, and it fails closed: a pull that cannot be made without
credentials fails the job with a message naming the likely cause, on every
event -- not only on forks, which is where a package that went private again
would otherwise first show up.

The cluster job is the real gate; this pins, without a cluster or a registry:

* the workflow's upgrade step has no ``if:`` and the job logs in to no
  registry;
* ``previous`` resolves N-1 for a pull request from a fork exactly as for any
  other event (run as shell, with ``git`` stubbed);
* ``pull_anonymously`` exits non-zero with the message when ``docker pull``
  fails, and sends no stored credential when it succeeds (run as shell, with
  ``docker`` stubbed).
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import textwrap

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.regression]

ROOT = pathlib.Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "chart_kind.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "chart-kind.yml"

MESSAGE = (
    "The previous release image could not be pulled anonymously: "
    "is the package still public?"
)


def _function(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"(?m)^{name}\(\) {{ .*}}\n", text) or re.search(
        rf"(?ms)^{name}\(\) {{\n.*?^}}\n", text
    )
    assert match, f"{name}() not found in {SCRIPT}"
    return match.group(0)


def _assignment(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"(?m)^{name}=.*$", text)
    assert match, f"{name}= not found in {SCRIPT}"
    return match.group(0)


def _stub(bindir: pathlib.Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)


def _run(
    script: str, cwd: pathlib.Path, env: dict[str, str]
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _harness(*functions: str, assignments: tuple[str, ...] = ()) -> str:
    """The script's own definitions, extracted, under its own shell options."""
    parts = ["set -euo pipefail", "PROFILE=core", 'WORK="$TMPDIR/work"']
    parts += [_assignment(a) for a in assignments]
    parts += [_function(f) for f in ("say", "fail", *functions)]
    return "\n".join(parts) + "\n"


@pytest.fixture
def sandbox(tmp_path: pathlib.Path) -> tuple[pathlib.Path, dict[str, str]]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "LOG": str(tmp_path / "docker.log"),
    }
    return tmp_path, env


def _upgrade_previous_step() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = data["jobs"]["chart-kind"]["steps"]
    matches = [s for s in steps if "upgrade-previous" in str(s.get("run", ""))]
    assert len(matches) == 1, "expected exactly one step running upgrade-previous"
    return matches[0]


def test_the_upgrade_previous_step_is_never_skipped():
    step = _upgrade_previous_step()
    assert "if" not in step, (
        f"the N-1 -> N step is conditional ({step['if']!r}); it must run on "
        "every event, pull requests from forks included"
    )


def test_the_job_logs_in_to_no_registry():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = data["jobs"]["chart-kind"]
    uses = [str(s.get("uses", "")) for s in job["steps"]]
    assert not [u for u in uses if "login-action" in u], (
        "chart-kind logs in to a registry: the N-1 pull must work with no "
        "credentials, as it does on a pull request from a fork"
    )
    assert "packages" not in job.get("permissions", {})


def test_previous_resolves_n_minus_1_on_a_pull_request_from_a_fork(sandbox):
    tmp, env = sandbox
    (tmp / "VERSION").write_text("0.16.2\n", encoding="utf-8")
    chart = tmp / "trees" / "v0.16.2" / "charts" / "experimently"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text('appVersion: "0.16.2"\n', encoding="utf-8")
    (chart / "values.yaml").write_text(
        "image:\n  api:\n    repository: ghcr.io/example/api\n"
        "  web:\n    repository: ghcr.io/example/web\n",
        encoding="utf-8",
    )
    _stub(
        tmp / "bin",
        "git",
        """\
        case "$1" in
            ls-remote)
                printf 'aaa\\trefs/tags/v0.16.1\\n'
                printf 'bbb\\trefs/tags/v0.16.2\\n'
                printf 'ccc\\trefs/tags/v0.17.0\\n' ;;
            fetch) ;;
            archive) tag=$2; shift 2; tar -c -C "$TREES/$tag" "$@" ;;
            *) exit 64 ;;
        esac
        """,
    )
    env["TREES"] = str(tmp / "trees")
    # Every manifest read answers: the images are published.
    _stub(tmp / "bin", "docker", "exit 0\n")
    out = tmp / "github_output"
    summary = tmp / "step_summary"
    env.update(
        GITHUB_OUTPUT=str(out),
        GITHUB_STEP_SUMMARY=str(summary),
        GITHUB_REPOSITORY="Controlvar/controlvar",
        # What a fork's pull request looked like to the old skip.
        HEAD_REPO="someone/controlvar",
        GITHUB_EVENT_NAME="pull_request",
    )
    result = _run(
        _harness(
            "values_repo",
            "tag_images",
            "published",
            "do_previous",
            assignments=("API_REPO", "WEB_REPO", "PULL_FAILED"),
        )
        + "do_previous\n",
        tmp,
        env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "not run" not in combined.lower()
    written = out.read_text(encoding="utf-8").splitlines()
    assert "version=0.16.2" in written
    assert "run=false" not in written
    assert not summary.exists() or "not run" not in summary.read_text().lower()


def test_a_failed_anonymous_pull_fails_with_the_message(sandbox):
    tmp, env = sandbox
    _stub(tmp / "bin", "docker", 'printf "denied\\n" >&2\nexit 1\n')
    result = _run(
        _harness("pull_anonymously", assignments=("PULL_FAILED",))
        + "pull_anonymously ghcr.io/controlvar/controlvar:core-0.16.2\n"
        + "printf 'still-running\\n'\n",
        tmp,
        env,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert MESSAGE in result.stdout + result.stderr
    assert "still-running" not in result.stdout


def test_a_successful_pull_sends_no_stored_credential(sandbox):
    tmp, env = sandbox
    # A logged-in client: if the pull read this, it would not be anonymous.
    home_cfg = tmp / ".docker"
    home_cfg.mkdir()
    (home_cfg / "config.json").write_text(
        '{"auths": {"ghcr.io": {"auth": "c3R1YjpzdHVi"}}}\n',
        encoding="utf-8",
    )
    env["DOCKER_CONFIG"] = str(home_cfg)
    _stub(
        tmp / "bin",
        "docker",
        """\
        {
          printf 'args=%s\\n' "$*"
          printf 'config=%s\\n' "${DOCKER_CONFIG:-}"
          cat "${DOCKER_CONFIG:-/nonexistent}/config.json" 2>/dev/null || printf 'no-config\\n'
        } >>"$LOG"
        """,
    )
    result = _run(
        _harness("pull_anonymously", assignments=("PULL_FAILED",))
        + "pull_anonymously ghcr.io/controlvar/controlvar:core-0.16.2\n",
        tmp,
        env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    log = (tmp / "docker.log").read_text(encoding="utf-8")
    assert "args=pull ghcr.io/controlvar/controlvar:core-0.16.2" in log
    assert f"config={home_cfg}\n" not in log
    assert "auths" not in log, log
