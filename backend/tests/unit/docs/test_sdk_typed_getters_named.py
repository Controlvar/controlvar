"""Every typed getter an SDK defines is named in its page's "Flag values" section (#1512).

The JavaScript and Python SDKs read the value a string, number or json flag
serves with one getter per type: ``getStringValue`` / ``getNumberValue`` /
``getJsonValue`` in ``sdk/js/src/client.ts`` and ``get_string_value`` /
``get_number_value`` / ``get_json_value`` in ``sdk/python/controlvar/client.py``.
Their pages, ``docs/sdk/javascript.md`` and ``docs/sdk/python.md``, named none
of them until the SDK version that publishes them, so a reader of the docs site
could not find out that flag values can be read at all (EM1512-7).

This reads the getters from each SDK's source, not from a list written here, so
a getter added later fails until its page names it:

* JavaScript: every ``async get<Something>Value(`` method of
  ``ExperimentationClient``;
* Python: every public ``get_<something>_value`` method of
  ``ExperimentationClient``, found by parsing the module.

Each must be named in backticks in the page's ``## Flag values`` section (the
API table elsewhere on the page does not count: the section is where the
getters are explained). The three getters each SDK has today are a positive
control, so a pattern that found nothing cannot pass.

Reads files only: no git and no import of the SDKs, so it runs the same in
``scripts/core_build.sh``'s copy. It is in the docs-only gate's "Docs content
tests" through its directory, ``backend/tests/unit/docs/``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import List, Set

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
JS_CLIENT = REPO_ROOT / "sdk" / "js" / "src" / "client.ts"
PY_CLIENT = REPO_ROOT / "sdk" / "python" / "controlvar" / "client.py"
JS_PAGE = REPO_ROOT / "docs" / "sdk" / "javascript.md"
PY_PAGE = REPO_ROOT / "docs" / "sdk" / "python.md"

SECTION = "Flag values"

#: The getters each SDK has today: the scan must find at least these.
JS_KNOWN = {"getStringValue", "getNumberValue", "getJsonValue"}
PY_KNOWN = {"get_string_value", "get_number_value", "get_json_value"}

#: A public typed getter of the JavaScript client: ``async getJsonValue<T>(``.
JS_GETTER = re.compile(r"^\s+async (get[A-Z][A-Za-z]*Value)\s*(?:<[^>\n]*>)?\(", re.M)
#: A public typed getter of the Python client.
PY_GETTER = re.compile(r"^get_[a-z]+_value$")


def js_getters(source: str) -> Set[str]:
    """The typed getters of ``class ExperimentationClient`` in client.ts."""
    start = re.search(r"^export class ExperimentationClient\b", source, re.M)
    assert start, (
        f"{JS_CLIENT.relative_to(REPO_ROOT)} has no ExperimentationClient class"
    )
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


def test_the_scan_finds_the_getters_each_sdk_has():
    assert JS_KNOWN <= js_getters(JS_CLIENT.read_text(encoding="utf-8"))
    assert PY_KNOWN <= py_getters(PY_CLIENT.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "client, page, scan",
    [
        (JS_CLIENT, JS_PAGE, js_getters),
        (PY_CLIENT, PY_PAGE, py_getters),
    ],
    ids=["docs/sdk/javascript.md", "docs/sdk/python.md"],
)
def test_every_typed_getter_is_named_in_the_flag_values_section(client, page, scan):
    getters = scan(client.read_text(encoding="utf-8"))
    text = page.read_text(encoding="utf-8")
    assert section(text), f"{page.relative_to(REPO_ROOT)} has no '## {SECTION}' section"
    absent = missing(getters, text)
    assert not absent, (
        f"{page.relative_to(REPO_ROOT)}'s {SECTION!r} section does not name "
        f"{absent}, which {client.relative_to(REPO_ROOT)} defines"
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
