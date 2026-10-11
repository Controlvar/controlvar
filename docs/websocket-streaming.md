# EP-058: Real-time WebSocket Streaming Results

Live experiment results delivered over WebSocket — no polling required.

## Overview

EP-058 adds a WebSocket endpoint that streams experiment result snapshots to
connected clients. The frontend uses the `useExperimentStream` hook and the
`LiveResultsPanel` component to display live data in the results dashboard.

## Architecture

```text
Browser (LiveResultsPanel)
    |
    | WebSocket ws://host/api/v1/ws/experiments/{id}/results
    |
ConnectionManager  ←──── broadcasts to all subscribers of this experiment
    |
ResultsStreamingService
    |
    | Queries assignments + events tables
    |
PostgreSQL (Aurora)
```

## Backend

### WebSocket Endpoint

```text
ws://localhost:8000/api/v1/ws/experiments/{experiment_id}/results
```

On connect the server sends an initial snapshot. Periodic updates are sent every
30 seconds (configurable via `RESULTS_STREAM_INTERVAL_SECONDS`). The socket needs an
access token; see [Authentication](#authentication) below for the three ways to send it.

Run the commands on this page in one terminal, in order, against the stack from the
[Quick Start](getting-started/quick-start.md). Each uses the shell variables set by the
ones before it. Log in first:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`. This saves the id of the demo data's `checkout_button_color`
experiment in `$EXP_ID`. The collection URL ends with a slash, `/api/v1/experiments/`;
without it the API answers `307`, which `curl` doesn't follow:

```{.bash exec}
EXP_ID=$(curl -s localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" \
  | jq -r '.items[] | select(.key == "checkout_button_color") | .id')

echo "$EXP_ID" | wc -c
```
<!-- expect: 37 -->

It prints `37`, the length of an id and its newline.

Any WebSocket client can connect, such as `websocat` or a browser. `curl` alone can show
the first snapshot: it sends the upgrade request with the token as the subprotocol, then
prints what arrives until `--max-time` stops it three seconds later. That stop is exit
code 28, which the block accepts. The snapshot arrives inside a binary WebSocket frame, so
`grep` reads it as bytes (`LC_ALL=C grep -a`) and picks out the event, status and variant
keys:

```{.bash exec}
{ curl -s -N --http1.1 --max-time 3 \
    -H 'Connection: Upgrade' \
    -H 'Upgrade: websocket' \
    -H 'Sec-WebSocket-Version: 13' \
    -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
    -H "Sec-WebSocket-Protocol: experimently.bearer, $TOKEN" \
    localhost:8000/api/v1/ws/experiments/$EXP_ID/results || [ $? -eq 28 ]; } \
  | LC_ALL=C grep -aoE '"(event|status|key)": "[a-z_]*"'
```
<!-- expect: "event": "results_update" -->
<!-- expect: "status": "active" -->
<!-- expect: "key": "blue_button" -->
<!-- expect: "key": "green_button" -->

It prints `"event": "results_update"`, the experiment's `"status": "active"`, and its two
variants, `blue_button` and `green_button`. The whole snapshot has the shape below.

### Client Message Protocol

| Client sends                  | Server responds                |
|-------------------------------|--------------------------------|
| `{"action": "ping"}`          | `{"event": "pong"}`            |
| `{"action": "refresh"}`       | Fresh snapshot (same shape)    |
| Any unknown action            | Ignored (no crash)             |

### Snapshot Schema

```json
{
  "event": "results_update",
  "experiment_id": "uuid-string",
  "timestamp": "2026-03-07T12:00:00+00:00",
  "status": "active",
  "variants": [
    {
      "key": "control",
      "name": "Control",
      "participant_count": 500,
      "conversion_count": 50,
      "conversion_rate": 0.10,
      "relative_lift": 0.0,
      "p_value": null,
      "is_control": true
    },
    {
      "key": "variant_b",
      "name": "Variant B",
      "participant_count": 500,
      "conversion_count": 72,
      "conversion_rate": 0.144,
      "relative_lift": 0.44,
      "p_value": 0.033531,
      "is_control": false
    }
  ],
  "total_participants": 1000,
  "days_running": 7,
  "is_significant": true
}
```

`p_value` is an unadjusted, two-sided pooled two-proportion z-test of the variant
against the control, computed from each arm's own participants and conversions (the
arms need not be the same size). It is `null` for the control, and for a variant
whose test is undefined (a control with no participants, fewer than 5 variant
participants, or no conversions at all in the two arms together).

### HTTP Companion Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/ws/experiments/{id}/results/subscribers` | Active subscriber count |
| `GET` | `/api/v1/ws/active-experiments` | All experiments with active subscribers |

Both are informational and need no token. With the `curl` connection above closed,
nobody is subscribed:

```{.bash exec}
curl -s localhost:8000/api/v1/ws/experiments/$EXP_ID/results/subscribers | jq .subscriber_count
curl -s localhost:8000/api/v1/ws/active-experiments
```
<!-- expect: 0 -->
<!-- expect: {"active_experiments":[]} -->

It prints `0`, then `{"active_experiments":[]}`.

### Key Classes

**`ConnectionManager`** (`backend/app/services/websocket_manager.py`):
- Thread-safe `asyncio.Lock`-protected subscriber registry
- `connect(ws, experiment_id)` — accept and register
- `disconnect(ws, experiment_id)` — deregister; cleans up empty experiment keys
- `broadcast(experiment_id, message)` — sends to all; removes dead connections

**`ResultsStreamingService`** (`backend/app/services/results_streaming_service.py`):
- `get_live_snapshot(experiment_id)` — queries DB, returns snapshot dict
- Uses a `db_session_factory` callable for fresh sessions per snapshot
- Gracefully handles missing experiments and DB errors (returns error snapshot)
- `compute_and_broadcast(manager, experiment_id)` — convenience wrapper

### Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `RESULTS_STREAM_INTERVAL_SECONDS` | `30` | Periodic update interval |

## Frontend

### `useExperimentStream` Hook

```typescript
import { useExperimentStream } from '@/hooks/useExperimentStream';

function MyComponent({ experimentId }: { experimentId: string }) {
  const { snapshot, status, error, refresh, disconnect } =
    useExperimentStream(experimentId);

  // status: 'connecting' | 'connected' | 'disconnected' | 'error'
  // snapshot: ExperimentSnapshot | null
}
```

The hook auto-connects when `experimentId` is set and auto-reconnects up to 5
times (configurable) on unexpected disconnection.

WebSocket URL: `NEXT_PUBLIC_WS_URL` env var (default: `ws://localhost:8000`).

### `LiveResultsPanel` Component

```tsx
import { LiveResultsPanel } from '@/components/experiments/LiveResultsPanel';

<LiveResultsPanel experimentId="exp-uuid" />
```

Features:
- Connection status badge (green/yellow/grey/red dot)
- Pulsing "LIVE" indicator when connected
- Variant results table: Name | Participants | Conversions | Rate | Lift | P-value | Status
- Refresh button
- Last updated timestamp
- Auto-reconnect countdown when disconnected
- Error banner with reconnect button

### Results Dashboard Integration

The `LiveResultsPanel` is accessible via the **⚡ Live** tab in the
`ResultsDashboard` component on the `/results/[id]` page.

## Tests

### Running

In a development checkout, with the virtual environment active, this runs the two unit
test files and the integration test file together:

```{.bash skip reason="dev: runs this repository's test suite in a development checkout"}
source venv/bin/activate
export APP_ENV=test TESTING=true
python -m pytest \
  backend/tests/unit/services/test_websocket_manager.py \
  backend/tests/unit/services/test_results_streaming_service.py \
  backend/tests/integration/api/test_websocket_results.py \
  -v --tb=short
```

Pass one of the three paths instead to run that file alone.

### Test Coverage

| Test file | Covers |
|-----------|--------|
| `test_websocket_manager.py` | ConnectionManager lifecycle, broadcast, concurrency |
| `test_results_streaming_service.py` | Snapshot schema, DB error handling, significance logic |
| `test_websocket_results.py` | WebSocket protocol, HTTP endpoints, edge cases |

All tests mock the DB session; no live PostgreSQL required.

## Authentication

The stream requires the same credentials as the HTTP API. The token is validated
before the socket is registered; a missing or invalid token is answered with an
accepted-then-closed socket, close code **4401**, so browser clients can tell
"unauthorized" from a network failure and must not reconnect with the same token.

Ways to present the token, in order of preference:

1. **Subprotocol** (what the dashboard uses):
   `new WebSocket(url, ['experimently.bearer', token])`. The server echoes
   `experimently.bearer` as the accepted subprotocol. This keeps the token out of
   URLs and therefore out of access logs.
2. **`Authorization: Bearer <token>` header** for non-browser clients.
3. **`?token=<token>` query parameter**, supported for compatibility only. URLs are
   written to proxy access logs, so prefer 1 or 2. (The API's own log writes the
   value as `[redacted]`. The dashboard's nginx, which proxies `/api/` in Docker
   Compose, leaves the query string out of its access log; its error log, written
   when it cannot reach the API, still includes it.)

`frontend/src/hooks/useExperimentStream.ts` stops reconnecting and reports
`status: 'unauthorized'` on close code 4401.

The HTTP companion endpoints (subscriber counts, active experiments) are read-only
and informational.
