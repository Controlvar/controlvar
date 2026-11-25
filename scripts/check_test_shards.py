#!/usr/bin/env python3
"""Prove that a sharded suite ran every collected test exactly once.

    python3 scripts/check_test_shards.py --suite backend/tests/integration \\
        [--only-suites] [--github-annotations] \\
        [--allow-skip 'backend/tests/integration=<exact skip reason>' ...] \\
        [--any-skip <suite>] [--allow-disabled-plugin <suite>=cov ...] \
        <report or directory> [...]

Each sharded pytest session writes a report (backend/tests/shard.py). A
directory is searched recursively for ``shard-*.json``; a file is read whatever
its name. Only reports whose ``suite`` is a ``--suite`` given here are checked;
a report for another suite is left alone, unless ``--only-suites`` is given,
for a summary whose reports must all be of the suites it names: then each
such report is a problem, ``PROBLEM: <path>: suite <s> is not a --suite``.

``--github-annotations`` also prints each problem, a bad report and "no shard
report found" as a GitHub Actions error annotation (``::error::<problem>``,
one line, ``%``, CR and LF escaped), so a workflow step that runs the checker
needs no pipe to show them: a pipe would replace the checker's exit status
with the last command's under a step's default ``bash -e``.

For every ``--suite``, it exits 0 only when all of these hold:

* at least one report, one ``of``, and shard numbers exactly ``1..of``;
* every shard collected the same, non-empty list of node ids;
* the selected lists are non-empty, disjoint, and together equal that list;
* nothing narrowed any shard (``-m``, ``-k``, ``--deselect``, ``--ignore``,
  ``--ignore-glob``, ``--lf``, an item another plugin deselected, an item
  removed before the shard hook without being deselected, an item present
  there that pytest never collected, a ``-p no:NAME``
  not allowed below, or plugin autoload turned off), and no collector failed;
* every selected test has exactly one ``ran`` record, nothing else ran, and
  each outcome is passed, xfailed, or skipped with an allowed reason.

Skips: none is allowed unless named. ``--allow-skip SUITE=REASON`` allows one
exact reason (repeat it); ``--any-skip SUITE`` leaves the suite's skips
unconstrained.

Disabled plugins: ``--allow-disabled-plugin SUITE=NAME`` allows one
``-p no:NAME`` (the CI commands pass ``-p no:cov``, so they name ``cov``).
There is no other narrowing allowance.

What it cannot see: a change to the tree itself -- a ``collect_ignore`` entry,
a deleted test file, a generator yielding fewer parameters. pytest then never
collects those tests, every shard agrees, and nothing in the session differs.
Those are out of scope here and reviewed as code.

Exit status: 0 proven; 1 a rule refused; 2 a usage error, or a report that is
unreadable, unparsable, truncated or malformed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPORT_VERSION = 1
REPORT_GLOB = "shard-*.json"
OUTCOMES = {"passed", "failed", "skipped", "xfailed", "xpassed", "error"}
EXAMPLES = 5


class BadReport(Exception):
    """A report that cannot be read as one."""


class Usage(Exception):
    """The command line is wrong."""


def _norm_suite(suite: str) -> str:
    parts = []
    for part in suite.split():
        while part.startswith("./"):
            part = part[2:]
        parts.append(part.rstrip("/") or part)
    return " ".join(sorted(parts))


def _examples(ids) -> str:
    ids = sorted(ids)
    shown = ", ".join(repr(i) for i in ids[:EXAMPLES])
    more = f" and {len(ids) - EXAMPLES} more" if len(ids) > EXAMPLES else ""
    return f"[{shown}{more}]"


def _str_list(doc: dict, key: str, where: str) -> list[str]:
    value = doc.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BadReport(f"{where}: {key!r} is not a list of strings")
    return value


def _int(doc: dict, key: str, where: str, minimum: int) -> int:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BadReport(f"{where}: {key!r} is not an integer >= {minimum}")
    return value


def load_report(path: Path) -> dict:
    where = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BadReport(f"{where}: unreadable: {exc}") from None
    if not text.strip():
        raise BadReport(f"{where}: empty (the session did not finish writing it)")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BadReport(f"{where}: not JSON, or truncated: {exc}") from None
    if not isinstance(doc, dict):
        raise BadReport(f"{where}: not a JSON object")
    if doc.get("version") != REPORT_VERSION:
        raise BadReport(
            f"{where}: version {doc.get('version')!r}, not {REPORT_VERSION}"
        )
    if not isinstance(doc.get("suite"), str) or not doc["suite"]:
        raise BadReport(f"{where}: 'suite' is missing")
    _int(doc, "shard", where, 1)
    _int(doc, "of", where, 1)
    _int(doc, "pre_deselected", where, 0)
    _str_list(doc, "unannounced", where)
    _str_list(doc, "fabricated", where)
    _str_list(doc, "disabled_plugins", where)
    if not isinstance(doc.get("autoload_disabled"), bool):
        raise BadReport(f"{where}: 'autoload_disabled' is not a boolean")
    for key in ("collected", "selected", "deselect", "ignore", "ignore_glob"):
        _str_list(doc, key, where)
    _str_list(doc, "collect_errors", where)
    for key in ("markexpr", "keyword"):
        if not isinstance(doc.get(key), str):
            raise BadReport(f"{where}: {key!r} is not a string")
    if not isinstance(doc.get("last_failed"), bool):
        raise BadReport(f"{where}: 'last_failed' is not a boolean")
    ran = doc.get("ran")
    if not isinstance(ran, list):
        raise BadReport(f"{where}: 'ran' is not a list")
    for record in ran:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("nodeid"), str)
            or not (record.get("outcome") is None or record.get("outcome") in OUTCOMES)
            or not (
                record.get("skip_reason") is None
                or isinstance(record.get("skip_reason"), str)
            )
        ):
            raise BadReport(f"{where}: a 'ran' record is malformed: {record!r}")
    doc["_path"] = where
    return doc


def find_reports(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found.extend(sorted(p for p in path.rglob(REPORT_GLOB) if p.is_file()))
        elif path.is_file():
            found.append(path)
        else:
            raise Usage(f"{raw}: no such file or directory")
    return found


def _narrowing(doc: dict, plugins_allowed: set[str]) -> list[str]:
    narrowed = []
    for key in ("markexpr", "keyword"):
        if doc[key]:
            narrowed.append(f"{key}={doc[key]!r}")
    for key in ("deselect", "ignore", "ignore_glob"):
        if doc[key]:
            narrowed.append(f"{key}={doc[key]!r}")
    if doc["last_failed"]:
        narrowed.append("last_failed=True (--lf)")
    if doc["pre_deselected"]:
        narrowed.append(
            f"pre_deselected={doc['pre_deselected']} (items another plugin deselected)"
        )
    if doc["unannounced"]:
        narrowed.append(
            f"unannounced={_examples(doc['unannounced'])} ({len(doc['unannounced'])} "
            "collected but gone before the shard hook, never deselected)"
        )
    if doc["fabricated"]:
        narrowed.append(
            f"fabricated={_examples(doc['fabricated'])} ({len(doc['fabricated'])} "
            "present at the shard hook but never collected)"
        )
    disabled = [p for p in doc["disabled_plugins"] if p not in plugins_allowed]
    if disabled:
        narrowed.append(f"disabled_plugins={disabled} (-p no:)")
    if doc["autoload_disabled"]:
        narrowed.append("autoload_disabled=True (plugin autoload is off)")
    return narrowed


def check_suite(
    suite: str,
    docs: list[dict],
    allowed: set[str] | None,
    plugins_allowed: set[str] | frozenset[str] = frozenset(),
) -> tuple[list[str], int]:
    """Return the problems with one suite's reports, and the tests proven."""
    if not docs:
        return [f"{suite}: no shard report for this suite"], 0
    problems: list[str] = []

    totals = sorted({d["of"] for d in docs})
    if len(totals) != 1:
        problems.append(f"{suite}: the reports disagree on the shard count: {totals}")
        return problems, 0
    total = totals[0]
    by_index: dict[int, list[dict]] = {}
    for doc in docs:
        by_index.setdefault(doc["shard"], []).append(doc)
    for index, group in sorted(by_index.items()):
        if index > total:
            problems.append(f"{suite}: shard {index} reported, but of={total}")
        if len(group) > 1:
            paths = ", ".join(d["_path"] for d in group)
            problems.append(
                f"{suite}: shard {index} reported {len(group)} times ({paths})"
            )
    missing = [i for i in range(1, total + 1) if i not in by_index]
    if missing:
        problems.append(f"{suite}: shard(s) {missing} of {total} have no report")

    collected = docs[0]["collected"]
    for doc in docs[1:]:
        if doc["collected"] != collected:
            only_here = set(doc["collected"]) - set(collected)
            only_there = set(collected) - set(doc["collected"])
            problems.append(
                f"{suite}: shard {doc['shard']} collected a different list from "
                f"shard {docs[0]['shard']} (node ids differ between processes?): "
                f"{len(only_here)} only in shard {doc['shard']} {_examples(only_here)}, "
                f"{len(only_there)} only in shard {docs[0]['shard']} "
                f"{_examples(only_there)}"
            )
    if not collected:
        problems.append(
            f"{suite}: the collected set is EMPTY: refusing to compare against nothing"
        )
    duplicated = {node for node, count in Counter(collected).items() if count > 1}
    if duplicated:
        problems.append(
            f"{suite}: collected twice in one session: {_examples(duplicated)}"
        )
    expected = set(collected)

    seen: dict[str, int] = {}
    for doc in docs:
        tag = f"{suite}: shard {doc['shard']}"
        if not doc["selected"]:
            problems.append(f"{tag} selected nothing")
        for node in set(doc["selected"]):
            seen[node] = seen.get(node, 0) + 1
        stray = set(doc["selected"]) - expected
        if stray:
            problems.append(
                f"{tag} selected {len(stray)} not collected: {_examples(stray)}"
            )
        narrowed = _narrowing(doc, set(plugins_allowed))
        if narrowed:
            problems.append(f"{tag}: narrowed by {', '.join(narrowed)}")
        if doc["collect_errors"]:
            problems.append(
                f"{tag}: {len(doc['collect_errors'])} collection error(s): "
                f"{_examples(doc['collect_errors'])}"
            )
        problems.extend(_check_ran(tag, doc, allowed))
    none = expected - set(seen)
    if none:
        problems.append(
            f"{suite}: {len(none)} collected but selected by no shard {_examples(none)}"
        )
    several = {n for n, c in seen.items() if c > 1}
    if several:
        problems.append(
            f"{suite}: {len(several)} selected by more than one shard {_examples(several)}"
        )
    return problems, len(expected)


def _check_ran(tag: str, doc: dict, allowed: set[str] | None) -> list[str]:
    problems: list[str] = []
    selected = set(doc["selected"])
    runs: dict[str, int] = {}
    for record in doc["ran"]:
        runs[record["nodeid"]] = runs.get(record["nodeid"], 0) + 1
    not_run = selected - set(runs)
    if not_run:
        problems.append(
            f"{tag}: {len(not_run)} selected but did not run {_examples(not_run)}"
        )
    twice = {n for n, c in runs.items() if c > 1}
    if twice:
        problems.append(f"{tag}: {len(twice)} ran more than once {_examples(twice)}")
    extra = set(runs) - selected
    if extra:
        problems.append(
            f"{tag}: {len(extra)} ran but were not selected {_examples(extra)}"
        )
    for record in doc["ran"]:
        node, outcome = record["nodeid"], record["outcome"]
        if outcome in ("passed", "xfailed"):
            continue
        if outcome == "skipped":
            reason = record["skip_reason"] or ""
            if allowed is None or reason in allowed:
                continue
            problems.append(
                f"{tag}: {node} was skipped, and its reason is not allowed: {reason!r}"
            )
        elif outcome is None:
            problems.append(f"{tag}: {node} started but never finished")
        else:
            problems.append(f"{tag}: {node} {outcome}")
    return problems


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prove a sharded suite ran every collected test exactly once."
    )
    parser.add_argument("--suite", action="append", default=[], required=True)
    parser.add_argument(
        "--only-suites",
        action="store_true",
        help="a report of a suite not named by --suite is a problem",
    )
    parser.add_argument(
        "--github-annotations",
        action="store_true",
        help="also print each problem as a GitHub Actions ::error:: annotation",
    )
    parser.add_argument(
        "--allow-skip", action="append", default=[], metavar="SUITE=REASON"
    )
    parser.add_argument("--any-skip", action="append", default=[], metavar="SUITE")
    parser.add_argument(
        "--allow-disabled-plugin", action="append", default=[], metavar="SUITE=NAME"
    )
    parser.add_argument("paths", nargs="+")
    try:
        return parser.parse_args(argv)
    except SystemExit as exc:
        raise Usage("bad arguments") from exc


def _annotation(text: str) -> str:
    """*text* as a GitHub Actions error annotation, on one line: ``%``, CR and
    LF are escaped as a workflow command's data requires."""
    data = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::error::{data}"


def _one_line(text: str) -> str:
    """A problem on one line: a CR or LF in a value read from a report is
    shown escaped, so it cannot start a line of its own in the log."""
    return text.replace("\r", "\\r").replace("\n", "\\n")


def run(argv: list[str]) -> int:
    annotate = False
    try:
        args = _parse_args(argv)
        annotate = args.github_annotations
        suites = [_norm_suite(s) for s in args.suite]
        if len(set(suites)) != len(suites):
            raise Usage(f"a --suite is named twice: {suites}")
        allowed: dict[str, set[str] | None] = {s: set() for s in suites}
        for suite in (_norm_suite(s) for s in args.any_skip):
            if suite not in allowed:
                raise Usage(f"--any-skip {suite}: not a --suite")
            allowed[suite] = None
        for entry in args.allow_skip:
            suite, sep, reason = entry.partition("=")
            suite = _norm_suite(suite)
            if not sep or not reason:
                raise Usage(f"--allow-skip {entry!r}: expected SUITE=REASON")
            if suite not in allowed:
                raise Usage(f"--allow-skip {entry!r}: {suite} is not a --suite")
            if allowed[suite] is None:
                raise Usage(f"--allow-skip {entry!r}: {suite} already has --any-skip")
            allowed[suite].add(reason)
        plugins_allowed: dict[str, set[str]] = {s: set() for s in suites}
        for entry in args.allow_disabled_plugin:
            suite, sep, name = entry.partition("=")
            suite = _norm_suite(suite)
            if not sep or not name or name.startswith("no:"):
                raise Usage(f"--allow-disabled-plugin {entry!r}: expected SUITE=NAME")
            if suite not in plugins_allowed:
                raise Usage(
                    f"--allow-disabled-plugin {entry!r}: {suite} is not a --suite"
                )
            plugins_allowed[suite].add(name)
        paths = find_reports(args.paths)
        docs = [load_report(p) for p in paths]
    except Usage as exc:
        print(f"check_test_shards: {exc}", file=sys.stderr)
        return 2
    except BadReport as exc:
        print(f"check_test_shards: refusing a bad report: {exc}", file=sys.stderr)
        if annotate:
            print(_annotation(f"refusing a bad report: {exc}"))
        return 2

    def problem(text: str) -> None:
        text = _one_line(text)
        print(f"PROBLEM: {text}")
        if annotate:
            print(_annotation(text))

    if not docs:
        message = "no shard report found in " + " ".join(args.paths)
        print(f"check_test_shards: {message}")
        if annotate:
            print(_annotation(message))
        return 1
    failed = False
    if args.only_suites:
        for doc in docs:
            if _norm_suite(doc["suite"]) not in suites:
                failed = True
                problem(f"{doc['_path']}: suite {doc['suite']} is not a --suite")
    for suite in suites:
        mine = [d for d in docs if _norm_suite(d["suite"]) == suite]
        problems, proven = check_suite(
            suite, mine, allowed[suite], plugins_allowed[suite]
        )
        if problems:
            failed = True
            for text in problems:
                problem(text)
        else:
            print(
                f"{suite}: {len(mine)} shards, {proven} collected, each ran exactly once"
            )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
