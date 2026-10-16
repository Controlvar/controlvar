"""Each description on the dashboard's docs hub is true of the page it links to (#1142).

The hub, ``frontend/src/pages/docs/index.tsx``, gives every link a description
(``desc:``) and every section one (``description:``). Its registry,
``docs/_claims/frontend/src/pages/docs/index.tsx.desc.toml``, points each
description quoted in ``CLAIMS`` below at ``mechanism:docs-hub``
(``scripts/doc_claims.py``), and this file holds each one to what the page
says:

* a link's description to the page its ``href`` names: ``docsUrl('<page>')``
  is ``docs/<page>.md``, and a link to a file on GitHub
  (``${DOCS_REPO}/blob/${DOCS_BRANCH}/<path>``) is that file;
* a section's description to the pages of its own links, each phrase naming
  the page it is on.

For each description, ``lede`` phrases must be in the page's title or lede
(everything above its first ``##`` heading; for a file that is not Markdown,
its leading comment), so the link goes to a page about what the description
says, and ``says`` phrases anywhere on the page, one for each further fact.
Phrases are compared with the page's markup taken out as the claims registry
takes it out (backticks, ``*``, link targets), whitespace collapsed and case
ignored.

A description edited on the hub is no longer quoted here, so ``doc_claims.py
check`` refuses it until its entry is written again against the page; a page
that stops saying a phrase fails ``test_each_description_is_true_of_its_page``.
Reads files only: no Node, no network.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
HUB = "frontend/src/pages/docs/index.tsx"
DOC_CLAIMS = REPO_ROOT / "scripts" / "doc_claims.py"

LEDE = "lede"
PAGE = "page"


class Said(NamedTuple):
    """One phrase a page says: where on it (``lede`` or ``page``), and which page."""

    where: str
    phrase: str
    page: str = ""  # a section's description names the page; a link's is its href


def lede(phrase: str, page: str = "") -> Said:
    return Said(LEDE, phrase, page)


def says(phrase: str, page: str = "") -> Said:
    return Said(PAGE, phrase, page)


QS = "docs/getting-started/quick-start.md"

#: Each description the hub shows, word for word, and what its page says.
CLAIMS: Dict[str, Tuple[Said, ...]] = {
    # Getting Started
    "Run the stack and create your first experiment.": (
        lede("From a fresh clone to a running experiment", QS),
        says("docker compose up", QS),
    ),
    "Run the stack and create your first experiment": (
        lede("From a fresh clone to a running experiment"),
        says("docker compose up"),
    ),
    "Feature flags, experiments, variants, and metrics": (
        lede("Core Concepts"),
        says("## Feature Flags"),
        says("## Experiments"),
        says("## Variants"),
        says("## Metrics"),
    ),
    "How the components fit together, and how data flows between them": (
        lede("how the components fit together, how data flows"),
    ),
    "Common questions answered": (lede("Frequently Asked Questions"),),
    "What the core profile includes, what each module adds, and how to run the full profile": (
        lede("core — the product without the optional modules"),
        says("full — the product with the optional modules installed"),
    ),
    # Feature Flags
    "Create flags, target them with rules, and roll them out in stages.": (
        lede(
            "This guide creates a flag, adds targeting rules",
            "docs/feature-flags/create.md",
        ),
        lede("in controlled stages", "docs/feature-flags/rollouts.md"),
    ),
    "Set up your first flag": (lede("This guide creates a flag"),),
    "The operators a targeting rule can use, with examples": (
        lede("Operator Reference"),
        says("Usage examples"),
    ),
    "Staged rollout schedules": (
        lede(
            "raises a feature flag's rollout percentage over time, in controlled stages"
        ),
        says("rollout schedule"),
    ),
    "Roll a flag back when its error rate or latency breaches a threshold": (
        lede("error rate and latency against thresholds"),
        lede("it rolls the flag back"),
    ),
    # A/B Testing & Experiments
    "Create and start experiments, read their results, and the statistics behind them.": (
        lede(
            "The table lists each capability and where you do it today",
            "docs/guides/dashboard-and-api.md",
        ),
        says(
            "Create an experiment, with guided setup",
            "docs/guides/dashboard-and-api.md",
        ),
        says(
            "Start, pause, resume and complete an experiment",
            "docs/guides/dashboard-and-api.md",
        ),
        says("### Reading Results", "docs/guides/user-guide.md"),
        lede("Sequential Testing", "docs/api/sequential-testing.md"),
    ),
    "End-to-end experiment lifecycle": (
        lede("User Guide"),
        says("### Creating an Experiment"),
        says("### Pausing and Stopping"),
        says("### Reading Results"),
        says("#### Recommendation Meanings"),
    ),
    "Which capabilities have a dashboard screen, and which work through the API only for now": (
        lede("The table lists each capability and where you do it today"),
        lede("API only: it works through the API, with no screen yet"),
    ),
    "Sequential testing with mSPRT": (
        lede("Sequential Testing"),
        says("### mSPRT (mixture Sequential Probability Ratio Test)"),
    ),
    "Variance reduction from each user's own pre-experiment events (API only)": (
        lede("CUPED Variance Reduction"),
        lede("by adjusting for what each user did before the experiment"),
        lede("the dashboard and GET /results/{id} show the unadjusted numbers"),
    ),
    "Thompson Sampling, UCB1, Epsilon-Greedy (create a bandit through the API)": (
        lede("Multi-Armed Bandit"),
        says(
            "Set optimization_type to one of thompson_sampling, ucb1 or epsilon_greedy when you create the experiment"
        ),
    ),
    "Full profile: store split URL experiments and preview the URL a user id hashes to. The edge router does not split traffic yet.": (
        lede("present in the full profile"),
        lede("The edge router does not split traffic in this release"),
        lede(
            "What works today is storing a split URL experiment and its configuration, and the preview endpoint"
        ),
        lede("it hashes the user_id you pass with the experiment's id"),
        says(
            "Returns the URL variant the preview's own hash gives a user_id, without redirecting"
        ),
    ),
    "Keep users out of conflicting experiments, and a share of new users out of every experiment": (
        lede("prevent users from being enrolled in multiple conflicting experiments"),
        lede(
            "A global holdout keeps a percentage of new users out of every experiment"
        ),
    ),
    "Live experiment results over WebSocket: a snapshot on connect, then a fresh one every 30 seconds (by default), whatever the client sends.": (
        lede("Live Results over WebSocket"),
        says("streams experiment result snapshots to connected clients"),
        says(
            "On connect the server sends an initial snapshot. Then it sends a fresh one every 30 seconds"
        ),
        says("RESULTS_STREAM_INTERVAL_SECONDS"),
        says("whatever the client sends in between"),
    ),
    # LLM / AI Model Evaluation
    "Compare model variants on a task; the winner has the best business metric when every variant has one, and otherwise the best LLM-judge score.": (
        lede(
            "compares two models on a customer support task",
            "docs/llm-evaluation/quickstart.md",
        ),
        says(
            "the winner is the variant with the highest mean on it",
            "docs/llm-evaluation/overview.md",
        ),
        says(
            "The metric is business_metric when every variant has at least one business-metric value; otherwise it is auto_eval",
            "docs/llm-evaluation/overview.md",
        ),
        says(
            "Judge scores are stored as auto_eval_score",
            "docs/llm-evaluation/overview.md",
        ),
    ),
    "Supported providers, task types, variant assignment, judge scoring, cost estimation and the analysis": (
        lede("LLM/AI Model Evaluation — Overview"),
        says("## Supported Providers and Models"),
        says("## Task types and evaluation metrics"),
        says("## Variant Assignment"),
        says("## LLM-as-Judge Scoring"),
        says("## Cost Estimation"),
        says("## Statistical Analysis"),
    ),
    "Compare GPT-4o with Claude 3.5 Sonnet (steps 3–5 need provider API keys)": (
        lede("claude-3-5-sonnet-20241022 is the control and gpt-4o the treatment"),
        lede("Steps 3 to 5 need a provider key"),
    ),
    # SDKs
    "Client libraries for web, mobile and server. The JavaScript SDK is on npm and the Python SDK is on PyPI.": (
        lede("for Node >= 18 and browsers", "docs/sdk/javascript.md"),
        lede("iOS 14+", "docs/sdk/ios.md"),
        lede("Android applications", "docs/sdk/android.md"),
        lede("for Go services", "docs/sdk/go.md"),
        says("The @getexperimently/js-sdk package is on npm", "docs/sdk/javascript.md"),
        says("The experimently package is on PyPI", "docs/sdk/python.md"),
    ),
    "Browser and Node.js": (lede("for Node >= 18 and browsers"),),
    "A synchronous Python client with no dependencies": (
        lede("is a synchronous, dependency-free Python client"),
    ),
    "Spring Boot 3 auto-configuration starter; thread-safe client with a TTL cache": (
        lede(
            "adds Spring Boot auto-configuration (built and tested against Spring Boot 3.5.16"
        ),
        lede("caches the answer per user + key for a TTL"),
        says("The client is thread-safe"),
    ),
    "A context provider, hooks and a higher-order component": (
        lede("provides a context provider, hooks, a higher-order"),
    ),
    "A Go client for services: standard library only, goroutine-safe, context-aware.": (
        lede("for Go services"),
        lede("Standard library only, goroutine-safe, context-aware"),
    ),
    "Native Swift SDK for iOS 14+ and macOS 11+, with an async/await API.": (
        lede("is a Swift Package (iOS 14+, macOS 11+"),
        lede("event tracking with async/await"),
    ),
    "Native Kotlin SDK for Android (minSdk 21). Coroutines, OkHttp, Compose examples.": (
        lede("built on Kotlin Coroutines and OkHttp"),
        lede("Compose example"),
        says("minSdk 21"),
    ),
    "Dart SDK for Flutter (iOS, Android, web and desktop). Results are decided by the server, with an offline fallback.": (
        lede("for Flutter applications on iOS, Android, Web, macOS, Windows and Linux"),
        lede("decided by the server"),
        says("Persist results to the OfflineStore"),
    ),
    "useFlag and useExperiment hooks, Provider, AsyncStorage offline support.": (
        lede("a context provider, three hooks"),
        lede("AsyncStorage-backed offline fallback"),
        says("### useFlag(flagKey): FlagState"),
        says("### useExperiment(experimentKey): ExperimentState"),
    ),
    "Describes the API's tools for AI assistants.": (
        lede(
            "describes five tools in the vocabulary of the Model Context Protocol (MCP)"
        ),
    ),
    "OpenFeature (a CNCF project) providers": (
        lede("OpenFeature Provider"),
        says("is a CNCF (Cloud Native Computing Foundation) standard"),
    ),
    "For Cloudflare Workers, Vercel Edge and Deno Deploy. Zero runtime dependencies; results cached in memory or KV.": (
        lede(
            "with adapters for Cloudflare Workers, Vercel Edge Middleware and Deno Deploy"
        ),
        lede("It has zero runtime dependencies"),
        lede("in memory and, optionally, in a shared KV store"),
    ),
    "Ruby gem with zero runtime dependencies, a thread-safe TTL cache and Net::HTTP.": (
        lede("zero runtime gem dependencies (Net::HTTP"),
        lede("safe to share between threads"),
        lede("for a TTL"),
    ),
    "For PHP 8.1+, installed with Composer; it needs only ext-curl and ext-json.": (
        lede("for PHP 8.1+ applications. It depends only on ext-curl and ext-json"),
        says("Composer for installation"),
    ),
    "Targets netstandard2.1 and net6.0; System.Text.Json and HttpClient, no other dependencies.": (
        lede(
            "It targets netstandard2.1 and net6.0, uses System.Text.Json and HttpClient, and has no other dependencies"
        ),
    ),
    "For Elixir and Phoenix: OTP's :httpc and Jason, with a GenServer-backed ETS cache.": (
        lede("for Elixir and Phoenix applications"),
        lede("a GenServer client with a private ETS-backed cache"),
        lede(
            "uses Erlang's built-in :httpc for HTTP, so the only runtime dependency is Jason"
        ),
    ),
    # API Reference
    "How to call the REST API: authentication, and the endpoints with example requests.": (
        lede("The platform uses two authentication methods", "docs/api/auth.md"),
        lede("API Endpoints Documentation", "docs/api/endpoints.md"),
        says("## Usage Examples", "docs/api/endpoints.md"),
    ),
    "API keys for SDKs, bearer tokens for people": (
        lede("API Key (X-API-Key header) | SDK clients"),
        lede("Bearer token (Authorization: Bearer header) | People"),
    ),
    "List experiments, get one, and get its results": (
        lede("API Endpoints Documentation"),
        says("### List Experiments"),
        says("### Get Experiment"),
        says("### Get Experiment Results"),
    ),
    "List flags, a user's flags, and archived flags": (
        lede("API Endpoints Documentation"),
        says("### List Feature Flags"),
        says("### Get User Feature Flags"),
        says("### Archived Flags"),
    ),
    "Full profile: signed audit events and evidence-report export": (
        lede(
            "The SOC 2 / ISO 27001 reports and the export are the compliance module's"
        ),
        lede("HMAC signing of new events is also the module's"),
    ),
    "Jira, Salesforce, GitHub webhooks": (
        lede(
            "one stored configuration each for Jira, Salesforce and GitHub, and a webhook endpoint for each"
        ),
    ),
    "Endpoint reference, with example requests and error responses": (
        lede("API Endpoints Documentation"),
        says("## Usage Examples"),
        says("## Error Responses"),
    ),
    # Integrations
    "The AWS services Controlvar runs on, the webhooks it receives, and the alerts it sends.": (
        lede("how each AWS service is used", "docs/integrations/aws.md"),
        lede("a webhook endpoint for each", "docs/api/integrations.md"),
        lede(
            "sends a Slack message for each event",
            "docs/api/alerting.md",
        ),
    ),
    "How the platform uses ECS Fargate, Aurora, ElastiCache and DynamoDB": (
        lede("how each AWS service is used"),
        says("## ECS Fargate"),
        says("## Aurora PostgreSQL"),
        says("## ElastiCache Redis"),
        says("## DynamoDB"),
    ),
    "Inbound webhooks, signed or with a shared secret": (
        lede("a webhook endpoint for each that authenticates what the service sends"),
        says("Signature (preferred)"),
        says("Shared secret | X-Experimently-Webhook-Secret"),
    ),
    "Inbound webhooks from a Flow or Apex callout": (
        lede(
            "receives the events a Salesforce Flow or Apex callout sends to the platform"
        ),
    ),
    "Inbound webhooks, each checked by its HMAC-SHA256 signature": (
        lede(
            "receives your repository's webhook deliveries and checks each one's signature"
        ),
        says("HMAC-SHA256 signature"),
    ),
    "Slack messages for scheduled experiment changes and rollout stage advances; email only from the test route": (
        lede("sends a Slack message for each event"),
        lede("Email is sent only by the test route"),
        says("Scheduled experiment change"),
        says("Rollout stage advance"),
    ),
    # Compliance, roles, API keys and sign-in
    "Audit records, compliance reports, roles, API keys and single sign-on.": (
        lede(
            "The audit log is a record of who changed what", "docs/api/audit-logging.md"
        ),
        lede("The SOC 2 / ISO 27001 reports", "docs/api/compliance.md"),
        lede("Custom roles", "docs/api/rbac.md"),
        lede("API Key Management", "docs/security/api-keys.md"),
        lede("Single Sign-On (SSO)", "docs/auth/sso.md"),
    ),
    "An append-only record of changes to experiments and flags; signing and export in the full profile": (
        lede("an append-only table of changes to experiments, feature flags"),
        lede("HMAC signing of new events is also the module's"),
        lede("the export are the compliance module's"),
    ),
    "Full profile: SOC 2 and ISO 27001 reports and an export of the audit events": (
        lede(
            "The SOC 2 / ISO 27001 reports and the export are the compliance module's"
        ),
        says("### Export Audit Events"),
        says("GET /api/v1/compliance/export"),
    ),
    "Who changed what through the API; no API edits or deletes an entry": (
        lede("The audit log is a record of who changed what"),
        lede("No API edits or deletes an entry"),
        lede("It records the changes people make through the API"),
    ),
    "Admin, Developer, Analyst and Viewer decide what a user can do; custom roles (full profile) are stored but not applied yet": (
        lede(
            "Custom roles is one of the optional modules -- present in the full profile"
        ),
        says(
            "One of four built-in system roles assigned to every user (admin, developer, analyst, viewer)"
        ),
        says("Only the base role decides what a user can do"),
        says("Custom roles and direct grants are stored"),
        says("no permission check reads them"),
    ),
    "Create, expire and delete keys; the sdk:ruleset scope for local evaluation": (
        lede("an API key lasts until you delete it or it reaches its own expires_at"),
        says("sdk:ruleset is for a server: one that evaluates flags locally"),
    ),
    "Full profile: sign in through Okta, Google or GitHub (OIDC). SAML 2.0 sign-in is not available yet; use OIDC.": (
        lede("present in the full profile"),
        lede("Use an OIDC configuration (okta, google or github) for sign-in"),
        lede("SAML 2.0 sign-in is not available yet; use OIDC"),
    ),
    "Full profile: PHI encryption (Fernet), a 6-year retention marker on PHI audit records, BAA records and the data-residency configuration.": (
        lede("present in the full profile"),
        says("Protected Health Information (PHI) using Fernet"),
        says("Retention marker (retention_years = 6"),
        says("Business Associate Agreements are tracked"),
        says("Data residency configuration"),
    ),
    # Self-Hosting
    "Run Controlvar yourself: Docker Compose on one machine, Helm on Kubernetes, or a CDK app that deploys to AWS.": (
        lede(
            "runs Controlvar on one Docker host from the published images",
            "deploy/compose/README.md",
        ),
        lede(
            "installs on Kubernetes with the Helm chart",
            "docs/self-hosting/kubernetes.md",
        ),
        lede(
            "cdk deploy --all provisions the API, the dashboard and the data stores they need in your AWS account",
            "docs/self-hosting/cdk.md",
        ),
    ),
    "CDK app for ECS Fargate, Aurora and ElastiCache": (
        lede("AWS CDK v2"),
        lede("The Fargate stack runs two services"),
        says("Aurora PostgreSQL cluster"),
        says("ElastiCache Redis replication group"),
    ),
    "The published images on one Docker host: deploy/compose/compose.yml, and a README that says how to run it and lists its settings": (
        lede("Self-hosting with Docker Compose"),
        lede(
            "compose.yml in this directory runs Controlvar on one Docker host from the published images"
        ),
        says("## Run it"),
        says("## Settings"),
    ),
    "The Helm chart in charts/experimently: a one-time secret step, then one install command": (
        lede(
            "with the Helm chart in charts/experimently: a one-time secret step, then one install command"
        ),
    ),
    "Configuration reference": (lede("Environment Configuration Setup Guide"),),
    "Alembic migration guide": (
        lede("The platform uses Alembic for PostgreSQL schema management"),
    ),
    "CloudWatch dashboards, Prometheus metrics": (
        lede("adds the CloudWatch dashboards"),
        lede("exposes Prometheus metrics"),
        says("## Prometheus Metrics"),
    ),
    # Guides & Tutorials
    "End-to-end experiment walkthrough": (
        lede("User Guide"),
        says("### Creating an Experiment"),
        says("### Reading Results"),
        says("#### Recommendation Meanings"),
    ),
    "Beta-Binomial posteriors and stopping rules": (
        lede("a posterior for each variant"),
        lede("a stopping recommendation"),
        says("Beta-Binomial model"),
        says("stopping rules"),
    ),
    "Guided setup in the dashboard, and the wizard API": (
        lede("the dashboard's guided setup, and the wizard API"),
    ),
    "Beta: find experiments that share users, and whether their effects interact": (
        lede("Two experiments that run at the same time usually share users"),
        lede("how to test for an interaction"),
        lede("(beta) reports the overlap of any two"),
    ),
    # Statistics Reference
    "How power analysis, post-stratification and FDR correction work.": (
        lede("Statistical Power Analysis", "docs/statistics/power-analysis.md"),
        lede(
            "Post-Stratification Variance Reduction",
            "docs/statistics/post-stratification.md",
        ),
        lede("Benjamini-Hochberg FDR Correction", "docs/statistics/fdr-correction.md"),
    ),
    "Pre-experiment planning: MDE, power and runtime estimation": (
        lede("pre-experiment Power Calculator to plan your A/B tests"),
        says("Minimum Detectable Effect (MDE)"),
        says("Statistical power"),
        says("## Runtime Estimation"),
    ),
    "The method, explained; not available through the API yet": (
        lede("Not available through the API yet"),
        lede("answers 501 Not Implemented"),
    ),
    "Benjamini-Hochberg false discovery rate correction for multiple metrics": (
        lede(
            "The Benjamini-Hochberg (BH) procedure controls the False Discovery Rate (FDR)"
        ),
        lede("when testing multiple metrics"),
    ),
    # Team Workspaces
    "Group members into teams, projects or product areas with workspace roles. Workspaces do not limit access to experiments or flags; that is by platform role.": (
        says(
            "It represents a team, project, or product area",
            "docs/workspaces/overview.md",
        ),
        lede(
            "A workspace groups members and their workspace roles",
            "docs/workspaces/quickstart.md",
        ),
        lede(
            "It does not limit which experiments, feature flags or API keys anyone can see or change",
            "docs/workspaces/quickstart.md",
        ),
    ),
    "What workspaces do and do not do, workspace roles and invitations": (
        lede("Team Workspaces — Overview"),
        says(
            "A workspace holds its members, their workspace roles and its pending invitations"
        ),
    ),
    "Create a workspace, add your team to it, and connect an SDK": (
        lede("how to create a workspace, add your team to it, and connect an SDK"),
    ),
}


def _doc_claims():
    name = "_hub_claims_doc_claims"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, DOC_CLAIMS)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # dataclasses looks its module up
        spec.loader.exec_module(module)
    return sys.modules[name]


class Link(NamedTuple):
    section: str
    page: Optional[str]  # None for a section's own description


_CATEGORY = re.compile(r"^\s{0,12}category: '(?P<name>[^'\n]{1,80})',")
_HREF = re.compile(r"\bhref: (?P<href>[^,\n]{1,200}), desc:")
_DOCS_URL = re.compile(r"^docsUrl\('(?P<page>[^'\n]{0,200})'\)$")
_BLOB = re.compile(
    r"^`\$\{DOCS_REPO\}/blob/\$\{DOCS_BRANCH\}/(?P<path>[^`\s]{1,200})`$"
)


def _target(href: str) -> str:
    """The repository file a hub link opens."""
    match = _DOCS_URL.match(href)
    if match:
        page = match["page"].strip("/")
        return "docs/README.md" if page in ("", "README") else f"docs/{page}.md"
    match = _BLOB.match(href)
    if match:
        return match["path"]
    raise AssertionError(f"a hub link this file cannot follow: href {href}")


def hub_descriptions() -> Dict[str, Link]:
    """Each description on the hub, as the claims registry reads it, with its
    section and, for a link's, the file the link opens."""
    text = (REPO_ROOT / HUB).read_text(encoding="utf-8")
    lines = text.split("\n")
    section_of_line: List[str] = []
    section = ""
    for line in lines:
        match = _CATEGORY.match(line)
        if match:
            section = match["name"]
        section_of_line.append(section)
    found: Dict[str, Link] = {}
    for unit in _doc_claims().hub_units(text):
        line = lines[unit.line - 1]
        href = _HREF.search(line)
        found[unit.text] = Link(
            section_of_line[unit.line - 1], _target(href["href"]) if href else None
        )
    return found


def _plain(text: str) -> str:
    return _doc_claims().normalise(text).casefold()


def _lede(path: str, text: str) -> str:
    """The title and lede: above the first ``##`` heading, or a file's leading comment."""
    lines = text.split("\n")
    if path.endswith(".md"):
        kept = []
        for line in lines:
            if line.startswith("## "):
                break
            kept.append(line)
        return "\n".join(kept)
    kept = []
    for line in lines:
        if not line.startswith("#"):
            break
        kept.append(line.lstrip("#"))
    return "\n".join(kept)


def _problems(
    description: str, said: Tuple[Said, ...], hub: Dict[str, Link]
) -> List[str]:
    link = hub.get(description)
    if link is None:
        return [f"{description!r} is not on the hub any more: remove its entry"]
    found = []
    if not any(item.where == LEDE for item in said):
        found.append(
            f"{description!r}: name at least one phrase of its page's title or lede"
        )
    section_pages = {
        l.page for l in hub.values() if l.section == link.section and l.page
    }
    for item in said:
        page = item.page or link.page
        if link.page is not None and page != link.page:
            found.append(f"{description!r}: links to {link.page}, not {page}")
            continue
        if link.page is None and page not in section_pages:
            found.append(
                f"{description!r} describes the section {link.section!r}, which does"
                f" not link to {page}"
            )
            continue
        path = REPO_ROOT / page
        if not path.is_file():
            found.append(f"{description!r}: {page} does not exist")
            continue
        text = path.read_text(encoding="utf-8")
        where = _lede(page, text) if item.where == LEDE else text
        if _plain(item.phrase) not in _plain(where):
            found.append(
                f"{description!r}: {page} does not say {item.phrase!r}"
                + (" in its title or lede" if item.where == LEDE else "")
            )
    return found


def test_each_description_is_true_of_its_page():
    hub = hub_descriptions()
    found = [p for d, said in CLAIMS.items() for p in _problems(d, said, hub)]
    assert not found, "\n".join(found)


def test_the_hub_is_read_link_by_link():
    """The positive control for the reader above: every link line of the hub
    yields its description with the file it opens, and every section its own."""
    hub = hub_descriptions()
    text = (REPO_ROOT / HUB).read_text(encoding="utf-8")
    assert len(hub) == text.count(" desc: ") + text.count("description: '") > 70
    assert hub["Browser and Node.js"] == Link("SDKs", "docs/sdk/javascript.md")
    assert (
        hub["Configuration reference"].page
        == "docs/getting-started/environment-setup.md"
    )
    compose = [l for l in hub.values() if l.page == "deploy/compose/README.md"]
    assert len(compose) == 1
    assert hub[
        "How power analysis, post-stratification and FDR correction work."
    ] == Link("Statistics Reference", None)


@pytest.mark.parametrize(
    "description, said, problem",
    [
        pytest.param(
            "Browser and Node.js",
            (lede("for Deno and browsers"),),
            "does not say 'for Deno and browsers' in its title or lede",
            id="a phrase the page does not say",
        ),
        pytest.param(
            "Browser and Node.js",
            (says("Installation"),),
            "name at least one phrase of its page's title or lede",
            id="no phrase from the lede",
        ),
        pytest.param(
            "Browser and Node.js",
            (lede("Python SDK", "docs/sdk/python.md"),),
            "links to docs/sdk/javascript.md, not docs/sdk/python.md",
            id="evidence from a page the link does not open",
        ),
        pytest.param(
            "How power analysis, post-stratification and FDR correction work.",
            (lede("Gradual Rollouts", "docs/feature-flags/rollouts.md"),),
            "which does not link to docs/feature-flags/rollouts.md",
            id="a section's evidence from another section's page",
        ),
        pytest.param(
            "Works offline, everywhere",
            (lede("JavaScript SDK"),),
            "is not on the hub any more",
            id="an entry for a description the hub no longer shows",
        ),
        pytest.param(
            "CloudWatch dashboards, Prometheus metrics",
            (lede("CloudWatch"), says("## Grafana Dashboards")),
            "does not say '## Grafana Dashboards'",
            id="a phrase below the lede that the page does not say",
        ),
    ],
)
def test_a_description_its_page_does_not_back_is_refused(description, said, problem):
    found = _problems(description, said, hub_descriptions())
    assert any(problem in p for p in found), found
