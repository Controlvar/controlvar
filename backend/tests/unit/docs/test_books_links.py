"""The docs site, its home page and README.md link the Controlvar books (#1859).

The books live in their own repository, CC BY 4.0, and teach through this
codebase. controlvar.com links them from a homepage section and the footer
through ``BOOKS_URL`` in ``frontend/src/content/site-links.ts``, which Jest
holds (``frontend/src/tests/pages/home-page.test.tsx``). The other three
places are held here, because a pull request that changes only docs/,
mkdocs.yml or README.md runs no Jest: it runs the "Docs content tests" step,
which collects this directory.

Reads files only (no git, no network), so it runs in ``scripts/core_build.sh``'s
copy as well.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
BOOKS_URL = "https://github.com/amarkanday/controlvar-books"


class _AnyTagLoader(yaml.SafeLoader):
    """A safe loader that builds a node with an unknown tag as plain YAML."""


def _untagged(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_AnyTagLoader.add_multi_constructor("", _untagged)


def markdown_section(path: Path, heading: str) -> str:
    """The lines under ``## <heading>`` up to the next ``## `` heading."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"## {heading}")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start + 1 : end])


def test_the_homepage_links_the_same_url():
    text = (REPO_ROOT / "frontend" / "src" / "content" / "site-links.ts").read_text(
        encoding="utf-8"
    )
    assert f"export const BOOKS_URL = '{BOOKS_URL}';" in text


def test_the_docs_navigation_lists_the_books_under_getting_started():
    config: Dict[str, Any] = yaml.load(
        (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8"), Loader=_AnyTagLoader
    )
    getting_started: List[Any] = next(
        item["Getting Started"]
        for item in config["nav"]
        if isinstance(item, dict) and "Getting Started" in item
    )
    assert {"The Controlvar Books": BOOKS_URL} in getting_started


def test_the_docs_home_page_has_a_start_here_row_for_the_books():
    section = markdown_section(REPO_ROOT / "docs" / "README.md", "Start Here")
    rows = [
        line
        for line in section.splitlines()
        if line.startswith(f"| [The Controlvar books]({BOOKS_URL}) | ")
    ]
    assert len(rows) == 1


def test_the_readme_documentation_list_links_the_books():
    section = markdown_section(REPO_ROOT / "README.md", "Documentation")
    assert f"\n- [The Controlvar books]({BOOKS_URL}): " in f"\n{section}"
