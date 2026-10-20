"""The manifest's ``tool``: the commit the capture ran from, and whether its tree was clean.

A take records the capture checkout's HEAD, and ``clean`` only when that
checkout had no uncommitted change (modified, staged or untracked; ignored
files such as ``node_modules`` are not changes) when the run started and again
when the capture was written, at the same commit. A dirty tree is recorded, not
refused: a development take is allowed one. The intent, enforced outside this
repository by a final's approval step, is that ``clean`` is checked at the
reviewed commit, so a dirty tree recorded as clean is the defect these tests
plant.

Each test makes its own git repository in a temporary directory, so they run
as well in core_build.sh's copy, which has no ``.git``.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path

import pytest
from showcase import contract
from showcase.capture import build, config, guard, run, storyboard
from showcase.capture.procs import Children
from showcase.capture.stack import Ports

from backend.tests.unit.showcase.capture_dirs import (
    default_cues,
    default_manifest,
    jpeg_bytes,
)

pytestmark = [pytest.mark.unit]

ACCEPTANCE = Path(__file__).resolve().parents[4] / "tests" / "acceptance"
CAPTURE = ACCEPTANCE / "showcase" / "capture"


def git(repo: Path, *args: str) -> str:
    """git in a throwaway repository, whatever the machine's own configuration."""
    done = subprocess.run(
        [
            "git",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    """A checkout with one commit: a tracked file, and ``node_modules`` ignored."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main", ".")
    (repo / "storyboard.yaml").write_text("slug: probe\n", encoding="utf-8")
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "one")
    return repo


def test_a_clean_checkout_is_recorded_with_its_head(checkout):
    (checkout / "node_modules").mkdir()
    (checkout / "node_modules" / "next.js").write_text("ignored\n", encoding="utf-8")
    found = build.tool_checkout(checkout)
    assert found == {
        "commit": git(checkout, "rev-parse", "HEAD"),
        "clean": True,
        "changes": 0,
        "first_changes": [],
    }
    assert build.tool_line(found) == f"tool checkout {found['commit'][:12]}, clean"


def _write(repo: Path, name: str, text: str = "x = 1\n") -> None:
    (repo / name).write_text(text, encoding="utf-8")


#: Every way a checkout differs from its commit, as git's status sees it.
DIRTY = {
    "a tracked file changed": lambda repo: _write(
        repo, "storyboard.yaml", "slug: other\n"
    ),
    "a tracked file deleted": lambda repo: (repo / "storyboard.yaml").unlink(),
    "a new file staged": lambda repo: (
        _write(repo, "new.py"),
        git(repo, "add", "new.py"),
    ),
    "an untracked file": lambda repo: _write(repo, "new.py"),
    # git's own setting must not hide one: the status asks for untracked files.
    "an untracked file, git set to hide them": lambda repo: (
        git(repo, "config", "status.showUntrackedFiles", "no"),
        _write(repo, "new.py"),
    ),
}


@pytest.mark.regression
@pytest.mark.parametrize("how", sorted(DIRTY))
def test_a_dirty_checkout_is_never_recorded_as_clean(checkout, how):
    """The plant: a dirty tree recorded as clean would let a final pass the
    approval step's check at a commit it was not recorded from."""
    head = git(checkout, "rev-parse", "HEAD")
    DIRTY[how](checkout)
    found = build.tool_checkout(checkout)
    assert found["commit"] == head
    assert found["clean"] is False
    assert found["changes"] == 1
    assert len(found["first_changes"]) == 1
    record = build.tool_record(found, build.tool_checkout(checkout))
    assert record["commit"] == head
    assert record["clean"] is False
    assert build.tool_line(found) == (
        f"tool checkout {head[:12]}, 1 uncommitted change(s) (recorded as not clean)"
    )


def test_a_take_is_clean_only_when_clean_at_its_start_and_end_at_one_commit(
    checkout,
):
    start = build.tool_checkout(checkout)
    assert build.tool_record(start, build.tool_checkout(checkout)) == {
        "commit": start["commit"],
        "clean": True,
        "start": start,
        "end": start,
    }
    # Changed while the take ran.
    _write(checkout, "storyboard.yaml", "slug: other\n")
    assert build.tool_record(start, build.tool_checkout(checkout))["clean"] is False
    # Then committed: clean again, but not the commit the take started from.
    git(checkout, "commit", "-q", "--no-verify", "-am", "two")
    end = build.tool_checkout(checkout)
    assert end["clean"] is True
    assert end["commit"] != start["commit"]
    assert build.tool_record(start, end) == {
        "commit": start["commit"],
        "clean": False,
        "start": start,
        "end": end,
    }
    # No reading at the start: nothing to compare, so not clean.
    assert build.tool_record(None, end) == {
        "commit": None,
        "clean": False,
        "start": None,
        "end": end,
    }


def test_a_tree_that_is_not_a_checkout_of_its_own_has_no_commit(checkout, tmp_path):
    """core_build.sh's copy has no ``.git``; a copy inside another checkout must
    not report that checkout's HEAD as its own."""
    plain = tmp_path / "copy"
    plain.mkdir()
    inside = checkout / "copy"
    inside.mkdir()
    _write(inside, "kept.txt")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "--no-verify", "-m", "a copy inside")
    for tree in (plain, inside):
        found = build.tool_checkout(tree)
        assert found == {
            "commit": None,
            "clean": False,
            "changes": None,
            "first_changes": [],
        }
        assert build.tool_line(found) == (
            "tool checkout: no commit of its own (recorded as not clean)"
        )


def test_when_git_cannot_answer_nothing_is_clean(checkout):
    def no_git(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    assert build.tool_checkout(checkout, run=no_git)["clean"] is False

    def status_fails(argv, **kwargs):
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 128, "", "fatal: bad index file")
        return subprocess.run(argv, **kwargs)

    found = build.tool_checkout(checkout, run=status_fails)
    assert found["commit"] == git(checkout, "rev-parse", "HEAD")
    assert found["clean"] is False
    assert found["changes"] is None
    assert build.tool_line(found) == (
        f"tool checkout {found['commit'][:12]}, status unknown (recorded as not clean)"
    )


def test_every_change_is_counted_and_the_first_ones_listed(checkout):
    extra = build.TOOL_CHANGES_SHOWN + 5
    for number in range(extra):
        _write(checkout, f"new-{number:02d}.txt")
    found = build.tool_checkout(checkout)
    assert found["changes"] == extra
    assert len(found["first_changes"]) == build.TOOL_CHANGES_SHOWN
    assert found["first_changes"][0] == "?? new-00.txt"


def test_the_reading_writes_nothing_and_passes_git_only_the_tools_environment(
    checkout, monkeypatch
):
    """``git status`` refreshes the index when a file's times change; with
    ``--no-optional-locks`` it leaves the index as it was."""
    monkeypatch.setenv("GIT_DIR", str(checkout / "elsewhere"))
    tracked = checkout / "storyboard.yaml"
    later = tracked.stat().st_mtime + 10
    os.utime(tracked, (later, later))
    index = checkout / ".git" / "index"
    before = index.read_bytes()
    seen = []

    def recording(argv, **kwargs):
        seen.append((argv, kwargs["env"]))
        return subprocess.run(argv, **kwargs)

    assert build.tool_checkout(checkout, run=recording)["clean"] is True
    assert index.read_bytes() == before
    assert [argv[1] for argv, _ in seen] == ["--no-optional-locks"] * 3
    assert [argv[4] for argv, _ in seen] == ["rev-parse", "rev-parse", "status"]
    assert all(env == guard.tool_env() for _, env in seen)
    assert all("GIT_DIR" not in env for _, env in seen)


# --- The run and the manifest ---------------------------------------------------


def test_the_run_reads_the_checkout_before_it_builds_anything():
    """``run.run`` needs a stack, so its order is read from the source: the
    reading comes before the preflight (which loads the storyboards) and before
    the ref is resolved, archived or any app built."""
    tree = ast.parse((CAPTURE / "run.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    lines = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
            elif isinstance(node.func, ast.Name):
                name = node.func.id
            else:
                continue
            lines[name] = min(lines.get(name, node.lineno), node.lineno)
    assert "tool_checkout" in lines, "run never reads the tool's checkout"
    for later in ("preflight", "resolve_ref", "archive", "build_app"):
        assert lines["tool_checkout"] < lines[later], later
    versions = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Dict)
        and "tool" in [getattr(key, "value", None) for key in node.keys]
    ]
    assert versions, "run does not hand the reading on to the captures"


def _write_capture(tmp_path: Path, repo: Path, versions):
    capture = tmp_path / "work" / "01-getting-started" / "capture"
    (capture / "frames").mkdir(parents=True)
    entries = []
    for number, t_ms in enumerate([0, 10_000, 20_000, 30_000, 40_000], start=1):
        name = f"{number:06d}.jpg"
        (capture / "frames" / name).write_bytes(
            jpeg_bytes(contract.PAGE_W, contract.PAGE_H)
        )
        entries.append({"file": name, "t_ms": t_ms})
    work = guard.WorkRoot(tmp_path / "work")
    run.write_capture(
        capture,
        board=storyboard.load(CAPTURE.parent / config.VIDEOS["01-getting-started"]),
        source=build.Source(ref="v0.26.4", commit="0" * 40, version="0.26.4"),
        options=run.Options(
            videos=["01-getting-started"],
            ref="v0.26.4",
            repo=repo,
            work_dir=work.path,
            profile="core",
            ports=Ports(28400, 28401, 28402),
            out=tmp_path / "videos",
        ),
        versions=versions,
        facts={
            "entries": entries,
            "duration_ms": 50_000,
            "cues": default_cues(),
            "needles": ["VALUE-ONE-TWO-THREE"],
            "zoom": 1.25,
        },
        gates=default_manifest()["gates"],
        children=Children(work, {}),
        wall_seconds=1.0,
    )
    return json.loads((capture / "manifest.json").read_text(encoding="utf-8"))


def test_the_manifest_records_the_tool_checkout_clean(checkout, tmp_path):
    start = build.tool_checkout(checkout)
    versions = {"next_used": "16.3.5", "next_lockfile": "16.3.5", "tool": start}
    manifest = _write_capture(tmp_path, checkout, versions)
    assert manifest["tool"] == {
        "commit": git(checkout, "rev-parse", "HEAD"),
        "clean": True,
        "start": start,
        "end": start,
    }


@pytest.mark.regression
def test_the_manifest_records_a_tree_changed_during_the_take_as_not_clean(
    checkout, tmp_path
):
    """Recorded, not refused: the capture is written, with ``clean`` false."""
    start = build.tool_checkout(checkout)
    _write(checkout, "storyboard.yaml", "slug: changed while recording\n")
    versions = {"next_used": "16.3.5", "next_lockfile": "16.3.5", "tool": start}
    manifest = _write_capture(tmp_path, checkout, versions)
    assert manifest["tool"]["commit"] == start["commit"]
    assert manifest["tool"]["clean"] is False
    assert manifest["tool"]["end"]["first_changes"] == [" M storyboard.yaml"]


def test_a_capture_without_a_reading_at_the_start_is_not_clean(checkout, tmp_path):
    manifest = _write_capture(
        tmp_path, checkout, {"next_used": "16.3.5", "next_lockfile": "16.3.5"}
    )
    assert manifest["tool"]["commit"] is None
    assert manifest["tool"]["clean"] is False
