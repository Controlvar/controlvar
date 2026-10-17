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
	// A nil assignment and an error: not ACTIVE (404), bad key (401), network error.
	// A service would fall back to control here; the quick start stops.
	a, err := client.GetAssignment(ctx, "checkout_flow", user)
	if err != nil {
		log.Fatalf("assignment: %v", err)
	}

	// 2. Feature flag (GET /api/v1/feature-flags/evaluate/new_search?user_id=user-123).
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
```

| Method | Endpoint | Returns | On failure |
|---|---|---|---|
| `EvaluateFlag` | `GET /feature-flags/evaluate/{key}?user_id=` | `*EvalResult` | `&EvalResult{Key: flagKey, Enabled: false}` **and** `error`. Only invalid arguments (empty `flagKey`, nil user / empty `user.ID`) return a `nil` result |
| `GetAssignment` | `POST /tracking/assign` | `*Assignment` | `(nil, error)` |
| `Track` | `/tracking/track` or `/tracking/batch` | `error` | Never panics; validation errors (nil event, empty `UserID`, no `EventName`/`EventType`) are returned before any request |
| `TrackBatch` | `/tracking/batch` (chunks of 100) | `error` | As `Track`; keyed events are batched as-is, unkeyed ones fanned out |
| `Close` | — | `nil` | Clears the cache and idle connections; safe to call twice |

### Types

| Type | Fields |
|---|---|
| `User` | `ID string` (required), `Attributes map[string]interface{}` — sent as `context` on assignment |
| `EvalResult` | `Key string`, `Enabled bool`, `Config map[string]any` — always `nil`: the evaluate endpoint returns `config: null` for every flag |
| `Assignment` | `ExperimentKey`, `UserID`, `VariantID` (UUID), `VariantName` (`"control"`, `"treatment"`, …), `IsControl bool`, `Configuration map[string]any` |
| `TrackEvent` | `UserID`, `EventName`, `EventType` (→ `event_type`, defaults to `EventName`), `ExperimentKey`, `FeatureFlagKey`, `Value *float64`, `Properties` (→ `metadata`), `Timestamp time.Time` (→ RFC 3339 when non-zero, else server-stamped) |
| `APIError` | `StatusCode int`, `Message string` (response body), `RetryAfter string` (raw header on 429) |

```go
var apiErr *exp.APIError
if _, err := client.GetAssignment(ctx, "exp", user); errors.As(err, &apiErr) && apiErr.StatusCode == 404 {
    // experiment unknown or not ACTIVE
}
```

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
| `EvaluateFlag` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; `enabled: false`, `reason: "inactive"` when the flag is not ACTIVE; 404 only for an unknown key |
| `GetAssignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when the experiment is not ACTIVE |
| `Track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `Track` without keys, `TrackBatch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | ignored |

Errors: 401 bad key, 404 (an experiment unknown or not ACTIVE, or an unknown flag key), 422 invalid event, 429
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
