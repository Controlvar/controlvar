#!/usr/bin/env python3
"""Fail unless a packaged Helm chart is the release it is about to be attached to.

``release.yml``'s ``chart`` job runs ``helm package charts/experimently`` and
then this, before the ``.tgz`` goes anywhere:

    python3 scripts/check_chart_package.py dist/controlvar-X.Y.Z.tgz --expect X.Y.Z

It reads the ``Chart.yaml`` *inside the archive* -- what a user who downloads
the asset installs -- not the one in the tree, which ``check_version_sources.py``
has already compared with the tag. The two can differ: ``helm package`` takes
``--version`` and ``--app-version`` overrides, and it packages whatever the
checkout holds.

Checked, each exactly (image tags and chart versions are strings, not PEP 440
versions, so ``0.1.0rc1`` is not ``0.1.0-rc.1``):

* the archive holds exactly one ``Chart.yaml``, at ``controlvar/Chart.yaml``;
* its ``name`` is ``controlvar``;
* its ``version`` and ``appVersion`` are both the expected version -- the
  chart's default image tag is ``<profile>-<appVersion>``, so a stale
  ``appVersion`` installs the previous release's images;
* the file is called ``controlvar-<version>.tgz``, the name ``helm package``
  gives it and the one the release asset is published under.

Standard library only: ``helm package`` rewrites ``Chart.yaml`` as plain
top-level ``key: value`` lines (comments dropped, strings quoted only when YAML
needs it), and the release job installs nothing to read them.
"""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
from pathlib import Path

CHART_NAME = "controlvar"
CHART_YAML = f"{CHART_NAME}/Chart.yaml"


def _field(text: str, field: str) -> str:
    """One top-level scalar, unquoted. Missing or repeated is a failure."""
    pattern = re.compile(
        rf"""^{field}:[ \t]*(?:"([^"\n]*)"|'([^'\n]*)'|([^\s#'"][^\s#]*))[ \t]*$"""
    )
    values = [
        next(g for g in m.groups() if g is not None)
        for line in text.splitlines()
        if (m := pattern.match(line))
    ]
    if len(values) != 1:
        raise ValueError(
            f"expected exactly one top-level `{field}:` line in {CHART_YAML}, "
            f"found {len(values)}"
        )
    return values[0]


def read_packaged_chart(archive: Path) -> dict[str, str]:
    with tarfile.open(archive, "r:gz") as tar:
        charts = [m for m in tar.getmembers() if m.name.endswith("/Chart.yaml")]
        # Only the top-level chart may carry a Chart.yaml: this chart has no
        # subcharts, and one appearing is a packaging change worth a look.
        if [m.name for m in charts] != [CHART_YAML]:
            raise ValueError(
                f"expected exactly one Chart.yaml, at {CHART_YAML}; "
                f"found {[m.name for m in charts]}"
            )
        handle = tar.extractfile(charts[0])
        if handle is None:
            raise ValueError(f"{CHART_YAML} in the archive is not a regular file")
        text = handle.read().decode("utf-8")
    return {f: _field(text, f) for f in ("name", "version", "appVersion")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path, help="the .tgz helm package wrote")
    parser.add_argument(
        "--expect",
        required=True,
        metavar="X.Y.Z",
        help="the release version (the tag without its leading 'v')",
    )
    args = parser.parse_args(argv)
    expected = args.expect

    try:
        chart = read_packaged_chart(args.archive)
    except (OSError, tarfile.TarError, ValueError, UnicodeDecodeError) as exc:
        print(f"error: {args.archive}: {exc}", file=sys.stderr)
        return 1

    for key, value in chart.items():
        print(f"  {key:12} {value}")

    failures = []
    if chart["name"] != CHART_NAME:
        failures.append(f"name is {chart['name']!r}, expected {CHART_NAME!r}")
    for key in ("version", "appVersion"):
        if chart[key] != expected:
            failures.append(f"{key} is {chart[key]!r}, expected {expected!r}")
    wanted_file = f"{CHART_NAME}-{expected}.tgz"
    if args.archive.name != wanted_file:
        failures.append(f"file is {args.archive.name!r}, expected {wanted_file!r}")

    if failures:
        for failure in failures:
            print(f"error: packaged chart: {failure}", file=sys.stderr)
        return 1
    print(f"packaged chart is {CHART_NAME} {expected}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
