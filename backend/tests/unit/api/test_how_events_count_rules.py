"""How events count, pinned where the code decides it: the routes each SDK
sends events to, the rate limiter in front of them, and the code on the event
path, which reads no User-Agent.

``docs/guides/how-events-count.md`` states these rules, and its registry under
``docs/_claims/`` names each test here. The integration tests beside them, in
``backend/tests/integration/api/test_how_events_count.py``, hold what is stored
and counted.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.app.middleware.rate_limiter import (
    RateLimitMiddleware,
    SlidingWindowRateLimiter,
    rate_limit_key,
)

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[4]
SDK = ROOT / "sdk"

#: Each directory under sdk/: where its own source is, and the client it
#: sends events through when it sends none itself (the OpenFeature providers).
SDK_SOURCES = {
    "android": ("sdk/android/sdk/src/main", None),
    "dotnet": ("sdk/dotnet/src", None),
    "edge": ("sdk/edge/src", None),
    "elixir": ("sdk/elixir/lib", None),
    "flutter": ("sdk/flutter/lib", None),
    "go": ("sdk/go", None),
    "ios": ("sdk/ios/Sources/Controlvar", None),
    "java": ("sdk/java/core/src/main", None),
    "js": ("sdk/js/src", None),
    "openfeature": ("sdk/openfeature/src", "@controlvar/js-sdk"),
    "openfeature-python": (
        "sdk/openfeature-python/src",
        "from controlvar import ExperimentationClient",
    ),
    "php": ("sdk/php/src", None),
    "python": ("sdk/python/controlvar", None),
    "react": ("sdk/react/src", None),
    "react-native": ("sdk/react-native/src", None),
    "ruby": ("sdk/ruby/lib", None),
}
SOURCE_SUFFIXES = frozenset(
    {
        ".cs",
        ".dart",
        ".ex",
        ".go",
        ".java",
        ".js",
        ".kt",
        ".mjs",
        ".php",
        ".py",
        ".rb",
        ".swift",
        ".ts",
        ".tsx",
    }
)
SKIPPED_PARTS = frozenset(
    {
        "__tests__",
        "test",
        "tests",
        "examples",
        "example",
        "node_modules",
        "dist",
        "build",
    }
)
TEST_NAME = re.compile(r"(_test\.go|\.test\.tsx?|\.spec\.tsx?|_spec\.rb|Tests?\.\w+)$")

#: The modules on the event path: the routes that receive events, the service
#: and model that store them, the analysis that counts them, and the limiter.
EVENT_PATH = (
    "backend/app/api/v1/endpoints/tracking.py",
    "backend/app/services/event_service.py",
    "backend/app/models/event.py",
    "backend/app/services/analysis_service.py",
    "backend/app/services/event_matching.py",
    "backend/app/middleware/rate_limiter.py",
)
AGENT_OR_BOT = re.compile(
    r"user[-_ ]?agent|(?<![a-z])(?:bots?|crawlers?|spiders?)(?![a-z])", re.IGNORECASE
)


def _source(rel: str) -> str:
    """Every source file under ``rel``, tests and examples left out."""
    texts = []
    for path in sorted((ROOT / rel).rglob("*")):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        if SKIPPED_PARTS.intersection(path.relative_to(ROOT / rel).parts[:-1]):
            continue
        if TEST_NAME.search(path.name):
            continue
        texts.append(path.read_text(encoding="utf-8"))
    assert texts, f"{rel}: no source read"
    return "\n".join(texts)


def test_every_sdk_directory_is_listed():
    """A new SDK fails here until it is listed, so the next test reads it."""
    found = sorted(
        p.name
        for p in SDK.iterdir()
        if p.is_dir() and not p.name.startswith((".", "__"))
    )
    assert found == sorted(SDK_SOURCES)


@pytest.mark.parametrize("sdk", sorted(SDK_SOURCES))
def test_every_sdk_sends_its_events_through_track_and_batch(sdk):
    """Each SDK posts its events to ``/tracking/track`` and
    ``/tracking/batch``, never to ``/tracking/events``; the OpenFeature
    providers name no tracking route and send through the JS or Python
    client."""
    rel, wrapped = SDK_SOURCES[sdk]
    source = _source(rel)
    assert "/tracking/events" not in source, f"{sdk} sends to /tracking/events"
    if wrapped is None:
        assert "/tracking/track" in source, f"{sdk} names no /tracking/track"
        assert "/tracking/batch" in source, f"{sdk} names no /tracking/batch"
    else:
        assert "/tracking/" not in source, f"{sdk} names a tracking route itself"
        assert wrapped in source, f"{sdk} does not use {wrapped!r}"


def _limited(limit: int):
    """The rate limiter in front of two recording tracking routes, with the
    SDK paths' limit set to ``limit``; the client and what the routes ran."""
    app = FastAPI()
    ran = []

    @app.post("/api/v1/tracking/track")
    async def track(request: Request):
        ran.append(await request.json())
        return {"stored": True}

    @app.post("/api/v1/tracking/batch")
    async def batch(request: Request):
        ran.append(await request.json())
        return {"stored": True}

    middleware = RateLimitMiddleware(app, enabled=True)
    middleware._limiter = SlidingWindowRateLimiter()
    middleware._sdk_limit = limit
    return TestClient(middleware), ran


@pytest.mark.parametrize("route", ["/api/v1/tracking/track", "/api/v1/tracking/batch"])
def test_a_429_is_answered_before_the_route_runs(route):
    """Over the limit, the answer is 429 with ``Retry-After`` and the route,
    which is what stores an event, never runs."""
    client, ran = _limited(2)
    answers = [client.post(route, json={"user_id": f"user-{i}"}) for i in range(3)]
    assert [a.status_code for a in answers] == [200, 200, 429]
    assert answers[2].headers["Retry-After"]
    assert ran == [{"user_id": "user-0"}, {"user_id": "user-1"}]


def test_the_rate_limit_counts_requests_per_address_not_per_user():
    """Three users from one address share one counter, and the counter is
    named by the address and the path alone."""
    client, ran = _limited(2)
    statuses = [
        client.post("/api/v1/tracking/track", json={"user_id": user}).status_code
        for user in ("alice", "bob", "carol")
    ]
    assert statuses == [200, 200, 429]
    assert list(inspect.signature(rate_limit_key).parameters) == ["client_ip", "path"]
    assert (
        rate_limit_key("203.0.113.9", "/api/v1/tracking/track")
        == "203.0.113.9:/api/v1/tracking/track"
    )


@pytest.mark.parametrize("rel", EVENT_PATH)
def test_the_event_path_reads_no_user_agent(rel):
    """Nothing on the event path looks at the sender's User-Agent or names a
    bot: no event is told apart by who sent it."""
    text = (ROOT / rel).read_text(encoding="utf-8")
    found = sorted({m.group(0) for m in AGENT_OR_BOT.finditer(text)})
    assert not found, f"{rel} mentions {found}"


def test_the_bot_scan_finds_what_it_looks_for():
    """The scan above reads real words: it finds each form it refuses."""
    for planted in (
        'request.headers.get("user-agent")',
        "request.headers['User-Agent']",
        "if is_bot(ua):",
        "KNOWN_CRAWLERS = []",
        "a spider",
    ):
        assert AGENT_OR_BOT.search(planted), planted
    assert not AGENT_OR_BOT.search("both bottom robot abbot")
