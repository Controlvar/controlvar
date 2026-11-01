"""`scripts/contributor_guard.py`: which files a fork's pull request may change.

The rules are an allow-list, so the dangerous failure is a quiet one: a pattern
that matches nothing, a status or a page that is never read, an error read as
an empty list. So:

* each rule is pinned on fixture names, one plant per way it can be dodged;
* each rule is shown to refuse at least one path of the tree it runs in, and
  every path under a refused top-level area is shown to be refused by its
  location; the tree is walked on the filesystem, never through git, so this
  holds in core_build.sh's copy (no ``.git``, no ``modules/``) and is never
  checked over 0 paths;
* exact per-rule counts are pinned on a frozen list of every tracked path
  (``contributor_guard_tree.txt``), so a rule that moves shows up as a count
  that moved, and every extension in it is classified;
* the reading is driven through a fake API: every page, every status, the
  count, a truncated tree, an error, a head that moves;
* the output is checked as text: fixed sentences, reasons from the table,
  every path JSON-escaped between ``::stop-commands::`` and its resume line.

The guard's workflow shape is pinned in ``test_privileged_triggers.py``.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import posixpath
import re
import stat
import subprocess
import sys
import urllib.error
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "contributor_guard.py"
FROZEN = Path(__file__).with_name("contributor_guard_tree.txt")
G2_TEST = Path(__file__).with_name("test_privileged_triggers.py")

_spec = importlib.util.spec_from_file_location("contributor_guard", SCRIPT)
cg = importlib.util.module_from_spec(_spec)
sys.modules["contributor_guard"] = cg  # dataclasses look their module up
_spec.loader.exec_module(cg)

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"
REPO = "owner/repo"
NUMBER = 7


def rule_of(path: str) -> Optional[str]:
    """The rule that refuses `path` in a pull request that adds it, against a
    tree holding it as a regular file."""
    refusals = cg.judge([cg.Name(path, True)], _tree_for([path]))
    return refusals[0].rule if refusals else None


def _tree_for(
    paths: Iterable[str], modes: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    """A head tree: each path as a file (mode 100644 unless given), and every
    directory above it."""
    tree: Dict[str, str] = {}
    for path in paths:
        parts = path.split("/")
        for end in range(1, len(parts)):
            tree.setdefault("/".join(parts[:end]), "040000")
        tree[path] = (modes or {}).get(path, "100644")
    return tree


# --------------------------------------------------------------------------
# A fake API
# --------------------------------------------------------------------------


class FakeApi:
    """The three reads the guard makes, served from fixtures. Each call is
    recorded; anything else is an error in the test."""

    def __init__(
        self,
        files: List[Dict[str, Any]],
        tree: Optional[Dict[str, str]] = None,
        heads: Iterable[str] = (SHA, SHA),
        truncated: Any = False,
        fail: Optional[Dict[str, str]] = None,
    ) -> None:
        self.files = files
        if tree is None:
            tree = _tree_for(
                f["filename"]
                for f in files
                if isinstance(f, dict)
                and isinstance(f.get("filename"), str)
                and f["filename"]
                and f.get("status") != "removed"
            )
        self.tree = tree
        self.heads = list(heads)
        self.truncated = truncated
        self.fail = fail or {}
        self.calls: List[str] = []

    def __call__(self, path: str) -> Any:
        self.calls.append(path)
        for prefix, reason in self.fail.items():
            if path.startswith(prefix):
                raise cg.Stop(reason)
        if path == f"/repos/{REPO}/pulls/{NUMBER}":
            head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
            return {"head": {"sha": head}}
        match = re.fullmatch(
            rf"/repos/{REPO}/pulls/{NUMBER}/files\?per_page=(\d+)&page=(\d+)", path
        )
        if match:
            size, page = int(match.group(1)), int(match.group(2))
            return self.files[(page - 1) * size : page * size]
        if path == f"/repos/{REPO}/git/trees/{SHA}?recursive=1":
            entries = [
                {"path": p, "mode": m, "type": "x"} for p, m in self.tree.items()
            ]
            return {"sha": SHA, "tree": entries, "truncated": self.truncated}
        raise AssertionError(f"unexpected API call {path}")


def event(changed: int, head_repo_id: Optional[int] = 2, base_repo_id: int = 1):
    return cg.Event(REPO, NUMBER, SHA, head_repo_id, base_repo_id, changed)


def added(*paths: str) -> List[Dict[str, Any]]:
    return [{"filename": p, "status": "added"} for p in paths]


def run(files: List[Dict[str, Any]], **fake: Any) -> Any:
    api = FakeApi(files, **fake)
    return cg.run(lambda: event(len(files)), api)


def refused(files: List[Dict[str, Any]], **fake: Any) -> Dict[str, str]:
    result = run(files, **fake)
    assert result.outcome in ("passed", "refused"), (result.outcome, result.stop)
    return {r.path: r.rule for r in result.refusals}


# --------------------------------------------------------------------------
# Rules 1-7 on fixture names
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "newdir/x.py",
        ".devcontainer/devcontainer.json",
        "Makefile",
        "package.json",
        "pyproject.toml",
        "CONTRIBUTING.md",
        "scripts/leak_guard.py",
        ".github/workflows/x.yml",
        "modules/lambda/x.py",
        "modules/new/x.py",
        "backend/tests/unit/infrastructure/_workflow_graph.py",
        "backend/tests/unit/infrastructure/fixtures/x.json",
        "backend/__init__.py",
        "backend/tests/__init__.py",
        "backend/tests/unit/__init__.py",
    ],
)
def test_a_path_outside_the_open_areas_is_refused_by_location(path):
    assert rule_of(path) == "location"


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "backend/app/x.py",
        "backend/lambda/x/handler.py",
        "backend/tests/unit/api/test_x.py",
        # Beside the refused package files, and other packages' own files.
        "backend/tests/unit/test_x.py",
        "backend/tests/test_x.py",
        "backend/app/__init__.py",
        "backend/tests/unit/api/__init__.py",
        "backend/app/sitecustomize_helper.py",
        "modules/backend/app/x.py",
        "modules/frontend/src/x.tsx",
        "frontend/src/x.tsx",
        "sdk/go/x.go",
        "demo/shoplab/src/x.ts",
        "docs/guide.md",
        "docs/api/openapi-v1.full.json",
        "tests/acceptance/x.py",
    ],
)
def test_ordinary_paths_pass(path):
    assert rule_of(path) is None


@pytest.mark.parametrize(
    "path",
    [
        "backend/app/ä.py",
        "backend/app/a b.py",
        "backend/app/a\nb.py",
        "backend/app/a\tb.py",
        "backend/app/a\x7fb.py",
        "backend//x.py",
        "/backend/x.py",
        "backend/x/",
        "",
    ],
)
def test_only_printable_ascii_names_pass(path):
    assert cg.path_rule(path) == "chars"


@pytest.mark.parametrize(
    "path",
    ["frontend/.npmrc", "demo/x/.env", "docs/../scripts/x.py", "backend/./x.py"],
)
def test_a_segment_starting_with_a_dot_is_refused(path):
    assert rule_of(path) == "dot"


@pytest.mark.parametrize(
    "path",
    [
        "backend/tests/unit/scripts/test_contributor_guard.py",
        "backend/scripts/seed.py",
        "frontend/scripts/x.js",
        "sdk/python/scripts/x.py",
    ],
)
def test_a_scripts_segment_at_any_depth_is_refused(path):
    assert rule_of(path) == "scripts"


@pytest.mark.parametrize(
    "path", ["docs/_claims/docs/x.md.toml", "docs/_claims/x.md", "docs/security/x.md"]
)
def test_the_claims_registry_and_the_policy_documents_are_refused(path):
    assert rule_of(path) == "claims"


#: Names QA measured slipping through a list of names, and two more: each is
#: refused by its kind, or by name where its extension is an allowed one.
KIND_AND_NAME = {
    "sdk/dotnet/Directory.Packages.props": "kind",
    "sdk/dotnet/Directory.Build.props": "kind",
    "sdk/dotnet/nuget.config": "kind",
    "sdk/dotnet/global.json": "manifest",
    "sdk/java/gradle/libs.versions.toml": "kind",
    "sdk/python/Pipfile": "kind",
    "sdk/python/tox.ini": "kind",
    "sdk/python/noxfile.py": "manifest",
    "sdk/ios/X.podspec": "kind",
    "sdk/dotnet/packages.config": "kind",
    "sdk/dotnet/X.fsproj": "kind",
    "sdk/dotnet/X.sln": "kind",
    "backend/GNUmakefile": "kind",
    "backend/makefile": "kind",
    "sdk/x/Brewfile": "kind",
    "sdk/x/a.nupkg": "kind",
    # Names whose extension is allowed.
    "sdk/dotnet/packages.lock.json": "manifest",
    "sdk/ios/X.podspec.json": "manifest",
    "frontend/package.json": "manifest",
    "sdk/js/package-lock.json": "manifest",
    "sdk/x/npm-shrinkwrap.json": "manifest",
    "sdk/php/composer.json": "manifest",
    "sdk/x/deno.json": "manifest",
    "sdk/python/setup.py": "manifest",
    "sdk/ios/Package.swift": "manifest",
    "sdk/rust/build.rs": "manifest",
    "sdk/elixir/mix.exs": "manifest",
    "sdk/x/Dockerfile.md": "manifest",
    "backend/tests/no_real_aws.py": "manifest",
    "backend/docker/sitecustomize.py": "manifest",
    "backend/app/usercustomize.py": "manifest",
    "sdk/python/SiteCustomize.py": "manifest",
    "sdk/java/gradle/wrapper/x.json": "manifest",
    # Kinds outside the list, from the tree.
    "backend/x.conf": "kind",
    "backend/py.typed": "kind",
    "backend/app/db/migrations/script.py.mako": "kind",
    "tests/acceptance/requirements.txt": "kind",
    "tests/acceptance/x.yaml": "kind",
    "frontend/x.sh": "kind",
    "backend/Dockerfile": "kind",
    "backend/x.": "kind",
}


@pytest.mark.parametrize("path", sorted(KIND_AND_NAME))
def test_kinds_and_names(path):
    assert rule_of(path) == KIND_AND_NAME[path]


#: Rules 2-7 read the lower-cased name, and a name that differs only in case
#: from one in the head tree is refused.
CASE = {
    "backend/Scripts/x.py": "scripts",
    "docs/Security/x.md": "claims",
    "docs/_Claims/x.md": "claims",
    "frontend/Package.json": "manifest",
    "Backend/tests/unit/Infrastructure/x.py": "location",
    "SCRIPTS/x.py": "location",
    "Backend/Tests/__init__.py": "location",
    "sdk/x/MAKEFILE": "kind",
}


@pytest.mark.parametrize("path", sorted(CASE))
def test_rules_compare_the_lower_cased_name(path):
    assert cg.path_rule(path) == CASE[path]


def test_a_name_that_differs_only_in_case_from_a_file_is_refused():
    tree = _tree_for(["docs/guide.md", "docs/Guide.md"])
    assert refused(added("docs/Guide.md"), tree=tree) == {"docs/Guide.md": "case"}


def test_a_name_that_differs_only_in_case_from_a_directory_is_refused():
    tree = _tree_for(["backend/app/x.py", "Backend/y.py"])
    assert refused(added("Backend/y.py"), tree=tree) == {"Backend/y.py": "case"}
    tree = _tree_for(["docs/guide/x.md", "docs/Guide/y.md"])
    assert refused(added("docs/Guide/y.md"), tree=tree) == {"docs/Guide/y.md": "case"}


def test_a_case_only_rename_is_refused_by_its_previous_name():
    """Rule 9 reads previous and removed names too, not only the names that
    are in the head tree."""
    files = [
        {"filename": "readme.md", "status": "renamed", "previous_filename": "README.md"}
    ]
    assert refused(files, tree={"readme.md": "100644"}) == {"README.md": "case"}


def test_a_removed_name_that_differs_only_in_case_is_refused():
    files = [{"filename": "docs/Guide.md", "status": "removed"}]
    tree = {"docs/guide.md": "100644"}
    assert refused(files, tree=tree) == {"docs/Guide.md": "case"}


def test_removed_and_previous_names_unique_in_any_case_pass():
    files = [
        {"filename": "docs/old.md", "status": "removed"},
        {
            "filename": "docs/b.md",
            "status": "renamed",
            "previous_filename": "docs/a.md",
        },
    ]
    assert refused(files, tree={"docs/new.md": "100644", "docs/b.md": "100644"}) == {}


def test_a_case_variant_of_a_tracked_manifest_is_refused_by_its_name():
    tree = _tree_for(["frontend/package.json", "frontend/Package.json"])
    files = added("frontend/Package.json")
    assert refused(files, tree=tree) == {"frontend/Package.json": "manifest"}


def test_names_unique_in_any_case_pass():
    tree = _tree_for(["docs/guide.md", "docs/other.md", "backend/app/x.py"])
    assert refused(added("docs/other.md"), tree=tree) == {}


# --------------------------------------------------------------------------
# Rule 8 and the statuses
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["120000", "160000", "040000", "100664"])
def test_only_a_regular_file_passes(mode):
    """By the mode in the head tree: the trees API reports a link as type
    `blob`, so its type says nothing."""
    tree = _tree_for(["backend/x.py"], {"backend/x.py": mode})
    assert refused(added("backend/x.py"), tree=tree) == {"backend/x.py": "mode"}
    result = run(added("backend/x.py"), tree=tree)
    assert result.refusals[0].detail == mode


def test_an_executable_file_passes():
    tree = _tree_for(["backend/x.py"], {"backend/x.py": "100755"})
    assert refused(added("backend/x.py"), tree=tree) == {}


def test_a_file_turned_into_a_link_is_refused():
    files = [{"filename": "backend/x.py", "status": "changed"}]
    tree = _tree_for(["backend/x.py"], {"backend/x.py": "120000"})
    assert refused(files, tree=tree) == {"backend/x.py": "mode"}


def test_a_listed_file_missing_from_the_tree_is_refused():
    files = added("backend/x.py")
    assert refused(files, tree=_tree_for(["backend/y.py"])) == {"backend/x.py": "mode"}


def test_a_rename_is_judged_by_both_names_and_a_removal_by_its_name():
    files = [
        {
            "filename": "backend/x.py",
            "status": "renamed",
            "previous_filename": "scripts/leak_guard.py",
        },
        {"filename": ".github/workflows/leak-guard.yml", "status": "removed"},
    ]
    assert refused(files) == {
        "scripts/leak_guard.py": "location",
        ".github/workflows/leak-guard.yml": "location",
    }


def test_a_file_added_under_a_name_another_entry_renamed_away_from_is_read():
    files = [
        {
            "filename": "backend/b.py",
            "status": "renamed",
            "previous_filename": "backend/a.py",
        },
        {"filename": "backend/a.py", "status": "added"},
    ]
    tree = _tree_for(["backend/a.py", "backend/b.py"], {"backend/a.py": "120000"})
    assert refused(files, tree=tree) == {"backend/a.py": "mode"}


#: Every status the API documents is judged: these are the ones a guard that
#: reads only added, removed, modified and renamed would let through.
STATUS_FIXTURES = {
    "a mode-only change": (
        [{"filename": "backend/x.py", "status": "changed"}],
        {"backend/x.py": "120000"},
        {"backend/x.py": "mode"},
    ),
    "a copy from a refused path": (
        [
            {
                "filename": "backend/x.py",
                "status": "copied",
                "previous_filename": "scripts/leak_guard.py",
            }
        ],
        {},
        {"scripts/leak_guard.py": "location"},
    ),
    "an unchanged entry": (
        [{"filename": "scripts/x.py", "status": "unchanged"}],
        {},
        {"scripts/x.py": "location"},
    ),
}


@pytest.mark.parametrize("fixture", sorted(STATUS_FIXTURES))
def test_every_status_is_judged(fixture):
    files, modes, expected = STATUS_FIXTURES[fixture]
    paths = [f["filename"] for f in files] + ["scripts/leak_guard.py"]
    assert refused(files, tree=_tree_for(paths, modes)) == expected


@pytest.mark.parametrize("status", ["invented", "", None, "Added"])
def test_an_unknown_status_stops_the_check(status):
    result = run([{"filename": "backend/x.py", "status": status}])
    assert result.outcome == "stopped"
    assert result.stop.reason == "unknown file status"


def test_the_statuses_are_the_documented_set():
    assert cg.STATUSES == {
        "added",
        "removed",
        "modified",
        "renamed",
        "copied",
        "changed",
        "unchanged",
    }


@pytest.mark.parametrize(
    "entry",
    [
        {"filename": "backend/b.py", "status": "renamed"},
        {"filename": "backend/b.py", "status": "copied", "previous_filename": ""},
        {"filename": "", "status": "added"},
        {"status": "added"},
        "backend/x.py",
    ],
)
def test_an_entry_that_cannot_be_read_stops_the_check(entry):
    result = run([entry])
    assert result.outcome == "stopped"
    assert result.stop.reason == "file list unreadable"


def test_a_file_named_twice_stops_the_check():
    result = run(added("backend/x.py") + added("backend/x.py"))
    assert (result.outcome, result.stop.reason) == (
        "stopped",
        "file list names a file twice",
    )


# --------------------------------------------------------------------------
# Reading: pages, counts, the tree, the head, errors
# --------------------------------------------------------------------------


def _many(count: int, bad: Iterable[int] = ()) -> List[Dict[str, Any]]:
    bad = set(bad)
    return [
        {
            "filename": f"scripts/{i}.py" if i in bad else f"backend/f{i:04d}.py",
            "status": "added",
        }
        for i in range(1, count + 1)
    ]


@pytest.mark.parametrize("bad", [31, 101, 250])
def test_every_page_is_read(bad):
    files = _many(250, bad=[bad])
    api = FakeApi(files)
    result = cg.run(lambda: event(250), api)
    assert {r.path for r in result.refusals} == {f"scripts/{bad}.py"}
    pages = [c for c in api.calls if "/files?" in c]
    assert pages == [
        f"/repos/{REPO}/pulls/{NUMBER}/files?per_page=100&page={n}" for n in (1, 2, 3)
    ]


def test_a_full_last_page_is_followed_by_one_more_read():
    api = FakeApi(_many(200))
    assert cg.run(lambda: event(200), api).outcome == "passed"
    assert sum("/files?" in c for c in api.calls) == 3


def test_a_file_list_shorter_than_changed_files_stops_the_check():
    api = FakeApi(_many(2999))
    result = cg.run(lambda: event(3000), api)
    assert (result.outcome, result.stop.reason) == (
        "stopped",
        "file list incomplete (2999 of 3000)",
    )


def test_a_file_list_longer_than_changed_files_stops_the_check():
    api = FakeApi(_many(150))
    result = cg.run(lambda: event(120), api)
    assert result.outcome == "stopped"
    assert result.stop.reason == "file list longer than expected (150 of 120)"


def test_reading_stops_one_page_past_the_api_limit():
    """A server that never sends a short page is read 31 times, not for
    ever, and the count decides."""

    class Endless(FakeApi):
        def __call__(self, path: str) -> Any:
            if "/files?" in path:
                self.calls.append(path)
                return _many(100)
            return super().__call__(path)

    api = Endless([])
    result = cg.run(lambda: event(5000), api)
    assert result.outcome == "stopped"
    assert sum("/files?" in c for c in api.calls) == cg.MAX_PAGES == 31


def test_no_changed_files_stops_the_check():
    result = cg.run(lambda: event(0), FakeApi([]))
    assert result.outcome == "stopped"
    assert result.stop.reason == "the pull request lists no changed files"


PARTIAL_TREE = "GitHub returned only part of the commit's list of files"


@pytest.mark.parametrize("truncated", [True, None, "false", 0])
def test_a_tree_that_is_not_known_to_be_whole_stops_the_check(truncated):
    result = run(added("backend/x.py"), truncated=truncated)
    assert (result.outcome, result.stop.reason) == ("stopped", PARTIAL_TREE)


class _TreeBody(FakeApi):
    """Serves `body` as the trees API's answer (a 200 with that JSON)."""

    def __init__(self, files: List[Dict[str, Any]], body: Any) -> None:
        super().__init__(files)
        self.body = body

    def __call__(self, path: str) -> Any:
        answer = super().__call__(path)
        return self.body if "/git/trees/" in path else answer


def test_a_tree_without_a_key_stops_the_check():
    body = {"sha": SHA, "tree": [{"path": "backend/x.py", "mode": "100644"}]}
    result = cg.run(lambda: event(1), _TreeBody(added("backend/x.py"), body))
    assert (result.outcome, result.stop.reason) == ("stopped", PARTIAL_TREE)


TREE_BODIES = {
    "no tree list": {"sha": SHA, "truncated": False},
    "a tree that is not a list": {"sha": SHA, "tree": {}, "truncated": False},
    "an entry with no mode": {
        "sha": SHA,
        "tree": [{"path": "backend/x.py", "type": "blob"}],
        "truncated": False,
    },
    "an entry with no path": {
        "sha": SHA,
        "tree": [{"mode": "100644", "type": "blob"}],
        "truncated": False,
    },
    "an entry that is not a mapping": {
        "sha": SHA,
        "tree": ["backend/x.py"],
        "truncated": False,
    },
    "a list for a body": [],
}


@pytest.mark.parametrize("shape", sorted(TREE_BODIES))
def test_a_tree_that_cannot_be_read_stops_the_check(shape):
    """Never read as an empty tree: the contributor sees the stop text, not a
    refusal for a reason that is not theirs."""
    api = _TreeBody(added("backend/x.py"), TREE_BODIES[shape])
    result = cg.run(lambda: event(1), api)
    assert (result.outcome, result.stop.reason) == ("stopped", "tree unreadable")


@pytest.mark.parametrize("heads", [(OTHER, SHA), (SHA, OTHER)])
def test_a_head_that_moved_stops_the_check(heads):
    """Before the file list is read, or after it and the tree."""
    result = run(added("backend/x.py"), heads=heads)
    assert (result.outcome, result.stop.reason) == (
        "stopped",
        "the pull request changed while it was being checked; "
        "the new commit gets its own run",
    )


@pytest.mark.parametrize(
    "where",
    [
        f"/repos/{REPO}/pulls/{NUMBER}/files",
        f"/repos/{REPO}/git/trees/",
        f"/repos/{REPO}/pulls/{NUMBER}",
    ],
)
def test_an_api_error_at_any_read_stops_the_check(where):
    result = run(added("scripts/x.py"), fail={where: "API error (HTTP 500)"})
    assert (result.outcome, result.stop.reason) == ("stopped", "API error (HTTP 500)")
    assert result.refusals == []


class _Opener:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.requests: List[Any] = []

    def open(self, request: Any, timeout: float) -> Any:
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return io.BytesIO(self.outcome)


@pytest.mark.parametrize(
    "outcome, reason",
    [
        (urllib.error.HTTPError("u", 500, "x", {}, None), "API error (HTTP 500)"),
        (urllib.error.HTTPError("u", 403, "x", {}, None), "API error (HTTP 403)"),
        (urllib.error.HTTPError("u", 301, "x", {}, None), "API error (HTTP 301)"),
        (urllib.error.URLError("down"), "API error (no response)"),
        (TimeoutError(), "API error (no response)"),
        (b"<html>", "API error (unreadable response)"),
    ],
)
def test_the_api_reader_turns_every_failure_into_a_stop(monkeypatch, outcome, reason):
    opener = _Opener(outcome)
    monkeypatch.setattr(cg.urllib.request, "build_opener", lambda *handlers: opener)
    get = cg.http_get("t0ken")
    with pytest.raises(cg.Stop) as stop:
        get("/repos/o/r/pulls/1")
    assert stop.value.reason == reason


def test_the_api_reader_sends_the_token_to_the_api_only(monkeypatch):
    opener = _Opener(b'{"ok": true}')
    handlers: List[Any] = []

    def build(*given: Any) -> _Opener:
        handlers.extend(given)
        return opener

    monkeypatch.setattr(cg.urllib.request, "build_opener", build)
    assert cg.http_get("t0ken")("/repos/o/r/pulls/1") == {"ok": True}
    request = opener.requests[0]
    assert request.full_url == "https://api.github.com/repos/o/r/pulls/1"
    assert request.get_header("Authorization") == "Bearer t0ken"
    assert handlers == [cg._NoRedirect]
    assert cg._NoRedirect().redirect_request() is None


# --------------------------------------------------------------------------
# classify(): the decision, for a tool that imports the script
# --------------------------------------------------------------------------

CLASSIFY_FILES = [
    {"filename": "backend/x.py", "status": "modified"},
    {"filename": "scripts/y.py", "status": "added"},
    {
        "filename": "docs/b.md",
        "status": "renamed",
        "previous_filename": "docs/_claims/a.md",
    },
    {"filename": "backend/link.py", "status": "added"},
    {"filename": ".github/workflows/z.yml", "status": "removed"},
]
CLASSIFY_TREE = {
    "backend/x.py": "100644",
    "scripts/y.py": "100755",
    "docs/b.md": "100644",
    "backend/link.py": "120000",
}


def test_classify_has_the_signature_tools_call():
    import inspect

    signature = inspect.signature(cg.classify)
    assert list(signature.parameters) == ["entries", "tree"]
    assert signature.return_annotation == "List[Tuple[str, str]]"


def test_classify_returns_one_path_and_reason_per_refused_path():
    assert cg.classify(CLASSIFY_FILES, CLASSIFY_TREE) == [
        ("scripts/y.py", cg.SCRIPTS),
        ("docs/_claims/a.md", cg.CHECKED),
        ("backend/link.py", cg.NAME),
        (".github/workflows/z.yml", cg.WORKFLOWS),
    ]
    assert cg.classify(CLASSIFY_FILES[:1], CLASSIFY_TREE) == []


def test_classify_gives_the_check_s_answer():
    """The workflow's path and classify() reach the same refusals."""
    result = run(CLASSIFY_FILES, tree=CLASSIFY_TREE)
    assert [(r.path, r.reason) for r in result.refusals] == cg.classify(
        CLASSIFY_FILES, CLASSIFY_TREE
    )


def test_classify_reads_directories_from_a_tree_of_files():
    """A caller may pass the files alone: a directory differing only in case
    is still found."""
    tree = {"backend/app/x.py": "100644", "Backend/y.py": "100644"}
    assert cg.classify(added("Backend/y.py"), tree) == [("Backend/y.py", cg.NAME)]


def test_classify_stops_on_what_it_cannot_read():
    with pytest.raises(cg.Stop, match="unknown file status"):
        cg.classify([{"filename": "backend/x.py", "status": "invented"}], {})


def test_classify_reads_nothing_but_its_arguments(monkeypatch):
    """No network, no git or other process, no environment."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("classify reached outside its arguments")

    class NoEnvironment(dict):
        def __getitem__(self, key: str) -> str:
            refuse()

        def get(self, *args: Any) -> Any:
            refuse()

    with monkeypatch.context() as patch:
        patch.setattr(cg, "http_get", refuse)
        patch.setattr(cg.urllib.request, "build_opener", refuse)
        patch.setattr(cg.urllib.request, "urlopen", refuse)
        patch.setattr(subprocess, "run", refuse)
        patch.setattr(subprocess, "Popen", refuse)
        patch.setattr(cg.os, "environ", NoEnvironment())
        answer = cg.classify(CLASSIFY_FILES, CLASSIFY_TREE)
    assert len(answer) == 4


# --------------------------------------------------------------------------
# Same repository or fork: by id
# --------------------------------------------------------------------------


def test_a_pull_request_from_this_repository_passes_without_reading_anything():
    api = FakeApi(added("scripts/x.py"))
    result = cg.run(lambda: event(1, head_repo_id=1, base_repo_id=1), api)
    assert result.outcome == "same"
    assert api.calls == []


def test_a_deleted_head_repository_counts_as_a_fork():
    result = cg.run(lambda: event(1, head_repo_id=None), FakeApi(added("scripts/x.py")))
    assert (result.outcome, [r.path for r in result.refusals]) == (
        "refused",
        ["scripts/x.py"],
    )


def test_a_fork_is_judged_whatever_the_names_say():
    """Only the ids are compared; the repository's name is not an input."""
    result = cg.run(lambda: event(1, head_repo_id=2), FakeApi(added("scripts/x.py")))
    assert result.outcome == "refused"


def _env(**changes: str) -> Dict[str, str]:
    env = {
        "GH_TOKEN": "t0ken",
        "REPOSITORY": REPO,
        "PR_NUMBER": str(NUMBER),
        "PR_SHA": SHA,
        "PR_REPO_ID": "2",
        "BASE_REPO_ID": "1",
        "CHANGED_FILES": "1",
    }
    env.update(changes)
    return env


def test_the_event_is_read_from_the_environment():
    assert cg.event_from_env(_env(PR_REPO_ID="")) == cg.Event(
        REPO, NUMBER, SHA, None, 1, 1
    )
    assert cg.event_from_env(_env()) == cg.Event(REPO, NUMBER, SHA, 2, 1, 1)


@pytest.mark.parametrize(
    "key, value",
    [
        ("REPOSITORY", ""),
        ("REPOSITORY", "owner/repo/extra"),
        ("PR_NUMBER", "0"),
        ("PR_NUMBER", "7 "),
        ("PR_SHA", SHA.upper()),
        ("PR_SHA", SHA[:39]),
        ("PR_REPO_ID", "x"),
        ("BASE_REPO_ID", ""),
        ("CHANGED_FILES", ""),
        ("CHANGED_FILES", "-1"),
    ],
)
def test_a_malformed_input_stops_the_check(key, value):
    with pytest.raises(cg.Stop, match=f"input {key}"):
        cg.event_from_env(_env(**{key: value}))


@pytest.mark.parametrize(
    "key",
    [
        "REPOSITORY",
        "PR_NUMBER",
        "PR_SHA",
        "PR_REPO_ID",
        "BASE_REPO_ID",
        "CHANGED_FILES",
    ],
)
def test_a_missing_input_stops_the_check(key):
    env = _env()
    del env[key]
    with pytest.raises(cg.Stop, match=f"input {key}"):
        cg.event_from_env(env)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

#: Words the guard's own text never uses (the paths are the pull request's).
BANNED = ("blocked", "denied", "forbidden", "security", "lane", "pull_request_target")


def _fixed_texts() -> List[str]:
    return [
        cg.SAME_REPOSITORY,
        cg.PASSED,
        cg.REFUSED_HEAD,
        cg.REFUSED_TAIL,
        cg.STOPPED,
        cg.MORE,
        cg.MOVED,
        cg.PARTIAL_TREE,
        *cg.REASONS,
    ]


def test_the_fixed_text_uses_none_of_the_banned_words():
    for text in _fixed_texts():
        for word in BANNED:
            assert word not in text.lower(), (word, text)


def test_the_fixed_text_is_printable_ascii_and_issues_no_command():
    for text in _fixed_texts():
        assert text.isascii(), text
        assert not any(line.lstrip().startswith("::") for line in text.splitlines())


def test_every_reason_shown_is_one_of_the_table():
    rules = {cg.path_rule(p) for p in ["scripts/x", "frontend/x.sh"]}
    assert rules == {"location", "kind"}
    for rule in cg.RULES:
        for path in [
            "scripts/x.py",
            ".github/x",
            "docs/security/x.md",
            "x",
            "charts/x",
        ]:
            assert cg.reason(rule, path) in cg.REASONS


@pytest.mark.parametrize(
    "path, reason",
    [
        (".github/workflows/x.yml", cg.WORKFLOWS),
        ("scripts/x.py", cg.SCRIPTS),
        ("backend/tests/unit/infrastructure/x.py", cg.SCRIPTS),
        ("backend/__init__.py", cg.SCRIPTS),
        ("backend/tests/unit/__init__.py", cg.SCRIPTS),
        ("backend/docker/sitecustomize.py", cg.DEPENDENCIES),
        ("backend/scripts/x.py", cg.SCRIPTS),
        ("requirements/runtime.txt", cg.DEPENDENCIES),
        ("frontend/package.json", cg.DEPENDENCIES),
        ("tests/acceptance/x.yaml", cg.KIND),
        ("tests/acceptance/requirements.txt", cg.DEPENDENCIES),
        ("backend/Dockerfile", cg.DEPENDENCIES),
        ("sdk/elixir/mix.lock", cg.DEPENDENCIES),
        ("charts/x/Chart.yaml", cg.INFRASTRUCTURE),
        ("modules/infrastructure/x.py", cg.INFRASTRUCTURE),
        ("deploy/x.yml", cg.INFRASTRUCTURE),
        ("security/x.toml", cg.POLICY),
        ("docs/security/x.md", cg.POLICY),
        ("docs/_claims/x.md.toml", cg.CHECKED),
        ("frontend/.npmrc", cg.NAME),
        ("backend/a b.py", cg.NAME),
        ("modules/lambda/x.py", cg.OUTSIDE),
        ("newdir/x.py", cg.OUTSIDE),
        ("Makefile", cg.OUTSIDE),
    ],
)
def test_each_area_has_its_reason(path, reason):
    assert cg.reason(cg.path_rule(path), path) == reason


def test_every_refused_root_has_a_reason_from_the_table():
    assert set(cg.REFUSED_ROOTS.values()) <= set(cg.REASONS)
    assert all(root == root.lower() and root.endswith("/") for root in cg.REFUSED_ROOTS)


#: Paths that try to speak to the runner or to break out of their line; each
#: is refused, so each is printed.
HOSTILE = [
    "docs/::add-mask::x",
    'docs/a"b',
    "docs/a\nb.md",
    "docs/ä.md",
    "docs/::set-output name=x::y",
    "scripts/::endgroup::",
    "scripts/\\u0000.py",
    "::error::x",
]


def _block(output: str) -> Tuple[List[str], List[str], str]:
    """(lines inside stop-commands, lines outside, the token)."""
    lines = output.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("::stop-commands::")]
    assert len(starts) == 1, output
    token = lines[starts[0]][len("::stop-commands::") :]
    assert re.fullmatch(r"[0-9a-f]{32}", token), token
    end = lines.index(f"::{token}::")
    return lines[starts[0] + 1 : end], lines[: starts[0]] + lines[end + 1 :], token


def test_every_path_is_printed_json_escaped_inside_stop_commands():
    files = added(*HOSTILE)
    result = run(files)
    output = cg.render(result, "a" * 32)
    inside, outside, _ = _block(output)
    assert output.isascii()
    printed = []
    for line in inside:
        match = re.fullmatch(r'  ("(?:[^"\\]|\\.)*") +reason: (.+)', line)
        assert match, line
        printed.append(json.loads(match.group(1)))
        assert match.group(2) in cg.REASONS
    assert printed == HOSTILE
    for line in outside:
        assert not line.lstrip().startswith("::"), line
        assert not any(p.splitlines()[0] in line for p in HOSTILE if "/" in p), line


def test_the_stop_token_is_fresh_each_run(capsys, monkeypatch):
    monkeypatch.setattr(cg, "http_get", lambda token: FakeApi(added("scripts/x.py")))
    tokens = set()
    for _ in range(3):
        assert cg.main([], _env()) == 1
        tokens.add(_block(capsys.readouterr().out)[2])
    assert len(tokens) == 3


def test_more_than_twenty_paths_show_twenty_and_a_count():
    files = added(*(f"scripts/{i:02d}.py" for i in range(25)))
    inside, _, _ = _block(cg.render(run(files), "b" * 32))
    assert len(inside) == 21
    assert inside[-1] == "  ...and 5 more paths."
    assert json.loads(inside[19].split()[0]) == "scripts/19.py"


def test_a_refusal_reads_as_the_fixed_text():
    output = cg.render(run(added("scripts/x.py", "backend/x.py")), "c" * 32)
    assert output.startswith(cg.REFUSED_HEAD + "\n::stop-commands::")
    assert output.endswith("\n\n" + cg.REFUSED_TAIL)
    assert '  "scripts/x.py"  reason: repository scripts and CI checks' in output
    assert "backend/x.py" not in output


def test_a_pass_says_how_many_paths_were_checked():
    files = added("backend/x.py") + [
        {"filename": "docs/b.md", "status": "renamed", "previous_filename": "docs/a.md"}
    ]
    result = run(files)
    assert cg.render(result, "d" * 32) == (
        "contributor-guard: checked 3 paths in 2 changed files; "
        "all are in areas open to outside contributors."
    )


def test_a_stop_prints_the_fixed_text_and_never_a_pass():
    output = cg.render(
        run(_many(3), fail={"/repos/": "API error (HTTP 403)"}), "e" * 32
    )
    assert output == cg.STOPPED.format(reason="API error (HTTP 403)")
    for word in BANNED + ("passed", "nothing to check", "0 paths"):
        assert word not in output.lower()


def test_a_value_from_the_api_in_a_stop_is_inside_stop_commands():
    output = cg.render(run([{"filename": "x", "status": "::warning::x"}]), "f" * 32)
    inside, outside, _ = _block(output)
    assert inside == ['  "::warning::x"']
    assert "Reason: unknown file status" in outside


def test_nothing_is_written_for_later_steps(tmp_path, monkeypatch, capsys):
    """Neither GITHUB_ENV nor GITHUB_OUTPUT is touched, whatever the result."""
    paths = {name: tmp_path / name for name in ("GITHUB_ENV", "GITHUB_OUTPUT")}
    for name, path in paths.items():
        path.write_text("")
        monkeypatch.setenv(name, str(path))
    for files in (added("scripts/x.py"), added("backend/x.py"), [{"status": "?"}]):
        monkeypatch.setattr(cg, "http_get", lambda token, f=files: FakeApi(f))
        cg.main([], {**_env(), **{k: str(p) for k, p in paths.items()}})
    capsys.readouterr()
    assert all(path.read_text() == "" for path in paths.values())


@pytest.mark.parametrize(
    "env, code, first",
    [
        ({"PR_REPO_ID": "1"}, 0, cg.SAME_REPOSITORY),
        ({"PR_REPO_ID": "1", "GH_TOKEN": ""}, 0, cg.SAME_REPOSITORY),
        ({"GH_TOKEN": ""}, 2, "contributor-guard could not read"),
        ({"PR_SHA": "x"}, 2, "contributor-guard could not read"),
    ],
)
def test_the_script_runs_on_its_own(env, code, first):
    """As the workflow runs it: a bare interpreter, the standard library, no
    network (no case here reaches the API)."""
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT)],
        env={**_env(**env), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == code, result.stdout + result.stderr
    assert result.stdout.startswith(first), result.stdout
    assert result.stderr == ""


def test_main_reports_each_outcome_by_exit_status(monkeypatch, capsys):
    for files, code in ((added("backend/x.py"), 0), (added("scripts/x.py"), 1)):
        monkeypatch.setattr(cg, "http_get", lambda token, f=files: FakeApi(f))
        assert cg.main([], _env()) == code
    monkeypatch.setattr(cg, "http_get", lambda token: FakeApi([], heads=[OTHER]))
    assert cg.main([], _env()) == 2
    capsys.readouterr()


def test_by_hand_the_pull_request_is_read_from_the_api(monkeypatch, capsys):
    class ByHand(FakeApi):
        def __call__(self, path: str) -> Any:
            if path == f"/repos/{REPO}/pulls/{NUMBER}" and not self.calls:
                self.calls.append(path)
                return {
                    "head": {"sha": SHA, "repo": {"id": 2}},
                    "base": {"repo": {"id": 1}},
                    "changed_files": 1,
                }
            return super().__call__(path)

    monkeypatch.setattr(cg, "http_get", lambda token: ByHand(added("scripts/x.py")))
    assert cg.main(["--pr", str(NUMBER), "--repo", REPO], {"GH_TOKEN": "t"}) == 1
    assert '"scripts/x.py"' in capsys.readouterr().out


def test_the_script_imports_only_the_standard_library():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    names = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert names, "no imports found: the scan is broken"
    assert names <= set(sys.stdlib_module_names) | {"__future__"}, names


# --------------------------------------------------------------------------
# The tree this runs in, walked on the filesystem
# --------------------------------------------------------------------------

#: Directories a checkout gains by being used; never part of the tree.
_NOT_TREE = {
    ".git",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    ".next",
    ".tox",
}


def _mode(path: Path) -> str:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return "120000"
    if stat.S_ISREG(info.st_mode):
        return "100755" if info.st_mode & 0o111 else "100644"
    return "000000"


@pytest.fixture(scope="module")
def walked() -> Dict[str, str]:
    """Every file (and link) under the repository root -> its git mode."""
    found: Dict[str, str] = {}
    for directory, dirs, files in os.walk(REPO_ROOT):
        base = Path(directory)
        links = [d for d in dirs if (base / d).is_symlink()]
        dirs[:] = sorted(d for d in dirs if d not in _NOT_TREE and d not in links)
        for name in files + links:
            path = base / name
            found[path.relative_to(REPO_ROOT).as_posix()] = _mode(path)
    return found


def test_the_walk_found_the_tree(walked):
    """Never a pass over nothing: the walk reaches this file and the script,
    and finds a tree's worth of files."""
    here = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()
    assert here in walked and "scripts/contributor_guard.py" in walked
    assert len(walked) > 1000, len(walked)


def _modules_absent(root: str) -> bool:
    return root.startswith("modules/") and not (REPO_ROOT / "modules").is_dir()


@pytest.mark.parametrize("root", sorted(cg.REFUSED_ROOTS))
def test_every_path_under_a_refused_root_is_refused_by_its_location(root, walked):
    if _modules_absent(root):
        pytest.skip("core tree: modules/ is deleted by design")
    under = sorted(p for p in walked if p.lower().startswith(root))
    assert under, f"{root} holds no file here: take it out of REFUSED_ROOTS"
    wrong = [p for p in under if cg.path_rule(p) != "location"]
    assert not wrong, f"allowed, or refused by another rule, under {root}: {wrong}"


@pytest.mark.parametrize("rule", [rule for rule, _ in cg.NAME_RULES])
def test_each_rule_refuses_a_path_of_this_tree(rule, walked):
    refuses = dict(cg.NAME_RULES)[rule]
    count = sum(refuses(p.lower()) for p in walked)
    assert count >= 1, f"rule {rule} refused 0 paths of this tree; expected >= 1"


@pytest.mark.parametrize("prefix", cg.CLAIMS + cg.POLICY_DOCS)
def test_each_area_of_the_claims_rule_holds_a_path_of_this_tree(prefix, walked):
    count = sum(p.lower().startswith(prefix) for p in walked)
    assert count >= 1, f"rule claims ({prefix}) refused 0 paths of this tree"


def test_the_mode_rule_refuses_a_path_of_this_tree(walked):
    links = [p for p, mode in walked.items() if mode not in cg.FILE_MODES]
    assert links, "rule mode refused 0 paths of this tree; expected >= 1"


# --------------------------------------------------------------------------
# The frozen list: exact counts
# --------------------------------------------------------------------------


def _frozen() -> Dict[str, str]:
    tree: Dict[str, str] = {}
    for line in FROZEN.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            continue
        mode, path = line.split(" ", 1)
        tree[path] = mode
    return tree


#: How many paths of the frozen list each rule refuses first. A change to a
#: rule moves these; change them in the same pull request, on purpose.
COUNTS = {
    "allowed": 2580,
    "chars": 0,
    "location": 437,
    "dot": 14,
    "scripts": 79,
    "claims": 125,
    "kind": 128,
    "manifest": 26,
    "mode": 0,
    "case": 0,
}


def test_the_frozen_list_counts_are_exact():
    files = _frozen()
    assert len(files) == sum(COUNTS.values()) == 3389
    tree = dict(files)
    for path in files:
        tree.update({d: "040000" for d in cg._prefixes(path) if d != path})
    first = {r.path: r.rule for r in cg.judge([cg.Name(p, True) for p in files], tree)}
    counts = dict.fromkeys(COUNTS, 0)
    for path in files:
        counts[first.get(path, "allowed")] += 1
    assert counts == COUNTS


#: Extensions the kind rule refuses, as found in the frozen list. Every
#: extension there is either allowed or here, so a new one is a decision.
REFUSED_KINDS = frozenset(
    ".toml .ini .cfg .yaml .yml .txt .xml .lock .sum .mod .props .config .gradle"
    " .kts .sln .fsproj .csproj .podspec .gemspec .sh .ps1 .bat .conf .cmd .mako"
    " .imports .typed"
    # Only under paths an earlier rule refuses today; refused here all the same.
    " .example .in .properties .tmpl .tpl .tsv".split()
) | {""}


def test_every_extension_in_the_frozen_list_is_classified():
    assert not REFUSED_KINDS & cg.KINDS
    extensions = {
        posixpath.splitext(p.lower().rsplit("/", 1)[-1])[1] for p in _frozen()
    }
    unclassified = sorted(extensions - cg.KINDS - REFUSED_KINDS)
    assert not unclassified, "\n".join(
        f"unclassified extension {e}" for e in unclassified
    )


# --------------------------------------------------------------------------
# What the gate tests import
# --------------------------------------------------------------------------


def _module_file(dotted: str) -> Optional[Path]:
    base = REPO_ROOT.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imported(path: Path) -> Set[Path]:
    """The repository's files `path`'s import statements name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = path.relative_to(REPO_ROOT).parent.as_posix().replace("/", ".")
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1])
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return {f for f in map(_module_file, names) if f is not None}


def _parent_packages(path: Path) -> Set[Path]:
    """The ``__init__.py`` of every package above `path`, up to the
    repository root: the files Python runs before a module in them."""
    found: Set[Path] = set()
    directory = path.parent
    while directory != REPO_ROOT and REPO_ROOT in directory.parents:
        if (directory / "__init__.py").is_file():
            found.add(directory / "__init__.py")
        directory = directory.parent
    return found


def import_closure(roots: Iterable[Path]) -> Dict[Path, Path]:
    """Each repository file reachable from `roots` by import statements, and
    the parent packages' ``__init__.py`` files of each -> a file that brings
    it in (a root maps to itself)."""
    found = {root: root for root in roots}
    queue = list(found)
    while queue:
        current = queue.pop()
        for target in _imported(current) | _parent_packages(current):
            if target not in found:
                found[target] = current
                queue.append(target)
    return found


def test_what_the_gate_tests_import_only_a_maintainer_changes():
    """The guard's and the privileged-trigger pin's tests, the script they
    load, every repository file their import statements reach, and the
    ``__init__.py`` of every package above each of those, lie in paths the
    guard refuses: a fork cannot change what a gate's test runs through a
    helper it imports or a package it sits in.

    conftest.py files are loaded by pytest, not imported; they are outside
    this walk."""
    closure = import_closure([Path(__file__).resolve(), G2_TEST, SCRIPT])
    rel = {p.relative_to(REPO_ROOT).as_posix(): i for p, i in closure.items()}
    assert "backend/tests/unit/infrastructure/_workflow_graph.py" in rel, sorted(rel)
    gate = "backend/tests/unit/infrastructure/test_docs_only_gate.py"
    assert gate in rel, sorted(rel)
    for package in cg.REFUSED_FILES:
        assert package in rel, f"the walk did not reach {package}: {sorted(rel)}"
    allowed = sorted(
        f"{importer.relative_to(REPO_ROOT).as_posix()} imports {path}"
        for path, importer in rel.items()
        if cg.path_rule(path) is None
    )
    assert not allowed, "\n".join(allowed)


# --------------------------------------------------------------------------
# The pointer in the text
# --------------------------------------------------------------------------

POINTER = re.compile(r'CONTRIBUTING\.md, section "([^"]+)"')


def test_the_text_points_to_a_section_that_exists():
    """The refusal text ends by naming CONTRIBUTING.md's "What to expect".
    Whichever is true is pinned: the heading exists and the text names it,
    or neither."""
    headings = {
        match.group(1)
        for match in re.finditer(
            r"^#{1,6} +(.+?) *$",
            (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8"),
            re.M,
        )
    }
    named = set(POINTER.findall(cg.REFUSED_TAIL))
    missing = sorted(named - headings)
    assert not missing, (
        f"CONTRIBUTING.md has no heading {missing}, which the text names"
    )
    if "What to expect" in headings:
        assert named == {"What to expect"}, "the text should point to What to expect"
