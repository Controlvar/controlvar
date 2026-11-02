"""
The CloudFront edge router for split URL experiments (#393). Python standard
library only.

One Lambda@Edge function, associated with a distribution's viewer-request and
viewer-response events. It follows the contract every redirector follows
(``docs/api/split-url.md``, "Set it up in your server or edge"): it keeps the
routes table (``GET /api/v1/tracking/split-url/routes``), and for a GET whose
host and path are an entry it asks ``POST /api/v1/tracking/split-url/assign``
where to send the visitor, then redirects or serves. The API decides; the
router does what it is told and fails open.

Its settings are in ``config.json`` beside this file, written when the edge
app (``modules/infrastructure/split_url_edge``) is synthesised, because
Lambda@Edge takes no environment variables.

* **Viewer request** decides. A redirect is answered here, with its own
  ``Set-Cookie``: CloudFront does not run viewer response for a response a
  viewer-request function generated. Anything else serves the page, with the
  internal request header ``x-controlvar-split`` naming the outcome.
* **Viewer response** sets the visitor-id cookie on an entry page served to a
  visitor who arrived without one, whatever the API answered, and turns
  ``x-controlvar-split`` into ``X-Split-URL``.

A visitor without the cookie gets an id derived from the event's
``config.requestId``, which CloudFront gives both events of one request, so
the cookie set at viewer response holds the id viewer request sent to the
API.
"""

import hashlib
import http.client
import json
import logging
import os
import ssl
import string
import time
import urllib.parse
import uuid

ROUTES_PATH = "/api/v1/tracking/split-url/routes"
ASSIGN_PATH = "/api/v1/tracking/split-url/assign"
ROUTES_TIMEOUT = 1.0
ASSIGN_TIMEOUT = 1.5
SKIP_SECONDS = 30
MIN_MAX_AGE = 10
MAX_MAX_AGE = 300
MAX_QUERY_LENGTH = 8192
MAX_USER_AGENT_LENGTH = 1024
MAX_USER_ID_LENGTH = 255
NOT_ENROLLED = {"holdout", "mutual_exclusion", "targeting", "not_entry"}
#: Every X-Split-URL value. A redirect carries it itself; the other four
#: reach viewer response through the internal header.
OUTCOMES = ("redirected", "served", "not-enrolled", "skipped", "failed")
SERVED_OUTCOMES = frozenset(OUTCOMES[1:])
#: A 4xx about one request: logged, and the next visitor is still asked about.
ONE_REQUEST = {str(status) for status in range(400, 500)} - {"401", "403", "429"}
INTERNAL_PREFIX = "x-controlvar-"
OUTCOME_HEADER = "x-controlvar-split"
#: The settings file the edge app writes beside this one.
CONFIG_FILE = "config.json"

log = logging.getLogger("controlvar.split_url_router")
_tls = None


def _tls_context():
    global _tls
    if _tls is None:
        _tls = ssl.create_default_context()
    return _tls


def http_call(method, url, headers, body, timeout):
    """One request with http.client: (status, body). Raises when there is no
    response. It follows no redirect: a 3xx is an answer like any other, so
    the key goes only to the API's own URL."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(
            parts.hostname, parts.port, timeout=timeout, context=_tls_context()
        )
    elif parts.scheme == "http":
        connection = http.client.HTTPConnection(
            parts.hostname, parts.port, timeout=timeout
        )
    else:
        raise ValueError("the API URL is not http(s)")
    target = parts.path + ("?" + parts.query if parts.query else "")
    try:
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class Failed(Exception):
    """No usable answer: the text is the HTTP status, or a fixed word."""


def entry_of(host, path):
    """A request's host and path as the routes table writes them: the host
    in lower case without a port (an IPv6 literal keeps its brackets), the
    path without one trailing ``/`` except at the root. The same rule as
    ``backend/app/schemas/split_url_config.py``'s ``normalize_host`` and
    ``normalize_path``."""
    host = host.lower()
    if host.startswith("["):
        end = host.find("]")
        host = host[: end + 1] if end != -1 else host
    else:
        host = host.split(":", 1)[0]
    if not path:
        path = "/"
    elif len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return host, path


def storable(text):
    """Whether the API accepts text: no NUL and no lone surrogate."""
    return not any(c == "\x00" or "\ud800" <= c <= "\udfff" for c in text)


def query_to_send(query):
    """The query string when the API accepts it (at most 8192 characters, no
    #, whitespace or control character), else None: the visitor is still
    routed, without it."""
    if not query or len(query) > MAX_QUERY_LENGTH or not storable(query):
        return None
    if any(c == "#" or c.isspace() or ord(c) < 32 or ord(c) == 127 for c in query):
        return None
    return query


def user_agent_to_send(user_agent):
    """The User-Agent's first 1024 characters when the API accepts them, else
    None."""
    user_agent = (user_agent or "")[:MAX_USER_AGENT_LENGTH]
    return user_agent if user_agent and storable(user_agent) else None


def usable(value):
    """Whether a visitor id can be sent as ``user_id`` and set as a cookie:
    1 to 255 printable characters, none of them ``;``."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_USER_ID_LENGTH
        and value.isprintable()
        and ";" not in value
    )


def visitor_id(request_id):
    """The id of a visitor who arrived without the cookie: a one-way function
    of CloudFront's ``config.requestId``, which identifies the same request in
    its viewer-request and viewer-response events. Formatted as a UUID; None
    without a request id."""
    if not isinstance(request_id, str) or not request_id:
        return None
    digest = hashlib.sha256(
        b"controlvar-split-url-visitor:" + request_id.encode("utf-8", "surrogatepass")
    ).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def _first(headers, name):
    """The first value of a header, or ""."""
    entries = headers.get(name) or []
    for item in entries:
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            return item["value"]
    return ""


def _cookie_header(headers):
    """Every Cookie header entry, joined with "; "."""
    return "; ".join(
        item["value"]
        for item in headers.get("cookie") or []
        if isinstance(item, dict) and isinstance(item.get("value"), str)
    )


def _cookies(header):
    cookies = {}
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        cookies.setdefault(name, value)
    return cookies


def _prefetch(headers):
    purpose = _first(headers, "sec-purpose") + _first(headers, "purpose")
    return "prefetch" in purpose.lower()


def _header(name, value):
    return [{"key": name, "value": value}]


def _log_failure(phase, status):
    log.warning(
        json.dumps(
            {"event": "split_url_failed", "phase": phase, "status": status},
            sort_keys=True,
        )
    )


class Router:
    """The router's state in one container: its settings, the routes table
    and the 30-second skip window. ``http`` and ``clock`` are injected by the
    tests."""

    def __init__(self, settings, http=None, clock=None):
        self.api_url = settings["api_url"].rstrip("/")
        self.api_key = settings["api_key"]
        self.cookie_name = settings.get("cookie_name") or "controlvar_uid"
        self.cookie_max_age = int(settings.get("cookie_max_age_days") or 30) * 86400
        self.cookie_domain = settings.get("cookie_domain") or None
        self.http = http if http is not None else http_call
        self.clock = clock if clock is not None else time.monotonic
        self.table = None
        self.expires = 0.0
        self.skip_until = 0.0

    # -- the API ----------------------------------------------------------

    def _call(self, method, path, payload, timeout):
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        try:
            status, data = self.http(
                method, self.api_url + path, headers, body, timeout
            )
        except TimeoutError:
            raise Failed("timeout") from None
        except (OSError, ValueError, http.client.HTTPException):
            raise Failed("unreachable") from None
        if status != 200:
            raise Failed(str(status))
        try:
            return json.loads(data)
        except ValueError:
            raise Failed("unparseable") from None

    def _failed(self, phase, reason, skip=True):
        _log_failure(phase, str(reason))
        if skip:
            self.skip_until = self.clock() + SKIP_SECONDS

    def routes(self, phase):
        """The routes table by (host, path). One loader for both phases: it
        loads on a miss, keeps the table for its ``max_age`` clamped to
        10-300 seconds, keeps the last good table on error, and loads
        nothing for 30 seconds after a failure."""
        now = self.clock()
        if self.table is not None and now < self.expires:
            return self.table
        if now < self.skip_until:
            return self.table or {}
        try:
            reply = self._call("GET", ROUTES_PATH, None, ROUTES_TIMEOUT)
            table = {}
            for route in reply["routes"]:
                key, host, path = route["experiment_key"], route["host"], route["path"]
                if not all(isinstance(v, str) for v in (key, host, path)):
                    raise ValueError("a route that is not text")
                table[(host, path)] = route
            max_age = min(max(int(reply["max_age"]), MIN_MAX_AGE), MAX_MAX_AGE)
        except Failed as failure:
            self._failed(phase, failure)
            return self.table or {}
        except (KeyError, TypeError, ValueError):
            self._failed(phase, "unparseable")
            return self.table or {}
        self.table, self.expires = table, now + max_age
        return table

    def _assign(self, payload):
        reply = self._call("POST", ASSIGN_PATH, payload, ASSIGN_TIMEOUT)
        return reply["action"], reply["location"], reply["reason"]

    # -- the cookie -------------------------------------------------------

    def set_cookie(self, uid):
        cookie = (
            f"{self.cookie_name}={uid}; Max-Age={self.cookie_max_age}; "
            "Path=/; SameSite=Lax; Secure"
        )
        if self.cookie_domain:
            cookie += f"; Domain={self.cookie_domain}"
        return cookie

    def _visitor(self, headers):
        """(the cookie's value or None, whether the request carried it)."""
        cookies = _cookies(_cookie_header(headers))
        if self.cookie_name in cookies:
            return cookies[self.cookie_name], True
        return None, False

    # -- the two phases ---------------------------------------------------

    def _route_of(self, request, headers, phase):
        """The routes-table entry the request is for, or None."""
        uri = request.get("uri")
        if not isinstance(uri, str):
            return None
        return self.routes(phase).get(entry_of(_first(headers, "host"), uri))

    def decide(self, cf):
        """Viewer request, after the internal headers are gone: (a generated
        response, or None) and the outcome (None for a request that is not an
        entry)."""
        phase = "viewer-request"
        request = cf["request"]
        headers = request["headers"]
        route = self._route_of(request, headers, phase)
        if route is None:
            return None, None
        if request.get("method") != "GET" or _prefetch(headers):
            return None, "skipped"
        uid, carried = self._visitor(headers)
        if carried and not usable(uid):
            return None, "skipped"
        cookie = []
        if not carried:
            uid = visitor_id((cf.get("config") or {}).get("requestId"))
            if not usable(uid):
                self._failed(phase, "no-request-id", skip=False)
                return None, "failed"
            cookie = _header("Set-Cookie", self.set_cookie(uid))
        if self.clock() < self.skip_until:
            return None, "failed"
        payload = {
            "experiment_key": route["experiment_key"],
            "user_id": uid,
            "host": route["host"],
            "path": request["uri"],
            "query": query_to_send(request.get("querystring")),
            "user_agent": user_agent_to_send(_first(headers, "user-agent")),
        }
        try:
            action, location, reason = self._assign(payload)
        except Failed as failure:
            self._failed(phase, failure, skip=str(failure) not in ONE_REQUEST)
            return None, "failed"
        except (KeyError, TypeError):
            self._failed(phase, "unparseable")
            return None, "failed"
        if action == "redirect":
            if (
                not isinstance(location, str)
                or "\r" in location
                or "\n" in location
                or not location[:8].lower().startswith(("http://", "https://"))
            ):
                self._failed(phase, "unparseable", skip=False)
                return None, "failed"
            response_headers = {
                "location": _header(
                    "Location", urllib.parse.quote(location, safe=string.punctuation)
                ),
                "cache-control": _header("Cache-Control", "no-store"),
                "x-split-url": _header("X-Split-URL", "redirected"),
            }
            if cookie:
                response_headers["set-cookie"] = cookie
            return {
                "status": "302",
                "statusDescription": "Found",
                "headers": response_headers,
            }, "redirected"
        return None, "not-enrolled" if reason in NOT_ENROLLED else "served"

    def on_request(self, cf):
        """Viewer request: the request to send on, or a redirect. The
        ``x-controlvar-*`` request headers are the router's own, so any the
        request arrived with are removed first, on every request. Never
        raises."""
        request = cf["request"]
        headers = request.get("headers")
        if not isinstance(headers, dict):
            headers = {}
            request["headers"] = headers
        for name in [n for n in headers if n.lower().startswith(INTERNAL_PREFIX)]:
            del headers[name]
        try:
            response, outcome = self.decide(cf)
        except Exception:
            _log_failure("viewer-request", "error")
            return request
        if response is not None:
            return response
        if outcome is not None:
            headers[OUTCOME_HEADER] = _header("X-Controlvar-Split", outcome)
        return request

    def on_response(self, cf):
        """Viewer response: the origin's response, with X-Split-URL and, for
        a visitor who arrived without the cookie, Set-Cookie. Never raises."""
        response = cf["response"]
        try:
            request = cf.get("request") or {}
            headers = request.get("headers") or {}
            if self._route_of(request, headers, "viewer-response") is None:
                return response
            out = response.get("headers")
            if not isinstance(out, dict):
                out = {}
                response["headers"] = out
            outcome = _first(headers, OUTCOME_HEADER)
            if outcome in SERVED_OUTCOMES:
                out["x-split-url"] = _header("X-Split-URL", outcome)
            if request.get("method") != "GET" or _prefetch(headers):
                return response
            _, carried = self._visitor(headers)
            if carried:
                return response
            uid = visitor_id((cf.get("config") or {}).get("requestId"))
            if usable(uid):
                out.setdefault("set-cookie", []).append(
                    {"key": "Set-Cookie", "value": self.set_cookie(uid)}
                )
        except Exception:
            _log_failure("viewer-response", "error")
        return response

    def handle(self, event):
        """One Lambda@Edge event, of either kind."""
        cf = event["Records"][0]["cf"]
        if cf["config"]["eventType"] == "viewer-response":
            return self.on_response(cf)
        return self.on_request(cf)


_router = None


def _settings():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILE)
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def handler(event, context):
    """The Lambda@Edge entry point, for both events. It never raises: without
    usable settings, or on an event it cannot read, it passes the request or
    the response on as it came, with no ``x-controlvar-*`` header."""
    global _router
    try:
        if _router is None:
            _router = Router(_settings())
        return _router.handle(event)
    except Exception:
        _log_failure("setup", "error")
    try:
        cf = event["Records"][0]["cf"]
        if "response" in cf:
            return cf["response"]
        headers = cf["request"].get("headers") or {}
        for name in [n for n in headers if n.lower().startswith(INTERNAL_PREFIX)]:
            del headers[name]
        return cf["request"]
    except Exception:
        return event
