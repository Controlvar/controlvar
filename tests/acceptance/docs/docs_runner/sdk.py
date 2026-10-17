"""An SDK's quick start, installed from the public registry and run as its page says.

An ``sdk`` step (compose-dev only) follows an SDK page the way its reader does:

1. In a fresh temporary directory it makes a project (``package.json`` holding
   ``{"private": true}``) or a virtual environment (``python -m venv``), and
   runs the page's install command exactly: the first shell block of the
   section the step names as ``install``, which must be one line,
   ``npm install <package>@<version>`` or ``pip install <package>==<version>``.
   npm is pointed at ``NPM_REGISTRY`` and pip at ``PYPI_INDEX``, with no user
   configuration and no cache, so the package comes from the public registry.
   An install that does not exit 0, or that installs any other version, fails
   the step: it is never NOT RUN.
2. It writes the page's code block (the first fenced block of the section the
   step names as ``snippet``, in the step's ``language``) to a file, with only
   the page's placeholders replaced, and runs it: ``node`` with its type
   stripping for TypeScript, the environment's ``python`` for Python. A
   placeholder is a whole quoted string of the block (``'user-123'``,
   ``"http://localhost:8000"``), replaced by one of ``PLACEHOLDERS``: the
   stack's API address, the experiment's key, the flag's key, the step's user.
   The API key is given as ``CONTROLVAR_API_KEY`` and the API's address as
   ``CONTROLVAR_API_URL``, the variables the pages' blocks read, and under
   their names before the rename for a page that predates it.
3. Before the block, each of the step's ``stubs`` is defined: a function the
   block calls that the page leaves to the reader (``render_new_checkout()``),
   which records that it was called and does nothing else. After the block,
   one line is printed: the value of each expression the step's ``answers``
   name (a variable of the block, an attribute or key path under one, or
   ``called.<stub>``). Nothing else is added to the block.
4. Each answer must be what the oracle gives, independently of the SDK
   (``traffic.py``): the variant the documented assignment hash gives the user
   under the experiment's key and allocations, and the flag answer the
   documented rollout hash gives at the step's percentage (reason
   ``rollout``).

Go (``language: go``) differs in three places, because its install is not one
pinned line and nothing can be appended to its block (an unused variable or
import does not compile):

* The install section's first shell block holds the page's own commands, run
  as written and in the page's order: at most one ``go mod init <path>`` and
  exactly one ``go get <module>[@<version>]``. The runner never writes go.mod,
  so a page that drops ``go mod init`` fails at ``go get`` ("go.mod file not
  found"), as its reader would. ``go`` is the one on PATH (setup-go installs
  the line sdk/go/go.mod's ``go`` directive names): ``go version`` must be
  that line (``go1.21.x`` for ``go 1.21``), and the page's "Requires Go X+"
  must name the directive's version (the loader refuses the journey otherwise,
  ``go_requirement_problem``). Modules come from proxy.golang.org, checked
  against sum.golang.org, with ``GOTOOLCHAIN=local`` and no user configuration
  (``GOENV=off``, ``GOWORK=off``).
* What ``go get`` resolved is recorded (``go list -m -json``) and must be a
  pseudo-version or a semver tag; a ``replace`` of the module, in the project's
  go.mod or in what ``go list`` reports, is refused. The downloaded module
  (``go mod download -json``'s ``Dir``) is compared with this checkout's
  sdk/go, file by file (``comparison_rule``): on a run of a branch (main, or a
  dispatched branch) that resolved a pseudo-version, the head of main, a
  difference fails the step; on a run of a tag, or when ``go get`` resolved a
  semver tag (the module's first release onwards), the difference is recorded
  and does not fail. So a main run fails when sdk/go changed within the module
  proxy's cache window (``go get`` then still resolves the commit before the
  change), and a branch run fails when main's sdk/go has moved past the
  branch's.
* The block is the page's complete ``package main``, written to main.go with
  only its placeholders replaced, and run with ``go run .``. The program names
  the API's address itself, so a go step must replace it with ``{{api-url}}``
  (``GO_REQUIRED_PLACEHOLDERS``; the model refuses a step that does not). It must print
  exactly one line on stdout, ``key=value`` pairs separated by single spaces
  (``variant=treatment flag=true``), whose keys are exactly the step's
  ``answers`` (``read_line``); ``true`` and ``false`` are read as booleans.
  The block takes no ``stubs``.

Every command's output passes through the run's redactor before it is kept,
and a command whose output held a value the run keeps out of its files (the
API key, a token) fails the step: a quick start must not print a credential.
The directory is removed when the step ends; nothing in it is written to the
run directory, and the key is never written to a file, only given in the
environment of the block's process.

Nothing here imports Playwright or the product; the commands go through an
``execute`` the runner passes in, so the unit job tests the rest with a fake.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from docs_runner import traffic

NPM_REGISTRY = "https://registry.npmjs.org/"
PYPI_INDEX = "https://pypi.org/simple"
#: Where ``go get`` fetches modules from, and the checksum database it checks.
GO_PROXY = "https://proxy.golang.org,direct"
GO_SUMDB = "sum.golang.org"
#: This checkout. A Go step compares the module ``go get`` resolved with its
#: sdk/go, whose go.mod also names the Go line the page must require.
REPO_ROOT = Path(__file__).resolve().parents[4]
GO_MODULE = Path("sdk") / "go"
#: The page's Go install commands: ``go mod init <path>``, ``go get <module>``.
GO_MOD_INIT = re.compile(r"^go mod init (?P<path>[A-Za-z0-9][A-Za-z0-9._~/-]{0,200})$")
GO_GET = re.compile(
    r"^go get (?P<package>[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/[A-Za-z0-9._~-]+){1,20})"
    r"(?:@(?P<version>v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?))?$"
)
#: "Requires Go 1.21+" on the page, and go.mod's ``go 1.21``.
GO_REQUIRES = re.compile(r"Requires Go (?P<version>[0-9]+\.[0-9]+(?:\.[0-9]+)?)\+")
GO_DIRECTIVE = re.compile(r"^go (?P<version>[0-9]+\.[0-9]+(?:\.[0-9]+)?)[ \t]*$", re.M)
#: What ``go version`` prints: ``go version go1.21.13 linux/amd64``.
GO_VERSION = re.compile(r"^go version go(?P<version>[0-9][0-9A-Za-z.]{0,30}) ", re.M)
#: A ``replace`` directive in a go.mod, alone or as a block.
GO_REPLACE = re.compile(r"^[ \t]*replace\b", re.M)
#: The three forms of a Go pseudo-version (golang.org/x/mod/module), with a
#: 12-character commit: ``v0.0.0-20261007170856-0ee808b07642`` with no tag.
PSEUDO_VERSION = re.compile(
    r"^v[0-9]+\.(?:0\.0-|[0-9]+\.[0-9]+-(?:[0-9A-Za-z.-]*\.)?0\.)[0-9]{14}-[0-9a-f]{12}$"
)
#: A module's release tag: ``v0.1.0``, ``v1.2.3-rc.1``.
SEMVER_TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
#: A key of a Go block's answer line, and its value.
LINE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LINE_VALUE = re.compile(r"^[^\s=]{1,200}$")
#: What a replacement may be: the stack's API address, or an id from the journey.
PLACEHOLDERS = ("{{api-url}}", "{{experiment}}", "{{flag}}", "{{user}}")
#: The placeholders every sdk step must replace: the oracle answers for these.
REQUIRED_PLACEHOLDERS = ("{{experiment}}", "{{flag}}", "{{user}}")
#: A Go block also names the API's address in the program itself
#: (``exp.WithBaseURL("http://localhost:8000")``), where no environment variable
#: overrides it, so a go step must replace that too: a step that did not would
#: run the program against whatever answers on the page's address, never the
#: journey's stack.
GO_REQUIRED_PLACEHOLDERS = REQUIRED_PLACEHOLDERS + ("{{api-url}}",)


def required_placeholders(language: str) -> Tuple[str, ...]:
    """The placeholders a step in *language* must replace."""
    return GO_REQUIRED_PLACEHOLDERS if language == "go" else REQUIRED_PLACEHOLDERS


#: The page's install command, by the language of its block.
INSTALL = {
    "typescript": re.compile(
        r"^npm install (?P<package>@?[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)?)"
        r"@(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)$"
    ),
    "python": re.compile(
        r"^pip install (?P<package>[A-Za-z0-9][A-Za-z0-9._-]*)"
        r"==(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.]+)?)$"
    ),
}
SHELL_LANGUAGES = ("bash", "sh", "shell")
#: The line the program prints after the block, before the answers' JSON.
MARKER = "DOCS-JOURNEY-ANSWERS "
#: ``called.<stub>``: true when the block called that stub.
CALLED = "called"
#: A value put in place of a placeholder: no quote, no backslash, no space.
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/-]{1,200}$")
#: How long each command may take, in seconds.
VENV_SECONDS = 180
INSTALL_SECONDS = 300
VERSION_SECONDS = 60
RUN_SECONDS = 120
#: ``go run .`` compiles first, the standard library included on a fresh cache.
GO_RUN_SECONDS = 300
#: How much of each command's output is kept, from its end, after redaction.
OUTPUT_CHARS = 4000

FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*(.*)$")


class SdkError(Exception):
    """The page or the step cannot be followed; the message says why."""


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Block:
    language: str
    text: str


def blocks(section: str) -> List[Block]:
    """The fenced code blocks of a guide's section, in order.

    A block's language is the first word of its info string, without the
    braces and the dot of an attribute list (``{.bash exec}`` is ``bash``).
    """
    found: List[Block] = []
    lines = section.splitlines()
    index = 0
    while index < len(lines):
        opened = FENCE.match(lines[index])
        if not opened:
            index += 1
            continue
        fence, info = opened.group(1), opened.group(2).strip()
        words = info.lstrip("{").split()
        language = words[0].lstrip(".").rstrip("}") if words else ""
        body: List[str] = []
        index += 1
        while index < len(lines):
            closing = FENCE.match(lines[index])
            if (
                closing
                and closing.group(1)[0] == fence[0]
                and len(closing.group(1)) >= len(fence)
                and not closing.group(2).strip()
            ):
                break
            body.append(lines[index])
            index += 1
        found.append(Block(language=language, text="\n".join(body) + "\n"))
        index += 1
    return found


@dataclass(frozen=True)
class Install:
    """The page's install command, as written, and what it names.

    For Go, ``line`` is the ``go get`` and ``commands`` every command of the
    block, in the page's order; ``version`` is empty when the page names none.
    """

    line: str
    tool: str
    package: str
    version: str
    commands: Tuple[str, ...] = ()


def _go_install(lines: Sequence[str]) -> Install:
    """The page's Go commands: at most one ``go mod init``, one ``go get``."""
    gets = []
    inits = 0
    for line in lines:
        if GO_MOD_INIT.match(line):
            inits += 1
            continue
        got = GO_GET.match(line)
        if got is None:
            raise SdkError(
                f"the install command {line!r} is not `go mod init <path>` or"
                " `go get <module>[@<version>]`"
            )
        gets.append(got)
    if len(gets) != 1:
        raise SdkError(
            f"the install section's first block holds {len(gets)} go get commands,"
            " not one"
        )
    if inits > 1:
        raise SdkError(
            f"the install section's first block holds {inits} go mod init commands"
        )
    return Install(
        line=gets[0].group(0),
        tool="go",
        package=gets[0]["package"],
        version=gets[0]["version"] or "",
        commands=tuple(lines),
    )


def install_command(section: str, language: str) -> Install:
    """The section's first code block as an install command, or SdkError."""
    found = blocks(section)
    if not found:
        raise SdkError("the install section has no code block")
    first = found[0]
    if first.language not in SHELL_LANGUAGES:
        raise SdkError(
            f"the install section's first block is {first.language or 'untagged'},"
            " not a shell block"
        )
    lines = [
        line.strip()
        for line in first.text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if language == "go":
        return _go_install(lines)
    if len(lines) != 1:
        raise SdkError(
            f"the install section's first block holds {len(lines)} commands, not one"
        )
    match = INSTALL[language].match(lines[0])
    if not match:
        shape = (
            "npm install <package>@<version>"
            if language == "typescript"
            else "pip install <package>==<version>"
        )
        raise SdkError(
            f"the install command {lines[0]!r} is not {shape}, a package at one version"
        )
    tool = "npm" if language == "typescript" else "pip"
    return Install(
        line=lines[0], tool=tool, package=match["package"], version=match["version"]
    )


def snippet(section: str, language: str) -> str:
    """The section's first code block, which must be in *language*, or SdkError."""
    found = blocks(section)
    if not found:
        raise SdkError("the snippet section has no code block")
    if found[0].language != language:
        raise SdkError(
            f"the snippet section's first block is {found[0].language or 'untagged'},"
            f" not {language}"
        )
    return found[0].text


def _quoted(literal: str) -> Tuple[str, str]:
    return f"'{literal}'", f'"{literal}"'


def occurrences(text: str, literal: str) -> int:
    """How many times *literal* is a whole quoted string of *text*."""
    return sum(text.count(form) for form in _quoted(literal))


def substitute(text: str, replacements: Mapping[str, str]) -> str:
    """*text* with each placeholder, a whole quoted string, replaced; SdkError if absent.

    The quote is kept: ``'user-123'`` becomes ``'sdk-js-user-1'``. Each value
    must be a ``SAFE_VALUE``, so a replacement can only be an address or an id.
    """
    for literal, value in replacements.items():
        if not SAFE_VALUE.match(value):
            raise SdkError(f"{value!r} cannot stand in for {literal!r}")
        if not occurrences(text, literal):
            raise SdkError(f"the block has no quoted {literal!r} to replace")
        for form, quote in zip(_quoted(literal), ("'", '"')):
            text = text.replace(form, f"{quote}{value}{quote}")
    return text


# ---------------------------------------------------------------------------
# The program: stubs, the block, the answers
# ---------------------------------------------------------------------------
def _parts(expression: str) -> Tuple[str, List[str]]:
    root, *rest = expression.split(".")
    return root, rest


def _typescript(block: str, stubs: Sequence[str], answers: Sequence[str]) -> str:
    head = []
    if stubs:
        head.append("const __docsJourneyCalled = new Set();")
        for name in stubs:
            head.append(
                f"function {name}(...__docsJourneyArgs) {{"
                f" __docsJourneyCalled.add({json.dumps(name)}); }}"
            )
    reads = []
    for expression in answers:
        root, rest = _parts(expression)
        if root == CALLED:
            code = f"__docsJourneyCalled.has({json.dumps(rest[0])})"
        else:
            code = root + "".join(f"?.[{json.dumps(part)}]" for part in rest)
        reads.append(f"  {json.dumps(expression)}: __docsJourneyRead(() => {code}),")
    tail = [
        "const __docsJourneyRead = (read) => {",
        "  try {",
        "    const value = read();",
        "    return value === undefined ? { undefined: true } : value;",
        "  } catch (error) {",
        "    return { error: String(error) };",
        "  }",
        "};",
        f"console.log({json.dumps(MARKER)} + JSON.stringify({{",
        *reads,
        "}));",
    ]
    return "\n".join([*head, block.rstrip("\n"), *tail]) + "\n"


def _python(block: str, stubs: Sequence[str], answers: Sequence[str]) -> str:
    head = []
    if stubs:
        head.append("__docs_journey_called = set()")
        for name in stubs:
            head.append(
                f"def {name}(*args, **kwargs):\n"
                f"    __docs_journey_called.add({json.dumps(name)})"
            )
    reads = []
    for expression in answers:
        root, rest = _parts(expression)
        if root == CALLED:
            code = f"{json.dumps(rest[0])} in __docs_journey_called"
        else:
            code = root
            for part in rest:
                code = f"__docs_journey_part({code}, {json.dumps(part)})"
        reads.append(
            f"        {json.dumps(expression)}: __docs_journey_read(lambda: {code}),"
        )
    tail = [
        "def __docs_journey_read(read):",
        "    try:",
        "        return read()",
        "    except Exception as error:",
        '        return {"error": f"{type(error).__name__}: {error}"}',
        "def __docs_journey_part(value, part):",
        "    return value[part] if isinstance(value, dict) else getattr(value, part)",
        "import json as __docs_journey_json",
        "print(",
        f"    {json.dumps(MARKER)}",
        "    + __docs_journey_json.dumps(",
        "        {",
        *reads,
        "        },",
        "        default=repr,",
        "    ),",
        "    flush=True,",
        ")",
    ]
    return "\n".join([*head, block.rstrip("\n"), *tail]) + "\n"


def program(
    language: str, block: str, stubs: Sequence[str], answers: Sequence[str]
) -> str:
    """What is run: the stubs, the block (already substituted), the answers' line.

    A Go block is a complete program that prints its own answer line: it is run
    as it is, and takes no stubs.
    """
    if language == "go":
        if stubs:
            raise SdkError("a Go block is a complete program; it takes no stubs")
        return block
    build = _typescript if language == "typescript" else _python
    return build(block, stubs, answers)


def read_answers(stdout: str) -> Dict[str, Any]:
    """The answers the program printed, or SdkError: exactly one ``MARKER`` line."""
    lines = [line for line in stdout.splitlines() if line.startswith(MARKER)]
    if len(lines) != 1:
        raise SdkError(
            f"the block's answers were printed {len(lines)} times, not once (the"
            " block stopped before its end, or printed the marker itself)"
        )
    try:
        answers = json.loads(lines[0][len(MARKER) :])
    except ValueError:
        raise SdkError("the block's answers are not JSON") from None
    if not isinstance(answers, dict):
        raise SdkError("the block's answers are not an object")
    return answers


def read_line(stdout: str, keys: Sequence[str]) -> Dict[str, Any]:
    """A Go block's answers: its one line of ``key=value`` pairs, or SdkError.

    stdout must hold exactly one line that is not blank, the pairs separated by
    single spaces, each key once and the keys exactly *keys*. ``true`` and
    ``false`` are booleans; every other value is a string.
    """
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise SdkError(
            f"the program printed {len(lines)} lines on stdout, not its one answer line"
        )
    line = lines[0]
    answers: Dict[str, Any] = {}
    for pair in line.split(" "):
        key, equals, value = pair.partition("=")
        if (
            not equals
            or not LINE_KEY.match(key)
            or not LINE_VALUE.match(value)
            or key in answers
        ):
            raise SdkError(
                f"the answer line {line[:200]!r} is not key=value pairs separated by"
                " single spaces, each key once"
            )
        answers[key] = {"true": True, "false": False}.get(value, value)
    wanted = sorted(set(keys))
    if sorted(answers) != wanted:
        raise SdkError(
            f"the answer line names {', '.join(sorted(answers))}, not"
            f" {', '.join(wanted)}"
        )
    return answers


# ---------------------------------------------------------------------------
# Go: the toolchain line, the module go get resolved
# ---------------------------------------------------------------------------
def go_requirement(page: str) -> str:
    """The version of the page's "Requires Go X+", which it must say once."""
    found = GO_REQUIRES.findall(page)
    if len(found) != 1:
        raise SdkError(f'the page says "Requires Go X+" {len(found)} times, not once')
    return found[0]


def go_directive(go_mod: str) -> str:
    """The version of a go.mod's ``go`` directive, which it must have once."""
    found = GO_DIRECTIVE.findall(go_mod)
    if len(found) != 1:
        raise SdkError(f"sdk/go/go.mod has {len(found)} go directives, not one")
    return found[0]


def go_requirement_problem(page: str, go_mod: str) -> Optional[str]:
    """Why the page's "Requires Go X+" is not sdk/go/go.mod's ``go`` version, or None."""
    try:
        required, directive = go_requirement(page), go_directive(go_mod)
    except SdkError as problem:
        return str(problem)
    if required != directive:
        return (
            f'the page says "Requires Go {required}+", but sdk/go/go.mod says'
            f" go {directive}"
        )
    return None


def _go_line(version: str) -> str:
    return ".".join(version.split(".")[:2])


def go_version_problem(printed: str, directive: str) -> Optional[str]:
    """Why ``go version``'s output is not the line of *directive*, or None."""
    found = GO_VERSION.search(printed)
    if found is None:
        return f"`go version` printed {printed.strip()[:200]!r}, not a Go version"
    line = _go_line(directive)
    version = found["version"]
    if version != line and not version.startswith(line + "."):
        return (
            f"`go version` is go{version}, not go{line}.x, the line sdk/go/go.mod's"
            f" go {directive} names (setup-go installs it from that file)"
        )
    return None


def classify(version: str) -> str:
    """``pseudo-version`` (a commit, the head of main) or ``tag``; SdkError otherwise."""
    if PSEUDO_VERSION.match(version):
        return "pseudo-version"
    if SEMVER_TAG.match(version):
        return "tag"
    raise SdkError(
        f"go get resolved {version or '(no version)'}, neither a pseudo-version nor"
        " a semver tag"
    )


def comparison_rule(kind: str, ref: str) -> str:
    """``compare`` (a difference fails the step) or ``record`` (it is only kept).

    A run of a branch (main, or a dispatched branch: ``refs/heads/...``, or no
    ref when run by hand) that resolved a pseudo-version, which is the head of
    main, compares. A run of a tag, or a module that resolved a semver tag, may
    rightly differ from this checkout, so it records.
    """
    if kind == "tag" or ref.startswith("refs/tags/"):
        return "record"
    return "compare"


def _files(root: Path) -> Dict[str, str]:
    """Each regular file under *root*, by its path below it, with its digest."""
    found: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            found[path.relative_to(root).as_posix()] = digest
    return found


def differences(module: Path, checkout: Path) -> List[str]:
    """How the module ``go get`` resolved differs from this checkout's sdk/go."""
    theirs, ours = _files(module), _files(checkout)
    found = []
    for name in sorted(set(theirs) | set(ours)):
        if name not in theirs:
            found.append(f"{name}: only in this checkout")
        elif name not in ours:
            found.append(f"{name}: only in the module go get resolved")
        elif theirs[name] != ours[name]:
            found.append(f"{name}: differs")
    return found


def replace_problem(listed: Mapping[str, Any], go_mod: str) -> Optional[str]:
    """Why the module built is not the one resolved: a ``replace``, or None."""
    target = listed.get("Replace")
    if target:
        where = target if isinstance(target, Mapping) else {}
        shown = " ".join(
            str(where.get(part)) for part in ("Path", "Version") if where.get(part)
        )
        return (
            f"the project's go.mod replaces {listed.get('Path', 'the module')} with"
            f" {shown or 'another module'}; the module built must be the one go get"
            " resolved"
        )
    if GO_REPLACE.search(go_mod):
        return (
            "the project's go.mod has a replace directive; the module built must be"
            " the one go get resolved"
        )
    return None


@dataclass(frozen=True)
class GoCheck:
    """What a Go run is held to besides the page."""

    #: sdk/go/go.mod's ``go`` version: ``go version`` must be its line.
    directive: str
    #: This checkout's sdk/go, compared with the module go get resolved.
    checkout: Path
    #: The run's ``GITHUB_REF`` (empty when run by hand): a tag run records.
    ref: str


# ---------------------------------------------------------------------------
# The oracle and the comparison
# ---------------------------------------------------------------------------
def expected(
    user: str,
    experiment_key: str,
    allocations: Sequence[Tuple[str, int]],
    flag: str,
    rollout: int,
) -> Dict[str, Any]:
    """What the documented hashes give *user* (``traffic.py``), independently of the SDK."""
    enabled, reason = traffic.expected_answer(user, flag, "rollout", rollout)
    return {
        "variant": traffic.variant_for(user, experiment_key, allocations),
        "enabled": enabled,
        "reason": reason,
    }


#: What each answer is held to, for the step's file and its log line.
ORACLE_NAMES = {
    "variant": "the documented assignment hash",
    "enabled": "the documented rollout hash",
    "reason": "a flag with no rule, by rollout,",
}


def _same(value: Any, wanted: Any) -> bool:
    """Equal as JSON values: ``true`` is not ``1``, as it would be in Python."""
    if isinstance(wanted, bool) or isinstance(value, bool):
        return isinstance(value, bool) and isinstance(wanted, bool) and value == wanted
    return value == wanted


def compare(
    answers_spec: Mapping[str, Sequence[str]],
    answers: Mapping[str, Any],
    wanted: Mapping[str, Any],
) -> List[str]:
    """One problem per expression whose answer is not the oracle's."""
    problems = []
    for kind, expressions in answers_spec.items():
        for expression in expressions:
            value = answers.get(expression, {"absent": True})
            if not _same(value, wanted[kind]):
                problems.append(
                    f"{expression} is {json.dumps(value)}; {ORACLE_NAMES[kind]}"
                    f" gives {json.dumps(wanted[kind])}"
                )
    return problems


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------
@dataclass
class Result:
    """A command's exit status (None when it timed out or could not start)."""

    status: Optional[int]
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    seconds: float = 0.0


#: ``execute(argv, cwd, env, timeout)``: run one command, its output captured.
Execute = Callable[[Sequence[str], Path, Mapping[str, str], float], Result]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def execute(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float
) -> Result:
    """Run *argv* with no shell, its output captured; never raises."""
    started = time.monotonic()
    try:
        done = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        return Result(
            status=None,
            stdout=_text(error.stdout),
            stderr=_text(error.stderr),
            error=f"stopped after {timeout:.0f} s",
            seconds=time.monotonic() - started,
        )
    except OSError as error:
        return Result(
            status=None,
            error=f"{argv[0]} could not be started ({type(error).__name__})",
            seconds=time.monotonic() - started,
        )
    return Result(
        status=done.returncode,
        stdout=_text(done.stdout),
        stderr=_text(done.stderr),
        seconds=time.monotonic() - started,
    )


def environment(home: Path) -> Dict[str, str]:
    """What every command sees: PATH, a home of its own, and the public registries.

    Nothing else from the runner's environment: no user or global npm or pip
    configuration (the home is the step's own directory), no cache from an
    earlier install, no proxy or mirror.
    """
    tmp = home / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "npm_config_registry": NPM_REGISTRY,
        "npm_config_cache": str(home / "npm-cache"),
        "npm_config_userconfig": str(home / ".npmrc"),
        "npm_config_globalconfig": str(home / "npmrc"),
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "npm_config_update_notifier": "false",
        "PIP_INDEX_URL": PYPI_INDEX,
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1",
        "PIP_NO_INPUT": "1",
    }


def go_environment(home: Path) -> Dict[str, str]:
    """What a Go step's commands see besides ``environment``: the public proxy
    and checksum database, the toolchain on PATH and no other, and module and
    build caches of the step's own, with no go.env, workspace or GOFLAGS."""
    return {
        "GOPATH": str(home / "go"),
        "GOMODCACHE": str(home / "go" / "pkg" / "mod"),
        "GOCACHE": str(home / "go-build"),
        "GOENV": "off",
        "GOWORK": "off",
        "GOTOOLCHAIN": "local",
        "GOFLAGS": "",
        "GOPROXY": GO_PROXY,
        "GOSUMDB": GO_SUMDB,
        "GONOSUMDB": "",
        "GONOPROXY": "",
        "GOPRIVATE": "",
        "GOINSECURE": "",
    }


@dataclass
class Outcome:
    """What the step did, in the order it did it, with every output redacted."""

    install: Install
    commands: List[Dict[str, Any]] = field(default_factory=list)
    installed: str = ""
    answers: Dict[str, Any] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)
    #: True when any command printed a value the run keeps out of its files.
    printed_a_secret: bool = False
    #: Go: what ``go version`` printed.
    toolchain: str = ""
    #: Go: the module go get resolved, its kind, the rule applied and the
    #: differences from this checkout's sdk/go.
    module: Dict[str, Any] = field(default_factory=dict)


def _tail(text: str) -> str:
    return text if len(text) <= OUTPUT_CHARS else "..." + text[-OUTPUT_CHARS:]


#: What the go command prints on its way, which says nothing about a failure.
GO_PROGRESS = (
    "go: downloading ",
    "go: added ",
    "go: upgraded ",
    "go: finding ",
    "go: creating new go.mod",
    "go: to add module requirements",
)
#: ``go run``'s own last line when the program exits non-zero.
EXIT_STATUS = re.compile(r"^exit status [0-9]+$")


def _says_why(line: str) -> bool:
    if "complete log of this run" in line:
        return False
    if "error" in line.lower():
        return True
    return line.startswith("go: ") and not line.startswith(GO_PROGRESS)


def _what_it_said(*texts: str) -> str:
    """The lines of a failed command's output that say why: its first two
    error lines (npm's pointer to its own log and the go command's progress
    aside), else its last line (``go run``'s "exit status N" aside)."""
    for text in texts:
        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not EXIT_STATUS.match(line.strip())
        ]
        errors = [line for line in lines if _says_why(line)]
        if errors:
            return " / ".join(line[:200] for line in errors[:2])
        if lines:
            return lines[-1][:200]
    return ""


def run(
    *,
    language: str,
    install: Install,
    source: str,
    secrets_env: Mapping[str, str],
    redact: Callable[[str], str],
    run_command: Execute = execute,
    python: str = sys.executable,
    go: Optional[GoCheck] = None,
    answer_keys: Sequence[str] = (),
) -> Outcome:
    """Install as the page says, in a fresh directory, and run *source*.

    *redact* takes every value the run keeps out of its files out of a text;
    each command's output goes through it before anything else reads it, and
    a text it changes means the command printed one. *secrets_env* (the API
    key and the API's address) is given only to the program's process. The
    directory is removed before this returns. A Go run needs *go* (what it is
    held to) and *answer_keys* (the keys of the line its block prints).
    """
    if language == "go" and go is None:
        raise SdkError("a Go run needs the toolchain line and the checkout's sdk/go")
    outcome = Outcome(install=install)
    home = Path(tempfile.mkdtemp(prefix="docs-journey-sdk-"))
    try:
        _run(
            outcome,
            home,
            language,
            install,
            source,
            secrets_env,
            redact,
            run_command,
            python,
            go,
            answer_keys,
        )
    finally:
        _remove(home)
    if outcome.printed_a_secret:
        outcome.problems.insert(
            0,
            "a command printed a value the run keeps out of its files (the API key"
            " or a token); it is redacted in every file",
        )
    return outcome


def _remove(home: Path) -> None:
    """Remove the step's directory. Go keeps its module cache read-only, so each
    directory under it (never a link out of it) is made writable first."""
    for root, dirs, _ in os.walk(home):
        for name in dirs:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                try:
                    os.chmod(path, 0o700)
                except OSError:
                    pass
    shutil.rmtree(home, ignore_errors=True)


def _run(
    outcome: Outcome,
    home: Path,
    language: str,
    install: Install,
    source: str,
    secrets_env: Mapping[str, str],
    redact: Callable[[str], str],
    run_command: Execute,
    python: str,
    go: Optional[GoCheck] = None,
    answer_keys: Sequence[str] = (),
) -> None:
    env = environment(home)
    if language == "go":
        env.update(go_environment(home))
    project = home / "project"
    project.mkdir()

    def command(argv: Sequence[str], timeout: float, shown: str = "", extra=None):
        used = dict(env)
        used.update(extra or {})
        result = run_command(argv, project, used, timeout)
        out, err = redact(result.stdout), redact(result.stderr)
        if out != result.stdout or err != result.stderr:
            outcome.printed_a_secret = True
        outcome.commands.append(
            {
                "run": shown or " ".join([Path(argv[0]).name, *argv[1:]]),
                "status": result.status,
                "seconds": round(result.seconds, 1),
                "stdout": _tail(out),
                "stderr": _tail(err),
                **({"error": result.error} if result.error else {}),
            }
        )
        if result.status == 0:
            return out, ""
        said = result.error or f"exited {result.status}"
        last = _what_it_said(err, out)
        return out, said + (f": {last}" if last else "")

    if language == "go":
        assert go is not None
        _run_go(
            outcome, project, install, source, secrets_env, command, go, answer_keys
        )
        return
    if language == "typescript":
        (project / "package.json").write_text('{"private": true}\n', encoding="utf-8")
        installer = ["npm", "install", f"{install.package}@{install.version}"]
        file_name = "quick-start.mts"
        runner = [
            "node",
            "--experimental-strip-types",
            "--disable-warning=ExperimentalWarning",
            file_name,
        ]
    else:
        venv = home / "venv"
        _, why = command(
            [python, "-m", "venv", str(venv)],
            VENV_SECONDS,
            shown=f"{Path(python).name} -m venv (a fresh directory)",
        )
        if why:
            outcome.problems.append(f"making a virtual environment {why}")
            return
        installer = [
            str(venv / "bin" / "pip"),
            "install",
            f"{install.package}=={install.version}",
        ]
        file_name = "quick_start.py"
        runner = [str(venv / "bin" / "python"), file_name]
    _, why = command(installer, INSTALL_SECONDS, shown=install.line)
    if why:
        outcome.problems.append(f"the page's install, `{install.line}`, {why}")
        return
    outcome.installed = _installed_version(language, install, home, project, command)
    if outcome.installed != install.version:
        outcome.problems.append(
            f"`{install.line}` installed {install.package}"
            f" {outcome.installed or '(none found)'}, not {install.version}"
        )
        return
    (project / file_name).write_text(source, encoding="utf-8")
    out, why = command(runner, RUN_SECONDS, extra=secrets_env)
    if why:
        outcome.problems.append(f"the block {why}")
        return
    try:
        outcome.answers = read_answers(out)
    except SdkError as problem:
        outcome.problems.append(str(problem))


def _installed_version(
    language: str, install: Install, home: Path, project: Path, command
) -> str:
    """The version of the package now in the project or the environment, or ""."""
    if language == "typescript":
        manifest = project / "node_modules" / install.package / "package.json"
        try:
            return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
        except (OSError, ValueError, KeyError, TypeError):
            return ""
    out, why = command(
        [
            str(home / "venv" / "bin" / "python"),
            "-c",
            "import importlib.metadata as m, sys; print(m.version(sys.argv[1]))",
            install.package,
        ],
        VERSION_SECONDS,
        shown=f"python -c (the version of {install.package} installed)",
    )
    return "" if why else out.strip()


def _json_of(out: str, what: str) -> Mapping[str, Any]:
    try:
        value = json.loads(out)
    except ValueError:
        raise SdkError(f"{what} did not print one JSON object") from None
    if not isinstance(value, dict):
        raise SdkError(f"{what} did not print one JSON object")
    return value


def _run_go(
    outcome: Outcome,
    project: Path,
    install: Install,
    source: str,
    secrets_env: Mapping[str, str],
    command,
    go: GoCheck,
    answer_keys: Sequence[str],
) -> None:
    """The toolchain, the page's commands in its order, the module, the block."""
    out, why = command(["go", "version"], VERSION_SECONDS)
    if why:
        outcome.problems.append(f"`go version` {why}")
        return
    outcome.toolchain = out.strip()
    problem = go_version_problem(out, go.directive)
    if problem:
        outcome.problems.append(problem)
        return
    for line in install.commands:
        _, why = command(line.split(" "), INSTALL_SECONDS, shown=line)
        if why:
            outcome.problems.append(f"the page's install, `{line}`, {why}")
            return
    listing = f"go list -m -json {install.package}"
    out, why = command(listing.split(" "), VERSION_SECONDS, shown=listing)
    if why:
        outcome.problems.append(f"`{listing}` {why}")
        return
    go_mod = project / "go.mod"
    try:
        listed = _json_of(out, f"`{listing}`")
        problem = replace_problem(
            listed, go_mod.read_text(encoding="utf-8") if go_mod.is_file() else ""
        )
        if problem:
            raise SdkError(problem)
        outcome.installed = str(listed.get("Version") or "")
        kind = classify(outcome.installed)
    except SdkError as problem:
        outcome.problems.append(str(problem))
        return
    if install.version and outcome.installed != install.version:
        outcome.problems.append(
            f"`{install.line}` resolved {install.package} {outcome.installed},"
            f" not {install.version}"
        )
        return
    rule = comparison_rule(kind, go.ref)
    outcome.module = {
        "version": outcome.installed,
        "kind": kind,
        "ref": go.ref or "(none: run by hand)",
        "rule": rule,
        "compared_with": f"{GO_MODULE.as_posix()} of this checkout",
    }
    fetch = f"go mod download -json {install.package}@{outcome.installed}"
    out, why = command(fetch.split(" "), INSTALL_SECONDS, shown=fetch)
    if why:
        outcome.problems.append(f"`{fetch}` {why}")
        return
    try:
        directory = str(_json_of(out, f"`{fetch}`").get("Dir") or "")
    except SdkError as problem:
        outcome.problems.append(str(problem))
        return
    if not directory or not Path(directory).is_dir():
        outcome.problems.append(f"`{fetch}` named no directory of the module")
        return
    found = differences(Path(directory), go.checkout)
    outcome.module["differences"] = found
    if found and rule == "compare":
        outcome.problems.append(
            f"the module go get resolved, {install.package} {outcome.installed}, is"
            f" not this checkout's {GO_MODULE.as_posix()} ({'; '.join(found[:5])}):"
            " on a branch run the head of main must be this source (a push to"
            " sdk/go inside the module proxy's cache window, or a branch behind"
            " main's sdk/go, resolves other source)"
        )
    (project / "main.go").write_text(source, encoding="utf-8")
    out, why = command(["go", "run", "."], GO_RUN_SECONDS, extra=secrets_env)
    if why:
        outcome.problems.append(f"the block {why}")
        return
    try:
        outcome.answers = read_line(out, answer_keys)
    except SdkError as problem:
        outcome.problems.append(str(problem))


def fingerprint(text: str) -> str:
    """A short digest of the block, so a run's file says which block it ran."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
