# StreamPulse device simulator + rollout story

Python 3.11, stdlib only. Talks to the platform exactly like the app does: the public API with the
`streampulse-app` API key (`demo/streampulse/.api_key`, written by `backend/scripts/seed_streampulse.py`)
for devices, the StreamPulse server key (`streampulse-server`, scope `sdk:ruleset`,
`demo/streampulse/.api_key_server`, written by the same seed) for the crash reports, and the dashboard
API with a bearer token for the story.

| File | What it is |
|---|---|
| `devices.py` | Deterministic population of simulated devices (seeded RNG) following the spec mix: iOS 55% (16.x/17.x/18.x = 20/60/20), Android 45% (12.x/13.x/14.x = 25/35/40), regions US 45 · GB 15 · DE 12 · IN 18 · BR 10, premium 30%, employee 1%, app 3.1.0/3.2.0/3.2.1 = 30/50/20. `Device.attributes()` is the targeting `context` (`os`, `os_version`, `app_version`, `region`, `tier`, `employee`, `device_model`, `device_id`). `PRESET_DEVICES` is the app's five presets, with the same device ids and attributes (`demo/streampulse/src/lib/devices.ts`). |
| `traffic.py` | Ticks through app sessions: evaluates the 4 flags (`GET /feature-flags/evaluate/{key}?user_id=&context=<url-encoded JSON>`), assigns the 5 experiments (`POST /tracking/assign` with `context`), walks the screens with the spec's true rates and sends the §3 events through `POST /tracking/batch`. `assigned: false` (global holdout, mutual exclusion group, targeting) means no events for that experiment. Prints stats every 10 s. |
| `rollout_story.py` | The 7-step "Player v2" narrative (see below). |
| `test_traffic.py`, `test_story.py` | Offline pytest tests (fake clients, no network). |

## Traffic

From the repository root, with the venv active, this sends steady traffic, 6 sessions a second
for five minutes:

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
source venv/bin/activate
python demo/streampulse/simulator/traffic.py --rate 6 --duration 300
```

The crash incident, for 90 seconds:

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
python demo/streampulse/simulator/traffic.py --incident android12 --rate 6 --duration 90
```

`--dry-run` prints one session's planned events without touching the network; with
`--incident android12` the session is a crashing one:

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
python demo/streampulse/simulator/traffic.py --dry-run --seed 1
python demo/streampulse/simulator/traffic.py --dry-run --incident android12
```

Options: `--api-url` (default `http://localhost:8000`), `--api-key` (default: `demo/streampulse/.api_key`),
`--server-api-key` (the key the crash reports are sent with; default: `demo/streampulse/.api_key_server`),
`--rate` sessions/s, `--duration` seconds (0 = forever), `--seed`, `--devices N` (population size, default 5000),
`--population-seed` (keep it fixed so device ids are stable), `--incident {off,android12}`, `--max-ticks`,
`--no-probe`, `--dry-run`.

A session is one of the `--devices` devices that have the app at that moment; after it a device comes back with
probability 0.25 (`RETURN_RATE`), and otherwise, or after an uninstall, a new install takes its place.

At start-up it prints what the app's five preset devices get right now (flag on/off + `reason`, variant + `assigned`/`reason`).
The employee's device gets `player_v2=ON(targeting_rule)`, and `ai_search` is ON only for iOS 17+ / US / premium.

Exit codes: `0` ok · `2` bad or missing API key, or `--incident` without the server key · `3` catalogue not
seeded · `4` API unreachable.

### The incident model (`--incident android12`)

A broken Player v2 build ships to Android 12. 40% of Android 12 devices (stable per device id) that have
`streampulse_player_v2` ON crash when the player opens, and each crash is reported with
`POST /tracking/errors {feature_flag_key: streampulse_player_v2, error_type: "crash", message: "NullPointerException in PlayerV2Fragment", metadata: {os, os_version, device_model, app_version}}`.
The reports are sent with the StreamPulse server key, the way the StreamPulse server would forward its
devices' crash reports: only reports sent with a server-side key (scope `sdk:ruleset`) count toward a
flag's error rate and automatic rollback. A report sent with the app's key is stored, and the flag's
safety check shows it as not counted.
Every incident session is an Android 12 device's, and a crashed app gets relaunched by its user (crash loop,
`INCIDENT_RELAUNCHES = 4`): every relaunch re-evaluates the flag — still ON — and crashes again. Safety
monitoring computes `error_rate = crash reports ÷ flag evaluations` over the last 15 minutes, and every
evaluation of the flag counts, the steady traffic's too. With the flag at 25%, the incident's reports come to
well over 5% (the critical threshold) of its own and an equally busy steady traffic's evaluations, so the rate
climbs past 5% while the incident runs; step 3 of the story runs it until the check is critical, at most
5 rounds of 90 seconds. With automatic rollbacks on, it goes on until the safety monitor has rolled the flag
back, within the same 5 rounds: once the incident stops, the steady traffic dilutes the rate, which can fall
back under 5% before the monitor's next pass has seen it.

## Rollout story

The story calls the dashboard API with a bearer token. Without `--token` it gets one itself:
it signs in with `POST /api/v1/auth/login` as `--email`/`--password`, which default to the
seeded demo admin (`admin@demo.com` / `Demo1234!`). That needs the backend on its default local
sign-in (`AUTH_PROVIDER=local`), which is how `setup-local.sh` starts it. A failed sign-in stops
the story with exit code 2 and says why.

To run one step, here the first:

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
python demo/streampulse/simulator/rollout_story.py --step 1
```

To run all seven, pausing 20 seconds between steps (`--pace`, default 15):

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
python demo/streampulse/simulator/rollout_story.py --auto --pace 20
```

To use a token you already have instead, pass `--token "$TOKEN"`; to sign in as another user
who may change flags, pass `--email` and `--password`.

Other options: `--api-url`, `--api-key` (for the step-3 traffic), `--dashboard-url` (default `http://localhost:3100`),
`--app-url` (default `http://localhost:3300`), `--incident-rate/--incident-duration`, `--rollback-wait`, `--no-wait`.

| Step | What happens | Endpoints |
|---|---|---|
| 1 | Prints the flag (5%, `employee equals true` → 100%), the 4-stage schedule, safety config and check | `GET /feature-flags/`, `/rollout-schedules/?feature_flag_id=`, `/safety/...` |
| 2 | Completes stage 1 → stage 2 in progress → flag 25% | `PUT /rollout-schedules/stages/{id}` (trigger → manual), `POST .../stages/{id}/advance` |
| 3 | Runs `traffic.py --incident android12 --rate 6 --duration 90` as a subprocess, then the safety check; while the check is still healthy, and with automatic rollbacks ON until the flag is rolled back, runs it again, at most 5 rounds in all | `GET /safety/feature-flags/{id}/check`, `GET /scheduler/safety/history` |
| 4 | Automatic rollbacks ON → polls the flag until the safety monitor sets it to 5%, printing the error rate and each of the monitor's passes; rolls back by hand only after two passes that started after the step began ran without a rollback, or when no pass runs for 6 min. OFF → manual rollback. Either way the rollback pauses the flag's rollout schedule | `GET /safety/settings`, `GET /scheduler/safety/history`, `POST /safety/feature-flags/{id}/rollback?percentage=5&reason=` |
| 5 | Adds `app_version semver_gte 3.2.1` (→ 100%) next to the employee group, dashboard shape, OR | `PUT /feature-flags/{id}` `{targeting_rules}` |
| 6 | Waits until the check is healthy again (the 15-minute error window), resumes the schedule the rollback paused, then stage 3 → 50% | `POST /rollout-schedules/{id}/activate`, then as step 2 |
| 7 | Stage 4 → 100%, completes the schedule, removes the rules | as step 2 + `PUT /feature-flags/{id}` |

Every step but step 3 is idempotent: re-running it prints the current state and writes nothing.
Re-running step 3 runs the incident again. Notes:

- The seeded stages are `time_based`; the advance endpoint only accepts `manual` stages, so the story switches a
  stage's trigger to manual right before advancing it (and says so).
- Step 4 relies on the backend's safety scheduler: run it with `SAFETY_CHECK_INTERVAL_MINUTES=1` for the demo
  (default 5). The seed enables automatic rollbacks in the global safety settings; the flag's safety config rolls
  back to 5% (`rollback_percentage`). A rollback to 5% lowers only the global rollout percentage, so devices
  matched by the `employee equals true` rule keep Player v2 through it, and it pauses the rollout schedule,
  which step 6 resumes (a rollback to 0% would turn the flag off for everyone; see
  [What a rollback changes](../../../docs/feature-flags/safety.md#what-a-rollback-changes)).
- When step 4 rolls back by hand it says so, as the user the story signed in as: the audit log names that
  user, not `system:safety-monitor`.
- After the incident the flag stays critical until its error rate over the last 15 minutes is back at 5% or
  below; step 6 waits for that (pass `--no-wait` to skip, at the cost of the scheduler rolling the flag back again).
- Reset the whole story with `python backend/scripts/seed_streampulse.py --reset` followed by a reseed.

## Tests

```{.bash skip reason="dev: runs the simulator's offline tests in a development checkout"}
source venv/bin/activate
python -m pytest demo/streampulse/simulator -q -o addopts="" -p no:cacheprovider
```
