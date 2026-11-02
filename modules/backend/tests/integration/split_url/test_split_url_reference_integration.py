"""The reference integration (``controlvar_split_url.py``) keeps the contract (#393).

The split URL guide prints that file whole, and Doc Examples runs the page's
copy (``test_split_url_reference_page.py`` holds the two equal). Here it is
driven with a fake API, a fake clock and a fake ASGI server, and once
against the in-process API for every location vector:

* a redirect is exactly 302, Location, ``Cache-Control: no-store``, no Vary;
* the visitor-id cookie is exactly the vectors' string, set whenever an id
  was minted -- whatever the API answered -- and never otherwise;
* a served entry page carries ``Cache-Control: private`` unless it said
  ``no-store``;
* every failure serves the page and logs one line with the HTTP status or a
  fixed word; the API is skipped for 30 seconds, except after a 4xx about
  one request (any 4xx but 401, 403 and 429);
* it sends only what the API accepts: the entry's host, and the query string
  and User-Agent only within the API's limits;
* the routes table is kept for its clamped ``max_age``, and a request that is
  not in a current table takes no thread.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from modules.backend.tests.integration.split_url import controlvar_split_url as ref
from modules.backend.tests.integration.split_url.transport import client_transport

pytestmark = [pytest.mark.integration]

VECTORS = json.loads((Path(__file__).with_name("vectors.json")).read_text())
ROUTE = {
    "experiment_key": "checkout-pages",
    "experiment_id": "0b5e4f1a-2c3d-4e5f-8a9b-0c1d2e3f4a5b",
    "host": "e.com",
    "path": "/checkout",
}
REDIRECT = {
    "action": "redirect",
    "location": "https://e.com/checkout-v2?utm_source=x",
    "reason": "assigned",
}
SERVE = {"action": "serve", "location": None, "reason": "assigned"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeApi:
    """The two routes, from fixed answers; every call recorded."""

    def __init__(self, assign=REDIRECT, routes=(ROUTE,), max_age=60) -> None:
        self.assign = assign
        self.routes = {"routes": list(routes), "max_age": max_age}
        self.calls: List[Dict] = []

    @staticmethod
    def _reply(answer):
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, tuple):
            return answer
        return 200, json.dumps(answer).encode()

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": json.loads(body) if body else None,
                "timeout": timeout,
            }
        )
        if url.endswith("/routes"):
            return self._reply(self.routes)
        return self._reply(self.assign)

    def assign_calls(self) -> List[Dict]:
        return [c for c in self.calls if c["url"].endswith("/assign")]


def split_url(api: FakeApi, clock: Optional[Clock] = None, **kwargs) -> ref.SplitUrl:
    return ref.SplitUrl(
        "https://api.example", "key-1", http=api, clock=clock or Clock(), **kwargs
    )


def visit(integration, cookie="", host="e.com", path="/checkout", **kwargs):
    return integration.answer("GET", host, path, "utm_source=x", cookie, **kwargs)


def logged(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.name == "controlvar.split_url"]


def test_a_redirect_is_exact():
    """V8(a): 302, Location, no-store, the cookie for a new visitor, and no
    Vary."""
    api = FakeApi()
    answer = visit(split_url(api))
    uid = api.assign_calls()[0]["body"]["user_id"]
    assert answer.status == 302
    assert answer.headers == [
        ("Location", "https://e.com/checkout-v2?utm_source=x"),
        ("Cache-Control", "no-store"),
        (
            "Set-Cookie",
            f"controlvar_uid={uid}; Max-Age=2592000; Path=/; SameSite=Lax; Secure",
        ),
        ("X-Split-URL", "redirected"),
    ]
    assert not [n for n, _ in answer.headers if n.lower() == "vary"]


@pytest.mark.parametrize("vector", VECTORS["set_cookie"], ids=lambda v: v["id"])
@pytest.mark.parametrize("assign", [REDIRECT, SERVE], ids=["redirect", "serve"])
def test_the_cookie_is_exact(vector, assign):
    """V8(b): exactly the vectors' string, with the id sent to the API, never
    HttpOnly."""
    api = FakeApi(assign=assign)
    answer = visit(split_url(api, cookie_domain=vector["cookie_domain"]))
    uid = api.assign_calls()[0]["body"]["user_id"]
    cookies = [v for n, v in answer.headers if n == "Set-Cookie"]
    assert cookies == [vector["expect"].replace("<id>", uid)], vector["id"]
    assert "httponly" not in cookies[0].lower()


def test_a_returning_visitor_keeps_their_id_and_gets_no_cookie():
    api = FakeApi()
    answer = visit(split_url(api), cookie="other=1; controlvar_uid=visitor-7")
    assert api.assign_calls()[0]["body"]["user_id"] == "visitor-7"
    assert [n for n, _ in answer.headers if n == "Set-Cookie"] == []


@pytest.mark.parametrize(
    "value", ["", "a\tb", "x" * 256], ids=["empty", "control", "too-long"]
)
def test_an_unusable_cookie_is_skipped_and_never_overwritten(value):
    api = FakeApi()
    answer = visit(split_url(api), cookie=f"controlvar_uid={value}")
    assert answer.outcome == "skipped"
    assert api.assign_calls() == []
    assert [n for n, _ in answer.headers if n == "Set-Cookie"] == []


@pytest.mark.parametrize(
    "page, expected",
    [
        ([("Cache-Control", "public, max-age=600")], [("Cache-Control", "private")]),
        ([], [("Cache-Control", "private")]),
        ([("cache-control", "no-store")], [("Cache-Control", "no-store")]),
    ],
    ids=["public-replaced", "none-added", "no-store-kept"],
)
def test_a_served_entry_page_is_private(page, expected):
    """V8(c): served, so a shared cache must not keep it for the next
    visitor; a page's own no-store is kept."""
    answer = visit(split_url(FakeApi(assign=SERVE)), cookie="controlvar_uid=v")
    headers = answer.page_headers([("Content-Type", "text/html"), *page])
    assert [h for h in headers if h[0].lower() == "cache-control"] == expected
    assert ("Content-Type", "text/html") in headers
    assert ("X-Split-URL", "served") in headers


def test_a_page_that_is_not_an_entry_is_left_alone():
    """No API call, no header: not in the routes table."""
    api = FakeApi()
    answer = visit(split_url(api), host="other.example")
    assert answer.outcome == "not-routed"
    assert api.assign_calls() == []
    page = [("Cache-Control", "public, max-age=600")]
    assert answer.page_headers(page) == page


@pytest.mark.parametrize(
    "method, kwargs",
    [
        ("HEAD", {}),
        ("POST", {}),
        ("GET", {"prefetch": True}),
        ("GET", {"consented": False}),
    ],
    ids=["head", "post", "prefetch", "no-consent"],
)
def test_only_a_get_with_consent_is_split(method, kwargs):
    """HEAD, other methods and prefetches are skipped; without consent no id
    is minted, no call made and no cookie set."""
    api = FakeApi()
    answer = ref.SplitUrl(
        "https://api.example", "key-1", http=api, clock=Clock()
    ).answer(method, "e.com", "/checkout", "", "", **kwargs)
    assert answer.outcome == "skipped"
    assert api.assign_calls() == []
    assert [n for n, _ in answer.headers if n == "Set-Cookie"] == []


FAILURES = {
    "transport": (ConnectionRefusedError(), "unreachable"),
    "protocol": (http.client.IncompleteRead(b""), "unreachable"),
    "bad-status-line": (http.client.BadStatusLine("garbage"), "unreachable"),
    "401": ((401, b'{"detail": "Invalid API Key"}'), "401"),
    "403": ((403, b"{}"), "403"),
    "404": ((404, b"{}"), "404"),
    "409": ((409, b"{}"), "409"),
    "422": ((422, b'{"detail": []}'), "422"),
    "429": ((429, b'{"detail": "Too many requests"}'), "429"),
    "500": ((500, b"oops"), "500"),
    "timeout": (TimeoutError(), "timeout"),
    "malformed": ((200, b"<html>"), "unparseable"),
    "missing-fields": ((200, b'{"action": "redirect"}'), "unparseable"),
}
#: A refusal of one request: it does not pause routing.
ABOUT_ONE_REQUEST = {"404", "409", "422"}


@pytest.mark.regression
@pytest.mark.parametrize("kind", sorted(FAILURES))
def test_a_failure_serves_the_page_and_logs_its_status(kind, caplog):
    """V15 for the reference integration: the page is served; X-Split-URL is
    failed; the cookie is set with the minted id whatever the API answered;
    exactly one log line, with the HTTP status or a fixed word. The API is
    then skipped for 30 s and asked again, except after a refusal of one
    request, when the next visitor is asked about at once."""
    failure, logged = FAILURES[kind]
    api, clock = FakeApi(assign=failure), Clock()
    integration = split_url(api, clock)
    with caplog.at_level(logging.WARNING, logger="controlvar.split_url"):
        answer = visit(integration)
    uid = api.assign_calls()[0]["body"]["user_id"]
    assert answer.status is None
    assert answer.outcome == "failed"
    assert ("X-Split-URL", "failed") in answer.headers
    assert (
        "Set-Cookie",
        f"controlvar_uid={uid}; Max-Age=2592000; Path=/; SameSite=Lax; Secure",
    ) in answer.headers
    lines = [r.getMessage() for r in caplog.records if r.name == "controlvar.split_url"]
    assert lines == [f"split_url_failed status={logged}"], kind

    clock.now += 29
    api.assign = REDIRECT
    following = visit(integration)
    if kind not in ABOUT_ONE_REQUEST:
        assert (following.outcome, len(api.assign_calls())) == ("failed", 1), kind
        clock.now += 2
        following = visit(integration)
    assert (following.status, len(api.assign_calls())) == (302, 2), kind


def test_the_timeouts_are_one_and_a_half_seconds():
    """V15: checked as the values configured, never measured."""
    api = FakeApi()
    visit(split_url(api))
    assert [(c["url"].rsplit("/", 1)[1], c["timeout"]) for c in api.calls] == [
        ("routes", 1.0),
        ("assign", 1.5),
    ]
    assert api.calls[1]["headers"]["X-API-Key"] == "key-1"


@pytest.mark.parametrize(
    "location",
    ["https://e.com/a\r\nSet-Cookie: x=1", "javascript:alert(1)", "//e.com/b", None],
    ids=["crlf", "javascript", "scheme-relative", "null"],
)
def test_a_location_that_is_not_an_http_url_is_not_followed(location):
    api = FakeApi(assign={**REDIRECT, "location": location})
    answer = visit(split_url(api))
    assert answer.status is None
    assert answer.outcome == "failed"
    assert not [n for n, _ in answer.headers if n == "Location"]


@pytest.mark.regression
def test_a_location_whose_scheme_is_in_upper_case_is_followed():
    """The validation rules accept a scheme in any case, and so does the
    integration."""
    api = FakeApi(assign={**REDIRECT, "location": "HTTPS://e.com/b"})
    answer = visit(split_url(api))
    assert answer.status == 302
    assert dict(answer.headers)["Location"] == "HTTPS://e.com/b"


@pytest.mark.regression
def test_a_location_with_a_non_ascii_character_is_percent_encoded():
    """The API returns a variant URL's query string as stored, so it can hold
    a character a header cannot carry; the integration sends it as a browser
    would, percent-encoded as UTF-8, instead of failing the visitor."""
    api = FakeApi(assign={**REDIRECT, "location": "https://e.com/b?ref=€"})
    sent, pages = run_middleware({}, api)
    assert pages == []
    assert (b"location", b"https://e.com/b?ref=%E2%82%AC") in [
        (n.lower(), v) for n, v in sent[0]["headers"]
    ]
    answer = visit(split_url(api))
    assert answer.status == 302
    assert dict(answer.headers)["Location"] == "https://e.com/b?ref=%E2%82%AC"


def test_the_routes_table_is_kept_for_its_max_age():
    """V17 for the reference integration: one fetch within max_age, a second
    once the clock passes it."""
    api, clock = FakeApi(), Clock()
    integration = split_url(api, clock)
    for _ in range(50):
        visit(integration, cookie="controlvar_uid=v")
    assert [c["url"] for c in api.calls].count(
        "https://api.example/api/v1/tracking/split-url/routes"
    ) == 1
    clock.now += 61
    visit(integration, cookie="controlvar_uid=v")
    assert len([c for c in api.calls if c["url"].endswith("/routes")]) == 2


@pytest.mark.parametrize("max_age, kept", [(1, 10), (60, 60), (100000, 300)])
def test_max_age_is_clamped(max_age, kept):
    api, clock = FakeApi(max_age=max_age), Clock()
    integration = split_url(api, clock)
    integration.routes()
    clock.now += kept - 0.5
    integration.routes()
    assert len(api.calls) == 1
    clock.now += 1
    integration.routes()
    assert len(api.calls) == 2


def test_the_last_good_table_is_kept_on_error(caplog):
    api, clock = FakeApi(), Clock()
    integration = split_url(api, clock)
    assert integration.routes()
    clock.now += 61
    api.routes = (503, b"")
    with caplog.at_level(logging.WARNING, logger="controlvar.split_url"):
        assert list(integration.routes()) == [("e.com", "/checkout")]
    assert [r.getMessage() for r in caplog.records] == ["split_url_failed status=503"]


@pytest.mark.parametrize("vector", VECTORS["location"], ids=lambda v: v["id"])
def test_the_location_is_the_api_s(admin_client, split_experiment, vector):
    """V9: against the in-process API, the integration redirects to exactly
    the vector's Location, and makes no assign call off the entry."""
    split_experiment(
        "split-vectors",
        [
            ("Control", True, 50, vector["control"]),
            ("Treatment", False, 50, vector["variant"]),
        ],
        optimization_type="thompson_sampling",
        bandit_weights={"Control": 0.0, "Treatment": 1.0},
    )
    calls = []
    integration = ref.SplitUrl(
        "http://api", "key", http=client_transport(admin_client, calls)
    )
    answer = integration.answer(
        "GET", vector["host"], vector["path"], vector["query"], ""
    )
    assigned = [c for c in calls if c[1].endswith("/assign")]
    if vector["expect"] == "not_entry":
        assert (answer.outcome, assigned) == ("not-routed", []), vector["id"]
    else:
        assert dict(answer.headers)["Location"] == vector["expect"], vector["id"]
        assert answer.status == 302


#: A visitor's host, query or User-Agent beyond what the API accepts, and the
#: Location they are sent to: the entry's host is sent, and a query string or
#: User-Agent the API does not accept is left out.
BEYOND_THE_LIMITS = {
    "host-with-a-space": ({"host": "e.com:1 2"}, "https://e.com/checkout-v2?q=1"),
    "host-too-long": ({"host": "e.com:" + "1" * 300}, "https://e.com/checkout-v2?q=1"),
    "query-with-a-hash": ({"query": "a=1#x"}, "https://e.com/checkout-v2"),
    "query-too-long": ({"query": "a=" + "x" * 9000}, "https://e.com/checkout-v2"),
    "query-with-a-space": ({"query": "a=1 2"}, "https://e.com/checkout-v2"),
    "query-with-a-control": ({"query": "a=1\x01"}, "https://e.com/checkout-v2"),
    "user-agent-with-nul": ({"ua": "Mozilla/5.0\x00"}, "https://e.com/checkout-v2?q=1"),
}


@pytest.mark.regression
@pytest.mark.parametrize("kind", sorted(BEYOND_THE_LIMITS))
def test_only_what_the_api_accepts_is_sent(admin_client, split_experiment, kind):
    """Against the in-process API: a treatment visitor whose host, query or
    User-Agent the API would not accept is still sent to the treatment, and
    the next visitor is still asked about."""
    split_experiment(
        "split-accepted",
        optimization_type="thompson_sampling",
        bandit_weights={"Control": 0.0, "Treatment": 1.0},
    )
    beyond, expected = BEYOND_THE_LIMITS[kind]
    visitor = {"host": "e.com", "query": "q=1", "ua": "Mozilla/5.0", **beyond}
    calls = []
    integration = ref.SplitUrl(
        "http://api", "key", http=client_transport(admin_client, calls)
    )
    first = integration.answer(
        "GET", visitor["host"], "/checkout", visitor["query"], "", visitor["ua"]
    )
    following = integration.answer("GET", "e.com", "/checkout", "q=1", "")
    assigned = [c for c in calls if c[1].endswith("/assign")]
    assert (first.status, dict(first.headers).get("Location")) == (302, expected)
    assert (following.status, len(assigned)) == (302, 2), kind


def run_middleware(
    scope_overrides, answer_api: FakeApi, page_headers=(), integration=None
):
    """Drive SplitUrlMiddleware around a one-page ASGI app; what it sent."""
    sent, pages = [], []

    async def page(scope, receive, send):
        pages.append(scope["path"])
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": list(page_headers),
            }
        )
        await send({"type": "http.response.body", "body": b"page"})

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/checkout",
        "raw_path": b"/checkout",
        "query_string": b"utm_source=x",
        "headers": [(b"host", b"e.com")],
        **scope_overrides,
    }
    middleware = ref.SplitUrlMiddleware(page, integration or split_url(answer_api))
    asyncio.run(middleware(scope, receive, send))
    return sent, pages


def test_the_middleware_redirects_without_calling_the_page():
    api = FakeApi()
    sent, pages = run_middleware({}, api)
    assert pages == []
    assert sent[0]["status"] == 302
    assert (b"location", b"https://e.com/checkout-v2?utm_source=x") in [
        (n.lower(), v) for n, v in sent[0]["headers"]
    ]


def test_the_middleware_adds_its_headers_to_the_page():
    api = FakeApi(assign=SERVE)
    sent, pages = run_middleware(
        {
            "headers": [
                (b"host", b"e.com"),
                (b"cookie", b"a=1"),
                (b"cookie", b"controlvar_uid=v-9"),
            ]
        },
        api,
        page_headers=[(b"cache-control", b"public, max-age=600")],
    )
    assert pages == ["/checkout"]
    assert api.assign_calls()[0]["body"]["user_id"] == "v-9"
    headers = sent[0]["headers"]
    assert (b"Cache-Control", b"private") in headers
    assert (b"cache-control", b"public, max-age=600") not in headers
    assert (b"X-Split-URL", b"served") in headers


def test_the_middleware_compares_the_path_as_sent():
    """The raw path, not percent-decoded: an entry written /caf%C3%A9 is the
    visitor's /caf%C3%A9, whatever the server decoded it to."""
    api = FakeApi(routes=[{**ROUTE, "path": "/caf%C3%A9"}])
    sent, _ = run_middleware({"path": "/café", "raw_path": b"/caf%C3%A9"}, api)
    assert sent[0]["status"] == 302
    assert api.assign_calls()[0]["body"]["path"] == "/caf%C3%A9"


def test_the_middleware_serves_as_it_is_without_a_raw_path():
    """A server that does not give the raw path: no call, nothing added."""
    api = FakeApi()
    scope = {"path": "/checkout"}
    sent, pages = run_middleware({**scope, "raw_path": None}, api)
    assert pages == ["/checkout"]
    assert api.calls == []
    assert sent[0]["headers"] == []


@pytest.mark.regression
@pytest.mark.parametrize(
    "broken",
    [http.client.IncompleteRead(b""), http.client.BadStatusLine("garbage")],
    ids=["incomplete-read", "bad-status-line"],
)
def test_a_broken_routes_answer_serves_the_page(broken, caplog):
    """A routes answer that is not well-formed HTTP is a failure like any
    other: the page is served as it is, with one log line."""
    api = FakeApi()
    api.routes = broken
    with caplog.at_level(logging.WARNING, logger="controlvar.split_url"):
        sent, pages = run_middleware({"path": "/about", "raw_path": b"/about"}, api)
    assert pages == ["/about"]
    assert (sent[0]["status"], sent[0]["headers"]) == (200, [])
    assert logged(caplog) == ["split_url_failed status=unreachable"]


@pytest.mark.regression
def test_an_unexpected_error_serves_the_page(caplog):
    """Anything else the integration raises is logged with a fixed word, and
    the page is served as it is."""
    api = FakeApi()
    api.routes = RuntimeError("unexpected")
    with caplog.at_level(logging.WARNING, logger="controlvar.split_url"):
        sent, pages = run_middleware({}, api)
    assert pages == ["/checkout"]
    assert (sent[0]["status"], sent[0]["headers"]) == (200, [])
    assert logged(caplog) == ["split_url_failed status=error"]


def test_a_request_that_is_not_in_a_current_table_takes_no_thread(monkeypatch):
    """With a current routes table in hand, a request that is not an entry is
    served without a thread; an entry, or any request once the table is due,
    takes one."""
    threads = []
    to_thread = asyncio.to_thread

    async def counted(function, *args):
        threads.append(args[2])
        return await to_thread(function, *args)

    monkeypatch.setattr(ref.asyncio, "to_thread", counted)
    api, clock = FakeApi(assign=SERVE), Clock()
    integration = split_url(api, clock)
    integration.routes()
    asset = {"path": "/static/app.css", "raw_path": b"/static/app.css"}
    _, pages = run_middleware(asset, api, integration=integration)
    assert (pages, threads) == (["/static/app.css"], [])
    run_middleware({}, api, integration=integration)
    assert threads == ["/checkout"]
    clock.now += 61
    run_middleware(asset, api, integration=integration)
    assert threads == ["/checkout", "/static/app.css"]
