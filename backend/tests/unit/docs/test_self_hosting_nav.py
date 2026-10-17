"""Self-Hosting in the docs navigation leads to the self-hosting stack (#1148).

The Docker guide and the compose-file page describe the development stack:
``docker-compose.yml`` at the repository root, a demo administrator, a short
``SECRET_KEY`` accepted, ports on ``127.0.0.1`` only. The stack meant for a
server is ``deploy/compose/compose.yml``, which runs the published images.
The navigation had the development guide under ``Self-Hosting -> Docker
Compose``, so a reader who followed it set up the wrong thing. It now leads to
``deploy/compose/README.md``, which says how to run that file and lists its
settings.

These read ``mkdocs.yml`` and pages as text; no Docker, no git, so they run
in the docs-only gate's "Docs content tests" (this directory) and in
``scripts/core_build.sh``'s copy.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
MKDOCS = REPO_ROOT / "mkdocs.yml"
DOCS = REPO_ROOT / "docs"

#: The two pages that describe the development stack.
DEVELOPMENT_PAGES = (
    "getting-started/docker-guide.md",
    "getting-started/docker-compose-file.md",
)
BLOB = "https://github.com/Controlvar/controlvar/blob/main/"
SELF_HOSTING_STACK = BLOB + "deploy/compose/compose.yml"
#: What Self-Hosting -> Docker Compose links: the README beside the file, which
#: says how to run it and lists its settings.
SELF_HOSTING_README = BLOB + "deploy/compose/README.md"


class _AnyTagLoader(yaml.SafeLoader):
    """A safe loader that builds a node with an unknown tag as plain YAML."""


def _untagged(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_AnyTagLoader.add_multi_constructor("", _untagged)


def leaves(node: Any) -> List[str]:
    """Every page path or URL under *node*, in nav order."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [leaf for item in node for leaf in leaves(item)]
    if isinstance(node, dict):
        return [leaf for value in node.values() for leaf in leaves(value)]
    return []


def section(nav: List[Any], title: str) -> List[str]:
    """The leaves of the top-level section called *title*."""
    for item in nav:
        if isinstance(item, dict) and title in item:
            return leaves(item[title])
    raise AssertionError(f"the nav has no top-level section {title!r}")


def problems(nav: List[Any]) -> List[str]:
    """What is wrong with the Self-Hosting and Getting Started sections."""
    found: List[str] = []
    self_hosting = section(nav, "Self-Hosting")
    getting_started = section(nav, "Getting Started")
    for page in DEVELOPMENT_PAGES:
        if page in self_hosting:
            found.append(f"Self-Hosting lists the development page {page}")
        if page not in getting_started:
            found.append(f"Getting Started does not list {page}")
    if SELF_HOSTING_README not in self_hosting:
        found.append(f"Self-Hosting does not link {SELF_HOSTING_README}")
    if "self-hosting/releases.md" not in self_hosting:
        found.append("Self-Hosting does not list self-hosting/releases.md")
    return found


@pytest.fixture(scope="module")
def nav() -> List[Any]:
    config: Dict[str, Any] = yaml.load(
        MKDOCS.read_text(encoding="utf-8"), Loader=_AnyTagLoader
    )
    return config["nav"]


def test_self_hosting_leads_to_the_self_hosting_stack(nav):
    assert problems(nav) == []


@pytest.mark.parametrize("link", [SELF_HOSTING_STACK, SELF_HOSTING_README])
def test_the_linked_stack_is_a_file_in_this_repository(link):
    """A link that names a path which moved would answer 404 on the site."""
    path = link.removeprefix(BLOB)
    assert (REPO_ROOT / path).is_file(), f"{path} is not in the repository"


def test_the_docker_guide_says_it_is_for_local_development():
    text = (DOCS / "getting-started" / "docker-guide.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "# Docker Local Development"
    opening = " ".join(lines[1:6])
    for phrase in (
        "local development stack",
        "not for a server",
        "127.0.0.1",
        "short `SECRET_KEY`",
        "demo administrator",
        SELF_HOSTING_STACK,
        SELF_HOSTING_README,
    ):
        assert phrase in opening, f"the guide's opening does not say {phrase!r}"


def test_the_pages_that_send_a_reader_to_the_stack_say_only_what_is_true():
    """releases.md names compose.yml and has no run procedure; the commands
    and settings are deploy/compose/README.md. A sentence that says the page
    "describes" the stack is the claim this pins against, and the two
    development pages link the README as well as the file."""
    releases = (DOCS / "self-hosting" / "releases.md").read_text(encoding="utf-8")
    assert "deploy/compose/compose.yml" in releases
    for relative in (
        "getting-started/docker-guide.md",
        "getting-started/docker-compose-file.md",
        "getting-started/quick-start.md",
    ):
        text = (DOCS / relative).read_text(encoding="utf-8")
        assert SELF_HOSTING_STACK in text, f"{relative} does not link the stack"
        assert "described in [Releases" not in text, relative
        assert "cannot reach it" not in text, relative
        if relative in DEVELOPMENT_PAGES:
            assert SELF_HOSTING_README in text, f"{relative} does not link the README"


def test_no_page_starts_a_development_server_on_all_interfaces():
    """`uvicorn ... --host 0.0.0.0` and `next dev -H 0.0.0.0` in a page or a
    slash command are what a reader runs next to the development stack."""
    sources = sorted(DOCS.rglob("*.md")) + sorted(
        (REPO_ROOT / ".claude" / "commands").glob("*.md")
    )
    assert len(sources) > 100, "the scan found almost nothing: it is broken"
    found: List[str] = []
    for path in sources:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for index in range(len(lines)):
            if listens_on_all_interfaces(lines[index]):
                where = path.relative_to(REPO_ROOT).as_posix()
                found.append(f"{where}:{index + 1}: {lines[index].strip()}")
    assert found == []


def listens_on_all_interfaces(line: str) -> bool:
    """A command line that starts uvicorn or next on 0.0.0.0 (not a CIDR)."""
    return bool(
        re.search(r"\b(uvicorn|next dev|npm run dev)\b", line)
        and re.search(r"(?:--host|--hostname|-H)[ =]+0\.0\.0\.0(?![\w./])", line)
    )


@pytest.mark.parametrize(
    "line, expected",
    [
        ("uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000", True),
        ("npm run dev -- -H 0.0.0.0", True),
        ("next dev --hostname=0.0.0.0", True),
        ("uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000", False),
        ("ALB: Ingress 443 from 0.0.0.0/0", False),
        ("uvicorn backend.app.main:app --host 0.0.0.0/0", False),
    ],
)
def test_a_planted_all_interfaces_command_is_seen(line, expected):
    assert listens_on_all_interfaces(line) is expected


# ---------------------------------------------------------------------------
# The check, planted against the navigation as it was
# ---------------------------------------------------------------------------
GOOD = [
    {
        "Getting Started": [
            {"Docker guide": DEVELOPMENT_PAGES[0]},
            {"The compose file": DEVELOPMENT_PAGES[1]},
        ]
    },
    {
        "Self-Hosting": [
            {"Docker Compose": SELF_HOSTING_README},
            {"Releases": "self-hosting/releases.md"},
        ]
    },
]


def test_the_planted_good_navigation_passes():
    assert problems(GOOD) == []


def test_the_old_navigation_is_refused():
    """Docker guide under Self-Hosting, nothing linking the self-hosting stack."""
    old = [
        {"Getting Started": [{"Quick Start": "getting-started/quick-start.md"}]},
        {
            "Self-Hosting": [
                {"Docker Compose": DEVELOPMENT_PAGES[0]},
                {"The Compose File": DEVELOPMENT_PAGES[1]},
                {"Releases": "self-hosting/releases.md"},
            ]
        },
    ]
    found = problems(old)
    assert f"Self-Hosting lists the development page {DEVELOPMENT_PAGES[0]}" in found
    assert f"Self-Hosting lists the development page {DEVELOPMENT_PAGES[1]}" in found
    assert f"Getting Started does not list {DEVELOPMENT_PAGES[0]}" in found
    assert f"Self-Hosting does not link {SELF_HOSTING_README}" in found


def test_a_navigation_without_the_releases_page_is_refused():
    nav = [
        GOOD[0],
        {"Self-Hosting": [{"Docker Compose": SELF_HOSTING_README}]},
    ]
    assert problems(nav) == ["Self-Hosting does not list self-hosting/releases.md"]


def test_a_navigation_without_the_section_is_refused():
    with pytest.raises(AssertionError, match="no top-level section 'Self-Hosting'"):
        problems([GOOD[0]])
