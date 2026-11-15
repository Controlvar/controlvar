# Live Demo

A read-only copy of Controlvar runs at <https://demo.controlvar.com>. There is nothing to
install and no account to create.

## What it is

The dashboard with the demo data, as a viewer sees it. Two demo apps send it simulated traffic:
ShopLab, a storefront, at <https://shoplab.demo.controlvar.com>, and StreamPulse, a phone app
drawn in a web page, at <https://streampulse.demo.controlvar.com>. The numbers grow as the demo
apps send traffic; reload a page to see the latest.

## What you can do

Select **Enter the demo** on the sign-in page. You enter as a viewer: open any experiment and its
results, any feature flag with its targeting rules and rollouts, and any segment. Each demo app's
**Open dashboard** link opens the experiments.

## What is turned off, and why

Creating and changing things is turned off, because everyone who opens the demo shares it. The
API refuses every write except signing in, signing out and the demo apps' tracking calls, and the
dashboard shows a note where a button to create or change something would be. The sign-in page
has no password form, and the admin@demo.com account in the README does not work here.

## When it resets

Everything is rebuilt from scratch every night at 08:00 UTC. While it is rebuilt, every page
says so. Afterwards, enter the demo again.

## The same thing on your machine

The same dashboard, demo data and demo apps, with nothing turned off. From the root of a clone of
the repository:

```{.bash skip reason="demo: builds both demo app images and starts their simulators"}
SEED=demo,shoplab,streampulse docker compose --profile demo up -d --wait
```

Open http://localhost:3000 and sign in as the administrator, **admin@demo.com / Demo1234!**, to
create and change experiments, flags and segments. ShopLab is on http://localhost:3200 and
StreamPulse on http://localhost:3300. The [Quick Start](quick-start.md) has the rest.

## Reporting a problem

Found a way to change something? Report it privately
([SECURITY.md](https://github.com/Controlvar/controlvar/blob/main/SECURITY.md)), not in an issue.
