"""Every typed getter an SDK defines is named in its page's "Flag values" section (#1512, #1810).

Each SDK that reads the value a string, number or json flag serves does it
with one reader per type, or one hook given the type. Their pages named none
of them until the SDK version that publishes them, so a reader of the docs site
could not find out that flag values can be read at all (EM1512-7).

This reads the readers from each SDK's source, not from a list written here,
so a reader added later fails until its page names it:

* JavaScript: every ``async get<Something>Value(`` method of
  ``ExperimentationClient`` in ``sdk/js/src/client.ts``;
* Python: every public ``get_<something>_value`` method of
  ``ExperimentationClient`` in ``sdk/python/controlvar/client.py``, found by
  parsing the module;
* React and React Native: every ``use<Something>Value`` hook the package's
  entry point (``src/index.ts``) exports;
* React Native: every ``async get<Something>Value(`` method of
  ``ExperimentationClient`` in ``sdk/react-native/src/client.ts``;
* Java: every public ``get<Something>Value(`` method in
  ``ExperimentationClient.java``;
* .NET: every public ``Get<Something>ValueAsync(`` method in
  ``ExperimentationClient.cs``.

Each must be named in backticks in the page's ``## Flag values`` section (the
API table elsewhere on the page does not count: the section is where the
getters are explained). The readers each SDK has today are a positive
control, so a pattern that found nothing cannot pass. ``READERS`` is the one
list of these scans; a check that needs to know which SDKs read a value reads
it rather than scanning again (EM1810-4).

Reads files only: no git and no import of the SDKs, so it runs the same in
``scripts/core_build.sh``'s copy. It is in the docs-only gate's "Docs content
tests" through its directory, ``backend/tests/unit/docs/``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Callable, List, Set, Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
SDK = REPO_ROOT / "sdk"
DOCS = REPO_ROOT / "docs" / "sdk"
JS_CLIENT = SDK / "js" / "src" / "client.ts"
PY_CLIENT = SDK / "python" / "controlvar" / "client.py"
REACT_INDEX = SDK / "react" / "src" / "index.ts"
RN_INDEX = SDK / "react-native" / "src" / "index.ts"
RN_CLIENT = SDK / "react-native" / "src" / "client.ts"
JAVA_CLIENT = (
    SDK / "java" / "core" / "src" / "main" / "java" / "com" / "controlvar" / "sdk"
) / "ExperimentationClient.java"
DOTNET_CLIENT = SDK / "dotnet" / "src" / "Controlvar" / "ExperimentationClient.cs"
JS_PAGE = DOCS / "javascript.md"
PY_PAGE = DOCS / "python.md"
REACT_PAGE = DOCS / "react.md"
RN_PAGE = DOCS / "react-native.md"
JAVA_PAGE = DOCS / "java.md"
DOTNET_PAGE = DOCS / "dotnet.md"

SECTION = "Flag values"

#: The getters each SDK has today: the scan must find at least these.
JS_KNOWN = {"getStringValue", "getNumberValue", "getJsonValue"}
PY_KNOWN = {"get_string_value", "get_number_value", "get_json_value"}
REACT_KNOWN = {"useFlagValue"}
RN_HOOK_KNOWN = {"useFlagValue"}
RN_KNOWN = {"getStringValue", "getNumberValue", "getJsonValue"}
JAVA_KNOWN = {"getStringValue", "getNumberValue", "getJsonValue"}
DOTNET_KNOWN = {"GetStringValueAsync", "GetNumberValueAsync", "GetJsonValueAsync"}

#: A public typed getter of a TypeScript client: ``async getJsonValue<T>(``.
JS_GETTER = re.compile(r"^\s+async (get[A-Z][A-Za-z]*Value)\s*(?:<[^>\n]*>)?\(", re.M)
#: A public typed getter of the Python client.
PY_GETTER = re.compile(r"^get_[a-z]+_value$")
#: The names an entry point re-exports: ``export { useFlagValue } from '...';``
#: (``export type { ... }`` exports no value, so it is not read).
EXPORT = re.compile(r"^export \{([^}]*)\} from ", re.M)
#: A value hook: ``useFlagValue``.
VALUE_HOOK = re.compile(r"^use[A-Z][A-Za-z]*Value$")
#: A public typed getter of the Java client: ``public String getStringValue(``.
#: ``FlagEvaluation.getValue()`` has no type in its name, and is in another file.
JAVA_GETTER = re.compile(
    r"^[ \t]+public\b[^\n(=;]*?\s(get[A-Z][A-Za-z]*Value)\s*\(", re.M
)
#: A public typed getter of the .NET client: ``public async Task<string> GetStringValueAsync(``.
DOTNET_GETTER = re.compile(
    r"^[ \t]+public\b[^\n(=;]*?\s(Get[A-Z][A-Za-z]*ValueAsync)\s*\(", re.M
)


def js_getters(source: str) -> Set[str]:
    """The typed getters of ``class ExperimentationClient`` in a TypeScript client
    (sdk/js/src/client.ts, sdk/react-native/src/client.ts)."""
    start = re.search(r"^export class ExperimentationClient\b", source, re.M)
    assert start, "the source has no exported ExperimentationClient class"
    # The class runs to the next top-level declaration or the end of the file.
    rest = source[start.end() :]
    following = re.search(
        r"^(?:export )?(?:class|function|const|interface|type) ", rest, re.M
    )
    body = rest if following is None else rest[: following.start()]
    return set(JS_GETTER.findall(body))


def py_getters(source: str) -> Set[str]:
    """The public typed getters of ``class ExperimentationClient`` in client.py."""
    tree = ast.parse(source)
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ExperimentationClient"
    ]
    assert len(classes) == 1, (
        f"{PY_CLIENT.relative_to(REPO_ROOT)}: no single ExperimentationClient"
    )
    return {
        node.name
        for node in classes[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and PY_GETTER.match(node.name)
    }


def value_hooks(source: str) -> Set[str]:
    """The ``use<Something>Value`` hooks an entry point (src/index.ts) exports."""
    names = set()
    for exported in EXPORT.findall(source):
        for name in exported.split(","):
            name = name.split(" as ")[-1].strip()
            if VALUE_HOOK.match(name):
                names.add(name)
    return names


def java_getters(source: str) -> Set[str]:
    """The public typed getters in ExperimentationClient.java."""
    return set(JAVA_GETTER.findall(source))


def dotnet_getters(source: str) -> Set[str]:
    """The public typed getters in ExperimentationClient.cs."""
    return set(DOTNET_GETTER.findall(source))


Scan = Callable[[str], Set[str]]

#: Every SDK that reads a flag's value: its name, the source its readers are
#: scanned from, its page, the scan, and the readers it has today. React Native
#: has two rows, its hook and its client's getters, on the one page.
READERS: Tuple[Tuple[str, Path, Path, Scan, Set[str]], ...] = (
    ("javascript", JS_CLIENT, JS_PAGE, js_getters, JS_KNOWN),
    ("python", PY_CLIENT, PY_PAGE, py_getters, PY_KNOWN),
    ("react", REACT_INDEX, REACT_PAGE, value_hooks, REACT_KNOWN),
    ("react-native-hook", RN_INDEX, RN_PAGE, value_hooks, RN_HOOK_KNOWN),
    ("react-native-client", RN_CLIENT, RN_PAGE, js_getters, RN_KNOWN),
    ("java", JAVA_CLIENT, JAVA_PAGE, java_getters, JAVA_KNOWN),
    ("dotnet", DOTNET_CLIENT, DOTNET_PAGE, dotnet_getters, DOTNET_KNOWN),
)
READER_IDS = [reader[0] for reader in READERS]


def rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def section(text: str) -> str:
    """The ``## Flag values`` section of a page: its heading to the next ``#`` or ``##`` heading."""
    heading = re.search(rf"^## {re.escape(SECTION)}[ \t]*$", text, re.M)
    if heading is None:
        return ""
    rest = text[heading.end() :]
    following = re.search(r"^#{1,2} ", rest, re.M)
    return rest if following is None else rest[: following.start()]


def missing(names: Set[str], page_text: str) -> List[str]:
    """The names not given in backticks in the page's Flag values section."""
    body = section(page_text)
    return sorted(name for name in names if f"`{name}`" not in body)


@pytest.mark.parametrize("name, source, page, scan, known", READERS, ids=READER_IDS)
def test_the_scan_finds_the_getters_each_sdk_has(name, source, page, scan, known):
    found = scan(source.read_text(encoding="utf-8"))
    assert known <= found, (
        f"{rel(source)}: the {name} scan found {sorted(found)}, "
        f"which lacks {sorted(known - found)}"
    )


@pytest.mark.parametrize("name, source, page, scan, known", READERS, ids=READER_IDS)
def test_every_typed_getter_is_named_in_the_flag_values_section(
    name, source, page, scan, known
):
    getters = scan(source.read_text(encoding="utf-8"))
    text = page.read_text(encoding="utf-8")
    assert section(text), f"{rel(page)} has no '## {SECTION}' section"
    absent = missing(getters, text)
    assert not absent, (
        f"{rel(page)}'s {SECTION!r} section does not name "
        f"{absent}, which {rel(source)} defines"
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "## Flag values\n\nUse `getStringValue`.\n\n## Next\n\n`getNumberValue`\n",
            ["getNumberValue"],
        ),
        (
            "## API\n\n`getStringValue` `getNumberValue`\n",
            ["getNumberValue", "getStringValue"],
        ),
        (
            "## Flag values\n\ngetStringValue and getNumberValue\n",
            ["getNumberValue", "getStringValue"],
        ),
        ("## Flag values\n\n### Sub\n\n`getStringValue`, `getNumberValue`\n", []),
    ],
    ids=[
        "only-in-the-section",
        "no-section",
        "not-in-backticks",
        "a-subheading-stays-inside",
    ],
)
def test_a_getter_outside_the_section_or_without_backticks_is_missing(text, expected):
    assert missing({"getStringValue", "getNumberValue"}, text) == expected


def test_the_javascript_scan_reads_only_the_client_class():
    source = (
        "export class ExperimentationClient {\n"
        "  async getStringValue(flagKey: string): Promise<string> {}\n"
        "  async getJsonValue<T = unknown>(flagKey: string): Promise<T> {}\n"
        "  private async typedValue(flagKey: string) {}\n"
        "}\n"
        "export class Other {\n"
        "  async getOtherValue(): Promise<number> {}\n"
        "}\n"
    )
    assert js_getters(source) == {"getStringValue", "getJsonValue"}


def test_the_hook_scan_reads_only_the_value_hooks_the_entry_point_exports():
    source = (
        "export { useFeatureFlag } from './hooks/useFeatureFlag';\n"
        "export { useFlagValue } from './hooks/useFlagValue';\n"
        "export type { FlagValueResult } from './hooks/useFlagValue';\n"
        "export { FlagValueError, useInner as useOtherValue } from './client';\n"
        "// export { useCommentedValue } from './hooks/useCommentedValue';\n"
        "import { useImportedValue } from './hooks/useImportedValue';\n"
    )
    assert value_hooks(source) == {"useFlagValue", "useOtherValue"}


def test_the_java_scan_reads_only_public_typed_getters():
    source = (
        "public class ExperimentationClient implements AutoCloseable {\n"
        "    /** See {@link #getNumberValue}. */\n"
        "    public String getStringValue(User user, String flagKey, String defaultValue) {\n"
        "    public Object getJsonValue(User user, String flagKey, Object defaultValue) {\n"
        "    private Object typedValue(User user, String flagKey, String type) {\n"
        "    public Object getValue() {\n"
        "        Object value = getNumberValue(user, flagKey, 1);\n"
        "}\n"
    )
    assert java_getters(source) == {"getStringValue", "getJsonValue"}


def test_the_dotnet_scan_reads_only_public_typed_getters():
    source = (
        "public class ExperimentationClient : IDisposable\n"
        "{\n"
        '    /// <see cref="GetNumberValueAsync"/>\n'
        "    public async Task<string> GetStringValueAsync(\n"
        "    public Task<JsonElement> GetJsonValueAsync(string flagKey, string userId)\n"
        "    private async Task<JsonElement?> TypedValueAsync(\n"
        "        var value = await GetNumberValueAsync(flagKey, userId, 1);\n"
        "}\n"
    )
    assert dotnet_getters(source) == {"GetStringValueAsync", "GetJsonValueAsync"}
