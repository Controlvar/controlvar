"""The exposure examples read only fields that each SDK's ``Exposure`` has (#1499).

docs/sdk/exposure-hook.md shows one browser JavaScript example per analytics
tool, and the "Exposure hook" section of each SDK page shows that SDK's own.
Every ``exposure.<field>`` and ``e.<field>`` in those code blocks is checked
against the ``Exposure`` type of the page's SDK, read from the SDK's source
here rather than from a list kept in this file, in that language's spelling:

* JavaScript, React, React Native: the properties of ``export interface
  Exposure`` in each package's types.ts (``variantName``). The hub page's
  examples receive the ``Exposure`` of all three, so a field they read must be
  in all three.
* Python: the annotated fields of ``class Exposure`` in controlvar/types.py
  (``variant_name``), read with ``ast``. Each Python block must also parse.
* Go: the fields of ``type Exposure struct`` (``VariantName``).
* Java: the public no-argument methods of ``Exposure``, called
  (``getVariantName()``).
* .NET: the properties of ``record Exposure`` (``VariantName``).

The hub page has exactly five examples: five tools (Segment, Google
Analytics 4, Amplitude, Mixpanel, RudderStack), one browser JavaScript
example each. It shows no server-side example; the SDK pages do.

An SDK page that names its SDK's hook option has exactly one "Exposure hook"
section, so a renamed heading cannot take its blocks out of this check. A
page that names no hook option has no section and nothing to check.

Reads files only: no git, node, JVM, Go or dotnet, so it runs the same in
``scripts/core_build.sh``'s copy and on a docs-only change.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
HUB = "docs/sdk/exposure-hook.md"

#: The hub page's examples, in order: one per tool, each under its own H3 of
#: "Examples", each a browser JavaScript block.
TOOLS = ("Segment", "Google Analytics 4", "Amplitude", "Mixpanel", "RudderStack")

#: The variables the examples give an Exposure. A use is ``<variable>.<member>``.
#: ``e.g.`` in a comment is prose, not a use.
USE = re.compile(r"(?<![\w$.])(exposure|e(?!\.g\.))\??\.([A-Za-z_$][\w$]*)(\s*\()?")

FENCE_OPEN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*([^\s`{]*)")
HEADING = re.compile(r"^(#{2,3})[ \t]+(.+?)[ \t]*$")


# ---------------------------------------------------------------------------
# The Exposure types, read from source
# ---------------------------------------------------------------------------
def _ts_fields(source: str) -> FrozenSet[str]:
    """The properties of ``export interface Exposure { ... }``."""
    found = re.search(r"^export interface Exposure\s*\{(.*?)^\}", source, re.M | re.S)
    assert found, "no `export interface Exposure { ... }`"
    body = re.sub(r"/\*.*?\*/", "", found.group(1), flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    return frozenset(
        re.findall(r"^\s*(?:readonly\s+)?([A-Za-z_$][\w$]*)\??\s*:", body, re.M)
    )


def _python_fields(source: str) -> FrozenSet[str]:
    """The annotated fields of ``class Exposure``."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == "Exposure":
            return frozenset(
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            )
    raise AssertionError("no `class Exposure`")


def _go_fields(source: str) -> FrozenSet[str]:
    """The named fields of ``type Exposure struct { ... }``."""
    found = re.search(r"^type Exposure struct\s*\{(.*?)^\}", source, re.M | re.S)
    assert found, "no `type Exposure struct { ... }`"
    names = set()
    for line in re.sub(r"//[^\n]*", "", found.group(1)).splitlines():
        declared = re.match(r"\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s+\S", line)
        if declared:
            names.update(name.strip() for name in declared.group(1).split(","))
    return frozenset(names)


def _java_methods(source: str) -> FrozenSet[str]:
    """The public no-argument methods declared in ``class Exposure``."""
    found = re.search(r"\bclass Exposure\b[^{]*\{(.*)\}", source, re.S)
    assert found, "no `class Exposure { ... }`"
    return frozenset(
        re.findall(r"^\s*public\s+[\w<>\[\]]+\s+(\w+)\s*\(\s*\)", found.group(1), re.M)
    )


def _csharp_properties(source: str) -> FrozenSet[str]:
    """The properties of ``record Exposure``: its positional parameters, and any
    ``public T Name { get ... }`` in its body."""
    names = set()
    positional = re.search(r"\brecord\s+Exposure\s*\(([^)]*)\)", source)
    if positional:
        for parameter in positional.group(1).split(","):
            words = parameter.split("=")[0].split()
            if words:
                names.add(words[-1])
    body = re.search(r"\brecord\s+Exposure\b[^{;]*\{(.*)\}", source, re.S)
    if body:
        names.update(
            re.findall(r"\bpublic\s+[\w?<>\[\], ]+?\s+(\w+)\s*\{\s*get", body.group(1))
        )
    assert names, "no `record Exposure`"
    return frozenset(names)


@dataclass(frozen=True)
class Sdk:
    """An SDK's Exposure: where it is declared, and how its members are read."""

    name: str
    source: str
    reader: Callable[[str], FrozenSet[str]]
    #: Java's members are methods: a use must call one.
    called: bool = False

    def members(self) -> FrozenSet[str]:
        path = REPO_ROOT / self.source
        assert path.is_file(), (
            f"{self.source} is gone: where is {self.name}'s Exposure?"
        )
        members = self.reader(path.read_text(encoding="utf-8"))
        assert members, f"read no member of Exposure in {self.source}"
        return members


JS = Sdk("JavaScript", "sdk/js/src/types.ts", _ts_fields)
REACT = Sdk("React", "sdk/react/src/client/types.ts", _ts_fields)
REACT_NATIVE = Sdk("React Native", "sdk/react-native/src/types.ts", _ts_fields)
PYTHON = Sdk("Python", "sdk/python/controlvar/types.py", _python_fields)
GO = Sdk("Go", "sdk/go/types.go", _go_fields)
JAVA = Sdk(
    "Java",
    "sdk/java/core/src/main/java/com/controlvar/sdk/model/Exposure.java",
    _java_methods,
    called=True,
)
DOTNET = Sdk(".NET", "sdk/dotnet/src/Controlvar/Models/Exposure.cs", _csharp_properties)

#: Each SDK page, its SDK, and the name of its hook option: a page that names
#: it documents the hook.
SDK_PAGES: Dict[str, Tuple[Sdk, str]] = {
    "docs/sdk/javascript.md": (JS, "onExposure"),
    "docs/sdk/python.md": (PYTHON, "on_exposure"),
    "docs/sdk/go.md": (GO, "WithOnExposure"),
    "docs/sdk/react.md": (REACT, "onExposure"),
    "docs/sdk/react-native.md": (REACT_NATIVE, "onExposure"),
    "docs/sdk/java.md": (JAVA, "exposureListener"),
    "docs/sdk/dotnet.md": (DOTNET, "OnExposure"),
}

#: The SDKs whose Exposure the hub page's examples receive.
HUB_SDKS = (JS, REACT, REACT_NATIVE)


# ---------------------------------------------------------------------------
# The pages' code blocks
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Block:
    """A fenced code block, with the headings it sits under."""

    page: str
    line: int  # the page line of its first code line
    info: str  # its language tag, lower case
    code: str
    h2: Optional[str]
    h3: Optional[str]


def _text(page: str) -> str:
    return (REPO_ROOT / page).read_text(encoding="utf-8")


def _scan(page: str, text: Optional[str] = None) -> Tuple[List[str], List[Block]]:
    """The level-2 headings and the fenced blocks of ``page``, in order. A
    heading inside a block is code, not a heading."""
    lines = (_text(page) if text is None else text).splitlines()
    h2s: List[str] = []
    found: List[Block] = []
    h2: Optional[str] = None
    h3: Optional[str] = None
    fence: Optional[Tuple[str, str, int, List[str]]] = None
    for number, line in enumerate(lines, 1):
        if fence is None:
            opened = FENCE_OPEN.match(line)
            if opened:
                fence = (opened.group(1), opened.group(2).lower(), number, [])
                continue
            heading = HEADING.match(line)
            if heading and len(heading.group(1)) == 2:
                h2, h3 = heading.group(2), None
                h2s.append(h2)
            elif heading:
                h3 = heading.group(2)
            continue
        marker, info, start, body = fence
        close = r"^[ \t]{0,3}" + re.escape(marker[0]) + "{%d,}[ \t]*$" % len(marker)
        if re.match(close, line):
            found.append(Block(page, start + 1, info, "\n".join(body), h2, h3))
            fence = None
        else:
            body.append(line)
    assert fence is None, f"{page}: the block opened at line {fence[2]} is never closed"
    return h2s, found


def _blocks(page: str, text: Optional[str] = None) -> List[Block]:
    return _scan(page, text)[1]


def _h2s(page: str) -> List[str]:
    return _scan(page)[0]


def _uses(block: Block) -> List[Tuple[int, str, str, bool]]:
    """``(page line, variable, member, called)`` for each use in the block."""
    uses = []
    for offset, line in enumerate(block.code.splitlines()):
        for use in USE.finditer(line):
            uses.append(
                (block.line + offset, use.group(1), use.group(2), bool(use.group(3)))
            )
    return uses


def _problems(block: Block, sdk: Sdk, where: str) -> List[str]:
    """One line per use in ``block`` that ``sdk``'s Exposure does not have."""
    members = sdk.members()
    problems = []
    for line, variable, member, called in _uses(block):
        spelled = f"{variable}.{member}" + ("()" if called else "")
        if member not in members:
            problems.append(
                f"{where} ({block.page}:{line}): {spelled} is not a "
                f"{'method' if sdk.called else 'field'} of Exposure in {sdk.source}"
                f" ({', '.join(sorted(members))})"
            )
        elif sdk.called and not called:
            problems.append(
                f"{where} ({block.page}:{line}): {spelled} does not call {member}();"
                f" {sdk.name}'s Exposure has methods, not fields"
            )
    return problems


# ---------------------------------------------------------------------------
# The hub page
# ---------------------------------------------------------------------------
def test_the_hub_page_has_exactly_five_examples_one_per_tool():
    """Five tools, one browser JavaScript example each, and no other block."""
    shape = [(b.h2, b.h3, b.info) for b in _blocks(HUB)]
    assert shape == [("Examples", tool, "js") for tool in TOOLS], (
        f"{HUB} has {len(shape)} code blocks; it shows exactly {len(TOOLS)} examples,"
        f" one js block under each of {TOOLS} in Examples: {shape}"
    )


def test_the_hub_page_links_exactly_these_seven_sdk_pages():
    """The hub's table links each SDK page's section; an SDK added there is
    added here, or its examples go unchecked."""
    linked = set(re.findall(r"\]\(([\w-]+\.md)#exposure-hook\)", _text(HUB)))
    assert {f"docs/sdk/{page}" for page in linked} == set(SDK_PAGES)


def test_the_hub_examples_read_only_fields_of_each_javascript_exposure():
    problems = []
    for number, block in enumerate(_blocks(HUB), 1):
        where = f"JS example {number} ({block.h3})"
        if not _uses(block):
            problems.append(
                f"{where} reads no exposure field: nothing here would check it"
            )
        for sdk in HUB_SDKS:
            problems.extend(_problems(block, sdk, where))
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# The SDK pages
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("page", sorted(SDK_PAGES))
def test_an_sdk_page_that_names_its_hook_has_one_exposure_hook_section(page):
    _, option = SDK_PAGES[page]
    sections = _h2s(page).count("Exposure hook")
    if re.search(r"(?<![\w$])" + re.escape(option) + r"(?![\w$])", _text(page)):
        assert sections == 1, (
            f"{page} names {option} but has {sections} '## Exposure hook' sections;"
            " its hook examples must sit under exactly one"
        )
    else:
        assert sections == 0, (
            f"{page} has an Exposure hook section but never names {option}"
        )


@pytest.mark.parametrize("page", sorted(SDK_PAGES))
def test_an_sdk_pages_hook_examples_read_only_fields_of_its_exposure(page):
    sdk, _ = SDK_PAGES[page]
    blocks = [b for b in _blocks(page) if b.h2 == "Exposure hook"]
    if "Exposure hook" not in _h2s(page):
        assert not blocks
        return  # no hook text on this page yet: the test above holds that
    assert blocks, f"{page}'s Exposure hook section has no code block"
    problems = []
    for block in blocks:
        if block.info in ("python", "py"):
            try:
                ast.parse(block.code)
            except SyntaxError as error:
                problems.append(
                    f"{page}:{block.line + (error.lineno or 1) - 1}: the Python block does"
                    f" not parse: {error.msg}"
                )
        problems.extend(_problems(block, sdk, f"{sdk.name} hook example"))
    assert any(_uses(b) for b in blocks), (
        f"{page}'s Exposure hook blocks read no exposure field: nothing here would check them"
    )
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# The readers, on samples
# ---------------------------------------------------------------------------
def test_the_readers_find_what_they_exist_for():
    assert _ts_fields(
        "export interface Exposure {\n  /** a: b */\n  readonly x: string;\n"
        "  y?: number | null; // z: w\n}\n"
    ) == {"x", "y"}
    assert _python_fields(
        "@dataclass\nclass Exposure:\n    '''d'''\n    a: str\n    b: Optional[str]\n"
        "    def f(self): pass\n"
    ) == {"a", "b"}
    assert _go_fields(
        'type Exposure struct {\n\t// A is: it\n\tA, B string\n\tC int `json:"c"`\n}\n'
    ) == {"A", "B", "C"}
    assert _java_methods(
        "public final class Exposure {\n  private final String a;\n"
        "  public Exposure(String a) {}\n  public String getA() {}\n"
        "  public boolean equals(Object o) {}\n}\n"
    ) == {"getA"}
    assert _csharp_properties(
        "public sealed record Exposure(string A, string? B);"
    ) == {
        "A",
        "B",
    }
    sample = Block(
        HUB,
        10,
        "js",
        "exposures.x; // e.g. y\nexposure.a, e?.B, ue.c, x.e.d, e.m(), exposure.variantName",
        None,
        None,
    )
    assert _uses(sample) == [
        (11, "exposure", "a", False),
        (11, "e", "B", False),
        (11, "e", "m", True),
        (11, "exposure", "variantName", False),
    ]
    h2s, fenced = _scan(
        HUB, "## A\n```python\n## not a heading\n```\n### B\n~~~go\nx\n~~~\n## C\n"
    )
    assert h2s == ["A", "C"]
    assert [(b.line, b.info, b.code, b.h2, b.h3) for b in fenced] == [
        (3, "python", "## not a heading", "A", None),
        (7, "go", "x", "A", "B"),
    ]
