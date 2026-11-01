# Go SDK

`github.com/controlvar/controlvar/sdk/go` (package `experimentation`) provides
feature flag evaluation, experiment assignment and event tracking for Go services. Standard
library only, goroutine-safe, context-aware.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key for a TTL. Nothing is bucketed locally.

Source: `sdk/go`. Requires Go 1.21+.

On macOS, use Go 1.23 or later: a program built with Go 1.21 or 1.22 may not
start there (`dyld: missing LC_UUID load command`). On Linux, Go 1.21 works: every night CI runs this page's
Installation and Quick Start with go1.21 against a running stack. Only the
Quick Start's quoted address and keys change: the stack's API address, the keys
of an experiment and a flag the run creates, and a user ID of its own take their
place, and `CONTROLVAR_API_KEY` holds a key the run creates.

---

## Installation

In a new directory, make a module for your program and add the SDK to it:

```bash
go mod init example.com/quickstart
go get github.com/controlvar/controlvar/sdk/go
```

Adding the SDK to a program that already has a `go.mod`? Run only the `go get`.

The module has no tagged release yet, so `go get` resolves the head of the
`main` branch and records it in your `go.mod` as a pseudo-version
(`v0.0.0-<date>-<commit>`). Your builds stay on that version until you run
`go get` again. To pin a particular commit, name it:
`go get github.com/controlvar/controlvar/sdk/go@<commit>`, where
`<commit>` is a commit hash from this repository.

---

## Quick Start

If you set `EXPERIMENTLY_API_KEY` or `EXPERIMENTLY_API_URL` from an earlier version of these pages, set `CONTROLVAR_API_KEY` and `CONTROLVAR_API_URL` instead.

Save this as `main.go` next to the `go.mod`, set `CONTROLVAR_API_KEY` to an
API key, and run it with `go run .`. It needs an ACTIVE experiment with the key
`checkout_flow` and a feature flag with the key `new_search`; put your own keys
in their place.

```go
package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"time"

	exp "github.com/controlvar/controlvar/sdk/go"
)

func main() {
	client, err := exp.New(
		exp.WithBaseURL("http://localhost:8000"),          // origin only; the SDK appends /api/v1/...
		exp.WithAPIKey(os.Getenv("CONTROLVAR_API_KEY")), // sent as X-API-Key
		exp.WithTimeout(5*time.Second),
	)
	if err != nil {
		log.Fatal(err)
	}
	defer client.Close()

	ctx := context.Background()
	user := &exp.User{ID: "user-123", Attributes: map[string]interface{}{"plan": "pro"}}

	// 1. Assignment (POST /api/v1/tracking/assign: sticky per user and experiment).
	// A nil assignment and an error: no experiment has the key (404), bad key (401), network error.
	// A service would fall back to control here; the quick start stops.
	a, err := client.GetAssignment(ctx, "checkout_flow", user)
	if err != nil {
		log.Fatalf("assignment: %v", err)
	}

	// 2. Feature flag (GET /api/v1/feature-flags/evaluate/new_search?user_id=user-123&context=...,
	// the user's attributes as JSON).
	// On an error the result is disabled, so a service can use it as it is.
	flag, err := client.EvaluateFlag(ctx, "new_search", user)
	if err != nil {
		log.Fatalf("flag: %v", err)
	}

	// 3. Track with a key: one POST /api/v1/tracking/track
	err = client.Track(ctx, &exp.TrackEvent{
		UserID: user.ID, EventName: "purchase", ExperimentKey: "checkout_flow",
		Value: exp.Float64(49.99), Properties: map[string]interface{}{"sku": "pro-plan"},
	})
	if err != nil {
		log.Fatalf("track: %v", err)
	}

	// 4. Track without a key: fanned out to every cached assignment and flag of this user
	err = client.Track(ctx, &exp.TrackEvent{UserID: user.ID, EventName: "page_view"})
	if err != nil {
		log.Fatalf("track: %v", err)
	}

	fmt.Printf("variant=%s flag=%t\n", a.VariantName, flag.Enabled)
}
```

It prints the user's variant and whether the flag is on for them, for example:

```text
variant=treatment flag=true
```

Errors go to stderr through `log`, and the program exits 1.

---

## Configuration

| Option | Type | Default | Description |
|---|---|---|---|
| `WithBaseURL(url)` | `string` | `http://localhost:8000` | Backend origin. Trailing `/` stripped; empty → `New` returns an error |
| `WithAPIKey(key)` | `string` | `""` | Sent as `X-API-Key` (header omitted when empty) |
| `WithTimeout(d)` | `time.Duration` | `10s` | `http.Client` timeout; requests also stop when `ctx` is cancelled |
| `WithCacheTTL(d)` | `time.Duration` | `5m` | Lifetime of a cached evaluation/assignment; `<= 0` never expires |
| `WithCacheSize(n)` | `int` | `1000` | Max entries across evaluations + assignments, LRU eviction (`<= 0` → 1) |
| `WithLocalEval(bool)` | — | — | **Deprecated** no-op; the server evaluates everything |

---

## API Reference

```go
type Client interface {
    EvaluateFlag(ctx context.Context, flagKey string, user *User) (*EvalResult, error)
    GetAssignment(ctx context.Context, experimentKey string, user *User) (*Assignment, error)
    Track(ctx context.Context, event *TrackEvent) error
    TrackBatch(ctx context.Context, events []*TrackEvent) error
    Close() error
}
func New(opts ...Option) (Client, error)
func ConsistentHash(userID, key string) float64
func Float64(v float64) *float64

func (r *EvalResult) StringValue(def string) string
func (r *EvalResult) NumberValue(def float64) float64
func (r *EvalResult) DecodeValue(v any) error
```

| Method | Endpoint | Returns | On failure |
|---|---|---|---|
| `EvaluateFlag` | `GET /feature-flags/evaluate/{key}?user_id=&context=` | `*EvalResult` | `&EvalResult{Key: flagKey, Enabled: false}` **and** `error`. Only invalid arguments (empty `flagKey`, nil user / empty `user.ID`) return a `nil` result |
| `GetAssignment` | `POST /tracking/assign` | `*Assignment` | `(nil, error)` |
| `Track` | `/tracking/track` or `/tracking/batch` | `error` | Never panics; validation errors (nil event, empty `UserID`, no `EventName`/`EventType`) are returned before any request |
| `TrackBatch` | `/tracking/batch` (chunks of 100) | `error` | As `Track`; keyed events are batched as-is, unkeyed ones fanned out |
| `Close` | — | `nil` | Clears the cache and idle connections; safe to call twice |
| `(*EvalResult).StringValue(def)` | — | `string` | `def`; never panics (see [Flag values](#flag-values)) |
| `(*EvalResult).NumberValue(def)` | — | `float64` | `def`; never panics (see [Flag values](#flag-values)) |
| `(*EvalResult).DecodeValue(v)` | — | `error` | An error, with `v` left as it was; never panics (see [Flag values](#flag-values)) |

### Types

| Type | Fields |
|---|---|
| `User` | `ID string` (required), `Attributes map[string]interface{}` — sent as `context` on assignment and on flag evaluation (see [Targeting attributes](#targeting-attributes)) |
| `EvalResult` | `Key string`, `Enabled bool`, `Config map[string]any` — always `nil`: the evaluate endpoint returns `config: null` for every flag; `Value json.RawMessage` — what the flag serves this user, `nil` when the server sent none; `ValueType string` — `"boolean"`, `"string"`, `"number"` or `"json"`, `""` when the server sent none |
| `Assignment` | `ExperimentKey`, `UserID`, `VariantID` (UUID), `VariantName` (`"control"`, `"treatment"`, …), `IsControl bool`, `Configuration map[string]any` |
| `TrackEvent` | `UserID`, `EventName`, `EventType` (→ `event_type`, defaults to `EventName`), `ExperimentKey`, `FeatureFlagKey`, `Value *float64`, `Properties` (→ `metadata`), `Timestamp time.Time` (→ RFC 3339 when non-zero, else server-stamped) |
| `APIError` | `StatusCode int`, `Message string` (response body), `RetryAfter string` (raw header on 429) |

```go
var apiErr *exp.APIError
if _, err := client.GetAssignment(ctx, "exp", user); errors.As(err, &apiErr) && apiErr.StatusCode == 404 {
    // no experiment has the key, or a draft has no control variant
}
```

---

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

---

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
- The SDK does not check the server's [targeting context limits](../api/endpoints.md#targeting-context-limits).
  A `context` over them is answered 422, and a server or proxy that refuses a long URL may answer
  400, 414 or 431; `EvaluateFlag` then returns the disabled result and the `*APIError`, and
  caches nothing.

---

## Caching and failure behaviour

- Successful results are cached per **user + key** (namespaced by kind, so a flag and an
  experiment with the same key never collide) for `CacheTTL`. A cache hit makes **no** request.
- **Failures are never cached** — the next call retries.
- Network/timeout, `401`, `404`, `422`, `429`, `5xx`: `EvaluateFlag` degrades to a disabled result
  with the error attached; `GetAssignment` returns `nil` and the error. Non-2xx responses are
  `*APIError` (`RetryAfter` set on 429); transport errors wrap the `net/http` error.
- `Track`/`TrackBatch` never panic; treat the error as informational.
- The client is safe for concurrent use: the cache is mutex-protected and `net/http` pools
  connections. Pass a cancellable `ctx` to bound latency per call.

---

## Tracking fan-out

With `ExperimentKey` and/or `FeatureFlagKey` set, `Track` sends one `POST /api/v1/tracking/track`.
Without a key it sends one `POST /api/v1/tracking/batch` with one entry per experiment the user
was assigned to through this client (`experiment_key`) plus one per flag evaluated for the user
(`feature_flag_key`), all taken from the cache. Nothing cached → nothing is sent and `nil` is
returned. Batches are chunked at 100 events.

This SDK reads only the variant from an answer: an `assigned: false` answer (a user the
experiment did not enrol, or an experiment that is not ACTIVE) is cached and returned as the
control variant, and an event tracked without a key after the user's cache entries have expired
(`CacheTTL`, 5 minutes by default) is not sent, so pass `ExperimentKey` on a conversion that can
come later.

Conversions are matched to metrics by **event name**: a metric whose `event_name` is `purchase`
counts every `purchase` event regardless of `event_type`.

---

## Consistent hash (compatibility utility)

`ConsistentHash(userID, key)` = `MD5("{userID}:{key}")`, first 4 bytes as little-endian uint32,
divided by 2^32. It exists only so the golden vectors in `tests/sdk-contract/golden-vectors.json`
stay identical across SDKs. **Nothing in the SDK calls it to pick a variant** — the server decides.

```go
exp.ConsistentHash("user-123", "my-flag") // 0.6927449859213084
```

---

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `EvaluateFlag` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=…` | `context`: the user's attributes as URL-encoded JSON, only when there are any | `{key, enabled, value, value_type, config}`; `enabled: false`, `reason: "inactive"` when the flag is not ACTIVE; 404 only for an unknown key |
| `GetAssignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when no experiment has the key, or for a draft with no control variant; an experiment that is not ACTIVE answers its control variant with `assigned: false` |
| `Track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `Track` without keys, `TrackBatch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | ignored |

Errors: 401 bad key, 404 (an unknown experiment or flag key, or a draft experiment with no control variant), 422 invalid event, 429
rate limited (`Retry-After`). These paths share the backend's per-IP `SDK_RATE_LIMIT_PER_MINUTE`
ceiling (default 6000).

---

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
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). The smoke assigns
three times (twice on one client, once on a fresh client) to prove server-side stickiness,
evaluates the flag, tracks `purchase` with the experiment key, tracks `page_view` without a key
and sends a 2-event `TrackBatch`. Fixtures: `backend/scripts/seed_sdk_contract.py`; repo-wide
runner: `python tests/sdk-contract/live/run_live_contract.py --sdk go --strict`.

CI runs this SDK's contract smoke against a real API: `go` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml).

---

## Development

```bash
cd sdk/go
go vet ./... && go test -race ./...
go run ./examples
```

- `go vet ./... && go test -race ./...`: HTTP mocked with net/http/httptest
- `go run ./examples`: examples/main.go walkthrough against a running backend
