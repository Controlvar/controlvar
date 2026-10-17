"""Every page in the docs navigation is classified for end-to-end verification (#939).

``tests/acceptance/docs/inventory.toml`` gives each page that the ``nav`` of
``mkdocs.yml`` lists exactly one class saying how the page is verified, with a
one-line reason, and maps each flow in the "Flows, at least" list of #939 to
what verifies it. The file's header is the contract; this test holds it:

* every nav page has an entry, and every entry is a nav page (keys are the
  paths exactly as the nav writes them, so a page the nav lists twice is one
  key, and a second entry for it is a TOML parse error);
* the class is one of ``CLASSES`` or ``external:<service>`` with a slug for the
  service, and only the fields that class allows are present;
* a ``journey`` names its id, a slug no other page uses, and either its
  expectations file ``tests/acceptance/docs/journeys/<id>.yaml`` exists or the
  entry says ``pending = true`` (refused once the file exists, so a pending
  flag cannot outlive the journey it stands for) and ``planned``, the pull
  request that writes it (one of ``PLANNED``; only on a pending journey, so
  the run summary can say where each journey not yet written comes from);
* an ``exec`` page is enrolled in ``scripts/doc_examples.toml`` with at least
  one exec block, and a ``reference`` page is not: steps that Doc Examples
  runs are not "no steps to follow";
* a ``workflow`` page names a workflow whose ``on.<event>.paths`` lists the
  page's own path, ``docs/<page>``, as a literal entry. The workflow file is
  parsed, not searched: a comment or a step name that mentions the page does
  not count, and neither does a glob such as ``docs/**`` (#1075);
* an ``aws`` page says which step exercises it in ``exercised_by``: a list of
  values from ``EXERCISED_BY`` -- the ten steps of #295 word for word, ``605``
  or ``production-deploy`` -- or ``["none"]`` when no step does (#1075). It
  records which step exercises the page, never whether that step has run;
* every flow of #939 (``FLOWS_939``, copied from the issue) is mapped, to
  declared journeys, to classified pages or to a class some page has.

And every other user-facing file (#1142): each file
``scripts/doc_examples.py:walk()`` finds (loaded by path, so there is one walk)
is classified exactly once, a nav page under ``[pages]`` and anything else
under ``[documents]``, either in scope with a class, held to the same rules as
a page's, or out of scope with a category from ``OUT_CATEGORIES`` and a reason
of at most 200 characters. ``[meta] walked`` is the walk's exact count, so a
walk that silently loses files fails. The non-Markdown text a user reads is a
closed list of named surfaces (``SURFACES``), each read by its extractor; the
docs hub's extractor reads single- and double-quoted ``desc:`` and
``description:`` literals with bounded quantifiers, names any key whose value
it cannot read, and the number it reads is exactly the entry's ``literals``.

The planted-defect tests below run the same checks on small inventories with
one defect each, so each refusal is seen to fire.

``mkdocs.yml`` is read with a YAML loader that accepts any tag (a MkDocs config
may carry ``!ENV`` or ``!!python/name:``), building the tagged node as plain
YAML. Reads only files; no git, so it runs the same in
``scripts/core_build.sh``'s copy. It is in the docs-only gate's "Docs content
tests" through its directory, ``backend/tests/unit/docs/``.
"""

from __future__ import annotations

import functools
import importlib.util
import posixpath
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
MKDOCS = REPO_ROOT / "mkdocs.yml"
INVENTORY = REPO_ROOT / "tests" / "acceptance" / "docs" / "inventory.toml"
JOURNEYS = REPO_ROOT / "tests" / "acceptance" / "docs" / "journeys"
DOC_EXAMPLES = REPO_ROOT / "scripts" / "doc_examples.toml"
#: The walk that defines the universe, loaded by path: one walk, no copy of it.
DOC_EXAMPLES_SCRIPT = REPO_ROOT / "scripts" / "doc_examples.py"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: The classes a page can have, besides ``external:<service>``. Pinned: a new
#: class is a change to the plan, not to this file alone.
CLASSES: Tuple[str, ...] = ("journey", "exec", "workflow", "reference", "aws", "device")
EXTERNAL = re.compile(r"external:(?P<service>.*)", re.DOTALL)
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

#: The fields each class may carry; ``class`` and ``reason`` are required on all.
FIELDS: Dict[str, Set[str]] = {
    "journey": {"class", "reason", "journey", "pending", "planned"},
    "workflow": {"class", "reason", "workflow"},
    "aws": {"class", "reason", "exercised_by"},
}
PLAIN_FIELDS = {"class", "reason"}
#: The pull requests of the QA plan (#897, #939) a pending journey may name.
#: "D73" is the plan of #1142, which writes the journeys [documents] declares.
PLANNED: Tuple[str, ...] = ("D2", "D3a", "D3b", "D73", "unassigned")
FLOW_FIELDS = {"text", "note", "journeys", "pages", "classes"}

#: The steps of #295 (the first deploy to a real staging account, and a
#: rollback rehearsal), verbatim but for each line's closing punctuation. When
#: the issue's list changes, change this and the mapping.
STEPS_295: Tuple[str, ...] = (
    "bootstrap the account",
    "create the image repositories and a bootstrap image",
    "set up GitHub OIDC access for the deploy workflow",
    "run the first stack deploy",
    "migrate",
    "smoke through the load balancer on a non-probe path",
    "deploy a second release and roll back",
    "force a circuit breaker and an alarm rollback",
    "replace the invented test fixtures with captured output",
    "tear down, with the residue measured",
)
#: Everything an aws page's ``exercised_by`` may name, and nothing else: the
#: steps of #295, ``605`` (#605, which removes two DynamoDB tables after an
#: on-demand backup of each), ``production-deploy`` (the first deploy to
#: production) and ``none``, which stands alone.
NONE = "none"
EXERCISED_BY: Tuple[str, ...] = STEPS_295 + ("605", "production-deploy", NONE)

#: The "Flows, at least" list of #939, verbatim but for each line's closing
#: punctuation. When the issue's list changes, change this and the mapping.
FLOWS_939: Tuple[str, ...] = (
    "the docs site: every page in the nav reachable, links resolve, search works,"
    " the power analysis and sample size pages render and calculate",
    "Quick Start, end to end",
    "the Docker Compose guide: start the stack as written, open the dashboard, sign in",
    "feature flags: create, targeting, gradual rollout, safety monitoring",
    "experiments: the guided builder, assign, track, results (including the"
    " statistics the guide promises)",
    "the dashboard-and-API guide and the API examples (the `{.bash exec}` blocks"
    " that `doc-examples.yml` already runs)",
    "the SDK quick starts",
    "self-hosting guides where they can be rehearsed without AWS",
    "any other first-run flow the plan identifies",
)

#: [documents]: what an out-of-scope document can be, and nothing else.
OUT_CATEGORIES: Tuple[str, ...] = (
    "contributor",
    "legal",
    "generated",
    "template",
    "in-code-note",
    "removed-code",
    "test-harness",
)
OUT_FIELDS = {"scope", "out", "reason"}
REASON_MAX = 200

#: The named surfaces: text a user reads that is not a Markdown file, so the
#: walk cannot find it. A closed list: an entry for any other file that is not
#: Markdown is refused. Each maps to the extractor that reads its text, or to
#: None when a journey holds it (class journey, and no extractor).
SURFACES: Dict[str, Optional[str]] = {
    "charts/experimently/templates/NOTES.txt": "text",
    "deploy/compose/compose.yml#header": "leading-hash-comments",
    "deploy/compose/generate-env.sh#header": "leading-hash-comments",
    "frontend/src/pages/docs/index.tsx#desc": "docs-hub-desc",
    "frontend/src/components/experiments/FirstRunChecklist.tsx": None,
}
HUB = "frontend/src/pages/docs/index.tsx#desc"

HOW_TO_CLASSIFY = (
    "Each file scripts/doc_examples.py:walk() finds is classified once in"
    " tests/acceptance/docs/inventory.toml: a nav page under [pages], any other"
    ' file under [documents."<repository path>"] with scope = "in", class and'
    ' reason, or scope = "out", out (one of '
    + ", ".join(OUT_CATEGORIES)
    + ") and a reason of 1-200 characters; and [meta] walked is the number of"
    " files the walk finds. The file's header says what each field means."
)

HOW_TO_FIX = (
    "Each nav page needs one entry in tests/acceptance/docs/inventory.toml: "
    '[pages."<path as the nav writes it>"] with class = one of '
    + ", ".join(CLASSES)
    + ', external:<service>, and a one-line reason = "...". '
    "The file's header says what each class means."
)


# ---------------------------------------------------------------------------
# Reading the inputs
# ---------------------------------------------------------------------------
class _AnyTagLoader(yaml.SafeLoader):
    """A safe loader that builds a node with an unknown tag as plain YAML."""


def _untagged(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


# The empty prefix matches every tag the safe loader has no constructor for.
_AnyTagLoader.add_multi_constructor("", _untagged)


def load_mkdocs(text: str) -> Dict[str, Any]:
    return yaml.load(text, Loader=_AnyTagLoader)


def nav_pages(config: Mapping[str, Any]) -> Tuple[Set[str], List[str]]:
    """The set of Markdown pages the nav lists, and the problems found walking it.

    A leaf that is an http(s) URL is a link, not a page. Any other leaf that is
    not a ``.md`` path is refused rather than ignored.
    """
    pages: Set[str] = set()
    problems: List[str] = []

    def walk(node: Any, where: str) -> None:
        if isinstance(node, str):
            if node.startswith(("http://", "https://")):
                return
            if node.endswith(".md"):
                pages.add(posixpath.normpath(node))
            else:
                problems.append(f"nav entry {where!r} is not a .md page: {node!r}")
        elif isinstance(node, list):
            for item in node:
                walk(item, where)
        elif isinstance(node, dict):
            for title, value in node.items():
                walk(value, str(title))
        else:
            problems.append(f"nav entry {where!r} is not a page or a section: {node!r}")

    nav = config.get("nav")
    if not nav:
        problems.append("mkdocs.yml has no nav")
    else:
        walk(nav, "nav")
    return pages, problems


def parse_inventory(text: str) -> Tuple[Dict[str, Any], List[str]]:
    """The inventory, or an empty one and the parse error (a duplicate key is one)."""
    try:
        return tomllib.loads(text), []
    except tomllib.TOMLDecodeError as error:
        return {}, [
            f"inventory.toml does not parse (a duplicate key does not): {error}"
        ]


def enrolled_exec_counts(text: str) -> Dict[str, int]:
    """Each page doc_examples.toml enrols, relative to docs/, with its exec count."""
    counts: Dict[str, int] = {}
    for document in tomllib.loads(text).get("document", []):
        path = document["path"]
        if path.startswith("docs/"):
            counts[path[len("docs/") :]] = int(document.get("exec", 0))
    return counts


def workflow_paths(text: str) -> Set[str]:
    """Every literal entry of the workflow's ``on.<event>.paths`` lists.

    Read from the parsed YAML, so a comment, a step name or a script line that
    names a page is not an entry. A YAML 1.1 loader reads the bare key ``on``
    as the boolean ``True``; both spellings are looked up. ``paths-ignore`` is
    not ``paths``.
    """
    config = yaml.safe_load(text)
    if not isinstance(config, dict):
        return set()
    triggers = config["on"] if "on" in config else config.get(True)
    if not isinstance(triggers, dict):
        return set()
    entries: Set[str] = set()
    for event in triggers.values():
        if isinstance(event, dict) and isinstance(event.get("paths"), list):
            entries.update(p for p in event["paths"] if isinstance(p, str))
    return entries


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------
def _one_line(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != "" and "\n" not in value


def _class_problems(key: str, entry: Mapping[str, Any]) -> Tuple[str, List[str]]:
    """The entry's class family (``external`` for every service) and its problems."""
    cls = entry.get("class")
    if not isinstance(cls, str):
        return "", [f"{key}: no class"]
    match = EXTERNAL.fullmatch(cls)
    if match:
        service = match.group("service")
        if service == "":
            return "external", [f"{key}: class external: names no service"]
        if not SLUG.fullmatch(service):
            return "external", [
                f"{key}: external service {service!r} is not a lower-case slug"
            ]
        return "external", []
    if cls not in CLASSES:
        return "", [f"{key}: unknown class {cls!r}"]
    return cls, []


def problems(
    inventory: Mapping[str, Any],
    nav: Set[str],
    *,
    enrolled: Mapping[str, int],
    workflows: Mapping[str, Set[str]],
    written: Set[str],
) -> List[str]:
    """Every way the inventory breaks its contract; empty when it holds.

    ``enrolled`` is doc_examples.toml's exec count per page, ``workflows``
    each workflow file's stem with its literal ``on.<event>.paths`` entries
    (``workflow_paths``), ``written`` the journey ids with an expectations
    file.
    """
    found: List[str] = []
    unknown_top = set(inventory) - {"pages", "flow", "documents", "meta"}
    if unknown_top:
        found.append(f"unknown top-level keys: {sorted(unknown_top)}")

    pages = inventory.get("pages", {})
    if not isinstance(pages, dict):
        return found + ["[pages] is not a table"]

    for page in sorted(nav - set(pages)):
        found.append(f"unclassified nav page: {page}")
    for page in sorted(set(pages) - nav):
        found.append(f"classified page not in the nav: {page}")

    classes_seen: Set[str] = set()
    journey_pages: Dict[str, str] = {}
    for key in sorted(pages):
        entry = pages[key]
        if not isinstance(entry, dict):
            found.append(f"{key}: not a table")
            continue
        family, class_problems = _class_problems(key, entry)
        found.extend(class_problems)
        if family:
            classes_seen.add(entry["class"])
            classes_seen.add(family)
        allowed = FIELDS.get(family, PLAIN_FIELDS)
        extra = set(entry) - allowed
        if extra:
            found.append(
                f"{key}: fields not allowed on class {family!r}: {sorted(extra)}"
            )
        if not _one_line(entry.get("reason")):
            found.append(f"{key}: reason must be one non-empty line")

        found.extend(
            _entry_problems(
                key,
                entry,
                family,
                path=f"docs/{key}",
                exec_count=enrolled.get(key, 0),
                workflows=workflows,
                written=written,
                journey_pages=journey_pages,
            )
        )

    found.extend(
        _flow_problems(
            inventory.get("flow", []), set(pages), set(journey_pages), classes_seen
        )
    )
    return found


def _entry_problems(
    key: str,
    entry: Mapping[str, Any],
    family: str,
    *,
    path: str,
    exec_count: int,
    workflows: Mapping[str, Set[str]],
    written: Set[str],
    journey_pages: Dict[str, str],
) -> List[str]:
    """What the entry's class asks of it; the same for a page and a document.

    ``path`` is the file as a workflow's ``paths`` would list it (``docs/<page>``
    for a nav page, the repository path for a document), ``exec_count`` its exec
    blocks in scripts/doc_examples.toml. A journey's id is recorded in
    ``journey_pages``, so no two entries of either table share one.
    """
    found: List[str] = []
    if family == "journey":
        journey = entry.get("journey")
        if not isinstance(journey, str) or journey == "":
            return [f"{key}: journey without an id"]
        if not SLUG.fullmatch(journey):
            found.append(f"{key}: journey id {journey!r} is not a lower-case slug")
        if journey in journey_pages:
            found.append(
                f"{key}: journey id {journey!r} is also {journey_pages[journey]}'s"
                " (one expectations file per page)"
            )
        journey_pages[journey] = key
        if "pending" in entry and entry["pending"] is not True:
            found.append(f"{key}: pending must be true, or absent")
        elif entry.get("pending") is True and journey in written:
            found.append(
                f"{key}: journey {journey!r} has its expectations file;"
                " remove pending = true"
            )
        elif "pending" not in entry and journey not in written:
            found.append(
                f"{key}: journey {journey!r} has no expectations file"
                f" journeys/{journey}.yaml and is not pending = true"
            )
        if entry.get("pending") is True and entry.get("planned") not in PLANNED:
            found.append(
                f"{key}: a pending journey names the pull request that writes it:"
                f" planned = one of {', '.join(PLANNED)}"
            )
        elif entry.get("pending") is not True and "planned" in entry:
            found.append(f"{key}: planned is only for a pending journey")
    elif family == "exec":
        if exec_count < 1:
            found.append(
                f"{key}: class exec, but scripts/doc_examples.toml enrols no exec"
                " block of it"
            )
    elif family == "reference":
        if exec_count >= 1:
            found.append(
                f"{key}: class reference, but scripts/doc_examples.toml enrols"
                f" {path} with exec = {exec_count}: Doc Examples runs its steps,"
                " so it is class exec"
            )
    elif family == "workflow":
        workflow = entry.get("workflow")
        if not isinstance(workflow, str) or workflow not in workflows:
            found.append(
                f"{key}: workflow {workflow!r} is not a file in .github/workflows/"
            )
        elif path not in workflows[workflow]:
            found.append(
                f"{key}: workflow {workflow!r} does not run this page:"
                f" '{path}' is not a literal on.<event>.paths entry of"
                f" .github/workflows/{workflow}.yml"
            )
    elif family == "aws":
        found.extend(_exercised_by_problems(key, entry))
    return found


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _exercised_by_problems(key: str, entry: Mapping[str, Any]) -> List[str]:
    """An aws page's ``exercised_by``: values of ``EXERCISED_BY``, none twice."""
    if "exercised_by" not in entry:
        return [
            f"{key}: class aws needs exercised_by, the steps that exercise the"
            f' page (["{NONE}"] when no step does)'
        ]
    steps = entry["exercised_by"]
    if not _strings(steps) or not steps:
        return [f"{key}: exercised_by must be a non-empty list of strings"]
    found = [
        f"{key}: exercised_by {step!r} is not a step of #295 word for word,"
        " '605', 'production-deploy' or 'none'"
        for step in steps
        if step not in EXERCISED_BY
    ]
    for step in sorted({s for s in steps if steps.count(s) > 1}):
        found.append(f"{key}: exercised_by names {step!r} twice")
    if NONE in steps and len(steps) > 1:
        found.append(f"{key}: exercised_by {NONE!r} stands alone")
    return found


def _flow_problems(
    flows: Any, pages: Set[str], journeys: Set[str], classes: Set[str]
) -> List[str]:
    if not isinstance(flows, list):
        return ["[[flow]] is not an array of tables"]
    found: List[str] = []
    texts: List[str] = []
    for index, flow in enumerate(flows):
        if not isinstance(flow, dict):
            found.append(f"flow {index}: not a table")
            continue
        text = flow.get("text")
        label = repr(text) if isinstance(text, str) else f"flow {index}"
        if not isinstance(text, str):
            found.append(f"{label}: no text")
        else:
            texts.append(text)
        extra = set(flow) - FLOW_FIELDS
        if extra:
            found.append(f"{label}: fields not allowed: {sorted(extra)}")
        if not _one_line(flow.get("note")):
            found.append(f"{label}: note must be one non-empty line")
        lists = {name: flow.get(name, []) for name in ("journeys", "pages", "classes")}
        for name, value in lists.items():
            if not _strings(value):
                found.append(f"{label}: {name} must be a list of strings")
                lists[name] = []
        if not any(lists.values()):
            found.append(f"{label}: maps to no journey, page or class")
        for journey in lists["journeys"]:
            if journey not in journeys:
                found.append(f"{label}: journey {journey!r} is no page's journey")
        for page in lists["pages"]:
            if page not in pages:
                found.append(f"{label}: page {page!r} is not in [pages]")
        for cls in lists["classes"]:
            if cls not in classes:
                found.append(f"{label}: no page has class {cls!r}")

    for text in sorted({t for t in texts if texts.count(t) > 1}):
        found.append(f"flow mapped twice: {text!r}")
    for text in FLOWS_939:
        if text not in texts:
            found.append(f"#939 flow not mapped: {text!r}")
    for text in texts:
        if text not in FLOWS_939:
            found.append(f"flow not in #939's list: {text!r}")
    return found


# ---------------------------------------------------------------------------
# [documents]: every other file the walk finds, and the named surfaces
# ---------------------------------------------------------------------------
#: A desc: or description: key of the docs hub, whatever its value is.
HUB_KEY = re.compile(r"\b(?:desc|description)['\"]?[ \t]{0,8}:")
#: The same key whose whole value is one single- or double-quoted literal of
#: at most 500 characters: the literal must end the value (a comma, a closing
#: bracket or the end of the line follows), so a concatenation is not read as
#: its first piece. Every quantifier is bounded, and in a literal a character
#: can match only one of the two alternatives, so no input makes it backtrack
#: past the bound.
HUB_LITERAL = re.compile(
    r"\b(?:desc|description)['\"]?[ \t]{0,8}:\s{0,40}"
    r"(?:'(?P<single>(?:[^'\\\n]|\\.){0,500})'"
    r"|\"(?P<double>(?:[^\"\\\n]|\\.){0,500})\")"
    r"(?=[ \t]{0,8}(?:[,}\])\n]|\Z))"
)


def docs_hub_desc(text: str) -> Tuple[List[str], List[int], int]:
    """The docs hub's desc:/description: literals, as written between quotes.

    Also the line of each key whose value is not such a literal (a template
    literal, a concatenation, a string over 500 characters), and the number of
    keys. The extractor reads every key or the check says which it did not.
    """
    literals: Dict[int, str] = {}
    for match in HUB_LITERAL.finditer(text):
        single = match.group("single")
        literals[match.start()] = (
            single if single is not None else match.group("double")
        )
    keys = [match.start() for match in HUB_KEY.finditer(text)]
    unread = [text.count("\n", 0, start) + 1 for start in keys if start not in literals]
    return list(literals.values()), unread, len(keys)


def leading_hash_comments(text: str) -> List[str]:
    """The comment block at the top of a file (after a ``#!`` line), as one unit."""
    lines = text.splitlines()
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    block: List[str] = []
    for line in lines:
        if not line.startswith("#"):
            break
        block.append(line[1:].strip())
    header = "\n".join(block).strip()
    return [header] if header else []


EXTRACTORS: Dict[str, Callable[[str], List[str]]] = {
    "text": lambda text: [text] if text.strip() else [],
    "leading-hash-comments": leading_hash_comments,
    "docs-hub-desc": lambda text: docs_hub_desc(text)[0],
}


def enrolled_documents(text: str) -> Dict[str, int]:
    """Each file doc_examples.toml enrols, by repository path, with its exec count."""
    return {
        document["path"]: int(document.get("exec", 0))
        for document in tomllib.loads(text).get("document", [])
    }


def _meta_problems(meta: Any, walked: Set[str]) -> List[str]:
    if (
        not isinstance(meta, dict)
        or set(meta) != {"walked"}
        or type(meta["walked"]) is not int
    ):
        return [
            "[meta] needs exactly walked = <the number of files"
            " scripts/doc_examples.py:walk() finds>"
        ]
    if len(walked) != meta["walked"]:
        return [
            f"walked {len(walked)} documents, the inventory names exactly"
            f" {meta['walked']}"
        ]
    return []


def _hub_problems(
    key: str, path: str, text: str, entry: Mapping[str, Any]
) -> List[str]:
    """Every hub key's value is read, and the inventory names how many exactly."""
    literals, unread, keys = docs_hub_desc(text)
    found: List[str] = []
    if unread:
        found.append(
            f"{key}: the docs-hub-desc extractor read {len(literals)} literals, the"
            f" file has {keys} desc:/description: keys; line"
            f" {', '.join(str(n) for n in unread)} holds a value it cannot read (a"
            " single- or double-quoted string of at most 500 characters)"
        )
    named = entry.get("literals")
    if type(named) is not int:
        return found + [
            f"{key}: literals must be the number of desc:/description: literals"
            f" in {path}"
        ]
    if len(literals) != named:
        found.append(
            f"{key}: the docs-hub-desc extractor read {len(literals)} literals, the"
            f" inventory names exactly {named}"
        )
    return found


def _surface_problems(
    key: str, entry: Any, read: Callable[[str], Optional[str]]
) -> List[str]:
    """The surface's file exists, and its extractor reads it."""
    path = key.partition("#")[0]
    text = read(path)
    if text is None:
        return [f"named surface {path} does not exist"]
    if not isinstance(entry, dict):
        return [f"named surface {key} has no entry in [documents]"]
    if entry.get("scope") != "in":
        return [f'{key}: a named surface is shown to users: scope = "in"']
    extractor = SURFACES[key]
    if extractor is None:
        if entry.get("class") != "journey":
            return [
                f'{key}: has no extractor, so its journey holds it: class = "journey"'
            ]
        return []
    if entry.get("extractor") != extractor:
        return [f"{key}: extractor = {extractor!r}"]
    found: List[str] = []
    if not EXTRACTORS[extractor](text):
        found.append(f"{key}: the {extractor} extractor reads nothing from {path}")
    if key == HUB:
        found.extend(_hub_problems(key, path, text, entry))
    return found


def document_problems(
    inventory: Mapping[str, Any],
    walked: Set[str],
    *,
    enrolled: Mapping[str, int],
    workflows: Mapping[str, Set[str]],
    written: Set[str],
    read: Callable[[str], Optional[str]],
) -> List[str]:
    """Every way [documents] and [meta] break the contract; empty when they hold.

    ``walked`` is what scripts/doc_examples.py:walk() finds, ``enrolled``
    doc_examples.toml's exec count per repository path, ``read`` a file's text
    by repository path (None when there is no such file). [pages] itself is
    problems()'s; here it only says which walked files are nav pages.
    """
    pages = inventory.get("pages", {})
    pages = pages if isinstance(pages, dict) else {}
    documents = inventory.get("documents", {})
    if not isinstance(documents, dict):
        return ["[documents] is not a table"]
    found = _meta_problems(inventory.get("meta"), walked)

    nav_files = {f"docs/{page}" for page in pages}
    for rel in sorted(walked - nav_files - set(documents)):
        found.append(
            f"{rel}: user-facing document with no D73 class (class+reason, or"
            " excluded+reason)"
        )
    for rel in sorted(nav_files - walked):
        found.append(f"{rel}: a nav page the walk does not find")

    journey_pages = {
        entry["journey"]: page
        for page, entry in pages.items()
        if isinstance(entry, dict)
        and entry.get("class") == "journey"
        and isinstance(entry.get("journey"), str)
    }
    for key in sorted(documents):
        entry = documents[key]
        if not isinstance(entry, dict):
            found.append(f"{key}: not a table")
            continue
        if key in nav_files:
            found.append(
                f'{key}: a nav page, classified as [pages."{key[len("docs/") :]}"],'
                " not under [documents]"
            )
            continue
        if key not in SURFACES and key not in walked:
            if key.lower().endswith(".md"):
                found.append(
                    f"{key}: in [documents] but the walk does not find it. Renamed?"
                    " Change the key. Deleted? Remove the entry."
                )
            else:
                found.append(
                    f"{key}: not a named surface; the closed list is"
                    f" {', '.join(SURFACES)}"
                )
            continue
        scope = entry.get("scope")
        if scope == "out":
            extra = set(entry) - OUT_FIELDS
            if extra:
                found.append(
                    f"{key}: fields not allowed on scope 'out': {sorted(extra)}"
                )
            if entry.get("out") not in OUT_CATEGORIES:
                found.append(
                    f"{key}: out = one of {', '.join(OUT_CATEGORIES)}, not"
                    f" {entry.get('out')!r}"
                )
            reason = entry.get("reason")
            if not _one_line(reason) or len(reason) > REASON_MAX:
                found.append(
                    f"{key}: excluded needs a reason (1-{REASON_MAX} characters)"
                )
        elif scope == "in":
            family, class_problems = _class_problems(key, entry)
            found.extend(class_problems)
            allowed = FIELDS.get(family, PLAIN_FIELDS) | {"scope"}
            if SURFACES.get(key) is not None:
                allowed = (
                    allowed | {"extractor"} | ({"literals"} if key == HUB else set())
                )
            extra = set(entry) - allowed
            if extra:
                found.append(
                    f"{key}: fields not allowed on class {family!r}: {sorted(extra)}"
                )
            if not _one_line(entry.get("reason")):
                found.append(f"{key}: reason must be one non-empty line")
            found.extend(
                _entry_problems(
                    key,
                    entry,
                    family,
                    path=key.partition("#")[0],
                    exec_count=enrolled.get(key, 0),
                    workflows=workflows,
                    written=written,
                    journey_pages=journey_pages,
                )
            )
        else:
            found.append(f'{key}: scope must be "in" or "out"')

    for key in SURFACES:
        found.extend(_surface_problems(key, documents.get(key), read))
    return found


# ---------------------------------------------------------------------------
# The real tree
# ---------------------------------------------------------------------------
def _real_nav() -> Tuple[Set[str], List[str]]:
    return nav_pages(load_mkdocs(MKDOCS.read_text(encoding="utf-8")))


def _real_context() -> Dict[str, Any]:
    return {
        "enrolled": enrolled_exec_counts(DOC_EXAMPLES.read_text(encoding="utf-8")),
        "workflows": {
            p.stem: workflow_paths(p.read_text(encoding="utf-8"))
            for p in WORKFLOWS.glob("*.yml")
        },
        "written": {p.stem for p in JOURNEYS.glob("*.yaml")},
    }


def test_every_nav_page_is_classified_and_every_flow_mapped():
    nav, found = _real_nav()
    inventory, parse_found = parse_inventory(INVENTORY.read_text(encoding="utf-8"))
    found += parse_found or problems(inventory, nav, **_real_context())
    assert not found, "\n".join(found) + "\n\n" + HOW_TO_FIX


@functools.lru_cache(maxsize=1)
def _walked() -> Tuple[str, ...]:
    """scripts/doc_examples.py:walk() over this tree, loaded by path.

    A module name of its own, so the module test_doc_examples.py registers as
    ``doc_examples`` is not replaced (its dataclasses look their module up).
    """
    spec = importlib.util.spec_from_file_location(
        "doc_examples_inventory_walk", DOC_EXAMPLES_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return tuple(sorted(module.walk()))


def _read(path: str) -> Optional[str]:
    file = REPO_ROOT / path
    return file.read_text(encoding="utf-8") if file.is_file() else None


def _document_context() -> Dict[str, Any]:
    context = _real_context()
    context["enrolled"] = enrolled_documents(DOC_EXAMPLES.read_text(encoding="utf-8"))
    context["read"] = _read
    return context


def test_every_document_the_walk_finds_is_classified_exactly_once():
    inventory, found = parse_inventory(INVENTORY.read_text(encoding="utf-8"))
    found = found or document_problems(inventory, set(_walked()), **_document_context())
    assert not found, "\n".join(found) + "\n\n" + HOW_TO_CLASSIFY


def test_the_docs_hub_extractor_reads_every_key_in_both_quotings():
    """Each desc:/description: key of the real hub is read, single- and
    double-quoted alike, and the inventory names that number exactly."""
    text = _read(HUB.partition("#")[0])
    assert text is not None
    literals, unread, keys = docs_hub_desc(text)
    inventory, _ = parse_inventory(INVENTORY.read_text(encoding="utf-8"))
    assert unread == []
    assert len(literals) == keys == inventory["documents"][HUB]["literals"] > 0
    double = [m for m in HUB_LITERAL.finditer(text) if m.group("double") is not None]
    assert double, "the hub has double-quoted literals; the extractor must read them"


#: The workflow page of the real tree, and two workflows that exist but do not
#: run it: `docs` lists `docs/**` in its paths (a glob, not the page), and
#: `leak-guard` has no paths at all. Either one passed before #1075.
WORKFLOW_PAGE = "self-hosting/kubernetes.md"


@pytest.mark.regression
@pytest.mark.parametrize("workflow", ["docs", "leak-guard"])
def test_a_workflow_page_naming_a_workflow_that_does_not_list_it_is_refused(
    workflow,
):
    nav, _found = _real_nav()
    inventory, _parse = parse_inventory(INVENTORY.read_text(encoding="utf-8"))
    context = _real_context()
    entry = inventory["pages"][WORKFLOW_PAGE]
    assert (entry["class"], entry["workflow"]) == ("workflow", "chart-kind")
    assert f"docs/{WORKFLOW_PAGE}" in context["workflows"]["chart-kind"]
    assert workflow in context["workflows"]

    entry["workflow"] = workflow
    found = problems(inventory, nav, **context)
    assert found == [
        f"{WORKFLOW_PAGE}: workflow {workflow!r} does not run this page:"
        f" 'docs/{WORKFLOW_PAGE}' is not a literal on.<event>.paths entry of"
        f" .github/workflows/{workflow}.yml"
    ]


def test_workflow_paths_reads_only_the_literal_on_paths_entries():
    text = (
        "# docs/a.md is run below, by name (a comment, not an entry)\n"
        "name: A\n"
        "on:\n"
        "  pull_request:\n"
        "    paths:\n"
        "      - 'docs/a.md'\n"
        "      - 'docs/**'\n"
        "  push:\n"
        "    branches: [main]\n"
        "    paths-ignore: ['docs/b.md']\n"
        "  schedule:\n"
        "    - cron: '0 0 * * *'\n"
        "jobs:\n"
        "  run:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: docs/c.md\n"
        "        run: scripts/x.sh docs/c.md\n"
    )
    assert workflow_paths(text) == {"docs/a.md", "docs/**"}
    assert workflow_paths("on: [push, pull_request]\n") == set()
    assert workflow_paths("on: push\n") == set()
    assert workflow_paths("'on':\n  push:\n    paths: [docs/a.md]\n") == {"docs/a.md"}


def test_the_nav_parsed_to_real_pages():
    """A nav that came back empty would leave nothing for the check above."""
    nav, _found = _real_nav()
    assert len(nav) > 100, f"the nav parsed to {len(nav)} pages"
    assert "getting-started/quick-start.md" in nav


# ---------------------------------------------------------------------------
# Planted defects: each refusal fires
# ---------------------------------------------------------------------------
GOOD = """
[pages."a.md"]
class = "journey"
journey = "a-journey"
pending = true
planned = "D3a"
reason = "walked"

[pages."b.md"]
class = "exec"
reason = "run by doc examples"

[pages."c.md"]
class = "workflow"
workflow = "chart-kind"
reason = "run by a workflow"

[pages."d.md"]
class = "external:salesforce"
reason = "needs a third party"

[pages."e.md"]
class = "reference"
reason = "nothing to walk"

[pages."f.md"]
class = "aws"
exercised_by = ["none"]
reason = "needs AWS"

[pages."j.md"]
class = "aws"
exercised_by = ["run the first stack deploy", "605", "production-deploy"]
reason = "needs AWS"

[pages."g.md"]
class = "device"
reason = "needs a simulator"
"""

NAV = {"a.md", "b.md", "c.md", "d.md", "e.md", "f.md", "g.md", "j.md"}
CONTEXT: Dict[str, Any] = {
    "enrolled": {"b.md": 3},
    "workflows": {"chart-kind": {"docs/c.md", "charts/**"}},
    "written": set(),
}
#: j.md's mapping, which the exercised_by plants edit.
J_STEPS = 'exercised_by = ["run the first stack deploy", "605", "production-deploy"]'


def _flows(skip: Iterable[str] = ()) -> str:
    blocks = [
        f'[[flow]]\ntext = "{text}"\njourneys = ["a-journey"]\nnote = "mapped"\n'
        for text in FLOWS_939
        if text not in skip
    ]
    return "\n" + "\n".join(blocks)


def _check(text: str, nav: Set[str] = NAV, **context: Any) -> List[str]:
    inventory, found = parse_inventory(text)
    if found:
        return found
    return problems(inventory, nav, **{**CONTEXT, **context})


#: The mapping line every flow of the fixture carries; the plants edit the first.
MAPPED = 'journeys = ["a-journey"]'


def test_the_good_fixture_passes():
    assert _check(GOOD + _flows()) == []


PLANTS = [
    pytest.param(
        GOOD + _flows(),
        NAV | {"new.md"},
        {},
        "unclassified nav page: new.md",
        id="unclassified-nav-page",
    ),
    pytest.param(
        GOOD + '\n[pages."gone.md"]\nclass = "reference"\nreason = "x"\n' + _flows(),
        NAV,
        {},
        "classified page not in the nav: gone.md",
        id="classified-page-not-in-nav",
    ),
    pytest.param(
        GOOD + '\n[pages."e.md"]\nclass = "reference"\nreason = "again"\n' + _flows(),
        NAV,
        {},
        "does not parse",
        id="duplicate-key",
    ),
    pytest.param(
        GOOD.replace('class = "aws"', 'class = "manual"') + _flows(),
        NAV,
        {},
        "f.md: unknown class 'manual'",
        id="unknown-class",
    ),
    pytest.param(
        GOOD.replace('"external:salesforce"', '"external:"') + _flows(),
        NAV,
        {},
        "d.md: class external: names no service",
        id="external-without-service",
    ),
    pytest.param(
        GOOD.replace('"external:salesforce"', '"external"') + _flows(),
        NAV,
        {},
        "d.md: unknown class 'external'",
        id="external-without-colon",
    ),
    pytest.param(
        GOOD.replace('"external:salesforce"', '"external:Sales Force"') + _flows(),
        NAV,
        {},
        "is not a lower-case slug",
        id="external-service-not-a-slug",
    ),
    pytest.param(
        GOOD.replace('journey = "a-journey"\n', "") + _flows(),
        NAV,
        {},
        "a.md: journey without an id",
        id="journey-without-id",
    ),
    pytest.param(
        GOOD.replace('pending = true\nplanned = "D3a"\n', "") + _flows(),
        NAV,
        {},
        "has no expectations file",
        id="journey-neither-written-nor-pending",
    ),
    pytest.param(
        GOOD + _flows(),
        NAV,
        {"written": {"a-journey"}},
        "remove pending = true",
        id="pending-after-the-file-exists",
    ),
    pytest.param(
        GOOD.replace('planned = "D3a"\n', "") + _flows(),
        NAV,
        {},
        "a.md: a pending journey names the pull request that writes it",
        id="pending-without-planned",
    ),
    pytest.param(
        GOOD.replace('planned = "D3a"', 'planned = "D9"') + _flows(),
        NAV,
        {},
        "a.md: a pending journey names the pull request that writes it",
        id="planned-unknown",
    ),
    pytest.param(
        GOOD.replace("pending = true\n", "") + _flows(),
        NAV,
        {"written": {"a-journey"}},
        "a.md: planned is only for a pending journey",
        id="planned-on-a-written-journey",
    ),
    pytest.param(
        GOOD.replace("pending = true", "pending = false") + _flows(),
        NAV,
        {},
        "pending must be true",
        id="pending-false",
    ),
    pytest.param(
        GOOD.replace(
            'class = "reference"\nreason = "nothing to walk"',
            'class = "journey"\njourney = "a-journey"\n'
            'pending = true\nplanned = "D3b"\nreason = "nothing to walk"',
        )
        + _flows(),
        NAV,
        {},
        "is also a.md's",
        id="journey-id-shared",
    ),
    pytest.param(
        GOOD + _flows(),
        NAV,
        {"enrolled": {}},
        "b.md: class exec, but",
        id="exec-not-enrolled",
    ),
    pytest.param(
        GOOD + _flows(),
        NAV,
        {"enrolled": {"b.md": 3, "e.md": 1}},
        "e.md: class reference, but scripts/doc_examples.toml enrols docs/e.md with"
        " exec = 1: Doc Examples runs its steps, so it is class exec",
        id="reference-page-with-exec-blocks",
    ),
    pytest.param(
        GOOD + _flows(),
        NAV,
        {"workflows": {}},
        "c.md: workflow 'chart-kind' is not a file",
        id="workflow-missing",
    ),
    pytest.param(
        GOOD + _flows(),
        NAV,
        {"workflows": {"chart-kind": set()}},
        "c.md: workflow 'chart-kind' does not run this page",
        id="workflow-without-paths",
    ),
    pytest.param(
        GOOD + _flows(),
        NAV,
        {"workflows": {"chart-kind": {"docs/**", "docs/*.md", "c.md"}}},
        "c.md: workflow 'chart-kind' does not run this page",
        id="workflow-paths-glob-only",
    ),
    pytest.param(
        GOOD.replace('exercised_by = ["none"]\n', "") + _flows(),
        NAV,
        {},
        "f.md: class aws needs exercised_by",
        id="aws-without-exercised-by",
    ),
    pytest.param(
        GOOD.replace('exercised_by = ["none"]', "exercised_by = []") + _flows(),
        NAV,
        {},
        "f.md: exercised_by must be a non-empty list of strings",
        id="aws-exercised-by-empty",
    ),
    pytest.param(
        GOOD.replace('exercised_by = ["none"]', 'exercised_by = "none"') + _flows(),
        NAV,
        {},
        "f.md: exercised_by must be a non-empty list of strings",
        id="aws-exercised-by-not-a-list",
    ),
    pytest.param(
        GOOD.replace(J_STEPS, 'exercised_by = ["bootstrap the account;"]') + _flows(),
        NAV,
        {},
        "j.md: exercised_by 'bootstrap the account;' is not a step of #295",
        id="aws-step-not-verbatim",
    ),
    pytest.param(
        GOOD.replace(J_STEPS, 'exercised_by = ["Run the first stack deploy"]')
        + _flows(),
        NAV,
        {},
        "j.md: exercised_by 'Run the first stack deploy' is not a step of #295",
        id="aws-step-case-differs",
    ),
    pytest.param(
        GOOD.replace(J_STEPS, 'exercised_by = ["a step of our own"]') + _flows(),
        NAV,
        {},
        "j.md: exercised_by 'a step of our own' is not a step of #295",
        id="aws-step-invented",
    ),
    pytest.param(
        GOOD.replace(J_STEPS, 'exercised_by = ["605", "605"]') + _flows(),
        NAV,
        {},
        "j.md: exercised_by names '605' twice",
        id="aws-step-twice",
    ),
    pytest.param(
        GOOD.replace(J_STEPS, 'exercised_by = ["605", "none"]') + _flows(),
        NAV,
        {},
        "j.md: exercised_by 'none' stands alone",
        id="aws-none-with-a-step",
    ),
    pytest.param(
        GOOD.replace(
            'class = "reference"', 'class = "reference"\nexercised_by = ["none"]'
        )
        + _flows(),
        NAV,
        {},
        "e.md: fields not allowed on class 'reference': ['exercised_by']",
        id="exercised-by-off-aws",
    ),
    pytest.param(
        GOOD.replace('reason = "needs AWS"', 'reason = ""') + _flows(),
        NAV,
        {},
        "f.md: reason must be one non-empty line",
        id="empty-reason",
    ),
    pytest.param(
        GOOD.replace('reason = "needs AWS"', 'reason = """two\nlines"""') + _flows(),
        NAV,
        {},
        "f.md: reason must be one non-empty line",
        id="two-line-reason",
    ),
    pytest.param(
        GOOD.replace('class = "aws"', 'class = "aws"\nworkflow = "chart-kind"')
        + _flows(),
        NAV,
        {},
        "f.md: fields not allowed",
        id="field-not-allowed",
    ),
    pytest.param(
        GOOD + _flows(skip=[FLOWS_939[6]]),
        NAV,
        {},
        "#939 flow not mapped: 'the SDK quick starts'",
        id="flow-unmapped",
    ),
    pytest.param(
        GOOD + _flows() + '\n[[flow]]\ntext = "a flow of our own"\n'
        'journeys = ["a-journey"]\nnote = "x"\n',
        NAV,
        {},
        "flow not in #939's list",
        id="flow-invented",
    ),
    pytest.param(
        GOOD + _flows().replace('journeys = ["a-journey"]', "journeys = []", 1),
        NAV,
        {},
        "maps to no journey, page or class",
        id="flow-maps-to-nothing",
    ),
    pytest.param(
        GOOD + _flows().replace('"a-journey"', '"no-such-journey"', 1),
        NAV,
        {},
        "journey 'no-such-journey' is no page's journey",
        id="flow-names-undeclared-journey",
    ),
    pytest.param(
        GOOD + _flows().replace(MAPPED, MAPPED + '\npages = ["nowhere.md"]', 1),
        NAV,
        {},
        "page 'nowhere.md' is not in [pages]",
        id="flow-names-unknown-page",
    ),
    pytest.param(
        GOOD + _flows().replace(MAPPED, MAPPED + '\nclasses = ["external:github"]', 1),
        NAV,
        {},
        "no page has class 'external:github'",
        id="flow-names-class-no-page-has",
    ),
    pytest.param(
        GOOD + _flows() + _flows(skip=FLOWS_939[1:]),
        NAV,
        {},
        "flow mapped twice",
        id="flow-mapped-twice",
    ),
    pytest.param(
        GOOD + _flows() + "\n[extra]\nkey = 1\n",
        NAV,
        {},
        "unknown top-level keys: ['extra']",
        id="unknown-top-level-key",
    ),
    pytest.param(
        GOOD.replace('journey = "a-journey"', 'journey = "A Journey"') + _flows(),
        NAV,
        {},
        "journey id 'A Journey' is not a lower-case slug",
        id="journey-id-not-a-slug",
    ),
    pytest.param(
        GOOD + _flows().replace('note = "mapped"', 'note = ""', 1),
        NAV,
        {},
        "note must be one non-empty line",
        id="flow-note-empty",
    ),
    pytest.param(
        GOOD + _flows().replace('note = "mapped"', 'note = "mapped"\nowner = "x"', 1),
        NAV,
        {},
        "fields not allowed: ['owner']",
        id="flow-field-not-allowed",
    ),
    pytest.param(
        GOOD.replace('class = "device"\n', "") + _flows(),
        NAV,
        {},
        "g.md: no class",
        id="entry-without-class",
    ),
    pytest.param(
        "pages = 3\n" + _flows(),
        NAV,
        {},
        "[pages] is not a table",
        id="pages-not-a-table",
    ),
    pytest.param(
        '[pages]\n"h.md" = "reference"\n' + GOOD + _flows(),
        NAV | {"h.md"},
        {},
        "h.md: not a table",
        id="entry-not-a-table",
    ),
    pytest.param(
        "flow = 3\n" + GOOD,
        NAV,
        {},
        "[[flow]] is not an array of tables",
        id="flows-not-an-array",
    ),
    pytest.param(
        "flow = [1]\n" + GOOD,
        NAV,
        {},
        "flow 0: not a table",
        id="flow-not-a-table",
    ),
    pytest.param(
        GOOD + _flows().replace(f'text = "{FLOWS_939[0]}"\n', "", 1),
        NAV,
        {},
        "flow 0: no text",
        id="flow-without-text",
    ),
    pytest.param(
        GOOD + _flows().replace(MAPPED, 'journeys = "a-journey"', 1),
        NAV,
        {},
        "journeys must be a list of strings",
        id="flow-list-not-strings",
    ),
]


@pytest.mark.parametrize("text, nav, context, expected", PLANTS)
def test_planted_defect_is_refused(text, nav, context, expected):
    found = _check(text, nav, **context)
    assert any(expected in line for line in found), found


# ---------------------------------------------------------------------------
# Planted defects in [documents] and [meta]: each refusal fires (#1142)
# ---------------------------------------------------------------------------
DOCS_GOOD = """
[meta]
walked = 5

[pages."a.md"]
class = "reference"
reason = "nothing to walk"

[pages."j.md"]
class = "journey"
journey = "a-journey"
pending = true
planned = "D3a"
reason = "walked"

[documents."README.md"]
scope = "in"
class = "exec"
reason = "its blocks run in Doc Examples"

[documents."CONTRIBUTING.md"]
scope = "out"
out = "contributor"
reason = "for contributors"

[documents."sdk/x/README.md"]
scope = "in"
class = "device"
reason = "needs a simulator"

[documents."charts/experimently/templates/NOTES.txt"]
scope = "in"
class = "reference"
extractor = "text"
reason = "printed by helm install"

[documents."deploy/compose/compose.yml#header"]
scope = "in"
class = "reference"
extractor = "leading-hash-comments"
reason = "the header"

[documents."deploy/compose/generate-env.sh#header"]
scope = "in"
class = "reference"
extractor = "leading-hash-comments"
reason = "the usage header"

[documents."frontend/src/pages/docs/index.tsx#desc"]
scope = "in"
class = "reference"
extractor = "docs-hub-desc"
literals = 3
reason = "the docs hub"

[documents."frontend/src/components/experiments/FirstRunChecklist.tsx"]
scope = "in"
class = "journey"
journey = "first-run"
pending = true
planned = "D73"
reason = "the first-run card"
"""

WALKED = {"docs/a.md", "docs/j.md", "README.md", "CONTRIBUTING.md", "sdk/x/README.md"}
HUB_TEXT = """const sections = [
  {
    description: 'Run the stack',
    links: [
      { label: 'JS', desc: 'Browser and Node.js' },
      { label: 'CUPED', desc: "Variance reduction from each user's own events" },
    ],
  },
];
"""
#: The hub's last card, which the hub plants add a card after.
HUB_LAST = """desc: "Variance reduction from each user's own events" },"""
FILES: Dict[str, str] = {
    "charts/experimently/templates/NOTES.txt": "Controlvar is installing.\n",
    "deploy/compose/compose.yml": "# The production compose file.\nname: x\n",
    "deploy/compose/generate-env.sh": "#!/bin/sh\n# Writes .env.\nset -eu\n",
    "frontend/src/pages/docs/index.tsx": HUB_TEXT,
    "frontend/src/components/experiments/FirstRunChecklist.tsx": "export {};\n",
}
DOCS_CONTEXT: Dict[str, Any] = {
    "enrolled": {"README.md": 2},
    "workflows": {"chart-kind": {"docs/c.md", "sdk/**"}},
    "written": set(),
}
OUT_REASON = 'reason = "for contributors"'


def _doc_check(
    text: str,
    walked: Set[str] = WALKED,
    files: Optional[Mapping[str, str]] = None,
    **context: Any,
) -> List[str]:
    inventory, found = parse_inventory(text)
    if found:
        return found
    files = FILES if files is None else files
    return document_problems(
        inventory, walked, read=files.get, **{**DOCS_CONTEXT, **context}
    )


def _hub(card: str) -> Dict[str, str]:
    """FILES with one more card on the hub, after its last one."""
    return {
        **FILES,
        "frontend/src/pages/docs/index.tsx": HUB_TEXT.replace(
            HUB_LAST, HUB_LAST + "\n      " + card
        ),
    }


def test_the_good_documents_fixture_passes():
    assert _doc_check(DOCS_GOOD) == []


UNCLASSIFIED = (
    "user-facing document with no D73 class (class+reason, or excluded+reason)"
)

DOC_PLANTS = [
    # G1a, G1b: a new Markdown file anywhere in the walk refuses until classified.
    pytest.param(
        DOCS_GOOD.replace("walked = 5", "walked = 6"),
        WALKED | {"docs/guides/offline-mode.md"},
        None,
        {},
        f"docs/guides/offline-mode.md: {UNCLASSIFIED}",
        id="G1a-new-docs-page-not-in-the-nav",
        marks=pytest.mark.regression,
    ),
    pytest.param(
        DOCS_GOOD.replace("walked = 5", "walked = 6"),
        WALKED | {"sdk/kotlin/README.md"},
        None,
        {},
        f"sdk/kotlin/README.md: {UNCLASSIFIED}",
        id="G1b-new-sdk-readme",
        marks=pytest.mark.regression,
    ),
    # G1c: an exclusion says why, in at most 200 characters.
    pytest.param(
        DOCS_GOOD.replace(OUT_REASON, 'reason = ""'),
        WALKED,
        None,
        {},
        "CONTRIBUTING.md: excluded needs a reason (1-200 characters)",
        id="G1c-exclusion-reason-empty",
    ),
    pytest.param(
        DOCS_GOOD.replace(OUT_REASON + "\n", ""),
        WALKED,
        None,
        {},
        "CONTRIBUTING.md: excluded needs a reason (1-200 characters)",
        id="G1c-exclusion-without-reason",
    ),
    pytest.param(
        DOCS_GOOD.replace(OUT_REASON, f'reason = "{"x" * 201}"'),
        WALKED,
        None,
        {},
        "CONTRIBUTING.md: excluded needs a reason (1-200 characters)",
        id="G1c-exclusion-reason-over-200",
    ),
    # G1d: the named surfaces exist, are classified, and their extractors read.
    pytest.param(
        DOCS_GOOD,
        WALKED,
        {k: v for k, v in FILES.items() if not k.endswith("NOTES.txt")},
        {},
        "named surface charts/experimently/templates/NOTES.txt does not exist",
        id="G1d-surface-file-gone",
    ),
    pytest.param(
        DOCS_GOOD + '\n[documents."charts/experimently/Chart.yaml"]\nscope = "in"\n'
        'class = "reference"\nreason = "x"\n',
        WALKED,
        None,
        {},
        "charts/experimently/Chart.yaml: not a named surface; the closed list is",
        id="G1d-surface-not-in-the-closed-list",
    ),
    pytest.param(
        DOCS_GOOD.replace(
            '[documents."charts/experimently/templates/NOTES.txt"]\nscope = "in"\n'
            'class = "reference"\nextractor = "text"\nreason = "printed by helm'
            ' install"\n',
            "",
        ),
        WALKED,
        None,
        {},
        "named surface charts/experimently/templates/NOTES.txt has no entry in"
        " [documents]",
        id="G1d-surface-unclassified",
    ),
    pytest.param(
        DOCS_GOOD,
        WALKED,
        {**FILES, "deploy/compose/compose.yml": "name: x\n# not at the top\n"},
        {},
        "deploy/compose/compose.yml#header: the leading-hash-comments extractor"
        " reads nothing from deploy/compose/compose.yml",
        id="G1d-header-surface-without-a-header",
    ),
    pytest.param(
        DOCS_GOOD,
        WALKED,
        {**FILES, "deploy/compose/generate-env.sh": "#!/bin/sh\n#\nset -eu\n"},
        {},
        "deploy/compose/generate-env.sh#header: the leading-hash-comments extractor"
        " reads nothing",
        id="G1d-header-of-empty-comments",
    ),
    pytest.param(
        DOCS_GOOD.replace('extractor = "text"', 'extractor = "leading-hash-comments"'),
        WALKED,
        None,
        {},
        "charts/experimently/templates/NOTES.txt: extractor = 'text'",
        id="G1d-wrong-extractor",
    ),
    pytest.param(
        DOCS_GOOD.replace(
            'reason = "the first-run card"', 'reason = "x"\nextractor = "text"'
        ),
        WALKED,
        None,
        {},
        "frontend/src/components/experiments/FirstRunChecklist.tsx: fields not"
        " allowed on class 'journey': ['extractor']",
        id="G1d-extractor-on-the-journey-surface",
    ),
    pytest.param(
        DOCS_GOOD.replace(
            'class = "journey"\njourney = "first-run"\npending = true\nplanned = "D73"\n',
            'class = "reference"\n',
        ),
        WALKED,
        None,
        {},
        "FirstRunChecklist.tsx: has no extractor, so its journey holds it",
        id="G1d-journey-surface-not-a-journey",
    ),
    pytest.param(
        DOCS_GOOD.replace(
            'class = "reference"\nextractor = "docs-hub-desc"',
            'out = "template"\nextractor = "docs-hub-desc"',
        ).replace(
            '[documents."frontend/src/pages/docs/index.tsx#desc"]\nscope = "in"',
            '[documents."frontend/src/pages/docs/index.tsx#desc"]\nscope = "out"',
        ),
        WALKED,
        None,
        {},
        'index.tsx#desc: a named surface is shown to users: scope = "in"',
        id="G1d-surface-excluded",
    ),
    pytest.param(
        DOCS_GOOD.replace('extractor = "text"', 'extractor = "text"\nliterals = 1'),
        WALKED,
        None,
        {},
        "NOTES.txt: fields not allowed on class 'reference': ['literals']",
        id="literals-off-the-hub",
    ),
    # G1e: the walk's count is exact, never "more than none".
    pytest.param(
        DOCS_GOOD.replace("walked = 5", "walked = 6"),
        WALKED,
        None,
        {},
        "walked 5 documents, the inventory names exactly 6",
        id="G1e-count-differs",
    ),
    pytest.param(
        DOCS_GOOD,
        set(),
        None,
        {},
        "walked 0 documents, the inventory names exactly 5",
        id="G1e-walk-finds-nothing",
    ),
    pytest.param(
        DOCS_GOOD,
        WALKED - {"docs/a.md", "docs/j.md"},
        None,
        {},
        "walked 3 documents, the inventory names exactly 5",
        id="G1e-docs-pruned",
    ),
    pytest.param(
        DOCS_GOOD,
        WALKED - {"docs/a.md"},
        None,
        {},
        "docs/a.md: a nav page the walk does not find",
        id="nav-page-outside-the-walk",
    ),
    pytest.param(
        DOCS_GOOD.replace("[meta]\nwalked = 5\n", ""),
        WALKED,
        None,
        {},
        "[meta] needs exactly walked",
        id="meta-missing",
    ),
    pytest.param(
        DOCS_GOOD.replace("walked = 5", "walked = true"),
        WALKED,
        None,
        {},
        "[meta] needs exactly walked",
        id="meta-walked-not-a-number",
    ),
    # Exactly once, and only what the walk finds.
    pytest.param(
        DOCS_GOOD
        + '\n[documents."docs/a.md"]\nscope = "in"\nclass = "reference"\nreason = "x"\n',
        WALKED,
        None,
        {},
        'docs/a.md: a nav page, classified as [pages."a.md"], not under [documents]',
        id="nav-page-under-documents",
    ),
    pytest.param(
        DOCS_GOOD
        + '\n[documents."gone.md"]\nscope = "out"\nout = "contributor"\nreason = "x"\n',
        WALKED,
        None,
        {},
        "gone.md: in [documents] but the walk does not find it",
        id="document-deleted",
    ),
    pytest.param(
        DOCS_GOOD + '\n[documents."README.md"]\nscope = "out"\n',
        WALKED,
        None,
        {},
        "does not parse",
        id="document-twice",
    ),
    pytest.param(
        DOCS_GOOD.replace(
            '[documents."README.md"]\nscope = "in"\n', '[documents."README.md"]\n'
        ),
        WALKED,
        None,
        {},
        'README.md: scope must be "in" or "out"',
        id="scope-missing",
    ),
    pytest.param(
        DOCS_GOOD.replace('scope = "in"\nclass = "exec"', 'scope = "in"'),
        WALKED,
        None,
        {},
        "README.md: no class",
        id="in-without-class",
    ),
    pytest.param(
        DOCS_GOOD.replace('class = "device"', 'class = "mobile"'),
        WALKED,
        None,
        {},
        "sdk/x/README.md: unknown class 'mobile'",
        id="in-unknown-class",
    ),
    pytest.param(
        DOCS_GOOD.replace('reason = "needs a simulator"', 'reason = ""'),
        WALKED,
        None,
        {},
        "sdk/x/README.md: reason must be one non-empty line",
        id="in-without-reason",
    ),
    pytest.param(
        DOCS_GOOD.replace('out = "contributor"', 'out = "misc"'),
        WALKED,
        None,
        {},
        "CONTRIBUTING.md: out = one of contributor, legal, generated, template,"
        " in-code-note, removed-code, test-harness, not 'misc'",
        id="out-unknown-category",
    ),
    pytest.param(
        DOCS_GOOD.replace(
            'out = "contributor"', 'out = "contributor"\nclass = "reference"'
        ),
        WALKED,
        None,
        {},
        "CONTRIBUTING.md: fields not allowed on scope 'out': ['class']",
        id="out-with-a-class",
    ),
    pytest.param(
        DOCS_GOOD,
        WALKED,
        None,
        {"enrolled": {}},
        "README.md: class exec, but scripts/doc_examples.toml enrols no exec block",
        id="exec-document-not-enrolled",
    ),
    pytest.param(
        DOCS_GOOD.replace('class = "exec"', 'class = "reference"'),
        WALKED,
        None,
        {},
        "README.md: class reference, but scripts/doc_examples.toml enrols README.md"
        " with exec = 2: Doc Examples runs its steps, so it is class exec",
        id="reference-document-with-exec-blocks",
    ),
    pytest.param(
        DOCS_GOOD.replace(
            'class = "device"', 'class = "workflow"\nworkflow = "chart-kind"'
        ),
        WALKED,
        None,
        {},
        "sdk/x/README.md: workflow 'chart-kind' does not run this page:"
        " 'sdk/x/README.md' is not a literal",
        id="workflow-document-glob-only",
    ),
    pytest.param(
        DOCS_GOOD.replace('journey = "first-run"', 'journey = "a-journey"'),
        WALKED,
        None,
        {},
        "journey id 'a-journey' is also j.md's",
        id="journey-id-shared-with-a-page",
    ),
    pytest.param(
        DOCS_GOOD,
        WALKED,
        None,
        {"written": {"first-run"}},
        "journey 'first-run' has its expectations file; remove pending = true",
        id="pending-document-journey-written",
    ),
    pytest.param(
        "documents = 3\n" + DOCS_GOOD.replace('[documents."', '[other."'),
        WALKED,
        None,
        {},
        "[documents] is not a table",
        id="documents-not-a-table",
    ),
    # The docs hub: every desc literal, in either quoting, is read and counted
    # exactly; a key whose value is not one is named.
    pytest.param(
        DOCS_GOOD,
        WALKED,
        _hub("{ label: 'Offline', desc: \"Works offline\" },"),
        {},
        "frontend/src/pages/docs/index.tsx#desc: the docs-hub-desc extractor read 4"
        " literals, the inventory names exactly 3",
        id="hub-new-double-quoted-desc",
        marks=pytest.mark.regression,
    ),
    pytest.param(
        DOCS_GOOD,
        WALKED,
        _hub("{ label: 'Offline', desc: 'Works offline' },"),
        {},
        "frontend/src/pages/docs/index.tsx#desc: the docs-hub-desc extractor read 4"
        " literals, the inventory names exactly 3",
        id="hub-new-single-quoted-desc",
    ),
    pytest.param(
        DOCS_GOOD.replace("literals = 3", "literals = 4"),
        WALKED,
        None,
        {},
        "index.tsx#desc: the docs-hub-desc extractor read 3 literals, the inventory"
        " names exactly 4",
        id="hub-literal-removed",
    ),
    pytest.param(
        DOCS_GOOD,
        WALKED,
        _hub("{ label: 'Offline', desc: `Works offline` },"),
        {},
        "index.tsx#desc: the docs-hub-desc extractor read 3 literals, the file has"
        " 4 desc:/description: keys; line 7 holds a value it cannot read",
        id="hub-template-literal",
    ),
    pytest.param(
        DOCS_GOOD,
        WALKED,
        _hub("{ label: 'Offline', desc: 'Works ' + 'offline' },"),
        {},
        "the file has 4 desc:/description: keys; line 7 holds a value it cannot read",
        id="hub-concatenation",
    ),
    pytest.param(
        DOCS_GOOD,
        WALKED,
        _hub("{ label: 'Long', desc: '" + "x" * 501 + "' },"),
        {},
        "the file has 4 desc:/description: keys; line 7 holds a value it cannot read",
        id="hub-literal-over-500",
    ),
    pytest.param(
        DOCS_GOOD.replace("literals = 3", 'literals = "3"'),
        WALKED,
        None,
        {},
        "index.tsx#desc: literals must be the number of desc:/description: literals"
        " in frontend/src/pages/docs/index.tsx",
        id="hub-literals-not-a-number",
    ),
]


@pytest.mark.parametrize("text, walked, files, context, expected", DOC_PLANTS)
def test_planted_document_defect_is_refused(text, walked, files, context, expected):
    found = _doc_check(text, walked, files, **context)
    assert any(expected in line for line in found), found


@pytest.mark.regression
def test_a_new_page_outside_the_nav_is_refused_against_the_real_tree():
    """G1a on the real inventory and walk: before #1142 a page outside the nav
    passed every docs test (the walk's universe list was the only line to add)."""
    inventory, _ = parse_inventory(INVENTORY.read_text(encoding="utf-8"))
    walked = set(_walked()) | {"docs/guides/offline-mode.md"}
    found = document_problems(inventory, walked, **_document_context())
    assert found == [
        f"walked {len(walked)} documents, the inventory names exactly"
        f" {len(walked) - 1}",
        f"docs/guides/offline-mode.md: {UNCLASSIFIED}",
    ]


def test_the_hub_extractor_reads_both_quotings_and_names_what_it_cannot():
    text = (
        "description: 'one',\n"
        '  desc: "it\'s two",\n'
        "  desc: 'three \\' quoted',\n"
        "  description:\n    'four, on the next line',\n"
        "  desc: `five`,\n"
        "  'desc': 'six',\n"
        "  desc: 'see desc: inside',\n"
        "  link.desc\n"
    )
    literals, unread, keys = docs_hub_desc(text)
    assert literals == [
        "one",
        "it's two",
        "three \\' quoted",
        "four, on the next line",
        "six",
        "see desc: inside",
    ]
    assert keys == 8
    assert unread == [6, 8]


def test_the_hub_extractor_is_bounded_on_a_long_unterminated_literal():
    """An unclosed quote 200,000 characters long: the bounded quantifiers give
    up after 500 and the key is reported, rather than scanning to the end."""
    literals, unread, keys = docs_hub_desc("desc: '" + "a" * 200_000 + "\n")
    assert (literals, unread, keys) == ([], [1], 1)


def test_the_leading_hash_comments_extractor():
    assert leading_hash_comments(
        "#!/bin/sh\n# usage\n#\n# more\nset -eu\n# later\n"
    ) == ["usage\n\nmore"]
    assert leading_hash_comments("# one\n\n# two\n") == ["one"]
    assert leading_hash_comments("name: x\n# later\n") == []
    assert leading_hash_comments("#!/bin/sh\nset -eu\n") == []


# ---------------------------------------------------------------------------
# Reading mkdocs.yml
# ---------------------------------------------------------------------------
def test_the_loader_accepts_mkdocs_tags():
    config = load_mkdocs(
        "site_name: !ENV [SITE_NAME, x]\n"
        "markdown_extensions:\n"
        "  - pymdownx.emoji:\n"
        "      emoji_index: !!python/name:material.extensions.emoji.twemoji\n"
        "nav:\n"
        "  - Home: index.md\n"
    )
    assert config["site_name"] == ["SITE_NAME", "x"]
    assert nav_pages(config) == ({"index.md"}, [])


def test_the_nav_walk_reads_every_shape_and_refuses_the_unknown():
    config = load_mkdocs(
        "nav:\n"
        "  - index.md\n"
        "  - Section:\n"
        "    - Page: a/b.md\n"
        "    - Deeper:\n"
        "      - c.md\n"
        "    - Twice: a/b.md\n"
        "  - Link: https://example.com/\n"
        "  - Odd: notes.txt\n"
        "  - Number: 3\n"
    )
    pages, found = nav_pages(config)
    assert pages == {"index.md", "a/b.md", "c.md"}
    assert found == [
        "nav entry 'Odd' is not a .md page: 'notes.txt'",
        "nav entry 'Number' is not a page or a section: 3",
    ]
    assert nav_pages({}) == (set(), ["mkdocs.yml has no nav"])
