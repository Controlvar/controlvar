"""The CloudFront edge router keeps the redirect contract (#393).

The router asks the API, at viewer request, where to send each visitor who
opens an entry page, and redirects or serves; at viewer response it sets the
visitor-id cookie and ``X-Split-URL``. Driven here with a fake API, a fake
clock and the simulated CloudFront of ``harness.py`` (events in the shape AWS
documents). The same rules as the reference integration
(``modules/backend/tests/integration/split_url/controlvar_split_url.py``),
held equal to it and to the core helpers where they share one:

* a redirect is exactly 302, Location, ``Cache-Control: no-store`` and its
  own Set-Cookie for a visitor who arrived without the cookie, no Vary;
* the visitor-id cookie is exactly the vectors' string, with the id derived
  from ``config.requestId`` that viewer request sent to the API, set
  whenever an id was derived -- whatever the API answered -- and never
  otherwise; it does not rest on the internal header reaching viewer
  response;
* every failure serves the page, logs one line with the HTTP status or a
  fixed word, and skips the API for 30 seconds, except after a 4xx about one
  request; nothing raises;
* only a GET of an entry is split; it sends the entry's host, and the query
  string and User-Agent only within the API's limits;
* the routes table is kept for its clamped ``max_age``, by one loader both
  phases use, which loads on a miss in either.
"""

from __future__ import annotations

import http.client
import http.server
import json
import logging
import random
import threading
import uuid
from typing import List

import pytest

from backend.app.schemas.split_url_config import normalize_host, normalize_path
from modules.backend.tests.integration.split_url import controlvar_split_url as ref
from modules.backend.tests.unit.split_url_router.harness import (
    REDIRECT,
    ROUTER_FILE,
    SAMPLE_REQUEST_ID,
    SERVE,
    VECTORS,
    Clock,
    FakeApi,
    cookie_id,
    deliver,
    load_router,
    make_router,
    router_module,
    viewer_request,
    viewer_response,
)

pytestmark = [pytest.mark.unit]

COOKIE = "controlvar_uid={}; Max-Age=2592000; Path=/; SameSite=Lax; Secure"
LOGGER = "controlvar.split_url_router"


def logged(caplog) -> List[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == LOGGER]


def sent_ids(api: FakeApi) -> List[str]:
    return [c["body"]["user_id"] for c in api.assign_calls()]


# -- the visitor id ---------------------------------------------------------


def test_the_visitor_id_is_derived_from_the_request_id():
    """The derivation, fixed by a vector: the documented sample's request id
    gives this id. It is a UUID (version 4 form), the same for the same
    request id, and different for another."""
    derived = router_module.visitor_id(SAMPLE_REQUEST_ID)
    assert derived == "0559f6cf-542f-4187-8cc2-eaece4a21c4c"
    assert str(uuid.UUID(derived)) == derived
    assert uuid.UUID(derived).version == 4
    assert router_module.visitor_id(SAMPLE_REQUEST_ID) == derived
    assert router_module.visitor_id(SAMPLE_REQUEST_ID[:-1] + "A") != derived
    assert router_module.usable(derived)
    assert router_module.visitor_id("") is None
    assert router_module.visitor_id(None) is None


def test_distinct_request_ids_give_distinct_ids():
    ids = {router_module.visitor_id(f"request-{i}") for i in range(10000)}
    assert len(ids) == 10000


# -- what the viewer gets ---------------------------------------------------


def test_a_redirect_is_exact():
    """V8(a): 302, Location, no-store, the visitor's own cookie with the id
    sent to the API, X-Split-URL, and no Vary. Generated at viewer request,
    so it carries its own Set-Cookie: viewer response does not run for it."""
    api = FakeApi()
    delivery = deliver(make_router(api), viewer_request())
    uid = sent_ids(api)[0]
    assert uid == router_module.visitor_id(SAMPLE_REQUEST_ID)
    assert delivery.generated == {
        "status": "302",
        "statusDescription": "Found",
        "headers": {
            "location": [
                {"key": "Location", "value": "https://e.com/checkout-v2?utm_source=x"}
            ],
            "cache-control": [{"key": "Cache-Control", "value": "no-store"}],
            "x-split-url": [{"key": "X-Split-URL", "value": "redirected"}],
            "set-cookie": [{"key": "Set-Cookie", "value": COOKIE.format(uid)}],
        },
    }
    assert delivery.headers("Vary") == []


@pytest.mark.parametrize("vector", VECTORS["set_cookie"], ids=lambda v: v["id"])
@pytest.mark.parametrize("assign", [REDIRECT, SERVE], ids=["redirect", "serve"])
def test_the_cookie_is_exact(vector, assign):
    """V8(b): exactly the vectors' string, with the id sent to the API, never
    HttpOnly, on the redirect and on the served page alike."""
    api = FakeApi(assign=assign)
    router = make_router(api, cookie_domain=vector["cookie_domain"])
    delivery = deliver(router, viewer_request())
    uid = sent_ids(api)[0]
    cookies = delivery.headers("Set-Cookie")
    assert cookies == [vector["expect"].replace("<id>", uid)], vector["id"]
    assert "httponly" not in cookies[0].lower()


def test_a_served_page_keeps_its_own_headers_and_gets_the_routers():
    """A control visitor: the page is served with the origin's headers, its
    own Set-Cookie kept, plus X-Split-URL and the cookie. The id in the
    cookie is the one viewer request sent to the API."""
    api = FakeApi(assign=SERVE)
    delivery = deliver(
        make_router(api),
        viewer_request(),
        origin_headers=[("Content-Type", "text/html"), ("Set-Cookie", "session=s1")],
    )
    assert delivery.generated is None
    assert delivery.status == "200"
    assert delivery.header("Content-Type") == "text/html"
    assert delivery.header("X-Split-URL") == "served"
    assert delivery.headers("Set-Cookie") == [
        "session=s1",
        COOKIE.format(sent_ids(api)[0]),
    ]
    assert cookie_id(delivery) == sent_ids(api)[0]


def test_the_cookie_does_not_rest_on_the_internal_header():
    """Viewer response derives the id from the same request id, so the cookie
    is set even when the header viewer request added does not reach it (PE-1
    false); only X-Split-URL goes."""
    api = FakeApi(assign=SERVE)
    delivery = deliver(make_router(api), viewer_request(), pe1=False)
    assert cookie_id(delivery) == sent_ids(api)[0]
    assert delivery.header("X-Split-URL") is None


@pytest.mark.parametrize(
    "assign, outcome",
    [
        (SERVE, "served"),
        ({**SERVE, "reason": "holdout"}, "not-enrolled"),
        ({**SERVE, "reason": "targeting"}, "not-enrolled"),
        ({**SERVE, "reason": "mutual_exclusion"}, "not-enrolled"),
        ({**SERVE, "reason": "paused"}, "served"),
        ({**SERVE, "reason": "bot"}, "served"),
    ],
    ids=["assigned", "holdout", "targeting", "mutual-exclusion", "paused", "bot"],
)
def test_a_served_entry_page_names_its_outcome(assign, outcome):
    delivery = deliver(make_router(FakeApi(assign=assign)), viewer_request())
    assert delivery.header("X-Split-URL") == outcome


def test_x_split_url_takes_exactly_five_values():
    """Every outcome the router gives, across requests of every kind."""
    seen = set()
    cases = [
        (FakeApi(), viewer_request()),
        (FakeApi(assign=SERVE), viewer_request()),
        (FakeApi(assign={**SERVE, "reason": "holdout"}), viewer_request()),
        (FakeApi(), viewer_request(method="HEAD")),
        (FakeApi(assign=(500, b"")), viewer_request()),
        (FakeApi(), viewer_request(uri="/about")),
    ]
    for api, event in cases:
        delivery = deliver(make_router(api), event)
        seen.update(delivery.headers("X-Split-URL"))
    assert seen == set(router_module.OUTCOMES)
    assert router_module.OUTCOMES == (
        "redirected",
        "served",
        "not-enrolled",
        "skipped",
        "failed",
    )


def test_a_returning_visitor_keeps_their_id_and_gets_no_cookie():
    """Every Cookie header entry is read, not only the first."""
    api = FakeApi(assign=SERVE)
    delivery = deliver(
        make_router(api),
        viewer_request(cookies=["other=1", "theme=dark; controlvar_uid=visitor-7"]),
    )
    assert sent_ids(api) == ["visitor-7"]
    assert delivery.headers("Set-Cookie") == []
    assert delivery.header("X-Split-URL") == "served"


@pytest.mark.parametrize(
    "value", ["", "a\tb", "x" * 256], ids=["empty", "control", "too-long"]
)
def test_an_unusable_cookie_is_skipped_and_never_overwritten(value):
    api = FakeApi()
    delivery = deliver(
        make_router(api), viewer_request(cookies=[f"controlvar_uid={value}"])
    )
    assert delivery.header("X-Split-URL") == "skipped"
    assert api.assign_calls() == []
    assert delivery.headers("Set-Cookie") == []


@pytest.mark.parametrize(
    "event",
    [
        viewer_request(method="HEAD"),
        viewer_request(method="POST"),
        viewer_request(extra=[("Sec-Purpose", "prefetch")]),
        viewer_request(extra=[("Purpose", "prefetch")]),
    ],
    ids=["head", "post", "sec-purpose-prefetch", "purpose-prefetch"],
)
def test_only_a_get_is_split(event):
    """HEAD, other methods and prefetches make no API call, get no id and no
    cookie, and are served as skipped."""
    api, clock = FakeApi(), Clock()
    router = make_router(api, clock)
    router.routes("viewer-request")
    before = len(api.calls)
    delivery = deliver(router, event)
    assert len(api.calls) - before == 0
    assert delivery.header("X-Split-URL") == "skipped"
    assert delivery.headers("Set-Cookie") == []


@pytest.mark.parametrize(
    "event",
    [
        viewer_request(uri="/about"),
        viewer_request(host="other.example"),
        viewer_request(uri="/checkout-v2"),
        viewer_request(uri="/Checkout"),
    ],
    ids=["another-path", "another-host", "a-treatment-url", "path-case"],
)
def test_a_page_that_is_not_an_entry_is_left_alone(event):
    """No API call, no cookie, no header: a treatment URL opened directly is
    not intercepted either, so nothing can loop."""
    api = FakeApi()
    delivery = deliver(make_router(api), event)
    assert api.assign_calls() == []
    assert delivery.generated is None
    assert delivery.headers("Set-Cookie") == []
    assert delivery.header("X-Split-URL") is None


@pytest.mark.parametrize(
    "host, uri",
    [("E.com:443", "/checkout"), ("e.com", "/checkout/"), ("e.COM", "/checkout")],
    ids=["host-case-and-port", "one-trailing-slash", "host-case"],
)
def test_an_entry_is_matched_as_the_routes_table_writes_it(host, uri):
    api = FakeApi()
    deliver(make_router(api), viewer_request(host=host, uri=uri))
    assert [c["body"]["host"] for c in api.assign_calls()] == ["e.com"]
    assert [c["body"]["path"] for c in api.assign_calls()] == [uri]


# -- failures ---------------------------------------------------------------

FAILURES = {
    "transport": (ConnectionRefusedError(), "unreachable"),
    "protocol": (http.client.IncompleteRead(b""), "unreachable"),
    "bad-status-line": (http.client.BadStatusLine("garbage"), "unreachable"),
    "302": ((302, b""), "302"),
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
    """V15: the page is served; X-Split-URL is failed; the cookie carries the
    derived id whatever the API answered; exactly one log line, with the HTTP
    status or a fixed word. The API is then skipped for 30 s and asked again,
    except after a refusal of one request. A 3xx is a failure, not a hop."""
    failure, status = FAILURES[kind]
    api, clock = FakeApi(assign=failure), Clock()
    router = make_router(api, clock)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        delivery = deliver(router, viewer_request())
    uid = sent_ids(api)[0]
    assert delivery.generated is None
    assert delivery.header("X-Split-URL") == "failed"
    assert delivery.headers("Set-Cookie") == [COOKIE.format(uid)]
    assert logged(caplog) == [
        {"event": "split_url_failed", "phase": "viewer-request", "status": status}
    ], kind

    clock.now += 29
    api.assign = REDIRECT
    following = deliver(router, viewer_request(request_id="second-request"))
    if kind not in ABOUT_ONE_REQUEST:
        assert (following.header("X-Split-URL"), len(api.assign_calls())) == (
            "failed",
            1,
        ), kind
        clock.now += 2
        following = deliver(router, viewer_request(request_id="third-request"))
    assert (following.status, len(api.assign_calls())) == ("302", 2), kind


def test_the_timeouts_are_one_and_a_half_seconds():
    """V15: checked as the values configured, never measured."""
    api = FakeApi()
    deliver(make_router(api), viewer_request())
    assert [(c["url"].rsplit("/", 1)[1], c["timeout"]) for c in api.calls] == [
        ("routes", 1.0),
        ("assign", 1.5),
    ]
    assert api.calls[1]["headers"]["X-API-Key"] == "key-1"
    assert (
        api.calls[1]["url"]
        == "https://api.example.com/api/v1/tracking/split-url/assign"
    )


@pytest.mark.regression
def test_an_unexpected_error_serves_the_page(caplog):
    """Anything the router raises is logged with a fixed word, and the request
    goes on as it came: never an exception to CloudFront."""
    api = FakeApi(assign=RuntimeError("unexpected"))
    router = make_router(api)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        answer = router.handle(viewer_request())
    assert "status" not in answer
    assert answer["uri"] == "/checkout"
    assert logged(caplog) == [
        {"event": "split_url_failed", "phase": "viewer-request", "status": "error"}
    ]


@pytest.mark.regression
def test_the_entry_point_never_raises(monkeypatch, caplog):
    """handler(): without its settings file every request and response goes
    on as it came; a malformed event comes back as it was."""
    module = router_module
    monkeypatch.setattr(module, "_router", None)
    monkeypatch.setattr(module, "CONFIG_FILE", "no-such-file.json")
    event = viewer_request(extra=[("X-Controlvar-Split", "served")])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        answer = module.handler(event, None)
        response_event = viewer_response({"headers": {}}, SAMPLE_REQUEST_ID)
        response = module.handler(response_event, None)
        assert module.handler({"Records": []}, None) == {"Records": []}
    assert answer["uri"] == "/checkout"
    assert "x-controlvar-split" not in answer["headers"]
    assert response == response_event["Records"][0]["cf"]["response"]
    assert {line["status"] for line in logged(caplog)} == {"error"}


def test_the_entry_point_reads_its_settings_file(monkeypatch, tmp_path):
    """handler() builds its router from config.json beside the handler, as
    the edge app bundles them."""
    (tmp_path / "handler.py").write_bytes(ROUTER_FILE.read_bytes())
    settings = {"api_url": "https://api.example.com", "api_key": "key-9"}
    (tmp_path / "config.json").write_text(json.dumps(settings), encoding="utf-8")
    bundled = load_router("bundled_split_url_router", tmp_path / "handler.py")
    api = FakeApi()
    monkeypatch.setattr(bundled, "http_call", api)
    answer = bundled.handler(viewer_request(), None)
    assert answer["status"] == "302"
    assert {c["headers"]["X-API-Key"] for c in api.calls} == {"key-9"}


@pytest.mark.parametrize(
    "location",
    ["https://e.com/a\r\nSet-Cookie: x=1", "javascript:alert(1)", "//e.com/b", None],
    ids=["crlf", "javascript", "scheme-relative", "null"],
)
def test_a_location_that_is_not_an_http_url_is_not_followed(location):
    api = FakeApi(assign={**REDIRECT, "location": location})
    router = make_router(api)
    delivery = deliver(router, viewer_request())
    assert delivery.generated is None
    assert delivery.header("X-Split-URL") == "failed"
    assert router.skip_until == 0.0


@pytest.mark.regression
def test_a_location_whose_scheme_is_in_upper_case_is_followed():
    """The validation rules accept a scheme in any case, and so does the
    router."""
    api = FakeApi(assign={**REDIRECT, "location": "HTTPS://e.com/b"})
    delivery = deliver(make_router(api), viewer_request())
    assert (delivery.status, delivery.header("Location")) == ("302", "HTTPS://e.com/b")


@pytest.mark.regression
def test_a_location_with_a_non_ascii_character_is_percent_encoded():
    """A variant URL's query string is stored as given and can hold a
    character a header cannot carry: it is sent as a browser would send it,
    percent-encoded as UTF-8."""
    api = FakeApi(assign={**REDIRECT, "location": "https://e.com/b?ref=€&q=%41"})
    delivery = deliver(make_router(api), viewer_request())
    assert delivery.header("Location") == "https://e.com/b?ref=%E2%82%AC&q=%41"


#: A visitor's query or User-Agent beyond what the API accepts: left out, and
#: the visitor is still routed.
BEYOND_THE_LIMITS = {
    "query-with-a-hash": ({"querystring": "a=1#x"}, {"query": None}),
    "query-too-long": ({"querystring": "a=" + "x" * 9000}, {"query": None}),
    "query-with-a-space": ({"querystring": "a=1 2"}, {"query": None}),
    "query-with-a-control": ({"querystring": "a=1\x01"}, {"query": None}),
    "user-agent-with-nul": ({"user_agent": "Mozilla/5.0\x00"}, {"user_agent": None}),
    "user-agent-too-long": (
        {"user_agent": "M" * 2000},
        {"user_agent": "M" * 1024},
    ),
    "host-with-a-port": ({"host": "E.com:8443"}, {"host": "e.com"}),
}


@pytest.mark.regression
@pytest.mark.parametrize("kind", sorted(BEYOND_THE_LIMITS))
def test_only_what_the_api_accepts_is_sent(kind):
    """The entry's host, and the query string and User-Agent only within the
    API's limits: the visitor is still routed, and the next visitor is still
    asked about."""
    request, expected = BEYOND_THE_LIMITS[kind]
    api = FakeApi()
    router = make_router(api)
    first = deliver(router, viewer_request(**request))
    following = deliver(router, viewer_request(request_id="next-visitor"))
    body = api.assign_calls()[0]["body"]
    assert {k: body[k] for k in expected} == expected, kind
    assert (first.status, following.status, len(api.assign_calls())) == (
        "302",
        "302",
        2,
    )


def test_the_routers_rules_are_the_ones_the_api_and_the_reference_use():
    """One rule, held equal rather than copied: an entry is what the core
    helpers the routes table is built with make of a host and a path; the
    query string and the User-Agent sent are what the reference integration
    sends."""
    rng = random.Random(393)
    alphabet = "aE:/[]%#?& \t\x00\x01\x7fé\ud800.-_=0129"
    texts = ["", "/", "//", "a", "x" * 9000, "a=1#x", "q=caf%C3%A9&b=%20&c"]
    texts += [
        "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        for _ in range(500)
    ]
    for text in texts:
        assert router_module.query_to_send(text) == ref.query_to_send(text), repr(text)
        assert router_module.user_agent_to_send(text) == ref.user_agent_to_send(text), (
            repr(text)
        )
        assert router_module.entry_of(text, text) == (
            normalize_host(text),
            normalize_path(text),
        ), repr(text)


# -- internal headers -------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "event",
    [
        viewer_request(uri="/about"),
        viewer_request(method="HEAD"),
        viewer_request(extra=[("Sec-Purpose", "prefetch")]),
        viewer_request(),
        viewer_request(cookies=["controlvar_uid=visitor-3"]),
    ],
    ids=["not-an-entry", "head", "prefetch", "entry", "returning"],
)
def test_the_routers_own_headers_are_its_own(event):
    """The x-controlvar-* request headers are the router's: any the request
    arrives with are gone from what reaches the origin, whatever the request,
    and only the router's own outcome is there."""
    for name, value in (
        ("X-Controlvar-Split", "served"),
        ("x-controlvar-set-uid", "visitor-9"),
        ("X-Controlvar-Anything", "1"),
    ):
        event["Records"][0]["cf"]["request"]["headers"][name.lower()] = [
            {"key": name, "value": value}
        ]
    api = FakeApi(assign=SERVE)
    router = make_router(api)
    router.skip_until = 0.0
    delivery = deliver(router, event)
    internal = {
        name: entries
        for name, entries in delivery.forwarded["headers"].items()
        if name.startswith("x-controlvar-")
    }
    assert set(internal) <= {"x-controlvar-split"}
    for entries in internal.values():
        assert [e["value"] for e in entries] in (["served"], ["skipped"])
    assert "visitor-9" not in json.dumps(delivery.response)


def test_the_routers_own_headers_are_its_own_while_it_waits():
    """The same in the 30 seconds after a failure, when the API is not asked."""
    api, clock = FakeApi(assign=(500, b"")), Clock()
    router = make_router(api, clock)
    deliver(router, viewer_request(request_id="first"))
    event = viewer_request(
        request_id="second",
        extra=[("X-Controlvar-Split", "served"), ("X-Controlvar-Set-Uid", "v-9")],
    )
    delivery = deliver(router, event)
    internal = {
        name: entries
        for name, entries in delivery.forwarded["headers"].items()
        if name.startswith("x-controlvar-")
    }
    assert internal == {
        "x-controlvar-split": [{"key": "X-Controlvar-Split", "value": "failed"}]
    }
    assert len(api.assign_calls()) == 1


@pytest.mark.parametrize("value", ["redirected", "<b>", "served\r\nX: 1", ""])
def test_viewer_response_names_only_an_outcome_it_can_have(value):
    """Viewer response turns the internal header into X-Split-URL only for
    one of the four outcomes of a served page, and only on an entry."""
    router = make_router(FakeApi())
    request = viewer_request(
        cookies=["controlvar_uid=v"], extra=[("X-Controlvar-Split", value)]
    )["Records"][0]["cf"]["request"]
    response = router.handle(viewer_response(request, "request-1"))
    assert "x-split-url" not in response["headers"]
    off_entry = viewer_request(uri="/about", extra=[("X-Controlvar-Split", "served")])
    response = router.handle(
        viewer_response(off_entry["Records"][0]["cf"]["request"], "request-2")
    )
    assert "x-split-url" not in response["headers"]
    assert "set-cookie" not in response["headers"]


def test_the_old_configuration_header_is_not_read():
    """V18: a request carrying the old configuration header gets what the API
    answers, never what the header says."""
    name = "X-Split-URL-" + "Config"
    config = json.dumps(
        {
            "experiment_key": "checkout-pages",
            "variants": [
                {
                    "name": "c",
                    "url": "https://elsewhere.example/a",
                    "traffic_allocation": 0,
                },
                {
                    "name": "t",
                    "url": "https://elsewhere.example/b",
                    "traffic_allocation": 100,
                },
            ],
        }
    )
    for assign, location in ((REDIRECT, REDIRECT["location"]), (SERVE, None)):
        api = FakeApi(assign=assign)
        delivery = deliver(make_router(api), viewer_request(extra=[(name, config)]))
        assert delivery.header("Location") == location
        assert len(api.assign_calls()) == 1


# -- the routes table -------------------------------------------------------


def test_the_routes_table_is_kept_for_its_max_age():
    """V17: one fetch within max_age, a second once the clock passes it, both
    phases sharing the loader."""
    api, clock = FakeApi(assign=SERVE), Clock()
    router = make_router(api, clock)
    for i in range(50):
        deliver(
            router, viewer_request(cookies=["controlvar_uid=v"], request_id=f"r{i}")
        )
    assert len([c for c in api.calls if c["url"].endswith("/routes")]) == 1
    clock.now += 61
    deliver(router, viewer_request(cookies=["controlvar_uid=v"]))
    assert len([c for c in api.calls if c["url"].endswith("/routes")]) == 2


@pytest.mark.parametrize("max_age, kept", [(1, 10), (60, 60), (100000, 300)])
def test_max_age_is_clamped(max_age, kept):
    api, clock = FakeApi(max_age=max_age), Clock()
    router = make_router(api, clock)
    router.routes("viewer-request")
    clock.now += kept - 0.5
    router.routes("viewer-request")
    assert len(api.calls) == 1
    clock.now += 1
    router.routes("viewer-request")
    assert len(api.calls) == 2


def test_the_last_good_table_is_kept_on_error(caplog):
    api, clock = FakeApi(), Clock()
    router = make_router(api, clock)
    assert router.routes("viewer-request")
    clock.now += 61
    api.routes = (503, b"")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert list(router.routes("viewer-request")) == [("e.com", "/checkout")]
    assert logged(caplog) == [
        {"event": "split_url_failed", "phase": "viewer-request", "status": "503"}
    ]
    clock.now += 10
    router.routes("viewer-request")
    assert len(api.calls) == 2


def test_a_paused_test_leaves_the_table_after_max_age():
    api, clock = FakeApi(), Clock()
    router = make_router(api, clock)
    assert deliver(router, viewer_request()).status == "302"
    api.routes = {"routes": [], "max_age": 60}
    clock.now += 61
    delivery = deliver(router, viewer_request(request_id="later"))
    assert (delivery.generated, delivery.header("X-Split-URL")) == (None, None)
    assert len(api.assign_calls()) == 1


@pytest.mark.parametrize(
    "routes",
    [
        {"routes": [{"host": "e.com", "path": "/x"}], "max_age": 60},
        {"max_age": 60},
        [1],
    ],
    ids=["route-without-key", "no-routes", "not-an-object"],
)
def test_a_routes_answer_it_cannot_read_is_a_failure(routes, caplog):
    api = FakeApi(routes=())
    api.routes = routes
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        delivery = deliver(make_router(api), viewer_request())
    assert (delivery.generated, delivery.header("X-Split-URL")) == (None, None)
    assert {line["status"] for line in logged(caplog)} == {"unparseable"}


@pytest.mark.regression
def test_viewer_response_in_a_cold_container_sets_the_cookie():
    """EM393-5: viewer response runs in a container that has no routes table
    yet. It loads one through the same loader, and sets the cookie with the
    id viewer request sent."""
    request_api, response_api = FakeApi(assign=SERVE), FakeApi(assign=SERVE)
    cold = make_router(response_api)
    delivery = deliver(make_router(request_api), viewer_request(), response_router=cold)
    assert cookie_id(delivery) == sent_ids(request_api)[0]
    assert [c["url"].rsplit("/", 1)[1] for c in response_api.calls] == ["routes"]
    assert response_api.calls[0]["timeout"] == 1.0


def test_viewer_response_sets_the_cookie_when_the_request_failed():
    """EM393-6: the cookie is set whenever an id was derived, whatever the API
    answered, in the skip window too."""
    api, clock = FakeApi(assign=(500, b"")), Clock()
    router = make_router(api, clock)
    first = deliver(router, viewer_request(request_id="a"))
    second = deliver(router, viewer_request(request_id="b"))
    assert cookie_id(first) == router_module.visitor_id("a")
    assert cookie_id(second) == router_module.visitor_id("b")
    assert len(api.assign_calls()) == 1


def test_a_request_without_a_request_id_is_served(caplog):
    api = FakeApi()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        delivery = deliver(make_router(api), viewer_request(request_id=None))
    assert api.assign_calls() == []
    assert delivery.header("X-Split-URL") == "failed"
    assert delivery.headers("Set-Cookie") == []
    assert [line["status"] for line in logged(caplog)] == ["no-request-id"]


# -- the transport ----------------------------------------------------------


class _Api(http.server.BaseHTTPRequestHandler):
    """Answers every request with a redirect, recording what it was sent."""

    seen: List[tuple] = []

    def do_POST(self):
        self._answer()

    def do_GET(self):
        self._answer()

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).seen.append((self.command, self.path, self.headers.get("X-API-Key")))
        self.send_response(302)
        self.send_header(
            "Location", f"http://127.0.0.1:{self.server.server_port}/elsewhere"
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def _local_server():
    """An HTTP server on 127.0.0.1, on a free port above 55000."""
    rng = random.Random()
    for _ in range(50):
        try:
            return http.server.ThreadingHTTPServer(
                ("127.0.0.1", rng.randint(55001, 64999)), _Api
            )
        except OSError:
            continue
    raise RuntimeError("no free port above 55000")


@pytest.mark.regression
def test_the_router_follows_no_redirect_with_its_key():
    """A 3xx from the API is an answer, not a hop: the key is sent once, to
    the API's URL, and nothing goes to the Location."""
    _Api.seen = []
    server = _local_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/v1/tracking/split-url/assign"
        status, _ = router_module.http_call(
            "POST",
            url,
            {"X-API-Key": "key-1", "Content-Type": "application/json"},
            b"{}",
            1.5,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert status == 302
    assert _Api.seen == [("POST", "/api/v1/tracking/split-url/assign", "key-1")]


def test_the_transport_refuses_another_scheme():
    with pytest.raises(ValueError):
        router_module.http_call("GET", "ftp://e.com/x", {}, None, 1.0)
