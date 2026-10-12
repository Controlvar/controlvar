#!/usr/bin/env python3
"""Fail when a test under a directory was skipped, or when none ran at all.

    python3 scripts/check_junit_skips.py <junit.xml> <tests directory> [...]

The workflow tests (`backend/tests/unit/infrastructure`) and the CDK suite
(`infrastructure/tests`) mostly `skipif` their subject file is absent, so that
a distribution which does not ship `.github/workflows` has nothing to check.
The same guard makes a RENAME silent: renaming the deploy workflow without
updating the constants that name it skipped 19 tests, and both jobs stayed
green (QA SILENT 1). Nothing in either suite skips on a full checkout, so the
jobs that run one assert exactly that: zero skipped, and at least one test
found under each directory (a scope that matches nothing is a vacuous pass).

A directory is matched through the JUnit ``classname``, which pytest writes as
the dotted module path (``backend.tests.unit.infrastructure.test_x.TestY``, or
``infrastructure.tests.test_x`` under ``--import-mode=importlib``). A module
that skips itself while it is collected (``pytest.skip(...,
allow_module_level=True)``) is written with an empty ``classname`` and the
dotted module path as its ``name``, so a case with no classname is matched
through its name: before #1142 such a skip was not seen at all.

Exit status: 0 no skips; 1 skips, or nothing found; 2 unreadable report.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _prefix(directory: str) -> str:
    return directory.strip("/").replace("/", ".") + "."


def _dotted(case: ET.Element) -> str:
    """Where the case lives, as a dotted path: its classname, or for a module
    skipped while it was collected (no classname) its name."""
    return case.get("classname") or case.get("name", "")


def _label(case: ET.Element) -> str:
    classname, name = case.get("classname"), case.get("name", "")
    return f"{classname}::{name}" if classname else name


def check(report: Path, directories: list[str]) -> list[str]:
    """Return one message per problem; empty means the report is clean."""
    root = ET.parse(report).getroot()
    problems: list[str] = []
    cases = list(root.iter("testcase"))
    for directory in directories:
        prefix = _prefix(directory)
        scoped = [c for c in cases if _dotted(c).startswith(prefix)]
        if not scoped:
            problems.append(
                f"no test under {directory} is in {report}, so its zero-skip "
                "assertion would pass having checked nothing"
            )
            continue
        for case in scoped:
            skipped = case.find("skipped")
            if skipped is not None:
                reason = skipped.get("message") or (skipped.text or "").strip()
                problems.append(f"{_label(case)} was skipped: {reason}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    report, directories = Path(argv[0]), argv[1:]
    try:
        problems = check(report, directories)
    except (OSError, ET.ParseError) as exc:
        print(f"::error::cannot read JUnit report {report}: {exc}")
        return 2
    if problems:
        print(
            f"::error::{len(problems)} problem(s) under {', '.join(directories)}. "
            "These suites skip nothing on a full checkout; a skip here usually "
            "means a file they read was renamed or moved without updating the "
            "test that names it."
        )
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(f"no skipped tests under {', '.join(directories)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
