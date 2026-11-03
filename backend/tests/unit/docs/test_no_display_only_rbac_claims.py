"""The sentences that say custom roles and direct grants change nothing (#891).

While custom roles and direct permission grants did not change what a user
could do, the docs, the dashboard and their tests said so: "no permission check
reads them", "they do not change what anyone can do yet", "the base role
decides". They take effect now (#891), so every such sentence is false:
``EXPECTED``, the exact set of those sentences in the tree, is empty, and one
that comes back fails ``test_the_display_only_sentences_are_exactly_these``
and names it.

How a sentence is found:

* every file under the repository root with one of ``SUFFIXES``, walked from
  this file's own path with no git (``scripts/core_build.sh``'s copy has no
  ``.git``), skipping ``SKIP_DIRS``, ``CHANGELOG.md`` (release history quotes
  each release as it was) and this file;
* its whitespace collapsed to single spaces, so a sentence wrapped over two
  lines is found (a line-by-line scan misses ``EditUserModal.tsx``'s);
* each match of a phrase in ``PHRASES`` (case ignored), reported with the text
  around it up to the nearest ``BOUNDARY`` either side (a sentence end, a
  quote, a table cell, a fence or a comment marker), at most ``WINDOW``
  characters away.

This file holds the tree outside ``modules/``, which both profiles ship; a
core file may not name a module's path (``test_core_boundary.py``), so the
sentences under ``modules/`` are held by its twin,
``test_no_display_only_rbac_claims_modules.py`` in the modules' unit tests,
with this scan.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path
from typing import Dict, Iterator, Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
THIS = Path(__file__).resolve()

#: (anchor, phrase): a file is searched only if its lower-cased text holds an
#: anchor, and each phrase holds its own anchor, so none is skipped.
PHRASES: Tuple[Tuple[str, str], ...] = (
    ("permission check", r"no permission check (?:reads|uses)"),
    (
        "change what",
        r"(?:do|does) not change what (?:anyone|a user|the user|the analyst) can do",
    ),
    ("nothing", r"changes? nothing (?:a|the) (?:user|analyst) can do"),
    ("base role", r"only the base role decides"),
    ("base role", r"routes check (?:only )?the base role"),
    ("base role", r"reads only the base role"),
    ("base role", r"decided by (?:their|the) base role"),
    ("base role", r"which their base role decides"),
    ("built-in role", r"built-in role decides"),
    ("change access", r"(?:do|does) not change access"),
    ("not applied yet", r"(?:is|are) stored but not applied yet"),
    ("not applied yet", r"custom roles are not applied yet"),
    ("decide nothing", r"stored and shown, and decide nothing"),
    ("take effect", r"make them take effect"),
    ("any route", r"is not called by any route"),
    ("a display", r"this is a display"),
    ("is unchanged", r"what the analyst can do is unchanged"),
    ("only what", r"can still do only what"),
)
_PATTERN = re.compile("|".join(p for _, p in PHRASES), re.IGNORECASE)
#: Each phrase on its own, searched only when its anchor is in the text: one
#: alternation over every file costs seconds, this a fraction of one.
_SEARCHES = tuple((a, re.compile(p, re.IGNORECASE)) for a, p in PHRASES)

SUFFIXES = frozenset(
    {".md", ".py", ".ts", ".tsx", ".js", ".jsx", ".toml", ".yml", ".yaml", ".json"}
    | {".txt", ".html"}
)
SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".next",
        "out",
        "build",
        "dist",
        "site",
        "coverage",
        "htmlcov",
        "__pycache__",
        "venv",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)
EXCLUDED = frozenset({"CHANGELOG.md"})
BOUNDARY = re.compile(
    r"[.!?](?=\s|$)|\"|(?<![A-Za-z])'|'(?![A-Za-z])|\||```|/\*|\*/|<!--|-->"
    r"|(?:^|(?<=\s))//"
)
WINDOW = 300

Hit = Tuple[str, str]


#: The top-level directory the core build deletes.
MODULES = "modules"


def _files(root: Path, modules: bool, skip: Path) -> Iterator[Tuple[str, Path]]:
    """Files under *root* outside ``modules/``, or (*modules*) only inside it;
    never *skip*, the test that lists the sentences (it quotes them)."""
    # os.walk, so that a skipped directory (a node_modules) is never entered.
    for top, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        if Path(top) == root:
            dirs[:] = [d for d in dirs if (d == MODULES) == modules]
            if modules:
                names = []
        for name in sorted(names):
            path = Path(top) / name
            rel = path.relative_to(root).as_posix()
            if rel in EXCLUDED or path.resolve() == skip:
                continue
            if path.suffix in SUFFIXES and path.is_file():
                yield rel, path


def _sentence(text: str, start: int, end: int) -> str:
    """The text around [start, end) up to the nearest boundary each side."""
    left = max(0, start - WINDOW)
    before = list(BOUNDARY.finditer(text, left, start))
    if before:
        left = before[-1].end()
    right = min(len(text), end + WINDOW)
    after = BOUNDARY.search(text, end, right)
    if after:
        right = after.end()
    return text[left:right].strip()


def find(text: str) -> Counter:
    """Each display-only sentence in *text*, with how many phrases match it."""
    text = " ".join(text.split())
    found: Counter = Counter()
    lower = text.lower()
    for anchor, phrase in _SEARCHES:
        if anchor in lower:
            for match in phrase.finditer(text):
                found[_sentence(text, match.start(), match.end())] += 1
    return found


def scan(root: Path, *, modules: bool = False, skip: Path = THIS) -> Dict[Hit, int]:
    """Every (file, sentence) under *root* outside ``modules/`` (or, with
    *modules*, inside it), with its count; *skip* is left out."""
    hits: Counter = Counter()
    for rel, path in _files(root, modules, skip.resolve()):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for sentence, n in find(text).items():
            hits[(rel, sentence)] += n
    return dict(hits)


#: The sentences in the tree now: none. Custom roles and direct grants take
#: effect (#891), so a sentence saying they change nothing is false; a new
#: one fails here.
EXPECTED: Dict[Hit, int] = {}


def differences(found: Dict[Hit, int], expected: Dict[Hit, int]) -> str:
    """What is wrong with *found* against *expected*; empty if nothing."""
    gone = sorted(set(expected) - set(found))
    new = sorted(set(found) - set(expected))
    moved = sorted(k for k in set(found) & set(expected) if found[k] != expected[k])
    if not (gone or new or moved):
        return ""
    return (
        f"no longer found: {gone}\nnot listed: {new}\n"
        f"count changed: {[(k, expected[k], found[k]) for k in moved]}"
    )


def test_the_display_only_sentences_are_exactly_these():
    assert not differences(scan(REPO), EXPECTED), differences(scan(REPO), EXPECTED)


def test_a_wrapped_sentence_is_found_and_excluded_files_are_not(tmp_path):
    """The positive control, on a tree made here: a sentence wrapped over two
    lines is found, as a line-by-line scan would not; CHANGELOG.md and
    node_modules are skipped; a file with no phrase yields nothing; and the
    modules' tree is scanned only when asked for."""
    wrapped = (
        "  'Custom roles and direct permission grants (full edition) are\n"
        "  recorded, but they do not change\n"
        "  what anyone can do yet: the role set here decides.';\n"
    )
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "Modal.tsx").write_text(f"const NOTE =\n{wrapped}")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "page.md").write_text(
        "# A page.\n\nGranting a direct permission is not recorded because no\n"
        "permission check reads direct grants yet.\n\nNothing else here.\n"
    )
    (tmp_path / "docs" / "other.md").write_text("Roles decide what a user may do.\n")
    (tmp_path / "CHANGELOG.md").write_text("no permission check reads them yet.\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("// no permission check reads\n")
    (tmp_path / MODULES / "sub").mkdir(parents=True)
    (tmp_path / MODULES / "sub" / "Roles.tsx").write_text(
        "const NOTE = 'They do not change access yet.';\n"
    )

    assert scan(tmp_path, modules=True) == {
        (f"{MODULES}/sub/Roles.tsx", "They do not change access yet.'"): 1
    }
    assert scan(tmp_path) == {
        (
            "frontend/Modal.tsx",
            "Custom roles and direct permission grants (full edition) are "
            "recorded, but they do not change what anyone can do yet: the role "
            "set here decides.'",
        ): 1,
        (
            "docs/page.md",
            "Granting a direct permission is not recorded because no permission "
            "check reads direct grants yet.",
        ): 1,
    }
    # Line by line, neither sentence is found: each is wrapped.
    for line in (wrapped + "no\npermission check reads direct grants").splitlines():
        assert not _PATTERN.search(line), line


def test_two_phrases_in_one_sentence_count_twice():
    assert find(
        "The role is stored and shown; it does not change what the user can do, "
        "which their base role decides (#891). Next sentence."
    ) == {
        "The role is stored and shown; it does not change what the user can do, "
        "which their base role decides (#891).": 2
    }


def test_every_phrase_is_searched_through_its_anchor():
    for anchor, phrase in PHRASES:
        assert anchor in phrase, (anchor, phrase)
