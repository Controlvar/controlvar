#!/usr/bin/env python3
"""A TypeScript React Native app installs and type-checks the packed SDK.

``@controlvar/react-native-sdk`` ships its TypeScript source (``types`` is
``src/index.ts``), so an app's own ``tsc`` checks the SDK's code under the
app's settings, against the app's copy of every module that code imports.
It takes AsyncStorage as a peer at ``>=1.21.0 <4``, so that copy is whatever
major the app already has. Three kinds of defect show only in such an app,
never in the SDK's own tree, where every devDependency is installed and
AsyncStorage is the version its lock pins:

* a type package the source needs that the tarball does not bring
  (``@types/md5`` as a devDependency: TS7016 in every app);
* a call that only some AsyncStorage majors declare (3.x's default export has
  no ``multiRemove``: TS2339);
* an install that moves the app's AsyncStorage or adds a second copy of it
  (React Native links one native module, from the app's own dependencies).

So this makes one scratch app with ``react``, ``react-native``,
``@types/react``, ``@types/jest`` and ``typescript`` at the versions the SDK's
own lock was tested with, and the React Native app template's TypeScript
config (``CONFIG``: strict, ``isolatedModules``, ``moduleResolution:
bundler``). Then, for each AsyncStorage version in ``STORAGE`` (one per major
of the peer range), it:

1. makes that version the app's own dependency (an app already on it);
2. runs the install the SDK page documents, the tarball alone
   (``npm install <tgz>``);
3. requires the app's AsyncStorage entry in package.json to be unchanged,
   exactly one copy of AsyncStorage under ``node_modules`` (that version: a
   peer is never nested under the SDK), and ``tsc --noEmit`` to pass over an
   ``App.tsx`` that imports the SDK.

Every leg runs, and each failure is printed.

Usage::

    python3 scripts/check_react_native_app.py controlvar-react-native-sdk-0.1.0.tgz
    python3 scripts/check_react_native_app.py --print-plan

Exit 0 when every leg passes, 1 when one fails (or the app cannot be set
up), 2 on a usage error. ``--npm`` names the npm to run (default ``npm``) and
``--workdir`` where the app is made (default: a new temporary directory).
Installs run with ``--ignore-scripts``. Standard library only, so the
runner's python3 is enough. sdk-unit-tests.yml runs it on every pull request
that changes sdk/react-native, and sdk-release.yml's ``npm-build`` on the
tarball it publishes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

SDK_DIR = Path(__file__).resolve().parents[1] / "sdk" / "react-native"
STORAGE_PACKAGE = "@react-native-async-storage/async-storage"
#: One version of each AsyncStorage major the SDK's peer range allows. Each is
#: a version an app gets today: 1.24.0 and 2.2.0 are the last of their majors,
#: 3.1.1 was npm's `latest` when this list was written.
STORAGE = ("1.24.0", "2.2.0", "3.1.1")
#: The tsconfig the React Native app template extends, for the React Native
#: minor the SDK's lock tests with (0.73).
CONFIG = "@react-native/typescript-config@0.73.1"
#: Installed at the version the SDK's own lock pins, so the app type-checks
#: with what the SDK was tested with.
FROM_LOCK = ("react", "react-native", "@types/react", "@types/jest", "typescript")
INSTALL = ("install", "--ignore-scripts", "--no-audit", "--no-fund")
#: The app's one file: the client, the hooks and a typed read (#1810), so a
#: missing export or an overload the app's settings refuse fails ``tsc``.
APP_TSX = (
    "import {\n"
    "  ExperimentationClient,\n"
    "  useFlag,\n"
    "  useFlagValue,\n"
    "} from '@controlvar/react-native-sdk';\n"
    "\n"
    "export const client = new ExperimentationClient({\n"
    "  apiKey: 'key',\n"
    "  baseUrl: 'http://localhost:8000',\n"
    "});\n"
    "export const hook = useFlag;\n"
    "export function useHeadline(): string {\n"
    "  return useFlagValue('headline', 'string', 'Welcome').value;\n"
    "}\n"
    "export const headline = (userId: string): Promise<string> =>\n"
    "  client.getStringValue('headline', userId, 'Welcome');\n"
)
TSCONFIG = {
    "extends": "@react-native/typescript-config/tsconfig.json",
    "include": ["App.tsx"],
}


def lock_pins(sdk_dir: Path = SDK_DIR) -> list[str]:
    """``name@version`` for each of ``FROM_LOCK``, from the SDK's lock."""
    lock = json.loads((sdk_dir / "package-lock.json").read_text(encoding="utf-8"))
    packages = lock.get("packages") or {}
    pins = []
    for name in FROM_LOCK:
        version = (packages.get(f"node_modules/{name}") or {}).get("version")
        if not isinstance(version, str) or not version:
            raise ValueError(f"{sdk_dir / 'package-lock.json'} records no {name}")
        pins.append(f"{name}@{version}")
    return pins


def setup_args(sdk_dir: Path = SDK_DIR) -> list[str]:
    """npm's arguments for the app's own packages, before any leg."""
    return [*INSTALL, *lock_pins(sdk_dir), CONFIG]


def leg_args(tgz: str, version: str) -> tuple[list[str], list[str]]:
    """npm's arguments for one leg: the app moved to that AsyncStorage, then
    the documented install of the SDK, the tarball alone."""
    return [*INSTALL, f"{STORAGE_PACKAGE}@{version}"], [*INSTALL, tgz]


def storage_spec(app: Path) -> str | None:
    """The app's own AsyncStorage entry in its package.json."""
    manifest = json.loads((app / "package.json").read_text(encoding="utf-8"))
    return (manifest.get("dependencies") or {}).get(STORAGE_PACKAGE)


def storage_copies(app: Path) -> list[str]:
    """The version of every copy of AsyncStorage under ``app/node_modules``,
    nested ones included: every package directory (``x`` or ``@scope/x``) of
    every ``node_modules``, and the ``node_modules`` inside each."""
    found = []
    stack = [app / "node_modules"]
    while stack:
        modules = stack.pop()
        if not modules.is_dir():
            continue
        for entry in sorted(modules.iterdir()):
            if not entry.is_dir():
                continue
            packages = (
                sorted(entry.iterdir()) if entry.name.startswith("@") else [entry]
            )
            for package in packages:
                if not package.is_dir():
                    continue
                if f"{entry.name}/{package.name}" == STORAGE_PACKAGE:
                    manifest = package / "package.json"
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                    found.append(str(data.get("version")))
                stack.append(package / "node_modules")
    return sorted(found)


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )


def _leg(app: Path, npm: str, tgz: str, version: str) -> list[str]:
    """The problems of one leg; empty when it passes."""
    move, documented = leg_args(tgz, version)
    moved = _run([npm, *move], app)
    if moved.returncode != 0:
        print(moved.stdout)
        return [f"npm install of AsyncStorage {version} exited {moved.returncode}"]
    before = storage_spec(app)
    installed = _run([npm, *documented], app)
    if installed.returncode != 0:
        print(installed.stdout)
        return [f"npm install of the tarball exited {installed.returncode}"]
    problems = []
    after = storage_spec(app)
    if after != before:
        problems.append(
            f"installing the tarball changed the app's AsyncStorage from {before!r} "
            f"to {after!r}"
        )
    copies = storage_copies(app)
    if copies != [version]:
        problems.append(
            f"the app has AsyncStorage {copies}; it must have one copy, its own {version}"
        )
    tsc = _run([str(app / "node_modules" / ".bin" / "tsc"), "--noEmit", "-p", "."], app)
    if tsc.returncode != 0:
        print(tsc.stdout)
        problems.append(f"tsc --noEmit exited {tsc.returncode}")
    return problems


def check(tgz: Path, npm: str, workdir: Path) -> int:
    app = workdir / "app"
    app.mkdir(parents=True)
    (app / "package.json").write_text(
        json.dumps({"name": "rn-app", "version": "0.0.0", "private": True}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    (app / "tsconfig.json").write_text(
        json.dumps(TSCONFIG, indent=2) + "\n", encoding="utf-8"
    )
    (app / "App.tsx").write_text(APP_TSX, encoding="utf-8")
    setup = _run([npm, *setup_args()], app)
    if setup.returncode != 0:
        print(setup.stdout)
        print(f"error: the app's own packages: npm install exited {setup.returncode}")
        return 1
    failed = []
    for version in STORAGE:
        label = f"AsyncStorage {version}"
        problems = _leg(app, npm, str(tgz.resolve()), version)
        for problem in problems:
            print(f"error: {label}: {problem}")
        if problems:
            failed.append(label)
        else:
            print(
                f"{label}: the app keeps its one AsyncStorage and type-checks the SDK"
            )
    if failed:
        print(
            f"error: {len(failed)} of {len(STORAGE)} legs failed: {', '.join(failed)}"
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("tgz", nargs="?", type=Path, help="The packed SDK.")
    parser.add_argument(
        "--print-plan", action="store_true", help="Print every npm command."
    )
    parser.add_argument("--npm", default="npm", help="The npm to run.")
    parser.add_argument("--workdir", type=Path, help="Where to make the app.")
    args = parser.parse_args(argv)
    if args.print_plan:
        tgz = str(args.tgz or "<tgz>")
        print(f"the app: npm {' '.join(setup_args())}")
        for version in STORAGE:
            for npm_args in leg_args(tgz, version):
                print(f"AsyncStorage {version}: npm {' '.join(npm_args)}")
        return 0
    if args.tgz is None:
        parser.error("name the packed SDK")
    if not args.tgz.is_file():
        print(f"error: {args.tgz} is not a file", file=sys.stderr)
        return 2
    if args.workdir is not None:
        return check(args.tgz, args.npm, args.workdir)
    with tempfile.TemporaryDirectory(prefix="rn-app-") as workdir:
        return check(args.tgz, args.npm, Path(workdir))


if __name__ == "__main__":
    sys.exit(main())
