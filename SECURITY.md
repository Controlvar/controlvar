# Security policy

Controlvar handles experiment assignments, feature-flag evaluations, audit
records and, with the `hipaa` module installed, PHI. We take vulnerability
reports seriously and we would rather hear about a problem from you than from
an incident.

## Reporting a vulnerability

**Do not open a public GitHub issue for a security problem.** Public issues are
world-readable the moment they are created.

Report privately, by either route:

1. **GitHub private vulnerability reporting** (preferred) — on this repository,
   go to **Security → Report a vulnerability**. This creates a private advisory
   that only the maintainers and you can see.
2. **Email** — <security@controlvar.com>.

Please include, as far as you have it:

- the affected component (backend API, dashboard, a specific SDK, a module
  under `modules/`, the CDK stacks) and the version, tag or commit SHA;
- a description of the issue and what an attacker can achieve with it;
- reproduction steps, a proof of concept, or a minimal request/response pair;
- any configuration required to reproduce (auth provider, profile, installed
  modules);
- whether you have disclosed it anywhere else, and any deadline you are working
  to.

A PGP key for encrypted reports is **not yet published**. Until it is, send
non-sensitive details by email and use the GitHub private advisory for anything
that includes credentials, customer data or a working exploit. This file will be
updated with a key fingerprint when one exists.

## What happens next

| Stage | Target |
|---|---|
| We acknowledge your report | 3 business days |
| We confirm or dispute the finding, with a severity | 10 business days |
| Fix released for **Critical** and **High** severity | **14 days** from confirmation |
| Fix released for Medium and Low severity | Next scheduled release |
| Public disclosure | **90 days** from your report, or on the day the fix ships, whichever comes first |

We will tell you which severity we assigned and why, using CVSS v3.1 as the
reference. If we need longer than 90 days — for example a fix that requires a
breaking change to an SDK contract — we will say so before day 90 and agree an
extension with you rather than letting the clock run out silently. If we cannot
agree, you are free to disclose at 90 days; we will not treat that as a breach
of anything.

We request a CVE for confirmed vulnerabilities in released versions and credit
reporters in the advisory and the changelog unless you ask us not to.

## Supported versions

Controlvar has not yet cut a `1.0.0` release. Until it does, only the latest
release receives fixes: upgrade to it.

| Version | Supported |
|---|---|
| The latest release | ✅ Receives fixes |
| Older releases and release candidates | ❌ Upgrade to the latest release |

Once `1.0.0` ships, the policy is: the current minor release and the one before
it receive security fixes, for at least 6 months after the newer minor is
released.

The whole repository is covered by this policy: the core, the optional modules
under `modules/` (both Apache-2.0) and the SDKs (MIT).

## Scope

In scope:

- the backend API, background schedulers and Lambda functions;
- the dashboard;
- the client SDKs in `sdk/`;
- the optional modules in `modules/` (workspaces, custom roles, SSO, HIPAA,
  compliance reporting, warehouse analytics, integrations, counters, ETL,
  split URL testing);
- the shipped container images, `docker-compose.yml` and the CDK stacks;
- authentication, authorisation, tenancy and audit-trail integrity.

Out of scope (report them anyway if you think we are wrong, but expect them to
be closed as informational):

- findings that require an attacker to already hold valid administrator
  credentials, unless they cross a documented trust boundary;
- results from a default development configuration — `DEV_AUTH_BYPASS=true`,
  `ENVIRONMENT=development`, the seeded `admin@demo.com` account, the
  placeholder `SECRET_KEY` and first administrator's password in `.env.example` — which are documented as
  insecure and refuse to start under `ENVIRONMENT=production`;
- missing hardening headers or rate limits on the demo applications in `demo/`;
- vulnerabilities in third-party dependencies with no exploitable path in this
  codebase (send them to the upstream project; open a normal issue here to ask
  for a bump);
- volumetric denial of service, spam or social-engineering findings;
- reports produced solely by an automated scanner with no demonstrated impact.

## Safe harbour

If you make a good-faith effort to comply with this policy while researching a
vulnerability in Controlvar, we will consider your research authorised, we
will not initiate or support legal action against you, and we will not report
you to law enforcement. We will make it known that your actions were authorised
if a third party brings legal action against you for research conducted under
this policy.

Good faith means:

- you only test against your own installation, or against infrastructure we have
  explicitly designated for testing — never against another user's data or
  another organisation's deployment;
- you do not access, modify, destroy or exfiltrate data that is not yours, and
  you stop as soon as you have confirmed the vulnerability;
- you do not degrade the availability of any service (no volumetric or
  destructive testing);
- you give us a reasonable opportunity to fix the issue before disclosing it,
  per the timelines above;
- you comply with applicable law.

This safe harbour is a commitment from the Controlvar maintainers about how we
will behave. It cannot bind a third party — if you test against someone else's
hosted Controlvar deployment, their terms and their law apply, not ours.

## Hardening your own deployment

`docs/security/` and `docs/self-hosting/` cover the settings that matter most:
`ENVIRONMENT=production` (which makes the configuration validators deny by
default), `AUTH_PROVIDER`, `DEV_AUTH_BYPASS=false`, a real `SECRET_KEY`, TLS
termination, and restricting `/metrics` with `METRICS_TOKEN`.
