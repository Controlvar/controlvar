#!/usr/bin/env python3
"""Fail when the dashboard's production npm closure gains a licence we cannot ship.

The Python side of this already exists: ``scripts/core_build.sh``'s ``licences``
step checks the transitive closure of ``backend/requirements/runtime.txt``
against an allow-list of exact licence names, and runs in the ``core-build``
and ``full-build`` jobs. Node had no equivalent -- ``license-checker`` was used
only to *generate* ``THIRD_PARTY_LICENSES.md``, never to gate -- so a
dependency arriving under GPL or SSPL would have been reported and merged.

An allow-list of exact names, for the same reason ``core_build.sh`` spells one
out rather than using ``pip-licenses --fail-on``: substring matching on licence
names is wrong in both directions. ``GPL`` matches ``LGPL-3.0-or-later`` and
``AGPL-3.0``, so a deny-list of ``GPL`` fails a package that is merely LGPL;
and a permissive allow-list entry can swallow a copyleft name it happens to
contain. Exact names, checked one at a time.

``--production``: devDependencies do not reach the image. The dashboard image
serves a static export, so what ships is the build output of this closure.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


#: Every Node closure that is published to npm or shipped in an image. The
#: same ones ``scripts/generate_third_party_licenses.py`` enumerates, and for
#: the same reason: those are the trees whose licences reach somebody else.
#: The first version of this gate checked only ``frontend``, which left the
#: five SDKs that are actually *published to users* ungated -- a GPL or SSPL
#: dependency arriving in ``sdk/js`` would have been redistributed to every
#: consumer, which is precisely what this script says it exists to stop.
def _node_packages() -> tuple[str, ...]:
    """Derived, not typed: the dashboard plus every SDK with a lockfile.

    The list existed in three places -- here, in
    ``scripts/generate_third_party_licenses.py``, and as a literal in the test
    that claimed to compare them -- with nothing tying them together, so a
    seventh SDK would have been ungated while every copy stayed green. That is
    the hand-typed-list-beside-a-growing-directory defect this repository keeps
    finding; a directory listing cannot go stale.

    A ``package-lock.json`` is the marker because it is what ``npm ci`` and
    license-checker need: an SDK without one is not resolvable and not
    publishable.
    """
    sdks = sorted(
        f"sdk/{entry.name}"
        for entry in (ROOT / "sdk").iterdir()
        if entry.is_dir() and (entry / "package-lock.json").is_file()
    )
    return ("frontend", *sdks)


NODE_PACKAGES = _node_packages()

#: Pinned, because an unpinned resolver is the drift this repository keeps
#: paying for. `npx --yes license-checker-rseidelsohn` resolves the latest
#: release on every run, so a change to how it spells a licence, or to what it
#: includes, rewrites rows in a legal document and turns a green gate red on a
#: pull request that changed no dependency. `pip-licenses` is pinned twice
#: already (`Makefile:PIP_LICENSES_VERSION`, `pr-qa-gate.yml`); this was the
#: one resolver still floating.
LICENSE_CHECKER = "license-checker-rseidelsohn@5.0.1"

#: Exact SPDX identifiers (and the spellings license-checker actually emits)
#: that an Apache-2.0 distribution can carry.
#:
#: LGPL is on the list for the reason psycopg2 is on the Python one: weak
#: copyleft, linked and not modified. GPL, AGPL and SSPL are not, because any
#: of them would bind the distribution.
#:
#: CC-BY-4.0 is here for `caniuse-lite`, which is a *data* package -- browser
#: support tables, no code -- and attribution is satisfied by
#: THIRD_PARTY_LICENSES.md.
#:
#: Bare `BSD` is unqualified -- it could be the 2- or the 3-clause text -- and
#: is accepted because both are permissive and both are already on this list
#: individually. `readline@1.3.0` (reached through react-native's tree in
#: sdk/react-native) declares exactly that, and `scripts/core_build.sh`'s
#: Python allow-list already carries `BSD` and `BSD License` for the same
#: packages-that-predate-SPDX reason. license-checker appends `*` to an
#: identifier it had to infer or could not validate as SPDX; `is_allowed`
#: strips it and judges the name.
ALLOWED = frozenset(
    {
        "0BSD",
        "Apache-2.0",
        "Apache-2.0 WITH LLVM-exception",
        "BlueOak-1.0.0",
        "BSD",  # unqualified; see the note below
        "BSD-2-Clause",
        "BSD-3-Clause",
        "CC-BY-4.0",
        "CC0-1.0",
        "ISC",
        "MIT",
        "MIT AND ISC",
        "MPL-2.0",
        "Python-2.0",
        "Unlicense",
        "WTFPL",
        "LGPL-3.0-or-later",
    }
)


def licences_of(directory: Path) -> dict[str, str]:
    """``{package@version: licence expression}`` for a package's production tree.

    Two details that the first version of this got wrong, both of which
    ``scripts/generate_third_party_licenses.py`` already handled -- evidence
    that they occur in this tree:

    * ``licenses`` may be a **list** (the legacy manifest form). ``str()`` on
      it produces ``"['MIT', 'ISC']"``, which matches no allow-list entry and
      failed the build on a correctly-licensed package.
    * ``--excludePrivatePackages`` drops the workspace's own package, which
      license-checker reports as ``UNLICENSED`` purely because it is
      ``private: true``. That removes the need for a name list to exempt it,
      and with it a guard that was inverted.
    """
    # Without node_modules, license-checker has nothing to read and reports
    # exactly one entry -- the package itself -- then exits 0. Measured: the
    # first version of this gate ran in a CI job that only does
    # `cd frontend && npm ci`, so the five SDK closures it had just been
    # widened to cover each reported "1 production packages, all allowed" and
    # the gate was decorative for everything it was added for. The
    # `readline@1.3.0: BSD*` it appeared to find was only findable on a laptop
    # where node_modules happened to exist -- a gate that passes because the
    # environment running it is privileged.
    if not (directory / "node_modules").is_dir():
        raise SystemExit(
            f"{directory.name}: no node_modules -- run `npm ci` here first. "
            "license-checker would otherwise report only the root package and "
            "this gate would pass without checking anything."
        )
    result = subprocess.run(
        [
            "npx",
            "--yes",
            LICENSE_CHECKER,
            "--production",
            "--json",
            "--excludePrivatePackages",
        ],
        cwd=directory,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise SystemExit(
            f"license-checker failed in {directory}:\n{result.stderr.strip()[-2000:]}"
        )
    report = json.loads(result.stdout)
    licences: dict[str, str] = {}
    for name, info in report.items():
        licences[name] = normalise(info.get("licenses", "UNKNOWN"))
    return licences


#: Spellings license-checker passes straight through from a manifest, mapped to
#: the SPDX identifier they mean. Taken from ``normalise()`` in
#: ``scripts/generate_third_party_licenses.py``, which has carried them for as
#: long as that report has existed -- evidence they occur.
_SPELLINGS = {
    "Apache License, Version 2.0": "Apache-2.0",
    "Apache License 2.0": "Apache-2.0",
    "Apache Software License": "Apache-2.0",
    "Apache 2.0": "Apache-2.0",
    "BSD License": "BSD",
    "MIT License": "MIT",
    "ISC License (ISCL)": "ISC",
    "The Unlicense (Unlicense)": "Unlicense",
    "Public Domain": "CC0-1.0",
}


def normalise(raw: object) -> str:
    """license-checker's ``licenses`` field, as one expression string.

    Three shapes, all real. A plain string; a list of strings (the legacy
    ``"licenses": ["MIT", "ISC"]`` manifest form); and a list of **objects**
    (``[{"type": "MIT", "url": ...}]``), which is the older form still. The
    first version of this handled only the middle one, so an MIT package
    declaring the object form produced ``"{'type': 'MIT', ...}"`` and the
    blocking gate rejected it -- the same defect it was written to fix, one
    shape deeper.
    """
    if isinstance(raw, dict):
        raw = raw.get("type", "UNKNOWN")
    if isinstance(raw, list):
        return " OR ".join(normalise(item) for item in raw) or "UNKNOWN"
    text = str(raw).strip()
    return _SPELLINGS.get(text, text)


def is_allowed(expression: str) -> bool:
    """Whether an SPDX-ish expression is satisfied by :data:`ALLOWED`.

    OR means any term suffices; AND means every term must. The first version
    claimed to do this and only implemented OR -- ``MIT AND ISC`` passed purely
    because someone had added that literal string to the allow-list, so
    ``MIT AND GPL-3.0`` would have passed too had it been spelled that way.

    A trailing ``*`` is license-checker's marker for a licence it *inferred*
    from the package contents rather than read from a field (``MIT*``). The
    inference is not evidence, but the licence it names is still the one to
    judge, so the marker is stripped and the name checked normally.
    """
    cleaned = expression.strip().strip("()").strip()
    if not cleaned:
        return False
    # A mixed expression is REFUSED rather than parsed. The comment here used
    # to say that was what happened; it was not -- splitting on " AND " first
    # inverted SPDX precedence, so `MIT AND ISC OR GPL-3.0` came back allowed.
    # Implementing precedence for a case no package in this tree has is the
    # clever option; refusing it, so a human reads the licence, is the dumb one
    # that cannot be silently wrong.
    if " AND " in cleaned and " OR " in cleaned:
        return False
    if " AND " in cleaned:
        return all(is_allowed(term) for term in cleaned.split(" AND "))
    if " OR " in cleaned:
        return any(is_allowed(term) for term in cleaned.split(" OR "))
    return cleaned.rstrip("*").strip() in ALLOWED


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directories",
        nargs="*",
        default=None,
        help=(
            "npm package directories to check. Defaults to every closure that "
            "is published or shipped (see NODE_PACKAGES)."
        ),
    )
    args = parser.parse_args()
    directories = args.directories or list(NODE_PACKAGES)

    offenders: list[str] = []
    for relative in directories:
        directory = ROOT / relative
        if not (directory / "package.json").is_file():
            raise SystemExit(f"{relative} has no package.json")

        found = licences_of(directory)
        bad = [
            f"{relative}: {package}: {expression}"
            for package, expression in sorted(found.items())
            if not is_allowed(expression)
        ]
        status = f"{len(bad)} not allowed" if bad else "all allowed"
        print(f"  {relative:20} {len(found):4} production packages, {status}")
        offenders.extend(bad)

    if offenders:
        print(
            "error: licence(s) not on the allow-list for an Apache-2.0 distribution:",
            file=sys.stderr,
        )
        for offender in offenders:
            print(f"    {offender}", file=sys.stderr)
        print(
            "  Add the exact identifier to ALLOWED in this script only after "
            "reading the licence, or drop the dependency.",
            file=sys.stderr,
        )
        return 1

    print(f"every production licence across {len(directories)} package(s) is allowed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
