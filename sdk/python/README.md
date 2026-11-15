# controlvar (Python)

**Beta.** This is a 0.x release: pin the exact version you test against.

Synchronous, dependency-free Python client for the Controlvar public API.
Experiment assignment and feature-flag evaluation are decided **by the server** by default (sticky
per user + experiment); the SDK caches the answers per user + key. Server-side code can opt in to
**local evaluation** (`evaluation="local"`, beta): flags are answered in-process from the server's
ruleset, with the server's answers, and anything that cannot be answered exactly goes to the
server. See [`docs/sdk/local-evaluation.md`](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/local-evaluation.md).

Requires Python 3.9+. HTTP goes through the standard library (`urllib`), so there is nothing
else to install.

## Install

If you installed `experimently`, the name this package had before the project was renamed, uninstall it and install `controlvar`.

The `controlvar` package is on [PyPI](https://pypi.org/project/controlvar/). 0.3.0 is a
beta release. It installs and imports as `controlvar`.

```bash
pip install controlvar==0.3.0
```

To work on the SDK itself, install it from the root of a clone of this repository instead:

```bash
pip install -e sdk/python
```

## Changes in this version

- `get_string_value`, `get_number_value` and `get_json_value` are new: they read the value a
  string, number or json flag serves. Each returns your `default` (keyword-only) when it cannot,
  logs a warning, and passes the reason to `on_error(error, "value")`. See
  [Flag values](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/python.md#flag-values).
- `FlagEvaluation` gains `value` and `value_type`, after `source`, so building one by position
  still works. `FlagEvaluation` equality, `repr` and `astuple`/`asdict` include the two new fields.
- `ExperimentationError` gains `code`: `"TYPE_MISMATCH"` or `"INVALID_RESPONSE"` on the errors the
  typed getters make, and `"HOOK_ERROR"` on the error `on_error` receives with `"exposure"`. Other
  errors carry no code in this version; `status` describes them.
- `on_exposure` is new: a function you give the client when you create it, which it calls once each
  time the server enrols a user in an experiment and the answer did not come from its cache. It
  receives an `Exposure`, which `controlvar` exports. See Exposure hook below.
- `on_error` is now also called in server mode: with the operation `"value"` when a typed getter
  returns your default, and with `"exposure"` when `on_exposure` raises. Code that treats an
  operation or a `code` it does not know as a mistake should handle them.
- `track()` takes an `event_id`, and `track_batch()` sends the `event_id` key of an event's dict.
  See Event ids below.
- `track()` returns `False`, and sends nothing, when `event_value` or `properties` holds NaN or an
  infinite number; see Tracking fan-out rule for the other methods. Before this version the request
  was sent.
- Local evaluation answers a string, number or json flag's value too. A flag entry with an
  `evaluation` this SDK does not know sends only that flag to the server, and the rest of the
  ruleset is still answered locally; before this version it refused the whole ruleset, and the
  client kept answering from its last one. An entry with a `value_type` it does not know, or a
  value of the wrong type, also sends only that flag to the server.
- Once local evaluation stops answering a flag (`max_stale_seconds` passed, the ruleset discarded,
  `close()`), a call for a user it answered locally goes to the server; before this version that
  user kept the local answer from the cache for up to `cache_ttl_seconds`.
- With a finite `cache_ttl_seconds` above 24 hours, a `track()` without a key still reaches an
  experiment or flag whose answer is still cached; before this version it reached only those
  answered in the last 24 hours.
- The message of a request that fails on the network, and the warning the client logs for it,
  name the URL without its query string, which carries the user id and the user's attributes;
  before this version they named the full URL.

## Event ids

`track(..., event_id=None)` sends an `event_id`, and `track_batch()` sends the `event_id` key of
an event's dict as it is. The server stores an event sent again with the same `event_id`, user,
experiment, flag and event name only once. It needs a Controlvar server that includes the `event_id` change (#1640,
in the first platform release after it merged): an older server ignores the field.

- The SDK makes no id: without an `event_id` none is sent, and an event sent again is counted
  again. Make the id once, when the event happens, and give the same one each time you send it.
- `None` and `""` send none. The server refuses an id of more than 255 characters or one holding
  NUL with `422`; the SDK does not check it.
- An event fanned out to several experiments and flags carries the same id in every entry. A `429`
  that is retried sends the same id. A request that fails on the network or times out is not sent
  again by the SDK.

## Quick start

Set `CONTROLVAR_API_KEY` to an API key, then run the block as it is; the two `print` calls
stand for your own code.

```python
import os
from controlvar import ExperimentationClient, ExperimentationError

client = ExperimentationClient(
    api_url=os.environ.get("CONTROLVAR_API_URL", "http://localhost:8000"),  # origin only
    api_key=os.environ["CONTROLVAR_API_KEY"],
)

# Experiments — sticky assignment made by the server (POST /api/v1/tracking/assign)
variant = client.get_variant("checkout_flow", user_id="user-123", user_attributes={"plan": "pro"})
if variant == "treatment":
    print("show the new checkout")   # your own code

assignment = client.get_assignment("checkout_flow", "user-123")   # raises on failure
assignment.variant_name, assignment.is_control, assignment.configuration

# Feature flags (GET /api/v1/feature-flags/evaluate/{key}?user_id=…&context=<url-encoded attributes>)
if client.is_feature_enabled("new_search", "user-123", user_attributes={"plan": "pro"}):
    print("use the new search")      # your own code
flag = client.get_feature_flag("new_search", "user-123")          # FlagEvaluation(key, enabled, config, reason, source, value, value_type)
flags = client.get_all_flags("user-123", {"plan": "pro"})          # {"new_search": True, ...}

# Tracking (never raises)
client.track("user-123", "purchase", event_value=49.99, experiment_key="checkout_flow")
client.track("user-123", "page_view", properties={"page": "/"})   # no key: fanned out, see below
```

Constructor: `ExperimentationClient(api_url, api_key, timeout_seconds=5.0, cache_ttl_seconds=300,
default_variant="control", transport=None)`. Instances are thread-safe.

## API

| Method | Returns | Failure behaviour |
|---|---|---|
| `get_assignment(experiment_key, user_id, user_attributes=None)` | `Assignment(experiment_key, user_id, variant_id, variant_name, is_control, configuration, assigned, reason)` | raises `ExperimentationError` (`status == 404` when no experiment has the key, or for a draft with no control variant); an experiment that is not ACTIVE returns its control variant with `assigned` False and `reason` its status |
| `get_variant(experiment_key, user_id, user_attributes=None)` | `str` variant name | returns `default_variant` |
| `get_feature_flag(flag_key, user_id, user_attributes=None)` | `FlagEvaluation(key, enabled, config, reason, source, value, value_type)` | raises `ExperimentationError` (`status == 404` when the flag key is unknown; a flag that is not ACTIVE returns `enabled=False`, `reason="inactive"`) |
| `is_feature_enabled(flag_key, user_id, user_attributes=None)` | `bool` | returns `False` |
| `get_string_value(flag_key, user_id, user_attributes=None, *, default)` | `str`, the string a string flag serves this user | returns `default`; never raises; logs a warning and calls `on_error(error, "value")` |
| `get_number_value(flag_key, user_id, user_attributes=None, *, default)` | `int` or `float` as saved (never a `bool`), the number a number flag serves this user | returns `default`; never raises; logs a warning and calls `on_error(error, "value")` |
| `get_json_value(flag_key, user_id, user_attributes=None, *, default)` | a new copy of the `dict` or `list` a json flag serves this user | returns `default`; never raises; logs a warning and calls `on_error(error, "value")` |
| `get_all_flags(user_id, user_attributes=None)` | `dict[str, bool]` (not cached) | raises `ExperimentationError` |
| `track(user_id, event_name, event_value=None, properties=None, experiment_key=None, feature_flag_key=None, event_type=None, timestamp=None, event_id=None)` | `bool` — `True` when sent and answered 2xx | never raises; `False` on failure |
| `track_batch(events)` | `BatchResult(success_count, failure_count, errors)`; `.ok` | never raises; a failed chunk counts all its events as failures |
| `report_error(error_type, message, *, feature_flag_key=None, experiment_key=None, user_id=None, stack_trace=None, metadata=None, timestamp=None)` | `bool` — `True` when the server stored the report (201) | never raises; `False` and a logged warning otherwise |
| `get_assignments(user_id, active_only=True)` | `list[dict]` from the server | raises `ExperimentationError` |
| `cached_assignments(user_id)` / `cached_flags(user_id)` | cached, unexpired entries | — |
| `clear_cache()` | — (keeps the keys a key-less `track` is sent to) | — |
| `ready(timeout_seconds=None)`, `status()`, `close()` / `with` | local mode: readiness, ruleset state, shutdown (sends evaluation counts) | never raise |
| `consistent_hash(user_id, flag_key)` / `md5_hex(user_id, flag_key)` | cross-SDK MD5 bucket in `[0, 1)` / hex digest | pure functions; **not** the flag bucketing function |

`ExperimentationError` carries `.status` (HTTP status, `None` for network errors/timeouts),
`.body` (raw response text) and `.code`: `"TYPE_MISMATCH"` or `"INVALID_RESPONSE"` on an error a
typed getter makes itself, and `"HOOK_ERROR"` on the error `on_error` receives with `"exposure"`.
Other errors carry no code in this version; `.status` describes them. Failures are never cached, so
the next call retries. A `429` is retried once after `Retry-After` seconds (capped at 5 s).

`user_attributes` is sent as `context` on assignment **and** on flag evaluation
(`&context=<url-encoded JSON>`, omitted when empty), so flag targeting rules evaluate against it
(`country` also matches `user.country`; nested dicts flatten to dotted keys such as `app.version`).
`FlagEvaluation.reason` (`targeting_rule` / `rollout` / `inactive` / `error`, or `None`) says why
the server decided. Caches are keyed by user + key only — call `clear_cache()` after changing a
user's attributes.

`report_error` returns `True` when the server stored the report. A stored report counts toward the
flag's error rate and automatic rollback only when the client's key has the `sdk:ruleset` scope and
was created by a user who can change feature flags; the flag's safety check shows the others as
`uncounted_error_count`.

### Enrolment: `assigned` and `reason`

An `Assignment` also carries `assigned` and `reason`. `assigned is False` means the server did not
enrol the user (`reason` is `"holdout"`, `"mutual_exclusion"` or `"targeting"`) and returned the
control variant so you render the default experience; no exposure was recorded. **When you export
exposures to a warehouse, exclude assignments with `assigned is False`**: the user was not enrolled,
and `reason` says why. A server that predates the fields sends neither, so `assigned` is `None`
(never `False`). Decide deliberately how to treat those rows rather than dropping them with an
`assigned is True` filter.

An experiment that is not ACTIVE answers every user with its control variant, `assigned` False and
`reason` set to its status (`"draft"`, `"paused"`, `"completed"` or `"archived"`).

## Tracking fan-out rule

- **with** `experiment_key` and/or `feature_flag_key` → one `POST /api/v1/tracking/track`;
- **without** a key → one `POST /api/v1/tracking/batch` (chunks of 100) containing one entry per
  experiment (`experiment_key`) and one per flag (`feature_flag_key`) this client answered for the
  user in the last 24 hours, cached or not, except an experiment whose latest answer has `reason`
  `"draft"`, `"paused"`, `"completed"` or `"archived"`;
- nothing to attribute it to → one `POST /api/v1/tracking/track` with no key, stored as history (it
  counts in holdout results and as CUPED history, never in an experiment's results);
- `track` returns `True` when the event was sent and the server answered 2xx, `False` otherwise.

The keys live in this client's memory, for up to 10,000 users; `clear_cache()` keeps them, and
another process does not have them. In a server, pass `experiment_key` on conversions.

`event_type` defaults to `event_name`; `properties` is sent as `metadata`; `timestamp` may be a
`datetime` (naive values are treated as UTC) or an ISO-8601 string.

`track` returns `False`, and sends nothing, when `event_value` or `properties` holds NaN or an
infinite number. `get_assignment` raises `ExperimentationError`
for the same in `user_attributes`, and `get_variant` returns the default for a user with no
cached assignment (a cached assignment is returned as before).

## Exposure hook

`on_exposure` is a function you give the client when you create it. The client calls it once each
time the server enrols the user in an experiment and the answer did not come from the client's
cache: the moment the server records the exposure. Use it to send the exposure to your analytics
tool or warehouse.

The example needs an ACTIVE experiment with the key `checkout_flow`; put your own key in its place.
It prints `exposure checkout_flow <variant> user-123` once, or nothing for a user the experiment
did not enrol.

```python
import os
import queue
import threading

from controlvar import ExperimentationClient

exposures = queue.Queue(maxsize=10_000)


def send_exposures():
    while True:
        exposure = exposures.get()
        # Your analytics tool's call goes here: it runs on this thread, not inside get_variant.
        print("exposure", exposure.experiment_key, exposure.variant_name, exposure.user_id)
        exposures.task_done()


threading.Thread(target=send_exposures, daemon=True).start()


def on_exposure(exposure):
    try:
        exposures.put_nowait(exposure)
    except queue.Full:
        pass  # drop it rather than make get_variant wait


client = ExperimentationClient(
    api_url=os.environ.get("CONTROLVAR_API_URL", "http://localhost:8000"),
    api_key=os.environ["CONTROLVAR_API_KEY"],
    on_exposure=on_exposure,
)
client.get_variant("checkout_flow", "user-123")
exposures.join()
```

It receives an `Exposure`: `experiment_key`, `user_id`, `variant_id` (`None` when the server sends
none) and `variant_name`. It is not called for a cached answer, a user the experiment did not enrol
or an experiment that is not running (`assigned` False), a call that fails, or a feature flag. It
runs in the thread that called `get_assignment` or `get_variant`, before that call returns, so keep
it quick. If it raises, the answer is unchanged, and the error is logged at ERROR and passed to
`on_error` with the operation `"exposure"` and `code` `"HOOK_ERROR"`.

Full details: [Exposure hook](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/python.md#exposure-hook).

## Backend endpoints used

Every request carries `X-API-Key: <key>`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `get_assignment`, `get_variant` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration, assigned, reason}`; 404 when no experiment has the key, or for a draft with no control variant; an experiment that is not ACTIVE answers its control variant with `assigned: false` |
| `get_feature_flag`, `is_feature_enabled` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=<url-encoded JSON>` | `context` = `user_attributes` (omitted when empty) | `{key, enabled, config, reason}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `get_all_flags` | `GET /api/v1/feature-flags/user/{user_id}?context=<url-encoded JSON>` | `context` = `user_attributes` (omitted when empty) | `{"<flag_key>": bool, …}` |
| `track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?, event_id?}` | ignored |
| `track` without keys | `POST /api/v1/tracking/batch`, one entry per experiment and flag it reaches; with none, `POST /api/v1/tracking/track` | `{events: [<track body>, …]}` (≤ 100 per request); with none, the track body with no key | ignored |
| `track_batch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (≤ 100 per request) | `{success_count, failure_count, errors}` |
| `get_assignments` | `GET /api/v1/tracking/assignments/{user_id}?active_only=true` | — | list of dicts |

## Contract smoke

```bash
CONTROLVAR_API_KEY=<key> python sdk/python/examples/contract_smoke.py
```

It prints:

```text
{"sdk":"python","assign":{"variant_name":"treatment","is_control":false,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true},"event_id":{"user_id":"smoke-<uuid>","event_name":"contract_dedup_<uuid>","id":"contract-<uuid>","sends":2},"exposure":{"calls":1,"payload":{"experiment_key":"sdk_contract_ab","variant_name":"treatment","variant_id":"<id>","user_id":"smoke-<uuid>"}}}
```

Env: `CONTROLVAR_API_URL` (default `http://localhost:8000`), `CONTROLVAR_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default `sdk_contract_flag`),
`CONTRACT_USER_ID` (default random `smoke-<uuid>`).

CI runs this SDK's contract smoke against a real API: `python` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml). Its fixtures,
`sdk_contract_ab` and `sdk_contract_flag`, are seeded with `backend/scripts/seed_sdk_contract.py`;
run it with `python tests/sdk-contract/live/run_live_contract.py --sdk python --strict`.

## Testing your own code

`controlvar.testing.FakeTransport` records requests and serves canned responses:

```python
from controlvar import ExperimentationClient
from controlvar.testing import FakeTransport

transport = FakeTransport().route("GET", "/api/v1/feature-flags/evaluate/new_search",
                                  json={"key": "new_search", "enabled": True, "config": None})
client = ExperimentationClient("http://test", "key", transport=transport)
assert client.is_feature_enabled("new_search", "user-1")
assert transport.last.query == {"user_id": "user-1"}
```

## Development

```bash
source venv/bin/activate
python -m pytest sdk/python/tests -q -o addopts="" -p no:cacheprovider
```

- `python -m pytest sdk/python/tests -q -o addopts="" -p no:cacheprovider`: HTTP is faked; CI runs them on Python 3.11
