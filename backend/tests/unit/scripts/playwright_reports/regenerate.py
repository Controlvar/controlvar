"""Write the Playwright JSON reports beside this file, which the tests of
``scripts/rc_evidence.py``'s converter read (``playwright_junit``).

    python3 backend/tests/unit/scripts/playwright_reports/regenerate.py probe
        [--node-modules DIR]
    python3 backend/tests/unit/scripts/playwright_reports/regenerate.py real RUN_JSON

``probe`` runs Playwright 1.64.0 (frontend/package-lock.json's) over
``probe/``, whose tests open no page, so no browser is downloaded or launched:
it copies ``probe/`` into a new directory, installs ``@playwright/test@1.64.0``
there with ``npm install --ignore-scripts`` (or links ``--node-modules``, a
directory that holds that install), runs each command in ``PROBE_RUNS`` from
the copy's ``frontend/`` with ``PLAYWRIGHT_JSON_OUTPUT_FILE`` set, and writes
each report here, trimmed (``trim``) and with the copy's directory written
``/work/controlvar`` (and the directory above it ``/work``).

``real`` trims a page-runtime run report that Browser E2E kept
(``page-runtime-reports``, ``page-runtime-run.json``) into
``page-runtime-run.json`` here, paths unchanged, and writes the converter's
JUnit of it, ``page-runtime.xml``, the golden the regression test judges. The
one here is from run 45525986412 (pull request #2135; Playwright 1.64.0, 106
tests, each passed first time).

``trim`` keeps what the converter reads and drops the rest: of the config,
``rootDir`` and ``version`` (the rest holds the CI's metadata, including the
pull request's diff); of each suite, its title, file, specs and suites; of each
spec, its title, file, line and tests; of each test, its project, status,
expected status, annotations and results; of each result, its status, retry,
errors and annotations; of each error, its message and location; and the
top-level errors and stats.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Tuple

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[4]
PLAYWRIGHT = "@playwright/test@1.64.0"
#: The real report's repository root, on GitHub's runner.
RUNNER_ROOT = "/home/runner/work/controlvar/controlvar"
#: What the copy's directory, and the directory above it, are written as.
COPY_AS, WORK_AS = "/work/controlvar", "/work"

#: Each report, and the arguments of ``npx playwright test`` that write it.
PROBE_RUNS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("outcomes.json", ("--project=page-runtime", "--reporter=json")),
    (
        "two-projects.json",
        ("--project=page-runtime", "--project=extended", "--reporter=json"),
    ),
    ("listing.json", ("--project=page-runtime", "--list", "--reporter=json")),
    (
        "no-tests.json",
        (
            "--project=page-runtime",
            "--grep",
            "no test has this title",
            "--reporter=json",
        ),
    ),
    ("load-error.json", ("--project=load-error", "--reporter=json")),
    ("duplicate-title.json", ("--project=duplicate", "--reporter=json")),
    ("server-down.json", ("-c", "server-down.config.ts", "--reporter=json")),
)


def _pick(data: Dict[str, Any], keys: Tuple[str, ...]) -> Dict[str, Any]:
    return {k: data[k] for k in keys if k in data}


def _error(error: Dict[str, Any]) -> Dict[str, Any]:
    return _pick(error, ("message", "location"))


def _suite(suite: Dict[str, Any]) -> Dict[str, Any]:
    out = _pick(suite, ("title", "file"))
    out["specs"] = [
        dict(
            _pick(spec, ("title", "file", "line")),
            tests=[
                dict(
                    _pick(
                        test, ("projectName", "status", "expectedStatus", "annotations")
                    ),
                    results=[
                        dict(
                            _pick(result, ("status", "retry", "annotations")),
                            errors=[_error(e) for e in result.get("errors", [])],
                        )
                        for result in test.get("results", [])
                    ],
                )
                for test in spec.get("tests", [])
            ],
        )
        for spec in suite.get("specs", [])
    ]
    out["suites"] = [_suite(s) for s in suite.get("suites", [])]
    return out


def trim(report: Dict[str, Any]) -> Dict[str, Any]:
    """What the converter reads of a Playwright JSON report (the docstring)."""
    return {
        "config": _pick(report.get("config", {}), ("rootDir", "version")),
        "suites": [_suite(s) for s in report.get("suites", [])],
        "errors": [_error(e) for e in report.get("errors", [])],
        "stats": report.get("stats", {}),
    }


def _write(name: str, report: Dict[str, Any]) -> None:
    (HERE / name).write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")


def _rc_evidence() -> Any:
    path = ROOT / "scripts" / "rc_evidence.py"
    spec = importlib.util.spec_from_file_location("rc_evidence_regenerate", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def real(run_json: pathlib.Path) -> None:
    _write("page-runtime-run.json", trim(json.loads(run_json.read_text("utf-8"))))
    count = _rc_evidence().playwright_junit(
        HERE / "page-runtime-run.json",
        HERE / "page-runtime.xml",
        "page-runtime",
        pathlib.Path(RUNNER_ROOT),
    )
    print(f"page-runtime.xml: {count} cases")


def probe(node_modules: pathlib.Path | None) -> None:
    work = pathlib.Path(tempfile.mkdtemp(prefix="playwright-probe-")).resolve()
    try:
        copy = work / "repo"
        shutil.copytree(HERE / "probe", copy)
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "CI" and not k.startswith("PLAYWRIGHT_")
        }
        env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
        replaced: List[Tuple[str, str]] = [(str(copy), COPY_AS)]
        if node_modules is None:
            (work / "package.json").write_text('{"private": true}\n')
            subprocess.run(
                [
                    "npm",
                    "install",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    PLAYWRIGHT,
                ],
                cwd=work,
                env=env,
                check=True,
            )
        else:
            target = node_modules.resolve()
            (work / "node_modules").symlink_to(target)
            replaced.append((str(target), f"{WORK_AS}/node_modules"))
        replaced.append((str(work), WORK_AS))
        for name, args in PROBE_RUNS:
            out = work / name
            done = subprocess.run(
                ["npx", "playwright", "test", *args],
                cwd=copy / "frontend",
                env=dict(env, PLAYWRIGHT_JSON_OUTPUT_FILE=str(out)),
                capture_output=True,
                text=True,
                check=False,
            )
            text = json.dumps(trim(json.loads(out.read_text(encoding="utf-8"))))
            for path, written in replaced:
                text = text.replace(path, written)
            for private in (str(pathlib.Path.home()), "/private/", "/var/folders"):
                if private in text:
                    raise SystemExit(f"{name}: still names {private!r}")
            _write(name, json.loads(text))
            print(
                f"{name}: npx playwright test {' '.join(args)}: exit {done.returncode}"
            )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="what", required=True)
    one = sub.add_parser("probe")
    one.add_argument("--node-modules", type=pathlib.Path)
    two = sub.add_parser("real")
    two.add_argument("run_json", type=pathlib.Path)
    args = parser.parse_args()
    if args.what == "probe":
        probe(args.node_modules)
    else:
        real(args.run_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
