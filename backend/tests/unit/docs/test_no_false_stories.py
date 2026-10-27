"""Five false stories the docs told, refused in every in-scope document (#1142).

Each story was told on many pages at once, so it is swept everywhere rather
than fixed page by page:

1. **inactive-404.** A flag that exists but is not ACTIVE does not answer 404
   and is not an error: ``GET /api/v1/feature-flags/evaluate/{key}`` answers
   200 with ``enabled: false`` and ``reason: "inactive"``. Only an unknown key
   is a 404 (``_evaluate_flag_by_key`` in
   ``backend/app/api/v1/endpoints/feature_flags.py``). Experiments are not
   covered by these rules: ``POST /api/v1/tracking/assign`` answers an
   experiment that is not ACTIVE with its control variant and 404s only for an
   unknown key or a draft with no control variant, while
   ``POST /api/v1/tracking/assign/batch`` still answers 404 for any experiment
   that is not ACTIVE.
2. **config-payload.** The evaluate route returns ``"config": None`` for every
   flag, and the SDK ruleset carries no variants, so an SDK's flag ``config``
   is always null. There is no config payload to read, and the OpenFeature
   providers' string, number and object calls always return the caller's
   default: only boolean flags carry a value.
3. **aws-path.** No Lambda function serves a request or processes an event,
   the API writes nothing to Kinesis, and nothing writes to OpenSearch.
   Assignment, evaluation and event tracking are API calls, events are stored
   in PostgreSQL and results are computed from there.
4. **no-context.** The evaluate endpoint takes a targeting context: GET as the
   ``context`` query parameter, POST in the body. Where an SDK does not send
   the attributes, that is the SDK's gap, not the endpoint's.
5. **test-counts.** A hand-written number of tests (tests, XCTests, RSpec
   examples, "77 core + 30 starter", or a table with a Tests or Unit tests
   column) goes stale with the next test anyone adds. A page names the suite
   and where it runs instead.

Each rule is a pattern, why it is wrong, the path prefixes it is limited to
(``None``: every in-scope document) and a sample of the removed text it must
match, so a pattern that has silently stopped matching fails here instead of
reporting "clean" for ever.

In scope is what ``tests/acceptance/docs/inventory.toml`` says, read through
``test_docs_floor.in_scope_texts``: every nav page, every ``[documents]``
entry with ``scope = "in"`` and each named surface as its extractor reads it.
Registered pages and those still in ``CLAIMS_PENDING`` alike, and the whole
text: fenced blocks, HTML comments and front matter included.

This is a sweep, not a proof. A paraphrase of a story passes ("an archived
flag is refused with a not-found"); on a registered page the claims registry
catches the edited sentence, and review catches the rest.

``ALLOWED`` excuses a hit inside an exact text on one page, with the reason.
An entry whose text is no longer on its page, or no longer holds a hit, is
refused, so the list only shrinks.

Reads only files; no git, so it runs the same in ``scripts/core_build.sh``'s
copy. It is in the docs-only gate's "Docs content tests" through its
directory, ``backend/tests/unit/docs/``.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Tuple

import pytest

from backend.tests.unit.docs.test_docs_floor import in_scope_texts

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
INVENTORY = REPO_ROOT / "tests" / "acceptance" / "docs" / "inventory.toml"

#: Documents the sweep must read, named, so a wrong root or an empty inventory
#: fails instead of passing: a nav page, an in-scope [documents] file, a
#: packaged SDK README and the docs hub's surface.
SWEEP_FLOOR = (
    "docs/sdk/openfeature.md",
    "docs/architecture/overview.md",
    "README.md",
    "sdk/js/README.md",
    "sdk/python/README.md",
    "frontend/src/pages/docs/index.tsx#desc",
)

#: (story, pattern, why it is wrong, the path prefixes it is limited to or None,
#: a sample of the removed text the pattern must match).
Rule = Tuple[str, str, str, Optional[Tuple[str, ...]], str]

_NOT_ACTIVE = (
    "a flag that is not ACTIVE answers 200 with enabled: false and reason"
    ' "inactive"; only an unknown flag key is a 404'
)
_NULL_CONFIG = (
    "the evaluate route returns config: null for every flag, so there is no"
    " config payload, and string, number and object values are always the"
    " caller's default"
)
_AWS = (
    "no Lambda function serves a request or processes an event, the API writes"
    " nothing to Kinesis and nothing writes to OpenSearch: events are stored in"
    " PostgreSQL and results are computed from there"
)
_CONTEXT = (
    "the evaluate endpoint takes a context (GET: the context query parameter;"
    " POST: the body); say that the SDK does not send it"
)
_COUNT = "a hand-written test count goes stale; name the suite and where it runs"

RULES: Tuple[Rule, ...] = (
    # 1. inactive-404
    (
        "inactive-404",
        r"\b(?:flags?/experiments?|experiments?/flags?)\b[^.|;]{0,40}?\bnot\s+ACTIVE\b",
        _NOT_ACTIVE,
        None,
        "Errors: 401 bad key, 404 flag/experiment not ACTIVE (never cached), 422 invalid event.",
    ),
    (
        "inactive-404",
        r"\b404\b[^.|;]{0,40}?\bflag\b(?!/)[^.|;]{0,30}?\bnot\s+ACTIVE\b",
        _NOT_ACTIVE,
        None,
        "raises `ExperimentationError` (`status == 404` when the flag is not ACTIVE)",
    ),
    (
        "inactive-404",
        r"\b404\b[^.|;]{0,20}?\(not\s+ACTIVE\s*/\s*unknown\)[^.|;]{0,20}?\bevaluate",
        _NOT_ACTIVE,
        None,
        "- Network error, timeout, 401, 404 (not ACTIVE / unknown), 429: `evaluateFeatureFlag` and",
    ),
    (
        "inactive-404",
        r"(?i)\b(?:error|failure)\b[^.|;()]{0,30}?\bflag\s+(?:is\s+)?not\s+ACTIVE\b",
        _NOT_ACTIVE,
        None,
        "  {:error, _reason} -> render_default()   # network/HTTP failure, flag not ACTIVE",
    ),
    (
        "inactive-404",
        r"(?i)\bfailures?\s*\(\s*including\s+`?enabled:\s*false",
        _NOT_ACTIVE + "; it is an answer, not a failure",
        None,
        'On a network/HTTP failure (including `enabled: false`, `reason: "inactive"` when the flag is not ACTIVE; 404 only for an unknown key) a cached',
    ),
    # 2. config-payload
    (
        "config-payload",
        r"(?i)\bconfig`?\s+payload\b",
        _NULL_CONFIG,
        None,
        "| `config` | `mixed`, `null` | The flag's `config` payload as returned by the server |",
    ),
    (
        "config-payload",
        r"(?i)\bserver's\s+payload\b",
        _NULL_CONFIG,
        None,
        "// Config is the server's payload (any JSON) or null",
    ),
    (
        "config-payload",
        r"\bconfig(?:\.variant|\.value|\[[\"'](?:variant|value)[\"']\])",
        _NULL_CONFIG,
        None,
        "const variant = await client.getStringValue('checkout-experiment', 'control', ctx); // → config.variant",
    ),
    (
        "config-payload",
        r"`?\bconfig`?\s+(?:itself\s+)?when\s+(?:it\s+is\s+)?(?:an?\s+)?(?:non-null\s+|JSON\s+)?(?:object|dict|number|string|numeric)\b",
        _NULL_CONFIG,
        None,
        "| `getObjectValue` / `getObjectDetails` | `config` when it is a non-null object | default, reason `DEFAULT` |",
    ),
    (
        "config-payload",
        r"\b(?:flag|result)(?:\.|->)(?:[Cc]onfig(?:Map)?)\??(?:\.get\(|\.GetProperty\(|\[)",
        _NULL_CONFIG,
        None,
        "    $theme = $flag->config['theme'] ?? 'dark';",
    ),
    (
        "config-payload",
        r"\(config\s+as\s+\w+\)\??\.\w",
        _NULL_CONFIG,
        None,
        "return isEnabled ? <NewSearch engine={(config as any)?.engine} /> : <LegacySearch />;",
    ),
    # 3. aws-path (case-sensitive: the service names, not the word "lambda")
    (
        "aws-path",
        r"\bLambda\s+for\s+(?:low-latency|real-time|assignment|evaluation)",
        _AWS,
        None,
        "- **Real-time**: AWS Lambda for low-latency operations",
    ),
    (
        "aws-path",
        r"\bKinesis\b[^.|\n]{0,20}(?:→|->)\s*Lambda\b|\bLambda\b[^.|\n]{0,20}(?:→|->)\s*OpenSearch\b",
        _AWS,
        None,
        "- **Analytics**: Kinesis → Lambda → OpenSearch pipeline",
    ),
    (
        "aws-path",
        r"\bKinesis\b[^.|\n]{0,30}?(?:\|\s*)?(?i:event\s+(?:ingestion|collection)|data\s+streaming|millions\s+of\s+events)",
        _AWS,
        None,
        "| Kinesis Data Streams | Event ingestion |",
    ),
    (
        "aws-path",
        r"\bOpenSearch\b[^.|\n]{0,30}?(?i:analytics\s+(?:engine|queries)|real-time\s+analytics)",
        _AWS,
        None,
        "- **OpenSearch**: Real-time analytics queries",
    ),
    (
        "aws-path",
        r"(?i:significance\s+testing|analytics)\b[^.|\n]{0,20}?\busing\s+OpenSearch\b",
        _AWS,
        None,
        "-   **Real-time significance testing** using OpenSearch aggregations",
    ),
    (
        "aws-path",
        r"\bLambda\s+invocations?\s*\|[^|\n]{0,20}\bevents\b",
        _AWS,
        None,
        "| Lambda invocations | 1M events/month | ~$5 |",
    ),
    # 4. no-context
    (
        "no-context",
        r"(?i)\bevaluate\s+endpoint\s+(?:takes|has|accepts)\s+no\s+context\b",
        _CONTEXT,
        None,
        "**Not sent.** The evaluate endpoint takes no context; attributes stay available to your own hooks.",
    ),
    (
        "no-context",
        r"(?i)\bevaluate\s+endpoint\s+(?:only\s+takes|takes\s+(?:only|just))\s+(?:the\s+)?`?user[_ ]id\b",
        _CONTEXT,
        None,
        "`:attributes` is accepted for symmetry but the evaluate endpoint only takes the\nuser id.",
    ),
    # 5. test-counts
    (
        "test-counts",
        r"(?i)(?<![\w.,])\d[\d,]*\+?\s+(?:[a-z][\w-]*\s+){0,2}tests?\b",
        _COUNT,
        None,
        "`python -m pytest sdk/python/tests -q`: 182 tests, HTTP is faked",
    ),
    (
        "test-counts",
        r"(?<![\w.,])\d[\d,]*\+?\s+XCTests?\b",
        _COUNT,
        None,
        "- `swift test`: 71 XCTests; URLSession is intercepted with a URLProtocol mock",
    ),
    (
        "test-counts",
        r"(?i)(?<![\w.,])\d[\d,]*\+?\s+(?:[a-z][\w-]*\s+){0,2}examples\b",
        _COUNT + " (an RSpec example is a test)",
        None,
        "- `bundle exec rspec`: 109 examples (run here on Ruby 2.6.10); HTTP stubbed with WebMock",
    ),
    (
        "test-counts",
        r"(?i)\btests?\b[^.\n]{0,60}?\(\d[\d,]*\s+[a-z]+\s*\+\s*\d[\d,]*\s+[a-z]+\)",
        _COUNT,
        None,
        "Tests: `cd sdk/java && mvn test` (77 core + 30 starter).",
    ),
    (
        "test-counts",
        r"(?im)^\|[^\n]{0,200}?\|\s*(?:unit\s+)?tests\s*\|",
        _COUNT + " (a table with a Tests column is one)",
        None,
        "| SDK | Location | Unit tests | Verified live | Docs |",
    ),
)

#: (document, exact text, why the hit inside it stays for now).
ALLOWED: Tuple[Tuple[str, str, str], ...] = ()


def _inventory() -> Mapping[str, object]:
    return tomllib.loads(INVENTORY.read_text(encoding="utf-8"))


def _read(path: str) -> Optional[str]:
    file = REPO_ROOT / path
    return file.read_text(encoding="utf-8") if file.is_file() else None


def _texts() -> Dict[str, str]:
    texts = in_scope_texts(_inventory(), _read)
    missing = [doc for doc in SWEEP_FLOOR if doc not in texts]
    assert not missing, f"the sweep did not read {missing}: it is broken, not clean"
    return texts


def _applies(doc: str, only: Optional[Tuple[str, ...]]) -> bool:
    return only is None or doc.startswith(only)


def _allowed_spans(doc: str, text: str) -> List[Tuple[int, int]]:
    spans = []
    for path, allowed, _why in ALLOWED:
        if path != doc:
            continue
        start = text.find(allowed)
        while start != -1:
            spans.append((start, start + len(allowed)))
            start = text.find(allowed, start + 1)
    return spans


def hits(doc: str, text: str) -> Iterator[Tuple[str, int, str, str]]:
    """Each (story, offset, matched text, why) in *text*, allowed hits included."""
    for story, pattern, why, only, _sample in RULES:
        if not _applies(doc, only):
            continue
        for match in re.finditer(pattern, text):
            yield story, match.start(), match.group(0), why


def problems(texts: Mapping[str, str]) -> List[str]:
    """Every unexcused hit, and every allow-list entry that has gone stale."""
    found: List[str] = []
    for doc in sorted(texts):
        text = texts[doc]
        spans = _allowed_spans(doc, text)
        for story, start, matched, why in hits(doc, text):
            if any(lo <= start and start + len(matched) <= hi for lo, hi in spans):
                continue
            line = text.count("\n", 0, start) + 1
            found.append(f"{doc}:{line}: [{story}] {matched!r} -- {why}")
    for doc, allowed, _why in ALLOWED:
        text = texts.get(doc)
        if text is None or allowed not in text:
            found.append(f"{doc}: allowed text is no longer on the page: {allowed!r}")
        elif not any(list(hits(doc, allowed))):
            found.append(f"{doc}: allowed text no longer holds a hit: {allowed!r}")
    return found


def test_the_sweep_reads_every_in_scope_document() -> None:
    texts = _texts()
    assert len(texts) > len(SWEEP_FLOOR)
    assert all(texts.values()), [doc for doc, text in texts.items() if not text]


def test_no_in_scope_document_tells_a_false_story() -> None:
    found = problems(_texts())
    assert not found, (
        "\n".join(found)
        + "\n\nReword each to what the code does (the docstring of this test says"
        " what that is), or remove it."
    )


@pytest.mark.parametrize(
    ("pattern", "sample", "only"),
    [(rule[1], rule[4], rule[3]) for rule in RULES],
    ids=[f"{RULES[i][0]}-{i}" for i in range(len(RULES))],
)
def test_each_forbidden_pattern_matches_the_text_it_removed(
    pattern: str, sample: str, only: Optional[Tuple[str, ...]]
) -> None:
    assert re.search(pattern, sample), (
        f"{pattern!r} no longer matches the text it was written for"
    )
    if only is not None:
        for prefix in only:
            assert (REPO_ROOT / prefix).exists(), prefix


def test_every_story_has_a_rule() -> None:
    assert {rule[0] for rule in RULES} == {
        "inactive-404",
        "config-payload",
        "aws-path",
        "no-context",
        "test-counts",
    }


@pytest.mark.parametrize(
    "true_text",
    [
        '`{key, enabled, config}`; `enabled: false`, `reason: "inactive"` when the flag is not ACTIVE; 404 only for an unknown key',
        "/assign/batch answers 404 when the experiment is not ACTIVE",
        "Errors: 401 bad key, 404 no ACTIVE experiment with that key, 422 invalid event",
        "**No Lambda function serves requests or processes events.** SDKs call the API",
        "The API does not publish tracking events to the stream today",
        "Give each variant a configuration (a JSON payload) when creating an experiment",
        "the evaluate endpoint takes a `context`, which this SDK does not send yet",
        "13 of the 16 SDKs run against a live backend",
        "Python 3.11 tests",
        "`main` requires 20 checks to merge; `lint` is one of them.",
        "`./vendor/bin/phpunit` runs the PHPUnit 10 suite (client, cache, HTTP client and hash tests)",
        "the default (2 vCPU + 4 GB)",
    ],
)
def test_a_true_sentence_is_not_a_hit(true_text: str) -> None:
    assert list(hits("docs/x.md", true_text)) == []


def test_an_allowed_text_that_has_gone_is_refused(monkeypatch) -> None:
    gone = (
        ("docs/x.md", "404 when the flag is not ACTIVE", "owned by another change"),
    )
    monkeypatch.setattr(f"{__name__}.ALLOWED", gone)
    assert problems({"docs/x.md": "nothing here"}) == [
        "docs/x.md: allowed text is no longer on the page: '404 when the flag is not ACTIVE'"
    ]
    assert problems({"docs/x.md": "a 404 when the flag is not ACTIVE."}) == []
    defused = (("docs/x.md", "the flag is off", "owned by another change"),)
    monkeypatch.setattr(f"{__name__}.ALLOWED", defused)
    assert problems({"docs/x.md": "the flag is off"}) == [
        "docs/x.md: allowed text no longer holds a hit: 'the flag is off'"
    ]
