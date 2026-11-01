# controlvar-openfeature (Python)

**Beta.** This is a 0.x release: pin the exact version you test against.

[OpenFeature](https://openfeature.dev) provider for Controlvar. Flags are
evaluated **by the server** through the [`controlvar`](https://github.com/Controlvar/controlvar/tree/main/sdk/python) Python SDK
(`GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=<targeting_key>&context=<attributes>`) and cached per
user + flag; nothing is bucketed locally unless you pass a `client=` built with `evaluation="local"`.
Boolean, string, integer, float and object calls are answered.

## Install

Install the provider and the Python SDK from the root of a clone of this repository
(`openfeature-sdk` is installed as a dependency):

```bash
pip install -e sdk/python -e sdk/openfeature-python
```

Requires Python 3.9+ and `openfeature-sdk` 0.10.0 or later.
The only other dependency is [`controlvar`](https://github.com/Controlvar/controlvar/tree/main/sdk/python), which itself has none.
Install it from the same clone, as above: the `controlvar` 0.2.0 on PyPI reads no flag value, so
with it every string, integer, float and object call answers your default with `GENERAL`.

## Quick start

```python
import os
from openfeature import api
from openfeature.evaluation_context import EvaluationContext
from controlvar_openfeature import ExperimentationProvider

provider = ExperimentationProvider(
    api_key=os.environ["CONTROLVAR_API_KEY"],
    base_url=os.environ.get("CONTROLVAR_API_URL", "http://localhost:8000"),
    cache_ttl=300,   # seconds a successful evaluation is reused per user + flag
    timeout=10,      # seconds
)
api.set_provider(provider)
client = api.get_client()

# targeting_key == platform user_id (required); the attributes go to the flag's targeting rules.
ctx = EvaluationContext(targeting_key="user-12345", attributes={"plan": "enterprise"})
enabled = client.get_boolean_value("dark-mode", False, ctx)                       # -> enabled
label = client.get_string_value("checkout-button-label", "Buy now", ctx)          # -> the flag's string value
max_items = client.get_integer_value("cart-max-items", 10, ctx)                   # -> the flag's number value
theme = client.get_object_value("checkout-theme", {"color": "blue"}, ctx)         # -> the flag's JSON value

# Experiments and event tracking are not part of OpenFeature: use the SDK client underneath.
assignment = provider.client.get_assignment("checkout_flow", "user-12345")
provider.client.track("user-12345", "purchase", event_value=49.99, experiment_key="checkout_flow")
```

Constructor: `ExperimentationProvider(api_key, base_url="http://localhost:8000", cache_ttl=300,
timeout=10, *, client=None, transport=None)`. Pass `client=` to share one
`controlvar.ExperimentationClient` (and its cache) with the rest of your application.

## Resolution rules

| OpenFeature call | Reads | Value |
|---|---|---|
| `get_boolean_*` | a boolean flag | `enabled` |
| `get_string_*` | a string flag | the flag's value |
| `get_integer_*` | a number flag | the flag's value when it has no fractional part (`10` or `10.0` answers `10`) |
| `get_float_*` | a number flag | the flag's value as a float (`10` answers `10.0`) |
| `get_object_*` | a json flag | a new copy of the flag's value (an object or an array) |

The flag's value is its on value when the flag is on for the user, and its off value otherwise,
an inactive flag included. Nothing is coerced: a call of another type gets your default, a
boolean call on a string, number or json flag included.

| The server's answer | Value | Reason | Variant |
|---|---|---|---|
| `reason: "targeting_rule"`, on / off | the on value / the off value | `TARGETING_MATCH` | `on` / `off` |
| `reason: "rollout"`, on / off | the on value / the off value | `SPLIT` | `on` / `off` |
| `reason: "inactive"` | the off value, not your default | `DISABLED` | `off` |
| answered from the SDK's cache | as cached | `CACHED` | as cached |
| `reason: "error"` | your default | `ERROR`, `GENERAL` | not set |
| a flag of another type than the call | your default | `ERROR`, `TYPE_MISMATCH` | not set |
| no value (a string, integer, float or object call; a server or an SDK without flag values) | your default | `ERROR`, `GENERAL` | not set |
| a value that is not of the flag's type | your default | `ERROR`, `PARSE_ERROR` | not set |

That table is for every call type. A boolean call reads `enabled`, so an answer with no
`value_type` (a server or an SDK without flag values, where every flag is boolean) answers it
rather than your default. A server reason the provider does not know answers the value with
reason `UNKNOWN`. An integer call on a number with a fractional part (`2.5`) answers
`TYPE_MISMATCH`, and `bool` is never a number.

- `targeting_key` → `user_id`. It is required: without it you get the default value with
  `TARGETING_KEY_MISSING`.
- The context's attributes are the user's attributes: the evaluate call carries them as its
  `context`, for the flag's targeting rules. A `datetime`, at any depth, goes as an ISO 8601
  string (a naive one is taken as UTC).
- By default the SDK caches an answer per user + flag key, and the attributes are not part of that key:
  within `cache_ttl` a second call for the same `targeting_key` and flag gets the first answer
  (reason `CACHED`), whatever its attributes. A changed attribute is seen once that answer
  expires; `cache_ttl=0` caches nothing, and `provider.client.clear_cache()` drops every answer.
- A `client=` built with `evaluation="local"` answers each call it can from its ruleset, using
  that call's attributes, and such an answer is never `CACHED`.
- Errors never raise and are never cached: 404 (an unknown flag key) → `FLAG_NOT_FOUND`; anything
  else (401, 422, 5xx, a 2xx whose body is not an evaluation, timeout, network) → `GENERAL`. Both
  return your default with reason `ERROR`.
- `client.track(event_name, ctx, TrackingEventDetails(value=…))` forwards to
  `ExperimentationClient.track` (an event with no key is fanned out by the Python SDK's rule).

### Argument order

OpenFeature's calls take your default second: `client.get_string_value("checkout-button-label",
"Buy now", ctx)`. The SDK client underneath, `provider.client`, has typed getters of its own
that take the user second and the default as a keyword:
`provider.client.get_string_value("checkout-button-label", "user-12345", {"plan": "enterprise"}, default="Buy now")`.
`default` is keyword-only, so a call written in OpenFeature's order on `provider.client` raises
`TypeError` instead of evaluating the flag for a user named "Buy now".

## Backend endpoints used

Every request carries `X-API-Key: <key>`, `Content-Type: application/json`, `Accept: application/json`.

| Call | Method and path | Response used |
|---|---|---|
| every `resolve_*_details` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…[&context=…]` | `{key, enabled, value, value_type, config, reason}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `provider.client.track` with a key | `POST /api/v1/tracking/track` | ignored |
| `provider.track`, and `provider.client.track` without a key | `POST /api/v1/tracking/batch`, one entry per experiment and flag it reaches; with none, `POST /api/v1/tracking/track` with no key | ignored |
| `provider.client.track_batch` | `POST /api/v1/tracking/batch` | `{success_count, failure_count, errors}` |
| `provider.client.get_assignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}` |

## Contract smoke

```bash
CONTROLVAR_API_KEY=<key> python sdk/openfeature-python/examples/contract_smoke.py
```

It prints:

```text
{"sdk":"openfeature-python","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `CONTROLVAR_API_URL` (default `http://localhost:8000`), `CONTROLVAR_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default `sdk_contract_flag`),
`CONTRACT_USER_ID` (default random `smoke-<uuid>`). The flag is resolved through the OpenFeature
API; assignment and tracking use `provider.client`.

CI runs this SDK's contract smoke against a real API: `openfeature-python` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml). Run it
with `python tests/sdk-contract/live/run_live_contract.py --sdk openfeature-python --strict`.

## Development

```bash
source venv/bin/activate && pip install openfeature-sdk
python -m pytest sdk/openfeature-python/tests -q -o addopts="" -p no:cacheprovider
```

`tests/conftest.py` puts `sdk/python` and `src/` on `sys.path`, so neither package needs to be installed.
