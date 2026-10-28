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

The `controlvar` package is on [PyPI](https://pypi.org/project/controlvar/). 0.2.0 is a
beta release. It installs and imports as `controlvar`.

```bash
pip install controlvar==0.2.0
```

To work on the SDK itself, install it from the root of a clone of this repository instead:

```bash
pip install -e sdk/python
```

## Changes in this version

- `report_error()` is new: it sends one error report to the server and returns `True` when the
  server stored it.
- `track()` returns `True` when the event was sent and the server answered 2xx, and `False`
  otherwise. Before this version, a `track()` without a key returned `True` only when the server
  accepted every entry it was fanned out to. Now a batch answered 2xx returns `True` even when the
  server refused one of its entries; `track_batch()` returns the per-entry results.
- A `track()` without a key still reaches the experiments and flags this client answered for the
  user in the last 24 hours (with the exceptions under Tracking fan-out rule) after their cached
  answers expire; before this version it reached only the answers still in the cache. Those keys
  are this client instance's only: an event tracked by another instance, in this process or
  another, reaches none of them.
- With nothing to reach, a `track()` without a key is now sent once with no key and stored as
  history, and returns `True` on a 2xx; before this version it sent nothing and returned `False`.

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
flag = client.get_feature_flag("new_search", "user-123")          # FlagEvaluation(key, enabled, config, reason)
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
| `get_feature_flag(flag_key, user_id, user_attributes=None)` | `FlagEvaluation(key, enabled, config, reason)` | raises `ExperimentationError` (`status == 404` when the flag key is unknown; a flag that is not ACTIVE returns `enabled=False`, `reason="inactive"`) |
| `is_feature_enabled(flag_key, user_id, user_attributes=None)` | `bool` | returns `False` |
| `get_all_flags(user_id, user_attributes=None)` | `dict[str, bool]` (not cached) | raises `ExperimentationError` |
| `track(user_id, event_name, event_value=None, properties=None, experiment_key=None, feature_flag_key=None, event_type=None, timestamp=None)` | `bool` — `True` when sent and answered 2xx | never raises; `False` on failure |
| `track_batch(events)` | `BatchResult(success_count, failure_count, errors)`; `.ok` | never raises; a failed chunk counts all its events as failures |
| `report_error(error_type, message, *, feature_flag_key=None, experiment_key=None, user_id=None, stack_trace=None, metadata=None, timestamp=None)` | `bool` — `True` when the server stored the report (201) | never raises; `False` and a logged warning otherwise |
| `get_assignments(user_id, active_only=True)` | `list[dict]` from the server | raises `ExperimentationError` |
| `cached_assignments(user_id)` / `cached_flags(user_id)` | cached, unexpired entries | — |
| `clear_cache()` | — (keeps the keys a key-less `track` is sent to) | — |
| `ready(timeout_seconds=None)`, `status()`, `close()` / `with` | local mode: readiness, ruleset state, shutdown (sends evaluation counts) | never raise |
| `consistent_hash(user_id, flag_key)` / `md5_hex(user_id, flag_key)` | cross-SDK MD5 bucket in `[0, 1)` / hex digest | pure functions; **not** the flag bucketing function |

`ExperimentationError` carries `.status` (HTTP status, `None` for network errors/timeouts) and
`.body` (raw response text). Failures are never cached, so the next call retries. A `429` is
retried once after `Retry-After` seconds (capped at 5 s).

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

## Backend endpoints used

Every request carries `X-API-Key: <key>`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `get_assignment`, `get_variant` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when no experiment has the key, or for a draft with no control variant; an experiment that is not ACTIVE answers its control variant with `assigned: false` |
| `get_feature_flag`, `is_feature_enabled` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=<url-encoded JSON>` | `context` = `user_attributes` (omitted when empty) | `{key, enabled, config, reason}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `get_all_flags` | `GET /api/v1/feature-flags/user/{user_id}?context=<url-encoded JSON>` | `context` = `user_attributes` (omitted when empty) | `{"<flag_key>": bool, …}` |
| `track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `track` without keys | `POST /api/v1/tracking/batch`, one entry per experiment and flag it reaches; with none, `POST /api/v1/tracking/track` | `{events: [<track body>, …]}` (≤ 100 per request); with none, the track body with no key | ignored |
| `track_batch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (≤ 100 per request) | `{success_count, failure_count, errors}` |
| `get_assignments` | `GET /api/v1/tracking/assignments/{user_id}?active_only=true` | — | list of dicts |

## Contract smoke

```bash
CONTROLVAR_API_KEY=<key> python sdk/python/examples/contract_smoke.py
```

It prints:

```text
{"sdk":"python","assign":{"variant_name":"treatment","is_control":false,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
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
