#!/usr/bin/env python3
"""contributor-guard: which files a pull request from a fork may change.

``.github/workflows/contributor-guard.yml`` runs this, as it is on ``main``,
for every pull request into ``main``. A pull request from this repository
passes at once. A pull request from a fork passes only when every path it
touches is in an area open to outside contributors; otherwise the check is red
and lists each path with the reason a maintainer handles it. The rest of the
review is unaffected: a red result means "a maintainer makes this part of the
change", not "the change is rejected".

THE RULES (an allow-list: anything not described here is refused)

Every name in the pull request's file list is judged: ``filename`` for every
entry whatever its status, and ``previous_filename`` for a rename or a copy.
A status outside GitHub's documented set stops the check (see "Stops" below).
A name is refused by the first rule it breaks:

1. chars     printable ASCII with no space (0x21-0x7E), no empty segment;
2. location  under ``backend/``, ``modules/backend/``, ``modules/frontend/``,
             ``frontend/``, ``sdk/``, ``demo/``, ``docs/`` or ``tests/``, or
             exactly ``README.md``; and not under
             ``backend/tests/unit/infrastructure/``;
3. dot       no segment starts with ``.``;
4. scripts   no segment is ``scripts``;
5. claims    not under ``docs/_claims/`` or ``docs/security/``;
6. kind      the extension is in KINDS (no extension is refused);
7. manifest  the name is not a dependency, build or packaging file (MANIFEST);
8. mode      in the head commit's tree the file is a regular file (100644 or
             100755), not a link (120000) or a submodule (160000); a listed
             file the tree does not hold is refused too;
9. case      rules 2-7 compare the lower-cased name, and a name that differs
             only in case from another path or directory of the head tree is
             refused.

Rules 1-7 read the name alone (``path_rule``); 8 and 9 read the head tree.
``classify(entries, tree)`` is the whole decision on a file list and its head
tree, as ``(path, reason)`` pairs. It is pure (no network, no git, no
environment), and the check below reaches its answer through the same code,
so a tool that imports this file gets the check's answer.

READING

Only metadata, through the API, with the workflow's read-only token: the pull
request (its head, before and after), every page of its file list (100 a
page; the number of entries must equal ``changed_files``), and the head
commit's tree (which must not be ``truncated``). Nothing of the pull request
is checked out or run.

STOPS (fail closed, never "nothing to check"): an input that is missing or
malformed; a pull request that lists no changed files; an API error, a
redirect or an unreadable response; a file list shorter or longer than
``changed_files``, or one that names a file twice; an unknown status; a
truncated or unreadable tree; a head that is not the event's head, before or
after the file list is read.

OUTPUT

Fixed text, and each refused path as a JSON string with a reason from
REASONS. Every value that comes from the pull request is printed between
``::stop-commands::<token>`` and ``::<token>::`` with a fresh random token, so
the runner reads none of it as a workflow command. Nothing is written to
``GITHUB_ENV`` or ``GITHUB_OUTPUT``.

Exit status: 0 passed, 1 refused, 2 stopped.

INPUTS (environment only; the workflow passes them through ``env:``)

  GH_TOKEN       the workflow's token
  REPOSITORY     owner/name of the base repository
  PR_NUMBER      the pull request's number
  PR_SHA         the head commit the event is about
  PR_REPO_ID     the head repository's id (empty when it was deleted: a fork)
  BASE_REPO_ID   the base repository's id
  CHANGED_FILES  the pull request's changed_files

``--pr N`` reads the same values for pull request N from the API instead
(with GH_TOKEN and ``--repo``), to run the check by hand. It only reads.

Standard library only: the workflow installs nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import secrets
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

API = "https://api.github.com"
PER_PAGE = 100
#: The API lists at most 3,000 files; one page past that ends any loop.
MAX_PAGES = 3000 // PER_PAGE + 1
#: How many refused paths are printed before a count line.
SHOWN = 20

# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------

#: Rule 2. Lower-case: the name is compared lower-cased (rule 9).
ALLOWED_ROOTS = (
    "backend/",
    "modules/backend/",
    "modules/frontend/",
    "frontend/",
    "sdk/",
    "demo/",
    "docs/",
    "tests/",
)
ALLOWED_FILES = ("readme.md",)
#: Inside an allowed root, refused as a location all the same.
CARVED_OUT = ("backend/tests/unit/infrastructure/",)

#: Rule 5.
CLAIMS = ("docs/_claims/",)
POLICY_DOCS = ("docs/security/",)

#: Rule 6: the extensions a contributor may add or change. Anything else, and a
#: name with no extension, is refused.
KINDS = frozenset(
    # source
    ".py .pyi .ts .tsx .js .jsx .mjs .cjs .go .java .kt .cs .rb .php .ex .exs"
    " .swift .dart .rs .sql .css .scss .html"
    # prose and data
    " .md .mdx .json .csv .snap"
    # images
    " .svg .png .jpg .jpeg .gif .webp .ico".split()
)

#: Rule 7: dependency, build and packaging files, by name. Many are already
#: outside KINDS; they are listed so that widening KINDS cannot let them in.
MANIFEST = tuple(
    re.compile(pattern)
    for pattern in (
        # Dependency manifests and locks.
        r"(^|/)(requirements[^/]*\.(txt|in)|constraints[^/]*\.txt|[^/]*\.lock"
        r"|package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|go\.(mod|sum)"
        r"|gemfile(\.lock)?|[^/]*\.gemspec|cargo\.(toml|lock)|composer\.(json|lock)"
        r"|pubspec\.(yaml|lock)|mix\.(exs|lock)|[^/]*\.csproj|package\.(swift|resolved)"
        r"|build\.gradle(\.kts)?|settings\.gradle(\.kts)?|gradle\.properties|pom\.xml"
        r"|podfile(\.lock)?)$",
        r"(^|/)(npm-shrinkwrap|global|deno|packages\.lock)\.json$",
        r"(^|/)[^/]*\.podspec\.json$",
        # Packaging and build.
        r"(^|/)(pyproject\.toml|setup\.py|setup\.cfg|manifest\.in|noxfile\.py)$",
        r"(^|/)(gradlew|mvnw)[^/]*$",
        r"(^|/)gradle/wrapper/",
        r"(^|/)dockerfile[^/]*$",
        r"(^|/)[^/]*compose[^/]*\.ya?ml$",
        r"(^|/)makefile$",
        r"\.mk$",
        r"(^|/)build\.rs$",
        # Test isolation.
        r"no_real_aws\.py$",
    )
)

#: Rule 8.
FILE_MODES = frozenset({"100644", "100755"})

#: The statuses the file-list API documents. Any other stops the check.
STATUSES = frozenset(
    {"added", "removed", "modified", "renamed", "copied", "changed", "unchanged"}
)
#: The statuses whose entry also names the file it came from.
WITH_PREVIOUS = frozenset({"renamed", "copied"})

#: Rule keys, in the order they are applied.
RULES = (
    "chars",
    "location",
    "dot",
    "scripts",
    "claims",
    "kind",
    "manifest",
    "mode",
    "case",
)

# --------------------------------------------------------------------------
# Reasons: the only words printed beside a path
# --------------------------------------------------------------------------

WORKFLOWS = "CI workflows and release setup"
SCRIPTS = "repository scripts and CI checks"
DEPENDENCIES = "dependency and build files"
KIND = "configuration, build, script and data files"
INFRASTRUCTURE = "infrastructure and deployment"
POLICY = "project policy documents"
CHECKED = "files that other checks read"
NAME = "file name or type not accepted (links, unusual characters)"
OUTSIDE = "outside the directories open to outside contributors"

REASONS = (
    WORKFLOWS,
    SCRIPTS,
    DEPENDENCIES,
    KIND,
    INFRASTRUCTURE,
    POLICY,
    CHECKED,
    NAME,
    OUTSIDE,
)

#: Top-level areas refused by rule 2, by the reason a contributor is shown.
#: ``backend/tests/unit/infrastructure/`` is here too: it holds the tests that
#: pin the CI checks. Any location not listed gets OUTSIDE.
REFUSED_ROOTS = {
    ".github/": WORKFLOWS,
    ".claude/": OUTSIDE,
    "scripts/": SCRIPTS,
    "backend/tests/unit/infrastructure/": SCRIPTS,
    "infrastructure/": INFRASTRUCTURE,
    "modules/infrastructure/": INFRASTRUCTURE,
    "modules/lambda/": OUTSIDE,
    "deploy/": INFRASTRUCTURE,
    "charts/": INFRASTRUCTURE,
    "localstack/": INFRASTRUCTURE,
    "requirements/": DEPENDENCIES,
    "licenses/": POLICY,
    "security/": POLICY,
}

# --------------------------------------------------------------------------
# What the check prints
# --------------------------------------------------------------------------

SAME_REPOSITORY = "Pull request from the same repository: nothing to check."

PASSED = (
    "contributor-guard: checked {paths} paths in {files} changed files; "
    "all are in areas open to outside contributors."
)

REFUSED_HEAD = """\
contributor-guard: this pull request changes files that only a maintainer can change.

This is not a judgement of your work. These areas are handled by a maintainer
for every pull request from a fork, and this check keeps them that way.

Paths that need a maintainer:"""

REFUSED_TAIL = """\
What happens next:
  - Leave the pull request open. A maintainer will follow up, usually by making
    those changes themselves or by taking the change over with credit to you.
  - You do not need to remove these files or split your work unless a
    maintainer asks you to.
  - This check stays red on this pull request. That is expected; it does not
    mean the rest of the change is rejected.

If a path above is one you did not mean to touch, remove it from your branch
and push, and this check will run again.

More: CONTRIBUTING.md, section "What to expect"."""

STOPPED = """\
contributor-guard could not read the files in this pull request, so it cannot
confirm them. A maintainer will follow up. There is nothing you need to change.

Reason: {reason}"""

MORE = "  ...and {count} more paths."

#: Two stop reasons a contributor is likely to meet, in plain words.
MOVED = (
    "the pull request changed while it was being checked; "
    "the new commit gets its own run"
)
PARTIAL_TREE = "GitHub returned only part of the commit's list of files"

# --------------------------------------------------------------------------
# Rules 1-7: the name alone
# --------------------------------------------------------------------------

_PRINTABLE = re.compile(r"[\x21-\x7e]+")


def _location(name: str) -> bool:
    allowed = name in ALLOWED_FILES or name.startswith(ALLOWED_ROOTS)
    return not allowed or name.startswith(CARVED_OUT)


def _dot(name: str) -> bool:
    return any(segment.startswith(".") for segment in name.split("/"))


def _scripts(name: str) -> bool:
    return "scripts" in name.split("/")


def _claims(name: str) -> bool:
    return name.startswith(CLAIMS + POLICY_DOCS)


def _kind(name: str) -> bool:
    return posixpath.splitext(name.rsplit("/", 1)[-1])[1] not in KINDS


def _manifest(name: str) -> bool:
    return any(pattern.search(name) for pattern in MANIFEST)


#: Rules 2-7, each on its own: does it refuse this lower-cased name?
NAME_RULES: Tuple[Tuple[str, Callable[[str], bool]], ...] = (
    ("location", _location),
    ("dot", _dot),
    ("scripts", _scripts),
    ("claims", _claims),
    ("kind", _kind),
    ("manifest", _manifest),
)


def path_rule(path: str) -> Optional[str]:
    """The first of rules 1-7 that refuses `path`, or None.

    Rule 1 reads the name as given; the others read it lower-cased, which for
    the printable ASCII rule 1 leaves is the same as case-folding it."""
    if not _PRINTABLE.fullmatch(path) or "" in path.split("/"):
        return "chars"
    name = path.lower()
    for rule, refuses in NAME_RULES:
        if refuses(name):
            return rule
    return None


def reason(rule: str, path: str) -> str:
    """The text a contributor sees for a path `rule` refused."""
    name = path.lower()
    if rule == "location":
        for root, text in REFUSED_ROOTS.items():
            if name.startswith(root):
                return text
        return OUTSIDE
    if rule == "scripts":
        return SCRIPTS
    if rule == "claims":
        return POLICY if name.startswith(POLICY_DOCS) else CHECKED
    if rule == "manifest" or (rule == "kind" and _manifest(name)):
        return DEPENDENCIES
    if rule == "kind":
        return KIND
    return NAME


# --------------------------------------------------------------------------
# Rules 8-9 and the file list
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    path: str
    rule: str
    detail: str = ""

    @property
    def reason(self) -> str:
        return reason(self.rule, self.path)


@dataclass(frozen=True)
class Name:
    """One name the check judges: a file's name, or the name it had."""

    path: str
    #: True for a name that must be a file in the head tree (rule 8).
    in_tree: bool


class Stop(Exception):
    """The check cannot reach a result. `reason` is fixed text; `value`, when
    given, came from the API and is printed only inside stop-commands."""

    def __init__(self, reason: str, value: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.value = value


def names(entries: List[Dict[str, Any]]) -> List[Name]:
    """Every name in the file list, in order, refusing an entry this check
    cannot account for."""
    seen: Set[str] = set()
    out: List[Name] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise Stop("file list unreadable")
        filename = entry.get("filename")
        status = entry.get("status")
        previous = entry.get("previous_filename")
        if not isinstance(filename, str) or not filename:
            raise Stop("file list unreadable")
        if status not in STATUSES:
            raise Stop("unknown file status", str(status))
        if filename in seen:
            raise Stop("file list names a file twice", filename)
        seen.add(filename)
        out.append(Name(filename, in_tree=status != "removed"))
        if status in WITH_PREVIOUS and not isinstance(previous, str):
            raise Stop("file list unreadable")
        if previous is not None:
            if not isinstance(previous, str) or not previous:
                raise Stop("file list unreadable")
            out.append(Name(previous, in_tree=False))
    return out


def _prefixes(path: str) -> Iterator[str]:
    parts = path.split("/")
    for end in range(1, len(parts) + 1):
        yield "/".join(parts[:end])


def judge(judged: List[Name], tree: Dict[str, str]) -> List[Refusal]:
    """The refusals for `judged`, against the head tree (path -> mode). One
    refusal per path: the first rule it breaks. The directories for rule 9
    are taken from the paths, so a tree of files alone is enough."""
    folded: Dict[str, Set[str]] = defaultdict(set)
    for path in tree:
        for prefix in _prefixes(path):
            folded[prefix.casefold()].add(prefix)
    refusals: List[Refusal] = []
    refused: Set[str] = set()
    # Every name is judged, even one seen before: a file added under a name
    # another entry renamed away from still has its mode read.
    for name in judged:
        refusal = _refusal(name, tree, folded)
        if refusal is not None and refusal.path not in refused:
            refused.add(refusal.path)
            refusals.append(refusal)
    return refusals


def refusals_for(entries: List[Dict[str, Any]], tree: Dict[str, str]) -> List[Refusal]:
    """Every rule, on a file list and its head tree. Raises Stop on an entry
    it cannot account for."""
    return judge(names(entries), tree)


def classify(
    entries: List[Dict[str, Any]], tree: Dict[str, str]
) -> List[Tuple[str, str]]:
    """The check's answer for a file list, as ``(path, reason)`` pairs: one per
    refused path, in file-list order, with the reason text a contributor sees.
    An empty list means every path is allowed.

    `entries` are the API's file entries (``filename``, ``status``, and
    ``previous_filename`` when there is one); `tree` maps the head tree's
    paths to their git modes ("100644", "120000", ...). Pure: no network, no
    git, no environment. Raises Stop on an entry it cannot account for (an
    unknown status, a missing name), never returns "allowed" for one."""
    return [(r.path, r.reason) for r in refusals_for(entries, tree)]


def _refusal(
    name: Name, tree: Dict[str, str], folded: Dict[str, Set[str]]
) -> Optional[Refusal]:
    rule = path_rule(name.path)
    if rule is not None:
        return Refusal(name.path, rule)
    if name.in_tree:
        mode = tree.get(name.path)
        if mode is None:
            return Refusal(name.path, "mode", "not in the head tree")
        if mode not in FILE_MODES:
            return Refusal(name.path, "mode", mode)
    for prefix in _prefixes(name.path):
        if folded.get(prefix.casefold(), set()) - {prefix}:
            return Refusal(name.path, "case", prefix)
    return None


# --------------------------------------------------------------------------
# Reading the pull request
# --------------------------------------------------------------------------

Get = Callable[[str], Any]

_SHA = re.compile(r"[0-9a-f]{40}")
_NUMBER = re.compile(r"[1-9][0-9]{0,18}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class Event:
    repository: str
    number: int
    sha: str
    #: None when the head repository was deleted, which counts as a fork.
    head_repo_id: Optional[int]
    base_repo_id: int
    changed_files: int


@dataclass
class Result:
    #: "same", "passed", "refused" or "stopped".
    outcome: str
    refusals: List[Refusal] = field(default_factory=list)
    paths: int = 0
    files: int = 0
    stop: Optional[Stop] = None


def _id(value: Optional[str], what: str, empty_ok: bool = False) -> Optional[int]:
    if empty_ok and value == "":
        return None
    if value is None or not _NUMBER.fullmatch(value):
        raise Stop(f"input {what} missing or malformed")
    return int(value)


def event_from_env(env: Dict[str, str]) -> Event:
    repository = env.get("REPOSITORY", "")
    sha = env.get("PR_SHA", "")
    if not _REPOSITORY.fullmatch(repository):
        raise Stop("input REPOSITORY missing or malformed")
    if not _SHA.fullmatch(sha):
        raise Stop("input PR_SHA missing or malformed")
    changed = env.get("CHANGED_FILES")
    if changed is None or not re.fullmatch(r"0|[1-9][0-9]{0,8}", changed):
        raise Stop("input CHANGED_FILES missing or malformed")
    return Event(
        repository=repository,
        number=_id(env.get("PR_NUMBER"), "PR_NUMBER"),  # type: ignore[arg-type]
        sha=sha,
        head_repo_id=_id(env.get("PR_REPO_ID"), "PR_REPO_ID", empty_ok=True),
        base_repo_id=_id(env.get("BASE_REPO_ID"), "BASE_REPO_ID"),  # type: ignore[arg-type]
        changed_files=int(changed),
    )


def _head(get: Get, event: Event) -> Dict[str, Any]:
    pull = get(f"/repos/{event.repository}/pulls/{event.number}")
    try:
        sha = pull["head"]["sha"]
    except (KeyError, TypeError):
        raise Stop("API error (unreadable response)") from None
    if sha != event.sha:
        raise Stop(MOVED)
    return pull


def _file_list(get: Get, event: Event) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        batch = get(
            f"/repos/{event.repository}/pulls/{event.number}/files"
            f"?per_page={PER_PAGE}&page={page}"
        )
        if not isinstance(batch, list):
            raise Stop("API error (unreadable response)")
        entries.extend(batch)
        if len(batch) < PER_PAGE or len(entries) > event.changed_files:
            break
    if len(entries) < event.changed_files:
        raise Stop(f"file list incomplete ({len(entries)} of {event.changed_files})")
    if len(entries) > event.changed_files:
        raise Stop(
            f"file list longer than expected ({len(entries)} of {event.changed_files})"
        )
    return entries


def _tree(get: Get, event: Event) -> Dict[str, str]:
    body = get(f"/repos/{event.repository}/git/trees/{event.sha}?recursive=1")
    if not isinstance(body, dict) or not isinstance(body.get("tree"), list):
        raise Stop("tree unreadable")
    if body.get("truncated") is not False:
        raise Stop(PARTIAL_TREE)
    tree: Dict[str, str] = {}
    for entry in body["tree"]:
        if not (
            isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and isinstance(entry.get("mode"), str)
        ):
            raise Stop("tree unreadable")
        tree[entry["path"]] = entry["mode"]
    return tree


def check(event: Event, get: Optional[Get]) -> Result:
    """The check for one pull request event. `get` reads one API path; a pull
    request from this repository needs none."""
    if event.head_repo_id is not None and event.head_repo_id == event.base_repo_id:
        return Result("same")
    if get is None:
        raise Stop("input GH_TOKEN missing")
    if event.changed_files == 0:
        raise Stop("the pull request lists no changed files")
    _head(get, event)
    entries = _file_list(get, event)
    judged = names(entries)
    tree = _tree(get, event)
    _head(get, event)
    refusals = refusals_for(entries, tree)
    return Result(
        "refused" if refusals else "passed",
        refusals,
        paths=len({name.path for name in judged}),
        files=len(entries),
    )


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def render(result: Result, token: str) -> str:
    """What the check prints. Every value from the pull request or the API is
    JSON-escaped and printed between stop-commands and its resume line."""
    if result.outcome == "same":
        return SAME_REPOSITORY
    if result.outcome == "passed":
        return PASSED.format(paths=result.paths, files=result.files)
    if result.outcome == "stopped":
        assert result.stop is not None
        lines = [STOPPED.format(reason=result.stop.reason)]
        if result.stop.value is not None:
            lines += [
                f"::stop-commands::{token}",
                f"  {_quoted(result.stop.value)}",
                f"::{token}::",
            ]
        return "\n".join(lines)
    shown = result.refusals[:SHOWN]
    width = min(max(len(_quoted(r.path)) for r in shown), 60)
    lines = [REFUSED_HEAD, f"::stop-commands::{token}"]
    lines += [f"  {_quoted(r.path):<{width}}  reason: {r.reason}" for r in shown]
    if len(result.refusals) > SHOWN:
        lines.append(MORE.format(count=len(result.refusals) - SHOWN))
    lines += [f"::{token}::", "", REFUSED_TAIL]
    return "\n".join(lines)


EXIT = {"same": 0, "passed": 0, "refused": 1, "stopped": 2}


def run(read_event: Callable[[], Event], get: Optional[Get]) -> Result:
    """`check`, with every stop turned into a result."""
    try:
        return check(read_event(), get)
    except Stop as stop:
        return Result("stopped", stop=stop)


# --------------------------------------------------------------------------
# The API, read-only
# --------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: the token is sent to the address asked for,
    and nowhere else."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def http_get(token: str, api: str = API) -> Get:
    opener = urllib.request.build_opener(_NoRedirect)
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "contributor-guard",
    }

    def get(path: str) -> Any:
        request = urllib.request.Request(api + path, headers=headers)
        try:
            with opener.open(request, timeout=30) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            raise Stop(f"API error (HTTP {error.code})") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise Stop("API error (no response)") from None
        try:
            return json.loads(body)
        except ValueError:
            raise Stop("API error (unreadable response)") from None

    return get


def event_from_api(get: Get, repository: str, number: int) -> Event:
    """Pull request `number`'s values, as the workflow's event would give them."""
    pull = get(f"/repos/{repository}/pulls/{number}")
    try:
        head_repo = pull["head"]["repo"]
        return Event(
            repository=repository,
            number=number,
            sha=pull["head"]["sha"],
            head_repo_id=None if head_repo is None else int(head_repo["id"]),
            base_repo_id=int(pull["base"]["repo"]["id"]),
            changed_files=int(pull["changed_files"]),
        )
    except (KeyError, TypeError, ValueError):
        raise Stop("API error (unreadable response)") from None


def main(argv: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None) -> int:
    env = dict(os.environ) if env is None else env
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pr", type=int, help="check pull request N by hand")
    parser.add_argument("--repo", default="Controlvar/controlvar")
    args = parser.parse_args(argv)
    token = env.get("GH_TOKEN", "")
    get = http_get(token) if token else None

    def read_event() -> Event:
        if args.pr is None:
            return event_from_env(env)
        if get is None:
            raise Stop("input GH_TOKEN missing")
        if not _REPOSITORY.fullmatch(args.repo):
            raise Stop("input --repo malformed")
        return event_from_api(get, args.repo, args.pr)

    result = run(read_event, get)
    print(render(result, secrets.token_hex(16)))
    return EXIT[result.outcome]


if __name__ == "__main__":
    sys.exit(main())
