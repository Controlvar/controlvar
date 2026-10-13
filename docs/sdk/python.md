# Python SDK

`experimently` (v0.1.0) is a synchronous, dependency-free Python client for the
Experimently public API: experiment assignment, feature-flag evaluation and event
tracking. Requires Python 3.9+; HTTP goes through the standard library (`urllib`).

By default, flag evaluation and experiment assignment are decided **by the server**: every call
goes to the public API with your `X-API-Key`, the server buckets the user (sticky per user +
experiment), and the SDK caches the answer per user + key. In server-side code you can opt in to
[local evaluation](#local-evaluation-server-side-beta) (`evaluation="local"`): flags are then
answered in-process from the server's ruleset, with the server's answers, and experiments stay on
the server.

Source: `sdk/python`. The OpenFeature provider in `sdk/openfeature-python` is built on top of this
client (see [openfeature.md](openfeature.md)).

---

## Installation

The `experimently` package is on [PyPI](https://pypi.org/project/experimently/). 0.1.0 is a
beta release.

```bash
pip install experimently==0.1.0
```

To work on the SDK itself, install it from the root of a clone of this repository instead:

```bash
pip install -e sdk/python
```

---

## Quick start

`render_new_checkout()` and `use_new_search()` below stand for your own code: define them, or
replace the two calls, before you run the block. As written, a branch that reaches one of them
raises `NameError`.

```python
import os
from experimentation import ExperimentationClient, ExperimentationError

client = ExperimentationClient(
    api_url=os.environ.get("EXPERIMENTLY_API_URL", "http://localhost:8000"),  # origin only
    api_key=os.environ["EXPERIMENTLY_API_KEY"],
)

# Experiments — sticky assignment made by the server (POST /api/v1/tracking/assign)
variant = client.get_variant("checkout_flow", user_id="user-123", user_attributes={"plan": "pro"})
if variant == "treatment":
    render_new_checkout()   # your own code

assignment = client.get_assignment("checkout_flow", "user-123")   # raises on failure
assignment.variant_name, assignment.is_control, assignment.configuration

# Feature flags (GET /api/v1/feature-flags/evaluate/{key}?user_id=…&context=<url-encoded attributes>)
if client.is_feature_enabled("new_search", "user-123", user_attributes={"plan": "pro"}):
    use_new_search()        # your own code
flag = client.get_feature_flag("new_search", "user-123")          # FlagEvaluation(key, enabled, config, reason)
flags = client.get_all_flags("user-123", {"plan": "pro"})          # {"new_search": True, ...}

# Tracking (never raises)
client.track("user-123", "purchase", event_value=49.99, experiment_key="checkout_flow")
client.track("user-123", "page_view", properties={"page": "/"})   # no key: fanned out, see below
```

### Constructor

```python
ExperimentationClient(
    api_url: str,                    # backend origin, e.g. "https://api.example.com"; the SDK appends /api/v1/...
    api_key: str,                    # sent as X-API-Key
    timeout_seconds: float = 5.0,    # per request
    cache_ttl_seconds: float = 300,  # how long a successful assignment/evaluation is reused per user + key
    default_variant: str = "control",# what get_variant returns on failure
    transport: Transport | None = None,  # inject your own HTTP layer (tests use experimentation.testing.FakeTransport)
    evaluation: str = "server",      # "server" (default) or "local" (server-side only; see Local evaluation)
    refresh_interval_seconds: float = 30,  # local mode: ruleset refresh (minimum 5, ±10% jitter)
    max_stale_seconds: float | None = None,  # local mode: evaluate on the server once the ruleset is this old
    on_error=None,                   # local mode: on_error(error, "refresh" | "flush" | "evaluate")
)
```

Instances are thread-safe and should be shared across your application (one per process is
typical): the per-user cache is what makes a key-less `track` count for every experiment the user
is in.

---

## API reference

| Method | Returns | Failure behaviour |
|---|---|---|
| `get_assignment(experiment_key, user_id, user_attributes=None)` | `Assignment(experiment_key, user_id, variant_id, variant_name, is_control, configuration, assigned, reason)` | raises `ExperimentationError` (`status == 404` when the experiment is not ACTIVE or unknown) |
| `get_variant(experiment_key, user_id, user_attributes=None)` | `str` — the variant name | returns `default_variant` (`"control"`) |
| `get_feature_flag(flag_key, user_id, user_attributes=None)` | `FlagEvaluation(key, enabled, config, reason)` | raises `ExperimentationError` (`status == 404` when the flag key is unknown; a flag that is not ACTIVE returns `enabled=False`, `reason="inactive"`) |
| `is_feature_enabled(flag_key, user_id, user_attributes=None)` | `bool` | returns `False` |
| `get_all_flags(user_id, user_attributes=None)` | `dict[str, bool]` (not cached) | raises `ExperimentationError` |
| `track(user_id, event_name, event_value=None, properties=None, experiment_key=None, feature_flag_key=None, event_type=None, timestamp=None)` | `bool` — `True` when the server accepted it | never raises; `False` on failure or when nothing was sent |
| `track_batch(events)` | `BatchResult(success_count, failure_count, errors)`, `.ok` | never raises; sends chunks of 100, a failed chunk counts all its events as failures |
| `get_assignments(user_id, active_only=True)` | `list[dict]` — the user's assignments from the server (not cached) | raises `ExperimentationError` |
| `cached_assignments(user_id)` / `cached_flags(user_id)` | cached, unexpired entries for the user | — |
| `clear_cache()` | — | — |
| `ready(timeout_seconds=None)` | `ReadyResult(ok, ruleset_version, error)`, truthy when ready; `ReadyResult(ok=True)` at once in server mode | never raises |
| `status()` | `LocalEvaluationStatus(evaluation, ready, ruleset_version, last_refresh_at, last_error, server_evaluated_flags)` | — |
| `close()`, `with ExperimentationClient(...) as client:` | local mode: stops the refresh thread and sends the remaining evaluation counts; a no-op in server mode | never raises |
| `consistent_hash(user_id, flag_key)` / `md5_hex(user_id, flag_key)` | cross-SDK MD5 bucket in `[0, 1)` / hex digest | pure functions; **not** the flag bucketing function and not used to decide variants or flags |

`Assignment` and `FlagEvaluation` are frozen dataclasses. `ExperimentationError` carries
`.status` (HTTP status, `None` for network errors and timeouts) and `.body` (raw response text).
`FlagEvaluation.reason` (`"targeting_rule"`, `"rollout"`, `"inactive"` or `"error"`) says why the
server decided; it is `None` when the server does not send one. `FlagEvaluation.source` is set only
in local mode: `"local"` when answered in-process, `"server"` when the server evaluated it.

### Enrolment: `assigned` and `reason`

An `Assignment` also carries `assigned` and `reason`. `assigned is False` means the server did not
enrol the user (`reason` is `"holdout"`, `"mutual_exclusion"` or `"targeting"`) and returned the
control variant so you render the default experience; no exposure was recorded. **When you export
exposures to a warehouse, exclude assignments with `assigned is False`**: the user was not enrolled,
and `reason` says why. A server that predates the fields sends neither, so `assigned` is `None`
(never `False`). Decide deliberately how to treat those rows rather than dropping them with an
`assigned is True` filter.

### Targeting context

`user_attributes` is what the platform's targeting rules evaluate against. It is sent as `context`
in the `POST /api/v1/tracking/assign` body and, when non-empty, as `context=<url-encoded JSON>`
(compact `json.dumps(..., separators=(",", ":"))`, percent-encoded) on
`GET /api/v1/feature-flags/evaluate/{flag_key}` and `GET /api/v1/feature-flags/user/{user_id}` —
so a flag whose dashboard rule says `os_version semver_gte 17.0.0 AND tier equals premium` turns
on only for matching users. Top-level keys are also reachable under `user.` / `device.` / `app.`
aliases in rules (`country` matches `user.country`), and nested dicts flatten to dotted keys
(`{"app": {"version": "3.2.1"}}` answers `app.version`).

Attributes are assumed **stable per user**: evaluations and assignments are cached by user + key
only, so call `clear_cache()` after changing a user's attributes.

### Caching and retries

- Successful assignments and evaluations are cached per user + key for `cache_ttl_seconds`
  (default 300 s). Failures are never cached, so the next call retries.
- A `429` is retried once after `Retry-After` seconds (capped at 5 s; 1 s when the header is
  missing or unparseable). Any other non-2xx status raises immediately.
- The cache is checked before any request, so a cached value is returned even if the backend is
  currently unreachable.

### Tracking fan-out rule

- **With** `experiment_key` and/or `feature_flag_key` → one `POST /api/v1/tracking/track`.
- **Without** a key → one `POST /api/v1/tracking/batch` (chunks of 100) containing one entry per
  experiment the user has been assigned to through this client (`experiment_key`) plus one per flag
  evaluated for the user (`feature_flag_key`), taken from the cache (successful, unexpired only).
- Nothing cached for the user → nothing is sent and `track` returns `False`.

`event_type` defaults to `event_name`; `properties` is sent as `metadata`; `timestamp` may be a
`datetime` (naive values are treated as UTC) or an ISO-8601 string. Conversions are matched to
experiment metrics by **event name**.

---

## Backend endpoints used

Every request carries `X-API-Key: <key>`, `Content-Type: application/json` and
`Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `get_assignment`, `get_variant` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when the experiment is not ACTIVE |
| `get_feature_flag`, `is_feature_enabled` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=<url-encoded JSON>` | `context` = `user_attributes` (omitted when empty) | `{key, enabled, config, reason}`; `enabled: false`, `reason: "inactive"` when the flag is not ACTIVE; 404 only for an unknown key |
| `get_all_flags` | `GET /api/v1/feature-flags/user/{user_id}?context=<url-encoded JSON>` | `context` = `user_attributes` (omitted when empty) | `{"<flag_key>": bool, …}` |
| `track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `track` without keys, `track_batch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | `{success_count, failure_count, errors}` |
| `get_assignments` | `GET /api/v1/tracking/assignments/{user_id}?active_only=true` | — | list of dicts |
| local mode: ruleset refresh | `GET /api/v1/sdk/ruleset` (beta) | `If-None-Match: "<version>"` | the ruleset and `ETag`; `304` when unchanged |
| local mode: evaluation counts | `POST /api/v1/tracking/evaluations` (beta) | `{evaluations: [{flag_key, count, enabled_count, window_start, window_end}]}` | `{accepted, errors}` |

These SDK paths share a per-IP rate-limit ceiling of `SDK_RATE_LIMIT_PER_MINUTE` requests
(default 6000) on the backend; the SDK honours `Retry-After` on `429` as described above.

---

## Local evaluation (server-side, beta)

With `evaluation="local"` the client downloads the flag ruleset (`GET /api/v1/sdk/ruleset`),
refreshes it on a daemon thread every `refresh_interval_seconds` (30 s by default, 5 s minimum),
and answers `get_feature_flag`, `is_feature_enabled` and `get_all_flags` in-process whenever it can
give exactly the server's answer; otherwise it makes the usual request. Experiments are always
assigned by the server. It needs an Experimently server at 0.11.0 or later and a key with the
`sdk:ruleset` scope, and it is for server-side code only.

```python
from experimentation import ExperimentationClient

with ExperimentationClient(api_url, server_key, evaluation="local") as client:
    client.ready(timeout_seconds=5)                       # optional; never raises
    flag = client.get_feature_flag("new_search", "user-123", {"country": "US"})
    print(flag.enabled, flag.reason, flag.source)         # source: "local" or "server"
```

A change made in the dashboard reaches the process at its next successful refresh. While the API
is unreachable the last ruleset keeps being served (set `max_stale_seconds` to bound that); a 401
or 403 on refresh discards it. A client created before `os.fork()` (gunicorn `--preload`) keeps
working in each child, which starts its own refresh thread and reports only its own evaluations.
What is answered locally, the refresh failure table, safety monitoring and troubleshooting:
[Local evaluation](local-evaluation.md).

---

## Testing your own code

`experimentation.testing.FakeTransport` records requests and serves canned responses, so unit
tests can assert the exact request without a network:

```python
from experimentation import ExperimentationClient
from experimentation.testing import FakeTransport

transport = FakeTransport().route(
    "GET", "/api/v1/feature-flags/evaluate/new_search",
    json={"key": "new_search", "enabled": True, "config": None},
)
client = ExperimentationClient("http://test", "key", transport=transport)
assert client.is_feature_enabled("new_search", "user-1")
assert transport.last.query == {"user_id": "user-1"}
assert transport.last.headers["X-API-Key"] == "key"
```

---

## Contract smoke

Runs the four contract steps (sticky assignment, flag evaluation, keyed track, key-less fan-out
plus a 2-event batch) against a live backend and prints one JSON line:

```bash
EXPERIMENTLY_API_KEY=<key> python sdk/python/examples/contract_smoke.py
```

It prints:

```text
{"sdk":"python","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `EXPERIMENTLY_API_URL` (default `http://localhost:8000`), `EXPERIMENTLY_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). No install is needed;
the script adds `sdk/python` to `sys.path`.

CI runs this SDK's contract smoke against a real API: `python` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml).
Fixtures: `backend/scripts/seed_sdk_contract.py`; the live
runner: `python tests/sdk-contract/live/run_live_contract.py --sdk python --strict`.

---

## Development

```bash
source venv/bin/activate
python -m pytest sdk/python/tests -q -o addopts="" -p no:cacheprovider
python tests/sdk-contract/hash_contract.py python
```

- `python -m pytest sdk/python/tests -q -o addopts="" -p no:cacheprovider`: HTTP is faked
- `python tests/sdk-contract/hash_contract.py python`: this SDK's hash vs the golden vectors
