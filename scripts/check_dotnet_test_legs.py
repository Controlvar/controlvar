#!/usr/bin/env python3
"""Refuse a .NET SDK test run that did not run every leg, or ran one short.

``dotnet test sdk/dotnet/Controlvar.sln --logger trx --results-directory DIR``
writes one TRX file per test project and target framework. The .NET SDK's
tests are one set of sources run three ways (``LEGS``), and a leg that runs
nothing passes as green as one that runs everything: a target framework
dropped from a project, a project dropped from the solution, a filter that
selects no test. So every leg must be present exactly once, completed, with
no failure, and with the same non-zero number of tests as the others.

Usage::

    python scripts/check_dotnet_test_legs.py DIR

Standard library only, so the runner's python3 is enough.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

#: (test assembly, the target framework it runs on): the SDK's net8.0 build on
#: the .NET 8 and .NET 10 runtimes, and its netstandard2.1 build on .NET 8.
#: backend/tests/smoke/test_sdk_release_nuget.py holds this to the test
#: projects' target frameworks.
LEGS = (
    ("controlvar.tests", "net8.0"),
    ("controlvar.tests", "net10.0"),
    ("controlvar.tests.netstandard", "net8.0"),
)
NAMESPACE = {"t": "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"}
COUNTERS = ("total", "executed", "passed", "failed", "error", "timeout", "aborted")


def read_leg(path: Path) -> tuple[tuple[str, str], dict[str, int], str]:
    """Return ``((assembly, framework), counters, outcome)`` of one TRX file.

    The leg is read from the test DLL's path, ``.../bin/<config>/<framework>/
    <assembly>.dll``; every test in the file must come from that one DLL.
    """
    root = ET.parse(path).getroot()
    storages = {
        PurePosixPath(test.get("storage", "").replace("\\", "/").lower())
        for test in root.iterfind("t:TestDefinitions/t:UnitTest", NAMESPACE)
    }
    if len(storages) != 1:
        raise ValueError(
            f"{path.name}: expected tests from one assembly, found {sorted(map(str, storages))}"
        )
    (dll,) = storages
    if dll.suffix != ".dll":
        raise ValueError(f"{path.name}: the tests come from {dll}, not a .dll")
    leg = (dll.stem, dll.parent.name)
    summary = root.find("t:ResultSummary", NAMESPACE)
    counters = summary.find("t:Counters", NAMESPACE) if summary is not None else None
    if summary is None or counters is None:
        raise ValueError(f"{path.name}: no ResultSummary/Counters")
    values = {name: int(counters.get(name, "0")) for name in COUNTERS}
    return leg, values, summary.get("outcome", "")


def problems(
    directory: Path,
) -> tuple[list[str], dict[tuple[str, str], dict[str, int]]]:
    found: list[str] = []
    legs: dict[tuple[str, str], dict[str, int]] = {}
    files = sorted(directory.glob("*.trx")) if directory.is_dir() else []
    if not files:
        return [f"no .trx file in {directory}"], legs
    for path in files:
        try:
            leg, counters, outcome = read_leg(path)
        except (ValueError, ET.ParseError) as error:
            found.append(str(error))
            continue
        if leg in legs:
            found.append(f"{path.name}: the leg {leg[0]} on {leg[1]} ran twice")
            continue
        legs[leg] = counters
        if outcome != "Completed":
            found.append(f"{leg[0]} on {leg[1]}: the run's outcome is {outcome!r}")
        bad = {
            k: counters[k]
            for k in ("failed", "error", "timeout", "aborted")
            if counters[k]
        }
        if bad:
            found.append(f"{leg[0]} on {leg[1]}: {bad}")
        if counters["total"] == 0 or counters["passed"] != counters["total"]:
            found.append(
                f"{leg[0]} on {leg[1]}: {counters['passed']} of {counters['total']} tests passed"
            )
    expected = set(LEGS)
    missing = sorted(expected - set(legs))
    extra = sorted(set(legs) - expected)
    if missing:
        found.append(f"expected results for {sorted(expected)}; missing {missing}")
    if extra:
        found.append(f"results for legs this check does not know: {extra}")
    totals = {legs[leg]["total"] for leg in expected if leg in legs}
    if len(totals) > 1:
        found.append(
            "the legs ran different numbers of tests: "
            + ", ".join(
                f"{a} on {f}: {legs[(a, f)]['total']}"
                for a, f in sorted(expected)
                if (a, f) in legs
            )
        )
    return found, legs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "directory", type=Path, help="The --results-directory of dotnet test."
    )
    args = parser.parse_args(argv)
    found, legs = problems(args.directory)
    for problem in found:
        print(f"error: {problem}", file=sys.stderr)
    if found:
        return 1
    for (assembly, framework), counters in sorted(legs.items()):
        print(
            f"{assembly} on {framework}: {counters['passed']} of {counters['total']} passed"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
