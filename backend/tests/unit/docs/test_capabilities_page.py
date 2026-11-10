"""The capability page: what its registry may exempt, and where its nav links go.

``docs/capabilities.md`` lists what Controlvar does, one line each, and every
line is a claim. ``scripts/doc_claims.py check`` accepts an ``exempt:`` target
on any unit, so a bullet could be marked "navigation" and stop being held by
anything without the check noticing. This pins it: in
``docs/_claims/docs/capabilities.md.toml`` only the title, the opening, the
headings, the one bullet that is a pointer, and the closing line may be
exempt, and every other bullet has at least one target that is not.

The last two hold the links into this repository on GitHub, in the nav and on
the page (``https://github.com/Controlvar/controlvar/blob/main/<path>``, which
the strict docs build does not check), to files that exist in the tree.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "capabilities.md"
REGISTRY = REPO_ROOT / "docs" / "_claims" / "docs" / "capabilities.md.toml"
MKDOCS = REPO_ROOT / "mkdocs.yml"

#: The one bullet that points at a table rather than stating a capability.
POINTER = "What has a screen and what is API only"

LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
BLOB = re.compile(r"https://github\.com/Controlvar/controlvar/blob/main/([^\s)\"']+)")


def _normal(line: str) -> str:
    return " ".join(LINK.sub(r"\1", line).replace("`", "").replace("*", "").split())


def _bullets() -> list[str]:
    return [
        _normal(line[2:])
        for line in PAGE.read_text(encoding="utf-8").splitlines()
        if line.startswith("- ")
    ]


def _units() -> dict[str, list[str]]:
    with REGISTRY.open("rb") as fh:
        return {u["text"]: u["by"] for u in tomllib.load(fh)["unit"]}


def exemption_problems(bullets: list[str], units: dict[str, list[str]]) -> list[str]:
    problems = []
    for bullet in bullets:
        unit = bullet if bullet in units else None
        if unit is None:
            problems.append(f"no registry unit for the bullet: {bullet[:60]}")
            continue
        targets = units[unit]
        if bullet.startswith(POINTER):
            continue
        if all(t.startswith("exempt:") for t in targets):
            problems.append(f"a capability line is exempt: {bullet[:60]}")
    return problems


def test_only_the_frame_and_the_pointer_are_exempt():
    bullets = _bullets()
    assert len(bullets) == 50, "the page's bullets moved: this count is the contract"
    assert exemption_problems(bullets, _units()) == []


def test_the_pin_fires_on_an_exempt_capability_line():
    """The probe: the first bullet marked exempt is refused."""
    bullets = _bullets()
    units = dict(_units())
    units[bullets[0]] = ["exempt:navigation"]
    assert exemption_problems(bullets, units) == [
        f"a capability line is exempt: {bullets[0][:60]}"
    ]


def test_every_github_nav_link_names_a_file_in_this_tree():
    links = BLOB.findall(MKDOCS.read_text(encoding="utf-8"))
    assert links, "the nav links no file on GitHub: the scan is broken, not clean"
    missing = [p for p in links if not (REPO_ROOT / p).is_file()]
    assert missing == []


def test_every_github_link_on_the_page_names_a_file_in_this_tree():
    links = BLOB.findall(PAGE.read_text(encoding="utf-8"))
    assert links, "the page links no file on GitHub: the scan is broken, not clean"
    missing = [p for p in links if not (REPO_ROOT / p).is_file()]
    assert missing == []
