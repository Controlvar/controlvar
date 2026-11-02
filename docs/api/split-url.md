# Split URL testing

!!! info "Part of the `split_url` module"
    Split URL testing is one of the optional modules -- present in the **full profile**, absent from the core one. A core deployment does not serve these routes. See [Modules and profiles](../getting-started/modules.md) for what each profile includes and how to run the full one.

A split URL experiment gives each variant its own page. When a visitor asks for the control's URL, your server or edge asks Controlvar which variant the visitor is in and, for a treatment, redirects them to that variant's URL. The visitor keeps their variant on later visits, and conversions tracked with their id and the experiment count for the variant they saw.

Creating a split URL experiment is an API call, shown on this page. Starting, pausing and ending it, and reading its results, are in the dashboard, as for any experiment. Nothing sends a visitor anywhere until your server or edge asks Controlvar for each one: an active experiment with nothing installed in front of the site shows only as zero visitors in its results.

!!! warning "The CloudFront edge router does not split traffic yet"
    The CloudFront edge router does not split traffic yet; use your server or edge, as this
    page shows ([#393](https://github.com/Controlvar/controlvar/issues/393)).

---

## Where the split runs

| Deployment | Split URL experiments | What sends each visitor to their variant |
|---|---|---|
| Docker Compose | Full profile only (the default is the core profile) | Your server or edge |
| Helm chart | Full profile only: `profile: full` (the default is `core`) | Your server or edge |
| AWS CDK app | Yes: a full checkout deploys the full profile | Your server or edge |
| AWS installer (beta) | No: it installs the core profile, which answers `501` to creating one | Nothing |
| Development checkout | Yes | Your server or edge |

---

## Create and start one

The commands on this page run as written against the full profile's stack (see
[Modules and profiles](../getting-started/modules.md)), in one terminal, top to bottom.
Each one uses the shell variables set by the ones before it. Log in, check that split URL
testing is installed, and make an API key for your server or edge:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)
KEY=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "split URL redirect"}' | jq -r .key)

curl -s localhost:8000/api/v1/modules | jq -c .modules
```
<!-- expect: "split_url" -->

It prints the installed modules, `"split_url"` among them.

Create the experiment with its variants and one URL per variant. The URL variants are named
like the experiment's variants, with the same `traffic_allocation`, and the control's URL is
the page visitors ask for. Then start it:

```{.bash exec}
EXP_ID=$(curl -s -X POST localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "name": "Checkout pages",
    "key": "checkout-pages",
    "experiment_type": "split_url",
    "variants": [
      {"name": "Control", "is_control": true, "traffic_allocation": 50},
      {"name": "Treatment", "is_control": false, "traffic_allocation": 50}
    ],
    "metrics": [
      {"name": "Purchase", "event_name": "purchase", "metric_type": "conversion", "is_primary": true}
    ],
    "split_url_config": {"variants": [
      {"name": "Control", "url": "https://shop.example.com/checkout", "traffic_allocation": 50},
      {"name": "Treatment", "url": "https://shop.example.com/checkout-v2", "traffic_allocation": 50}
    ]}
  }' | jq -r .id)

curl -s -X POST localhost:8000/api/v1/experiments/$EXP_ID/start \
  -H "Authorization: Bearer $TOKEN" | jq -r .status
```
<!-- expect: active -->

It prints `active`.

---

## Send a visitor to their variant

Your server or edge first fetches the routes table: the control page of every active split
URL experiment, as its host and path. It keeps the table for `max_age` seconds:

```{.bash exec}
curl -s localhost:8000/api/v1/tracking/split-url/routes -H "X-API-Key: $KEY" \
  | jq -c '.routes[] | {experiment_key, host, path}'
```
<!-- expect: {"experiment_key":"checkout-pages","host":"shop.example.com","path":"/checkout"} -->

It prints `{"experiment_key":"checkout-pages","host":"shop.example.com","path":"/checkout"}`.

For a request whose host and path are an entry, it asks where to send the visitor, with the
visitor's id, and the host, path and query string they asked for:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/tracking/split-url/assign \
  -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"experiment_key": "checkout-pages", "user_id": "visitor-2", "host": "shop.example.com", "path": "/checkout", "query": "utm_source=mail"}' \
  | jq -c '{action, location, reason}'
```
<!-- expect: {"action":"redirect","location":"https://shop.example.com/checkout-v2?utm_source=mail","reason":"assigned"} -->

`visitor-2` is in Treatment, so it prints
`{"action":"redirect","location":"https://shop.example.com/checkout-v2?utm_source=mail","reason":"assigned"}`:
answer `302` with that `Location`. Asked again, the API gives the same answer, because the
assignment is stored:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/tracking/split-url/assign \
  -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"experiment_key": "checkout-pages", "user_id": "visitor-2", "host": "shop.example.com", "path": "/checkout", "query": "utm_source=mail"}' \
  | jq -r .location
```
<!-- expect: https://shop.example.com/checkout-v2?utm_source=mail -->

It prints `https://shop.example.com/checkout-v2?utm_source=mail` again. A visitor in the
control gets `"action": "serve"`: serve the page they asked for.

The assign route gives each visitor the variant `POST /api/v1/tracking/assign` gives them,
through that route's own code: one assignment row, one exposure on each visit, and the global
holdout, mutual exclusion groups, targeting rules and bandit weights, as for any experiment.
A visitor the global holdout, a mutual exclusion group or the targeting rules keep out is
served the page they asked for, and nothing is recorded for them.

### `GET /api/v1/tracking/split-url/routes` (beta)

Takes an API key in `X-API-Key` and no parameters. The answer is
`{"routes": [{"experiment_key", "experiment_id", "host", "path"}], "max_age": 60}`, with
`Cache-Control: private, max-age=60`, ordered by host, then path; with no entry it is
`{"routes": [], "max_age": 60}`.

- `host` is the control URL's host name, in lower case, and `path` is its path without one
  trailing `/` (except `/` itself).
- It lists every active split URL experiment that has a `key` and a `split_url_config` that
  passes the [Validation Rules](#validation-rules).
- An experiment whose stored configuration breaks the rules is left out, with one warning in
  the API's log.
- Taking the experiments in `start_date` order (ties by the lower id), one whose control page
  an experiment listed before it has, or that would chain with one, is left out too, with
  one warning: the rules `/start` refuses with. That covers a scheduled start or resume, and
  two starts at the same moment, which `/start` does not check against each other.

### `POST /api/v1/tracking/split-url/assign` (beta)

Takes an API key in `X-API-Key`, and the body of `POST /api/v1/tracking/assign` plus the
request the visitor made:

| Field | Required | Description |
|---|---|---|
| `experiment_key` | Yes | The experiment's key, from the routes table (1-100 characters) |
| `user_id` | Yes | The visitor's id (1-255 characters) |
| `context` | No | Attributes for the experiment's targeting rules, as for `POST /api/v1/tracking/assign` |
| `host` | Yes | The host the visitor asked for, with or without a port (no whitespace or control characters, at most 261 characters) |
| `path` | Yes | The path they asked for, as they sent it: starting with `/`, at most 2048 characters |
| `query` | No | Their query string, without the `?`, as they sent it (no `#`, whitespace or control characters, at most 8192 characters) |
| `user_agent` | No | Their User-Agent (at most 1024 characters) |

The answer is `{experiment_key, user_id, action, location, variant_id, variant_name, assigned, reason}`:

- `action` is `redirect` (answer `302` with `Location` set to `location`) or `serve` (serve
  the page they asked for);
- `location` is the variant's URL on `redirect`, otherwise `null`;
- `assigned` is `true` when the visitor holds an assignment in the experiment;
- `reason` is one of the reasons below.

| `reason` | When |
|---|---|
| `assigned` | The visitor holds an assignment; `redirect` for a treatment, `serve` for the control |
| `holdout` | A new visitor the global holdout keeps out; served, nothing recorded |
| `mutual_exclusion` | A new visitor a mutual exclusion group gives to another experiment; served, nothing recorded |
| `targeting` | A new visitor the experiment's targeting rules exclude; served, nothing recorded |
| `draft` | The experiment is a draft; everyone is served, nothing recorded |
| `paused` | The experiment is paused; everyone is served, nothing recorded |
| `completed` | The experiment is completed; everyone is served, nothing recorded |
| `archived` | The experiment is archived; everyone is served, nothing recorded |
| `not_entry` | The host and path are not the control URL's; served, nothing recorded |
| `bot` | A crawler; answered as a new visitor with that id would be, and nothing recorded |

The route answers in this order: `404` for a key no experiment has; `400` for an experiment
that is not a split URL experiment, or has no `split_url_config`; `409` for a stored
`split_url_config` that breaks the rules; then `not_entry`; then, for an active experiment,
`bot`; then the assignment. A visitor whose stored assignment is a treatment is sent there on
every visit, whatever the traffic split is now.

A crawler is a User-Agent whose first 512 characters contain, in any case, one of:
`googlebot`, `bingbot`, `yandexbot`, `duckduckbot`, `applebot`, `slurp`,
`facebookexternalhit`, `twitterbot`, `linkedinbot`, `ahrefsbot`, `semrushbot`, `petalbot`,
`crawler`, `spider`.

---

## Set it up in your server or edge

Install this once, in front of the site. After that, starting, pausing and ending a test
changes nothing in your code. Whatever does the redirect follows these rules:

1. Fetch `GET /api/v1/tracking/split-url/routes` and keep it for its `max_age`. Keep the last
   table you had if a fetch fails.
2. Split only a `GET` (not a `HEAD`, another method, or a prefetch, which a `Sec-Purpose` or
   `Purpose: prefetch` header marks) whose host and path are an entry: the Host header in
   lower case without a port, and the path as the visitor sent it, not percent-decoded, without
   one trailing `/`.
3. Read the visitor-id cookie, or make up an id for a visitor without one (see
   [The visitor-id cookie](#the-visitor-id-cookie)).
4. Ask `POST /api/v1/tracking/split-url/assign` with the key from the table, the visitor's id,
   the entry's host, the path they sent, and their query string and User-Agent. Send only what
   the API accepts: a query string longer than 8192 characters, or with a `#`, whitespace or a
   control character, is left out, and the visitor is still sent to their variant.
5. On `redirect`, answer `302` with `Location` and `Cache-Control: no-store`. On `serve`, serve
   the page they asked for.
6. On anything else (no answer within 1.5 seconds, a status other than `200`, or an answer that
   is not JSON) serve the page they asked for and log the HTTP status. Then stop asking for 30
   seconds, unless the status is a `4xx` other than `401`, `403` and `429`, which refuses only
   that request.

### A reference integration

This one is in the Python standard library: `SplitUrl.answer()` decides one request, and
`SplitUrlMiddleware` applies it to any ASGI application (FastAPI, Starlette, Django's ASGI
handler). Save it as `controlvar_split_url.py` (here, in a new, empty directory):

```{.bash exec}
cd "$(mktemp -d)"
cat > controlvar_split_url.py <<'PY'
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
import string
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
                    ("Location", urllib.parse.quote(location, safe=string.punctuation)),
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
PY
```

In an ASGI application, wrap it once: `app = SplitUrlMiddleware(app, SplitUrl(API_URL, API_KEY))`.
It compares the path the visitor sent (`scope["raw_path"]`), because the decoded path is not
what the routes table holds; a server that does not give the raw path is served as it is. In
another framework, call `SplitUrl.answer()` before you serve the page, with the path as the
visitor sent it: answer its redirect, or add `Answer.page_headers()` to the page's headers.

Run from the command line, it prints what it would send for one request. For `visitor-2`,
whose cookie the site already set:

```{.bash exec}
CONTROLVAR_API_URL=http://localhost:8000 CONTROLVAR_API_KEY=$KEY python3 controlvar_split_url.py \
  GET shop.example.com /checkout utm_source=mail controlvar_uid=visitor-2
```
<!-- expect: 302 redirected -->
<!-- expect: Location: https://shop.example.com/checkout-v2?utm_source=mail -->
<!-- expect: Cache-Control: no-store -->

It prints `302 redirected`, then `Location: https://shop.example.com/checkout-v2?utm_source=mail`
and `Cache-Control: no-store`. A page the integration serves for an entry carries
`Cache-Control: private`, replacing the page's own unless that is `no-store`, so that a shared
cache does not keep one visitor's page for the next; it also carries `X-Split-URL` with what
it did (`redirected`, `served`, `not-enrolled`, `skipped` or `failed`). Put the integration in
front of any shared cache, such as a CDN or a framework's page cache: a cache in front of it
answers later visitors without it seeing them, so nothing splits.

---

## Count conversions

A conversion counts for a variant only when it is tracked with the same visitor id **and**
names the experiment: `experiment_key` on `POST /api/v1/tracking/track`, or the experiment and
variant ids on `POST /api/v1/tracking/events`. Today's JavaScript SDK sends a `track()` without
an experiment key only to experiments its own client answered in the last 24 hours, so it does
not reach a split URL experiment: send the conversion one of these two ways.

**Once for the whole site, with no change for each test.** On your server, for the visitor's
id (the visitor-id cookie):

1. take the `experiment_id` values of the routes table, which the integration already holds;
2. `GET /api/v1/tracking/assignments/{visitor id}`, which lists every active experiment the
   visitor is assigned in;
3. keep only the assignments whose `experiment_id` is in the routes table;
4. `POST /api/v1/tracking/events` for each, with its `experiment_id` and `variant_id`.

The filter in step 3 matters: the assignments answer carries no experiment type, so without
it each conversion also goes to every other experiment the visitor is in, and a count or value
metric there counts it twice. `SplitUrl.track()` in the reference integration does exactly
this; send `visitor-2`'s purchase with it:

```{.bash exec}
CONTROLVAR_API_URL=http://localhost:8000 CONTROLVAR_API_KEY=$KEY python3 controlvar_split_url.py \
  track visitor-2 purchase
```
<!-- expect: sent to 1 experiment(s) -->

It prints `sent to 1 experiment(s)`. The results count it for the variant `visitor-2` saw:

```{.bash exec}
curl -s "localhost:8000/api/v1/results/$EXP_ID?use_cache=false" -H "Authorization: Bearer $TOKEN" \
  | jq -c '[.metrics[] | select(.is_primary) | .variants[] | {variant_name, visitors: .sample_size, conversions}] | sort_by(.variant_name)'
```
<!-- expect: [{"variant_name":"Control","visitors":0,"conversions":0},{"variant_name":"Treatment","visitors":1,"conversions":1}] -->

It prints
`[{"variant_name":"Control","visitors":0,"conversions":0},{"variant_name":"Treatment","visitors":1,"conversions":1}]`:
one visitor and one conversion, in the variant `visitor-2` saw.

**Or name the test in each call.** Put the experiment's key in `POST /api/v1/tracking/track`;
a site that does this changes the call for each test:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/tracking/track \
  -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"event_type": "purchase", "user_id": "visitor-2", "experiment_key": "checkout-pages"}' \
  | jq -c '{event_name, counted_for_a_variant: (.variant_id != null)}'
```
<!-- expect: {"event_name":"purchase","counted_for_a_variant":true} -->

It prints `{"event_name":"purchase","counted_for_a_variant":true}`.

---

## Check your setup

If an active split URL experiment shows no visitors in its results, nothing is asking
Controlvar for them. Check, in order:

1. The routes table lists the experiment, with the host and path your server sees: the Host
   header in lower case (without a `www` the control URL does not have, and not a CDN's or
   proxy's own name), and the path exactly as the visitor sends it, without a locale prefix
   or `/index.html` the control URL does not have.
2. An entry page your server serves carries an `X-Split-URL` header. None means the request
   did not match an entry; `failed` means the API did not answer, and the integration's log
   says why, with the HTTP status (`401` for a revoked key, `429` for the rate limit).
3. Conversions are sent with the visitor-id cookie's value as `user_id`, and name the
   experiment, as in [Count conversions](#count-conversions).

---

## What visitors see

| Visitor | What happens |
|---|---|
| First visit, in the control | The page they asked for, with the visitor-id cookie set |
| First visit, in a treatment | A `302` to the treatment's URL, with the cookie set |
| Later visits | The same variant: their assignment is stored |
| Draft, paused or completed experiment | The page they asked for; nothing recorded. Assigned visitors get their variant again when it is resumed |
| Kept out by the global holdout, a mutual exclusion group or targeting rules | The page they asked for; nothing recorded |
| A crawler | What a new visitor with its id would see; nothing recorded |
| Cookies blocked | Each visit is a new visitor, with a new assignment |
| A treatment's URL opened directly | That page, whatever their assignment: only the control's URL is split |
| A request that is not a `GET`, or a prefetch | The page they asked for |
| The API does not answer in time, or answers an error | The page they asked for, with the cookie set if it is their first visit |

Clients without cookies count as a new visitor on each visit, and cannot be sent round in a
loop: only the control's URL is ever redirected, and the [Validation Rules](#validation-rules)
refuse the path forms a browser rewrites.

Starting or stopping a test reaches visitors within the routes table's `max_age` (60
seconds) plus however long your integration keeps a copy. A paused or completed test is not
in the table, so nothing is sent to it.

---

## Query strings and paths

The visitor's query string goes only to variants on the control URL's host; a variant on
another host gets its stored URL alone, so put any campaign parameter it needs in the stored
URL. Nothing from the request's own path or host is added.

| Control URL | Variant URL | The visitor asks for | They are sent to |
|---|---|---|---|
| `https://e.com/checkout` | `https://e.com/checkout-v2` | `e.com/checkout?utm_source=x` | `https://e.com/checkout-v2?utm_source=x` |
| `https://e.com/checkout` | `https://e.com/checkout-v2?a=1` | `e.com/checkout?utm_source=x` | `https://e.com/checkout-v2?a=1&utm_source=x` |
| `https://e.com/checkout` | `https://e.com:8443/checkout-v2` | `e.com/checkout?utm_source=x` | `https://e.com:8443/checkout-v2?utm_source=x` |
| `https://e.com/checkout` | `https://other.example/landing` | `e.com/checkout?utm_source=x` | `https://other.example/landing` |
| `https://e.com/checkout` | `https://e.com/checkout-v2` | `E.com:443/checkout/?utm_source=x` | `https://e.com/checkout-v2?utm_source=x` |
| `https://e.com/checkout` | `https://e.com/checkout-v2` | `e.com/Checkout` | Not split: paths keep their case |
| `https://e.com/checkout` | `https://e.com/checkout-v2` | `other.example/checkout` | Not split: another host |

Hosts are compared without case or port; paths keep their case and percent-encoding, and one
trailing `/` is ignored.

---

## The visitor-id cookie

One cookie per site holds only the visitor's id, which is also the `user_id` your tracking
calls send. It is set as:

```text
controlvar_uid=<id>; Max-Age=2592000; Path=/; SameSite=Lax; Secure
```

with `; Domain=<domain>` added when you give the integration a cookie domain, and never
`HttpOnly`, because the page's own script reads the id to send it with conversions. A visitor
without the cookie gets a new random id, and the integration sets the cookie whenever it made
up an id, whatever the API answered, so a visitor whose request failed keeps the same id next
time. A cookie that is there but unusable (empty, longer than 255 characters, or with a control
character) is left as it is, and the visitor is served the page.

The cookie is Secure, so a browser keeps it only on https pages, and on localhost.

A variant on another host counts conversions under the visitor's id only if that host can
read the visitor-id cookie (a shared parent domain with the cookie's Domain set). Otherwise
visitors split and its conversions are not counted.

A site that needs consent before it sets an identifying cookie makes no call and sets no
cookie until the visitor consents, and serves the page they asked for until then: the
reference integration takes a `consented` function for this.

---

## Search engines

The redirect is a `302 Found`, which tells search engines to keep the control's URL, and
crawlers are sent where a new visitor would be, so they see the same pages people do. Add
`<link rel="canonical" href="<control URL>">` to each variant's page yourself:
`canonical_url` is stored with the configuration, and nothing reads it.

---

## Split URL preview

```text
GET /api/v1/experiments/{experiment_id}/split-url/preview?user_id=<visitor id>
```

Returns the URL that visitor id gets now, without recording anything: their stored variant if
they have one, else the variant a new visitor with that id would get (by the bandit weights
for a bandit experiment). It checks no eligibility rule: a visitor the global holdout, a mutual
exclusion group or the targeting rules keep out is served the control page by the assign route
instead. It is for ADMIN and DEVELOPER users (and superusers), and in a core deployment it
answers `501`.

```{.bash exec}
curl -s "localhost:8000/api/v1/experiments/$EXP_ID/split-url/preview?user_id=visitor-2" \
  -H "Authorization: Bearer $TOKEN" | jq -c '{variant_name, url}'
```
<!-- expect: {"variant_name":"Treatment","url":"https://shop.example.com/checkout-v2"} -->

It prints `{"variant_name":"Treatment","url":"https://shop.example.com/checkout-v2"}`, the
variant `visitor-2` was sent to. The answer also carries `experiment_id`, `user_id` and the
variant's `traffic_allocation`.

---

## CloudFront CDK construct

The module also holds a CloudFront distribution construct with a Lambda@Edge router. The
CloudFront edge router does not split traffic yet: put the redirect in your server or edge.
See [AWS integration](../integrations/aws.md).

---

## Limits

- The AWS installer (beta) installs the core profile, so it does not run split URL
  experiments.
- A site with no server or edge of its own in front of its pages cannot split: the
  redirect happens before the page, not in the browser.
- Both routes have the limit `POST /api/v1/tracking/assign` has: 6,000 requests a minute per
  client address, per route.
- `SDK_RATE_LIMIT_PER_MINUTE` raises it, for every SDK route and every address.
- Over the limit the API answers `429`, and the reference integration serves the page with
  `X-Split-URL: failed` and logs `split_url_failed status=429`.
- Above the limit, a returning visitor whose assignment is a treatment is served the control
  page while their conversions still count for the treatment; a site with more than about 100
  entry-page views a second is in that range.
- Whenever the API does not answer (a timeout, a `429` or an outage), a returning visitor
  whose assignment is a treatment is served the control page, and their later conversions
  still count for the treatment, which pulls the result toward no effect.

---

## `SplitUrlConfig`

`split_url_config` holds the URL variants:

| Field | Type | Required | Description |
|---|---|---|---|
| `variants` | `array` | Yes | The URL variants: at least 2 |
| `variants[].name` | `string` | Yes | The name of the experiment variant this URL is for |
| `variants[].url` | `string` | Yes | The URL for this variant: an absolute `http://` or `https://` URL (see [Validation Rules](#validation-rules)), stored as given |
| `variants[].traffic_allocation` | `number` | Yes | Percentage of traffic: each from 0 to 100, they must add up to 100, and each equals its experiment variant's |
| `cookie_name` | `string` | No | Stored; nothing reads it (the cookie is the site's, above) |
| `cookie_ttl_days` | `int` | No | Stored; nothing reads it (default `30`) |
| `canonical_url` | `string` | No | Stored; nothing reads it |

---

## Validation Rules

On create and update, `split_url_config` is checked for:

- at least 2 URL variants, with unique names;
- `traffic_allocation` values that are each from 0 to 100 and add up to 100 (within 0.01), none of them NaN or infinite;
- a `url` for each that is an absolute `http://` or `https://` URL of at most 2048 characters with an ASCII host name without a `%` sign (an internationalised name in its `xn--` form), without a user name or password, a `#` fragment, a backslash, whitespace or control characters, and whose path has no single-dot or double-dot segment (such as `/./` or `/../`, also written with `%2e`) and no character a browser percent-encodes in a path: a non-ASCII character, `"`, `<`, `>`, `^`, a backquote, `{`, `|` or `}`, which you write percent-encoded, as the browser sends it (`é` as `%C3%A9`).

A URL is stored exactly as sent.

In a `split_url` experiment the configuration must also match the experiment's variants:

- each URL variant names one of the experiment's variants, and each of those has exactly one URL variant;
- each URL variant's `traffic_allocation` equals its experiment variant's, within 0.01: the experiment's variants decide the split;
- no URL variant but the control's has the control URL's host and path.

The control URL is the URL variant named like the experiment's control variant. Hosts are
compared without case or port, and paths without one trailing `/`; the query string is not
compared. A variant whose URL has the control URL's path on another host is accepted.

A refusal answers `422` at the rule's place, such as
`["body", "split_url_config", "variants", 1, "name"]`, with a fixed message that names the
variant and never repeats a URL. An update is checked on its result: a `PUT` that sends
`variants`, `split_url_config` or `experiment_type` is checked with whatever it leaves out
taken from what is stored, so `variants` sent alone are checked against the stored
configuration, and the refusal is then at `["body", "variants"]` (or
`["body", "experiment_type"]`).

A configuration stored before a rule is kept as it was: a `PUT` that sends none of those
three fields works, and one that sends the configuration back must correct it. An experiment
of another type can carry a configuration, which is held to the first list only, and a
`split_url` experiment can be created without one (its preview then answers `400`).

### Starting a split URL experiment

`POST /api/v1/experiments/{id}/start` answers `400`, with a `detail` that says what to change
and that it is changed with `PUT /api/v1/experiments/{id}`, when the split URL experiment:

1. has no `split_url_config`;
2. has one that breaks a rule above;
3. has the same control URL host and path as an active split URL experiment;
4. would chain with an active split URL experiment: its control URL has the host and path of that experiment's variant URL, or one of its variant URLs has the host and path of that experiment's control URL.

Refusing every such link refuses chains and loops of any length. The same path on another
host is neither the same control URL nor a chain. An active split URL experiment whose
stored configuration breaks a rule is left out of the last two checks. When the scheduler
starts a draft at its `start_date`, it makes the first two checks and leaves a draft that
fails them as a draft. The routes table applies the last two checks too, so of two active
experiments that clash, only the one started first is split.

---

## Error Responses

| Status | Meaning |
|---|---|
| `400 Bad Request` | Preview and assign: the experiment is not a `split_url` experiment (`Experiment is not a split_url type`), or has no `split_url_config` (`split_url_config is not set for this experiment`). Start: see [Starting a split URL experiment](#starting-a-split-url-experiment) |
| `401 Unauthorized` | Routes and assign: a missing or invalid API key. Preview, create and update: a missing or invalid Bearer token |
| `403 Forbidden` | Preview: the user is not ADMIN, DEVELOPER or a superuser |
| `404 Not Found` | Preview: no experiment has this id. Assign: no experiment has the key (`No experiment has the key '<key>'.`). Routes and assign in a core deployment |
| `409 Conflict` | Preview and assign: the stored `split_url_config` breaks a rule, such as an allocation outside 0 to 100 or a URL saved before it was checked (`split_url_config is not valid; update it with PUT /api/v1/experiments/{id}`) |
| `422 Unprocessable Entity` | Create or update: the body fails validation, such as allocations that do not sum to 100 or URL variants that do not match the experiment's variants. Assign: the body fails validation, at the field |
| `429 Too Many Requests` | Routes and assign: over the rate limit (see [Limits](#limits)) |
| `501 Not Implemented` | Preview, or creating a `split_url` experiment, in a core deployment |

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "split_url_config"],
      "msg": "Value error, Traffic allocations must sum to 100, got 95.0",
      "ctx": { "error": {} }
    }
  ]
}
```
