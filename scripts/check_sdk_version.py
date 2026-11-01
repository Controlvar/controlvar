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
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: How each SDK is published, and therefore which file carries its version.
#:
#: ``pypi`` / ``npm``   wired for the launch: the workflow builds and publishes.
#: ``maven``            the workflow builds, signs and uploads the bundle to
#:                      the Central Portal for validation; a person publishes
#:                      it there.  The version is the parent POM's, and every
#:                      module must name it as its parent's.
#: ``tag-only``         Go: `go get` resolves the tag itself, there is no
#:                      registry upload, so the tag *is* the release.
#: ``unwired``          a real SDK with a real manifest, but no publishing
#:                      identity yet (NuGet, RubyGems, Hex, pub.dev).
#:                      Tagging one is refused rather than ignored.
ECOSYSTEMS: dict[str, str] = {
    "python": "pypi",
    "openfeature-python": "pypi",
    "js": "npm",
    "edge": "npm",
    "openfeature": "npm",
    "react": "npm",
    "react-native": "npm",
    "go": "tag-only",
    "java": "maven",
    "dotnet": "unwired",
    "ruby": "unwired",
    "php": "unwired",
    "elixir": "unwired",
    "flutter": "unwired",
    "android": "unwired",
    "ios": "unwired",
}


POM_NAMESPACE = {"m": "http://maven.apache.org/POM/4.0.0"}


def _pom_text(pom: ET.Element, path: str) -> str:
    return (pom.findtext(path, default="", namespaces=POM_NAMESPACE) or "").strip()


def read_maven_version(sdk: str, directory: Path) -> tuple[str, str]:
    """Return ``(version, groupId:artifactId)`` from a parent POM.

    The parent's version is the one the tag names, and the bundle holds
    every module at it, so each module listed in ``<modules>`` must name
    that groupId, artifactId and version as its ``<parent>``, and must not
    give itself another groupId or version.
    """
    parent = ET.parse(directory / "pom.xml").getroot()
    group = _pom_text(parent, "m:groupId")
    artifact = _pom_text(parent, "m:artifactId")
    version = _pom_text(parent, "m:version")
    for field, value in (
        ("groupId", group),
        ("artifactId", artifact),
        ("version", version),
    ):
        if not value:
            raise SystemExit(f"error: sdk/{sdk}/pom.xml declares no <{field}>")

    modules = [
        (module.text or "").strip()
        for module in parent.findall("m:modules/m:module", POM_NAMESPACE)
    ]
    if not modules:
        raise SystemExit(f"error: sdk/{sdk}/pom.xml lists no <modules>")
    problems = []
    for module in modules:
        pom_path = directory / module / "pom.xml"
        if (
            not module
            or "/" in module
            or module.startswith(".")
            or not pom_path.is_file()
        ):
            problems.append(f"module {module!r} has no pom.xml beside the parent's")
            continue
        child = ET.parse(pom_path).getroot()
        named = (
            _pom_text(child, "m:parent/m:groupId"),
            _pom_text(child, "m:parent/m:artifactId"),
            _pom_text(child, "m:parent/m:version"),
        )
        if named != (group, artifact, version):
            problems.append(
                f"sdk/{sdk}/pom.xml is {group}:{artifact}:{version}, but module "
                f"{module}'s <parent> names {':'.join(named)}"
            )
        own_group = _pom_text(child, "m:groupId")
        if own_group and own_group != group:
            problems.append(
                f"module {module} sets groupId {own_group}; the bundle is {group}"
            )
        own_version = _pom_text(child, "m:version")
        if own_version and own_version != version:
            problems.append(
                f"module {module} sets version {own_version}; the parent is {version}"
            )
    if problems:
        raise SystemExit("error: " + "; ".join(problems))
    return version, f"{group}:{artifact}"


def read_manifest_version(sdk: str) -> tuple[str, str]:
    """Return ``(version, name)`` from the SDK's own manifest."""
    directory = ROOT / "sdk" / sdk
    if not directory.is_dir():
        raise SystemExit(f"error: sdk/{sdk} does not exist")

    if ECOSYSTEMS.get(sdk) == "maven":
        return read_maven_version(sdk, directory)

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
