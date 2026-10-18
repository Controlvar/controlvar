import { PageTitle } from '@/components/PageTitle';
import Link from 'next/link';
import { DOCS_BRANCH, DOCS_REPO, docsUrl } from '@/services/docs';

const sections = [
  {
    category: 'Getting Started',
    icon: '🚀',
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
    icon: '🚩',
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
    icon: '⚗️',
    description: 'Create and start experiments, read their results, and the statistics behind them.',
    links: [
      { label: 'Running Experiments', href: docsUrl('guides/user-guide'), desc: 'End-to-end experiment lifecycle' },
      { label: 'Dashboard and API', href: docsUrl('guides/dashboard-and-api'), desc: 'Which capabilities have a dashboard screen, and which work through the API only for now' },
      { label: 'Statistical Methods', href: docsUrl('api/sequential-testing'), desc: 'Sequential testing with mSPRT' },
      { label: 'CUPED', href: docsUrl('api/cuped'), desc: "Variance reduction from each user's own pre-experiment events (API only)" },
      { label: 'Multi-Armed Bandits', href: docsUrl('api/multi-armed-bandit'), desc: 'Thompson Sampling, UCB1, Epsilon-Greedy (create a bandit through the API)' },
      { label: 'Split URL Testing', href: docsUrl('api/split-url'), desc: 'Full profile: store split URL experiments and preview the URL a user id hashes to. The edge router does not split traffic yet.' },
      { label: 'Mutual Exclusion Groups', href: docsUrl('api/mutual-exclusion-groups'), desc: 'Keep users out of conflicting experiments, and a share of new users out of every experiment' },
      { label: 'Live Results Streaming', href: docsUrl('websocket-streaming'), desc: 'Live experiment results over WebSocket: a snapshot on connect, then a fresh one every 30 seconds (by default), whatever the client sends.' },
    ],
  },
  {
    category: 'LLM / AI Model Evaluation',
    icon: '🤖',
    description: 'Compare model variants on a task; the winner has the best business metric when every variant has one, and otherwise the best LLM-judge score.',
    links: [
      { label: 'Overview', href: docsUrl('llm-evaluation/overview'), desc: 'Supported providers, task types, variant assignment, judge scoring, cost estimation and the analysis' },
      { label: 'Quick Start', href: docsUrl('llm-evaluation/quickstart'), desc: 'Compare GPT-4o with Claude 3.5 Sonnet (steps 3–5 need provider API keys)' },
    ],
  },
  {
    category: 'SDKs',
    icon: '📦',
    description: 'Client libraries for web, mobile and server. The JavaScript SDK is on npm and the Python SDK is on PyPI.',
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
      { label: 'MCP tool manifest', href: docsUrl('mcp-server'), desc: "Describes the API's tools for AI assistants." },
      { label: 'OpenFeature Provider', href: docsUrl('sdk/openfeature'), desc: 'OpenFeature (a CNCF project) providers' },
      { label: 'Edge SDK', href: docsUrl('sdk/edge'), desc: 'For Cloudflare Workers, Vercel Edge and Deno Deploy. Zero runtime dependencies; results cached in memory or KV.' },
      { label: 'Ruby SDK', href: docsUrl('sdk/ruby'), desc: 'Ruby gem with zero runtime dependencies, a thread-safe TTL cache and Net::HTTP.' },
      { label: 'PHP SDK', href: docsUrl('sdk/php'), desc: 'For PHP 8.1+, installed with Composer; it needs only ext-curl and ext-json.' },
      { label: '.NET SDK', href: docsUrl('sdk/dotnet'), desc: 'Targets netstandard2.1 and net6.0; System.Text.Json and HttpClient, no other dependencies.' },
      { label: 'Elixir SDK', href: docsUrl('sdk/elixir'), desc: "For Elixir and Phoenix: OTP's :httpc and Jason, with a GenServer-backed ETS cache." },
    ],
  },
  {
    category: 'API Reference',
    icon: '📡',
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
    icon: '🔌',
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
    icon: '🔒',
    description: 'Audit records, compliance reports, roles, API keys and single sign-on.',
    links: [
      { label: 'Compliance audit logging', href: docsUrl('api/compliance'), desc: 'An append-only record of changes to experiments and flags; signing and export in the full profile' },
      { label: 'Compliance evidence', href: docsUrl('api/compliance'), desc: 'Full profile: SOC 2 and ISO 27001 reports and an export of the audit events' },
      { label: 'Audit Logging', href: docsUrl('api/audit-logging'), desc: 'Who changed what through the API; no API edits or deletes an entry' },
      { label: 'RBAC', href: docsUrl('api/rbac'), desc: 'Admin, Developer, Analyst and Viewer decide what a user can do; custom roles (full profile) are stored but not applied yet' },
      { label: 'API Key Management', href: docsUrl('security/api-keys'), desc: 'Create, expire and delete keys; the sdk:ruleset scope for local evaluation' },
      { label: 'SSO / SAML / OIDC', href: docsUrl('auth/sso'), desc: 'Full profile: sign in through Okta, Google or GitHub (OIDC). SAML 2.0 sign-in is not available yet; use OIDC.' },
      { label: 'HIPAA controls', href: docsUrl('hipaa/overview'), desc: 'Full profile: PHI encryption (Fernet), a 6-year retention marker on PHI audit records, BAA records and the data-residency configuration.' },
    ],
  },
  {
    category: 'Self-Hosting',
    icon: '🏗️',
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
    icon: '📖',
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
    icon: 'S',
    description: 'How power analysis, post-stratification and FDR correction work.',
    links: [
      { label: 'Power Analysis & Sample Size', href: docsUrl('statistics/power-analysis'), desc: 'Pre-experiment planning: MDE, power and runtime estimation' },
      { label: 'Post-Stratification', href: docsUrl('statistics/post-stratification'), desc: 'The method, explained; not available through the API yet' },
      { label: 'FDR Correction', href: docsUrl('statistics/fdr-correction'), desc: 'Benjamini-Hochberg false discovery rate correction for multiple metrics' },
    ],
  },
  {
    category: 'Team Workspaces',
    icon: 'W',
    description: 'Group members into teams, projects or product areas with workspace roles. Workspaces do not limit access to experiments or flags; that is by platform role.',
    links: [
      { label: 'Workspace Overview', href: docsUrl('workspaces/overview'), desc: 'What workspaces do and do not do, workspace roles and invitations' },
      { label: 'Quickstart', href: docsUrl('workspaces/quickstart'), desc: 'Create a workspace, add your team to it, and connect an SDK' },
    ],
  },
];

const quickLinks = [
  { label: 'Quick Start', href: docsUrl('getting-started/quick-start'), color: 'bg-blue-600 hover:bg-blue-700 text-white' },
  { label: 'API Reference', href: docsUrl('api/endpoints'), color: 'bg-gray-900 hover:bg-gray-800 text-white' },
  { label: 'SDK Guides', href: docsUrl('sdk/javascript'), color: 'bg-white hover:bg-gray-50 text-gray-900 border border-gray-200' },
];

export default function DocsIndex() {
  return (
    <>
      <PageTitle
        title="Documentation"
        description="Controlvar documentation: quick start, a guide for every SDK, API reference, integrations, HIPAA controls and self-hosting."
      />

      <div className="flex-1 bg-white">

        {/* Hero */}
        <div className="bg-gray-50 border-b border-gray-100 py-16 px-6">
          <div className="max-w-4xl mx-auto text-center">
            <h1 className="text-4xl font-bold text-gray-900 mb-4">
              Controlvar Documentation
            </h1>
            <p className="text-xl text-gray-600 mb-8">
              Everything you need to build, ship, and measure with confidence.
            </p>
            <div className="flex flex-wrap gap-3 justify-center">
              {quickLinks.map((link) => (
                <a
                  key={link.href}
                  href={link.href}
                  className={`px-6 py-3 rounded-lg font-medium text-sm transition shadow-sm ${link.color}`}
                >
                  {link.label}
                </a>
              ))}
            </div>
          </div>
        </div>

        {/* Search hint */}
        <div className="max-w-7xl mx-auto px-6 lg:px-8 py-6">
          <div className="flex items-center gap-2 bg-gray-50 border border-gray-200 rounded-lg px-4 py-3 max-w-lg">
            <svg className="w-4 h-4 text-gray-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <a href={docsUrl('')} className="text-sm text-gray-500 hover:text-gray-700">Browse the documentation →</a>
          </div>
        </div>

        {/* Docs grid */}
        <div className="max-w-7xl mx-auto px-6 lg:px-8 pb-24">
          <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-8">
            {sections.map((section) => (
              <div key={section.category} className="border border-gray-200 rounded-xl p-6 hover:border-blue-200 hover:shadow-sm transition">
                <div className="flex items-center gap-3 mb-3">
                  <span className="text-2xl" aria-hidden="true">{section.icon}</span>
                  <h2 className="text-lg font-semibold text-gray-900">{section.category}</h2>
                </div>
                <p className="text-sm text-gray-500 mb-5">{section.description}</p>
                <ul className="space-y-3">
                  {section.links.map((link) => (
                    <li key={link.href}>
                      <a
                        href={link.href}
                        className="group flex items-start gap-2 text-sm"
                      >
                        <span className="text-blue-600 group-hover:text-blue-700 font-medium transition flex-shrink-0">
                          {link.label}
                        </span>
                        <span className="text-gray-400">—</span>
                        <span className="text-gray-500 group-hover:text-gray-700 transition">
                          {link.desc}
                        </span>
                      </a>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </div>

        {/* Footer */}
        <footer className="bg-gray-900 text-white py-12 px-6">
          <div className="max-w-7xl mx-auto">
            <div className="flex flex-col md:flex-row justify-between items-center gap-6">
              <div>
                <Link href="/" className="text-xl font-semibold text-white">
                  Controlvar
                </Link>
                <p className="text-gray-400 text-sm mt-1">Modern experimentation platform</p>
              </div>
              <div className="flex flex-wrap gap-6 text-sm text-gray-400">
                <Link href="/" className="hover:text-white transition">Home</Link>
                <Link href="/docs" className="hover:text-white transition">Docs</Link>
                <a href={docsUrl('api/endpoints')} className="hover:text-white transition">API Reference</a>
                <a href={docsUrl('sdk/javascript')} className="hover:text-white transition">SDKs</a>
                <a href="mailto:hello@controlvar.com" className="hover:text-white transition">Contact</a>
              </div>
            </div>
            <div className="border-t border-gray-800 mt-8 pt-8">
              <p className="text-gray-400 text-sm text-center">
                © 2024–2026 The Controlvar authors. Apache-2.0; SDKs MIT.
              </p>
            </div>
          </div>
        </footer>
      </div>
    </>
  );
}
