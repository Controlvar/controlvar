"""`scripts/classify_changes.py` -- is a pull request documentation only?

A wrong "yes" merges a code change green without its tests (the heavy jobs of
``pr-qa-gate.yml`` report without working), so these cases pin the fail-closed
edges: ``docs/api/**``, the empty list, and the rename pair ``git diff`` prints
once ``--no-renames`` is given.

No git anywhere: the file list is handed in, exactly as the workflow pipes it,
so this runs in ``scripts/core_build.sh``'s copy too. That the workflow passes
``--no-renames`` is pinned separately, in
``backend/tests/unit/infrastructure/test_docs_only_gate.py``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "classify_changes.py"

_spec = importlib.util.spec_from_file_location("classify_changes", SCRIPT)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


def _run(stdin: str) -> str:
    """The script as the workflow runs it: file list in, one output line out."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.mark.parametrize(
    "files",
    [
        ["docs/auth/sso.md"],
        ["docs/getting-started/quick-start.md", "docs/README.md"],
        ["docs/deployment/rollback-runbook.md", "mkdocs.yml"],
    ],
    ids=["one page", "two pages", "page and site config"],
)
def test_documentation_only(files):
    assert cc.classify(files) == (True, [])
    assert _run("\n".join(files) + "\n") == "docs_only=true\n"


@pytest.mark.parametrize(
    "path",
    ["docs/_claims/docs/workspaces/quickstart.md.toml", "docs/_claims/_frozen.tsv"],
)
def test_the_claims_registry_is_documentation(path):
    """``docs/_claims/`` holds what verifies each sentence of a page
    (``scripts/doc_claims.py``). A pull request that changes a page and its
    registry stays docs-only, and core-build's docs step runs the registry
    test (it is under ``backend/tests/unit/docs/``)."""
    assert cc.classify([path, "docs/workspaces/quickstart.md"]) == (True, [])
    assert _run(path + "\n") == "docs_only=true\n"


def test_mkdocs_yml_alone_is_documentation():
    """The chosen rule: the site configuration is documentation.

    Nothing imports it; the one test that reads it (``test_doc_examples.py``,
    through ``markdown_extensions``) is among those ``core-build`` still runs
    on a docs-only change.
    """
    assert cc.classify(["mkdocs.yml"]) == (True, [])
    assert _run("mkdocs.yml\n") == "docs_only=true\n"


@pytest.mark.parametrize(
    "path",
    ["mkdocs.yaml", "sub/mkdocs.yml", "mkdocs.yml.orig"],
)
def test_only_the_exact_mkdocs_yml_is_exempt(path):
    assert cc.classify([path])[0] is False


@pytest.mark.parametrize(
    "path",
    [
        "docs/api/openapi-v1.stable.json",
        "docs/api/openapi-v1.full.json",
        "docs/api/stability.md",
    ],
)
def test_docs_api_is_never_documentation(path):
    """Generated from the code and pinned by the OpenAPI snapshot tests."""
    assert cc.classify([path]) == (False, [path])
    assert cc.classify(["docs/README.md", path]) == (False, [path])
    assert _run(f"docs/README.md\n{path}\n") == "docs_only=false\n"


def test_mixed_change_is_not_documentation():
    files = ["docs/auth/sso.md", "backend/app/main.py"]
    assert cc.classify(files) == (False, ["backend/app/main.py"])
    assert _run("\n".join(files) + "\n") == "docs_only=false\n"


def test_a_workflow_change_is_not_documentation():
    files = ["docs/README.md", ".github/workflows/pr-qa-gate.yml"]
    assert cc.classify(files)[0] is False


@pytest.mark.parametrize(
    "stdin", ["", "\n", "  \n\n"], ids=["empty", "newline", "blank"]
)
def test_empty_is_not_documentation(stdin):
    """Nothing to classify proves nothing: the full gate runs."""
    assert cc.classify(cc.parse(stdin.splitlines(True))) == (False, [])
    assert _run(stdin) == "docs_only=false\n"


def test_the_rename_pair_is_not_documentation():
    """``git mv backend/a.py docs/a.py`` under ``git diff --no-renames``.

    Without ``--no-renames``, ``--name-only`` prints only ``docs/a.py`` and the
    change would be classed as documentation. With it, both sides are listed
    and the deleted ``backend/a.py`` fails the rule.
    """
    assert cc.classify(["backend/a.py", "docs/a.py"]) == (False, ["backend/a.py"])
    assert _run("backend/a.py\ndocs/a.py\n") == "docs_only=false\n"


def test_a_path_that_only_starts_like_docs_is_not_documentation():
    for path in ["docs", "docsite/index.md", "documentation/x.md", "./docs/x.md"]:
        assert cc.classify([path])[0] is False, path


def test_the_output_is_exactly_one_line_for_github_output():
    """Anything else on stdout lands in $GITHUB_OUTPUT and corrupts it."""
    out = _run("docs/a.md\nbackend/b.py\n")
    assert out.count("\n") == 1
    assert out.startswith("docs_only=")
