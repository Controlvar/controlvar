# The read-only demo box

The recipe for the public demo at `https://demo.controlvar.com`, with ShopLab at
`https://shoplab.demo.controlvar.com` and StreamPulse at
`https://streampulse.demo.controlvar.com`. One small Linux box runs a released
Controlvar (core profile) with demo mode on: visitors sign in as a viewer, every
write except sign-in and the SDK's tracking calls is refused, and everything is
rebuilt from nothing every night at 08:00 UTC.

This is an operator's runbook. It holds no secrets: those are generated on the
box and never leave it.

## Layout

| Where | What |
| --- | --- |
| `/opt/controlvar-demo/recipe` | this repository at the recipe's commit: `deploy/demo/` |
| `/opt/controlvar-demo/release` | this repository at the release tag the box runs: `deploy/compose/compose.yml` names its images; ShopLab, StreamPulse and the simulators are built and run from it |
| `/etc/controlvar-demo/box.env` | where the two checkouts and the state are, for the units |
| `/etc/controlvar-demo/demo.env` | the stack's settings and secrets (mode 600), from `generate-env.sh` |
| `/etc/controlvar-demo/caddy.env` | `DEMO_ALLOW_FROM`: who may reach the demo |
| `/var/lib/controlvar-demo/` | the lock the scripts share, and `last-reset` |
| `/etc/caddy/Caddyfile` | one line: `import /opt/controlvar-demo/recipe/deploy/demo/Caddyfile` |

Caddy runs on the host, takes ports 80 and 443, and gets certificates from Let's
Encrypt. Docker runs everything else; only Caddy is reachable from outside (the
dashboard and the storefronts are published on loopback ports).

The host names are fixed: the dashboard links to ShopLab and StreamPulse as
`shoplab.<its own host>` and `streampulse.<its own host>`, so it must be served
at exactly `demo.controlvar.com`. The storefronts' "Open dashboard" link is
`https://demo.controlvar.com/experiments`: they are built with the dashboard's
origin and add the page themselves.

## What each file does

| File | What it does |
| --- | --- |
| `compose.demo.yml` | the overlay on `deploy/compose/compose.yml` (which it does not change): demo mode and the demo account, the host and origin lists, the SDK rate limit, the one-off `seed` service, the storefronts and their simulators, log rotation on every container |
| `Caddyfile` | the three sites; the `DEMO_ALLOW_FROM` allow-list; `/` on the demo host redirected to `/login`; `/api/v1/feature-flags/user/*` answered 404; a 16 KB cap on tracking request bodies; the rebuild page (HTML for pages, JSON with `detail` and `Retry-After` for `/api/`) while web is down |
| `generate-env.sh` | writes `demo.env` once: the production secrets (`deploy/compose/generate-env.sh`), then the demo account, the host and origin lists, the SDK limit and two random storefront keys (demo mode itself is set in the overlay) |
| `provision.sh` | sets up a fresh Ubuntu 24.04 box, then runs `redeploy.sh` |
| `redeploy.sh vX.Y.Z` | moves the release checkout to the tag, pulls its images, builds the storefronts, runs `reset.sh` |
| `reset.sh` | the rebuild: stop the public services, `down -v`, start postgres and redis, run the seeds, apply `lockdown.sql`, start the rest, run the probe. It opens the demo only when the probe passes |
| `lockdown.sql` | leaves one password that signs in, the demo account's: every other user's hash becomes `!`. Every user stays active, because the storefronts' keys belong to `admin@demo.com`. Refuses (and rolls back) unless exactly one active, non-superuser VIEWER can sign in |
| `check.sh` | the hourly check: the demo account signs in as a viewer, writes are refused with and without credentials, the published passwords and the first administrator's are refused. A wrong answer closes the demo; no answer twice, 65 s apart, runs `reset.sh` |
| `diskguard.sh` | every 10 minutes: at 80% disk, prune unused images and build cache, then reset |
| `probe.py` | the checks themselves (`probe.py --help`): 0 when every check passed, 1 when some got no answer (no response, a timeout, 5xx, 429), 3 when the demo gave a wrong answer (a write accepted, a locked password signing in, demo mode off, the API outside production, the demo account not a plain viewer) |
| `lib.sh` | what the scripts share: the compose command (`dc`), the lock, rebuild, probe and close |
| `systemd/` | the reset timer (08:00 UTC, the time the dashboard's demo banner and the rebuild page show), the hourly check (half past each hour) and the disk guard, each with its service |

**Closed** means web, ShopLab and StreamPulse are stopped: Caddy then answers
every host with the rebuild page. The demo is never left open unprobed:

| What the probe saw | What happens |
| --- | --- |
| everything as promised | open |
| no answer (busy, restarting, down), twice 65 s apart, in the hourly check | `reset.sh` at once: rebuilt, and open again as soon as its probe passes |
| no answer twice, or a failed step, in a reset | closed; the next hourly check finds no answer and resets again, unless `closed-wrong` is present: then the hourly check neither probes nor resets, and the demo stays closed until the nightly reset or an operator's (by design) |
| a wrong answer, in either | closed at once, and `/var/lib/controlvar-demo/closed-wrong` written: the hourly check leaves it closed; the next reset that passes its probe (the nightly one, or an operator's) opens it |

Nothing reports what failed anywhere public; the journal does.

## Moments that need a person

Each of these is decided by a person at the moment it happens.

1. **Create the box**: Lightsail, us-west-2, Ubuntu 24.04, 4 GB / 2 vCPU / 80 GB,
   a static IP, firewall 22 (key only), 80 and 443. In the same sitting, a
   monthly cost budget of $30 filtered to Lightsail, excluding credits, first at
   a threshold low enough to fire once. The box holds no cloud credentials.
2. **Add the DNS records**: three A records, TTL 300, to the static IP. No AAAA
   record (see "Disk"). The first certificate puts the three names in public
   certificate-transparency logs.
3. **Provision** (after 1 and 2), on the box, with your own address in place
   of the documentation address `203.0.113.7` and the release to run; a recipe
   commit other than `main` goes after the tag:

   ```{.bash skip reason="aws: runs on the demo box, a Lightsail instance"}
   sudo DEMO_ALLOW_FROM=203.0.113.7/32 bash provision.sh v0.46.0
   ```

   The demo answers only `DEMO_ALLOW_FROM` (and the box itself) until it is
   opened; everyone else gets 403.
4. **Open the demo**: set `DEMO_ALLOW_FROM=0.0.0.0/0` in
   `/etc/controlvar-demo/caddy.env`, then `sudo systemctl restart caddy`.
5. **Turn on the public probe**: set the repository variable `DEMO_URL` to
   `https://demo.controlvar.com`. `.github/workflows/demo-probe.yml` then says UP
   or DOWN every hour, and nothing else.

## Reaching it

`ssh ubuntu@<static IP>` with the box's key. On the box: start a reset now, read
the three units' journal and Caddy's, see when the last reset passed, and list
the timers:

```{.bash skip reason="aws: runs on the demo box, a Lightsail instance"}
sudo systemctl start controlvar-demo-reset
journalctl -u controlvar-demo-reset -u controlvar-demo-check -u controlvar-demo-diskguard
journalctl -u caddy
cat /var/lib/controlvar-demo/last-reset
systemctl list-timers 'controlvar-demo-*'
```

The compose command, for `ps`, `logs` and the rest:

```{.bash skip reason="aws: runs on the demo box, a Lightsail instance"}
sudo bash -c 'set -a; . /etc/controlvar-demo/box.env; set +a; . /opt/controlvar-demo/recipe/deploy/demo/lib.sh; dc ps'
```

Move to a release: `sudo bash -c 'set -a; . /etc/controlvar-demo/box.env; set +a;
/opt/controlvar-demo/recipe/deploy/demo/redeploy.sh vX.Y.Z'`. Move the recipe:
`git -C /opt/controlvar-demo/recipe fetch && git -C /opt/controlvar-demo/recipe
checkout --detach <commit>`, then a reset.

## Rehearsal

On a release without demo mode the first reset closes the demo with a wrong
answer: the probe's `config`, `refusal` and `anonymous` checks fail, which is
the proof that the probe fires. To look around such a release behind the
allow-list, start the stack by hand (`dc up -d`); the hourly check closes it
again within the hour, and the marker keeps it closed.

## Disk: what one address can write between resets

Writes reach the database through the SDK routes, which take anyone's request
with a storefront key, and those keys are public. The API counts SDK requests
per client address **and per path** (`rate_limit_key` in
`backend/app/middleware/rate_limiter.py`), so each of the five tracking POSTs
(`assign`, `track`, `batch`, `errors`, `errors/batch`) and each flag's evaluate
path has its own allowance of `SDK_RATE_LIMIT_PER_MINUTE` = 120. Caddy answers
413 to a `/api/v1/tracking/*` body over 16 KB (16,000 bytes), so a batch, at
most 100 entries, stores at most 100 rows and 16 KB of payload, and 404 to
`/api/v1/feature-flags/user/*`, which nothing in the demo calls. An assignment's
targeting context is at most 8,192 bytes (`MAX_CONTEXT_BYTES`), stored on the
assignment and on its exposure event.

Measured on a freshly seeded database: an event row takes 634 bytes on disk
with its indexes, an assignment 382, a raw metric 825 (error rows assumed the
same as events); the seeded database is 156 MB. The most one address can store:

| Path | Per minute |
| --- | --- |
| `batch`, `errors/batch` | 2 x 120 x (100 rows x 634 B + 16,000 B payload) = 19.1 MB |
| `track`, `errors` | 2 x 120 x (16,000 B payload + 634 B) = 4.0 MB |
| `assign` | 120 x (2 x 8,192 B context + 382 B + 634 B) = 2.1 MB |
| evaluate, 8 seeded flags | 8 x 120 x 3 rows x 825 B = 2.4 MB |
| **total** | **27.5 MB a minute, 40 GB a day** |

The disk is 80 GB, `diskguard.sh` resets at 80% (64 GB), and the system, images
and build cache take roughly 10 GB of that (an estimate): about 54 GB, or 33
hours from one address, against a reset every 24.

These budgets are per IPv4 address. An IPv6 visitor holds billions of addresses,
so the names have no AAAA record and the box is reached over IPv4 only.

The simulators stay under the limit: measured, ShopLab sends about 6.2 SDK
requests per visitor and StreamPulse 10 per session; at their rates here (0.3 and
0.2 a second) the busiest path, `assign`, gets about 60 a minute from each,
half the limit.

## Visitors' addresses

Caddy sets `X-Forwarded-For` to the visitor's address, the dashboard container
appends its own hop, and the API believes both (it trusts its own network), so
it rate-limits each visitor separately. Check it on the box: the API's access
log (`dc logs api`) shows visitors' public addresses, not `172.x` ones.

## Not on the box

No cloud credentials; no LLM, SMTP or warehouse keys (`generate-env.sh` writes
none, and keep it that way). The core profile. The simulators run `traffic.py`
only.

## Tested where

`.github/workflows/demo-box.yml` boots this recipe on every change to the code it
runs, nightly, and by hand at a release tag: the overlay with that change's
images, Caddy with this Caddyfile, the seed, `lockdown.sql`, the probe through
Caddy, two resets with a sentinel between, peak memory and log rotation. TLS
from Let's Encrypt, DNS, the timers, the 4 GB of memory and the storefront builds
on the box itself are first seen on the box.
