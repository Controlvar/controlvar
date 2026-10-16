"""Workflows and their scripts find this repository by its id, not its name.

A rename or a transfer changes ``owner/name`` and keeps the repository id. A
job guarded by ``github.repository == '<owner>/<name>'`` is skipped, and shows
green, from the moment the name changes; a script with the name written in it
reads, links or refuses against the old name. So:

* the five guards compare ``github.repository_id`` with :data:`REPOSITORY_ID`,
  each exactly as :data:`GUARDS` writes it, and no workflow compares
  ``github.repository`` with anything;
* every ``github.repository_id`` comparison is with :data:`REPOSITORY_ID`, so a
  mistyped id fails here rather than skipping a job;
* the scripts that resolved the name do it from the run or the id
  (``rc_evidence.REPOSITORY_ID`` is the same id);
* the old name appears in ``.github/workflows/``, ``.github/actions/`` and
  ``scripts/`` only on a line :data:`KEEP` lists, with its reason, and every
  :data:`KEEP` line is still there, so an entry cannot outlive its line.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCANNED = (WORKFLOWS, REPO_ROOT / ".github" / "actions", REPO_ROOT / "scripts")

#: This repository's id (``gh api repos/<owner>/<name> --jq .id``).
REPOSITORY_ID = "1367480368"
OLD_NAME = "getexperimently/experimently"
_OLD = re.compile(re.escape(OLD_NAME), re.IGNORECASE)

#: Each guard, by workflow, as its line reads (stripped).
GUARDS: Dict[str, str] = {
    "codeql.yml": (
        "upload: ${{ github.repository_id == '1367480368' && 'always' || 'never' }}"
    ),
    "dependabot-licences.yml": "if: github.repository_id == '1367480368'",
    "dependabot-lock.yml": "if: github.repository_id == '1367480368'",
    "docs.yml": (
        "if: startsWith(github.ref, 'refs/tags/v') && !contains(github.ref, '-')"
        " && github.repository_id == '1367480368'"
    ),
    "security-scan.yml": (
        "if: github.event_name == 'pull_request'"
        " && github.repository_id == '1367480368'"
    ),
}

#: Lines that keep the old name, by path, with why. Stripped, exact.
KEEP: List[Tuple[str, str, str]] = [
    (
        ".github/workflows/release.yml",
        "# publishes to ghcr.io/getexperimently/experimently with no edit at the cut.",
        "a comment about the image path, which follows the repository",
    ),
    (
        ".github/workflows/sdk-release.yml",
        "#   sdk/go/v1.2.3        github.com/getexperimently/experimently/sdk/go",
        "a comment: the Go module path go.mod declares",
    ),
    (
        ".github/workflows/sdk-release.yml",
        "# (getexperimently/experimently), deliberately and permanently, while",
        "a comment on the Go module path",
    ),
    (
        "scripts/doc_claims.py",
        'EVIDENCE_REPOSITORY = "getexperimently/experimently"',
        "the name dated evidence links were recorded with; never rewritten",
    ),
    (
        "scripts/check_required_checks.sh",
        'REPO="getexperimently/experimently"',
        "an operator tool run against the named repository",
    ),
    (
        "scripts/configure-repo.sh",
        'REPO="getexperimently/experimently"',
        "an operator tool run against the named repository",
    ),
    (
        "scripts/check_version_sources.py",
        '"api": "ghcr.io/getexperimently/experimently",',
        "the published image path, compared with equally literal files",
    ),
    (
        "scripts/check_version_sources.py",
        '"web": "ghcr.io/getexperimently/experimently-web",',
        "the published image path, compared with equally literal files",
    ),
    (
        "scripts/check_version_sources.py",
        r'r"^\s*image:\s*(?P<repo>ghcr\.io/getexperimently/experimently(?:-web)?)"',
        "the published image path, compared with equally literal files",
    ),
    (
        "scripts/check_version_sources.py",
        '"`image: ghcr.io/getexperimently/experimently[-web]:"',
        "the published image path, in a message",
    ),
    (
        "scripts/check_image_labels.py",
        'SOURCE = "https://github.com/getexperimently/experimently"',
        "the image source label, compared with the equally literal Dockerfiles",
    ),
    (
        "scripts/check_compose_config.py",
        '"api": f"ghcr.io/getexperimently/experimently:{profile}-{args.version}",',
        "the published image path, compared with the equally literal compose file",
    ),
    (
        "scripts/check_compose_config.py",
        '"web": f"ghcr.io/getexperimently/experimently-web:{profile}-{args.version}",',
        "the published image path, compared with the equally literal compose file",
    ),
    (
        "scripts/chart_kind.sh",
        "API_REPO=ghcr.io/getexperimently/experimently",
        "the published image path; the previous release's is resolved separately",
    ),
    (
        "scripts/chart_kind.sh",
        "WEB_REPO=ghcr.io/getexperimently/experimently-web",
        "the published image path; the previous release's is resolved separately",
    ),
]

_NAME_COMPARISON = re.compile(
    r"github\.repository\s*(?:==|!=)|(?:==|!=)\s*github\.repository\b(?!_)"
)
_ID_COMPARISON = re.compile(r"github\.repository_id\s*(?:==|!=)\s*'([^']*)'")


def _files() -> List[Path]:
    out = []
    for root in SCANNED:
        if root.is_dir():
            out.extend(
                p
                for p in sorted(root.rglob("*"))
                if p.is_file() and "__pycache__" not in p.parts
            )
    return out


def old_name_lines(files: List[Path], root: Path = REPO_ROOT) -> List[Tuple[str, str]]:
    """(path, stripped line) for every line naming the old repository."""
    found = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(root).as_posix()
        for line in text.splitlines():
            if _OLD.search(line):
                found.append((rel, line.strip()))
    return found


def unlisted(found: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    kept = {(path, line) for path, line, _ in KEEP}
    return [item for item in found if item not in kept]


def stale(found: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    present = set(found)
    return [(path, line) for path, line, _ in KEEP if (path, line) not in present]


@pytest.mark.parametrize("workflow", sorted(GUARDS))
def test_each_guard_compares_the_repository_id(workflow):
    lines = [
        line.strip()
        for line in (WORKFLOWS / workflow).read_text(encoding="utf-8").splitlines()
    ]
    assert GUARDS[workflow] in lines, f"{workflow}: no line {GUARDS[workflow]!r}"


def test_no_workflow_compares_the_repository_name():
    hits = [
        f"{path.name}: {line.strip()}"
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if _NAME_COMPARISON.search(line)
    ]
    assert hits == []


def test_every_repository_id_comparison_is_this_repository():
    compared = {
        path.name: _ID_COMPARISON.findall(path.read_text(encoding="utf-8"))
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
    }
    compared = {name: ids for name, ids in compared.items() if ids}
    assert set(compared) == set(GUARDS)
    for name, ids in compared.items():
        assert ids == [REPOSITORY_ID], f"{name}: {ids}"


def test_rc_evidence_resolves_the_same_id():
    spec = importlib.util.spec_from_file_location(
        "rc_evidence_for_guards", REPO_ROOT / "scripts" / "rc_evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.REPOSITORY_ID == REPOSITORY_ID
    assert not hasattr(module, "REPO")


def test_the_old_name_is_only_where_it_is_kept():
    assert unlisted(old_name_lines(_files())) == []


def test_every_kept_line_is_still_there():
    assert stale(old_name_lines(_files())) == []


def test_the_scan_finds_a_planted_name(tmp_path, monkeypatch):
    """The scan fires: a planted line, in any case, is unlisted; a KEEP entry
    whose line is gone is stale."""
    planted = tmp_path / "scripts" / "planted.py"
    planted.parent.mkdir()
    planted.write_text('X = 1\nR = "GetExperimently/Experimently"\n')
    found = old_name_lines([planted], root=tmp_path)
    assert unlisted(found) == [
        ("scripts/planted.py", 'R = "GetExperimently/Experimently"')
    ]
    monkeypatch.setattr(
        sys.modules[__name__],
        "KEEP",
        [("scripts/planted.py", 'R = "GetExperimently/Experimently"', "planted")],
    )
    assert unlisted(found) == []
    planted.write_text("X = 1\n")
    assert stale(old_name_lines([planted], root=tmp_path)) == [
        ("scripts/planted.py", 'R = "GetExperimently/Experimently"')
    ]


@pytest.mark.parametrize(
    "line",
    [
        "if: github.repository == 'a/b'",
        "if: github.repository != 'a/b'",
        "if: 'a/b' == github.repository",
        "upload: ${{ github.repository=='a/b' && 'always' || 'never' }}",
    ],
)
def test_the_name_comparison_check_fires(line):
    assert _NAME_COMPARISON.search(line)


def test_the_id_comparison_is_not_a_name_comparison():
    assert not _NAME_COMPARISON.search("if: github.repository_id == '1367480368'")
    assert not _NAME_COMPARISON.search("REPO: ${{ github.repository }}")
