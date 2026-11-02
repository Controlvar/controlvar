"""A simulated CloudFront for the split URL router's tests (#393).

The router (``modules/lambda/split_url_router/handler.py``) is loaded by file
path, as Lambda@Edge loads it, and driven with events built from the samples
AWS documents in "Lambda@Edge event structure" (Amazon CloudFront Developer
Guide): the viewer-request and viewer-response examples, whose
``config.requestId``, ``clientIp``, headers and ``querystring`` this copies.
As in that sample, a viewer-request event carries no ``X-Forwarded-For``.

``deliver`` plays CloudFront's part: it runs the viewer-request phase, and,
unless that phase answered itself (CloudFront does not run viewer response
for a response a viewer-request function generated), sends the request on
to a page and runs the viewer-response phase with the same ``requestId``.

**PE-1, assumed here:** the request the viewer-response phase sees is the
request the viewer-request phase returned, so a header added there is
visible there. AWS does not document this. ``pe1=False`` runs the response
phase with the request as the viewer sent it instead; the visitor-id cookie
does not rest on PE-1, and the tests that pass ``pe1=False`` hold that.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[5]
ROUTER_FILE = ROOT / "modules" / "lambda" / "split_url_router" / "handler.py"
VECTORS = json.loads(
    (
        ROOT
        / "modules"
        / "backend"
        / "tests"
        / "integration"
        / "split_url"
        / "vectors.json"
    ).read_text(encoding="utf-8")
)

#: The documented sample's request id.
SAMPLE_REQUEST_ID = "4TyzHTaYWb1GX1qTfsHhEqV6HUDd_BzoBZnwfnvQc_1oF26ClkoUSEQ=="
DISTRIBUTION = {
    "distributionDomainName": "d111111abcdef8.cloudfront.net",
    "distributionId": "EDFDVBD6EXAMPLE",
}
SETTINGS = {
    "api_url": "https://api.example.com",
    "api_key": "key-1",
    "cookie_name": "controlvar_uid",
    "cookie_max_age_days": 30,
    "cookie_domain": None,
}
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


def load_router(name: str = "split_url_router_under_test", path: Path = ROUTER_FILE):
    """The router module, loaded from its file, as Lambda@Edge loads it."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


router_module = load_router()


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


def make_router(api, clock: Optional[Clock] = None, **settings):
    return router_module.Router(
        {**SETTINGS, **settings}, http=api, clock=clock or Clock()
    )


def _headers(pairs: Sequence[tuple]) -> Dict[str, List[Dict[str, str]]]:
    headers: Dict[str, List[Dict[str, str]]] = {}
    for name, value in pairs:
        headers.setdefault(name.lower(), []).append({"key": name, "value": value})
    return headers


def viewer_request(
    uri: str = "/checkout",
    querystring: str = "utm_source=x",
    host: str = "e.com",
    method: str = "GET",
    cookies: Sequence[str] = (),
    user_agent: str = "curl/7.66.0",
    request_id: Optional[str] = SAMPLE_REQUEST_ID,
    client_ip: str = "203.0.113.178",
    extra: Sequence[tuple] = (),
) -> Dict[str, Any]:
    """A viewer-request event in the documented sample's shape."""
    pairs = [("Host", host), ("User-Agent", user_agent), ("accept", "*/*")]
    pairs += [("Cookie", value) for value in cookies]
    pairs += list(extra)
    config = {**DISTRIBUTION, "eventType": "viewer-request"}
    if request_id is not None:
        config["requestId"] = request_id
    return {
        "Records": [
            {
                "cf": {
                    "config": config,
                    "request": {
                        "clientIp": client_ip,
                        "headers": _headers(pairs),
                        "method": method,
                        "querystring": querystring,
                        "uri": uri,
                    },
                }
            }
        ]
    }


#: The documented viewer-response sample's origin headers (a selection).
ORIGIN_HEADERS = (
    ("Content-Type", "text/html; charset=utf-8"),
    ("Content-Length", "9593"),
    ("Server", "ExampleCustomOriginServer"),
)


def viewer_response(
    request: Dict[str, Any],
    request_id: Optional[str],
    origin_headers: Sequence[tuple] = ORIGIN_HEADERS,
) -> Dict[str, Any]:
    """A viewer-response event in the documented sample's shape."""
    config = {**DISTRIBUTION, "eventType": "viewer-response"}
    if request_id is not None:
        config["requestId"] = request_id
    return {
        "Records": [
            {
                "cf": {
                    "config": config,
                    "request": request,
                    "response": {
                        "headers": _headers(origin_headers),
                        "status": "200",
                        "statusDescription": "OK",
                    },
                }
            }
        ]
    }


class Delivery:
    """What CloudFront did with one request: the generated response, or the
    request it sent to the origin and the response the viewer got."""

    def __init__(self, generated=None, forwarded=None, response=None) -> None:
        self.generated = generated
        self.forwarded = forwarded
        self.response = response

    @property
    def status(self) -> str:
        return (self.generated or self.response)["status"]

    def headers(self, name: str) -> List[str]:
        """Every value of a response header the viewer got."""
        source = self.generated or self.response
        return [item["value"] for item in source["headers"].get(name.lower(), [])]

    def header(self, name: str) -> Optional[str]:
        values = self.headers(name)
        assert len(values) <= 1, (name, values)
        return values[0] if values else None


def deliver(
    router,
    event: Dict[str, Any],
    *,
    pe1: bool = True,
    response_router=None,
    origin_headers: Sequence[tuple] = ORIGIN_HEADERS,
) -> Delivery:
    """One request through CloudFront, the router at both viewer events.
    ``response_router`` runs the viewer-response phase in another container
    (by default the same one)."""
    event = copy.deepcopy(event)
    as_sent = copy.deepcopy(event["Records"][0]["cf"]["request"])
    request_id = event["Records"][0]["cf"]["config"].get("requestId")
    answer = router.handle(event)
    if "status" in answer:
        return Delivery(generated=answer)
    seen = copy.deepcopy(answer) if pe1 else as_sent
    response = (response_router or router).handle(
        viewer_response(seen, request_id, origin_headers)
    )
    return Delivery(forwarded=answer, response=response)


def cookie_id(delivery: Delivery, name: str = "controlvar_uid") -> Optional[str]:
    """The visitor id a Set-Cookie of the viewer's response carries."""
    for value in delivery.headers("Set-Cookie"):
        if value.startswith(name + "="):
            return value.split(";", 1)[0].split("=", 1)[1]
    return None
