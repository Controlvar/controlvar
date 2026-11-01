"""The sentences that say custom roles and direct grants change nothing (#891).

While custom roles and direct permission grants do not change what a user can
do, the docs, the dashboard and their tests say so: "no permission check reads
them", "they do not change what anyone can do yet", "the base role decides".
``EXPECTED`` is the exact set of those sentences in the tree, with how many
times each phrase matches it. A sentence removed or reworded early, or a new
one added, fails ``test_the_display_only_sentences_are_exactly_these`` and
names it. The intent (#891) is that when roles and grants take effect every
one of them goes, and the set is empty.

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


#: The sentences in the tree now. Not one of them may move without this.
EXPECTED: Dict[Hit, int] = {
    (
        "README.md",
        "Both are recorded, but they do not change what anyone can do yet: each user's built-in role decides ([#891](https://github.com/Controlvar/controlvar/issues/891)) |",
    ): 2,
    (
        "backend/tests/unit/docs/test_docs_hub_claims.py",
        'Admin, Developer, Analyst and Viewer decide what a user can do; custom roles (full profile) are stored but not applied yet"',
    ): 1,
    (
        "backend/tests/unit/docs/test_docs_hub_claims.py",
        'Only the base role decides what a user can do"',
    ): 1,
    (
        "backend/tests/unit/docs/test_docs_hub_claims.py",
        'no permission check reads them"',
    ): 1,
    (
        "docs/_claims/README.md.toml",
        "Both are recorded, but they do not change what anyone can do yet: each user's built-in role decides (#891)\"",
    ): 2,
    (
        "docs/_claims/docs/api/auth.md.toml",
        'These are stored and shown, but no permission check reads them yet; routes check the base role (#891)."',
    ): 2,
    (
        "docs/_claims/docs/api/rbac.md.toml",
        'Custom roles and direct grants are stored, and shown by GET /api/v1/rbac/users/{user_id}/permissions, but no permission check reads them: assigning a custom role or granting a permission changes nothing a user can do."',
    ): 2,
    (
        "docs/_claims/docs/api/rbac.md.toml",
        'No permission check uses this set (see the note above):"',
    ): 1,
    (
        "docs/_claims/docs/api/rbac.md.toml",
        'Only the base role decides what a user can do"',
    ): 1,
    (
        "docs/_claims/docs/api/rbac.md.toml",
        'Routes call check_permission() from backend/app/core/permissions.py, which reads only the base role."',
    ): 1,
    (
        "docs/_claims/docs/api/rbac.md.toml",
        'Routes check the base role (through check_permission() in backend/app/core/permissions.py) or the superuser flag; none reads custom roles or direct grants."',
    ): 1,
    (
        "docs/_claims/docs/api/rbac.md.toml",
        'The assignment is stored; the analyst can still do only what the analyst base role allows."',
    ): 1,
    (
        "docs/_claims/docs/api/rbac.md.toml",
        'The grant is stored and shown by Get Effective Permissions, but no permission check reads it: it does not let the analyst change an experiment, which their base role does not allow (#891)."',
    ): 1,
    (
        "docs/_claims/docs/api/rbac.md.toml",
        'The grant is stored and shown by Get Effective Permissions; no permission check reads it yet (#891)."',
    ): 1,
    (
        "docs/_claims/docs/api/rbac.md.toml",
        'The grant is stored and shown; it changes nothing the analyst can do."',
    ): 1,
    (
        "docs/_claims/docs/api/rbac.md.toml",
        'The role is stored and shown; it does not change what the user can do, which their base role decides (#891)."',
    ): 2,
    (
        "docs/_claims/docs/api/rbac.md.toml",
        'This is a display: routes check only the base role, so a custom role or direct grant listed here does not let the user do anything their base role does not."',
    ): 2,
    ("docs/_claims/docs/api/rbac.md.toml", 'What the analyst can do is unchanged."'): 1,
    (
        "docs/_claims/docs/api/rbac.md.toml",
        'What the user can actually do is decided by their base role alone (see Overview)."',
    ): 1,
    (
        "docs/_claims/docs/api/rbac.md.toml",
        'Whether to make them take effect or remove them is tracked in #891."',
    ): 1,
    (
        "docs/_claims/docs/api/rbac.md.toml",
        'check_effective_permission is not called by any route (#891):"',
    ): 1,
    (
        "docs/_claims/docs/getting-started/architecture.md.toml",
        'Custom roles and direct permission grants are stored and shown alongside the base role, but no permission check reads them yet: routes check the base role (#891)."',
    ): 2,
    (
        "docs/_claims/docs/getting-started/faq.md.toml",
        'Both are stored and shown by GET /api/v1/rbac/users/{user_id}/permissions, but no permission check reads them yet: what a user can do is decided by their base role (#891)."',
    ): 2,
    (
        "docs/_claims/docs/getting-started/limitations.md.toml",
        "The rbac module records them and shows them, but no permission check reads them: each user's built-in role decides (#891).\"",
    ): 2,
    (
        "docs/_claims/docs/getting-started/modules.md.toml",
        "Both are recorded, but they do not change what anyone can do yet: each user's built-in role decides (#891)\"",
    ): 2,
    (
        "docs/_claims/frontend/src/pages/docs/index.tsx.desc.toml",
        'Admin, Developer, Analyst and Viewer decide what a user can do; custom roles (full profile) are stored but not applied yet"',
    ): 1,
    (
        "docs/api/auth.md",
        "These are stored and shown, but no permission check reads them yet; routes check the base role ([#891](https://github.com/Controlvar/controlvar/issues/891)).",
    ): 2,
    (
        "docs/api/rbac.md",
        "Custom roles and direct grants are stored, and shown by `GET /api/v1/rbac/users/{user_id}/permissions`, but no permission check reads them: assigning a custom role or granting a permission changes nothing a user can do.",
    ): 2,
    (
        "docs/api/rbac.md",
        "No permission check uses this set (see the note above): ```",
    ): 1,
    ("docs/api/rbac.md", 'Only the base role decides what a user can do"'): 1,
    (
        "docs/api/rbac.md",
        "Routes call `check_permission()` from `backend/app/core/permissions.py`, which reads only the base role.",
    ): 1,
    (
        "docs/api/rbac.md",
        "Routes check the base role (through `check_permission()` in `backend/app/core/permissions.py`) or the superuser flag; none reads custom roles or direct grants.",
    ): 1,
    (
        "docs/api/rbac.md",
        "The assignment is stored; the analyst can still do only what the `analyst` base role allows.",
    ): 1,
    (
        "docs/api/rbac.md",
        "The grant is stored and shown by Get Effective Permissions, but no permission check reads it: it does **not** let the analyst change an experiment, which their base role does not allow ([#891](https://github.com/Controlvar/controlvar/issues/891)).",
    ): 1,
    (
        "docs/api/rbac.md",
        "The grant is stored and shown by Get Effective Permissions; no permission check reads it yet ([#891](https://github.com/Controlvar/controlvar/issues/891)).",
    ): 1,
    (
        "docs/api/rbac.md",
        "The grant is stored and shown; it changes nothing the analyst can do.",
    ): 1,
    (
        "docs/api/rbac.md",
        "The role is stored and shown; it does not change what the user can do, which their base role decides ([#891](https://github.com/Controlvar/controlvar/issues/891)).",
    ): 2,
    (
        "docs/api/rbac.md",
        "This is a display: routes check only the base role, so a custom role or direct grant listed here does not let the user do anything their base role does not.",
    ): 2,
    ("docs/api/rbac.md", "What the analyst can do is unchanged."): 1,
    (
        "docs/api/rbac.md",
        "What the user can actually do is decided by their base role alone (see [Overview](#overview)).",
    ): 1,
    (
        "docs/api/rbac.md",
        "Whether to make them take effect or remove them is tracked in [#891](https://github.com/Controlvar/controlvar/issues/891).",
    ): 1,
    (
        "docs/api/rbac.md",
        "`check_effective_permission` is not called by any route ([#891](https://github.com/Controlvar/controlvar/issues/891)): ```",
    ): 1,
    (
        "docs/getting-started/architecture.md",
        "Custom roles and direct permission grants are stored and shown alongside the base role, but no permission check reads them yet: routes check the base role ([#891](https://github.com/Controlvar/controlvar/issues/891)).",
    ): 2,
    (
        "docs/getting-started/faq.md",
        "Both are stored and shown by `GET /api/v1/rbac/users/{user_id}/permissions`, but no permission check reads them yet: what a user can do is decided by their base role ([#891](https://github.com/Controlvar/controlvar/issues/891)).",
    ): 2,
    (
        "docs/getting-started/limitations.md",
        "The `rbac` module records them and shows them, but no permission check reads them: each user's built-in role decides ([#891](https://github.com/Controlvar/controlvar/issues/891)).",
    ): 2,
    (
        "docs/getting-started/modules.md",
        "Both are recorded, but they do not change what anyone can do yet: each user's built-in role decides ([#891](https://github.com/Controlvar/controlvar/issues/891)) |",
    ): 2,
    (
        "frontend/src/components/admin/users/EditUserModal.tsx",
        "> Custom roles and direct permission grants (full edition) are recorded, but they do not change what anyone can do yet: the role set here decides.",
    ): 1,
    (
        "frontend/src/components/admin/users/EffectivePermissionsModal.tsx",
        "Custom roles and direct permission grants are recorded, but they do not change what anyone can do yet: each user's built-in role decides.\"",
    ): 2,
    (
        "frontend/src/components/admin/users/EffectivePermissionsModal.tsx",
        "No permission check reads custom roles or direct grants yet (#891).",
    ): 1,
    (
        "frontend/src/modules-stub/pages/admin/roles.tsx",
        "Both are recorded, but they do not change what anyone can do yet: each user's built-in role decides.\"",
    ): 2,
    (
        "frontend/src/pages/docs/index.tsx",
        "Admin, Developer, Analyst and Viewer decide what a user can do; custom roles (full profile) are stored but not applied yet'",
    ): 1,
    (
        "frontend/src/tests/admin/EditUserModal.test.tsx",
        "Custom roles and direct permission grants (full edition) are recorded, but they do not change what anyone can do yet: the role set here decides.'",
    ): 1,
    (
        "frontend/src/tests/admin/EditUserModal.test.tsx",
        "full profile: says custom roles and grants do not change access yet (#891), and what SSO does to the role'",
    ): 1,
    (
        "frontend/src/tests/admin/EffectivePermissionsModal.test.tsx",
        "No permission check reads custom roles or direct grants yet (#891), so the //",
    ): 1,
    (
        "frontend/src/tests/admin/EffectivePermissionsModal.test.tsx",
        "anyone can do yet: each user's built-in role decides.\"",
    ): 1,
    (
        "frontend/src/tests/admin/EffectivePermissionsModal.test.tsx",
        "says, above the list, that custom roles and direct grants do not change access yet (#891)'",
    ): 1,
    (
        "frontend/src/tests/pages/docs-index.test.tsx",
        ").toMatch(/custom roles \\(full profile\\) are stored but not applied yet/); }); it('",
    ): 1,
    (
        "frontend/src/tests/pages/docs-index.test.tsx",
        "says custom roles are not applied yet (#891)'",
    ): 1,
}


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
