"""The visual script's install guide against the script's own files (#1545).

``docs/sdk/visual.md`` documents the browser script in ``sdk/visual``. These
tests hold the parts of the page that copy something the script defines:

* the tag in its ``html`` block is ``sdk/visual/snippet.html``, the file the
  browser tests load under a strict Content-Security-Policy, and no example in
  either is an email address (an id that identifies a person);
* the policy in its ``text`` block stays strict: ``script-src`` has no
  ``'unsafe-inline'``, ``'unsafe-eval'``, ``*`` or ``data:``;
* the table of what the script stores is, row for row, the keys
  ``tests/sdk-contract/visual-storage-keys.json`` marks written (the file the
  script's own key list is pinned to), with that file's words;
* while the visual editor is not yet available, the page says so and is not
  in the navigation, the limitations page's lines about the script sit under a
  heading that says so, and nothing in the dashboard loads the script;
* no page and no dashboard source says no IP address is stored anywhere.

Reads files only (no git), so it runs the same in core_build's copy.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "sdk" / "visual.md"
LIMITATIONS = REPO_ROOT / "docs" / "getting-started" / "limitations.md"
SNIPPET = REPO_ROOT / "sdk" / "visual" / "snippet.html"
KEYS = REPO_ROOT / "tests" / "sdk-contract" / "visual-storage-keys.json"
MKDOCS = REPO_ROOT / "mkdocs.yml"
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"

NOT_YET = "The visual editor is not yet available"
LIMITATIONS_HEADING = "## Visual experiments (not yet available)"
FORBIDDEN_SOURCES = ("'unsafe-inline'", "'unsafe-eval'", "*", "data:")
NO_IP_ANYWHERE = re.compile(
    r"(?i)\bno\s+IP\s+address(?:es)?\s+(?:is|are)\s+stored\s+anywhere\b"
)


def _fences(text: str, language: str) -> List[str]:
    return re.findall(r"^```" + language + r"\n(.*?)^```$", text, flags=re.M | re.S)


def _table_rows(text: str, header: str) -> List[List[str]]:
    lines = text.splitlines()
    start = lines.index(header)
    rows = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip().strip("`") for cell in line.strip("|").split("|")])
    return rows


def policy_problems(policy: str) -> List[str]:
    """Each source the documented script-src must not allow."""
    directives: Dict[str, List[str]] = {}
    for part in policy.split(";"):
        words = part.split()
        if words:
            directives[words[0]] = words[1:]
    if "script-src" not in directives:
        return ["no script-src"]
    return [
        f"forbidden source {source} in script-src"
        for source in directives["script-src"]
        if source in FORBIDDEN_SOURCES
    ]


def email_examples(text: str) -> List[str]:
    return re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text)


def test_the_tag_on_the_page_is_the_snippet_file():
    blocks = _fences(PAGE.read_text(encoding="utf-8"), "html")
    assert blocks == [SNIPPET.read_text(encoding="utf-8")], (
        "the page's html block is not sdk/visual/snippet.html"
    )


def test_no_example_tag_holds_an_email_address():
    for text in (SNIPPET.read_text(encoding="utf-8"), PAGE.read_text(encoding="utf-8")):
        assert email_examples(text) == [], "snippet example is an email"


def test_the_documented_policy_stays_strict():
    blocks = _fences(PAGE.read_text(encoding="utf-8"), "text")
    assert len(blocks) == 1, "the page has one policy block"
    assert policy_problems(blocks[0]) == []


@pytest.mark.parametrize("source", FORBIDDEN_SOURCES)
def test_a_loose_policy_is_refused(source):
    assert policy_problems(f"script-src 'self' {source}; connect-src 'self'") == [
        f"forbidden source {source} in script-src"
    ]


def test_the_stored_keys_table_is_the_contract_files_written_keys():
    rows = _table_rows(
        PAGE.read_text(encoding="utf-8"), "| Key | Where | What it holds | How long |"
    )
    written = [
        [key["name"], key["storage"], key["holds"], key["kept"]]
        for key in json.loads(KEYS.read_text(encoding="utf-8"))["keys"]
        if key["written"]
    ]
    assert len(written) == 8
    assert rows == written


def _dashboard_files_naming_the_script() -> List[str]:
    return [
        str(path.relative_to(REPO_ROOT))
        for path in sorted(FRONTEND_SRC.rglob("*"))
        if path.is_file()
        and "node_modules" not in path.parts
        and "controlvar-visual" in path.read_text(encoding="utf-8", errors="replace")
    ]


def test_the_page_says_not_yet_available_nothing_offers_the_script_and_no_nav_lists_it():
    assert NOT_YET in PAGE.read_text(encoding="utf-8")
    assert "sdk/visual.md" not in MKDOCS.read_text(encoding="utf-8")
    assert _dashboard_files_naming_the_script() == []


def test_the_limitations_lines_sit_under_a_heading_that_says_not_yet_available():
    text = LIMITATIONS.read_text(encoding="utf-8")
    assert text.count(LIMITATIONS_HEADING) == 1
    section = text.split(LIMITATIONS_HEADING, 1)[1].split("\n## ", 1)[0]
    assert NOT_YET in section
    for words in ("hydration error", "keeps updating", "hash routing"):
        assert words in section, words
        assert words not in text.split(LIMITATIONS_HEADING, 1)[0], words


def test_no_page_or_dashboard_source_says_no_ip_address_is_stored_anywhere():
    assert NO_IP_ANYWHERE.search("No IP address is stored anywhere.")
    found = []
    for root in (REPO_ROOT / "docs", FRONTEND_SRC):
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in {".md", ".ts", ".tsx", ".json"}:
                if NO_IP_ANYWHERE.search(
                    path.read_text(encoding="utf-8", errors="replace")
                ):
                    found.append(str(path.relative_to(REPO_ROOT)))
    assert found == [], "forbidden claim 1 != 0"
