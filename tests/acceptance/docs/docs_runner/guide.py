"""A guide's Markdown as the loader needs it: its text, its headings and their anchors.

The anchors are the ids MkDocs gives the headings (mkdocs.yml enables ``toc``
and ``attr_list``):

* an explicit ``{#id}`` (or ``{: #id }``) at the end of a heading is its id;
* otherwise Python-Markdown's ``toc`` slug of the heading's text: inline
  Markdown and HTML removed, accents folded to ASCII, anything but word
  characters, spaces and hyphens dropped, lower-cased, runs of spaces and
  hyphens made one hyphen;
* a slug already taken (by an explicit id or an earlier heading) gets ``_1``,
  ``_2``, ... in page order.

A ``#`` line inside a fenced code block is not a heading (a shell comment is the
usual one), and an ``id="..."`` on raw HTML in the page is an anchor too.

A dashboard component (a ``.tsx`` guide, such as the first-run card) has no
headings: its anchors are its ``data-testid`` values, the names its own tests
find its parts by, and its text is the file's.

Measured on 2026-10-06 against ``mkdocs build`` (mkdocs 1.6.1, Markdown 3.11):
the anchors computed here for the 121 pages in the nav equal the ids of all
2,334 headings of the built site.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple

FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
ATTR_LIST = re.compile(r"[ \t]+\{:?([^}\n]*)\}[ \t]*$")
ATTR_ID = re.compile(r"(?:^|\s)#([^\s}]+)")
HTML_ID = re.compile(r"""<[A-Za-z][^>]*?\sid=["']([^"']+)["']""")
#: A component's anchors: its data-testid values, written as string literals.
TEST_ID = re.compile(r"""\bdata-testid=["']([A-Za-z0-9_-]{1,80})["']""")
#: A shell block Doc Examples runs: a fence whose info string is ``{.bash ... exec ...}``.
EXEC_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*\{\.bash[^}]*\bexec\b")


CODE_SPAN = re.compile(r"(`+)(.+?)\1")


def _plain_outside_code(text: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links, images
    text = re.sub(r"<[^>]+>", "", text)  # inline HTML
    text = re.sub(r"\*{1,3}(\S(?:.*?\S)?)\*{1,3}", r"\1", text)  # *emphasis*
    # _emphasis_ only at word edges: an underscore inside a word is a letter.
    text = re.sub(r"(?<!\w)_{1,3}(\S(?:.*?\S)?)_{1,3}(?!\w)", r"\1", text)
    text = re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", text)  # backslash escapes
    return html.unescape(text)


def _plain(text: str) -> str:
    """The heading's text as rendered, without its Markdown and HTML.

    A code span's content is kept as written (``Task<Result>`` is text there,
    not a tag); outside code spans, links keep their text, and inline HTML,
    emphasis and backslash escapes are removed.
    """
    parts: List[str] = []
    last = 0
    for match in CODE_SPAN.finditer(text):
        parts.append(_plain_outside_code(text[last : match.start()]))
        parts.append(match.group(2).strip())
        last = match.end()
    parts.append(_plain_outside_code(text[last:]))
    return "".join(parts).strip()


def slugify(text: str) -> str:
    """Python-Markdown ``toc``'s default slug of a heading's plain text."""
    value = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)


def normalise_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    anchor: str
    line: int


@dataclass
class Guide:
    """One page under ``docs/``."""

    path: str
    text: str
    headings: List[Heading] = field(default_factory=list)
    html_ids: List[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        for heading in self.headings:
            if heading.level == 1:
                return heading.text
        return self.path

    @property
    def anchors(self) -> Dict[str, str]:
        """Every anchor of the page, mapped to the heading it names (or "")."""
        found = {heading.anchor: heading.text for heading in self.headings}
        for html_id in self.html_ids:
            found.setdefault(html_id, "")
        return found

    def contains(self, phrase: str) -> bool:
        """True when the page's text says *phrase*, ignoring line breaks."""
        return normalise_space(phrase) in normalise_space(self.text)

    def section(self, anchor: str) -> str:
        """The lines under the heading *anchor*, up to the next heading of its level or above."""
        lines = self.text.splitlines()
        for index, heading in enumerate(self.headings):
            if heading.anchor != anchor:
                continue
            end = len(lines)
            for later in self.headings[index + 1 :]:
                if later.level <= heading.level:
                    end = later.line
                    break
            return "\n".join(lines[heading.line : end])
        return ""

    def has_exec_block(self, anchor: str) -> bool:
        return any(EXEC_FENCE.match(line) for line in self.section(anchor).splitlines())


IDCOUNT = re.compile(r"^(.*)_([0-9]+)$")


def _unique(anchor: str, used: Set[str]) -> str:
    """Python-Markdown ``toc``'s ``unique``: ``x``, then ``x_1``, ``x_2``, ..."""
    while anchor in used or not anchor:
        match = IDCOUNT.match(anchor)
        if match:
            anchor = f"{match.group(1)}_{int(match.group(2)) + 1}"
        else:
            anchor = f"{anchor}_1"
    used.add(anchor)
    return anchor


def parse(path: str, text: str) -> Guide:
    """The headings, anchors and raw-HTML ids of a page's Markdown *text*, or a
    component's ``data-testid`` values."""
    guide = Guide(path=path, text=text)
    if path.endswith(".tsx"):
        guide.html_ids.extend(TEST_ID.findall(text))
        return guide
    found: List[Tuple[int, str, str, int]] = []
    fence: Tuple[str, int] = ("", 0)
    for number, line in enumerate(text.splitlines()):
        opened = FENCE.match(line)
        if fence[0]:
            closes = (
                opened is not None
                and opened.group(1)[0] == fence[0]
                and len(opened.group(1)) >= fence[1]
                and not line.strip()[len(opened.group(1)) :].strip()
            )
            if closes:
                fence = ("", 0)
            continue
        if opened:
            fence = (opened.group(1)[0], len(opened.group(1)))
            continue
        guide.html_ids.extend(HTML_ID.findall(line))
        match = HEADING.match(line)
        if not match:
            continue
        raw = match.group(2)
        explicit = ""
        attrs = ATTR_LIST.search(raw)
        if attrs:
            raw = raw[: attrs.start()]
            id_match = ATTR_ID.search(attrs.group(1))
            if id_match:
                explicit = id_match.group(1)
        found.append((len(match.group(1)), _plain(raw), explicit, number))
    # As toc does: the explicit ids are taken first, then each other heading
    # gets its slug, made unique against everything taken so far.
    used: Set[str] = {explicit for _, _, explicit, _ in found if explicit}
    for level, plain, explicit, number in found:
        anchor = explicit or _unique(slugify(plain), used)
        guide.headings.append(Heading(level, plain, anchor, number))
    return guide


def load(docs_root: Path, path: str) -> Guide:
    return parse(path, (docs_root / path).read_text(encoding="utf-8"))
