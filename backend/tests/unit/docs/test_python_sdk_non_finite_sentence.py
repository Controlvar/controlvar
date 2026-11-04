"""The Python SDK's page and its README say the same about NaN and Infinity (#1630).

``docs/sdk/python.md`` and ``sdk/python/README.md`` (the package's PyPI page)
each carry one paragraph saying what ``track``, ``get_variant`` and
``get_assignment`` do with NaN or an infinite number. This test holds the two
to each other, so neither can be edited alone:

* each page carries the paragraph exactly once, in its tracking section (the
  "Tracking fan-out rule" heading, up to the next heading);
* the two paragraphs are the same text once line breaks are folded.

What the SDK does is held by ``sdk/python/tests/test_client.py``
(``TestNaNAndInfinityAreNotSent``), the target the claims registry names for
the sentence. Reads only files; no git, so it runs the same in
``scripts/core_build.sh``'s copy. It is in the docs-only gate's "Docs content
tests" through its directory, ``backend/tests/unit/docs/``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "sdk" / "python.md"
README = REPO_ROOT / "sdk" / "python" / "README.md"

#: How the paragraph starts, after its line breaks are folded.
OPENING = "From the next published version, `track` returns `False`, and sends nothing,"
#: The heading of the section the paragraph is in, on both pages.
SECTION = "Tracking fan-out rule"


def paragraphs_of_section(path: Path) -> list[str]:
    """The paragraphs of the section headed ``SECTION``, each with its whitespace folded."""
    text = path.read_text(encoding="utf-8")
    heading = re.search(rf"^#{{2,3}} {re.escape(SECTION)}\n", text, re.M)
    assert heading, f"{path.relative_to(REPO_ROOT)} has no {SECTION!r} heading"
    rest = text[heading.end() :]
    following = re.search(r"^#{1,6} ", rest, re.M)
    section = rest if following is None else rest[: following.start()]
    return [
        " ".join(block.split())
        for block in re.split(r"\n[ \t]*\n", section)
        if block.strip()
    ]


def the_paragraph(path: Path) -> str:
    found = [p for p in paragraphs_of_section(path) if p.startswith(OPENING)]
    assert len(found) == 1, (
        f"{path.relative_to(REPO_ROOT)}: {len(found)} paragraphs in its {SECTION!r} "
        f"section start {OPENING!r}, not one"
    )
    whole = " ".join(path.read_text(encoding="utf-8").split())
    assert whole.count(OPENING) == 1, (
        f"{path.relative_to(REPO_ROOT)} says it more than once"
    )
    return found[0]


@pytest.mark.parametrize(
    "path", [PAGE, README], ids=["docs/sdk/python.md", "sdk/python/README.md"]
)
def test_each_page_says_it_once_in_its_tracking_section(path):
    assert the_paragraph(path)


def test_the_page_and_the_readme_say_the_same():
    assert the_paragraph(PAGE) == the_paragraph(README)
