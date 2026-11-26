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
* the table of what the script reads about visitors is, row for row,
  ``tests/sdk-contract/visitor-attributes.json`` (the vocabulary the script's
  and the server's tests hold to), and the words around it name that file's
  values, its visit length, its data-collect values and the server's limits;
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
VOCABULARY = REPO_ROOT / "tests" / "sdk-contract" / "visitor-attributes.json"
VECTORS = REPO_ROOT / "tests" / "sdk-contract" / "visual-visitor-vectors.json"
DETAILS_HEADER = (
    "| Detail | Where it comes from | Example | Sent to Controlvar | Stored and where |"
)
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


def _either(values: List[str]) -> str:
    return ", ".join(values[:-1]) + " or " + values[-1]


def _vocabulary() -> Dict:
    return json.loads(VOCABULARY.read_text(encoding="utf-8"))


def _limits() -> Dict[str, int]:
    return json.loads(VECTORS.read_text(encoding="utf-8"))["limits"]


def test_the_details_table_is_the_vocabulary():
    rows = [
        [cell.replace("`", "") for cell in row]
        for row in _table_rows(PAGE.read_text(encoding="utf-8"), DETAILS_HEADER)
    ]
    details = _vocabulary()["details"]
    assert len(details) == 9
    assert rows == [
        [
            f"{d['label']} ({d['key']})",
            d["source"],
            d["example"],
            d["sent"],
            d["stored"],
        ]
        for d in details
    ]


def test_the_details_words_name_the_vocabulary_and_the_limits():
    text = PAGE.read_text(encoding="utf-8")
    vocabulary = _vocabulary()
    closed = {
        d["key"]: [v["value"] for v in d["values"]]
        for d in vocabulary["details"]
        if d["values"]
    }
    tokens = [f"`{t}`" for t in vocabulary["collect_tokens"] if t != "none"]
    limits = _limits()
    for words in (
        f"The device type is {_either(closed['web_device'])}; the browser is "
        f"{_either(closed['web_browser'])}; new or returning is "
        f"{_either(closed['web_visitor'])}.",
        f"A visit ends after {vocabulary['visit_minutes']} minutes without a page view",
        f"1 to {limits['utm']} of the letters",
        f"a path of at most {limits['landing_path']} characters",
        "`data-collect` names the details to send: "
        + ", ".join(tokens[:-1])
        + f" and {tokens[-1]}, separated by commas.",
    ):
        assert words in text, words


def test_the_limitations_name_what_the_website_details_leave_out():
    text = LIMITATIONS.read_text(encoding="utf-8")
    before, after = text.split(LIMITATIONS_HEADING, 1)
    section = after.split("\n## ", 1)[0]
    limits = _limits()
    for words in (
        "An iPad that reports a Mac",
        "not on real iPads",
        "after 7 days without a visit",
        "from the first page of a visit only",
        f"or over {limits['utm']} characters, is not recorded",
        f"over {limits['landing_path']} characters, or with an @, is not recorded",
        "keeps their variant on every device",
    ):
        assert words in section, words
        assert words not in before, words


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
