"""The former name appears only where it is kept on purpose (scripts/check_legacy_name.py).

The project was renamed. A mechanical rename would leave every other test
green while losing data (a compose volume, a Helm object) or splitting a
rolling upgrade (a lock key, a token issuer), so the edge between "renamed"
and "kept" is held from both sides: any occurrence outside the keep-list fails
(RESIDUAL), and any keep-list entry that matches nothing fails (STALE-ALLOW),
which is how a kept identifier renamed by accident shows up. Each entry pins
its count, and this file pins how many entries there are.

The check walks files, not git, so it runs in scripts/core_build.sh's copy as
well (this directory is its `smoke` step); in a checkout with `.git` it walks
what `git ls-files` lists.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_legacy_name.py"
KEEP = REPO_ROOT / "scripts" / "legacy_name_keep.toml"

#: Assembled, so this file never contains the name it plants.
OLD = "experi" + "mently"

#: The reviewed size of the keep-list. A new entry is a reviewed change, so it
#: moves these numbers in the same pull request.
ENTRIES = 510
KEPT = 1467
EXCLUDES = [
    "CHANGELOG.md",
    "sdk/",
    "docs/sdk/",
    "docs/_claims/docs/sdk/",
    "docs/_claims/sdk/",
    "tests/sdk-contract/",
    "backend/tests/unit/infrastructure/fixtures/branch_protection/live_main_2026-10-01.json",
    "backend/tests/unit/infrastructure/fixtures/rollback_runs/",
]


def _checker():
    spec = importlib.util.spec_from_file_location(
        "check_legacy_name_under_test", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _checker()


def test_the_tree_has_the_former_name_only_where_it_is_kept():
    problems, stats = checker.check(REPO_ROOT, KEEP)
    assert problems == [], "\n".join(problems)
    assert stats["kept"] == stats["occurrences"]


def test_the_keep_list_is_the_reviewed_one():
    keeps, excludes = checker.load_keep(KEEP)
    assert [e.path for e in excludes] == EXCLUDES
    assert len(keeps) == ENTRIES
    assert sum(k.count for k in keeps) == KEPT
    # A module entry is skipped only when modules/ is gone as a whole.
    if not (REPO_ROOT / "modules").is_dir():
        pytest.skip("core copy: the module entries' counts are not checked here")


# ---------------------------------------------------------------------------
# The check, on trees made here: each planted defect must be reported.
# ---------------------------------------------------------------------------


def _tree(tmp_path: Path, files: dict, keep: str = "") -> Path:
    root = tmp_path / "tree"
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "legacy_name_keep.toml").write_text(keep, encoding="utf-8")
    return root


def _run(root: Path):
    return checker.check(root, root / "scripts" / "legacy_name_keep.toml")[0]


def _keep(path: str, literal: str, count: int) -> str:
    return (
        f'[[keep]]\npath = "{path}"\nliteral = "{literal}"\n'
        f'count = {count}\nreason = "test"\n\n'
    )


@pytest.mark.parametrize(
    "files, expected",
    [
        ({"a.yml": f"x: {OLD.upper()}_SETTING\n"}, "RESIDUAL a.yml:1"),
        ({"b.md": f"Get{OLD.capitalize()} docs\n"}, "RESIDUAL b.md:1"),
        ({f"charts_{OLD}.txt": "nothing inside\n"}, f"RESIDUAL charts_{OLD}.txt:0"),
        ({"c.bin": b"\x00\x00get" + OLD.encode() + b"\x00\xff"}, "RESIDUAL c.bin:1"),
        ({"d.txt": f"%40get{OLD}%2Fjs-sdk\n"}, "RESIDUAL d.txt:1"),
        ({"e.py": f'NEEDLE = "{OLD}"\n'}, "RESIDUAL e.py:1"),
    ],
    ids=[
        "upper-case",
        "mixed-case",
        "path-name",
        "binary",
        "url-encoded",
        "self-literal",
    ],
)
def test_a_planted_occurrence_is_residual(tmp_path, files, expected):
    problems = _run(_tree(tmp_path, files))
    assert any(p.startswith(expected) for p in problems), problems


def test_a_kept_identifier_renamed_by_accident_is_stale(tmp_path):
    """The compose project name renamed by a sweep: its entry matches nothing."""
    root = _tree(
        tmp_path,
        {"compose.yml": "name: controlvar-selfhost\n"},
        _keep("compose.yml", f"{OLD}-selfhost", 1),
    )
    assert _run(root) == [f"STALE-ALLOW compose.yml '{OLD}-selfhost' matched nothing"]


def test_a_second_occurrence_in_a_kept_file_is_reported(tmp_path):
    files = {"compose.yml": f"name: {OLD}-selfhost\n# the {OLD} stack\n"}
    root = _tree(tmp_path, files, _keep("compose.yml", f"{OLD}-selfhost", 1))
    assert _run(root) == ["RESIDUAL compose.yml:2: # the " + OLD + " stack"]
    files = {"compose.yml": f"name: {OLD}-selfhost\nimage: {OLD}-selfhost\n"}
    root = _tree(tmp_path / "2", files, _keep("compose.yml", f"{OLD}-selfhost", 1))
    assert _run(root) == [f"COUNT compose.yml '{OLD}-selfhost': pinned 1, found 2"]


def test_an_entry_that_would_keep_everything_is_refused(tmp_path):
    root = _tree(tmp_path, {"a.md": OLD}, _keep("*", OLD, 1))
    with pytest.raises(checker.UsageError, match="bare name"):
        _run(root)


def test_a_module_entry_is_skipped_only_when_modules_is_gone(tmp_path):
    keep = _keep("modules/x.py", f"{OLD}.lock", 1)
    root = _tree(tmp_path, {"other.txt": "x\n"}, keep)
    assert _run(root) == []
    root = _tree(tmp_path / "2", {"modules/y.py": "x\n"}, keep)
    assert _run(root) == [f"STALE-ALLOW modules/x.py '{OLD}.lock' matched nothing"]


def test_a_module_file_is_kept_only_by_a_module_entry(tmp_path):
    """Otherwise an entry's count would differ between the full tree and the
    core copy, which deletes modules/."""
    files = {"modules/a.py": f"LOCK = '{OLD}.lock'\n", "b.py": f"LOCK = '{OLD}.lock'\n"}
    root = _tree(tmp_path, files, _keep("*", f"{OLD}.lock", 1))
    assert _run(root) == [f"RESIDUAL modules/a.py:1: LOCK = '{OLD}.lock'"]
    keep = _keep("*", f"{OLD}.lock", 1) + _keep("modules/*", f"{OLD}.lock", 1)
    assert _run(_tree(tmp_path / "2", files, keep)) == []


@pytest.mark.skipif(shutil.which("git") is None, reason="no git")
def test_a_checkout_is_walked_as_git_lists_it(tmp_path):
    """A clone's .git/config names the former remote and an editable install
    leaves an egg-info: neither is part of the tree. A tracked file is."""
    root = _tree(tmp_path, {"README.md": "Controlvar\n", ".gitignore": "*.egg-info/\n"})
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "remote",
            "add",
            "origin",
            f"https://github.com/get{OLD}/{OLD}.git",
        ],
        check=True,
        env=env,
    )
    egg = root / f"{OLD}_platform.egg-info"
    egg.mkdir()
    (egg / "PKG-INFO").write_text(f"Name: {OLD}-platform\n")
    assert _run(root) == []
    (root / "README.md").write_text(f"{OLD.capitalize()}\n")
    assert _run(root) == [f"RESIDUAL README.md:1: {OLD.capitalize()}"]


def test_without_git_generated_directories_are_skipped(tmp_path):
    """The core-build copy has no .git: everything but caches is walked."""
    root = _tree(
        tmp_path,
        {
            "__pycache__/x.pyc": OLD.encode(),
            "frontend/node_modules/p/index.js": OLD,
            "src/app.py": "pass\n",
        },
    )
    assert _run(root) == []
    (root / "src" / "app.py").write_text(f"# {OLD}\n")
    assert _run(root) == [f"RESIDUAL src/app.py:1: # {OLD}"]
