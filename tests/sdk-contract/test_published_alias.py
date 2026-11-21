"""``alias`` is in the source tree and in no published release (#449).

The JavaScript and Python SDK pages say that ``alias`` is in the source tree,
not in the published 0.3.0 packages, and arrives with each package's next
release. This reads every release pinned in ``published-sdks.json`` (npm
``@controlvar/js-sdk`` and PyPI ``controlvar``, each file checked by digest
before anything is read) and requires that no published client defines the
method, while both clients in this tree do. A release that ships it fails here
once it is pinned, until the pages and this test say which release has it.

It reads the files only: nothing is extracted, imported or run. It needs the
network (the npm and PyPI file hosts), as test_published_ruleset.py does. Run:
python -m pytest tests/sdk-contract/test_published_alias.py -o addopts="" -q
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "published_sdks", Path(__file__).parent / "published_sdks.py"
)
ps = importlib.util.module_from_spec(_spec)
sys.modules["published_sdks"] = ps  # dataclasses look their module up here
_spec.loader.exec_module(ps)

pytestmark = pytest.mark.regression

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTEFACTS = ps.load_pins()
#: Each language's client module, as a member of its published file.
PUBLISHED_CLIENT = {"js": "package/dist/client.js", "python": "controlvar/client.py"}
#: And in this tree.
SOURCE_CLIENT = {
    "js": REPO_ROOT / "sdk" / "js" / "src" / "client.ts",
    "python": REPO_ROOT / "sdk" / "python" / "controlvar" / "client.py",
}
#: A method named ``alias`` on the client: ``async alias(`` in the TypeScript
#: source and its compiled output, ``def alias(`` in Python.
DEFINES_ALIAS = {
    "js": re.compile(r"^\s*(?:async\s+)?alias\s*\(", re.M),
    "python": re.compile(r"^\s*def alias\(", re.M),
}
#: The same reader on a method every published client has.
DEFINES_GET_ASSIGNMENT = {
    "js": re.compile(r"^\s*(?:async\s+)?getAssignment\s*\(", re.M),
    "python": re.compile(r"^\s*def get_assignment\(", re.M),
}


@pytest.fixture(scope="module")
def published_client(tmp_path_factory):
    """The client module of a pinned release, read from its verified file: one
    download per run, the digest checked on every read."""
    cache = tmp_path_factory.mktemp("published-alias")
    read = {}

    def get(artefact) -> str:
        key = (artefact.url, artefact.digest)
        if key not in read:
            members = ps._members(artefact.language, ps.fetch(artefact, cache), artefact.label)
            name = PUBLISHED_CLIENT[artefact.language]
            assert name in members, f"{artefact.label} has no {name}"
            read[key] = members[name].decode("utf-8")
        return read[key]

    return get


def test_both_clients_in_this_tree_define_alias():
    for language, path in SOURCE_CLIENT.items():
        assert DEFINES_ALIAS[language].search(path.read_text(encoding="utf-8")), (
            f"{path.relative_to(REPO_ROOT)} defines no alias method"
        )


@pytest.mark.parametrize("artefact", ARTEFACTS, ids=[a.label for a in ARTEFACTS])
def test_no_published_release_defines_alias(published_client, artefact):
    client = published_client(artefact)
    # The same reader finds a method the release does have, so a miss is not a
    # reader that reads nothing.
    assert DEFINES_GET_ASSIGNMENT[artefact.language].search(client), artefact.label
    assert not DEFINES_ALIAS[artefact.language].search(client), (
        f"{artefact.label} ships alias: the JavaScript and Python SDK pages say no "
        "published package has it; say which release does, and update this test"
    )
