"""`.github/workflows/regression-guard.yml` -- the bug-label step, run for real (#399).

The step "Require a test with every bug fix" is extracted from the workflow and
run with bash in git repositories created under ``tmp_path`` (never this
checkout), shaped the way ``actions/checkout`` with ``fetch-depth: 0`` leaves a
pull request: full history, main has moved on since the branch point.
The repository root comes from this file's path, so the tests also run in
``scripts/core_build.sh``'s non-git copy.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO = pathlib.Path(__file__).resolve().parents[4]
WORKFLOW = REPO / ".github" / "workflows" / "regression-guard.yml"
BUG_STEP = "Require a test with every bug fix"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _bug_step_script() -> str:
    steps = _workflow()["jobs"]["regression-guard"]["steps"]
    (step,) = [s for s in steps if s.get("name") == BUG_STEP]
    return step["run"]


def _env(tmp_path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
        HOME=str(tmp_path),
    )
    return env


def _git(cwd, env, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo, env, path: str, text: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(repo, env, "add", path)
    _git(repo, env, "commit", "-q", "-m", f"add {path}")
    return _git(repo, env, "rev-parse", "HEAD")


def _behind_main(tmp_path, changed: str, *, fetch_new_main: bool = True):
    """A pull request branched from main, with main moved on since.

    Returns (clone, env, base_sha, head_sha). ``base_sha`` is main's tip, as
    ``github.event.pull_request.base.sha`` is; with ``fetch_new_main`` False
    the clone does not hold it yet, so the step has to fetch it."""
    env = _env(tmp_path)
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, env, "init", "-q", "-b", "main")
    _commit(origin, env, "app/fix.py", "one\n")
    _commit(origin, env, "app/other.py", "two\n")
    clone = tmp_path / "clone"
    _git(tmp_path, env, "clone", "-q", f"file://{origin}", str(clone))
    _git(clone, env, "checkout", "-q", "-b", "pr")
    _commit(clone, env, "app/fix.py", "fixed\n")
    head_sha = _commit(clone, env, changed, "change\n")
    # main moves on after the branch point
    _commit(origin, env, "app/later.py", "three\n")
    base_sha = _commit(origin, env, "tests/test_on_main.py", "main\n")
    if fetch_new_main:
        _git(clone, env, "fetch", "-q", "origin")
    return clone, env, base_sha, head_sha


def _run(clone, env, base_sha, head_sha, script=None, labels='["bug"]'):
    return subprocess.run(
        ["bash", "-c", script if script is not None else _bug_step_script()],
        cwd=clone,
        env={**env, "LABELS": labels, "BASE_SHA": base_sha, "HEAD_SHA": head_sha},
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("fetch_new_main", [True, False])
def test_a_bug_pr_behind_main_that_adds_a_test_passes(tmp_path, fetch_new_main):
    clone, env, base, head = _behind_main(
        tmp_path, "tests/test_fix.py", fetch_new_main=fetch_new_main
    )
    result = _run(clone, env, base, head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/test_fix.py" in result.stdout
    # A test that changed on main after the fork point is not this PR's test.
    assert "tests/test_on_main.py" not in result.stdout


@pytest.mark.parametrize("fetch_new_main", [True, False])
def test_a_bug_pr_behind_main_without_a_test_still_fails(tmp_path, fetch_new_main):
    clone, env, base, head = _behind_main(
        tmp_path, "app/extra.py", fetch_new_main=fetch_new_main
    )
    result = _run(clone, env, base, head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "changes no test file" in result.stdout


def test_the_old_depth_one_fetch_reproduces_no_merge_base(tmp_path):
    """The fixture is the failing shape: put the old fetch back and the step
    dies `no merge base` (exit 128), so the tests above are not green by
    construction."""
    clone, env, base, head = _behind_main(tmp_path, "tests/test_fix.py")
    script = _bug_step_script()
    start = script.index('if ! git cat-file -e "${BASE_SHA}^{commit}"')
    end = script.index("fi\n", start) + len("fi\n")
    old = (
        script[:start]
        + 'git fetch --no-tags --depth=1 origin "$BASE_SHA" 2>/dev/null || true\n'
        + script[end:]
    )
    result = _run(clone, env, base, head, script=old)
    assert result.returncode == 128, result.stdout + result.stderr
    assert "no merge base" in result.stderr


def test_an_unfetchable_base_is_red(tmp_path):
    clone, env, _, head = _behind_main(tmp_path, "tests/test_fix.py")
    for base in ("0" * 40, ""):
        result = _run(clone, env, base, head)
        assert result.returncode != 0, (base, result.stdout)
        assert "Regression coverage found" not in result.stdout


def test_the_step_never_fetches_shallow():
    fetches = [
        line
        for line in _bug_step_script().splitlines()
        if line.lstrip().startswith("git fetch")
    ]
    assert fetches, "the step must still fetch a base it does not hold"
    for line in fetches:
        assert "--depth" not in line and "--shallow" not in line, line
        assert "|| true" not in line and "/dev/null" not in line, line


def test_no_run_of_the_required_check_is_ever_cancelled():
    """`opened` + `labeled` start two runs; a concurrency group cancelled one,
    and the cancelled run blocked the merge although the check was green. A
    group with `cancel-in-progress: false` still cancels a replaced pending run,
    so the workflow has no group at all, at either level."""
    doc = _workflow()
    assert "concurrency" not in doc
    for job in doc["jobs"].values():
        assert "concurrency" not in job


def test_the_template_does_not_ask_an_author_to_add_a_label():
    """A fork author cannot add labels, so the pull request template must ask for
    a reason and leave the waiver label to a maintainer (D216, V77)."""
    template = (REPO / ".github" / "pull_request_template.md").read_text()
    text = " ".join(template.split())  # line breaks and indentation do not count
    assert "add the `skip-regression-guard` label" not in text
    assert "a maintainer decides whether to waive the check" in text


def test_contributing_has_the_what_to_expect_section():
    """The contributor-facing explanation of the approval wait lives under this
    exact heading; other text points at it by name (D216)."""
    lines = (REPO / "CONTRIBUTING.md").read_text().splitlines()
    assert "## What to expect" in lines
    assert "### Why your checks are waiting" in lines
