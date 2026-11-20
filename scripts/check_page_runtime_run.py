#!/usr/bin/env python3
"""Fail unless the page-runtime project ran every one of its tests, and each passed first time.

Usage: check_page_runtime_run.py LIST_JSON RUN_JSON SPEC_DIR

LIST_JSON is Playwright's JSON report of ``--project=page-runtime --list``,
RUN_JSON the JSON report of the run itself, SPEC_DIR the project's test
directory (frontend/tests/e2e). The run passes only when

* the listing holds every ``*.runtime.spec.ts`` under SPEC_DIR and no other
  file, and at least one test;
* the run's counts are exactly: expected == the number of tests listed,
  unexpected == 0, flaky == 0, skipped == 0, and the report has no errors.

Playwright exits 0 when a test is skipped (``test.skip``, a conditional skip,
a fixture that skips) or passes only on a retry (flaky), so its exit status
alone would pass a run that checked less than it lists (#1545, Browser E2E).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = "page-runtime"
SPEC_SUFFIX = ".runtime.spec.ts"


def _tests(suites: list[dict]) -> list[tuple[str, str]]:
    """``(file, title)`` of each page-runtime test in a report's suite tree."""
    found: list[tuple[str, str]] = []
    for suite in suites:
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                if test.get("projectName") == PROJECT:
                    found.append((spec.get("file", ""), spec.get("title", "")))
        found.extend(_tests(suite.get("suites", [])))
    return found


def check(listing: dict, run: dict, spec_dir: Path) -> list[str]:
    """One message per problem; empty when the run is complete and clean."""
    problems: list[str] = []
    listed = _tests(listing.get("suites", []))
    on_disk = sorted(
        p.relative_to(spec_dir).as_posix() for p in spec_dir.rglob(f"*{SPEC_SUFFIX}")
    )
    in_listing = sorted({file for file, _ in listed})
    if not listed:
        problems.append(f"the {PROJECT} listing holds no test")
    if in_listing != on_disk:
        missing = sorted(set(on_disk) - set(in_listing))
        extra = sorted(set(in_listing) - set(on_disk))
        problems.append(
            f"listed files != *{SPEC_SUFFIX} files: not listed {missing}, not a runtime spec {extra}"
        )
    stats = run.get("stats") or {}
    wanted = {"expected": len(listed), "unexpected": 0, "flaky": 0, "skipped": 0}
    for key, value in wanted.items():
        got = stats.get(key)
        if got != value:
            problems.append(f"{key} {got} != {value}")
    if run.get("errors"):
        problems.append(f"the run reported errors: {json.dumps(run['errors'])[:500]}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    list_path, run_path, spec_dir = Path(argv[0]), Path(argv[1]), Path(argv[2])
    try:
        listing = json.loads(list_path.read_text(encoding="utf-8"))
        run = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"::error::cannot read a Playwright JSON report: {exc}")
        return 2
    if not spec_dir.is_dir():
        print(f"::error::{spec_dir} is not a directory")
        return 2
    problems = check(listing, run, spec_dir)
    if problems:
        print(f"::error::{PROJECT}: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(
        f"{PROJECT}: {len(_tests(listing.get('suites', [])))} test(s) listed, each ran and passed first time"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
