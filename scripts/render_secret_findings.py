#!/usr/bin/env python3
"""Print trivy secret findings as path, rule, severity and line -- nothing else.

``.github/workflows/release.yml`` scans each image for stored credentials
before pushing it, with ``trivy ... --format json --output <file>``. The
Actions log of a public repository is readable by anyone, so the report itself
must never reach it: trivy's table output, and the JSON's ``Match``, ``Code``
and image-config fields, carry the matched text or the lines around it. This
script reads the JSON file and prints, for each finding, only

    Target   RuleID   Severity   StartLine

and appends the same to ``$GITHUB_STEP_SUMMARY`` when that is set. No other
field of the report is read into the output, whatever it contains.

Exit status:

* 0 -- a trivy report with no findings;
* 1 -- one or more findings (printed as above);
* 2 -- the file is missing, unreadable, not JSON, or not a trivy report.

It fails closed: a report it cannot read is never "no findings". The workflow
keeps trivy's own exit status as well (``status=$?`` ... ``exit "$status"``),
so a crash in trivy fails the step even when this script is satisfied.

``.github/workflows/security-scan.yml`` uses it the same way for the scans
that run on pull requests, pushes and the weekly schedule, with ``--stage ci``:
the same rows, worded for a scan where nothing is being pushed.
``--stage release`` is the default and its wording is unchanged.

Usage::

    render_secret_findings.py REPORT.json --label "controlvar:core-1.2.3 (image)"
    render_secret_findings.py REPORT.json --stage ci --label "backend/ (source tree)"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

#: The only per-finding fields that are ever printed.
FIELDS = ("Target", "RuleID", "Severity", "StartLine")

#: The wording around the rows, per stage. ``release`` scans an image before
#: it is pushed; ``ci`` scans a tree or an image on a pull request, a push to
#: main or the weekly schedule, where nothing is being published.
COPY = {
    "release": {
        "heading": "**Credential scan: {n} finding(s) in {label}** (trivy {version}, secret scanner). Not pushed.",
        "error": "::error title=Credential found in image::{label} was not pushed: {n} finding(s). See the job summary.",
        "clean": "Credential scan: no findings in {label} (trivy {version}, secret scanner), scanned before push.",
    },
    "ci": {
        "heading": "**Credential scan: {n} finding(s) in {label}** (trivy {version}, secret scanner). Remove the value from the file at that line.",
        "error": "::error title=Credential pattern found::{label}: {n} finding(s). The job summary lists path, rule, severity and line; the matched text is not printed.",
        "clean": "Credential scan: no findings in {label} (trivy {version}, secret scanner).",
    },
}

#: Every printed value is cut to this many characters. A path is the longest
#: thing printed; nothing legitimate is longer.
MAX_FIELD = 200


def _clean(value: object) -> str:
    """A field as one bounded line of printable text."""
    if isinstance(value, bool) or value is None:
        return "-"
    text = str(value)[:MAX_FIELD]
    # No control characters: a newline would start a new log line, and a log
    # line beginning with `::` is a workflow command.
    return "".join(ch if ch.isprintable() else "?" for ch in text) or "-"


def _line(value: object) -> str:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else "-"


def load_report(path: Path) -> dict:
    """The parsed report, or SystemExit(2) with a reason that prints no content."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"::error::no trivy report at {path}: {exc.strerror}", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        data = json.loads(raw)
    except ValueError:
        print(f"::error::the trivy report at {path} is not JSON", file=sys.stderr)
        raise SystemExit(2) from None
    if not isinstance(data, dict) or "SchemaVersion" not in data:
        print(f"::error::{path} is not a trivy JSON report", file=sys.stderr)
        raise SystemExit(2)
    results = data.get("Results")
    if results is not None and not isinstance(results, list):
        print(f"::error::{path}: Results is not a list", file=sys.stderr)
        raise SystemExit(2)
    return data


def findings(report: dict) -> list[tuple[str, str, str, str]]:
    """(Target, RuleID, Severity, StartLine) for every secret in the report."""
    rows = []
    for result in report.get("Results") or []:
        if not isinstance(result, dict):
            raise SystemExit(2)
        secrets = result.get("Secrets") or []
        if not isinstance(secrets, list):
            raise SystemExit(2)
        target = _clean(result.get("Target"))
        for secret in secrets:
            if not isinstance(secret, dict):
                raise SystemExit(2)
            rows.append(
                (
                    target,
                    _clean(secret.get("RuleID")),
                    _clean(secret.get("Severity")),
                    _line(secret.get("StartLine")),
                )
            )
    return rows


def trivy_version(report: dict) -> str:
    meta = report.get("Trivy")
    return _clean(meta.get("Version")) if isinstance(meta, dict) else "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", type=Path, help="trivy --format json output")
    parser.add_argument(
        "--label", required=True, help="what was scanned, for the summary"
    )
    parser.add_argument(
        "--stage",
        choices=sorted(COPY),
        default="release",
        help="which wording to print around the rows (default: release)",
    )
    args = parser.parse_args(argv)
    copy = COPY[args.stage]

    label = _clean(args.label)
    report = load_report(args.report)
    rows = findings(report)
    version = trivy_version(report)

    summary: list[str] = []
    if rows:
        print(f"{len(rows)} finding(s) in {label} (trivy {version}, secret scanner):")
        print("\t".join(FIELDS))
        summary.append(
            copy["heading"].format(n=len(rows), label=label, version=version)
        )
        summary.append("")
        summary.append("| " + " | ".join(FIELDS) + " |")
        summary.append("|" + "---|" * len(FIELDS))
        for row in rows:
            print("\t".join(row))
            summary.append(
                "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |"
            )
        print(copy["error"].format(n=len(rows), label=label))
    else:
        line = copy["clean"].format(label=label, version=version)
        print(line)
        summary.append(line)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write("\n".join(summary) + "\n\n")
    return 1 if rows else 0


if __name__ == "__main__":
    sys.exit(main())
