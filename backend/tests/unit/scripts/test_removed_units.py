"""scripts/removed_units.py: what a documentation change cut, with the audit's verdict.

Each test builds a base tree and a head tree (an inventory and a few pages) and
an audit file in tmp_path, and runs the script as a reviewer would. No file of
this repository's docs is read, and the audit rows are made up here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "removed_units.py"

INVENTORY = """
[pages."a.md"]
class = "reference"
reason = "a"

[pages."b.md"]
class = "reference"
reason = "b"

[documents."README.md"]
scope = "in"
class = "reference"
reason = "the readme"

[documents."CONTRIBUTING.md"]
scope = "out"
out = "contributor"
reason = "for contributors"
"""

PAGE_A = """# Page A

Experiments are listed newest first.
The default rate limit is 6000 a minute.
The dashboard works offline.

```bash
curl localhost:8000/api/v1/experiments/
```

Some sentence the audit never saw.
"""

BASE: Dict[str, str] = {
    "tests/acceptance/docs/inventory.toml": INVENTORY,
    "docs/a.md": PAGE_A,
    "docs/b.md": "# Page B\n\nNothing here yet.\n",
    "README.md": "# Readme\n\nRun `make dev`.\n",
    "CONTRIBUTING.md": "# Contributing\n\nOpen a pull request.\n",
}


def _row(line: int, text: str, **fields) -> Dict[str, object]:
    row: Dict[str, object] = {
        "doc": "docs/a.md",
        "kind": "claim",
        "line": line,
        "text": text,
        "covered": False,
        "by": None,
        "tag": None,
    }
    row.update(fields)
    return row


ROWS: List[Dict[str, object]] = [
    _row(
        3, "Experiments are listed newest first.", covered=True, by="test_list_order.py"
    ),
    _row(4, "The default rate limit is 6000 a minute.", tag="T"),
    _row(5, "The dashboard works offline.", tag="R"),
    _row(8, "curl localhost:8000/api/v1/experiments/", kind="flow", tag="S"),
    _row(1, "A sentence that was never on the page.", tag="T"),
]


def _tree(root: Path, files: Dict[str, Optional[str]]) -> Path:
    for path, text in files.items():
        if text is None:
            continue
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
    return root


def _audit(tmp_path: Path, rows: List[Dict[str, object]] = ROWS) -> Path:
    path = tmp_path / "audit" / "B01.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path.parent


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True
    )


def _report(tmp_path: Path, head_changes: Dict[str, Optional[str]], **audit) -> str:
    base = _tree(tmp_path / "base", BASE)
    head_files = {**BASE, **head_changes}
    head = _tree(tmp_path / "head", head_files)
    result = _run(
        "--audit",
        str(_audit(tmp_path, **audit)),
        "--base-dir",
        str(base),
        "--head-dir",
        str(head),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_a_cut_covered_claim_is_a_removed_true_claim(tmp_path):
    out = _report(
        tmp_path,
        {"docs/a.md": PAGE_A.replace("Experiments are listed newest first.\n", "")},
    )
    assert (
        "removed TRUE claim: where did it go?  docs/a.md:3 (audit)"
        "  [tested: test_list_order.py]  "
        '"Experiments are listed newest first."'
    ) in out
    assert "  removed TRUE claim: 1" in out
    assert "tested 1, T 0" in out


def test_a_cut_untested_claim_is_a_removed_true_claim_with_its_tag(tmp_path):
    out = _report(
        tmp_path,
        {"docs/a.md": PAGE_A.replace("The default rate limit is 6000 a minute.\n", "")},
    )
    assert "removed TRUE claim: where did it go?  docs/a.md:4 (audit)  [T]" in out


def test_a_cut_false_claim_is_marked_false(tmp_path):
    out = _report(
        tmp_path,
        {"docs/a.md": PAGE_A.replace("The dashboard works offline.\n", "")},
    )
    assert "removed false claim (R)  docs/a.md:5 (audit)  [R]" in out
    assert "removed TRUE claim: 0" in out
    assert "removed false (R): 1" in out


def test_a_cut_flow_is_a_removed_flow(tmp_path):
    out = _report(
        tmp_path,
        {"docs/a.md": PAGE_A.replace("curl localhost:8000/api/v1/experiments/\n", "")},
    )
    assert "removed flow: where did it go?  docs/a.md:8 (audit)  [S]" in out


def test_a_claim_moved_to_another_page_says_where(tmp_path):
    out = _report(
        tmp_path,
        {
            "docs/a.md": PAGE_A.replace("Experiments are listed newest first.\n", ""),
            "docs/b.md": "# Page B\n\nExperiments are listed\nnewest first.\n",
        },
    )
    assert "moved to docs/b.md  docs/a.md:3 (audit)" in out
    assert "removed TRUE claim: 0" in out


def test_an_excerpt_that_elides_is_matched_piece_by_piece(tmp_path):
    rows = [_row(8, "curl ... /api/v1/experiments/", kind="flow", tag="S")]
    out = _report(
        tmp_path,
        {"docs/a.md": PAGE_A.replace("curl localhost:8000/api/v1/experiments/\n", "")},
        rows=rows,
    )
    assert "removed flow: where did it go?  docs/a.md:8 (audit)  [S]" in out
    assert "audit rows not found on their base page (not checked): 0" in out


def test_a_removed_line_with_no_audit_row_is_listed(tmp_path):
    out = _report(
        tmp_path,
        {"docs/a.md": PAGE_A.replace("Some sentence the audit never saw.\n", "")},
    )
    assert (
        'removed, no audit row  docs/a.md:11  "Some sentence the audit never saw."'
    ) in out


def test_a_reflow_or_a_markup_edit_is_not_a_cut(tmp_path):
    out = _report(
        tmp_path,
        {
            "docs/a.md": PAGE_A.replace(
                "Experiments are listed newest first.",
                "Experiments are listed\n`newest` first.",
            )
        },
    )
    assert "docs/a.md\n  (changed; nothing removed)" in out
    assert "removed TRUE claim: 0" in out
    assert "removed, no audit row: 0" in out


def test_a_deleted_page_lists_every_row_that_was_on_it(tmp_path):
    out = _report(tmp_path, {"docs/a.md": None})
    assert "docs/a.md (deleted)" in out
    assert "removed TRUE claim: 2" in out
    assert "removed flow: 1" in out
    assert "removed false (R): 1" in out
    assert "documents changed: 1 (1 deleted)" in out


def test_an_unchanged_tree_reports_nothing(tmp_path):
    out = _report(tmp_path, {})
    assert "documents changed: 0 (0 deleted)" in out
    assert "removed TRUE claim: 0" in out


def test_a_row_not_on_its_base_page_is_counted_not_checked(tmp_path):
    out = _report(
        tmp_path,
        {"docs/a.md": PAGE_A.replace("Experiments are listed newest first.\n", "")},
    )
    assert "audit rows not found on their base page (not checked): 1" in out


def test_an_out_of_scope_document_is_not_read_unless_a_row_names_it(tmp_path):
    changed = {"CONTRIBUTING.md": "# Contributing\n"}
    assert "CONTRIBUTING.md" not in _report(tmp_path / "one", changed)
    rows = ROWS + [_row(3, "Open a pull request.", doc="CONTRIBUTING.md", tag="T")]
    out = _report(tmp_path / "two", changed, rows=rows)
    assert "removed TRUE claim: where did it go?  CONTRIBUTING.md:3 (audit)  [T]" in out


def test_out_inside_the_repository_is_refused_and_nothing_is_written(tmp_path):
    base = _tree(tmp_path / "base", BASE)
    target = REPO_ROOT / "removed-units-report.txt"
    assert not target.exists()
    result = _run(
        "--audit",
        str(_audit(tmp_path)),
        "--base-dir",
        str(base),
        "--head-dir",
        str(base),
        "--out",
        str(target),
    )
    assert result.returncode == 2, result.stdout
    assert "write it outside the repository" in result.stderr
    assert not target.exists()


def test_out_inside_the_head_tree_is_refused(tmp_path):
    base = _tree(tmp_path / "base", BASE)
    head = _tree(tmp_path / "head", BASE)
    result = _run(
        "--audit",
        str(_audit(tmp_path)),
        "--base-dir",
        str(base),
        "--head-dir",
        str(head),
        "--out",
        str(head / "docs" / "report.txt"),
    )
    assert result.returncode == 2
    assert not (head / "docs" / "report.txt").exists()


def test_out_elsewhere_gets_the_report_and_stdout_stays_empty(tmp_path):
    base = _tree(tmp_path / "base", BASE)
    head = _tree(
        tmp_path / "head",
        {
            **BASE,
            "docs/a.md": PAGE_A.replace("Experiments are listed newest first.\n", ""),
        },
    )
    target = tmp_path / "private" / "report.txt"
    target.parent.mkdir()
    result = _run(
        "--audit",
        str(_audit(tmp_path)),
        "--base-dir",
        str(base),
        "--head-dir",
        str(head),
        "--out",
        str(target),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "removed TRUE claim: where did it go?" in target.read_text()


def test_an_unreadable_audit_row_is_refused(tmp_path):
    base = _tree(tmp_path / "base", BASE)
    audit = tmp_path / "audit.jsonl"
    audit.write_text('{"doc": "docs/a.md"}\n')
    result = _run(
        "--audit", str(audit), "--base-dir", str(base), "--head-dir", str(base)
    )
    assert result.returncode == 2
    assert "audit.jsonl:1: not an audit row" in result.stderr


def test_the_audit_is_required(tmp_path):
    result = _run("--base-dir", str(tmp_path), "--head-dir", str(tmp_path))
    assert result.returncode == 2
    assert "--audit" in result.stderr


def test_the_base_can_be_a_git_ref(tmp_path):
    """The usual run: the pull request's working tree against origin/main."""
    git = shutil.which("git")
    assert git, "git is needed to read the base from a ref"
    head = _tree(tmp_path / "repo", BASE)
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    for command in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
        subprocess.run([git, "-C", str(head), *command], check=True, env=env)
    (head / "docs" / "a.md").write_text(
        PAGE_A.replace("Experiments are listed newest first.\n", "")
    )
    result = _run(
        "--audit", str(_audit(tmp_path)), "--base-ref", "HEAD", "--head-dir", str(head)
    )
    assert result.returncode == 0, result.stderr
    assert "removed TRUE claim: where did it go?  docs/a.md:3 (audit)" in result.stdout
    assert "Removed units: base HEAD" in result.stdout

    missing = _run(
        "--audit",
        str(_audit(tmp_path)),
        "--base-ref",
        "no-such-ref",
        "--head-dir",
        str(head),
    )
    assert missing.returncode == 2
    assert "no-such-ref is not a commit" in missing.stderr
