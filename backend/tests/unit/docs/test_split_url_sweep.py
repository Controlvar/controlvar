"""No page still says split URL testing cannot split (#393).

The claims registries hold a registered page's sentences to their tests, but a
sentence that is left unchanged stays registered, and a page or a code comment
outside the registries is held by nothing. So this sweeps the tree for the
sentences that #393 made false: that split URL experiments split no traffic,
that the preview hashes on its own and predicts nothing, that they ignore the
global holdout (the assign API); and that the CloudFront edge router reads
its configuration from ``X-Split-URL-Config``, which nothing sets, hashes the
viewer's IP address and User-Agent, passes every request through, is used by
no stack, and does not split traffic yet (the router).

* Every text file of the tree (Markdown, Python, TypeScript, TOML, YAML, JSON
  and plain text) except ``CHANGELOG.md`` (history), the claims registries
  and ledger under ``docs/_claims/`` (they quote what the pages say), and this
  file; ``node_modules``, ``venv``, ``site`` and dot-directories other than
  ``.github`` are not walked. No git: ``scripts/core_build.sh`` runs this in a
  copy without ``.git``.
* Whitespace is normalised and comment markers (``#``, ``//``, ``*``, ``>``,
  ``<!--``, ``-->``) stripped first, so a sentence wrapped across lines or
  split across comment lines is still found.
* A hit counts only in split URL context: a paragraph that names split URL
  testing, or a file whose path does.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, List, NamedTuple, Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
SELF = Path(__file__).resolve()

#: Lower case. What #393's assign API made false, then what its CloudFront
#: edge router made false.
PHRASES: Tuple[str, ...] = (
    "does not split traffic in this release",
    "splits no traffic",
    "no configuration source",
    "does not predict",
    "meant to redirect",
    "preview's own hash",
    "a user id hashes to",
    "hashes the user_id you pass",
    "split-url experiments ignore",
    "split url experiments ignore",
    "x-split-url-config",
    "ip address and user-agent",
    "ip + user-agent",
    "passes every request through",
    "no stack uses it",
    "does not split traffic yet",
)

SUFFIXES = {".md", ".py", ".ts", ".tsx", ".toml", ".yml", ".yaml", ".json", ".txt"}
PRUNED = {"node_modules", "venv", "site"}
KEPT_DOT_DIRS = {".github"}
EXCLUDED = {"CHANGELOG.md"}
EXCLUDED_DIRS = ("docs/_claims/",)

_CONTEXT = re.compile(r"split[ _-]url", re.IGNORECASE)
_MARKERS = re.compile(r"^\s*(?:#+|//+|\*+|>+|<!--)\s?|\s*-->\s*$")


def _files() -> Iterator[Path]:
    stack = [REPO_ROOT]
    while stack:
        directory = stack.pop()
        for entry in sorted(directory.iterdir()):
            if entry.is_dir():
                name = entry.name
                if name in PRUNED or (
                    name.startswith(".") and name not in KEPT_DOT_DIRS
                ):
                    continue
                stack.append(entry)
            elif entry.suffix in SUFFIXES and entry.resolve() != SELF:
                yield entry


class _Paragraph(NamedTuple):
    """A blank-line paragraph: its text, normalised and lower case, and where
    each of its lines starts in that text."""

    text: str
    lines: List[Tuple[int, int]]  # (offset in text, line number)

    def line_of(self, phrase: str) -> int:
        """The line the first occurrence of *phrase* starts on."""
        at = self.text.index(phrase)
        return max(number for offset, number in self.lines if offset <= at)


def _paragraphs(text: str) -> Iterator[_Paragraph]:
    """Each blank-line paragraph, comment markers stripped."""
    parts: List[str] = []
    lines: List[Tuple[int, int]] = []
    length = 0
    for number, line in enumerate(text.splitlines() + [""], 1):
        words = " ".join(_MARKERS.sub("", line).split()).lower()
        if line.strip():
            if words:
                offset = length + (1 if parts else 0)
                lines.append((offset, number))
                parts.append(words)
                length = offset + len(words)
        elif parts:
            yield _Paragraph(" ".join(parts), lines)
            parts, lines, length = [], [], 0


def stale_sentences(root: Path = REPO_ROOT) -> List[str]:
    """``path:line: phrase`` for every swept phrase still in split URL context."""
    hits = []
    for path in _files():
        rel = path.relative_to(root).as_posix()
        if rel in EXCLUDED or rel.startswith(EXCLUDED_DIRS):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        file_context = bool(_CONTEXT.search(rel))
        for paragraph in _paragraphs(text):
            if not (file_context or _CONTEXT.search(paragraph.text)):
                continue
            hits.extend(
                f"{rel}:{paragraph.line_of(p)}: {p}"
                for p in PHRASES
                if p in paragraph.text
            )
    return hits


@pytest.mark.regression
def test_no_page_says_split_url_testing_cannot_split():
    assert stale_sentences() == []


def test_the_sweep_finds_a_wrapped_sentence_in_a_comment():
    """The sweep's own probe: a phrase wrapped across two comment lines, in a
    paragraph that names split URL testing, is found."""
    text = "intro\n\n# The split URL router has no\n# configuration source in this release.\n"
    found = [
        (paragraph.line_of(p), p)
        for paragraph in _paragraphs(text)
        for p in PHRASES
        if p in paragraph.text
    ]
    assert found == [(3, "no configuration source")]
