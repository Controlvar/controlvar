#!/usr/bin/env python3
"""Refuse an SDK tag that does not match the package it would publish.

SDKs version independently of the platform (``sdk/<name>/vX.Y.Z``, the Go
convention reused for all of them), so ``VERSION`` at the repository root says
nothing about them and ``scripts/check_version_sources.py`` cannot help.  What
can still go wrong is the same thing: a tag pushed without the version bump
committed, which publishes ``1.0.0`` a second time (PyPI and npm both refuse
that, *after* the workflow has already built and tested) or, worse, publishes
the previous version's code under the new tag's name in an ecosystem that
allows it.

So: read the manifest the publish step will read, and compare.

Usage::

    python scripts/check_sdk_version.py js --expect 1.2.3
    python scripts/check_sdk_version.py python            # just print it
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: How each SDK is published, and therefore which file carries its version.
#:
#: ``pypi`` / ``npm``   wired for the launch: the workflow builds and publishes.
#: ``tag-only``         Go: `go get` resolves the tag itself, there is no
#:                      registry upload, so the tag *is* the release.
#: ``unwired``          a real SDK with a real manifest, but no publishing
#:                      identity yet (Maven Central, NuGet, RubyGems, Hex,
#:                      pub.dev).  Tagging one is refused rather than ignored.
ECOSYSTEMS: dict[str, str] = {
    "python": "pypi",
    "openfeature-python": "pypi",
    "js": "npm",
    "edge": "npm",
    "openfeature": "npm",
    "react": "npm",
    "react-native": "npm",
    "go": "tag-only",
    "java": "unwired",
    "dotnet": "unwired",
    "ruby": "unwired",
    "php": "unwired",
    "elixir": "unwired",
    "flutter": "unwired",
    "android": "unwired",
    "ios": "unwired",
}


def read_manifest_version(sdk: str) -> tuple[str, str]:
    """Return ``(version, name)`` from the SDK's own manifest."""
    directory = ROOT / "sdk" / sdk
    if not directory.is_dir():
        raise SystemExit(f"error: sdk/{sdk} does not exist")

    package_json = directory / "package.json"
    if package_json.is_file():
        data = json.loads(package_json.read_text(encoding="utf-8"))
        return str(data["version"]), str(data["name"])

    pyproject = directory / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project = data["project"]
        if "version" not in project:
            raise SystemExit(
                f"error: sdk/{sdk}/pyproject.toml declares no static version "
                "(a dynamic one would have to be resolved by a build, which "
                "this check deliberately does not run)"
            )
        return str(project["version"]), str(project["name"])

    raise SystemExit(f"error: sdk/{sdk} has neither package.json nor pyproject.toml")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sdk", help="Directory name under sdk/, e.g. js or openfeature-python."
    )
    parser.add_argument(
        "--expect",
        metavar="X.Y.Z",
        help="Require this version, from the tag (any leading 'v' is stripped).",
    )
    parser.add_argument(
        "--print-ecosystem",
        action="store_true",
        help="Print the publishing ecosystem instead of the version.",
    )
    parser.add_argument(
        "--print-name",
        action="store_true",
        help=(
            "Print the registry name from the manifest. The directory name is "
            "not it: sdk/openfeature is @controlvar/openfeature-provider "
            "and sdk/js is @controlvar/js-sdk, so anything that builds a "
            "package name by pattern is wrong for at least one of them."
        ),
    )
    args = parser.parse_args()

    ecosystem = ECOSYSTEMS.get(args.sdk)
    if ecosystem is None:
        print(
            f"error: {args.sdk!r} is not an SDK. Known: "
            + ", ".join(sorted(ECOSYSTEMS)),
            file=sys.stderr,
        )
        return 1

    if args.print_ecosystem:
        print(ecosystem)
        return 0

    if args.print_name:
        if ecosystem in {"tag-only", "unwired"}:
            print(f"error: sdk/{args.sdk} has no registry name", file=sys.stderr)
            return 1
        print(read_manifest_version(args.sdk)[1])
        return 0

    if ecosystem == "unwired":
        print(
            f"error: sdk/{args.sdk} has no publishing identity yet, so a "
            f"{args.sdk} tag would build and then publish nothing. Wire it in "
            "sdk-release.yml and move it out of 'unwired' in "
            "scripts/check_sdk_version.py's ECOSYSTEMS, in the same change "
            "that registers the account.",
            file=sys.stderr,
        )
        return 1

    if ecosystem == "tag-only":
        # There is no manifest to check: the module path and the tag are the
        # whole publication. Nothing to compare, and nothing to get wrong.
        print(f"sdk/{args.sdk}: published by tag alone (no registry upload)")
        return 0

    version, name = read_manifest_version(args.sdk)
    print(f"sdk/{args.sdk}: {name} {version} ({ecosystem})")

    if args.expect is not None:
        expected = args.expect.lstrip("v")
        if expected != version:
            print(
                f"error: the tag says {expected!r} but sdk/{args.sdk}'s manifest "
                f"says {version!r}. Bump the manifest in the commit the tag points at.",
                file=sys.stderr,
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
