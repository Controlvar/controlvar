# controlvar-openfeature (Python)

**Beta.** This is a 0.x release: pin the exact version you test against.

[OpenFeature](https://openfeature.dev) provider for Controlvar. Flags are
evaluated **by the server** through the [`controlvar`](https://github.com/Controlvar/controlvar/tree/main/sdk/python) Python SDK
(`GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=<targeting_key>`) and cached per
user + flag; nothing is bucketed locally.

## Install

Install the provider and the Python SDK from the root of a clone of this repository
(`openfeature-sdk` is installed as a dependency):

```bash
pip install -e sdk/python -e sdk/openfeature-python
```

Requires Python 3.9+ and `openfeature-sdk` 0.10.0 or later.
The only other dependency is [`controlvar`](https://github.com/Controlvar/controlvar/tree/main/sdk/python), which itself has none.

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

ctx = EvaluationContext(targeting_key="user-12345")          # targeting_key == platform user_id (required)
enabled = client.get_boolean_value("dark-mode", False, ctx)
# Only boolean flags carry a value: string, number and object calls return your default.

# Experiments and event tracking are not part of OpenFeature: use the SDK client underneath.
assignment = provider.client.get_assignment("checkout_flow", "user-12345")
provider.client.track("user-12345", "purchase", event_value=49.99, experiment_key="checkout_flow")
```

Constructor: `ExperimentationProvider(api_key, base_url="http://localhost:8000", cache_ttl=300,
timeout=10, *, client=None, transport=None)`. Pass `client=` to share one
`controlvar.ExperimentationClient` (and its cache) with the rest of your application.

## Resolution rules

| OpenFeature call | Value |
|---|---|
| `get_boolean_*` | `enabled` |
| `get_string_*`, `get_integer_*`, `get_float_*`, `get_object_*` | the caller's default, reason `DEFAULT` |

Only boolean flags carry a value: the evaluate endpoint returns `config: null` for every flag.

- `targeting_key` → `user_id`. It is required: without it you get the default value with
  `TARGETING_KEY_MISSING`. **Context attributes are not sent** — the evaluate endpoint takes a
  `context`, which this provider does not send yet.
- A disabled flag resolves non-boolean calls to the default with reason `DISABLED`.
- Reasons: `TARGETING_MATCH` (fresh from the server, on), `DISABLED`, `CACHED` (from the SDK
  cache), `DEFAULT`, `ERROR` (`FLAG_NOT_FOUND` on 404 — an unknown flag key — otherwise
  `GENERAL`). Errors never raise and are never cached.
- `variant` on the details is not set, because `config` is always `null`.
- `client.track(event_name, ctx, TrackingEventDetails(value=…))` forwards to
  `ExperimentationClient.track` (fans out to the user's cached assignments and flags).

## Backend endpoints used

Every request carries `X-API-Key: <key>`, `Content-Type: application/json`, `Accept: application/json`.

| Call | Method and path | Response used |
|---|---|---|
| every `resolve_*_details` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | `{key, enabled, config}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `provider.track` / `provider.client.track` | `POST /api/v1/tracking/track` (with a key) or `POST /api/v1/tracking/batch` (fan-out) | ignored / `{success_count, failure_count, errors}` |
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
