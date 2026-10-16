#!/usr/bin/env python3
"""Regenerate THIRD_PARTY_LICENSES.md from what the project actually depends on.

Sources
-------
Python   `pip-licenses` against the active virtualenv, which must be
         installed from `backend/requirements/runtime.lock` plus
         `modules/requirements.lock` -- the closures the images install, and
         the only ones that resolve to the same versions twice. See
         `REQUIREMENTS`.
Node     `npx license-checker-rseidelsohn --production` in each package that has
         a `package-lock.json` (the dashboard and the five JavaScript SDKs).
Other    Declared direct dependencies read out of the per-SDK manifests
         (go.mod, pom.xml, build.gradle.kts, composer.json, mix.exs,
         pubspec.yaml, Package.swift, *.csproj). No resolver is run for these
         ecosystems because the repository does not vendor their toolchains;
         the licence column is the upstream project's published licence.

The environment decides the answer
----------------------------------
Both resolvers read the machine they run on, not the lock files, so this
script only produces a correct document in an environment that matches what
ships:

* `pip-licenses` reports the active virtualenv. A pin that is not installed
  was silently skipped, so a developer with a partial venv got a shorter file
  and no warning.
* `license-checker` reads `node_modules`, and npm installs the **current
  platform's** optional binaries. `sharp` -- Next's image dependency -- ships
  per-platform: a macOS run records `@img/sharp-darwin-arm64` in the
  attribution record for a Linux image. A package with no `node_modules` at
  all does not error; it reports one entry and exits 0.

So `--check` refuses to run outside a matching environment rather than write
a plausible wrong answer (#170). `.github/workflows/third-party-licenses.yml`
is that environment; `--allow-foreign-platform` is for local debugging and
will not let you write the file.

Usage
-----
    # what CI does -- the locks, under --require-hashes
    pip install --require-hashes \
      -r backend/requirements/runtime.lock -r modules/requirements.lock
    pip install pip-licenses
    npm ci   # in each package in NODE_PACKAGES
    python scripts/generate_third_party_licenses.py --check

    # to rewrite the file (same environment required)
    python scripts/generate_third_party_licenses.py
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import platform
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_sibling(name: str):
    """Import another script in this directory; `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parent / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_node_licences = _load_sibling("check_node_licences")
OUT = ROOT / "THIRD_PARTY_LICENSES.md"


def _node_packages() -> list[tuple[str, str]]:
    """Derived from the tree, with labels, not typed out.

    `scripts/check_node_licences.py` already derives the identical list and
    says why: it "existed in three places ... with nothing tying them
    together, so a seventh SDK would have been ungated while every copy stayed
    green." This file was one of those three. Adding `sdk/preact` used to mean
    the blocking licence gate covered it while this document silently did not
    -- a published package with no attribution and `--check` reporting itself
    up to date.
    """
    labels = {
        "frontend": "Dashboard (Next.js)",
        "sdk/js": "JavaScript SDK",
        "sdk/react": "React SDK",
        "sdk/react-native": "React Native SDK",
        "sdk/openfeature": "OpenFeature provider (JavaScript)",
        "sdk/edge": "Edge SDK",
    }
    sdk_root = ROOT / "sdk"
    sdks = sorted(
        f"sdk/{entry.name}"
        for entry in (sdk_root.iterdir() if sdk_root.is_dir() else [])
        if entry.is_dir() and (entry / "package-lock.json").is_file()
    )
    return [
        (pkg, labels.get(pkg, pkg))
        for pkg in ("frontend", *sdks)
        if (ROOT / pkg / "package.json").is_file()
    ]


NODE_PACKAGES = _node_packages()

# Direct dependencies declared in manifests for ecosystems with no resolver here.
MANIFEST_DEPS: dict[str, list[tuple[str, str, str]]] = {
    "Go SDK (`sdk/go/go.mod`)": [
        ("(standard library only)", "—", "BSD-3-Clause (Go standard library)"),
    ],
    "Java SDK (`sdk/java/*/pom.xml`)": [
        ("com.squareup.okhttp3:okhttp", "4.12.0", "Apache-2.0"),
        ("com.fasterxml.jackson.core:jackson-databind", "see pom", "Apache-2.0"),
        ("org.springframework.boot:spring-boot-dependencies", "see pom", "Apache-2.0"),
        ("org.junit.jupiter:junit-jupiter (test)", "see pom", "EPL-2.0"),
        ("org.mockito:mockito-core (test)", "see pom", "MIT"),
        ("com.squareup.okhttp3:mockwebserver (test)", "4.12.0", "Apache-2.0"),
    ],
    "Android SDK (`sdk/android/sdk/build.gradle.kts`)": [
        ("com.squareup.okhttp3:okhttp", "4.12.0", "Apache-2.0"),
        ("org.jetbrains.kotlinx:kotlinx-coroutines-android", "1.7.3", "Apache-2.0"),
        ("org.jetbrains.kotlinx:kotlinx-serialization-json", "1.6.2", "Apache-2.0"),
        ("junit:junit (test)", "4.13.2", "EPL-1.0"),
        ("org.junit.jupiter:junit-jupiter (test)", "5.10.1", "EPL-2.0"),
        ("com.squareup.okhttp3:mockwebserver (test)", "4.12.0", "Apache-2.0"),
        ("org.json:json (test)", "20240303", "Public Domain"),
        ("io.mockk:mockk (test)", "1.13.9", "Apache-2.0"),
        ("org.assertj:assertj-core (test)", "3.24.2", "Apache-2.0"),
    ],
    "iOS SDK (`sdk/ios/Package.swift`)": [
        (
            "(no external package dependencies; Foundation + CommonCrypto)",
            "—",
            "Apple SDK terms",
        ),
    ],
    "Flutter SDK (`sdk/flutter/pubspec.yaml`)": [
        ("http", "^1.1.0", "BSD-3-Clause"),
        ("shared_preferences", "^2.2.0", "BSD-3-Clause"),
        ("crypto", "^3.0.3", "BSD-3-Clause"),
    ],
    "PHP SDK (`sdk/php/composer.json`)": [
        ("php (ext-json, ext-curl)", ">=8.1", "PHP-3.01 / runtime, not bundled"),
        ("phpunit/phpunit (dev)", "^10.0", "BSD-3-Clause"),
    ],
    "Ruby SDK (`sdk/ruby/experimently.gemspec`)": [
        ("(standard library only)", "—", "Ruby / BSD-2-Clause"),
    ],
    "Elixir SDK (`sdk/elixir/mix.exs`)": [
        ("jason", "~> 1.4", "Apache-2.0"),
        ("ex_doc (dev)", "~> 0.31", "Apache-2.0"),
    ],
    ".NET SDK (`sdk/dotnet/**/*.csproj`)": [
        ("System.Text.Json", "8.0.5", "MIT"),
        ("xunit (test)", "2.6.6", "Apache-2.0"),
        ("Microsoft.NET.Test.Sdk (test)", "17.8.0", "MIT"),
        ("coverlet.collector (test)", "6.0.0", "MIT"),
    ],
    "Python SDKs (`sdk/python`, `sdk/openfeature-python`)": [
        ("(experimently: standard library only)", "—", "—"),
        ("openfeature-sdk", ">=0.9.0", "Apache-2.0"),
    ],
}

# Licences that need a human decision before they ship in an Apache-2.0 work.
#
# This project is Apache-2.0, a permissive licence: a dependency is a problem
# when its own terms would reach the combined work.  Strong copyleft (GPL,
# AGPL) does exactly that -- a GPL library in the runtime closure would bind
# the distribution to the GPL, and the AGPL adds the network-use clause on
# top -- so those two are the ones to keep out.  Weak copyleft (LGPL, MPL,
# EPL, CDDL) only reaches the library itself: linking an unmodified copy is
# fine, modifications to the library must be published under its licence.
# psycopg2-binary (LGPL) is the standing example and is deliberately allowed.
#
# Matched as whole licence identifiers, not substrings.  A plain
# `"GPL" in lic.lower()` reports LGPL and AGPL as plain GPL and gives them the
# wrong note; the lookbehind keeps `GPL` from matching inside `LGPL`/`AGPL`,
# and the list is ordered most specific first so the right note wins.
COPYLEFT_FLAGS: list[tuple[str, str]] = [
    (
        r"(?<![A-Z])AGPL",
        "strong copyleft with a network-use clause — binds the combined work; "
        "must not ship in the runtime closure of an Apache-2.0 product",
    ),
    (
        r"(?<![A-Z])LGPL",
        "weak copyleft — linking an unmodified copy is fine; modifications "
        "to the library must be published under the LGPL",
    ),
    (
        r"(?<![A-Z])GPL",
        "strong copyleft — binds the combined work; must not ship in the "
        "runtime closure of an Apache-2.0 product",
    ),
    ("SSPL", "NOT an open-source licence; must not ship"),
    ("BUSL", "source-available, not open source; must not ship"),
    ("Elastic", "source-available, not open source; must not ship"),
    ("CC-BY-SA", "share-alike — unusable for code; data only, and it stays CC-BY-SA"),
    ("CC-BY-NC", "non-commercial, unusable"),
    (
        "CDDL",
        "file-level weak copyleft — combining is fine; modified files stay CDDL",
    ),
    (
        "EPL-1.0",
        "module-level weak copyleft — fine as a separate artefact; "
        "modifications to it stay EPL",
    ),
    (
        "MS-PL",
        "permissive but source redistributions of the library must stay MS-PL; "
        "keep it a separate artefact",
    ),
    ("UNKNOWN", "licence could not be determined — must be resolved by hand"),
    ("UNLICENSED", "no licence declared"),
]

_COPYLEFT_PATTERNS = [
    (re.compile(pattern, re.IGNORECASE), why) for pattern, why in COPYLEFT_FLAGS
]

#: Third-party code compiled INTO a wheel, which pip-licenses cannot see: it
#: reports the wheel's own licence only. Keyed by the canonical package name;
#: rendered only while that package is in the shipped closure. Read from the
#: wheel, not assumed: the google-re2 extension module carries abseil's symbols
#: (statically linked, no shared-library dependency) and is built with pybind11.
BUNDLED_COMPONENTS: dict[str, list[tuple[str, str]]] = {
    "google-re2": [
        ("abseil-cpp", "Apache-2.0"),
        ("pybind11", "BSD-3-Clause"),
    ],
}


def _require(executable: str, hint: str) -> None:
    """Fail with a sentence, not a traceback, when a resolver is absent.

    `environment_problems` exists to report every reason at once, and it
    reported neither of the two most likely: no pip-licenses and no node.
    Without them `run()` raised `CalledProcessError` (which prints a return
    code, not the stderr saying `No module named piplicenses`) or
    `FileNotFoundError`. `scripts/core_build.sh` and
    `scripts/check_node_licences.py` both say the sentence; this did not.
    """
    if shutil.which(executable) is None:
        raise SystemExit(f"{executable} is not on PATH. {hint}")


def run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"{' '.join(cmd[:3])} failed (exit {result.returncode})"
            + (f" in {cwd}" if cwd else "")
            + f":\n{result.stderr.strip() or '(no stderr)'}"
        )
    return result.stdout


#: What the images install, and therefore what is redistributed.
#:
#: The locks, not `backend/requirements.txt`. Two reasons, and the second one
#: only showed up once the CI gate existed:
#:
#: * **Scope.** An attribution record covers what we hand to someone else.
#:   Nobody receives `pytest` or `aws-cdk-lib` from this project -- a developer
#:   fetches those from PyPI themselves. The images are the artefact.
#: * **Reproducibility.** `requirements.txt` pins its direct dependencies and
#:   leaves pip to resolve the rest, so a transitive's version in this
#:   document was whatever PyPI served that day. The gate's second run failed
#:   on `platformdirs 4.11.11 -> 4.11.12` with no dependency change in the
#:   pull request at all -- a check that goes red on its own is one that gets
#:   disabled. These locks are `uv pip compile --universal --generate-hashes`
#:   output: every transitive pinned, every artefact hashed, byte-identical
#:   every run.
#:
#: `modules/requirements.lock` is absent in a core checkout.
REQUIREMENTS = [
    ROOT / "backend" / "requirements" / "runtime.lock",
    ROOT / "modules" / "requirements.lock",
]


def _canonical(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


#: A pinned requirement line in a `uv pip compile --generate-hashes` lock:
#: ``name==version``, optionally an environment marker, then the ``\`` that
#: continues onto the ``--hash=`` lines.
_LOCK_PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s;\\]+)"
    r"(?:\s*;\s*(?P<marker>[^\\]*?))?\s*\\?\s*$"
)

#: Below this, something is wrong with the parsing rather than with the tree.
#: Straight from experience: pointing `REQUIREMENTS` at the locks without
#: teaching the parser about `\` continuations and `--hash=` lines produced
#: **zero** pins and an empty Python section, with `--check` reporting the
#: document up to date. A legal record that lists no dependencies is the one
#: failure this script must not have.
MINIMUM_PLAUSIBLE_PINS = 40


def _lock_pins(path: Path) -> dict[str, str]:
    """``canonical name -> version`` for the pins that apply to THIS machine.

    The locks are `--universal`: one file resolved for every platform and
    Python version at once, so a pin may carry a marker saying when it
    applies.

        async-timeout==5.0.1 ; python_full_version < '3.11.3' \
        colorama==0.4.6 ; sys_platform == 'win32' \
        win32-setctime==1.2.0 ; sys_platform == 'win32' \

    Those three are not installed on the Linux, 3.11.16 runner, and treating
    every pin as required reported them as missing and refused to generate --
    correctly, given what the guard was told. A marker that does not hold is
    not a missing dependency; it is a dependency of a platform this image is
    not. Evaluated rather than string-matched, so a future marker of any shape
    is handled.
    """
    from packaging.markers import InvalidMarker, Marker

    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "--", "-r ", "-e ")):
            continue
        match = _LOCK_PIN.match(line)
        if not match:
            # As loud as the InvalidMarker branch below, and for the same
            # reason. A silent `continue` here is what produced an empty
            # Python section once; the floor in `environment_problems` is 40
            # against 83 real pins, so a format change breaking the regex for,
            # say, every pin carrying a marker could drop half the closure and
            # still pass it.
            raise SystemExit(
                f"{path}: cannot parse the requirement line {line!r}. The lock "
                "format has moved; teach _LOCK_PIN about it rather than "
                "dropping the pin."
            )
        marker = (match.group("marker") or "").strip()
        if marker:
            try:
                if not Marker(marker).evaluate():
                    continue
            except InvalidMarker:
                # Do not silently skip something we failed to understand: an
                # unreadable marker means this parser is out of date with the
                # lock format, which is how the whole file became empty once.
                raise SystemExit(
                    f"{path}: cannot parse the environment marker {marker!r} on "
                    f"{match.group('name')}. The lock format has moved; teach "
                    "_LOCK_PIN about it rather than dropping the pin."
                )
        pins[_canonical(match.group("name"))] = match.group("version")
    return pins


def declared_python_dependencies() -> set[str]:
    """The transitive closure of the pins in the requirements files.

    Whatever else happens to be installed in the virtualenv the generator
    runs in -- a formatter that was replaced, a tool tried once -- is not a
    dependency of the product and must not appear in a legal document that
    says it lists the product's dependencies.  Closed over ``Requires-Dist``
    with the environment's markers, extras included when the pin asks for
    them.
    """
    from importlib import metadata

    from packaging.requirements import Requirement

    roots: list[Requirement] = []
    for name, version in _declared_pins().items():
        roots.append(Requirement(f"{name}=={version}"))

    closure: set[str] = set()
    pending = list(roots)
    while pending:
        req = pending.pop()
        name = _canonical(req.name)
        if name in closure:
            continue
        try:
            dist = metadata.distribution(req.name)
        except metadata.PackageNotFoundError:
            continue  # not installed here: nothing to report, nothing to walk
        closure.add(name)
        for spec in dist.requires or ():
            child = Requirement(spec)
            if child.marker is not None:
                env = {"extra": ""}
                wanted = any(
                    child.marker.evaluate({"extra": extra}) for extra in req.extras
                ) or child.marker.evaluate(env)
                if not wanted:
                    continue
            pending.append(child)
    return closure


# ---------------------------------------------------------------------------
# The environment guard
#
# Every defect in #170 came from the generator answering from whatever was on
# the machine and saying nothing about it.  These checks make each of those a
# refusal instead.
# ---------------------------------------------------------------------------

#: What the distribution runs on.  `backend/Dockerfile` builds amd64 only
#: (#181), and npm resolves `sharp`'s optional binaries per platform, so a
#: document generated anywhere else describes a different closure.
SHIPPING_PLATFORM = "Linux"

#: And the architecture. `backend/Dockerfile` builds amd64 only (#181), and
#: `sharp` ships a binary per platform AND per arch -- the LGPL-3.0-or-later
#: rows in the dashboard section are `@img/sharp-libvips-linux-x64` and its
#: musl sibling. A Linux/arm64 machine (an Ampere runner, `docker run
#: --platform linux/arm64` on Apple Silicon) passed a system-only check and
#: would have recorded the arm64 binaries in their place. `x86_64` and `AMD64`
#: are the same thing spelled differently by platform.machine().
SHIPPING_MACHINES = frozenset({"x86_64", "amd64", "AMD64"})


def _declared_pins() -> dict[str, str]:
    """``canonical name -> pinned version`` for every pin the images install."""
    pins: dict[str, str] = {}
    for path in REQUIREMENTS:
        if path.exists():
            pins.update(_lock_pins(path))
    return pins


def environment_problems(allow_foreign_platform: bool = False) -> list[str]:
    """Every reason this machine would produce a document that is not the truth.

    Returned rather than raised so the caller can print all of them at once --
    being told about one missing package at a time is how people give up and
    edit the file by hand.
    """
    from importlib import metadata

    problems: list[str] = []

    foreign = (
        platform.system() != SHIPPING_PLATFORM
        or platform.machine() not in SHIPPING_MACHINES
    )
    if not allow_foreign_platform and foreign:
        problems.append(
            f"this is {platform.system()}/{platform.machine()}, and the "
            f"distribution runs on {SHIPPING_PLATFORM}/x86_64. npm installs "
            "the current platform's optional binaries, so the Node rows would "
            "name this machine's `sharp` build instead of the image's."
        )

    pins = _declared_pins()
    if len(pins) < MINIMUM_PLAUSIBLE_PINS:
        problems.append(
            f"only {len(pins)} pin(s) were parsed out of {len(REQUIREMENTS)} "
            "lock file(s). That is a parsing failure, not a small project -- "
            "and it would produce a document listing no Python dependencies "
            "while reporting itself up to date."
        )

    missing: list[str] = []
    wrong: list[str] = []
    for name, pinned in sorted(pins.items()):
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            missing.append(f"{name}=={pinned}")
            continue
        if installed != pinned:
            wrong.append(f"{name}: pinned {pinned}, installed {installed}")
    if missing:
        problems.append(
            f"{len(missing)} pinned package(s) are not installed, and an "
            "uninstalled pin is silently dropped from the document rather "
            "than reported: "
            + ", ".join(missing[:8])
            + (" ..." if len(missing) > 8 else "")
        )
    if wrong:
        problems.append(
            f"{len(wrong)} installed version(s) differ from the pin: "
            + ", ".join(wrong[:8])
            + (" ..." if len(wrong) > 8 else "")
        )

    if shutil.which("npx") is None:
        problems.append(
            "npx is not on PATH, so the Node closures cannot be resolved. "
            "Install Node 22."
        )
    try:
        import piplicenses  # noqa: F401
    except ImportError:
        problems.append(
            "pip-licenses is not installed, so the Python licences cannot be "
            "read. `pip install pip-licenses`."
        )

    for pkg_dir, label in NODE_PACKAGES:
        modules = ROOT / pkg_dir / "node_modules"
        if not modules.is_dir():
            problems.append(
                f"{pkg_dir} ({label}) has no node_modules. `license-checker` "
                "does not fail on an uninstalled tree -- it reports one entry "
                "and exits 0 -- so this section would come out almost empty. "
                "Run `npm ci` there."
            )
    return problems


def python_rows() -> list[tuple[str, str, str]]:
    raw = json.loads(run([sys.executable, "-m", "piplicenses", "--format=json"]))
    return python_rows_from(raw, declared_python_dependencies())


def python_rows_from(raw: list[dict], declared) -> list[tuple[str, str, str]]:
    """The shipped packages' rows, refusing any whose licence came back blank.

    A package that declares its licence only as a PEP 639
    ``License-Expression`` (sqlglot, for one) shows an empty ``License``
    field in ``pip show``; pip-licenses 5.5 reads the expression, but an
    older one, or a package with no licence metadata at all, reports ``""``
    or ``UNKNOWN``.  A blank row would render as a licence section with no
    name and pass ``--check``, so it is refused instead, naming the package.
    """
    rows = []
    blank = []
    for pkg in raw:
        name = pkg["Name"]
        if _canonical(name) not in declared:
            continue
        licence = normalise(pkg.get("License") or "")
        if not licence or licence.upper() == "UNKNOWN":
            blank.append(f"{name} {pkg.get('Version', '?')}")
            continue
        rows.append((name, pkg["Version"], licence))
    if blank:
        raise SystemExit(
            "pip-licenses reported no licence for "
            + ", ".join(sorted(blank))
            + ". Read it from the package's own metadata or LICENSE file and fix the"
            " environment (pip-licenses version) rather than writing a blank row."
        )
    return rows


def _own_package_names() -> set[str]:
    """Every npm package name this repository publishes.

    `--excludePrivatePackages` only drops `private: true`, which covers the
    dashboard and nothing else -- the SDKs are publishable, so each one
    reported *itself*, and `@getexperimently/js-sdk` additionally appeared as
    a dependency of the React Native SDK. The committed file therefore listed
    five of Controlvar's own packages as third-party software. They are
    first-party whichever table they turn up in, so they are excluded by name.
    """
    names = set()
    for pkg_dir, _label in NODE_PACKAGES:
        manifest = ROOT / pkg_dir / "package.json"
        if manifest.exists():
            name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
            if name:
                names.add(name)
    return names


def _declared_node_dependencies(pkg_dir: str) -> int:
    """How many THIRD-PARTY runtime dependencies the manifest declares.

    First-party ones are excluded because `node_rows` excludes them from the
    rows, and comparing a count that includes them against a list that does
    not made a correctly installed tree look uninstalled. `sdk/openfeature`
    declares one, `@getexperimently/js-sdk`, and takes `@openfeature/server-sdk`
    as a peer dependency; counting the first-party one could send an operator
    to run an `npm ci` that had already been run.
    """
    manifest = ROOT / pkg_dir / "package.json"
    if not manifest.exists():
        return 0
    declared = (
        json.loads(manifest.read_text(encoding="utf-8")).get("dependencies") or {}
    )
    own = _own_package_names()
    return len([name for name in declared if name not in own])


def node_rows(pkg_dir: str) -> list[tuple[str, str, str]]:
    out = run(
        [
            "npx",
            "--yes",
            # The same pin the blocking licence gate uses -- one constant, so
            # the two cannot resolve different resolvers.
            _node_licences.LICENSE_CHECKER,
            "--production",
            "--json",
            # Drops the dashboard, which is `private: true`. Not enough on its
            # own -- see `_own_package_names`.
            "--excludePrivatePackages",
        ],
        cwd=ROOT / pkg_dir,
    )
    data = json.loads(out)
    own = _own_package_names()
    rows = []
    for key, val in data.items():
        name, _, version = key.rpartition("@")
        name = name or key
        lic = val.get("licenses")
        if name in own:
            continue
        # Three shapes, all real: a string, a list of strings, and a list of
        # objects (`[{"type": "MIT", "url": ...}]`). This handled two, so the
        # object form raised `TypeError: sequence item 0: expected str
        # instance, dict found` and a bare dict landed in the legal document
        # as a Python repr with no flag. `check_node_licences.normalise` has
        # handled all three since that form broke the blocking gate; using it
        # rather than keeping a weaker copy beside it.
        rows.append(
            (
                name,
                version,
                normalise(_node_licences.normalise("UNKNOWN" if lic is None else lic)),
            )
        )

    # An empty closure is the right answer for a zero-dependency SDK -- three
    # of the six declare no runtime `dependencies` at all, which is a property
    # worth keeping, not a symptom. It is only wrong when the manifest says
    # otherwise, and that is what an uninstalled `node_modules` looks like:
    # `license-checker` reports the package itself and exits 0.
    declared = _declared_node_dependencies(pkg_dir)
    if declared and not rows:
        raise SystemExit(
            f"{pkg_dir} declares {declared} runtime dependenc(ies) and "
            "license-checker resolved none: that is an uninstalled tree, not "
            "a dependency closure. Run `npm ci` there."
        )
    return rows


def normalise(lic: str) -> str:
    table = {
        "MIT License": "MIT",
        "Apache Software License": "Apache-2.0",
        "Apache 2.0": "Apache-2.0",
        "Apache License 2.0": "Apache-2.0",
        "BSD License": "BSD",
        "3-Clause BSD License": "BSD-3-Clause",
        "ISC License (ISCL)": "ISC",
        "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
        "GNU Library or Lesser General Public License (LGPL)": "LGPL",
        "Python Software Foundation License": "PSF-2.0",
        "The Unlicense (Unlicense)": "Unlicense",
        "MIT License; MIT No Attribution License (MIT-0)": "MIT OR MIT-0",
        "Apache Software License; MIT License": "Apache-2.0 OR MIT",
        "Apache Software License; BSD License": "Apache-2.0 OR BSD",
    }
    return table.get(lic.strip(), lic.strip())


def flags_for(lic: str) -> str | None:
    """The note for the first flagged licence named in *lic*, or None.

    *lic* may be an expression ("Apache-2.0 OR MIT", "GPL-2.0 WITH
    Classpath-exception-2.0"), so this looks for identifiers inside it rather
    than comparing the whole string.
    """
    for pattern, why in _COPYLEFT_PATTERNS:
        if pattern.search(lic):
            return why
    return None


def bundled_table(rows: list[tuple[str, str, str]]) -> str:
    """The BUNDLED_COMPONENTS of every package in *rows*, or "" when none applies."""
    present = {_canonical(name): (name, version) for name, version, _ in rows}
    lines = []
    for package in sorted(BUNDLED_COMPONENTS):
        if package not in present:
            continue
        name, version = present[package]
        for component, lic in BUNDLED_COMPONENTS[package]:
            flag = flags_for(lic)
            suffix = f" ⚠️ {flag}" if flag else ""
            lines.append(f"| `{component}` | `{name}` {version} | {lic}{suffix} |")
    if not lines:
        return ""
    header = [
        "#### Compiled into the wheels above",
        "",
        "Not separate packages: these are built into another package's "
        "binary wheel, so its licence row does not cover them.",
        "",
        "| Component | Inside | Licence |",
        "|---|---|---|",
    ]
    return "\n".join(header + lines) + "\n"


def grouped_table(rows: list[tuple[str, str, str]]) -> str:
    by_lic: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for name, version, lic in rows:
        by_lic[lic].append((name, version))
    lines = []
    for lic in sorted(by_lic, key=lambda k: (-len(by_lic[k]), k)):
        flag = flags_for(lic)
        header = f"#### {lic} ({len(by_lic[lic])})"
        if flag:
            header += f"  ⚠️ {flag}"
        lines.append(header)
        lines.append("")
        lines.append("| Package | Version |")
        lines.append("|---|---|")
        for name, version in sorted(by_lic[lic]):
            lines.append(f"| `{name}` | {version} |")
        lines.append("")
    return "\n".join(lines)


#: The one line that legitimately differs between two correct runs.  `--check`
#: compares everything else, so a regeneration on a different day is not a
#: diff and the gate does not cry wolf.
_DATE_LINE = re.compile(
    r"^Generated by `scripts/generate_third_party_licenses\.py` on \d{4}-\d{2}-\d{2}\.$",
    re.MULTILINE,
)


def _comparable(text: str) -> list[str]:
    return _DATE_LINE.sub("Generated by <script> on <date>.", text).splitlines()


def render() -> str:
    parts: list[str] = []
    parts.append(
        f"""# Third-party licences

Generated by `scripts/generate_third_party_licenses.py` on {date.today().isoformat()}.
Do not edit by hand; re-run the script after a dependency change.

Controlvar's own licensing is in `LICENSE` (Apache-2.0, the whole repository
including the optional modules) and `sdk/LICENSE` (MIT, the client SDKs). This
file covers only third-party software: what the product depends on, and under
which licence each dependency is redistributed.

Sections are grouped by licence, most common first. A ⚠️ marks a licence that
needs a human decision before redistribution — for an Apache-2.0 product that
is strong copyleft (GPL, AGPL), which must not ship in the runtime closure,
and weak copyleft (LGPL, MPL, EPL, CDDL), which is fine to link unmodified.
"""
    )

    parts.append("## Python — what the images install\n")
    parts.append(
        "Everything the images install: `backend/requirements/runtime.lock` "
        "(the core profile) and `modules/requirements.lock` (what the full "
        "profile adds). Both are `uv pip compile --universal "
        "--generate-hashes` output, so every transitive is pinned and this "
        "list is the same on every run.\n\n"
        "Development-only dependencies — what `backend/requirements.txt` adds "
        "for test jobs and developer environments, and "
        "`infrastructure/cdk/requirements.txt` for the deployment toolchain — "
        "are **not** listed. They are not redistributed: nobody receives them "
        "from this project, they are fetched from PyPI when you build from "
        "source. They are audited for advisories separately, by "
        "`scripts/audit_dependencies.py` against "
        "`security/dependency-audit.toml`.\n"
    )
    py_rows = python_rows()
    parts.append(grouped_table(py_rows))
    bundled = bundled_table(py_rows)
    if bundled:
        parts.append(bundled)

    for pkg_dir, label in NODE_PACKAGES:
        parts.append(f"## Node — {label} (`{pkg_dir}`)\n")
        parts.append("Resolved with `license-checker-rseidelsohn --production`.\n")
        if pkg_dir == "frontend":
            # Measured from frontend/Dockerfile, not assumed: the runtime stage
            # is `nginxinc/nginx-unprivileged` and copies `/app/frontend/out`
            # (the static export) plus the nginx config. `node_modules` never
            # enters it. This matters because the rows below include
            # `@img/sharp-libvips-*`, which are LGPL-3.0-or-later.
            parts.append(
                "These are the dashboard's **build-time** dependencies. The "
                "dashboard image runs nginx over the static Next.js export: "
                "`frontend/Dockerfile`'s runtime stage copies `frontend/out` "
                "and the nginx configuration and nothing else, so none of the "
                "packages below is redistributed in it. They are listed "
                "because anyone building the dashboard from source resolves "
                "them.\n"
            )
        rows = node_rows(pkg_dir)
        if rows:
            parts.append(grouped_table(rows))
        else:
            manifest = ROOT / pkg_dir / "package.json"
            peers = {}
            if manifest.exists():
                peers = (
                    json.loads(manifest.read_text(encoding="utf-8")).get(
                        "peerDependencies"
                    )
                    or {}
                )
            if peers:
                parts.append(
                    "No third-party runtime dependencies: this package "
                    "declares none, and its `peerDependencies` are supplied "
                    "by the host application.\n"
                )
            else:
                parts.append(
                    "No third-party runtime dependencies: this package declares none.\n"
                )

    parts.append("## Other SDK ecosystems — declared direct dependencies\n")
    parts.append(
        "These ecosystems have no resolver in this repository, so the list below "
        "is read from the manifests and the licence column is the upstream "
        "project's published licence. Regenerate by hand when a manifest changes.\n"
    )
    for label, deps in MANIFEST_DEPS.items():
        parts.append(f"### {label}\n")
        parts.append("| Package | Version | Licence |")
        parts.append("|---|---|---|")
        for name, version, lic in deps:
            flag = flags_for(lic)
            suffix = f" ⚠️ {flag}" if flag else ""
            parts.append(f"| `{name}` | {version} | {lic}{suffix} |")
        parts.append("")

    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--check",
        action="store_true",
        help="regenerate in memory and diff against the committed file; "
        "exit 1 if they differ. Does not write.",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the regenerated document here instead of THIRD_PARTY_LICENSES.md "
        "(used by CI to attach the answer to a failing --check run).",
    )
    ap.add_argument(
        "--allow-foreign-platform",
        action="store_true",
        help="skip the platform check, for local debugging. The document "
        "produced is not the one that ships and must not be committed.",
    )
    args = ap.parse_args(argv)

    problems = environment_problems(args.allow_foreign_platform)
    if problems:
        print(
            "This environment cannot produce a correct attribution record.\n"
            "Both resolvers read the machine, not the lock files, so the\n"
            "document would be plausible and wrong (#170).\n",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\n.github/workflows/third-party-licenses.yml is an environment "
            "that satisfies all of the above.",
            file=sys.stderr,
        )
        return 2

    # The docstring, --help and the pull request all said this flag "will not
    # let you write the file". Nothing enforced it: `allow_foreign_platform`
    # was consulted once, to suppress the platform problem, and the write
    # below was unguarded. On a machine where the platform check was the only
    # objection -- Linux/arm64, or a macOS venv that happened to match the
    # locks -- running the flag "just to look" produced a committable document
    # naming the wrong `sharp` binaries. The claim is now true.
    if args.allow_foreign_platform and args.out is None and not args.check:
        print(
            "--allow-foreign-platform will not write "
            f"{OUT.relative_to(ROOT)}: the document it produces is not the "
            "one that ships. Use --out to write a copy somewhere else, or "
            "--check to see the difference.",
            file=sys.stderr,
        )
        return 2

    generated = render()

    if args.out is not None:
        args.out.write_text(generated, encoding="utf-8")
        print(f"wrote {args.out}")

    if args.check:
        committed = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if _comparable(committed) == _comparable(generated):
            print(f"{OUT.relative_to(ROOT)} is up to date")
            return 0
        diff = difflib.unified_diff(
            _comparable(committed),
            _comparable(generated),
            fromfile=f"{OUT.relative_to(ROOT)} (committed)",
            tofile="regenerated from this environment",
            lineterm="",
        )
        print("\n".join(diff))
        print(
            f"\n{OUT.relative_to(ROOT)} does not describe the current "
            "dependencies. Regenerate it in an environment that matches the "
            "distribution -- see the workflow named above.",
            file=sys.stderr,
        )
        return 1

    if args.out is None:
        OUT.write_text(generated, encoding="utf-8")
        print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
