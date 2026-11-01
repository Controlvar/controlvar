# Controlvar Go SDK

Go client for the Controlvar A/B testing and feature flag platform. Zero dependencies outside
the standard library, safe for concurrent use.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key for a TTL. Nothing is bucketed locally.

Full reference: [`docs/sdk/go.md`](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/go.md).

## Installation

```bash
go get github.com/controlvar/controlvar/sdk/go
```

- `go get github.com/controlvar/controlvar/sdk/go`: Go 1.21+

```go
import exp "github.com/controlvar/controlvar/sdk/go"
```

## Changes

If your `go.mod` holds a pseudo-version from before this note, the next `go get` brings two
changes:

- `EvaluateFlag` sends `User.Attributes` as `context`, as `GetAssignment` does, so a flag's
  targeting rules on attributes now match from Go. Answers for a flag with such rules change: a
  user a rule matches gets the rule's answer, where before the flag's rollout answered.
- Attributes that cannot be encoded as JSON (a NaN or infinite number, a channel, a func) now make
  `EvaluateFlag` return the disabled result and an error, and send nothing, as `GetAssignment`
  does with them. Before, `EvaluateFlag` did not send attributes at all, so they caused no error.

## Quick Start

```go
client, err := exp.New(
    exp.WithBaseURL(os.Getenv("CONTROLVAR_API_URL")), // origin only, e.g. http://localhost:8000
    exp.WithAPIKey(os.Getenv("CONTROLVAR_API_KEY")),  // sent as X-API-Key
)
if err != nil {
    log.Fatal(err)
}
defer client.Close()

ctx := context.Background()
user := &exp.User{ID: "user-123", Attributes: map[string]interface{}{"plan": "pro", "country": "US"}}

// Experiment assignment — POST /api/v1/tracking/assign (sticky on the server)
a, err := client.GetAssignment(ctx, "checkout_flow", user)
if err == nil {
    fmt.Println(a.VariantName, a.IsControl, a.Configuration) // "treatment" false map[...]
}

// Feature flag — GET /api/v1/feature-flags/evaluate/new_search?user_id=user-123&context=...
// (the user's attributes as JSON)
flag, err := client.EvaluateFlag(ctx, "new_search", user)
if flag.Enabled { /* flag is off (and err != nil) on any failure */ }

// Track with a key — one POST /api/v1/tracking/track
_ = client.Track(ctx, &exp.TrackEvent{
    UserID: user.ID, EventName: "purchase", ExperimentKey: "checkout_flow",
    Value: exp.Float64(49.99), Properties: map[string]interface{}{"currency": "USD"},
})

// Track without a key — fanned out to every cached assignment + flag for the user
_ = client.Track(ctx, &exp.TrackEvent{UserID: user.ID, EventName: "page_view"})
```

## Configuration

`exp.New` takes functional options:

| Option | Type | Default | Description |
|---|---|---|---|
| `WithBaseURL` | `string` | `http://localhost:8000` | Backend origin; the SDK appends `/api/v1/...`. A trailing `/` is stripped; empty is an error |
| `WithAPIKey` | `string` | `""` | Sent as `X-API-Key` on every request |
| `WithTimeout` | `time.Duration` | `10s` | Per-request HTTP timeout (also honours `ctx`) |
| `WithCacheTTL` | `time.Duration` | `5m` | How long a successful evaluation/assignment is reused; `<= 0` never expires |
| `WithCacheSize` | `int` | `1000` | Max cached evaluations + assignments (LRU eviction) |
| `WithLocalEval` | `bool` | — | Deprecated no-op kept for source compatibility |

## API

| Method | Returns | On failure |
|---|---|---|
| `EvaluateFlag(ctx, flagKey, *User)` | `(*EvalResult, error)` | `&EvalResult{Key, Enabled: false}` + `error` (`nil` result only for empty `flagKey`/`user.ID`) |
| `GetAssignment(ctx, experimentKey, *User)` | `(*Assignment, error)` | `(nil, error)` |
| `Track(ctx, *TrackEvent)` | `error` | Informational error; never panics |
| `TrackBatch(ctx, []*TrackEvent)` | `error` | Informational error; at most 100 events per request |
| `Close()` | `error` (always `nil`) | Clears the cache, closes idle connections; idempotent |
| `(*EvalResult).StringValue(def string)` | `string` | `def`; never panics (Flag values, below) |
| `(*EvalResult).NumberValue(def float64)` | `float64` | `def`; never panics (Flag values, below) |
| `(*EvalResult).DecodeValue(v any)` | `error` | An error, with `v` left as it was; never panics (Flag values, below) |
| `ConsistentHash(userID, key)` | `float64` in `[0, 1)` | Pure function (parity utility, see below) |

Types (`types.go`):

| Type | Fields |
|---|---|
| `User` | `ID string`, `Attributes map[string]interface{}` (sent as `context` on assignment and on flag evaluation; Targeting attributes, below) |
| `EvalResult` | `Key string`, `Enabled bool`, `Config map[string]any` (always `nil`: the evaluate endpoint returns `config: null` for every flag), `Value json.RawMessage` (what the flag serves this user; `nil` when the server sent none), `ValueType string` (`"boolean"`, `"string"`, `"number"` or `"json"`; `""` when the server sent none) |
| `Assignment` | `ExperimentKey`, `UserID`, `VariantID`, `VariantName string`, `IsControl bool`, `Configuration map[string]any` |
| `TrackEvent` | `UserID`, `EventName` (required), `EventType` (defaults to `EventName`), `ExperimentKey`, `FeatureFlagKey`, `Value *float64` (`exp.Float64(v)`), `Properties` (sent as `metadata`), `Timestamp time.Time` (ISO-8601 when non-zero) |
| `APIError` | `StatusCode int`, `Message string`, `RetryAfter string` (429); use `errors.As` |

## Flag values

`EvalResult.Value` is what the flag serves this user: its on value when `Enabled` is true, its
off value otherwise (`true` or `false` for a boolean flag). `EvalResult.ValueType` is the flag's
type. Read the value with the method for its type:

```go
label, _ := client.EvaluateFlag(ctx, "checkout-button-label", user) // a string flag
fmt.Println(label.StringValue("Buy now"))

limit, _ := client.EvaluateFlag(ctx, "max-items", user) // a number flag
fmt.Println(limit.NumberValue(10))

hero, _ := client.EvaluateFlag(ctx, "hero-banner", user) // a json flag
banner := Banner{Title: "Welcome"}                       // your default
if err := hero.DecodeValue(&banner); err != nil {
    // banner is still your default
}
```

- `StringValue` and `NumberValue` return your default, and `DecodeValue` returns an error and
  leaves `v` as it was, when there is no value of that type to read: a nil result, a failed
  evaluation, a flag of another type, or a value that does not decode.
- Against a server that sends no `value`, `StringValue` returns your default.
- None of them panics, on a nil `*EvalResult` included.
- `DecodeValue` decodes into a new value of `v`'s type and stores it in `v` only on success, so a
  field the value does not name is zero, not your default.
- `EvaluateFlag` returns a new copy on every call, cache hits included, so changing `Value`
  changes nothing the client has cached.

## Targeting attributes

`User.Attributes` are sent as `context` on both calls, so targeting rules can match them: in the
`GetAssignment` body, and on `EvaluateFlag` as a `context` query parameter holding the attributes
as compact JSON, URL-encoded. With nil or empty attributes, `EvaluateFlag` sends no `context`.

- Attributes are assumed stable per user: the cache key is the user's ID and the flag or
  experiment key, not the attributes. Within `CacheTTL`, a call for the same user and key gets
  the cached answer and sends nothing, whatever attributes it carries.
- Attributes must encode as JSON. With a NaN or infinite number, a channel or a func among them,
  `EvaluateFlag` returns the disabled result and an error wrapping the `encoding/json` error,
  and `GetAssignment` returns `nil` and that error; neither sends nor caches anything.
- The SDK does not check the server's
  [targeting context limits](https://github.com/Controlvar/controlvar/blob/main/docs/api/endpoints.md#targeting-context-limits).
  A `context` over them is answered 422, and a proxy that refuses a long URL may answer 400 or
  414; `EvaluateFlag` then returns the disabled result and the `*APIError`, and caches nothing.

## Caching and failure behaviour

- Successful evaluations and assignments are cached per **user + key** for `CacheTTL`
  (default 5 min, LRU-bounded by `CacheSize`). A cache hit makes no request; the second
  `GetAssignment` for the same user + experiment is served locally.
- **Failures are never cached**, so the next call retries.
- Network error, timeout, 401 (bad key), 404 (an unknown experiment or flag key, or a draft experiment with no control variant), 429
  (rate limited, `APIError.RetryAfter`): `EvaluateFlag` returns a **disabled** result plus the
  error; `GetAssignment` returns `nil` plus the error. Non-2xx responses are `*APIError`.
- `Track`/`TrackBatch` never panic; the returned error is safe to ignore.

## Tracking fan-out

With `ExperimentKey` and/or `FeatureFlagKey` set, `Track` sends one `POST /api/v1/tracking/track`.
Without a key it sends one `POST /api/v1/tracking/batch` containing one entry per experiment the
user was assigned to through this client (`experiment_key`) plus one per flag evaluated for the
user (`feature_flag_key`), taken from the cache. Nothing cached → nothing is sent. This is what
makes a single `Track("purchase")` count as a conversion for every experiment the user is in.
Conversions are matched to metrics by **event name**.

This SDK reads only the variant from an answer: an `assigned: false` answer (a user the
experiment did not enrol, or an experiment that is not ACTIVE) is cached and returned as the
control variant, and an event tracked without a key after the user's cache entries have expired
(`CacheTTL`, 5 minutes by default) is not sent, so pass `ExperimentKey` on a conversion that can
come later.

## Consistent hash (compatibility utility)

`exp.ConsistentHash(userID, key)` computes `MD5("{userID}:{key}")`, reads the first 4 bytes as a
little-endian uint32 and divides by 2^32. It is exported only so the cross-SDK golden vectors in
`tests/sdk-contract/` keep passing — **nothing in the SDK buckets locally**; the server decides.

```go
exp.ConsistentHash("user-123", "my-flag") // 0.6927449859213084
```

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `EvaluateFlag` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=…` | `context`: the user's attributes as URL-encoded JSON, only when there are any | `{key, enabled, value, value_type, config}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `GetAssignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when no experiment has the key, or for a draft with no control variant; an experiment that is not ACTIVE answers its control variant with `assigned: false` |
| `Track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `Track` without keys, `TrackBatch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | ignored |

## Contract smoke

```bash
cd sdk/go && go run ./examples/contract_smoke
```

It prints:

```text
{"sdk":"go","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `CONTROLVAR_API_URL` (default `http://localhost:8000`), `CONTROLVAR_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). Seed the fixtures with
`backend/scripts/seed_sdk_contract.py`; the repo-wide runner is
`python tests/sdk-contract/live/run_live_contract.py --sdk go --strict`.

CI runs this SDK's contract smoke against a real API: `go` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml).

## Tests

```bash
cd sdk/go && go vet ./... && go test -race ./...
```

- `cd sdk/go && go vet ./... && go test -race ./...`: HTTP mocked with httptest

A longer walkthrough lives in `examples/main.go` (`go run ./examples`).
