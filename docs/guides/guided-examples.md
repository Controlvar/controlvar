# Guided examples

The steps below are the guided-examples journey, run nightly by CI on the stack
`./demo/setup-local.sh` starts, after the other demo journeys
([the workflow](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/docs-journeys.yml)).
Each step says what to do and what you will see.

## Before you start

Run `./demo/setup-local.sh` from the repository root, as the
[demo guide on GitHub](https://github.com/Controlvar/controlvar/blob/main/demo/DEMO_GUIDE.md)
describes. It starts the API on http://localhost:8000, the dashboard on http://localhost:3100,
the ShopLab storefront on http://localhost:3200 and StreamPulse on http://localhost:3300, with
their seeded experiments and flags and their traffic simulators.

The demo administrator is admin@demo.com, with the password Demo1234!. Where a step sends a
request to the API, send it with that account's token:
[Quick Start step 2](../getting-started/quick-start.md#step-2-log-in) shows how to get one.

## Run an A/B test on ShopLab and read the result {#ab-test}

ShopLab's home page runs the A/B test `shoplab_hero_banner`: the image hero (its `control` arm)
against the video hero (`video_hero`). Its primary metric, Hero CTA click-through, counts the
visitors who press the hero's button.

### 1. Stop the ShopLab simulator {#ab-stop-the-simulator}

From the repository root, stop the simulator `./demo/setup-local.sh` started, so that only your
own visits move the counts:

```{.bash skip reason="demo: stops the ShopLab simulator that demo/setup-local.sh started"}
pid="$(cat demo/.pids/shoplab-simulator.pid)"
if ps -o args= -p "$pid" | grep -qF demo/shoplab/simulator/traffic.py; then kill "$pid"; fi
while ps -o args= -p "$pid" | grep -qF demo/shoplab/simulator/traffic.py; do sleep 1; done
printf 'ShopLab simulator stopped\n'
```

You will see `ShopLab simulator stopped`, printed once the simulator has gone, or at once if it
had already stopped.

### 2. Open the storefront {#ab-open-the-storefront}

Open http://localhost:3200.

You will see the hero the home page assigned you: "Gear up for the season" with the button
Shop the collection, or "See it in motion" with the button Watch & shop.

### 3. Open the panel {#ab-open-the-panel}

Press Powered by Controlvar, at the bottom right of the page.

You will see your visitor id, and beside Hero banner your arm, `control` or `video_hero`.

### 4. Find the test {#ab-find-the-test}

Ask the API for it: `GET /api/v1/experiments/?search=Hero%20banner`.

You will see one experiment, with the key `shoplab_hero_banner`, the status `active` and its
`id`.

### 5. Read the counts {#ab-read-the-counts}

Read its results with `GET /api/v1/results/{id}?use_cache=false`, which computes them now
rather than taking them from the cache.

You will see, for each metric, the `sample_size` (users assigned) and the `conversions` (users
who sent the metric's event) of `control` and of `video_hero`.

### 6. Press the hero's button {#ab-press-the-button}

Back in the storefront, press the hero's button, Shop the collection or Watch & shop.

You will see the product list.

### 7. Read the counts again {#ab-read-the-counts-again}

Send the same request again.

You will see Hero CTA click-through's `conversions` one higher in your arm and unchanged in the
other, and the same `sample_size` in both: your press counts once, in your arm.

### 8. Sign in to the dashboard {#ab-sign-in}

Open http://localhost:3100/login, enter admin@demo.com and Demo1234!, and press Sign in.

You will see the Experiments list.

### 9. Open the results page {#ab-open-the-results}

Open the test's results page, http://localhost:3100/results/ followed by its `id`.

You will see its All Metrics table, where each arm's Sample Size for Hero CTA click-through is
the `sample_size` the API gave.

## Pause the test, and turn a feature off and on with a flag {#pause}

Pausing a test stops it counting: until you start it again, the storefront shows every visitor
the control, and no one's purchase counts in either arm.

### 1. Pause the test {#pause-the-test}

Open the test's page in the dashboard, http://localhost:3100/experiments/ followed by its `id`,
and press Pause.

You will see its status change from Active to Paused, with Start in place of Pause.

### 2. Reload the storefront {#pause-reload-the-storefront}

Open http://localhost:3200 again and press Powered by Controlvar.

You will see the control's hero, "Gear up for the season", and beside Hero banner
`not enrolled: paused (control shown)`.

### 3. Read both tests' counts {#pause-read-the-counts}

Find the product page's test, `shoplab_pdp_buy_button`, with
`GET /api/v1/experiments/?search=PDP%20buy%20button`, and read its results and the hero banner's
as in step 5 above.

You will see the hero banner `paused`, and in both tests the Purchase metric's `conversions` for
each arm.

### 4. Place an order {#pause-place-an-order}

Close the panel if it is open (press Powered by Controlvar), then open Field Watch from the home
page, press Add to cart and then Go to checkout, fill in the checkout with any name, address and
card digits, pressing Continue between its steps, and press Place order.

You will see "Thanks for your order!".

### 5. Read both tests' counts again {#pause-read-the-counts-again}

Send the same two results requests again.

You will see Purchase's `conversions` one higher in your arm of the buy-button test, which is
running, and unchanged in both arms of the paused hero banner, whose `sample_size` is unchanged
too.

### 6. Start the test again {#pause-start-again}

On the test's page in the dashboard, press Start.

You will see its status back at Active, with Pause offered again.

### 7. Order again {#pause-order-again}

Open http://localhost:3200 again and buy Field Watch as in step 4.

You will see your own arm's hero again, and then "Thanks for your order!".

### 8. Read the hero banner's counts {#pause-read-the-result}

Read the hero banner's results again.

You will see Purchase's `conversions` one higher in your arm and unchanged in the other, with
the same `sample_size` in both: the test counts again.

### 9. Turn the free-shipping banner off {#pause-flag-off}

Open http://localhost:3100/feature-flags and press the switch of
`shoplab_free_shipping_banner`.

You will see the flag Off.

### 10. Reload the storefront {#pause-flag-off-storefront}

Open http://localhost:3200 again and press Powered by Controlvar.

You will see Free-shipping banner `off` in the panel.

### 11. Turn the banner back on {#pause-flag-on}

Press the same switch in the dashboard's Feature Flags.

You will see the flag On.

### 12. Reload the storefront again {#pause-flag-on-storefront}

Open http://localhost:3200 again.

You will see "Free shipping on every order this week — no minimum." again.

## Target a flag by device (web SDK) {#device}

StreamPulse draws a phone in a web page with the React SDK, a web SDK that sends the device's
attributes with every flag evaluation. The flag `streampulse_ai_search` is on for
`os = iOS`, `os_version ≥ 17.0.0`, `region = US` and `tier = premium`, and its own rollout is
0%. The native iOS, Android, React Native and Flutter SDKs do not send attributes on flag
evaluation yet ([#438](https://github.com/Controlvar/controlvar/issues/438)), so a rule like
this one would not match from them.

### 1. Open StreamPulse {#device-open}

Open http://localhost:3300.

You will see the phone, a Device panel with its presets, and the Powered by Controlvar panel
with each flag's answer.

### 2. Pick a premium iPhone {#device-premium-iphone}

Pick the preset iPhone 15 · iOS 17.4 · US · premium.

You will see `on: matched a targeting rule` beside AI search · targeting rules.

### 3. Open Search {#device-search}

Open the phone's Search tab.

You will see AI search ✨.

### 4. Pick a free iPhone {#device-free-iphone}

Pick the preset iPhone 14 · iOS 16.7 · GB · free.

You will see `off: outside the rollout %` beside AI search, and Search showing the keyword
search.

### 5. Set the lowest OS version the rule takes {#device-boundary}

Pick iPhone 15 · iOS 17.4 · US · premium again, change OS version to 17.0.0 and press Apply
device.

You will see `on: matched a targeting rule`, because 17.0.0 meets `os_version ≥ 17.0.0`.

### 6. Go one version below {#device-below}

Change OS version to 16.9.9 and press Apply device.

You will see `off: outside the rollout %`.

## Roll out a flag in stages, with an automatic rollback {#rollout}

StreamPulse's rollout story takes the flag `streampulse_player_v2` from employees' devices to
every device, through a crash spike on Android 12 and the automatic rollback it sets off. Its
steps are in
[the StreamPulse simulator guide, on GitHub](https://github.com/Controlvar/controlvar/blob/main/demo/streampulse/simulator/README.md#rollout-story),
and the same nightly CI run walks them.

## What these examples do not cover {#not-covered}

- **Which arm wins.** The arms' numbers come from the seeds' history and the simulators, so
  these examples check where your own actions count, not which arm is ahead.
- **A visitor in the global holdout.** The demo keeps a few visitors out of every test and shows
  them the control: the panel then says `in the global holdout (control shown)`, and nothing they
  do counts in an arm. The panel's New visitor button gives you a new id.
- **Completing a test.** It cannot be undone, so on the demo stack these examples pause the test
  instead.
- **What an order is worth.** These examples count the users who pressed a button or ordered;
  [Limitations](../getting-started/limitations.md) says how a revenue metric is analysed.
- **One visitor under two ids.** There is no identify or alias call, as
  [Limitations](../getting-started/limitations.md) says.
- **How soon an open page sees a flag change.** StreamPulse asks for its flags when you pick a
  device or press Refresh now, and does not poll.
- **CUPED.** The simulators' visitors are new, so CUPED has no history before their assignment
  to work with here.
- **Other SDKs and deployments.** The native mobile SDKs, the server-side SDKs and local
  evaluation, the Go SDK, rotating an API key under a running app, SSO and a cloud deployment
  each have their own guides.
