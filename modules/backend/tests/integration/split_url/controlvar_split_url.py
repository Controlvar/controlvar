"""Send each visitor who opens a split URL experiment's control page to their
variant, with Controlvar's split URL routes. Python standard library only.

SplitUrl.answer() decides one request; SplitUrlMiddleware applies it to an
ASGI application; SplitUrl.track() sends a conversion to every split URL
experiment the visitor is in.
"""

import asyncio
import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

COOKIE_NAME = "controlvar_uid"
COOKIE_MAX_AGE_DAYS = 30
ROUTES_TIMEOUT = 1.0
ASSIGN_TIMEOUT = 1.5
SKIP_SECONDS = 30
NOT_ENROLLED = {"holdout", "mutual_exclusion", "targeting", "not_entry"}
MAX_QUERY_LENGTH = 8192
MAX_USER_AGENT_LENGTH = 1024
# A 4xx about one request: logged, and the next visitor is still asked about.
ONE_REQUEST = {str(status) for status in range(400, 500)} - {"401", "403", "429"}

log = logging.getLogger("controlvar.split_url")


def http_call(method, url, headers, body, timeout):
    """One HTTP request: (status, body). Raises when there is no response."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class Failed(Exception):
    """No usable answer: the text is the HTTP status, or a fixed word."""


class Answer:
    """A redirect (status 302 and its headers), or the page with headers added."""

    def __init__(self, outcome, status=None, headers=(), entry=False):
        self.outcome = outcome
        self.status = status
        self.headers = list(headers)
        self.entry = entry

    def page_headers(self, headers):
        """The page's own (name, value) headers, with ours added."""
        headers = list(headers)
        if self.entry:
            own = [v for n, v in headers if n.lower() == "cache-control"]
            headers = [(n, v) for n, v in headers if n.lower() != "cache-control"]
            if any("no-store" in v.lower() for v in own):
                headers += [("Cache-Control", v) for v in own]
            else:
                headers.append(("Cache-Control", "private"))
        return headers + self.headers


def entry_of(host, raw_path):
    """A request's host and path as the routes table writes them."""
    host = host.lower()
    if host.startswith("["):
        host = host[: host.find("]") + 1]
    else:
        host = host.split(":")[0]
    if len(raw_path) > 1 and raw_path.endswith("/"):
        raw_path = raw_path[:-1]
    return host, raw_path


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


class SplitUrl:
    def __init__(
        self, api_url, api_key, cookie_domain=None, http=http_call, clock=time.monotonic
    ):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.cookie_domain = cookie_domain
        self.http = http
        self.clock = clock
        self.table = None
        self.expires = 0.0
        self.skip_until = 0.0

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
        except urllib.error.URLError as error:
            word = (
                "timeout" if isinstance(error.reason, TimeoutError) else "unreachable"
            )
            raise Failed(word) from None
        except (OSError, ValueError, http.client.HTTPException):
            raise Failed("unreachable") from None
        if status != 200:
            raise Failed(str(status))
        try:
            return json.loads(data)
        except ValueError:
            raise Failed("unparseable") from None

    def _failed(self, reason, skip=True):
        log.warning("split_url_failed status=%s", reason)
        if skip:
            self.skip_until = self.clock() + SKIP_SECONDS

    def routes(self):
        """The routes table by (host, path), fetched again after its max_age."""
        now = self.clock()
        if self.table is not None and now < self.expires:
            return self.table
        if now < self.skip_until:
            return self.table or {}
        try:
            reply = self._call(
                "GET", "/api/v1/tracking/split-url/routes", None, ROUTES_TIMEOUT
            )
            table = {(r["host"], r["path"]): r for r in reply["routes"]}
            max_age = min(max(int(reply["max_age"]), 10), 300)
        except Failed as failure:
            self._failed(failure)
            return self.table or {}
        except (KeyError, TypeError, ValueError):
            self._failed("unparseable")
            return self.table or {}
        self.table, self.expires = table, now + max_age
        return table

    def may_split(self, host, raw_path):
        """False when the routes table in hand is current and has no entry for
        the request, which then needs no call and no thread."""
        if self.table is not None and self.clock() < self.expires:
            return entry_of(host, raw_path) in self.table
        return True

    def _cookie(self, value):
        cookie = (
            f"{COOKIE_NAME}={value}; Max-Age={COOKIE_MAX_AGE_DAYS * 86400}; "
            "Path=/; SameSite=Lax; Secure"
        )
        if self.cookie_domain:
            cookie += f"; Domain={self.cookie_domain}"
        return cookie

    def answer(
        self,
        method,
        host,
        raw_path,
        query,
        cookie_header="",
        user_agent=None,
        prefetch=False,
        consented=True,
    ):
        """Decide one request. raw_path is the path as the visitor sent it, not
        percent-decoded; cookie_header joins every Cookie header with "; "."""
        route = self.routes().get(entry_of(host, raw_path))
        if route is None:
            return Answer("not-routed")
        skipped = Answer(
            "skipped", headers=[("X-Split-URL", "skipped")], entry=method == "GET"
        )
        if method != "GET" or prefetch or not consented:
            return skipped
        cookies = {}
        for part in (cookie_header or "").split(";"):
            name, _, value = part.strip().partition("=")
            cookies.setdefault(name, value)
        uid = cookies.get(COOKIE_NAME)
        if uid is not None and not (0 < len(uid) <= 255 and uid.isprintable()):
            return skipped
        cookie = []
        if uid is None:
            uid = str(uuid.uuid4())
            cookie = [("Set-Cookie", self._cookie(uid))]
        failed = Answer(
            "failed", headers=cookie + [("X-Split-URL", "failed")], entry=True
        )
        if self.clock() < self.skip_until:
            return failed
        payload = {
            "experiment_key": route["experiment_key"],
            "user_id": uid,
            "host": route["host"],
            "path": raw_path,
            "query": query_to_send(query),
            "user_agent": user_agent_to_send(user_agent),
        }
        try:
            reply = self._call(
                "POST", "/api/v1/tracking/split-url/assign", payload, ASSIGN_TIMEOUT
            )
            action, location, reason = (
                reply["action"],
                reply["location"],
                reply["reason"],
            )
        except Failed as failure:
            self._failed(failure, skip=str(failure) not in ONE_REQUEST)
            return failed
        except (KeyError, TypeError):
            self._failed("unparseable")
            return failed
        if action == "redirect":
            if (
                not isinstance(location, str)
                or "\r" in location
                or "\n" in location
                or not location[:8].lower().startswith(("http://", "https://"))
            ):
                self._failed("unparseable", skip=False)
                return failed
            return Answer(
                "redirected",
                status=302,
                headers=[
                    ("Location", location),
                    ("Cache-Control", "no-store"),
                    *cookie,
                    ("X-Split-URL", "redirected"),
                ],
            )
        outcome = "not-enrolled" if reason in NOT_ENROLLED else "served"
        return Answer(outcome, headers=cookie + [("X-Split-URL", outcome)], entry=True)

    def track(self, uid, event_name, value=None):
        """Send a conversion to each split URL experiment the visitor is in, and
        answer how many that was."""
        ids = {route["experiment_id"] for route in self.routes().values()}
        quoted = urllib.parse.quote(uid, safe="")
        assignments = self._call(
            "GET", f"/api/v1/tracking/assignments/{quoted}", None, 5.0
        )
        sent = 0
        for assignment in assignments:
            if assignment["experiment_id"] not in ids:
                continue
            event = {
                "event_name": event_name,
                "event_type": event_name,
                "user_id": uid,
                "experiment_id": assignment["experiment_id"],
                "variant_id": assignment["variant_id"],
                "value": value,
            }
            self._call("POST", "/api/v1/tracking/events", event, 5.0)
            sent += 1
        return sent


class SplitUrlMiddleware:
    """ASGI middleware. It reads scope["raw_path"], the path as the visitor sent
    it; a server that does not give one is served as it is, with nothing added."""

    def __init__(self, app, split_url, consented=lambda scope: True):
        self.app = app
        self.split_url = split_url
        self.consented = consented

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("raw_path"):
            return await self.app(scope, receive, send)
        headers = [
            (n.decode("latin-1").lower(), v.decode("latin-1"))
            for n, v in scope["headers"]
        ]
        first = dict(reversed(headers))
        host = first.get("host", "")
        raw_path = scope["raw_path"].decode("latin-1").split("?")[0]
        if not self.split_url.may_split(host, raw_path):
            return await self.app(scope, receive, send)
        purpose = first.get("sec-purpose", "") + first.get("purpose", "")
        try:
            answer = await asyncio.to_thread(
                self.split_url.answer,
                scope["method"],
                host,
                raw_path,
                scope["query_string"].decode("latin-1"),
                "; ".join(v for n, v in headers if n == "cookie"),
                first.get("user-agent"),
                "prefetch" in purpose.lower(),
                self.consented(scope),
            )
        except Exception:
            log.warning("split_url_failed status=error")
            return await self.app(scope, receive, send)
        if answer.status == 302:
            encoded = [
                (n.encode("latin-1"), v.encode("latin-1")) for n, v in answer.headers
            ]
            await send(
                {"type": "http.response.start", "status": 302, "headers": encoded}
            )
            return await send({"type": "http.response.body", "body": b""})

        async def send_with_ours(message):
            if message["type"] == "http.response.start":
                page = [
                    (n.decode("latin-1"), v.decode("latin-1"))
                    for n, v in message.get("headers", [])
                ]
                message = dict(
                    message,
                    headers=[
                        (n.encode("latin-1"), v.encode("latin-1"))
                        for n, v in answer.page_headers(page)
                    ],
                )
            await send(message)

        return await self.app(scope, receive, send_with_ours)


if __name__ == "__main__":
    import os
    import sys

    logging.basicConfig(format="%(message)s")
    split_url = SplitUrl(
        os.environ["CONTROLVAR_API_URL"], os.environ["CONTROLVAR_API_KEY"]
    )
    if sys.argv[1] == "track":
        print("sent to", split_url.track(sys.argv[2], sys.argv[3]), "experiment(s)")
    else:
        method, host, path, query, cookie = (sys.argv[1:] + [""] * 5)[:5]
        answer = split_url.answer(method, host, path, query, cookie)
        print(answer.status or 200, answer.outcome)
        for name, value in answer.headers:
            print(f"{name}: {value}")
