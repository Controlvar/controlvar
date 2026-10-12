#!/usr/bin/env python3
"""List what a documentation change cut, and what the audit said about each cut.

    python3 scripts/removed_units.py --audit <rows.jsonl | directory of them>
        (--base-ref <git ref> | --base-dir <tree>) [--head-dir <tree>] [--out <file>]

A tool for the pull requests of #1142 that shrink a page. It compares each
document at the base with the same document at the head (the working tree by
default) and lists:

* every audit row whose text was on the base page and is on no head page:
  ``removed TRUE claim: where did it go?`` for a claim the audit did not mark
  false (covered by a test, or tagged T, S, A, D or X), ``removed flow: where
  did it go?`` for a flow, and ``removed false claim (R)`` / ``removed false
  flow (R)`` for one it did;
* every audit row whose text left its page but is on another in-scope head
  page: ``moved to <page>``;
* every other removed line with letters or digits in it, which no audit row
  covers: ``removed, no audit row``;
* a summary: the counts, and how many of the changed pages' audit rows were
  not found on the base page at all (their excerpt is not verbatim, or the
  page changed after the audit), which the report cannot check.

The documents are the base's in-scope Markdown documents
(``tests/acceptance/docs/inventory.toml``: every nav page and every
``[documents]`` entry with ``scope = "in"``), plus any file an audit row names.
Text is compared with runs of whitespace made one space and backticks and
asterisks dropped, so a reflow or a markup-only edit is not a cut. An audit
excerpt may elide with ``...``: then each piece around it has to be on the
page, in order.

The audit rows are JSON objects, one per line, with at least ``doc`` (a path
from the repository root), ``kind`` (``claim`` or ``flow``), ``line``,
``text`` (the excerpt), ``covered``, ``by`` and ``tag`` (null, or one of T, S,
A, D, R, X). The audit is not part of this repository, and the report quotes
it: the report goes to stdout, or to the file ``--out`` names, and an
``--out`` inside the repository or the head tree is refused. It is for the
pull request's reviewer; do not paste it into the pull request.

Exit status: 0 the report was written; 2 bad arguments or unreadable input.
Standard library only.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
INVENTORY = "tests/acceptance/docs/inventory.toml"
TAGS = ("T", "S", "A", "D", "X")
MARKUP = re.compile(r"[`*]")
SPACE = re.compile(r"\s+")
HAS_TEXT = re.compile(r"[A-Za-z0-9]")
ELISION = re.compile(r"\.\.\.|\u2026")
EXCERPT_MAX = 160

Read = Callable[[str], Optional[str]]


class Refused(Exception):
    """Bad arguments or unreadable input: exit status 2."""


@dataclass(frozen=True)
class Row:
    doc: str
    kind: str
    line: int
    text: str
    covered: bool
    by: str
    tag: Optional[str]


def normalise(text: str) -> str:
    return SPACE.sub(" ", MARKUP.sub("", text)).strip()


def pieces(excerpt: str) -> List[str]:
    """An excerpt's verbatim pieces, normalised: it may elide with ``...``."""
    return [
        piece.strip()
        for piece in ELISION.split(normalise(excerpt))
        if HAS_TEXT.search(piece)
    ]


def contains(flat: str, parts: Sequence[str]) -> bool:
    """Every piece is in ``flat``, in order."""
    at = 0
    for part in parts:
        at = flat.find(part, at)
        if at < 0:
            return False
        at += len(part)
    return bool(parts)


def short(text: str) -> str:
    text = SPACE.sub(" ", text).strip()
    return text if len(text) <= EXCERPT_MAX else text[: EXCERPT_MAX - 3] + "..."


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
def load_rows(path: Path) -> List[Row]:
    files = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
    if not files:
        raise Refused(f"no *.jsonl file in {path}")
    rows: List[Row] = []
    for file in files:
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise Refused(f"cannot read the audit {file}: {error}") from error
        for number, line in zip(range(1, len(lines) + 1), lines):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                rows.append(
                    Row(
                        doc=str(raw["doc"]),
                        kind=str(raw.get("kind") or "claim"),
                        line=int(raw.get("line") or 0),
                        text=str(raw["text"]),
                        covered=bool(raw.get("covered")),
                        by=str(raw.get("by") or ""),
                        tag=raw.get("tag"),
                    )
                )
            except (ValueError, KeyError, TypeError) as error:
                raise Refused(f"{file}:{number}: not an audit row ({error})") from error
    return rows


def directory_reader(root: Path) -> Read:
    def read(path: str) -> Optional[str]:
        file = root / path
        return file.read_text(encoding="utf-8") if file.is_file() else None

    return read


def git_reader(repository: Path, ref: str) -> Read:
    check = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
        ],
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        raise Refused(f"{ref} is not a commit in {repository}")
    commit = check.stdout.strip()

    def read(path: str) -> Optional[str]:
        shown = subprocess.run(
            ["git", "-C", str(repository), "show", f"{commit}:{path}"],
            capture_output=True,
        )
        if shown.returncode != 0:
            return None
        return shown.stdout.decode("utf-8", errors="replace")

    return read


def in_scope_markdown(read: Read) -> List[str]:
    """The tree's in-scope Markdown documents, by repository path."""
    text = read(INVENTORY)
    if text is None:
        raise Refused(f"the tree has no {INVENTORY}")
    inventory = tomllib.loads(text)
    paths = [f"docs/{page}" for page in inventory.get("pages", {})]
    paths += [
        key
        for key, entry in inventory.get("documents", {}).items()
        if isinstance(entry, dict)
        and entry.get("scope") == "in"
        and key.endswith(".md")
    ]
    return sorted(set(paths))


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def verdict(row: Row) -> str:
    if row.tag == "R":
        return f"removed false {row.kind} (R)"
    if row.kind == "flow":
        return "removed flow: where did it go?"
    return "removed TRUE claim: where did it go?"


def grade(row: Row) -> str:
    """The audit's tag, or ``tested`` for a row a test covers."""
    if row.tag:
        return str(row.tag)
    return "tested" if row.covered else "untagged"


def evidence(row: Row) -> str:
    return f"tested: {short(row.by)}" if grade(row) == "tested" else grade(row)


def removed_lines(base: str, head: str) -> List[Tuple[int, str]]:
    """Each line of ``base`` that ``head`` deletes or replaces, with its number."""
    old, new = base.splitlines(), head.splitlines()
    found: List[Tuple[int, str]] = []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, start, end, _, _ in matcher.get_opcodes():
        if tag in ("delete", "replace"):
            found.extend((number + 1, old[number]) for number in range(start, end))
    return found


def report(
    rows: Sequence[Row],
    base: Read,
    head: Read,
    *,
    base_label: str,
    head_label: str,
    audit_label: str,
) -> str:
    base_docs = in_scope_markdown(base)
    head_docs = in_scope_markdown(head) if head(INVENTORY) is not None else []
    by_doc: Dict[str, List[Row]] = {}
    for row in rows:
        by_doc.setdefault(row.doc, []).append(row)
    documents = sorted(set(base_docs) | {d for d in by_doc if base(d) is not None})
    head_texts = {d: normalise(t) for d in head_docs if (t := head(d)) is not None}

    out: List[str] = [
        f"Removed units: base {base_label}, head {head_label}",
        f"Audit: {len(rows)} rows from {audit_label}",
        "",
    ]
    counts: Counter = Counter()
    changed = deleted = 0
    for doc in documents:
        old = base(doc)
        new = head(doc)
        if old is None or old == new:
            continue
        changed += 1
        deleted += new is None
        old_flat, new_flat = normalise(old), normalise(new or "")
        lines: List[str] = []
        covered_by_rows: List[List[str]] = []
        for row in sorted(by_doc.get(doc, []), key=lambda r: r.line):
            parts = pieces(row.text)
            if not contains(old_flat, parts):
                counts["not found at base"] += 1
                continue
            if contains(new_flat, parts):
                continue
            covered_by_rows.append(parts)
            elsewhere = [
                d
                for d in sorted(head_texts)
                if d != doc and contains(head_texts[d], parts)
            ]
            if elsewhere:
                what = f"moved to {elsewhere[0]}"
                counts["moved"] += 1
            else:
                what = verdict(row)
                counts[what] += 1
                if row.tag != "R":
                    counts[f"true:{grade(row)}"] += 1
            lines.append(
                f'  {what}  {doc}:{row.line} (audit)  [{evidence(row)}]  "{short(row.text)}"'
            )
        for number, line in removed_lines(old, new or ""):
            flat = normalise(line)
            if not HAS_TEXT.search(flat) or flat in new_flat:
                continue
            # The line is the row's when it is inside one of the row's pieces
            # (a line of a wrapped excerpt) or holds all of them, in order. A
            # line that merely contains one short piece ("6000") is not.
            if any(
                any(flat in piece for piece in parts) or contains(flat, parts)
                for parts in covered_by_rows
            ):
                continue
            counts["removed, no audit row"] += 1
            lines.append(f'  removed, no audit row  {doc}:{number}  "{short(line)}"')
        out.append(f"{doc} (deleted)" if new is None else doc)
        out.extend(lines or ["  (changed; nothing removed)"])
        out.append("")

    true_counts = ", ".join(
        f"{key} {counts[f'true:{key}']}" for key in ("tested", *TAGS, "untagged")
    )
    out += [
        "Summary",
        f"  documents changed: {changed} ({deleted} deleted)",
        f"  removed TRUE claim: {counts['removed TRUE claim: where did it go?']}",
        f"  removed flow: {counts['removed flow: where did it go?']}",
        f"    of the true claims and flows: {true_counts}",
        "  removed false (R): "
        f"{counts['removed false claim (R)'] + counts['removed false flow (R)']}",
        f"  moved to another page: {counts['moved']}",
        f"  removed, no audit row: {counts['removed, no audit row']}",
        f"  audit rows not found on their base page (not checked): {counts['not found at base']}",
    ]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def _inside(path: Path, tree: Path) -> bool:
    try:
        path.relative_to(tree)
    except ValueError:
        return False
    return True


def main(argv: Iterable[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--audit", required=True, type=Path)
    base = parser.add_mutually_exclusive_group(required=True)
    base.add_argument("--base-ref")
    base.add_argument("--base-dir", type=Path)
    parser.add_argument("--head-dir", type=Path, default=REPO_ROOT)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(list(argv))
    head_dir = args.head_dir.resolve()
    try:
        if args.out is not None:
            out = args.out.resolve()
            for tree in (REPO_ROOT, head_dir):
                if _inside(out, tree):
                    raise Refused(
                        f"--out {args.out} is inside {tree}: the report quotes the"
                        " audit, so write it outside the repository"
                    )
        rows = load_rows(args.audit)
        if args.base_ref:
            base_read, base_label = git_reader(head_dir, args.base_ref), args.base_ref
        else:
            if not args.base_dir.is_dir():
                raise Refused(f"--base-dir {args.base_dir} is not a directory")
            base_read, base_label = directory_reader(args.base_dir), str(args.base_dir)
        text = report(
            rows,
            base_read,
            directory_reader(head_dir),
            base_label=base_label,
            head_label=str(head_dir),
            audit_label=str(args.audit),
        )
    except Refused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    if args.out is not None:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
