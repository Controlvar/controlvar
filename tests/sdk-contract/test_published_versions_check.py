"""The completeness check's own logic, offline (published_versions_check.py).

The check itself lists the registries, so it runs in nightly-qa and at a release
tag, not here. These pin what it decides from a listing: which member names
mean "ships a local evaluator", and what it reports when the registries and
published-sdks.json disagree.

Run: python -m pytest tests/sdk-contract -o addopts="" -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "published_versions_check", Path(__file__).parent / "published_versions_check.py"
)
check = importlib.util.module_from_spec(_spec)
sys.modules["published_versions_check"] = check
_spec.loader.exec_module(check)
ps = check.ps

PINS = ps.load_pins()
ZEROS = "sha256:" + "0" * 64


@pytest.mark.parametrize(
    ("registry", "name", "local"),
    [
        ("npm", "package/dist/local.js", True),
        ("npm", "package/dist/esm/local.mjs", True),
        ("npm", "package/dist/local.d.ts", False),
        ("npm", "package/package.json", False),
        ("npm", "package/dist/evaluator.js", False),
        ("pypi", "controlvar/local.py", True),
        ("pypi", "controlvar-0.2.0/controlvar/local.py", True),
        ("pypi", "controlvar-0.2.0/src/controlvar/local.py", True),
        ("pypi", "controlvar/client.py", False),
        ("pypi", "controlvar-0.0.0.dist-info/METADATA", False),
    ],
)
def test_a_local_evaluator_is_recognised_by_its_member_name(registry, name, local):
    assert (check.local_evaluator(registry, [name]) is not None) is local


def _file(registry, package, version, url, digest):
    return check.RegistryFile(registry, package, version, url, digest)


def _found(pins, drop=(), add=()):
    found = {}
    for a in pins:
        if a.version in drop:
            continue
        member = (
            "package/dist/local.js" if a.registry == "npm" else "controlvar/local.py"
        )
        file = _file(a.registry, a.package, a.version, a.url, a.digest)
        found[(a.registry, a.package, a.version)] = [(file, member)]
    for registry, package, version in add:
        file = _file(registry, package, version, f"https://x/{version}", ZEROS)
        found[(registry, package, version)] = [(file, "package/dist/local.js")]
    return found


def test_the_pins_match_a_registry_that_holds_exactly_them():
    assert check.compare(_found(PINS), PINS) == []


def test_an_unpinned_release_with_an_evaluator_fails():
    found = _found(PINS, add=[("npm", "@controlvar/js-sdk", "0.3.0")])
    assert check.compare(found, PINS) == [
        "@controlvar/js-sdk 0.3.0 (npm) ships a local evaluator (package/dist/local.js) "
        "and is not in the gate: pin it in tests/sdk-contract/published-sdks.json"
    ]


def test_a_pin_the_registry_does_not_hold_fails():
    problems = check.compare(_found(PINS, drop=("0.1.1",)), PINS)
    assert len(problems) == 2
    assert all("is pinned, but the registry has no such release" in p for p in problems)


def test_a_pin_whose_digest_is_not_the_registrys_fails():
    found = _found(PINS)
    key = next(iter(found))
    file, member = found[key][0]
    other = _file(
        file.registry, file.package, file.version, file.url, "sha256:" + "1" * 64
    )
    found[key] = [(other, member)]
    (problem,) = check.compare(found, PINS)
    assert "is not the registry's sha256:1111" in problem


def test_member_names_reads_the_index_only():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("package/dist/local.js")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    file = _file("npm", "p", "1", "https://x/p-1.tgz", ZEROS)
    assert check.member_names(file, buffer.getvalue()) == ["package/dist/local.js"]


def test_a_registry_listing_with_no_files_is_refused():
    def download(url):
        return json.dumps({"versions": {}, "releases": {}}).encode()

    with pytest.raises(ps.GateError, match="lists no files"):
        check.survey(download=download, out=lambda line: None)
