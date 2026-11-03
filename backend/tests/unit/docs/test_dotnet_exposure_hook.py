"""The .NET SDK's exposure hook (#1499): what its pages say is held by a test.

docs/sdk/dotnet.md and sdk/dotnet/README.md are in ``CLAIMS_DEFERRED``
(scripts/doc_claims.py), so no claims registry binds their sentences. This
does, for the "Exposure hook" text: each sentence the pages state about
``OnExposure`` (folded to one line, as written), the pages that state it, and
the xUnit tests in sdk/dotnet/tests/Controlvar.Tests/ExposureTests.cs that
fail when it stops being true. Sentences of advice ("make it safe for that")
and the hedged thread sentence ("usually on a thread-pool thread; do not
assume a UI thread", which no test asserts) are left unbound on purpose.

It also holds the section's place (one H2 "Exposure hook", in no Quick Start,
linked absolutely from the packaged README) and the refusal's words, which
the page, the source and the tests must spell alike.

Reads files only: no git and no dotnet, so it runs the same in
``scripts/core_build.sh``'s copy.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = "docs/sdk/dotnet.md"
README = "sdk/dotnet/README.md"
TESTS = REPO_ROOT / "sdk" / "dotnet" / "tests" / "Controlvar.Tests" / "ExposureTests.cs"
SOURCE = REPO_ROOT / "sdk" / "dotnet" / "src" / "Controlvar" / "ExposureHook.cs"

#: UX-D4's refusal, as the source throws it and the tests expect it.
REFUSAL = (
    "OnExposure must not be async: an async lambda or method runs as async void, and an "
    "exception after its first await would end the process. Write the exposure to a queue "
    "instead, such as a Channel."
)

CALLED_ONCE = (
    "IsCalledOnceForAnAssignedTrueAnswerWithTheFourValues",
    "IsNotCalledForACachedAnswerHoweverOftenItIsAskedFor",
)
NOT_CALLED = (
    "IsNotCalledForACachedAnswerHoweverOftenItIsAskedFor",
    "IsNotCalledForAnAssignedFalseAnswerAndTheAnswerIsUnchanged",
    "IsNotCalledWhenTheCallFails",
    "IsNeverCalledForAFlag",
)
NEVER_UNRECORDED = (
    "IsNotCalledForAnAssignedFalseAnswerAndTheAnswerIsUnchanged",
    "IsNotCalledWhenAssignedIsNotTheLiteralTrueAndTheAnswerIsStillReturned",
)
REFUSED = (
    "AnAsyncLambdaIsRefusedWhenOnExposureIsSet",
    "AnAsyncMethodIsRefusedWhenOnExposureIsSet",
    "AnAsyncFunctionCombinedWithOthersIsRefused",
)

#: (sentence as written, folded to one line; the pages that state it; the
#: xUnit tests that hold it).
SENTENCES: Tuple[Tuple[str, Tuple[str, ...], Tuple[str, ...]], ...] = (
    (
        "The client calls it once each time the server enrols the user in an experiment and the"
        " answer did not come from the client's cache: the moment the server records the"
        " exposure.",
        (PAGE,),
        CALLED_ONCE,
    ),
    (
        "is a function the client calls once each time the server enrols the user in an"
        " experiment and the answer did not come from the client's cache: the moment the server"
        " records the exposure.",
        (README,),
        CALLED_ONCE,
    ),
    (
        "Called once each time the server enrols the user and the answer is not cached",
        (PAGE,),
        CALLED_ONCE,
    ),
    (
        "OnExposure = e => exposures.Writer.TryWrite(e), // never waits: drops the exposure when"
        " the channel is full",
        (PAGE, README),
        ("TheChannelExampleOnTheDocsPageReceivesTheExposure",),
    ),
    (
        "It receives an `Exposure`, a record in `Controlvar.Models`, new for each call:",
        (PAGE,),
        (
            "IsCalledOnceForAnAssignedTrueAnswerWithTheFourValues",
            "ReceivesANewObjectEachTime",
        ),
    ),
    (
        "| `VariantId` | `string?` | The variant's id, `null` when the server sends none |",
        (PAGE,),
        ("PassesANullVariantIdWhenTheServerSendsNone",),
    ),
    (
        "Nothing else: not the user's attributes and not the variant's configuration. Its"
        " positional order is `Assignment`'s: `new Exposure(experimentKey, userId, variantId,"
        " variantName)`.",
        (PAGE,),
        (
            "HasExactlyFourInitOnlyPublicProperties",
            "ThePositionalOrderIsTheAssignmentOrder",
        ),
    ),
    (
        "It receives an `Exposure` record (`Controlvar.Models`): `ExperimentKey`, `UserId`,"
        " `VariantId` (`null` when the server sends none) and `VariantName`.",
        (README,),
        (
            "IsCalledOnceForAnAssignedTrueAnswerWithTheFourValues",
            "PassesANullVariantIdWhenTheServerSendsNone",
        ),
    ),
    (
        "It is not called for: - an answer from the client's cache; - a user the experiment did"
        ' not enrol (`assigned: false`, with `reason` `"holdout"`, `"mutual_exclusion"` or'
        ' `"targeting"`), or an experiment that is not running (`"draft"`, `"paused"`,'
        ' `"completed"` or `"archived"`): the server records no exposure for them, and the client'
        " returns the control variant; - a call that fails, including one that times out after"
        " the server recorded the exposure; - a feature flag.",
        (PAGE,),
        NOT_CALLED,
    ),
    (
        "It is not called for a cached answer, a user the experiment did not enrol or an"
        " experiment that is not running (`assigned: false`), a call that fails, or a feature"
        " flag.",
        (README,),
        NOT_CALLED,
    ),
    (
        "This SDK's `Assignment` does not say whether the user was enrolled; `OnExposure` is not"
        " called for a user who was not. It is not called when the server does not send"
        " `assigned`; every Controlvar server release sends it.",
        (PAGE,),
        NEVER_UNRECORDED,
    ),
    (
        "It is called again, and the server records another exposure, after the cache lifetime"
        " (`CacheTtlSeconds`, 300 by default), after `ClearCache()`, after the entry is evicted"
        " from the cache (`MaxCacheSize`, 1000 entries per cache by default), and in every new"
        " client, such as each process on a server.",
        (PAGE,),
        (
            "IsCalledAgainAfterTheCacheLifetime",
            "IsCalledAgainAfterClearCache",
            "IsCalledAgainAfterEvictionFromAFullCache",
            "IsCalledAgainInANewClient",
        ),
    ),
    (
        "It never receives one the server did not record.",
        (PAGE,),
        NEVER_UNRECORDED,
    ),
    (
        "It runs inside `GetAssignmentAsync`, after the answer is cached and before its `Task`"
        " completes,",
        (PAGE,),
        (
            "RunsAfterTheAnswerIsCachedSoTheSameKeyIsAnsweredFromTheCache",
            "HoldsNoLockOfTheClientAndRunsInsideTheCall",
        ),
    ),
    (
        "Its running time is added to that call, with no timeout. Calls for other users and"
        " experiments are not held up.",
        (PAGE,),
        ("HoldsNoLockOfTheClientAndRunsInsideTheCall",),
    ),
    (
        "Two calls for the same new user and experiment at the same moment send two requests,"
        " and the server records two exposures; `OnExposure` is called once.",
        (PAGE,),
        ("ConcurrentFirstCallsMakeTwoRequestsAndOneCall",),
    ),
    (
        "If it throws, the client catches the exception and reports nothing: `GetAssignmentAsync`"
        " returns the answer, and a failing function looks like one that is never called.",
        (PAGE,),
        ("AThrowingFunctionLeavesTheAnswerUnchangedAndCached",),
    ),
    (
        "When several functions are combined into one delegate (`+=`), each is called in turn,"
        " and one that throws does not stop the next.",
        (PAGE,),
        ("EachCombinedFunctionIsCalledAndOneThatThrowsDoesNotStopTheNext",),
    ),
    (
        "An async lambda or `async` method is refused with `ArgumentException` when you set"
        " `OnExposure`, alone or combined with other functions: it would run as `async void`,"
        " and an exception after its first `await` would end the process.",
        (PAGE,),
        REFUSED,
    ),
    (
        "A synchronous lambda that itself calls an `async void` method is not detected."
        " `OnExposure = null` sets none.",
        (PAGE,),
        (
            "ASynchronousLambdaThatCallsAnAsyncVoidMethodIsNotDetected",
            "SynchronousFunctionsAndNullAreAccepted",
        ),
    ),
    (
        "In a test, the `/assign` answer your `HttpMessageHandler` returns must include"
        ' `"assigned": true`, or `OnExposure` is not called.',
        (PAGE,),
        ("IsNotCalledWhenAssignedIsNotTheLiteralTrueAndTheAnswerIsStillReturned",),
    ),
    (
        "It runs inside `GetAssignmentAsync`, before its `Task` completes, usually on a"
        " thread-pool thread, so keep it quick. If it throws, the answer is unchanged and"
        " nothing reports it. An async lambda or `async` method is refused with"
        " `ArgumentException`.",
        (README,),
        (
            "HoldsNoLockOfTheClientAndRunsInsideTheCall",
            "AThrowingFunctionLeavesTheAnswerUnchangedAndCached",
            "AnAsyncLambdaIsRefusedWhenOnExposureIsSet",
            "AnAsyncMethodIsRefusedWhenOnExposureIsSet",
        ),
    ),
)


def fold(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def text(page: str) -> str:
    return (REPO_ROOT / page).read_text(encoding="utf-8")


def xunit_tests() -> List[str]:
    """The names of the public test methods in ExposureTests.cs."""
    return re.findall(
        r"^\s{0,8}public\s+(?:async\s+)?(?:Task|void)\s+(\w{1,200})\s*\(",
        TESTS.read_text(encoding="utf-8"),
        re.M,
    )


def h2s(page: str) -> List[str]:
    """The level-2 headings of a page, outside fenced blocks."""
    without_fences = re.sub(r"^```.*?^```[ \t]*$", "", text(page), flags=re.S | re.M)
    return re.findall(r"^##[ \t]+(.+?)[ \t]*$", without_fences, re.M)


def h2_section(page: str, heading: str) -> str:
    """The text of the H2 section ``heading``, its code blocks included: to the
    next line that starts with ``## `` (no fence on these pages holds one)."""
    found = re.search(
        r"^##[ \t]+" + re.escape(heading) + r"[ \t]*$(.*?)(?=^##[ \t]|\Z)",
        text(page),
        re.M | re.S,
    )
    assert found, f"{page} has no section {heading!r}"
    return found.group(1)


def csharp_string(source: str, name: str) -> str:
    """The value of the C# constant ``name``: its string literals, joined."""
    found = re.search(name + r"\s*=\s*((?:\s*\"[^\"\n]*\"\s*\+?)+)\s*;", source)
    assert found, f"no constant {name}"
    return "".join(re.findall(r"\"([^\"\n]*)\"", found.group(1)))


def test_both_pages_give_the_exposure_hook_a_section_of_its_own():
    """H-M9: one H2 "Exposure hook" (``#exposure-hook``) on each page, never
    in a Quick Start; the packaged README links the page's section by an
    absolute link (T230's I-README)."""
    for page in (PAGE, README):
        headings = h2s(page)
        assert headings.count("Exposure hook") == 1, (page, headings)
        # Journeys run the Quick Start as written (UX-H4): the hook stays out of it.
        quick_start = h2_section(page, "Quick Start")
        assert "OnExposure" not in quick_start and "Exposure" not in quick_start, page
    assert (
        "https://github.com/Controlvar/controlvar/blob/main/docs/sdk/dotnet.md#exposure-hook"
        in text(README)
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


def test_the_docs_example_is_the_code_a_test_compiles():
    """The page's channel line is in the test that runs it, so CI compiles
    and runs the example on every leg."""
    line = "OnExposure = e => exposures.Writer.TryWrite(e),"
    for page in (PAGE, README):
        assert line in text(page), page
    assert line in TESTS.read_text(encoding="utf-8")


def test_the_refusal_is_spelled_alike_in_the_source_and_the_tests():
    """UX-D4's words: the constant the init accessor throws and the text the
    tests expect are the same, and the source passes the property's name."""
    source = SOURCE.read_text(encoding="utf-8")
    assert csharp_string(source, "AsyncRefused") == REFUSAL
    assert csharp_string(TESTS.read_text(encoding="utf-8"), "AsyncRefusal") == REFUSAL
    config = (REPO_ROOT / "sdk/dotnet/src/Controlvar/SdkConfig.cs").read_text(
        encoding="utf-8"
    )
    assert "ExposureHook.RefuseAsync(value, nameof(OnExposure))" in config


def test_the_sentence_reader_finds_what_it_exists_for():
    """The readers, on samples: a test name in each declaration form, and
    the constant reader on a split literal."""
    assert "IsCalledAgainAfterClearCache" in xunit_tests()
    assert "IsNotCalledForAnAssignedFalseAnswerAndTheAnswerIsUnchanged" in xunit_tests()
    assert "ThePositionalOrderIsTheAssignmentOrder" in xunit_tests()  # a void test
    assert csharp_string('const string X =\n    "a: " +\n    "b";', "X") == "a: b"
