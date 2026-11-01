"""Every published release with a local evaluator is in the gate (#1497).

    python tests/sdk-contract/published_versions_check.py [--pins FILE]

Lists every version of npm ``@controlvar/js-sdk`` and PyPI ``controlvar`` from
the registries' own JSON, downloads each of its files (checking the digest the
registry gives for it), and classifies the version by its member NAMES alone:
the tar or zip index is read, nothing is extracted and nothing is imported. A
version ships a local evaluator when any of its files holds one
(``LOCAL_EVALUATOR``). The set of such versions must equal the set pinned in
``published-sdks.json``, and each pin's URL and digest must be the registry's
own for that version.

Run by nightly-qa and by rc-evidence at a release tag, never on a pull request:
a publish (0.3.0 at the sitting) would otherwise turn every open pull request red
until the pull request pinning it lands. Exit 0 when the sets match; 1
otherwise, and 1 when a registry cannot be reached -- it never skips.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import re
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

_spec = importlib.util.spec_from_file_location(
    "published_sdks", Path(__file__).resolve().parent / "published_sdks.py"
)
ps = importlib.util.module_from_spec(_spec)
sys.modules["published_sdks"] = ps  # dataclasses look their module up here
_spec.loader.exec_module(ps)

#: (registry, package, the registry's JSON for it)
REGISTRIES = (
    ("npm", "@controlvar/js-sdk", "https://registry.npmjs.org/@controlvar%2fjs-sdk"),
    ("pypi", "controlvar", "https://pypi.org/pypi/controlvar/json"),
)
#: A member name that means "this file ships a local evaluator".
LOCAL_EVALUATOR = {
    "npm": re.compile(r"package/(?:[^/]{1,100}/){0,8}local\.(?:js|cjs|mjs)"),
    "pypi": re.compile(r"(?:[^/]{1,100}/){0,8}controlvar/local\.py"),
}
PINS_NAME = "tests/sdk-contract/published-sdks.json"


@dataclass(frozen=True)
class RegistryFile:
    registry: str
    package: str
    version: str
    url: str
    digest: str

    @property
    def label(self) -> str:
        return f"{self.package} {self.version} ({self.registry})"


def npm_files(package: str, meta: Dict) -> List[RegistryFile]:
    files = []
    for version, release in sorted(meta.get("versions", {}).items()):
        dist = release.get("dist") or {}
        integrity = str(dist.get("integrity") or "")
        sha512 = next((d for d in integrity.split() if d.startswith("sha512-")), "")
        if not sha512 or not dist.get("tarball"):
            raise ps.GateError(
                f"{package} {version} (npm) has no tarball with a sha512"
            )
        files.append(RegistryFile("npm", package, version, dist["tarball"], sha512))
    return files


def pypi_files(package: str, meta: Dict) -> List[RegistryFile]:
    files = []
    for version, release in sorted(meta.get("releases", {}).items()):
        for entry in release:
            sha256 = (entry.get("digests") or {}).get("sha256")
            if not sha256 or not entry.get("url"):
                raise ps.GateError(
                    f"{package} {version} (pypi) lists a file with no sha256"
                )
            digest = f"sha256:{sha256}"
            files.append(RegistryFile("pypi", package, version, entry["url"], digest))
    return files


def member_names(file: RegistryFile, data: bytes) -> List[str]:
    """The names in the file's index. Nothing is extracted."""
    name = file.url.rsplit("/", 1)[-1]
    if name.endswith((".whl", ".zip")):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return archive.namelist()
    if name.endswith((".tgz", ".tar.gz")):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            return archive.getnames()
    raise ps.GateError(f"{file.label}: cannot list {name} (not a wheel or tarball)")


def local_evaluator(registry: str, names: Iterable[str]) -> Optional[str]:
    """The first member that is a local evaluator, or None."""
    return next((n for n in names if LOCAL_EVALUATOR[registry].fullmatch(n)), None)


Key = Tuple[str, str, str]
Found = Dict[Key, List[Tuple[RegistryFile, str]]]


def compare(found: Found, pins: Sequence, pins_name: str = PINS_NAME) -> List[str]:
    """Problems: a release with a local evaluator not pinned, a pin with no such
    release, or a pin whose URL or digest is not the registry's."""
    problems = []
    pinned = {(a.registry, a.package, a.version): a for a in pins}
    for key in sorted(set(found) - set(pinned)):
        registry, package, version = key
        member = found[key][0][1]
        problems.append(
            f"{package} {version} ({registry}) ships a local evaluator ({member}) and "
            f"is not in the gate: pin it in {pins_name}"
        )
    for key in sorted(set(pinned) - set(found)):
        problems.append(
            f"{pinned[key].label} is pinned, but the registry has no such release "
            "with a local evaluator"
        )
    for key in sorted(set(pinned) & set(found)):
        artefact = pinned[key]
        files = {file.url: file for file, _ in found[key]}
        file = files.get(artefact.url)
        if file is None:
            problems.append(
                f"{artefact.label}: the pinned {artefact.url} is not one of the "
                f"registry's files with a local evaluator: {sorted(files)}"
            )
        elif file.digest != artefact.digest:
            problems.append(
                f"{artefact.label}: the pinned digest {artefact.digest} is not the "
                f"registry's {file.digest}"
            )
    return problems


def survey(
    download: Callable[[str], bytes] = ps.download,
    out: Callable[[str], None] = print,
) -> Found:
    """Every registry file, classified; returns the versions with an evaluator."""
    found: Found = {}
    for registry, package, url in REGISTRIES:
        meta = json.loads(download(url))
        if registry == "npm":
            files = npm_files(package, meta)
        else:
            files = pypi_files(package, meta)
        if not files:
            raise ps.GateError(f"{url} lists no files; refusing to call that complete")
        for file in files:
            data = download(file.url)
            actual = ps.digest_of(data, file.digest)
            if actual != file.digest:
                raise ps.GateError(
                    f"{file.label}: {file.url} is {actual}, not the registry's "
                    f"{file.digest}"
                )
            member = local_evaluator(registry, member_names(file, data))
            verdict = f"local evaluator ({member})" if member else "no local evaluator"
            out(f"{file.label}: {file.url.rsplit('/', 1)[-1]}: {verdict}")
            if member:
                key = (registry, package, file.version)
                found.setdefault(key, []).append((file, member))
    return found


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pins", type=Path, default=ps.PINS_PATH)
    args = parser.parse_args(argv)
    try:
        pins = ps.load_pins(args.pins)
        found = survey()
    except ps.GateError as exc:
        print(f"FAIL: {exc}")  # noqa: T201 - this script's output
        return 1
    name = PINS_NAME if args.pins == ps.PINS_PATH else str(args.pins)
    problems = compare(found, pins, name)
    for problem in problems:
        print(f"FAIL: {problem}")  # noqa: T201 - this script's output
    if problems:
        return 1
    labels = ", ".join(a.label for a in pins)
    print(  # noqa: T201 - this script's output
        f"OK: the {len(pins)} pinned releases are exactly the published releases "
        f"with a local evaluator: {labels}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
