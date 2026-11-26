"""Check a persona-days run and print one line of counts.

The nightly ``persona-days`` job (.github/workflows/nightly-qa.yml) runs the
``personas`` Playwright project with its output in files on the runner, then
runs this over those files:

``--list``
    ``PERSONAS=1 npx playwright test --list --project=personas --reporter=json``
``--report``
    the run's JSON report (``PERSONAS_REPORT``, playwright.config.ts)
``--oracle-log``
    the oracle log the days wrote (``PERSONAS_ORACLE_LOG``,
    frontend/tests/e2e/oracles/oracle.ts)
``--list-exit``, ``--run-exit``
    the two Playwright commands' exit statuses
``--details``
    a file that receives the reason for every failed check, one per line
``--backend-log``
    the API's own log for the job (the nightly job's ``backend.log``), whose
    access lines show every sign-in it was sent, however it was made

It prints exactly one line, to stdout and to ``$GITHUB_STEP_SUMMARY``::

    persona-days: listed <l> pinned <N> passed <p> failed <f> flaky <k> skipped <s> checks <c>

The line names no test, persona or step; the reasons go to ``--details`` only,
which stays on the machine that ran the days. ``checks`` counts the day steps
(and the other things below) with a failed check:

* every layer-A step of every card (tests/personas/personas/*.yaml) is
  exactly one ``test.step`` titled ``U-<ID> day <n>: <intent>``, in a test
  titled ``U-<ID>: ...``, and no ``test.step`` lacks a card line;
* no step of a day that passed carries an error, nested steps included: a
  step whose error the spec caught (``test.step(...).catch(...)``) is a
  failed check, not a pass;
* every oracle a step's card names was decided, with that kind and passing,
  in the oracle log, and the log holds no oracle no card names;
* every entry of frontend/tests/e2e/oracles/known-noise.json occurred;
* no 60 seconds of the run hold more sign-ins (the log's ``signin`` lines)
  than ``SIGN_IN_BUDGET``, one under the API's sign-in limit, and with
  ``--backend-log`` the API logged exactly that many sign-in requests: one
  sent around frontend/tests/e2e/oracles/signin.ts (unpaced, unlogged) is a
  failed check, whatever built its URL;
* the run ran every listed test and reported no error of its own.

The run passes (exit 0) only when both Playwright commands exited 0, the
listed count equals the pinned one (tests/personas/pinned.yaml, at least 1),
and no test failed, was flaky or was skipped, and no check failed. Otherwise
it exits 1. Any error of its own prints ``persona-days: check_run failed`` and
exits 2: never the error's text, which could quote a title.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import catalogue
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PINNED = HERE / "pinned.yaml"
KNOWN_NOISE = ROOT / "frontend" / "tests" / "e2e" / "oracles" / "known-noise.json"

PROJECT = "personas"
LINE = (
    "persona-days: listed {listed} pinned {pinned} passed {passed} failed {failed} "
    "flaky {flaky} skipped {skipped} checks {checks}"
)
REFUSED = "persona-days: pinned {pinned}: refused"
FAILED = "persona-days: check_run failed"
NOISE_KINDS = frozenset({"pageerror", "console", "response", "text"})
#: POST /api/v1/auth/login: 10 a minute per address (RATE_LIMIT_CONFIG in
#: backend/app/middleware/rate_limiter.py), and every day signs in from one.
#: frontend/tests/e2e/oracles/signin.ts paces the days to one under it.
SIGN_IN_LIMIT = 10
SIGN_IN_WINDOW_S = 60
SIGN_IN_BUDGET = SIGN_IN_LIMIT - 1
#: The JSON reporter's test outcomes and attempt results; anything else is an error.
TEST_STATUSES = frozenset({"expected", "unexpected", "flaky", "skipped"})
RESULT_STATUSES = frozenset(
    {None, "passed", "failed", "timedOut", "skipped", "interrupted"}
)

_TEST_TITLE = re.compile(r"^(U-[A-Z]+): ")
_STEP_TITLE = re.compile(r"^(U-[A-Z]+) day (\d+): (.+)$")


@dataclass
class Outcome:
    listed: int
    pinned: int
    passed: int = 0
    failed: int = 0
    flaky: int = 0
    skipped: int = 0
    #: one reason per failed check, keyed so a day step counts once
    problems: dict[tuple[str, str], str] = field(default_factory=dict)
    list_exit: int = 0
    run_exit: int = 0

    def problem(self, key: tuple[str, str], reason: str) -> None:
        self.problems.setdefault(key, reason)

    @property
    def checks(self) -> int:
        return len(self.problems)

    @property
    def ok(self) -> bool:
        return (
            self.list_exit == 0
            and self.run_exit == 0
            and self.pinned > 0
            and self.listed == self.pinned
            and self.failed == self.flaky == self.skipped == self.checks == 0
        )


def summary_line(outcome: Outcome) -> str:
    """The one public line: counts only."""
    if outcome.pinned < 1:
        return REFUSED.format(pinned=outcome.pinned)
    return LINE.format(
        listed=outcome.listed,
        pinned=outcome.pinned,
        passed=outcome.passed,
        failed=outcome.failed,
        flaky=outcome.flaky,
        skipped=outcome.skipped,
        checks=outcome.checks,
    )


# ------------------------------------------------------------------ the inputs


def project_tests(report: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every (test title, test) of the personas project in a JSON report."""

    def walk(suite: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
        for spec in suite.get("specs", []):
            for test in spec["tests"]:
                if test["projectName"] == PROJECT:
                    yield spec["title"], test
        for child in suite.get("suites", []):
            yield from walk(child)

    for suite in report["suites"]:
        yield from walk(suite)


def step_titles(steps: list[dict[str, Any]]) -> Iterator[str]:
    for step in steps:
        yield step["title"]
        yield from step_titles(step.get("steps", []))


def busiest_minute(times_ms: list[Any]) -> int:
    """The most sign-ins (epoch milliseconds) any SIGN_IN_WINDOW_S holds."""
    times = sorted(float(t) for t in times_ms)
    window = SIGN_IN_WINDOW_S * 1000
    most, start = 0, 0
    for end, at in enumerate(times):
        while at - times[start] >= window:
            start += 1
        most = max(most, end - start + 1)
    return most


def caught_step_errors(
    steps: list[dict[str, Any]], day: str | None = None
) -> Iterator[str]:
    """For each step, nested ones too, that carries an ``error``: the title
    of the day step it is in (its own title when it is in none)."""
    for step in steps:
        here = day
        if here is None and _STEP_TITLE.match(step["title"]):
            here = step["title"]
        if step.get("error"):
            yield here or step["title"]
        yield from caught_step_errors(step.get("steps", []), here)


def read_oracle_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            entry = json.loads(raw)
            if not isinstance(entry, dict):
                raise ValueError("an oracle log line is not an object")
            lines.append(entry)
    return lines


#: A colour code of the API's console log lines.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
#: uvicorn's access line for a sign-in, in the console or the JSON log.
_SIGN_IN_REQUEST = re.compile(
    r'\\?"POST /api/v1/auth/login(?:\?[^ "\\]*)? HTTP/[0-9.]+\\?" [0-9]{3}'
)


def api_sign_ins(path: Path) -> int:
    """How many sign-in requests the API's log shows it answered."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return sum(
        1 for line in text.splitlines() if _SIGN_IN_REQUEST.search(_ANSI.sub("", line))
    )


def read_pinned(path: Path) -> int:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    tests = data["tests"] if isinstance(data, dict) else None
    if not isinstance(tests, int) or isinstance(tests, bool):
        raise ValueError("pinned.yaml: tests is not a whole number")
    return tests


def read_known_noise(path: Path) -> list[dict[str, Any]]:
    entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    for entry in entries:
        issue, kind, match = entry["issue"], entry["kind"], entry["match"]
        if not isinstance(issue, int) or isinstance(issue, bool) or issue < 1:
            raise ValueError("known-noise: an entry's issue is not an issue number")
        if kind not in NOISE_KINDS or not isinstance(match, str) or not match:
            raise ValueError("known-noise: an entry's kind or match is not usable")
    return entries


# ------------------------------------------------------------------ the checks


def evaluate(
    *,
    list_report: dict[str, Any],
    report: dict[str, Any],
    personas: list[catalogue.Persona],
    oracle_log: list[dict[str, Any]],
    known_noise: list[dict[str, Any]],
    pinned: int,
    list_exit: int,
    run_exit: int,
    api_sign_ins: int | None = None,
) -> Outcome:
    """Count the run and run every check; nothing here prints."""
    outcome = Outcome(
        listed=sum(1 for _ in project_tests(list_report)),
        pinned=pinned,
        list_exit=list_exit,
        run_exit=run_exit,
    )
    cards = {p.id: p for p in personas}
    steps = {(p.id, s.n): s for p in personas for s in p.day if s.layer == "A"}
    oracle_owner = {
        o.id: (p.id, s.n, o.kind) for p in personas for s in p.day for o in s.oracles
    }

    if report.get("errors"):
        outcome.problem(("run", "errors"), "the run reported errors of its own")

    ran = 0
    tested: set[str] = set()  # personas with a test in the run
    complete: set[str] = set()  # ...whose day ran to the end and passed
    clean: set[str] = set()  # ...and passed at the first attempt
    seen: dict[tuple[str, int], int] = {}
    for title, test in project_tests(report):
        ran += 1
        status = test["status"]
        results = test.get("results") or []
        final = results[-1]["status"] if results else None
        if status not in TEST_STATUSES or final not in RESULT_STATUSES:
            raise ValueError("a test or result status Playwright does not write")
        if status == "skipped":
            outcome.skipped += 1
        elif status == "flaky":
            outcome.flaky += 1
        elif status == "expected" and final == "passed":
            outcome.passed += 1
        else:
            outcome.failed += 1

        match = _TEST_TITLE.match(title)
        if not match or match.group(1) not in cards:
            outcome.problem(("test", title), f"test {title!r}: not a persona's day")
            continue
        persona = match.group(1)
        tested.add(persona)
        if final == "passed":
            complete.add(persona)
            if status == "expected":
                clean.add(persona)

        for step_title in step_titles(results[-1].get("steps", []) if results else []):
            parsed = _STEP_TITLE.match(step_title)
            if not parsed:
                outcome.problem(
                    ("step", step_title), f"test.step {step_title!r}: no card line"
                )
                continue
            who, n, intent = parsed.group(1), int(parsed.group(2)), parsed.group(3)
            where = f"{who} day {n}"
            key = (who, str(n))
            if who != persona:
                outcome.problem(key, f"{where}: a step of another persona's test")
                continue
            step = steps.get((who, n))
            if step is None:
                outcome.problem(key, f"{where}: no layer-A step on the card")
                continue
            seen[(who, n)] = seen.get((who, n), 0) + 1
            if seen[(who, n)] > 1:
                outcome.problem(key, f"{where}: more than one test.step")
            if intent != step.intent:
                outcome.problem(key, f"{where}: test.step title != card intent")

        # A day that passed with an error inside a step: the spec caught it.
        if final == "passed":
            for title in caught_step_errors(results[-1].get("steps", [])):
                parsed = _STEP_TITLE.match(title)
                if parsed:
                    key = (parsed.group(1), str(int(parsed.group(2))))
                    where = f"{parsed.group(1)} day {int(parsed.group(2))}"
                else:
                    key, where = ("step", title), f"test.step {title!r}"
                outcome.problem(
                    key, f"{where}: a test.step error was caught and the day passed"
                )

    if ran != outcome.listed:
        outcome.problem(("run", "count"), f"ran {ran} tests, listed {outcome.listed}")

    decided = {
        (e.get("id"), e.get("kind"))
        for e in oracle_log
        if "noise" not in e and "signin" not in e and e.get("pass") is True
    }
    for (who, n), step in sorted(steps.items()):
        if who not in tested:
            outcome.problem((who, str(n)), f"{who} day {n}: no test runs this day")
            continue
        if who not in complete:
            continue  # the day failed, counted above; its later steps never ran
        if (who, n) not in seen:
            outcome.problem(
                (who, str(n)), f"{who} day {n}: no test.step for this card step"
            )
        for oracle in step.oracles:
            if (oracle.id, oracle.kind) not in decided:
                outcome.problem(
                    (who, str(n)),
                    f"declared oracle {oracle.id} ({oracle.kind}) absent from the oracle log",
                )

    busiest = busiest_minute([e["at"] for e in oracle_log if "signin" in e])
    if busiest > SIGN_IN_BUDGET:
        outcome.problem(
            ("run", "sign-ins"),
            f"sign-ins: {busiest} within {SIGN_IN_WINDOW_S} s, more than the "
            f"{SIGN_IN_BUDGET} the limit of {SIGN_IN_LIMIT} leaves",
        )
    made = sum(1 for e in oracle_log if "signin" in e)
    if api_sign_ins is not None and api_sign_ins != made:
        outcome.problem(
            ("run", "sign-ins-api"),
            f"sign-ins: the API logged {api_sign_ins} sign-in requests, "
            f"oracles/signin.ts made {made}",
        )

    noise_seen: set[int] = set()
    for entry in oracle_log:
        if "signin" in entry:
            continue
        if "noise" in entry:
            noise_seen.add(entry["noise"])
            continue
        owner = oracle_owner.get(entry.get("id"))
        if owner is None:
            outcome.problem(
                ("oracle", str(entry.get("id"))),
                f"oracle {entry.get('id')} is on no card",
            )
        elif entry.get("pass") is not True and owner[0] in clean:
            outcome.problem(
                (owner[0], str(owner[1])),
                f"oracle {entry.get('id')} failed in a day that passed",
            )
    listed_issues = {e["issue"] for e in known_noise}
    for issue in sorted(listed_issues - noise_seen):
        outcome.problem(("noise", str(issue)), f"known-noise #{issue} did not occur")
    for issue in sorted(noise_seen - listed_issues):
        outcome.problem(
            ("noise", str(issue)), f"noise #{issue} is not in known-noise.json"
        )
    return outcome


# ------------------------------------------------------------------ the command


def _arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", type=Path, required=True)
    parser.add_argument("--list-exit", type=int, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--run-exit", type=int, required=True)
    parser.add_argument("--oracle-log", type=Path, required=True)
    parser.add_argument("--details", type=Path, required=True)
    parser.add_argument("--personas", type=Path, default=HERE / "personas")
    parser.add_argument("--pinned", type=Path, default=PINNED)
    parser.add_argument("--known-noise", type=Path, default=KNOWN_NOISE)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--backend-log", type=Path, default=None)
    return parser.parse_args(argv)


def _emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def run(argv: list[str]) -> int:
    args = _arguments(argv)
    outcome = evaluate(
        list_report=json.loads(args.list.read_text(encoding="utf-8")),
        report=json.loads(args.report.read_text(encoding="utf-8")),
        personas=catalogue.load_personas(args.personas, args.root),
        oracle_log=read_oracle_log(args.oracle_log),
        known_noise=read_known_noise(args.known_noise),
        pinned=read_pinned(args.pinned),
        list_exit=args.list_exit,
        run_exit=args.run_exit,
        api_sign_ins=None
        if args.backend_log is None
        else api_sign_ins(args.backend_log),
    )
    reasons = sorted(outcome.problems.values())
    if outcome.list_exit:
        reasons.append(f"--list exited {outcome.list_exit}")
    if outcome.run_exit:
        reasons.append(f"the run exited {outcome.run_exit}")
    args.details.write_text("".join(f"{r}\n" for r in reasons), encoding="utf-8")
    _emit(summary_line(outcome))
    return 0 if outcome.ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        return run(argv)
    except BaseException:
        # One fixed line, whatever went wrong: an error's text could quote a
        # title from the report. The traceback goes to the details file only.
        try:
            details = _details_path(argv)
            if details is not None:
                details.write_text(traceback.format_exc(), encoding="utf-8")
        except BaseException:
            pass
        try:
            _emit(FAILED)
        except BaseException:
            sys.stdout.write(FAILED + "\n")
        return 2


def _details_path(argv: list[str]) -> Path | None:
    if "--details" in argv:
        index = argv.index("--details")
        if index + 1 < len(argv):
            return Path(argv[index + 1])
    return None


if __name__ == "__main__":
    sys.exit(main())
