"""Read one journey file, and refuse what the runner will not run.

``load`` returns a ``Journey`` or raises ``Refused`` with the problems found,
each one line naming the step. It reports them from every stage it reaches:
the checks on the raw YAML, the model's (without repeating what the raw checks
said), and the checks against the guide, the inventory and the registry, run on
the journey or, when the model refuses it, on each part that parsed (the guide,
the stack, the date, each step that is valid on its own). A file that is not
YAML, or not a mapping, stops at that. Refused, besides what ``model`` refuses:

* a CSS or XPath selector: under a key of its own (``selector``, ``css``,
  ``xpath``, a string ``locator``), or a name, label or option in a selector's
  form (``css=...``, ``xpath=...``, ``//...``, or one with ``>>``). Elements are
  found by role and accessible name, as a reader finds them; a name such as
  ``settings.json``, ``.env`` or ``.NET`` is a name;
* a sleep (``sleep``, ``wait``, ``delay``, ``pause``, ...): a step waits for
  what it expects, never for a time;
* a step without ``fail``: what failure looks like is written before the run;
* an unknown stack;
* a ``guide`` the inventory does not classify ``journey`` with this file's
  name as its id, or classifies ``pending = true``: a nav page under
  ``[pages]`` (its path from ``docs/``), or a document under ``[documents]``
  (its path from the repository's root, ``scope = "in"``), such as
  ``demo/DEMO_GUIDE.md``, or a dashboard component such as the first-run
  card (``.tsx``), whose anchors are its ``data-testid`` values;
* a ``doc`` anchor the guide does not have;
* an accessible name or label the guide's text does not contain, unless it says
  ``quote: false`` with a reason, so a label renamed in the UI or in the guide
  alone fails;
* a step on a screen (``goto``, ``click``, ``fill``, ``select``) without
  ``aria``, unless it says ``snapshot: false`` with a ``snapshot_reason`` and
  has a structural expectation (``url``, ``status``, ``visible``, ``text``,
  ``number``) and no ``aria``; an ``api`` step with neither ``status`` nor
  ``json``; a ``search`` step without ``found``; a ``crawl`` step with an
  ``expect``; and an expectation the step's kind cannot have;
* a ``search`` or ``crawl`` step on a stack that serves no documentation site
  (only docs-published and docs-local do), or with a ``snapshot`` setting;
* a journey none of whose steps this runner runs (every step is
  ``ref: doc-examples`` or declares ``not_run``);
* ``ref: doc-examples`` on a section with no shell block Doc Examples runs, or
  on a guide ``scripts/doc_examples.toml`` does not enrol, or on a document
  (only a page's enrolment under ``docs/`` is read here);
* an ``api`` step on a stack with no API (every stack but compose-dev,
  compose-empty, compose-sso and demo-local);
* an oracle not in ``docs_runner.oracles.ORACLES``;
* a ``not_run`` reason the registry does not let a journey declare;
* a ``written`` date after today;
* ``open`` on any stack but compose-dev, compose-empty, compose-sso and
  demo-local, or of a URL whose origin
  (``http://localhost:<port>``) the guide's text does not contain;
* a ``fill`` that types a ``secret`` no ``passwords`` entry makes up and no
  earlier ``keep`` step keeps, or that does not say ``snapshot: false``;
* a ``keep`` step with an ``expect`` or a ``snapshot`` setting (its check is
  the action, and it keeps no screen);
* a journey that makes up or keeps a secret, or has an api step that reveals
  one (``model.reveals_credential``), and is recorded (``video: true``): a
  recording would show what the run keeps out of its files;
* a screen step right before a ``keep`` step (its screen shows what the keep
  reads) without ``snapshot: false``;
* ``recordings`` beside ``video: true``; and, for each recording, a name that
  is not a table of ``recordings.toml``, is still ``pending`` there, or belongs
  to another guide, and a segment that holds a ``keep`` step or the step right
  before one, starts right after one, or has a ``fill`` type a secret into a
  field whose label does not say "password" (two segments that overlap, or a
  name given twice, the model refuses; the runner's checks of the screen are
  in ``execute.py``);
* a ``fill`` that types a written value into a field whose label says
  "password", where its screen is kept, unless the value is the documented
  demo password;
* ``save`` on a step that is not an ``api`` step, or a name saved twice;
* a ``{{name}}`` (in an api step's path, body or expected JSON, or in a
  ``goto``) that no earlier step saves, or that names a secret (a secret
  is sent only as a ``key``); a ``key`` that names no secret saved or kept earlier; a
  traffic step's ``experiment`` that names no value saved earlier;
* a ``traffic`` step on any stack but compose-dev, or with an ``expect`` or a
  ``snapshot`` setting (its check is the action);
* ``computed`` on a step that is not an ``api`` step, and ``cells`` on one that
  is not on a screen, or either naming an oracle not in ``ORACLES``;
* ``after`` on a step that is not an ``api`` step, or naming a value no earlier
  step saves, or a secret;
* ``poll`` on a step that is not an ``api`` step, or on one whose method is not
  ``GET`` (sending a change again would repeat it). A poll is not a sleep: it
  waits for what the step expects, up to a bound (``waiting.py``), and the
  sleep keys above are refused wherever they appear, ``wait`` included;
* an ``sdk`` step on any stack but compose-dev, or with an ``expect`` or a
  ``snapshot`` setting (its check is the action); whose ``install`` section's
  first block is not one ``npm install <package>@<version>`` or
  ``pip install <package>==<version>`` (for Go: ``go mod init <path>`` at most
  once and ``go get <module>`` once, and nothing else), or whose ``snippet``
  section's first block is not in its language (``sdk.py``); with a ``replace``
  key that is not a quoted string of that block, a stub the block does not
  call, or an answer whose variable the block does not name (for Go, a key the
  block does not print as ``<key>=``); a Go step on a page whose "Requires Go
  X+" is not sdk/go/go.mod's ``go`` version; and a journey whose sdk steps give
  every user the same answer for a flag (the oracle could not tell an SDK that
  always answers on, or always off, from a right one);
* a ``run`` or ``finish`` step on any stack but demo-local, or with a
  ``snapshot`` setting; a ``run`` whose ``command`` is not one shell block of
  its section (``doc``) word for word (``commands.block_problem``); a ``run``
  without ``background`` and a ``finish`` without ``expect.exit``, or
  expecting anything but ``exit`` and ``output``; a background ``run`` with an
  ``expect`` (its ``finish`` checks it); a ``finish`` that names no earlier
  background ``run``, or one already finished; and a background ``run`` no
  later step finishes.
"""

from __future__ import annotations

import datetime
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import yaml
from pydantic import ValidationError

from docs_runner import commands, registry, sdk, traffic, values
from docs_runner import guide as guides
from docs_runner.model import (
    API_ANSWER_EXPECTS,
    API_EXPECTS,
    BROWSER_EXPECTS,
    COMPOSE_STACKS,
    GUIDE_PATTERN,
    LOCAL_URL,
    RUN_EXPECTS,
    RUN_STACKS,
    SCREEN_ACTIONS,
    SEARCH_EXPECTS,
    SITE_STACKS,
    STACKS,
    STRUCTURAL_EXPECTS,
    Journey,
    Recording,
    Step,
    reveals_credential,
    segment_of,
)
from docs_runner.stacks import DEMO_ACCOUNTS

#: The documented demo password (the Quick Start prints it): the one password a
#: step may type where its screen is kept.
DEMO_PASSWORDS = frozenset(password for _, password in DEMO_ACCOUNTS.values())

#: Keys that would make a step wait for a time instead of for what it expects.
SLEEP_KEYS = frozenset(
    {
        "sleep",
        "wait",
        "wait_for",
        "wait_for_timeout",
        "wait_ms",
        "wait_seconds",
        "delay",
        "pause",
    }
)
#: Keys that would carry a selector rather than a role and a name.
SELECTOR_KEYS = frozenset({"selector", "css", "xpath"})
#: The keys whose string values name an element.
NAME_KEYS = frozenset({"role", "name", "label", "option", "locator"})

#: A name, label or option written as a selector: ``css=``/``xpath=`` engines,
#: an XPath (``//div``, ``.//a``, ``(//a)[1]``), or Playwright's ``>>`` chaining.
_SELECTOR_SHAPES = (
    re.compile(r"^(css|xpath)\s*="),
    re.compile(r"^\(?\s*\.{0,2}//"),
    re.compile(r">>"),
)


def looks_like_selector(value: str) -> bool:
    text = value.strip()
    return any(shape.search(text) for shape in _SELECTOR_SHAPES)


class Refused(Exception):
    """The journey file will not be run; ``problems`` says why, one line each."""

    def __init__(self, path: str, problems: List[str]):
        self.path = path
        self.problems = problems
        super().__init__(f"{path}: refused\n" + "\n".join(f"  - {p}" for p in problems))


@dataclass(frozen=True)
class Context:
    """What a journey is checked against, besides its own file."""

    docs_root: Path
    inventory: Mapping[str, Any]
    #: ``scripts/doc_examples.toml``'s exec-block count per page under docs/.
    doc_examples: Mapping[str, int]
    oracles: Mapping[str, Callable[..., float]]
    today: datetime.date
    #: ``recordings.toml``: the launch walkthroughs, by name.
    recordings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


def read_inventory(path: Path) -> Dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def read_recordings(path: Path) -> Dict[str, Dict[str, Any]]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def read_doc_examples(path: Path) -> Dict[str, int]:
    """Each page under docs/ that ``doc_examples.toml`` enrols, with its exec count."""
    counts: Dict[str, int] = {}
    for document in tomllib.loads(path.read_text(encoding="utf-8")).get("document", []):
        page = document["path"]
        if page.startswith("docs/"):
            counts[page[len("docs/") :]] = int(document.get("exec", 0))
    return counts


def context_for(repo_root: Path, today: Optional[datetime.date] = None) -> Context:
    """The real tree's context: docs/, the inventory, Doc Examples, the oracles."""
    from docs_runner.oracles import ORACLES

    here = repo_root / "tests" / "acceptance" / "docs"
    return Context(
        docs_root=repo_root / "docs",
        inventory=read_inventory(here / "inventory.toml"),
        doc_examples=read_doc_examples(repo_root / "scripts" / "doc_examples.toml"),
        oracles=ORACLES,
        today=today or datetime.datetime.now(datetime.timezone.utc).date(),
        recordings=read_recordings(here / "recordings.toml"),
    )


# ---------------------------------------------------------------------------
# Refusals on the raw file, before the model: each has a message of its own
# ---------------------------------------------------------------------------
Loc = Tuple[Any, ...]


def _walk(node: Any, where: str, loc: Loc) -> Iterable[tuple]:
    """Every (where, loc of the key, key, value) in a nested mapping/list."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield where, loc + (key,), key, value
            yield from _walk(value, f"{where}.{key}", loc + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{where}[{index}]", loc + (index,))


def _step_label(index: int, step: Any) -> str:
    if isinstance(step, dict) and isinstance(step.get("id"), str):
        return f"step {index + 1} ({step['id']})"
    return f"step {index + 1}"


def raw_problems(data: Dict[str, Any]) -> Tuple[List[str], Set[Loc]]:
    """Refusals that need only the parsed YAML, and the places they cover.

    A covered place is where the model would refuse the same thing again, in
    its own words (an unknown stack, a missing ``fail``, a key it does not
    know); ``load`` leaves the model's message for those out.
    """
    found: List[str] = []
    covered: Set[Loc] = set()
    stack = data.get("stack")
    if stack is not None and stack not in STACKS:
        found.append(f"unknown stack {stack!r}; a stack is one of {', '.join(STACKS)}")
        covered.add(("stack",))
    steps = data.get("steps")
    if not isinstance(steps, list):
        return found, covered
    for index, step in enumerate(steps):
        label = _step_label(index, step)
        if not isinstance(step, dict):
            continue
        if "fail" not in step:
            found.append(
                f"{label}: no fail; write what failure looks like before the run"
            )
            covered.add(("steps", index, "fail"))
        for where, loc, key, value in _walk(step, label, ("steps", index)):
            key_text = str(key).lower()
            if key_text in SLEEP_KEYS:
                found.append(
                    f"{where}: {key} is a sleep; a step waits for what it expects,"
                    " never for a time"
                )
                covered.add(loc)
            if key_text in SELECTOR_KEYS or (
                key_text == "locator" and isinstance(value, str)
            ):
                found.append(
                    f"{where}: {key} is a CSS or XPath selector; find the element"
                    " by role and name"
                )
                covered.add(loc)
            elif key_text in NAME_KEYS and isinstance(value, str):
                if looks_like_selector(value):
                    found.append(
                        f"{where}.{key}: {value!r} is a CSS or XPath selector;"
                        " give the role and accessible name a reader sees"
                    )
    return found, covered


def _model_problems(
    error: ValidationError, data: Dict[str, Any], covered: Set[Loc]
) -> List[str]:
    """Pydantic's errors, placed as ``step N (id).field``, short, not repeated."""
    steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    found = []
    for item in error.errors():
        raw_loc = tuple(item["loc"])
        if any(raw_loc[: len(place)] == place for place in covered):
            continue
        loc = list(raw_loc)
        if len(loc) >= 2 and loc[0] == "steps" and isinstance(loc[1], int):
            label = _step_label(loc[1], steps[loc[1]] if loc[1] < len(steps) else None)
            loc = [label, *loc[2:]]
        where = ".".join(str(part) for part in loc) or "journey"
        if item["type"] == "literal_error":
            message = f"{item['input']!r} is not one of the values allowed here"
        else:
            message = item["msg"].removeprefix("Value error, ")
        found.append(f"{where}: {message}")
    return found


# ---------------------------------------------------------------------------
# Refusals that need the guide, the inventory and the registry
# ---------------------------------------------------------------------------
def _quoted_names(step: Step) -> List[tuple]:
    """(what, name, quote) for every name or label the step asks a reader to find."""
    names: List[tuple] = []
    do = step.do
    if do.click is not None:
        names.append(("click.name", do.click.name, do.click.quote))
    if do.fill is not None:
        names.append(("fill.label", do.fill.label, do.fill.quote))
    if do.select is not None:
        names.append(("select.label", do.select.label, do.select.quote))
    if do.keep is not None:
        within = do.keep.within
        if within is not None:
            names.append(("keep.within.name", within.name, within.quote))
        else:
            names.append(("keep.prefix", do.keep.prefix, True))
    expect = step.expect
    if expect is not None:
        for index, named in enumerate(expect.visible or []):
            names.append((f"expect.visible[{index}].name", named.name, named.quote))
        if expect.number is not None:
            locator = expect.number.locator
            names.append(("expect.number.locator.name", locator.name, locator.quote))
        for index, cell in enumerate(expect.cells or []):
            names.append((f"expect.cells[{index}].row", cell.row, cell.quote))
            names.append((f"expect.cells[{index}].column", cell.column, cell.quote))
    return names


def _expect_problems(label: str, step: Step, stack: Optional[str]) -> List[str]:
    found: List[str] = []
    kind = step.do.kind
    given = step.expect.given() if step.expect is not None else []
    if kind == "ref":
        if given:
            found.append(
                f"{label}: ref: doc-examples is checked by Doc Examples; it has no"
                " expect here"
            )
        if step.snapshot:
            found.append(f"{label}: ref: doc-examples has no screen to snapshot")
        return found
    if kind == "sdk":
        if stack is not None and stack != "compose-dev":
            found.append(
                f"{label}: an sdk step needs the compose stack's API; {stack} has none"
            )
        if given:
            found.append(
                f"{label}: sdk checks what the action says (the install, the block's"
                " answers against the oracle); it has no expect"
            )
        if step.snapshot is not None:
            found.append(f"{label}: an sdk step has no screen to snapshot")
        return found
    if kind in ("traffic", "evaluations"):
        article = "an" if kind[0] in "aeiou" else "a"
        if stack is not None and stack != "compose-dev":
            found.append(
                f"{label}: {article} {kind} step needs the compose stack's API;"
                f" {stack} has none"
            )
        if given:
            found.append(
                f"{label}: {kind} checks what the action says (each answer as the"
                " step expects); it has no expect"
            )
        if step.snapshot is not None:
            found.append(f"{label}: {article} {kind} step has no screen to snapshot")
        return found
    if kind in ("run", "finish"):
        if stack is not None and stack not in RUN_STACKS:
            found.append(
                f"{label}: a {kind} step runs a block of the guide in the checkout"
                f" demo/setup-local.sh set up ({', '.join(RUN_STACKS)}); {stack}"
                " has none"
            )
        if step.snapshot is not None:
            found.append(f"{label}: a {kind} step has no screen to snapshot")
        if kind == "run" and step.do.run.background:
            if given:
                found.append(
                    f"{label}: a background run is checked by the finish step that"
                    " names it; it has no expect"
                )
            return found
        wrong = [name for name in given if name not in RUN_EXPECTS]
        if wrong:
            found.append(
                f"{label}: a {kind} step expects only exit or output, not {wrong}"
            )
        if "exit" not in given:
            found.append(
                f"{label}: a {kind} step needs expect.exit, the status its block"
                " must end with"
            )
        return found
    if kind == "api":
        if stack is not None and stack not in COMPOSE_STACKS:
            found.append(
                f"{label}: an api step needs a stack with an API; {stack} has none"
            )
        wrong = [name for name in given if name not in API_EXPECTS]
        if wrong:
            found.append(
                f"{label}: an api step expects only status or json, not {wrong}"
            )
        if not [name for name in given if name in API_ANSWER_EXPECTS]:
            found.append(f"{label}: an api step needs status or json in expect")
        if step.snapshot:
            found.append(f"{label}: an api step has no screen to snapshot")
        return found
    if kind in ("crawl", "search"):
        if stack is not None and stack not in SITE_STACKS:
            found.append(
                f"{label}: {kind} is for the documentation site's stacks"
                f" ({', '.join(SITE_STACKS)}); {stack} serves no site"
            )
        if step.snapshot is not None:
            found.append(
                f"{label}: a {kind} step takes no ARIA snapshot; leave snapshot out"
            )
    if kind == "crawl":
        if given:
            found.append(
                f"{label}: crawl: {step.do.crawl} checks what the action says; it has"
                " no expect"
            )
        return found
    if kind == "keep":
        if given:
            found.append(
                f"{label}: keep checks what the action says (one element, its text"
                " kept); it has no expect"
            )
        if step.snapshot is not None:
            found.append(f"{label}: a keep step keeps no screen; leave snapshot out")
        return found
    if kind == "search":
        wrong = [name for name in given if name not in SEARCH_EXPECTS]
        if wrong:
            found.append(f"{label}: a search step expects only found, not {wrong}")
        if "found" not in given:
            found.append(
                f"{label}: a search step needs expect.found, the page it must find"
            )
        return found
    if "found" in given:
        found.append(f"{label}: found is the page a search finds; {kind} has none")
    wrong = [name for name in given if name not in BROWSER_EXPECTS + ("found",)]
    if wrong:
        found.append(f"{label}: a step in a browser cannot expect {wrong}")
    if step.snapshot is not False:
        if "aria" not in given:
            found.append(
                f"{label}: a step on one screen needs expect.aria, its ARIA snapshot"
                " written before the run, beside any structural expect; or"
                " snapshot: false with a snapshot_reason"
            )
    else:
        if "aria" in given:
            found.append(
                f"{label}: snapshot: false drops the ARIA snapshot, but expect.aria"
                " gives one; remove one of the two"
            )
        if not [name for name in given if name in STRUCTURAL_EXPECTS]:
            found.append(
                f"{label}: a step with snapshot: false needs a structural expect"
                f" ({', '.join(STRUCTURAL_EXPECTS)})"
            )
    if "status" in given and kind not in ("goto", "open"):
        found.append(
            f"{label}: status is the answer to a goto or an open; {kind} has none"
        )
    if step.do.fill is not None and step.do.fill.secret and step.snapshot is not False:
        found.append(
            f"{label}: a fill that types the secret {step.do.fill.secret!r} keeps no"
            " screen: give snapshot: false and a snapshot_reason"
        )
    return found


def guide_root(context: Context, guide: str) -> Path:
    """Where *guide*'s path starts: ``docs/`` for a nav page (``[pages]``), the
    repository's root for a document (``[documents]``). A key both tables hold
    (``README.md``: the docs home page, and the repository's own) is the nav
    page's."""
    if guide not in context.inventory.get("pages", {}) and guide in (
        context.inventory.get("documents", {})
    ):
        return context.docs_root.parent
    return context.docs_root


def _guide_problems(guide: str, stem: str, context: Context) -> List[str]:
    found: List[str] = []
    entry = context.inventory.get("pages", {}).get(guide)
    if entry is None:
        entry = context.inventory.get("documents", {}).get(guide)
        if entry is not None and entry.get("scope") != "in":
            found.append(
                f"guide {guide!r} is a document inventory.toml puts out of scope;"
                " a journey walks a page or an in-scope document"
            )
            return found
    if entry is None:
        found.append(f"guide {guide!r} is not a page in inventory.toml, nor a document")
    elif entry.get("class") != "journey":
        found.append(
            f"guide {guide!r} is classified {entry.get('class')!r} in"
            " inventory.toml, not journey"
        )
    elif entry.get("journey") != stem:
        found.append(
            f"inventory.toml names {entry.get('journey')!r} as the journey of"
            f" {guide!r}, not {stem!r}"
        )
    elif entry.get("pending") is True:
        found.append(
            f"journey {stem!r} is pending = true in inventory.toml; a pending"
            " journey is not run"
        )
    return found


def _step_problems(
    label: str,
    step: Step,
    guide_path: Optional[str],
    guide: Optional[guides.Guide],
    stack: Optional[str],
    context: Context,
) -> List[str]:
    found: List[str] = []
    if guide is not None:
        anchors = guide.anchors
        if step.doc not in anchors:
            found.append(f"{label}: the guide has no anchor #{step.doc}")
        for what, name, quote in _quoted_names(step):
            if quote and not guide.contains(name):
                found.append(
                    f"{label}: {what} {name!r} is not in the guide's text; quote what"
                    " the guide says, or give quote: false and a reason"
                )
    found.extend(_expect_problems(label, step, stack))
    if step.do.open is not None:
        if stack is not None and stack not in COMPOSE_STACKS:
            found.append(
                f"{label}: open is for an address the guide gives for the compose"
                f" stack; {stack} is not it"
            )
        match = LOCAL_URL.match(step.do.open)
        if guide is not None and match and not guide.contains(match["origin"]):
            found.append(
                f"{label}: open {match['origin']!r} is not in the guide's text; open"
                " an address the guide gives"
            )
    if step.not_run is not None and not registry.is_declarable(step.not_run):
        found.append(
            f"{label}: not_run {step.not_run!r} is not a reason a journey may"
            f" give ({', '.join(registry.DECLARED)}, waived #<issue>)"
        )
    if step.poll is not None:
        if step.do.kind != "api":
            found.append(
                f"{label}: poll sends an api step's request again until its"
                f" expect holds; a {step.do.kind} step cannot poll"
            )
        elif step.do.api.method != "GET":
            found.append(
                f"{label}: poll sends a GET again; sending a"
                f" {step.do.api.method} again would repeat its change"
            )
    if step.do.kind == "ref" and guide_path is not None:
        if guide_root(context, guide_path) != context.docs_root:
            found.append(
                f"{label}: ref: doc-examples is for a page under docs/ (the"
                f" enrolment this runner reads); {guide_path} is a document"
            )
        elif context.doc_examples.get(guide_path, 0) < 1:
            found.append(
                f"{label}: ref: doc-examples, but scripts/doc_examples.toml"
                f" enrols no exec block of {guide_path}"
            )
        elif (
            guide is not None
            and step.doc in guide.anchors
            and not guide.has_exec_block(step.doc)
        ):
            found.append(
                f"{label}: ref: doc-examples, but the section #{step.doc} has no"
                " {.bash exec} block"
            )
    if step.expect is not None:
        named = []
        if step.expect.number is not None:
            named.append(step.expect.number.oracle.name)
        named.extend(c.oracle.name for c in (step.expect.computed or {}).values())
        named.extend(c.oracle.name for c in step.expect.cells or [])
        for oracle in named:
            if oracle not in context.oracles:
                found.append(f"{label}: no oracle named {oracle!r}")
    if step.save is not None and step.do.kind != "api":
        found.append(f"{label}: save keeps values from an api step's answer only")
    if step.do.run is not None and guide is not None and step.doc in guide.anchors:
        problem = commands.block_problem(guide.section(step.doc), step.do.run.command)
        if problem:
            found.append(
                f"{label}: run takes a shell block of #{step.doc} as the page writes"
                f" it: {problem}"
            )
    if step.do.sdk is not None and guide is not None:
        found.extend(_sdk_problems(label, step.do.sdk, guide, context.docs_root.parent))
    return found


def _sdk_problems(
    label: str, plan, guide: guides.Guide, repo_root: Optional[Path] = None
) -> List[str]:
    """An sdk step against its page: the install command, the block, its names."""
    found: List[str] = []
    for what, anchor in (("install", plan.install), ("snippet", plan.snippet)):
        if anchor not in guide.anchors:
            found.append(f"{label}: sdk.{what}: the guide has no anchor #{anchor}")
    if found:
        return found
    try:
        sdk.install_command(guide.section(plan.install), plan.language)
    except sdk.SdkError as error:
        found.append(f"{label}: sdk.install #{plan.install}: {error}")
    try:
        block = sdk.snippet(guide.section(plan.snippet), plan.language)
    except sdk.SdkError as error:
        found.append(f"{label}: sdk.snippet #{plan.snippet}: {error}")
        return found
    for literal in plan.replace:
        if not sdk.occurrences(block, literal):
            found.append(
                f"{label}: sdk.replace: {literal!r} is not a quoted string of the"
                f" block under #{plan.snippet}; only the page's placeholders are"
                " replaced"
            )
    for stub in plan.stubs:
        if not re.search(rf"(?<![\w.]){re.escape(stub)}\(", block):
            found.append(f"{label}: sdk.stubs: the block never calls {stub}()")
    for _, expressions in plan.answers.items():
        for expression in expressions:
            root = expression.split(".", 1)[0]
            if plan.language == "go":
                if not re.search(rf"(?<![\w.]){re.escape(root)}=", block):
                    found.append(
                        f"{label}: sdk.answers: {expression}: the block prints no"
                        f" {root}="
                    )
                continue
            if root != sdk.CALLED and not re.search(rf"\b{re.escape(root)}\b", block):
                found.append(
                    f"{label}: sdk.answers: {expression}: the block names no {root}"
                )
    if plan.language == "go" and repo_root is not None:
        go_mod = repo_root / sdk.GO_MODULE / "go.mod"
        if not go_mod.is_file():
            found.append(f"{label}: sdk: {sdk.GO_MODULE.as_posix()}/go.mod is missing")
        else:
            problem = sdk.go_requirement_problem(
                guide.text, go_mod.read_text(encoding="utf-8")
            )
            if problem:
                found.append(f"{label}: sdk: {problem}")
    return found


def _sdk_journey_problems(steps: Sequence[Tuple[int, Step]]) -> List[str]:
    """A flag the sdk steps evaluate must be on for one of their users and off for another."""
    answers: Dict[str, Set[bool]] = {}
    for _, step in steps:
        plan = step.do.sdk
        if plan is None:
            continue
        enabled, _ = traffic.expected_answer(
            plan.user, plan.flag, "rollout", plan.rollout
        )
        answers.setdefault(plan.flag, set()).add(enabled)
    return [
        f"the sdk steps' users all get the same answer for {flag}"
        f" ({'on' if True in seen else 'off'}); name one the documented rollout"
        " hash gives the flag and one it does not, or a wrong answer can pass"
        for flag, seen in answers.items()
        if len(seen) < 2
    ]


def _uses(step: Step) -> List[Tuple[str, str]]:
    """(what, name) for every saved value or secret the step uses."""
    used: List[Tuple[str, str]] = []
    do = step.do
    if do.api is not None:
        for name in values.placeholders(do.api.path):
            used.append(("value", name))
        for name in values.placeholders_in(do.api.body):
            used.append(("value", name))
        if do.api.key is not None:
            used.append(("key", do.api.key))
        if step.expect is not None:
            for name in values.placeholders_in(step.expect.json_):
                used.append(("value", name))
            for name in (step.expect.after or {}).values():
                used.append(("value", name))
    if do.goto is not None:
        for name in values.placeholders(do.goto):
            used.append(("value", name))
    if do.traffic is not None:
        used.append(("experiment", do.traffic.experiment))
        used.append(("key", do.traffic.key))
    if do.evaluations is not None:
        used.append(("key", do.evaluations.key))
    if do.sdk is not None:
        used.append(("experiment", do.sdk.experiment))
        used.append(("key", do.sdk.key))
    return used


def _value_problems(
    steps: Sequence[Tuple[int, Step]], passwords: Sequence[str] = ()
) -> List[str]:
    """A value or key used before a step saves it, a secret in a path, a name saved twice.

    A secret is a value saved with ``secret: true``, kept by a ``keep`` step or
    made up as one of the journey's ``passwords``.
    """
    found: List[str] = []
    saved: Dict[str, bool] = dict.fromkeys(passwords, True)
    for index, step in steps:
        label = f"step {index + 1} ({step.id})"
        for what, name in _uses(step):
            if name not in saved:
                found.append(f"{label}: {name!r} is used before any step saves it")
            elif what == "key" and not saved[name]:
                found.append(
                    f"{label}: the key {name!r} is not a secret; save the API key"
                    " with secret: true"
                )
            elif what != "key" and saved[name]:
                found.append(
                    f"{label}: {name!r} is a secret; a secret is sent only as a key,"
                    " never put into a path, a body or an address"
                )
        for name, save in (step.save or {}).items():
            if name in saved:
                found.append(
                    f"{label}: {name!r} is saved twice; give each its own name"
                )
            saved[name] = save.secret
        if step.do.keep is not None:
            saved[step.do.keep.secret] = True
    return found


def _secret_problems(
    steps: Sequence[Tuple[int, Step]],
    passwords: Sequence[str],
    video: Optional[bool],
) -> List[str]:
    """A secret typed before anything makes it up or keeps it; a recorded journey."""
    found: List[str] = []
    known = set(passwords)
    for index, step in steps:
        label = f"step {index + 1} ({step.id})"
        typed = step.do.fill.secret if step.do.fill is not None else None
        if typed is not None and typed not in known:
            found.append(
                f"{label}: fill types the secret {typed!r}, which no passwords entry"
                " makes up and no earlier keep step keeps"
            )
        if step.do.keep is not None:
            known.add(step.do.keep.secret)
    by_index = dict(steps)
    for index, step in steps:
        label = f"step {index + 1} ({step.id})"
        following = by_index.get(index + 1)
        reveals = following is not None and following.do.keep is not None
        if reveals and step.do.kind in SCREEN_ACTIONS and step.snapshot is not False:
            found.append(
                f"{label}: the next step keeps a value this step's screen shows,"
                " so this step keeps no screen: give snapshot: false and a"
                " snapshot_reason"
            )
        fill = step.do.fill
        if (
            fill is not None
            and fill.value is not None
            and "password" in fill.label.lower()
            and step.snapshot is not False
            and fill.value not in DEMO_PASSWORDS
        ):
            found.append(
                f"{label}: fill types a written password into {fill.label!r} where"
                " its screen is kept; only the documented demo password may be:"
                " make one up (passwords) or give snapshot: false"
            )
    has_secrets = bool(passwords) or any(
        step.do.keep is not None or reveals_credential(step) for _, step in steps
    )
    if has_secrets and video:
        found.append(
            "video: true, but the journey makes up or keeps a secret, or an api"
            " step reveals one; a recording would show it, so give video: false"
        )
    return found


def _recording_problems(
    steps: Sequence[Tuple[int, Step]],
    recordings: Sequence[Recording],
    guide_path: Optional[str],
    video: Optional[bool],
    context: Context,
    complete: bool,
) -> List[str]:
    """Recordings beside a video; for each, a name the registry does not know or
    a segment that could draw a secret.

    The segments are checked only when every step parsed (``complete``).
    """
    found: List[str] = []
    if recordings and video:
        found.append(
            "video: true and recordings: a journey is recorded whole (video) or"
            " for its walkthroughs (recordings), not both"
        )
    for recording in recordings:
        found.extend(_registry_problems(recording, guide_path, context))
        if complete and steps:
            found.extend(_segment_problems(steps, recording))
    return found


def _registry_problems(
    recording: Recording, guide_path: Optional[str], context: Context
) -> List[str]:
    name = recording.name
    entry = context.recordings.get(name)
    if entry is None:
        return [f"recording {name!r} is not a table of recordings.toml"]
    if entry.get("pending"):
        return [
            f"recording {name!r} is pending in recordings.toml; remove its pending"
            " line in the change that records it"
        ]
    if guide_path is not None and entry.get("guide") != guide_path:
        return [
            f"recording {name!r} is recorded from {entry.get('guide')!r} in"
            f" recordings.toml, not from {guide_path!r}"
        ]
    return []


def _segment_problems(
    steps: Sequence[Tuple[int, Step]], recording: Recording
) -> List[str]:
    """A segment that holds or starts on a screen showing a kept value, or types
    a secret where it is drawn."""
    try:
        first, last = segment_of([step for _, step in steps], recording)
    except ValueError:
        # The model says which bound names no step.
        return []
    found: List[str] = []
    name = recording.name
    by_index = dict(steps)
    for index in range(first, last + 1):
        step = by_index[index]
        label = f"step {index + 1} ({step.id})"
        following = by_index.get(index + 1)
        if step.do.keep is not None:
            found.append(
                f"{label}: recording {name!r} holds a keep step, whose screen shows"
                " the value it keeps; end the recording before it"
            )
        elif following is not None and following.do.keep is not None:
            found.append(
                f"{label}: recording {name!r} holds the screen the next step keeps"
                " a value from; end the recording before it"
            )
        fill = step.do.fill
        if fill is not None and fill.secret and "password" not in fill.label.lower():
            found.append(
                f"{label}: recording {name!r} types the secret {fill.secret!r} into"
                f" {fill.label!r}, which is not a password field; only a password"
                " field draws what is typed as dots"
            )
    before = by_index.get(first - 1)
    if before is not None and before.do.keep is not None:
        found.append(
            f"step {first + 1} ({by_index[first].id}): recording {name!r} starts"
            " right after a keep step, on the screen showing the value it kept"
        )
    return found


def semantic_problems(
    *,
    guide_path: Optional[str],
    stack: Optional[str],
    written: Optional[datetime.date],
    steps: Sequence[Tuple[int, Step]],
    complete: bool,
    stem: str,
    context: Context,
    passwords: Sequence[str] = (),
    video: Optional[bool] = None,
    recordings: Sequence[Recording] = (),
) -> List[str]:
    """Refusals against the guide, the inventory and the registry.

    Any argument may be missing (None, or fewer steps) when the model refused
    the journey; the checks that need it are left out. ``complete`` says every
    step is here, which the journey-wide checks need.
    """
    found: List[str] = []
    guide: Optional[guides.Guide] = None
    if guide_path is not None:
        found.extend(_guide_problems(guide_path, stem, context))
        root = guide_root(context, guide_path)
        if (root / guide_path).is_file():
            guide = guides.load(root, guide_path)
        elif root == context.docs_root:
            found.append(f"guide {guide_path!r} is not a file under docs/")
        else:
            found.append(f"guide {guide_path!r} is not a file in the repository")
    if written is not None and written > context.today:
        found.append(f"written {written} is after today ({context.today})")
    for index, step in steps:
        label = f"step {index + 1} ({step.id})"
        found.extend(_step_problems(label, step, guide_path, guide, stack, context))
    found.extend(_secret_problems(steps, passwords, video))
    found.extend(
        _recording_problems(steps, recordings, guide_path, video, context, complete)
    )
    if complete:
        found.extend(_value_problems(steps, passwords))
        found.extend(_sdk_journey_problems(steps))
        found.extend(_background_problems(steps))
    if complete and steps:
        if all(step.do.kind == "ref" or step.not_run is not None for _, step in steps):
            found.append(
                "no step is run by this runner: every step is ref: doc-examples or"
                " declares not_run"
            )
    return found


def _background_problems(steps: Sequence[Tuple[int, Step]]) -> List[str]:
    """Each ``finish`` names an earlier background run not finished before it,
    and each background run is finished by a later step."""
    found: List[str] = []
    started: Dict[str, str] = {}
    finished: Set[str] = set()
    for index, step in steps:
        label = f"step {index + 1} ({step.id})"
        if step.do.run is not None and step.do.run.background:
            started[step.id] = label
        name = step.do.finish
        if name is None:
            continue
        if name not in started:
            found.append(
                f"{label}: finish {name!r} names no earlier run step with"
                " background: true"
            )
        elif name in finished:
            found.append(f"{label}: finish {name!r} is finished twice")
        finished.add(name)
    for name, label in started.items():
        if name not in finished:
            found.append(
                f"{label}: a background run needs a later finish step that names"
                f" it ({name!r}): how it ended is checked there"
            )
    return found


def _parts(data: Dict[str, Any]) -> Dict[str, Any]:
    """What parsed of a journey the model refused: valid fields, valid steps."""
    guide = data.get("guide")
    guide_ok = isinstance(guide, str) and re.fullmatch(GUIDE_PATTERN, guide)
    stack = data.get("stack")
    written = data.get("written")
    passwords = data.get("passwords")
    video = data.get("video")
    recordings: List[Recording] = []
    raw_recordings = data.get("recordings")
    for raw in raw_recordings if isinstance(raw_recordings, list) else []:
        try:
            recordings.append(Recording.model_validate(raw))
        except ValidationError:
            continue
    raw_steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    steps: List[Tuple[int, Step]] = []
    for index, raw in enumerate(raw_steps):
        try:
            steps.append((index, Step.model_validate(raw)))
        except ValidationError:
            continue
    return {
        "guide_path": guide if guide_ok else None,
        "stack": stack if stack in STACKS else None,
        "written": (
            written
            if isinstance(written, datetime.date)
            and not isinstance(written, datetime.datetime)
            else None
        ),
        "steps": steps,
        "complete": bool(raw_steps) and len(steps) == len(raw_steps),
        "passwords": (
            [name for name in passwords if isinstance(name, str)]
            if isinstance(passwords, list)
            else []
        ),
        "video": video if isinstance(video, bool) else None,
        "recordings": recordings,
    }


def load(path: Path, context: Context) -> Journey:
    """The journey in *path*, or ``Refused`` with the problems of every stage."""
    shown = path.name
    stem = path.stem
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise Refused(shown, [f"not YAML: {error}".splitlines()[0]]) from None
    if not isinstance(data, dict):
        raise Refused(shown, ["the file is not a mapping"])
    problems, covered = raw_problems(data)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", stem):
        problems.append(f"the file name {stem!r} is not a lower-case slug")
    journey: Optional[Journey] = None
    try:
        journey = Journey.model_validate(data)
    except ValidationError as error:
        problems.extend(_model_problems(error, data, covered))
    if journey is not None:
        parts: Dict[str, Any] = {
            "guide_path": journey.guide,
            "stack": journey.stack,
            "written": journey.written,
            "steps": list(enumerate(journey.steps)),
            "complete": True,
            "passwords": list(journey.passwords),
            "video": journey.video,
            "recordings": list(journey.recordings),
        }
    else:
        parts = _parts(data)
    problems.extend(semantic_problems(**parts, stem=stem, context=context))
    if problems or journey is None:
        raise Refused(shown, problems or ["the journey does not load"])
    return journey
