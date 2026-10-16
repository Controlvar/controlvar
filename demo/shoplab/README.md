# ShopLab — demo storefront for Controlvar

ShopLab is a small e-commerce storefront (Next.js 16, pages router, Tailwind) whose every
page runs a real experiment or feature flag through the Controlvar platform via the React
SDK. It exists to *show* the platform: open the storefront, click around, watch the
"Powered by Controlvar" panel, then open the dashboard and see the same visitor, the
same variants and the same events turn into statistics.

Nothing is for sale. There are no images, no external fonts and no CDNs — just twelve
made-up products with gradient artwork.

## What each page demonstrates

| Page | Experiment / flag | Platform feature shown |
|---|---|---|
| **Homepage** `/` | `shoplab_hero_banner` — image hero vs. CSS-animated "video" hero | Classic A/B test, sequential testing (mSPRT), Bayesian results, variant configuration driving copy |
| **Product list** `/products` | `shoplab_plp_sort` — relevance vs. price vs. "ML personalised" ordering | **Multi-armed bandit** (Thompson sampling): traffic shifts toward the winning sort |
| **Product detail** `/products/[id]` | `shoplab_pdp_buy_button` — button colour × CTA text (4 cells) | **Multivariate** test, sequential testing, `value` on `add_to_cart` |
| **Checkout** `/checkout` | `shoplab_checkout_flow` — 3-step stepper vs. one-page form | **CUPED** (API only; the simulator's visitors are new, so they have no history), revenue metric from `purchase` value |
| **Search** `/search` | `shoplab_new_search` flag — exact name match vs. fuzzy engine with highlights | **Feature-flag gradual rollout** (10% → 50% → 100%), safety config, flag-attributed events |
| Header (every page) | `shoplab_free_shipping_banner` flag | **Kill switch**: turn the banner off in the dashboard, reload the storefront |

The bottom-right **Powered by Controlvar** panel shows the visitor id, the variant for each
of the four experiments, both flags, the last 8 tracked events, a "New visitor" button
(new id + reload → fresh assignments) and a link to the dashboard.

Event vocabulary (all `event_type == event_name`):
`page_view {page}` · `hero_cta_click` · `product_click {product_id, position, sort}` ·
`add_to_cart` value = unit price `{product_id, quantity, button}` · `begin_checkout` value = cart total
`{items, flow}` · `purchase` value = order total `{items, flow, order_id}` · `search {query, results, engine}`
(attached to the `shoplab_new_search` flag). Events without an explicit key are fanned out by the
SDK to every experiment the visitor has been assigned to in the session.

## Running it

Prerequisites: the Controlvar backend on `http://localhost:8000`, the dashboard on
`http://localhost:3100`, Node 20.9 or later (Next.js 16 refuses older versions), and the
repo's Python venv.

`demo/setup-local.sh` does all of this for you (set `SHOPLAB=0` to skip ShopLab). By hand:

**1. Seed** the ShopLab experiments, flags, history and API key, from the repository root.
`backend/scripts/seed_shoplab.py` is idempotent. It writes the key to `demo/shoplab/.api_key`
and the `NEXT_PUBLIC_CONTROLVAR_API_KEY` line into `demo/shoplab/.env.local`. `--no-history`
skips the 14-day backfill, and `--reset` removes everything it created.
A run on a database seeded before 0.34.3 leaves the experiments it finds as they are, so
their results pages plan the sample from the old `expected_effect` values until you re-seed
with `--reset`.

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
source venv/bin/activate
python backend/scripts/seed_shoplab.py
```

**2. Environment.** `cp -n` copies `.env.example` only if `.env.local` does not exist yet.
After step 1 it usually does, holding only the key line, so this changes nothing and the two
URLs take their defaults:

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
cd demo/shoplab
cp -n .env.example .env.local
```

| Setting | Value |
|---|---|
| `NEXT_PUBLIC_CONTROLVAR_API_URL` | defaults to `http://localhost:8000` |
| `NEXT_PUBLIC_CONTROLVAR_API_KEY` | the plaintext key for `shoplab-storefront`, written by the seed |
| `NEXT_PUBLIC_CONTROLVAR_DASHBOARD_URL` | defaults to `http://localhost:3100` |

Each is still read under its name before the project was renamed
(`NEXT_PUBLIC_EXPERIMENTLY_API_URL` and so on) when the new name is not set.

**3. Run** it, still in `demo/shoplab`. The storefront is at http://localhost:3200.

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
npm install
npm run dev
```

Other scripts: `npm test` (jest + Testing Library, SDK mocked), `npm run lint`,
`npm run typecheck` (type-checks against the SDK *source*), `npm run build`, `npm start`.

The React SDK is consumed straight from `../../sdk/react/src` (tsconfig `paths` + webpack alias +
`experimental.externalDir`), so SDK changes show up without a build step. `react` and `react-dom`
are aliased to this app's copies to avoid the duplicate-React "Invalid hook call" trap.

## Traffic simulator

`simulator/traffic.py` (Python 3.11, stdlib only) drives synthetic visitors through the same
funnel using only the public API and the storefront key — so the dashboard has fresh data
while you demo.

From the repository root, with the venv active, this sends 3 visitors a second for ten
minutes (`--duration 0`, the default, runs until you stop it):

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
source venv/bin/activate
python demo/shoplab/simulator/traffic.py --rate 3 --duration 600
```

To print one visitor's planned events without touching the network:

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
python demo/shoplab/simulator/traffic.py --dry-run --seed 1
```

Its other options are `--api-url`, `--api-key` (default: the contents of
`demo/shoplab/.api_key`) and `--max-visitors`; `--help` describes them all.

Each visitor is assigned to the four experiments (`POST /tracking/assign`, context = persona
attributes), has both flags evaluated, then walks the funnel with the spec's true conversion
rates (hero CTA 12% vs 15%; sort click 22/19/28%; add-to-cart 8.0/8.5/10.5/9.5%; purchase after
checkout 62% vs 68%, lognormal order value with median $85; search click 35% with the new engine
vs 25% without). Three personas scale intent — casual 60%, shopper 30%, power 10% — calibrated so
the population rates equal the spec. Events go out via `POST /tracking/batch` (≤100 per call) with
an explicit `experiment_key` for every assigned experiment and `feature_flag_key` for search.
Stats print every 10 s; the process exits non-zero on 401 (bad key) or 404 (not seeded).

Tests (no network):

```{.bash skip reason="dev: runs the simulator's offline tests in a development checkout"}
source venv/bin/activate && python -m pytest demo/shoplab/simulator/test_traffic.py -q -o addopts="" -p no:cacheprovider
```

## Demo script — what to show in the dashboard

1. **Hero banner** (`shoplab_hero_banner`): open Results. Point at the **Sequential** tab (mSPRT
   boundary, "can stop early?"). The probability that video beats control and the expected loss are
   in the Overview tab's Bayesian panel where your version has one, and always in the API
   (`GET /api/v1/results/{id}/bayesian`). Reload the
   storefront with "New visitor" until you land in `video_hero`.
2. **Product sort** (`shoplab_plp_sort`): open the experiment's page. Its **Current traffic
   weights** panel shows each variant's share of new visitors, drifting toward `ml_personalized`
   (true click rate 28% vs 22% / 19%). Press **Refresh** to see the latest weights; the panel does
   not update by itself, and the dashboard has no chart of past weights. Then go to `/products`
   and note "Sorted by Recommended for you".
3. **Buy button** (`shoplab_pdp_buy_button`): four cells. `orange_buy_now` wins on add-to-cart;
   show the multivariate breakdown and the sequential-testing status. On a product page, switch
   visitors to see colour and text change.
4. **Checkout flow** (`shoplab_checkout_flow`): open Results. Its primary metric is the `purchase`
   conversion, and its secondary the revenue metric *Order value*. There is no CUPED toggle in the dashboard.
   CUPED adjusts for each user's own purchases before assignment, and the simulator's visitors are
   new, so its `covariate_coverage_pct` is near 0 and it does not narrow the interval here; the
   Quick Start's `checkout_button_color` has history and shows a reduction
   ([CUPED](../../docs/api/cuped.md)). Walk through the 3-step vs. one-page form in the storefront.
5. **New search** (`shoplab_new_search`): open the flag → Rollout schedule (10% in progress → 50%
   → 100%) and the safety config (error-rate thresholds). Search "rain jaket" as a visitor with the
   flag on (fuzzy, highlighted, "New search ✨") vs. off (0 results).
6. **Free-shipping banner** (`shoplab_free_shipping_banner`): toggle the flag off in the
   dashboard, reload the storefront — banner gone. Toggle back on.
7. Keep the simulator running in a terminal during all of the above so the numbers move.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Panel shows `error` for every experiment; network tab shows **401** | Wrong or missing `NEXT_PUBLIC_CONTROLVAR_API_KEY`. Re-run the seed script (it rewrites `.env.local` and `.api_key`), then restart `npm run dev` (env vars are read at startup). |
| **404** on `/tracking/assign` or `/feature-flags/evaluate/...` | The ShopLab experiments are not seeded or not ACTIVE, or the flags are not seeded (a flag that is not ACTIVE answers `enabled: false`, not a 404). Run the seed script. |
| Browser console: **CORS** error from `localhost:3200` | Port 3200 is not in the backend's CORS allow-list. The development defaults (`DEFAULT_CORS_ORIGINS` in `backend/app/core/config.py`) include it; if you set `CORS_ORIGINS` or `BACKEND_CORS_ORIGINS` yourself, add `http://localhost:3200` and restart the API. Against a staging or production API, add it to that API's `CORS_ORIGINS`: those allow no other origin by default. |
| Every visitor is `control` and nothing loads | Backend not running on `NEXT_PUBLIC_CONTROLVAR_API_URL`. Start it from the repository root: `ENVIRONMENT=development uvicorn backend.app.main:app --reload`. |
| "Invalid hook call" in the browser | Two React copies. `next.config.js` aliases `react`/`react-dom` to this app's `node_modules`; make sure you ran `npm install` inside `demo/shoplab`. |
| Running with `docker compose --profile demo`: **401** everywhere, and the simulator log says `no /data/demo/shoplab/.api_key` | The shoplab seed did not run. Run `SEED=demo,shoplab,streampulse docker compose --profile demo up -d --wait`: it applies the missing seeds to the existing database (no `down -v`). |
| Simulator exits with code 2 / 3 / 4 | 2 = API key rejected or missing, 3 = experiments not seeded (404), 4 = backend unreachable. |
| `npm run typecheck` fails inside `sdk/react/src` | The app is written against React SDK v1.1.0's surface (`useExperiment`, `useTrackEvent`, `ExperimentAssignment.configuration`, ...). Make sure `sdk/react` is on that version. |
