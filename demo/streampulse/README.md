# StreamPulse — mobile feature-flag demo for Controlvar

StreamPulse is a web page that renders a phone. Pick a simulated device (OS, OS version, app
version, region, tier, employee) and the phone shows, screen by screen, which feature flags and
experiments *that device* gets from the Controlvar platform — with the server's reason for each
decision. A Python simulator pretends to be thousands of devices, and a scripted "rollout story"
walks a feature flag from internal testers to 100 % with a crash incident and an automatic rollback
on the way.

It exercises the feature-flag path of the platform: targeting rules with device context, gradual
rollout schedules, safety monitoring + auto-rollback, kill switches, mutual exclusion groups, the
global holdout and Bayesian A/B analysis. No audio, no images, no external fonts or CDNs.

## Screens ↔ platform features

| Screen | Flag / experiment | What the phone shows | Platform feature showcased |
|---|---|---|---|
| **Home** | `streampulse_recs_v2` flag | on → "For you" feed (`recs_impression`); off → "Classic" chronological feed | **Kill switch** — turn the flag off in the dashboard, then reload the app or press *Refresh now*, and the feed flips |
| **Player** | `streampulse_player_v2` flag (+ `streampulse_offline_mode`) | on → redesigned player; off → classic; `play {track_id, player}`; Download button when offline mode is on | **Gradual rollout** 5 → 25 → 50 → 100 %, **safety monitoring + auto-rollback**, employee targeting rule; offline mode = SDK caching / kill switch |
| **Search** | `streampulse_ai_search` flag | on → "AI search ✨" with explanations; off → keyword search; `search {query, engine, results}` | **Targeting rules**: `os = iOS AND os_version ≥ 17.0.0 AND region = US AND tier = premium` |
| **Notifications** | `streampulse_push_frequency` (A/B) | daily vs 3×/week push setting; "Open notification" → `notification_open`, "Uninstall app" → `app_uninstall` | **Sequential testing (mSPRT)** with an uninstall-rate **guardrail** |
| **Profile** | `streampulse_wrapped` + `streampulse_profile_badges` (A/B ×2) | "Your 2026 Wrapped" card (`wrapped_view`, `share`) / badge row (`badge_tap`) | **Mutual exclusion group** `streampulse-profile` — a device is in at most one of the two |
| **Onboarding** | `streampulse_onboarding_steps` (A/B) | 5-step vs 3-step stepper → `onboarding_complete {steps}` → `first_play` | **Bayesian analysis** — probability to be best, expected loss, early stopping |
| **Payments** | `streampulse_upsell_modal` (A/B) | classic price list vs value-first modal → `subscribe {plan, modal}` | **Audit trail** — every create/start/change of the experiment is logged |
| Every screen | all of the above | — | **Global holdout** `streampulse-holdout` (2 %): held-out devices see control everywhere with the reason `holdout` |

Every tracked event is sent with an explicit `experimentKey` / `featureFlagKey` (see the table);
`screen_view {screen}` has no key and is fanned out by the SDK to everything cached for the device.

The right-hand side has two panels:

- **Device** — five presets (iPhone 15 · iOS 17.4 · US · premium; Pixel 7 · Android 14 · DE · free;
  Galaxy S10 · Android 12 · US · premium · app 3.1.0; iPhone 14 · iOS 16.7 · GB · free; Internal
  tester · iPhone 15 · employee) plus custom fields. The device id is the SDK user id and every
  attribute is sent as targeting `context`. Applying any change re-mounts the SDK provider, which
  drops the SDK caches and re-fetches everything. Otherwise the SDK keeps each answer for 5 s
  (`cacheTtlMs: 5000`) and asks again the next time a screen needs it.
- **Powered by Controlvar** — per flag: on/off + the server `reason` (`targeting_rule`,
  `rollout`, `inactive`, `error`) spelled out; per experiment: the variant and whether the device
  was actually enrolled (`assigned: false` → "in global holdout streampulse-holdout", "excluded:
  mutual exclusion group streampulse-profile", "excluded: targeting rules"); the last 8 events;
  links to the dashboard; **Refresh now** (clears the SDK cache and re-evaluates).

The **Rollout story** button opens a drawer with the 7-step narrative, the command that drives it,
and a live readout of `streampulse_player_v2` for the current device.

## Running it

Prerequisites: the Controlvar backend on `http://localhost:8000`, the dashboard on
`http://localhost:3100`, Node 20.9 or later (Next.js 16 refuses older versions), and the
repo's Python venv.

`demo/setup-local.sh` does steps 1 to 4 for you, with steady traffic only (set `STREAMPULSE=0`
to skip StreamPulse). It does not run the crash incident or the rollout story.

**1. Seed** the StreamPulse catalogue, from the repository root. It is idempotent, and writes
the API key to `demo/streampulse/.api_key` and the `NEXT_PUBLIC_CONTROLVAR_API_KEY` line into
`demo/streampulse/.env.local`. It also writes a second key, the StreamPulse server key (scope
`sdk:ruleset`), to `demo/streampulse/.api_key_server` and nowhere else: the simulator sends its
crash reports with it, the way the StreamPulse server would forward them, because only reports
sent with a server-side key count toward automatic rollback. Never put that key in `.env.local`:
it can also download every flag's targeting rules. `--no-history` skips the 7-day backfill,
`--reset` removes everything it created, and the `STREAMPULSE_DEMO_DIR` environment variable
changes where the key files go. A run on a database seeded before 0.34.3 leaves the experiments it finds as they are, so
their results pages plan the sample from the old `expected_effect` values until you re-seed
with `--reset`.

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
source venv/bin/activate
python backend/scripts/seed_streampulse.py
```

**2. Environment.** `cp -n` copies `.env.example` only if `.env.local` does not exist yet.
After step 1 it usually does, holding only the key line, so this changes nothing and the two
URLs take their defaults:

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
cd demo/streampulse
cp -n .env.example .env.local
```

| Setting | Value |
|---|---|
| `NEXT_PUBLIC_CONTROLVAR_API_URL` | defaults to `http://localhost:8000` |
| `NEXT_PUBLIC_CONTROLVAR_API_KEY` | the plaintext key for `streampulse-app`, written by the seed |
| `NEXT_PUBLIC_CONTROLVAR_DASHBOARD_URL` | defaults to `http://localhost:3100` |

Each is still read under its name before the project was renamed
(`NEXT_PUBLIC_EXPERIMENTLY_API_URL` and so on) when the new name is not set.

**3. Run the app**, still in `demo/streampulse`. It is at http://localhost:3300.

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
npm install
npm run dev
```

**4. Device simulator.** In another terminal, from the repository root with the venv active,
this keeps the dashboard moving for ten minutes:

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
python demo/streampulse/simulator/traffic.py --rate 3 --duration 600
```

The crash incident is the same simulator with `--incident android12`:

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
python demo/streampulse/simulator/traffic.py --rate 5 --incident android12
```

**5. The rollout story**, in another terminal. It calls the dashboard API, so it signs in first
as the seeded demo admin; [the simulator's README](simulator/README.md#rollout-story) says how
to sign in as someone else or pass a token instead. `--auto` runs all seven steps, paced:

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
python demo/streampulse/simulator/rollout_story.py --auto
```

`--step 3` (any of 1 to 7) runs one step instead:

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
python demo/streampulse/simulator/rollout_story.py --step 3
```

Other scripts: `npm test` (jest + Testing Library, SDK mocked), `npm run lint`,
`npm run typecheck` (type-checks against the SDK *source*), `npm run build`, `npm start`.

The React SDK is consumed straight from `../../sdk/react/src` (tsconfig `paths` + webpack alias +
`experimental.externalDir`), so SDK changes show up without a build step. `react` and `react-dom`
are aliased to this app's copies to avoid the duplicate-React "Invalid hook call" trap. StreamPulse
uses `useFeatureFlag(...).reason` and `useExperiment(...).assigned` / `.reason` (React SDK ≥ 1.1.0
with the `assigned`/`reason` fields).

## Demo script — what to show

Keep `traffic.py` running in a terminal throughout so the numbers move.

1. **Home — kill switch** (`streampulse_recs_v2`). Any preset shows "For you". In the dashboard,
   toggle the flag off, then press *Refresh now* (or reload the page): the feed becomes "Classic" and
   the panel reads `off: flag is inactive`. Toggle it back on.
2. **Search — targeting rules** (`streampulse_ai_search`, rollout 0 %). Search "late night drive"
   as *iPhone 15 · iOS 17.4 · US · premium*: AI results with explanations, panel says `on: matched a
   targeting rule`. Switch to *iPhone 14 · iOS 16.7 · GB · free*: keyword engine, 0 results, `off:
   outside the rollout %` (no rule matched). Edit the custom fields (e.g. OS version 17.0.0, region
   US, tier premium, then *Apply device*) to show the rule boundary live.
3. **Player — rollout + safety** (`streampulse_player_v2`, 5 %, employee rule). *Internal tester*
   gets Player v2 by rule; the other presets are bucketed by the 5 % rollout. Open the **Rollout
   story** drawer and run `rollout_story.py --auto` (or step by step): 25 % → Android 12 crash
   incident (`traffic.py --incident android12` reports the crashes through `POST /tracking/errors`
   with the StreamPulse server key) →
   safety check unhealthy → rollback to 5 % → `app_version ≥ 3.2.1` rule → 50 % → 100 %. Watch
   the drawer's readout for *Galaxy S10 · app 3.1.0* vs *iPhone 15 · app 3.2.1*, and the Safety and
   Audit pages in the dashboard. The Audit page shows the rollback as a *Safety rollback* by
   *Safety monitor (automatic)*, or by you when the story rolls back by hand; the seed writes no
   rollback.
4. **Notifications — sequential test + guardrail** (`streampulse_push_frequency`). Show the
   variant, press *Open notification* a few times and *Uninstall app* once. In the dashboard open
   Results → sequential testing (mSPRT boundary) and the `app_uninstall` guardrail.
5. **Profile — mutual exclusion** (`streampulse_wrapped` vs `streampulse_profile_badges`). Every
   device is in at most one: the panel shows the other as `excluded: mutual exclusion group
   streampulse-profile (control shown)`. In `wrapped_2026` the Wrapped card fires `wrapped_view` on
   mount; *Share* fires `share`. In `badges`, tap a badge.
6. **Onboarding — Bayesian** (`streampulse_onboarding_steps`). Walk the 3- or 5-step flow, finish,
   press *Play your first track*. The probability `three_step` beats `five_step` and the expected
   loss are in the API, `GET /api/v1/results/{id}/bayesian`, and in the results page's Overview tab
   where your version has a Bayesian panel
   ([which screens exist](../../docs/guides/dashboard-and-api.md)).
7. **Payments — audit trail** (`streampulse_upsell_modal`). *Go Premium* → classic vs value modal →
   *Subscribe*. Dashboard → Admin → Audit log: the seed wrote create, change and start entries
   for this experiment, in the form the platform writes them; every later change is logged too.
8. **Global holdout**. Type a custom device id such as `sp-holdout-15` (bucket 1 under the demo
   holdout's salt) and *Apply device*: every experiment reads `in global holdout streampulse-holdout (control
   shown)` and no exposure is recorded. The preset ids are chosen to be outside the holdout.
   The API records each device first seen while the holdout is active; the seed writes no such
   records, so a comparison of held-out devices with the rest needs real traffic first (the
   simulator, or devices you apply) and starts from the moment the holdout was activated.

Which variant a preset lands in depends on the seeded experiment ids (assignment hashing is
per-experiment), so it can differ after `--reset`; the holdout bucket does not.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Yellow banner "NEXT_PUBLIC_CONTROLVAR_API_KEY is not set"; panel shows `error: API error: 401` everywhere | The key is missing or wrong. Run `python backend/scripts/seed_streampulse.py` (it rewrites `.env.local` and `.api_key`), then restart `npm run dev` — Next reads env vars at startup. |
| **401** in the network tab although `.env.local` has a key | The key was revoked or belongs to another environment/database. Re-seed. |
| **404** on `/tracking/assign` or `/feature-flags/evaluate/streampulse_...` | The StreamPulse experiments are not seeded or not ACTIVE, or the flags are not seeded (`--reset` removes both; a flag that is not ACTIVE answers `enabled: false`, not a 404). Run the seed script. |
| Browser console: **CORS** error from `localhost:3300` | Port 3300 is not in the backend's CORS allow-list. The development defaults (`DEFAULT_CORS_ORIGINS` in `backend/app/core/config.py`) include it; if you set `CORS_ORIGINS` or `BACKEND_CORS_ORIGINS` yourself, add `http://localhost:3300` and restart the API. Against a staging or production API, add it to that API's `CORS_ORIGINS`: those allow no other origin by default. |
| Every flag `off`, every experiment `error`, nothing loads | Backend not running on `NEXT_PUBLIC_CONTROLVAR_API_URL`. Start it from the repository root: `ENVIRONMENT=development uvicorn backend.app.main:app --reload`. |
| AI search never turns on | The rule needs `os = iOS`, `os_version ≥ 17.0.0`, `region = US`, `tier = premium` and the flag's own rollout is 0 %. Check the custom fields; the panel reason tells you whether a rule matched. |
| Player v2 does not change after the story rolls back / advances | The app does not poll: it asks for the decisions when you pick a device or press *Refresh now*, so press *Refresh now*. If the schedulers are involved (auto-rollback, time-based stages), they run every `SAFETY_CHECK_INTERVAL_MINUTES` / `ROLLOUT_CHECK_INTERVAL_MINUTES` (default 5 / 15) — set both to `1` in the backend `.env` for the demo (`setup-local.sh` sets both to `1` unless they are already set). |
| A preset shows "in global holdout streampulse-holdout" for every experiment | That id hashes into the 2 % holdout. The shipped preset ids do not; if you changed one, pick another id (or use it to demonstrate the holdout). |
| "Invalid hook call" in the browser | Two React copies. `next.config.js` aliases `react`/`react-dom` to this app's `node_modules`; run `npm install` inside `demo/streampulse`. |
| Running with `docker compose --profile demo`: **401** everywhere, and the simulator log says `no /data/demo/streampulse/.api_key` | The streampulse seed did not run. Run `SEED=demo,shoplab,streampulse docker compose --profile demo up -d --wait`: it applies the missing seeds to the existing database (no `down -v`). |
| Simulator exits with code 2 / 3 / 4 | 2 = API key rejected or missing, or `--incident` without the server key in `demo/streampulse/.api_key_server` (run the seed), 3 = catalogue not seeded (404), 4 = backend unreachable. |
| `npm run typecheck` fails inside `sdk/react/src` | The app needs `ExperimentAssignment.assigned` / `.reason` and `FeatureFlagEvaluation.reason` from the React SDK; make sure `sdk/react` is at the version in this repo. |
