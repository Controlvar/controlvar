"""The .NET SDK sends a flag evaluation's attributes as ``context`` (#1719):
what its pages say about it is held by a test.

docs/sdk/dotnet.md and sdk/dotnet/README.md are in ``CLAIMS_DEFERRED``
(scripts/doc_claims.py), so no claims registry binds their sentences. This
does, as test_dotnet_exposure_hook.py does for the exposure hook: each sentence
the pages state about the evaluate request's ``context`` and the cache key
(folded to one line, as written), the pages that state it, and the xUnit tests
in sdk/dotnet/tests/Controlvar.Tests/ExperimentationClientTests.cs that fail
when it stops being true.

It also holds the exact URL that test expects for an attribute value with
``+``, ``&``, ``=``, ``#``, a space and a non-ASCII letter to the server's
reading of it: the query parsed as Starlette parses a query string
(``urllib.parse.parse_qsl``) and the ``context`` value decoded as the evaluate
route decodes it (``json.loads``) give back the attributes the C# test passes.
So the expected URL cannot be edited to match an SDK that stopped sending what
the server reads back.

Reads files only: no git and no dotnet, so it runs the same in
``scripts/core_build.sh``'s copy.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path
from typing import List, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = "docs/sdk/dotnet.md"
README = "sdk/dotnet/README.md"
TESTS = (
    REPO_ROOT
    / "sdk"
    / "dotnet"
    / "tests"
    / "Controlvar.Tests"
    / "ExperimentationClientTests.cs"
)

EXACT_URL = "EvaluateFlagAsync_SendsAttributesAsUrlEncodedJsonContext"
NO_CONTEXT = "EvaluateFlagAsync_SendsNoContextForNullOrEmptyAttributes"
TYPES = "EvaluateFlagAsync_ContextCarriesStringsNumbersAndBooleansAsJson"
ENCODED = "EvaluateFlagAsync_SendsGetWithEncodedKeyUserIdQueryAndHeaders"
CACHE_KEY = "EvaluateFlagAsync_CacheKeyIgnoresAttributesUntilClearCache"

#: (sentence as written, folded to one line; the pages that state it; the
#: xUnit tests that hold it).
SENTENCES: Tuple[Tuple[str, Tuple[str, ...], Tuple[str, ...]], ...] = (
    (
        "Calls `GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=…&context=…` and returns a"
        " `FlagEvaluationResult`. `attributes` is sent as `context` on flag evaluation, so the"
        " flag's targeting rules are evaluated against them: compact JSON from the same"
        " `System.Text.Json` options as the assignment body (characters such as `+`, `&` and"
        " non-ASCII letters become `\\uXXXX` escapes), then percent-encoded, as the key and the"
        " user id are.",
        (PAGE,),
        (EXACT_URL, TYPES, ENCODED),
    ),
    (
        "With `null` or an empty dictionary no `context` is sent and the request carries the"
        " user id alone.",
        (PAGE,),
        (NO_CONTEXT, ENCODED),
    ),
    (
        "Attributes are assumed **stable per user**: an evaluation is cached by user + flag key"
        " only, so within `CacheTtlSeconds` a second call for the same user and flag returns the"
        " first answer, whatever its attributes. Call `ClearCache()` after changing a user's"
        " attributes.",
        (PAGE,),
        (CACHE_KEY,),
    ),
    (
        "| `EvaluateFlagAsync` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=…`"
        " | `context`: the attributes as URL-encoded JSON, only when there are any |",
        (PAGE, README),
        (EXACT_URL, NO_CONTEXT),
    ),
    (
        "`GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=…&context=…` (`attributes` sent as"
        " `context` on flag evaluation, as URL-encoded JSON; none sent when `null` or empty)",
        (README,),
        (EXACT_URL, NO_CONTEXT),
    ),
)


def fold(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def text(page: str) -> str:
    return (REPO_ROOT / page).read_text(encoding="utf-8")


def xunit_tests() -> List[str]:
    """The names of the public test methods in ExperimentationClientTests.cs."""
    return re.findall(
        r"^\s{0,8}public\s+(?:async\s+)?(?:Task|void)\s+(\w{1,200})\s*\(",
        TESTS.read_text(encoding="utf-8"),
        re.M,
    )


def csharp_literal(body: str) -> str:
    """A C# regular string literal's value: ``\\uXXXX``, ``\\"`` and ``\\\\``
    resolved (the only escapes the literals read here use)."""
    out = []
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        escape = body[index + 1]
        if escape == "u":
            out.append(chr(int(body[index + 2 : index + 6], 16)))
            index += 6
        elif escape in '"\\':
            out.append(escape)
            index += 2
        else:
            raise AssertionError(f"an escape this reader does not resolve: \\{escape}")
    return "".join(out)


def awkward_value() -> str:
    """The C# test's attribute value, the constant ``Awkward``."""
    found = re.search(
        r'private const string Awkward = "((?:[^"\\\n]|\\.){1,200})";',
        TESTS.read_text(encoding="utf-8"),
    )
    assert found, f"{TESTS.name} has no constant Awkward"
    return csharp_literal(found.group(1))


def expected_url() -> str:
    """The URL the exact-URL test expects: the string literals it joins with ``+``."""
    source = TESTS.read_text(encoding="utf-8")
    start = source.index(f"public async Task {EXACT_URL}(")
    end = source.find("[Fact]", start)
    method = source[start : end if end != -1 else len(source)]
    found = re.search(
        r'Assert\.Equal\(\s{0,40}((?:"[^"\n]{1,400}"\s{0,40}\+?\s{0,40}){1,8}),\s{0,40}handler\.Last\.Url\)',
        method,
    )
    assert found, f"{EXACT_URL} asserts no exact handler.Last.Url"
    return "".join(
        csharp_literal(literal)
        for literal in re.findall(r'"([^"\n]{1,400})"', found.group(1))
    )


@pytest.mark.parametrize(
    "sentence, pages, tests", SENTENCES, ids=[s[:60] for s, _, _ in SENTENCES]
)
def test_each_sentence_is_on_its_page_and_held_by_a_test(sentence, pages, tests):
    unsaid = [page for page in pages if sentence not in fold(text(page))]
    assert unsaid == [], f"no longer said there: {sentence}"
    names = set(xunit_tests())
    missing = [name for name in tests if name not in names]
    assert missing == [], f"no test in {TESTS.name} has these names"


def test_the_old_sentence_is_gone_from_both_pages():
    for page in (PAGE, README):
        folded = fold(text(page))
        assert "accepted for source compatibility and not sent" not in folded, page
        assert "`attributes` are not sent" not in folded, page


def test_the_exact_url_reads_back_as_the_attributes_the_test_passes():
    value = awkward_value()
    for char in "+&=# é":
        assert char in value, f"the attribute value lost {char!r}"
    url = expected_url()
    parts = urllib.parse.urlsplit(url)
    assert parts.path == "/api/v1/feature-flags/evaluate/new-ui"
    assert parts.fragment == ""
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    assert [name for name, _ in pairs] == ["user_id", "context"]
    assert pairs[0][1] == "user-1"
    assert json.loads(pairs[1][1]) == {"note": value}


def test_the_readers_find_what_they_exist_for():
    """The readers, on samples: the escapes the literals use, and the names."""
    assert csharp_literal('a\\u00e9\\"b\\\\') == 'aé"b\\'
    assert csharp_literal("%5Cu002B") == "%5Cu002B"
    assert awkward_value() == "a+b&c=d#e fé"
    assert expected_url().startswith(
        "http://test-api/api/v1/feature-flags/evaluate/new-ui?"
    )
    assert CACHE_KEY in xunit_tests()
