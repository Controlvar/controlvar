"""The live demo's page, its place in the nav, and the address and time it names.

``docs/getting-started/live-demo.md`` says what the public read-only demo is.
It is listed in the nav right after Quick Start: a page left out of the nav is
still built, so the strict docs build would not notice it missing.

The homepage's "Try the live demo" button (``DEMO_URL`` in
``frontend/src/content/site-links.ts``), the README's "Try the live demo"
section and the page give one address, the host the demo box serves
(``deploy/demo/generate-env.sh``'s default public URL), and the README and the
page give one reset time, the box's reset timer
(``deploy/demo/systemd/controlvar-demo-reset.timer``). ``test_demo_box.py``
holds the dashboard's banner and the box's rebuild page to the same timer.

These read files as text; no Docker, no git, no network.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
MKDOCS = REPO_ROOT / "mkdocs.yml"
PAGE_PATH = "getting-started/live-demo.md"
PAGE = REPO_ROOT / "docs" / PAGE_PATH
README = REPO_ROOT / "README.md"
SITE_LINKS = REPO_ROOT / "frontend" / "src" / "content" / "site-links.ts"
DEMO = REPO_ROOT / "deploy" / "demo"

BLOB = re.compile(r"https://github\.com/Controlvar/controlvar/blob/main/([^\s)\"'>]+)")
#: "every night at HH:MM UTC", the reset time as the README and the page say it.
RESET_SENTENCE = re.compile(r"every\s+night\s+at\s+(\d\d:\d\d) UTC")
#: Any https address on a host under controlvar.com's demo name.
DEMO_ADDRESS = re.compile(r"https://(?:[a-z0-9-]+\.)*demo\.controlvar\.com\b")


def _getting_started() -> List[Dict[str, Any]]:
    nav = yaml.safe_load(MKDOCS.read_text(encoding="utf-8"))["nav"]
    for item in nav:
        if isinstance(item, dict) and "Getting Started" in item:
            return item["Getting Started"]
    raise AssertionError("mkdocs.yml's nav has no Getting Started section")


def _readme_section() -> str:
    """The README's "Try the live demo" section, up to the next heading."""
    text = README.read_text(encoding="utf-8")
    parts = text.split("\n## Try the live demo\n", 1)
    assert len(parts) == 2, "README.md has no '## Try the live demo' section"
    return re.split(r"\n#{1,6} ", parts[1], maxsplit=1)[0]


def _box_host() -> str:
    script = (DEMO / "generate-env.sh").read_text(encoding="utf-8")
    match = re.search(
        r"public_base_url=\$\{PUBLIC_BASE_URL:-https://([a-z0-9.-]+)\}", script
    )
    assert match, "deploy/demo/generate-env.sh has no default PUBLIC_BASE_URL"
    return match.group(1)


def _timer_reset_time() -> str:
    timer = (DEMO / "systemd" / "controlvar-demo-reset.timer").read_text(
        encoding="utf-8"
    )
    match = re.search(r"^OnCalendar=\*-\*-\* (\d\d:\d\d):00 UTC$", timer, re.M)
    assert match, "the reset timer's OnCalendar is not '*-*-* HH:MM:00 UTC'"
    return match.group(1)


def test_the_page_is_in_the_nav_right_after_quick_start():
    entries = _getting_started()
    labels = [next(iter(entry)) for entry in entries]
    targets = [next(iter(entry.values())) for entry in entries]
    assert PAGE.is_file()
    assert targets.count(PAGE_PATH) == 1, "the live demo page is not in the nav once"
    index = targets.index(PAGE_PATH)
    assert labels[index] == "Live Demo"
    assert targets[index - 1] == "getting-started/quick-start.md", labels


def test_the_page_heading_is_the_nav_label():
    assert PAGE.read_text(encoding="utf-8").split("\n", 1)[0] == "# Live Demo"


def test_the_site_the_readme_and_the_page_give_the_address_the_box_serves():
    url = f"https://{_box_host()}"
    site = re.search(
        r"^export const DEMO_URL = '([^']+)';$",
        SITE_LINKS.read_text(encoding="utf-8"),
        re.M,
    )
    assert site, "frontend/src/content/site-links.ts has no DEMO_URL"
    assert site.group(1) == url
    readme = _readme_section()
    page = PAGE.read_text(encoding="utf-8")
    assert url in readme
    assert url in page
    # Every demo address they give is the box's host or one of its two storefronts.
    allowed = {
        url,
        f"https://shoplab.{_box_host()}",
        f"https://streampulse.{_box_host()}",
    }
    assert set(DEMO_ADDRESS.findall(readme + page)) <= allowed


def test_the_readme_and_the_page_give_the_box_reset_time():
    reset = _timer_reset_time()
    for name, text in (
        ("README.md", _readme_section()),
        (PAGE_PATH, PAGE.read_text(encoding="utf-8")),
    ):
        times = RESET_SENTENCE.findall(text)
        assert times, f"{name} does not say when the demo is rebuilt"
        assert set(times) == {reset}, name


def test_the_readme_section_links_the_page():
    assert f"](docs/{PAGE_PATH})" in _readme_section()


def test_every_github_link_on_the_page_names_a_file_in_this_tree():
    links = BLOB.findall(PAGE.read_text(encoding="utf-8"))
    assert "SECURITY.md" in links, "the page does not send reports to SECURITY.md"
    assert [p for p in links if not (REPO_ROOT / p).is_file()] == []
