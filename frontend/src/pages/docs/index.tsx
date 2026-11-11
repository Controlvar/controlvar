import React from 'react';
import { PageTitle } from '@/components/PageTitle';
import { DOCS_BRANCH, DOCS_REPO, docsUrl } from '@/services/docs';
import { BOOKS_URL } from '@/content/site-links';
import { FOCUS, H1, H2, WIDTH } from '@/components/homepage/styles';

/**
 * Line icons in the homepage's style (24 px grid, 2 px stroke), one per
 * group, in place of the emoji and letters the hub had (site design
 * 2026-11-11, /docs item 4). Paths only: drawn by `GroupIcon`.
 */
const ICONS = {
  start: ['M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z', 'M10 8l6 4-6 4z'],
  flag: ['M4 22V4', 'M4 4h12l-2 4 2 4H4'],
  flask: ['M9 3h6', 'M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3', 'M7.5 14h9'],
  chip: ['M6 6h12v12H6z', 'M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4'],
  box: ['M12 2l9 5v10l-9 5-9-5V7z', 'M12 22V12M21 7l-9 5-9-5'],
  code: ['M8 6l-6 6 6 6', 'M16 6l6 6-6 6'],
  plug: ['M9 2v6M15 2v6', 'M6 8h12v3a6 6 0 0 1-12 0z', 'M12 17v5'],
  shield: ['M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z', 'M9 12l2 2 4-4'],
  server: ['M4 4h16v6H4z', 'M4 14h16v6H4z', 'M8 7h.01M8 17h.01'],
  book: ['M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z', 'M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5'],
  chart: ['M3 3v18h18', 'M7 15l4-4 3 3 5-6'],
  users: ['M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', 'M2 21v-1a6 6 0 0 1 12 0v1', 'M16 3.5a4 4 0 0 1 0 7', 'M22 21v-1a6 6 0 0 0-4-5.6'],
} as const;

function GroupIcon({ paths }: { paths: readonly string[] }) {
  return (
    <span className="inline-flex h-9 w-9 flex-none items-center justify-center rounded-lg bg-blue-50 text-blue-700">
      <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        {paths.map((d) => (
          <path key={d} d={d} />
        ))}
      </svg>
    </span>
  );
}

const sections = [
  {
    category: 'Getting Started',
    icon: ICONS.start,
    description: 'Run the stack and create your first experiment.',
    links: [
      { label: 'Quick Start', href: docsUrl('getting-started/quick-start'), desc: 'Run the stack and create your first experiment' },
      { label: 'Core Concepts', href: docsUrl('getting-started/concepts'), desc: 'Feature flags, experiments, variants, and metrics' },
      { label: 'Architecture Overview', href: docsUrl('getting-started/architecture'), desc: 'How the components fit together, and how data flows between them' },
      { label: 'FAQ', href: docsUrl('getting-started/faq'), desc: 'Common questions answered' },
      { label: 'Modules & Profiles', href: docsUrl('getting-started/modules'), desc: 'What the core profile includes, what each module adds, and how to run the full profile' },
    ],
  },
  {
    category: 'Feature Flags',
    icon: ICONS.flag,
    description: 'Create flags, target them with rules, and roll them out in stages.',
    links: [
      { label: 'Creating Feature Flags', href: docsUrl('feature-flags/create'), desc: 'Set up your first flag' },
      { label: 'Targeting & Rules', href: docsUrl('Enhanced_Rules_Engine_Reference'), desc: 'The operators a targeting rule can use, with examples' },
      { label: 'Gradual Rollouts', href: docsUrl('feature-flags/rollouts'), desc: 'Staged rollout schedules' },
      { label: 'Safety Monitoring', href: docsUrl('feature-flags/safety'), desc: 'Roll a flag back when its error rate or latency breaches a threshold' },
    ],
  },
  {
    category: 'A/B Testing & Experiments',
    icon: ICONS.flask,
    description: 'Create and start experiments, read their results, and the statistics behind them.',
    links: [
      { label: 'Running Experiments', href: docsUrl('guides/user-guide'), desc: 'End-to-end experiment lifecycle' },
      { label: 'Dashboard and API', href: docsUrl('guides/dashboard-and-api'), desc: 'Which capabilities have a dashboard screen, and which work through the API only for now' },
      { label: 'Statistical Methods', href: docsUrl('api/sequential-testing'), desc: 'Sequential testing with mSPRT' },
      { label: 'CUPED', href: docsUrl('api/cuped'), desc: "Variance reduction from each user's own pre-experiment events (API only)" },
      { label: 'Multi-Armed Bandits', href: docsUrl('api/multi-armed-bandit'), desc: 'Thompson Sampling, UCB1, Epsilon-Greedy (create a bandit through the API)' },
      { label: 'Split URL Testing', href: docsUrl('api/split-url'), desc: 'Full profile: give each variant its own URL. Your server or edge, or on AWS an optional CloudFront router, asks Controlvar where to send each visitor, and conversions count for the variant they saw.' },
      { label: 'Mutual Exclusion Groups', href: docsUrl('api/mutual-exclusion-groups'), desc: 'Keep users out of conflicting experiments, and a share of new users out of every experiment' },
      { label: 'Balance Within Groups', href: docsUrl('guides/balance-within-groups'), desc: 'Give each group of users the same split as the whole experiment, set through the API (beta)' },
      { label: 'Live Results Streaming', href: docsUrl('websocket-streaming'), desc: 'Live experiment results over WebSocket: a snapshot on connect, then a fresh one every 30 seconds (by default), whatever the client sends.' },
    ],
  },
  {
    category: 'LLM / AI Model Evaluation',
    icon: ICONS.chip,
    description: 'Compare model variants on a task; the winner has the best business metric when every variant has one, and otherwise the best LLM-judge score.',
    links: [
      { label: 'Overview', href: docsUrl('llm-evaluation/overview'), desc: 'Supported providers, task types, variant assignment, judge scoring, cost estimation and the analysis' },
      { label: 'Quick Start', href: docsUrl('llm-evaluation/quickstart'), desc: 'Compare GPT-4o with Claude 3.5 Sonnet (steps 3–5 need provider API keys)' },
    ],
  },
  {
    category: 'SDKs',
    icon: ICONS.box,
    description: 'Client libraries for web, mobile and server. The JavaScript, React, React Native, Python, Java and .NET SDKs and the OpenFeature providers are on npm, PyPI, Maven Central or NuGet.',
    links: [
      { label: 'JavaScript SDK', href: docsUrl('sdk/javascript'), desc: 'Browser and Node.js' },
      { label: 'Python SDK', href: docsUrl('sdk/python'), desc: 'A synchronous Python client with no dependencies' },
      { label: 'Java SDK', href: docsUrl('sdk/java'), desc: 'Spring Boot 3 auto-configuration starter; thread-safe client with a TTL cache' },
      { label: 'React SDK', href: docsUrl('sdk/react'), desc: 'A context provider, hooks and a higher-order component' },
      { label: 'Go SDK', href: docsUrl('sdk/go'), desc: 'A Go client for services: standard library only, goroutine-safe, context-aware.' },
      { label: 'iOS Swift SDK', href: docsUrl('sdk/ios'), desc: 'Native Swift SDK for iOS 14+ and macOS 11+, with an async/await API.' },
      { label: 'Android Kotlin SDK', href: docsUrl('sdk/android'), desc: 'Native Kotlin SDK for Android (minSdk 21). Coroutines, OkHttp, Compose examples.' },
      { label: 'Flutter SDK', href: docsUrl('sdk/flutter'), desc: 'Dart SDK for Flutter (iOS, Android, web and desktop). Results are decided by the server, with an offline fallback.' },
      { label: 'React Native SDK', href: docsUrl('sdk/react-native'), desc: 'useFlag and useExperiment hooks, Provider, AsyncStorage offline support.' },
      { label: 'MCP server', href: docsUrl('mcp-server'), desc: 'Connect Claude Code, Claude Desktop or Cursor to your instance: read flags and experiment results.' },
      { label: 'OpenFeature Provider', href: docsUrl('sdk/openfeature'), desc: 'OpenFeature (a CNCF project) providers' },
      { label: 'Edge SDK', href: docsUrl('sdk/edge'), desc: 'For Cloudflare Workers, Vercel Edge and Deno Deploy. Zero runtime dependencies; results cached in memory or KV.' },
      { label: 'Ruby SDK', href: docsUrl('sdk/ruby'), desc: 'Ruby gem with zero runtime dependencies, a thread-safe TTL cache and Net::HTTP.' },
      { label: 'PHP SDK', href: docsUrl('sdk/php'), desc: 'For PHP 8.1+, installed with Composer; it needs only ext-curl and ext-json.' },
      { label: '.NET SDK', href: docsUrl('sdk/dotnet'), desc: 'Targets net8.0 and netstandard2.1; System.Text.Json and HttpClient, no other dependencies.' },
      { label: 'Elixir SDK', href: docsUrl('sdk/elixir'), desc: "For Elixir and Phoenix: OTP's :httpc and Jason, with a GenServer-backed ETS cache." },
    ],
  },
  {
    category: 'API Reference',
    icon: ICONS.code,
    description: 'How to call the REST API: authentication, and the endpoints with example requests.',
    links: [
      { label: 'Authentication', href: docsUrl('api/auth'), desc: 'API keys for SDKs, bearer tokens for people' },
      { label: 'Experiments', href: docsUrl('api/endpoints'), desc: 'List experiments, get one, and get its results' },
      { label: 'Feature Flags', href: docsUrl('api/endpoints'), desc: "List flags, a user's flags, and archived flags" },
      { label: 'Compliance & Audit', href: docsUrl('api/compliance'), desc: 'Full profile: signed audit events and evidence-report export' },
      { label: 'Integrations', href: docsUrl('api/integrations'), desc: 'Jira, Salesforce, GitHub webhooks' },
      { label: 'All Endpoints', href: docsUrl('api/endpoints'), desc: 'Endpoint reference, with example requests and error responses' },
    ],
  },
  {
    category: 'Integrations',
    icon: ICONS.plug,
    description: 'The AWS services Controlvar runs on, the webhooks it receives, and the alerts it sends.',
    links: [
      { label: 'AWS', href: docsUrl('integrations/aws'), desc: 'How the platform uses ECS Fargate, Aurora, ElastiCache and DynamoDB' },
      { label: 'Jira', href: docsUrl('api/integrations'), desc: 'Inbound webhooks, signed or with a shared secret' },
      { label: 'Salesforce', href: docsUrl('integrations/salesforce'), desc: 'Inbound webhooks from a Flow or Apex callout' },
      { label: 'GitHub', href: docsUrl('integrations/github'), desc: 'Inbound webhooks, each checked by its HMAC-SHA256 signature' },
      { label: 'Slack & Email', href: docsUrl('api/alerting'), desc: 'Slack messages for scheduled experiment changes and rollout stage advances; email only from the test route' },
    ],
  },
  {
    category: 'Security & Compliance',
    icon: ICONS.shield,
    description: 'Audit records, compliance reports, roles, API keys and single sign-on.',
    links: [
      { label: 'Compliance audit logging', href: docsUrl('api/compliance'), desc: 'An append-only record of changes to experiments and flags; signing and export in the full profile' },
      { label: 'Compliance evidence', href: docsUrl('api/compliance'), desc: 'Full profile: SOC 2 and ISO 27001 reports and an export of the audit events' },
      { label: 'Audit Logging', href: docsUrl('api/audit-logging'), desc: 'Who changed what through the API; no API edits or deletes an entry' },
      { label: 'RBAC', href: docsUrl('api/rbac'), desc: 'Admin, Developer, Analyst and Viewer decide what a user can do; in the full profile, custom roles and direct grants add to them' },
      { label: 'API Key Management', href: docsUrl('security/api-keys'), desc: 'Create, expire and delete keys; the sdk:ruleset scope for local evaluation' },
      { label: 'SSO / SAML / OIDC', href: docsUrl('auth/sso'), desc: 'Full profile: sign in through Okta, Google or GitHub (OIDC). SAML 2.0 sign-in is not available yet; use OIDC.' },
      { label: 'HIPAA controls', href: docsUrl('hipaa/overview'), desc: 'Full profile: PHI encryption (Fernet), a 6-year retention marker on PHI audit records, BAA records and the data-residency configuration.' },
    ],
  },
  {
    category: 'Self-Hosting',
    icon: ICONS.server,
    description: 'Run Controlvar yourself: Docker Compose on one machine, Helm on Kubernetes, or a CDK app that deploys to AWS.',
    links: [
      { label: 'AWS CDK Deployment', href: docsUrl('self-hosting/cdk'), desc: 'CDK app for ECS Fargate, Aurora and ElastiCache' },
      { label: 'Docker Compose', href: `${DOCS_REPO}/blob/${DOCS_BRANCH}/deploy/compose/README.md`, desc: 'The published images on one Docker host: deploy/compose/compose.yml, and a README that says how to run it and lists its settings' },
      { label: 'Kubernetes (Helm)', href: docsUrl('self-hosting/kubernetes'), desc: 'The Helm chart in charts/experimently: a one-time secret step, then one install command' },
      { label: 'Environment Variables', href: docsUrl('getting-started/environment-setup'), desc: 'Configuration reference' },
      { label: 'Database Migrations', href: docsUrl('self-hosting/migrations'), desc: 'Alembic migration guide' },
      { label: 'Monitoring', href: docsUrl('self-hosting/monitoring'), desc: 'CloudWatch dashboards, Prometheus metrics' },
    ],
  },
  {
    category: 'Guides & Tutorials',
    icon: ICONS.book,
    description: 'Walkthroughs and method guides.',
    links: [
      { label: 'Your First A/B Test', href: docsUrl('guides/user-guide'), desc: 'End-to-end experiment walkthrough' },
      { label: 'Bayesian Experimentation', href: docsUrl('api/bayesian'), desc: 'Beta-Binomial posteriors and stopping rules' },
      { label: 'Guided Experiment Builder', href: docsUrl('guides/experiment-wizard'), desc: 'Guided setup in the dashboard, and the wizard API' },
      { label: 'Interaction Detection', href: docsUrl('api/interaction-detection'), desc: 'Beta: find experiments that share users, and whether their effects interact' },
    ],
  },
  {
    category: 'Statistics Reference',
    icon: ICONS.chart,
    description: 'How power analysis, post-stratification and FDR correction work.',
    links: [
      { label: 'Power Analysis & Sample Size', href: docsUrl('statistics/power-analysis'), desc: 'Pre-experiment planning: MDE, power and runtime estimation' },
      { label: 'Post-Stratification', href: docsUrl('statistics/post-stratification'), desc: 'The method, explained; not available through the API yet' },
      { label: 'FDR Correction', href: docsUrl('statistics/fdr-correction'), desc: 'Benjamini-Hochberg false discovery rate correction for multiple metrics' },
    ],
  },
  {
    category: 'Team Workspaces',
    icon: ICONS.users,
    description: 'Group members into teams, projects or product areas with workspace roles. Workspaces do not limit access to experiments or flags; that is by platform role.',
    links: [
      { label: 'Workspace Overview', href: docsUrl('workspaces/overview'), desc: 'What workspaces do and do not do, workspace roles and invitations' },
      { label: 'Quickstart', href: docsUrl('workspaces/quickstart'), desc: 'Create a workspace, add your team to it, and connect an SDK' },
    ],
  },
];

/**
 * The ways in (site design 2026-11-11, /docs item 2): the quick start, the
 * concepts, and the Controlvar books, which the hub did not link at all
 * (#1859 linked them from the homepage and the footer only). Titles only:
 * every sentence on this page is a registered claim (the descriptions above,
 * in `docs/_claims/`), and these add none.
 */
const startHere = [
  { label: 'Quick start', href: docsUrl('getting-started/quick-start'), icon: ICONS.start },
  { label: 'Core concepts', href: docsUrl('getting-started/concepts'), icon: ICONS.box },
  { label: 'The Controlvar books', href: BOOKS_URL, icon: ICONS.book },
];

/**
 * The docs hub (`/docs`), restructured by the site design of 2026-11-11: a
 * left-aligned header like the homepage's with one button to the whole
 * documentation (the search-styled box it replaces searched nothing, P#14);
 * a "Start here" row; then every topic group, flowed in CSS columns so a
 * group is as tall as its own links (the stretched grid left 4,000 px of empty
 * card at 1440 px). Each link is its title with its description under it, so
 * every description starts at the same x. The descriptions are unchanged:
 * each is held to its page (`backend/tests/unit/docs/test_docs_hub_claims.py`).
 * The homepage's slate, line icons and 1,180 px width; the site's h1 and h2.
 */
export default function DocsIndex() {
  return (
    <>
      <PageTitle
        title="Documentation"
        path="/docs"
        description="Controlvar documentation: quick start, a guide for every SDK, API reference, integrations, HIPAA controls and self-hosting."
      />

      <div className="flex-1 bg-white text-slate-900">
        <div className="border-b border-slate-200 bg-gradient-to-b from-slate-50 to-white">
          <div className={`${WIDTH} py-12 lg:py-14`}>
            <h1 className={H1}>Controlvar Documentation</h1>
            <p className="mt-4 max-w-2xl text-lg leading-relaxed text-slate-600">
              Everything you need to build, ship, and measure with confidence.
            </p>
            <p className="mt-6">
              <a
                href={docsUrl('')}
                className={`inline-flex min-h-11 items-center rounded-md bg-blue-600 px-5 font-medium text-white hover:bg-blue-700 ${FOCUS}`}
              >
                Browse the documentation <span aria-hidden="true">&nbsp;→</span>
              </a>
            </p>
          </div>
        </div>

        <div className={`${WIDTH} pb-20`}>
          <section aria-labelledby="start-here-heading" className="pt-10">
            <h2 id="start-here-heading" className={H2}>
              Start here
            </h2>
            <ul role="list" className="mt-5 grid gap-4 sm:grid-cols-3">
              {startHere.map((link) => (
                <li key={link.label} className="min-w-0">
                  <a
                    href={link.href}
                    className={`flex min-h-11 items-center gap-3 rounded-xl border border-slate-200 p-4 font-semibold text-slate-900 hover:border-blue-300 hover:text-blue-700 ${FOCUS}`}
                  >
                    <GroupIcon paths={link.icon} />
                    {link.label}
                  </a>
                </li>
              ))}
            </ul>
          </section>

          <section aria-labelledby="topics-heading" className="pt-12">
            <h2 id="topics-heading" className={H2}>
              Every topic
            </h2>
            <div className="mt-6 gap-10 md:columns-2 lg:columns-3">
              {sections.map((section) => (
                <div key={section.category} className="mb-10 break-inside-avoid">
                  <div className="flex items-center gap-3">
                    <GroupIcon paths={section.icon} />
                    <h3 className="text-lg font-semibold text-slate-900">{section.category}</h3>
                  </div>
                  <p className="mt-2 text-sm leading-relaxed text-slate-600">{section.description}</p>
                  <ul role="list" className="mt-4 space-y-3 border-l border-slate-200 pl-4">
                    {section.links.map((link) => (
                      <li key={link.label}>
                        <a href={link.href} className={`group block text-sm ${FOCUS}`}>
                          <span className="block font-medium text-blue-700 group-hover:text-blue-900">{link.label}</span>{' '}
                          <span className="block text-slate-600 [overflow-wrap:anywhere]">{link.desc}</span>
                        </a>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          </section>
        </div>

        {/* No footer of its own: on the marketing build the shell adds the
            site's footer (HomeFooter) below every page; a platform build's
            dashboard has none. */}
      </div>
    </>
  );
}
