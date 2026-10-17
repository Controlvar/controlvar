#!/usr/bin/env python3
"""Fail on the project's former name anywhere it is not deliberately kept.

The project was renamed. A handful of identifiers keep the former spelling
on purpose, because a machine matches them against a stored or remote copy
(a compose volume name, an advisory-lock key, a JWT issuer, a header a third
party sends, ...). Everything else carries the new name. This check holds both
sides of that line:

* RESIDUAL   an occurrence of the former name, in a file's bytes or in a path,
             that no keep-list entry covers. Fix the text, or -- if it is a
             machine-matched identifier -- add an entry with its reason.
* STALE-ALLOW a keep-list entry that matched nothing: the kept identifier was
             renamed after all (which is the data-loss case the list exists
             for), or the entry is dead.
* COUNT      an entry that matched a different number of times than it pins.
             A count that moves is a reviewed change, not a side effect.

The keep-list is ``scripts/legacy_name_keep.toml``. Each entry names a path
(an exact path, or an ``fnmatch`` pattern), an exact case-sensitive literal
that contains the former name, the exact number of occurrences it covers, and
the reason. An occurrence is covered when an entry's literal occurs on the same
line and spans it; the longest such literal wins. The bare name may be kept
only in one named file (a pattern with the bare name would keep everything). A path name is checked as a
line of its own, numbered 0. ``[[exclude]]`` entries take whole directories or
files out, each with its reason.

What is walked: with a ``.git`` directory, the files ``git ls-files --cached
--others --exclude-standard`` lists -- the tree a release would see, so a
clone's ``.git/config``, a venv or an editable-install ``*.egg-info`` are out
of scope by construction. Without one (the ``core_build.sh`` copy), every file
under the root except generated directories (``__pycache__``, caches,
``node_modules``, ``.next``, ``*.egg-info``). Every file is read as bytes,
binaries included, and matched case-insensitively. A file under ``modules/`` is
kept only by an entry whose path is under ``modules/``, and those entries are
skipped only when ``modules/`` is absent as a whole (the core profile), so every
count holds in both profiles.

The search pattern is assembled at run time, so this file never matches
itself.

Usage::

    python scripts/check_legacy_name.py [--root DIR] [--keep FILE]

Exit status 0 when there is no RESIDUAL, STALE-ALLOW or COUNT line, else 1;
2 for a usage error (missing keep-list, malformed entry).
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEEP_FILE = Path("scripts") / "legacy_name_keep.toml"

# Assembled so this source never contains the word it looks for.
NEEDLE = ("experi" + "mently").encode("ascii")

GENERATED_DIRS = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        ".next",
        ".git",
    }
)


class UsageError(Exception):
    pass


@dataclass
class Keep:
    path: str
    literal: bytes
    count: int
    reason: str
    found: int = 0

    def applies_to(self, rel: str) -> bool:
        # A file under modules/ is kept only by an entry under modules/, so
        # that every other entry's count is the same with and without the
        # optional modules (scripts/core_build.sh deletes them).
        if rel.startswith("modules/") and not self.path.startswith("modules/"):
            return False
        return rel == self.path or fnmatch.fnmatchcase(rel, self.path)


@dataclass
class Exclude:
    path: str
    reason: str

    def covers(self, rel: str) -> bool:
        if self.path.endswith("/"):
            return rel.startswith(self.path)
        return rel == self.path


def load_keep(path: Path) -> tuple[list[Keep], list[Exclude]]:
    if not path.is_file():
        raise UsageError(f"no keep-list at {path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    keeps: list[Keep] = []
    for index, raw in enumerate(data.get("keep", [])):
        missing = {"path", "literal", "count", "reason"} - set(raw)
        if missing:
            raise UsageError(f"keep entry {index} lacks {sorted(missing)}")
        literal = raw["literal"].encode("utf-8")
        if NEEDLE not in literal.lower():
            raise UsageError(
                f"keep entry {index} ({raw['literal']!r}) does not contain the name"
            )
        if literal.lower() == NEEDLE and any(c in raw["path"] for c in "*?["):
            raise UsageError(
                f"keep entry {index}: the bare name may be kept only in one named "
                f"file, not across a pattern ({raw['path']!r})"
            )
        if not raw["reason"].strip():
            raise UsageError(f"keep entry {index} has an empty reason")
        if not isinstance(raw["count"], int) or raw["count"] < 1:
            raise UsageError(f"keep entry {index} needs a count of at least 1")
        keeps.append(Keep(raw["path"], literal, raw["count"], raw["reason"]))
    excludes = []
    for index, raw in enumerate(data.get("exclude", [])):
        if not raw.get("path") or not raw.get("reason", "").strip():
            raise UsageError(f"exclude entry {index} needs a path and a reason")
        excludes.append(Exclude(raw["path"], raw["reason"]))
    return keeps, excludes


def listed_files(root: Path) -> list[str]:
    """Relative POSIX paths of the files in scope (see the module docstring)."""
    if (root / ".git").exists():
        out = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--deduplicate",
            ],
            check=True,
            capture_output=True,
        ).stdout
        names = [os.fsdecode(n) for n in out.split(b"\0") if n]
        return sorted(n for n in names if os.path.lexists(root / n))
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in GENERATED_DIRS and not d.endswith(".egg-info")
        ]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            found.append(rel.replace(os.sep, "/"))
    return sorted(found)


def occurrences(line: bytes) -> list[int]:
    lowered = line.lower()
    starts = []
    at = lowered.find(NEEDLE)
    while at != -1:
        starts.append(at)
        at = lowered.find(NEEDLE, at + 1)
    return starts


def cover(line: bytes, start: int, candidates: list[Keep]) -> Keep | None:
    best: Keep | None = None
    end = start + len(NEEDLE)
    for keep in candidates:
        lit = keep.literal
        at = line.find(lit)
        while at != -1:
            if at <= start and at + len(lit) >= end:
                if best is None or len(lit) > len(best.literal):
                    best = keep
                break
            at = line.find(lit, at + 1)
    return best


def snippet(line: bytes) -> str:
    text = line.decode("utf-8", "replace").strip()
    return text if len(text) <= 160 else text[:157] + "..."


def check(root: Path, keep_path: Path) -> tuple[list[str], dict[str, int]]:
    keeps, excludes = load_keep(keep_path)
    try:
        keep_rel = keep_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        keep_rel = None
    modules_present = (root / "modules").is_dir()
    problems: list[str] = []
    stats = {"files": 0, "occurrences": 0, "kept": 0}
    for rel in listed_files(root):
        if rel == keep_rel or any(e.covers(rel) for e in excludes):
            continue
        stats["files"] += 1
        candidates = [k for k in keeps if k.applies_to(rel)]
        full = root / rel
        if full.is_symlink():
            data = os.readlink(full).encode("utf-8", "surrogateescape")
        elif full.is_file():
            data = full.read_bytes()
        else:
            continue
        lines = [(0, rel.encode("utf-8", "surrogateescape"))]
        if NEEDLE in data.lower():
            lines += list(enumerate(data.split(b"\n"), start=1))
        for number, line in lines:
            for start in occurrences(line):
                stats["occurrences"] += 1
                keep = cover(line, start, candidates)
                if keep is None:
                    where = "<path name>" if number == 0 else snippet(line)
                    problems.append(f"RESIDUAL {rel}:{number}: {where}")
                else:
                    keep.found += 1
                    stats["kept"] += 1
    for keep in keeps:
        if keep.path.startswith("modules/") and not modules_present:
            continue
        literal = keep.literal.decode("utf-8")
        if keep.found == 0:
            problems.append(f"STALE-ALLOW {keep.path} {literal!r} matched nothing")
        elif keep.found != keep.count:
            problems.append(
                f"COUNT {keep.path} {literal!r}: pinned {keep.count}, found {keep.found}"
            )
    return problems, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--keep", type=Path, default=None)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    keep_path = (args.keep or root / KEEP_FILE).resolve()
    try:
        problems, stats = check(root, keep_path)
    except UsageError as exc:
        print(f"legacy-name: {exc}", file=sys.stderr)
        return 2
    for line in problems:
        print(line)
    print(
        f"legacy-name: {stats['files']} files, {stats['occurrences']} occurrences, "
        f"{stats['kept']} kept, {len(problems)} problem(s)"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
