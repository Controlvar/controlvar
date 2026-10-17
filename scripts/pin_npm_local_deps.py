#!/usr/bin/env python3
"""Make the package.json an npm SDK publishes install from the registry.

``sdk/openfeature`` depends on ``@controlvar/js-sdk`` as ``file:../js``,
so its tests run against the JS SDK in the same tree. ``npm pack`` copies that
spec into the tarball unchanged and ``npm publish`` does not resolve it, so a
release published as-is installs with a dangling link and fails at the first
``import`` (#687). The source keeps ``file:``; the release job runs this script
on its own checkout just before ``npm pack``.

Usage::

    python3 scripts/pin_npm_local_deps.py sdk/openfeature --rewrites-file out.txt
    python3 scripts/pin_npm_local_deps.py --check-only package/package.json
    python3 scripts/pin_npm_local_deps.py --print-peer-pins sdk/openfeature

Rewrite (the first form): every ``file:`` spec in ``dependencies``,
``optionalDependencies`` and ``peerDependencies`` becomes ``^<version>``, the
version read from the target directory's own package.json. Refused, exit 1,
with nothing written: a target that is missing or has no package.json, a
target with no plain version, a target whose ``name`` is not the dependency's
key. Then every spec in those three sections must be a registry range (see
``registry_spec_problem``); anything else is refused the same way. Each
rewrite is printed as ``name: file:../js -> ^0.1.0`` and, with
``--rewrites-file``, written there as ``name@<exact version>``, one per line,
so the workflow can ask npm whether that version exists. The file is written
(empty) when nothing was rewritten. ``devDependencies`` are not read:
consumers never install them.

``--check-only <package.json>``: only the registry-range check, for the
package.json extracted from the packed tarball. Writes nothing.

``--print-peer-pins <sdk-dir>``: one ``name@version`` per peer dependency,
the version taken from the SDK's own package-lock.json, so the release job's
install check uses the peer the SDK was tested with rather than whatever is
newest. A peer missing from the lock is refused.

Standard library only: the release job runs it on the runner's ``python3``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: The sections a consumer's ``npm install`` reads.
PUBLISHED_SECTIONS = ("dependencies", "optionalDependencies", "peerDependencies")

#: A plain version, with an optional pre-release. Build metadata is refused:
#: npm ignores it when matching, so ``^1.0.0+x`` reads as something it is not.
_PLAIN_VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")

#: The characters a semver range or a dist-tag is made of. An allow-list, not
#: a list of refused prefixes: ``file:``, ``link:``, ``workspace:``, ``npm:``,
#: ``git+``, ``github:``, ``http(s):``, ``user/repo`` and ``../dir`` all carry
#: a ``:`` or a ``/`` and so fall outside it, and so does any prefix nobody has
#: thought of yet.
_RANGE_CHARS = re.compile(r"[0-9A-Za-z.*^~<>=| +-]+")

#: npm reads a spec ending like this as a tarball on disk.
_TARBALL_SUFFIXES = (".tgz", ".tar.gz", ".tar")


class Refused(Exception):
    """A package.json this script will not let through."""


def registry_spec_problem(spec: object) -> str | None:
    """Why ``spec`` is not a registry range, or ``None`` when it is one."""
    if not isinstance(spec, str):
        return f"is {type(spec).__name__}, not a string"
    text = spec.strip()
    if not text:
        return "is empty (npm reads that as '*')"
    if not _RANGE_CHARS.fullmatch(text):
        return "is not a registry range (a path, URL or protocol spec)"
    if text.lower().endswith(_TARBALL_SUFFIXES):
        return "names a tarball"
    return None


def check_registry_only(data: dict, where: str) -> None:
    """Refuse any spec in the published sections that is not a registry range."""
    problems = []
    for section in PUBLISHED_SECTIONS:
        deps = data.get(section) or {}
        if not isinstance(deps, dict):
            problems.append(f"{where}: {section} is not an object")
            continue
        for name, spec in deps.items():
            problem = registry_spec_problem(spec)
            if problem:
                problems.append(f"{where}: {section}.{name} = {spec!r} {problem}")
    if problems:
        raise Refused("\n".join(problems))


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Refused(f"{path} does not exist") from None
    except json.JSONDecodeError as error:
        raise Refused(f"{path} is not valid JSON: {error}") from None
    if not isinstance(data, dict):
        raise Refused(f"{path} is not a JSON object")
    return data


def pin_manifest(data: dict, sdk_dir: Path) -> tuple[dict, list[tuple[str, str, str]]]:
    """Return ``(new manifest, [(name, old spec, exact version), ...])``.

    ``data`` is not modified. Raises ``Refused`` for a bad ``file:`` target or
    for any non-registry spec left after the rewrite.
    """
    pinned = json.loads(json.dumps(data))
    rewrites: list[tuple[str, str, str]] = []
    for section in PUBLISHED_SECTIONS:
        deps = pinned.get(section) or {}
        if not isinstance(deps, dict):
            continue  # reported by check_registry_only below
        for name, spec in deps.items():
            if not (isinstance(spec, str) and spec.startswith("file:")):
                continue
            target = (sdk_dir / spec.removeprefix("file:")).resolve()
            target_manifest = target / "package.json"
            if not target_manifest.is_file():
                raise Refused(
                    f"{section}.{name} = {spec!r}: {target} has no package.json"
                )
            target_data = _read_json(target_manifest)
            if target_data.get("name") != name:
                raise Refused(
                    f"{section}.{name} = {spec!r}: {target_manifest} is "
                    f"{target_data.get('name')!r}, not {name!r}"
                )
            version = target_data.get("version")
            if not (isinstance(version, str) and _PLAIN_VERSION.fullmatch(version)):
                raise Refused(
                    f"{section}.{name} = {spec!r}: {target_manifest} has no plain "
                    f"version (found {version!r})"
                )
            deps[name] = f"^{version}"
            rewrites.append((name, spec, version))
    check_registry_only(pinned, str(sdk_dir / "package.json"))
    return pinned, rewrites


def peer_pins(sdk_dir: Path) -> list[str]:
    """``name@version`` for each peer dependency, from the SDK's lock."""
    data = _read_json(sdk_dir / "package.json")
    peers = data.get("peerDependencies") or {}
    if not peers:
        return []
    lock = _read_json(sdk_dir / "package-lock.json")
    packages = lock.get("packages") or {}
    pins = []
    missing = []
    for name in peers:
        version = (packages.get(f"node_modules/{name}") or {}).get("version")
        if isinstance(version, str) and _PLAIN_VERSION.fullmatch(version):
            pins.append(f"{name}@{version}")
        else:
            missing.append(name)
    if missing:
        raise Refused(
            f"{sdk_dir / 'package-lock.json'} records no version for peer "
            f"dependenc{'y' if len(missing) == 1 else 'ies'} {', '.join(missing)}"
        )
    return pins


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("sdk_dir", nargs="?", type=Path, help="SDK directory to rewrite")
    mode.add_argument("--check-only", type=Path, metavar="PACKAGE_JSON")
    mode.add_argument("--print-peer-pins", type=Path, metavar="SDK_DIR")
    parser.add_argument(
        "--rewrites-file",
        type=Path,
        help="write name@version per rewritten dependency here (empty when none)",
    )
    args = parser.parse_args(argv)

    try:
        if args.check_only is not None:
            check_registry_only(_read_json(args.check_only), str(args.check_only))
            print(f"{args.check_only}: registry ranges only")
            return 0
        if args.print_peer_pins is not None:
            for pin in peer_pins(args.print_peer_pins):
                print(pin)
            return 0

        manifest = args.sdk_dir / "package.json"
        pinned, rewrites = pin_manifest(_read_json(manifest), args.sdk_dir)
        if rewrites:
            manifest.write_text(json.dumps(pinned, indent=2) + "\n", encoding="utf-8")
        for name, old, version in rewrites:
            print(f"{name}: {old} -> ^{version}")
        if not rewrites:
            print(f"{manifest}: no local dependency to rewrite")
        if args.rewrites_file is not None:
            args.rewrites_file.write_text(
                "".join(f"{name}@{version}\n" for name, _, version in rewrites),
                encoding="utf-8",
            )
        return 0
    except Refused as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
