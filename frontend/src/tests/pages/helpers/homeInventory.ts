/**
 * The homepage's inventory (#1459, QA 1a and 5c, EM-3): every sentence the
 * page renders, and every alt, aria-label and title it carries, and its meta
 * description, each with its source. `tests/pages/home-page.test.tsx` renders
 * the page in both builds and fails a rendered item that is not a row
 * ("unlisted") and a row that is not rendered ("listed, not rendered").
 *
 * WHAT AN ITEM IS (`renderedItems` in `homeText.ts`): the text of one block --
 * a heading, paragraph, list item, term, definition, caption, summary or code
 * block, or a run of inline text such as a link standing on its own -- with
 * its whitespace collapsed (a code block keeps its lines). `tag` is the
 * element it sits in, or the attribute it is: `alt`, `aria-label`, `title`,
 * `meta` for the description.
 *
 * WHERE A ROW COMES FROM. Typed rows are below, each with its source: a line
 * of main's `index.tsx` (main at 314bd3e7) the redesign keeps, the UX draft
 * (`v1-ux-designer.md`) that wrote or moved it, or the unchanged header. Rows
 * that are DATA are read from the data, never typed (EM-3):
 *   - the walkthroughs: titles, descriptions, kinds, lengths and transcripts,
 *     from `content/videos.json` and `content/walkthroughs/` through the very
 *     helpers the section renders with, so a manifest entry adds its rows;
 *   - the screenshots' alt texts and caption, from `content/site-screens.json`;
 *   - the quoted sentences and code, from `content/home-quotes.json`;
 *   - the SDK chips, from `content/sdks.ts`.
 * A typed row that repeats one of those is refused (`typedRepeatsData`), so a
 * video's title cannot be typed here and go stale when the manifest changes.
 */
import quotes from '@/content/home-quotes.json';
import registry from '@/content/site-screens.json';
import {
  KIND_LABEL,
  WALKTHROUGHS,
  kindOf,
  lengthLabel,
  onScreenLine,
  transcriptFor,
} from '@/components/Walkthroughs';
import { sdkChips } from '@/components/homepage/SdkChips';

export type Build = 'platform' | 'marketing';

export interface Row {
  tag: string;
  text: string;
  source: string;
  /** Rendered in this build only; both when absent. */
  build?: Build;
}

const MAIN = 'main:frontend/src/pages/index.tsx';
const UX = 'v1-ux-designer.md';
const HEADER = 'SiteHeader.tsx, unchanged (#1458)';

/** The JavaScript snippet's first line: the install command of main:index.tsx:124, as a comment (UX, How it works). */
export const SNIPPET_COMMENT = '// npm install @controlvar/js-sdk';

export const TYPED: readonly Row[] = [
  // The header, unchanged: the row and the phone menu (hidden until opened, still in the DOM).
  { tag: 'div', text: 'CControlvar', source: `${HEADER}: Wordmark` },
  { tag: 'aria-label', text: 'Main', source: `${HEADER}: the row's nav` },
  { tag: 'aria-label', text: 'Main', source: `${HEADER}: the menu's nav` },
  { tag: 'aria-label', text: 'Menu', source: `${HEADER}: the menu button` },
  { tag: 'nav', text: 'Docs', source: `${HEADER}: row` },
  { tag: 'nav', text: 'Docs', source: `${HEADER}: menu` },
  { tag: 'nav', text: 'Power calculator', source: `${HEADER}: row` },
  { tag: 'nav', text: 'Power calculator', source: `${HEADER}: menu` },
  { tag: 'nav', text: 'Source', source: `${HEADER}: row`, build: 'marketing' },
  { tag: 'nav', text: 'Source', source: `${HEADER}: menu`, build: 'marketing' },
  { tag: 'nav', text: 'Sign in', source: `${HEADER}: row`, build: 'platform' },
  { tag: 'nav', text: 'Sign in', source: `${HEADER}: menu`, build: 'platform' },

  // The meta description, which the marketing build copies into og:description (QA 5c).
  {
    tag: 'meta',
    text: 'Open-source A/B testing and feature flags you run on your own infrastructure. Experiments, targeting rules, frequentist and Bayesian statistics, and MIT-licensed SDKs for web, mobile and server. Apache-2.0.',
    source: `${MAIN}:63, unchanged`,
  },

  // Hero.
  { tag: 'p', text: 'Open source · Apache-2.0', source: `${UX} Hero pill; ${MAIN}:63` },
  { tag: 'h1', text: 'A/B testing and feature flags you run yourself.', source: `${MAIN}:71` },
  {
    tag: 'p',
    text: 'Controlvar is an open-source experimentation platform: experiments, feature flags, targeting rules and statistics, deployed on your own infrastructure so the assignment data never leaves it.',
    source: `${MAIN}:74-76`,
  },
  { tag: 'div', text: 'Get started', source: `${MAIN}:83` },
  { tag: 'div', text: 'Try the power calculator', source: `${MAIN}:90`, build: 'marketing' },
  { tag: 'div', text: 'Sign in to the dashboard', source: `${MAIN}:97 (index.test.tsx; check-platform-export.mjs)`, build: 'platform' },
  { tag: 'p', text: 'Run it locally', source: `${MAIN}:145, the terminal's title (UX Hero)` },
  { tag: 'aria-label', text: 'Run it locally', source: 'PR G (#1459): the terminal <pre>' },
  {
    tag: 'pre',
    text: 'git clone https://github.com/Controlvar/controlvar.git\ncd controlvar\ndocker compose up -d --wait',
    source: `${MAIN}:158-160, without $ prompts (UX Hero)`,
  },
  {
    tag: 'p',
    text: 'Four containers — Postgres, Redis, the API and the dashboard — with the database created and seeded on the way up.',
    source: `${MAIN}:154-155`,
  },
  {
    tag: 'p',
    text: 'Then open localhost:3000. The quick start has the default credentials and what to change before anyone else can reach it.',
    source: `${MAIN}:163-167`,
  },

  // Facts strip.
  { tag: 'aria-label', text: 'At a glance', source: `${UX} Facts strip` },
  { tag: 'dt', text: 'Apache-2.0, SDKs MIT', source: `${UX} Facts 1; ${MAIN}:175` },
  { tag: 'dd', text: 'Both build profiles are the same licence and the same price: none.', source: `${MAIN}:102` },
  { tag: 'dt', text: 'SDKs for web, mobile and server', source: `${UX} Facts 2; ${MAIN}:63` },
  { tag: 'dd', text: 'Plus OpenFeature providers for JavaScript and Python.', source: `${UX} Facts 2; ${MAIN}:124` },
  { tag: 'dt', text: 'Compose, Helm or AWS CDK', source: `${UX} Facts 3; ${MAIN}:128` },
  { tag: 'dd', text: 'One machine, Kubernetes, or your own AWS account.', source: `${UX} Facts 3; ${MAIN}:128` },
  { tag: 'dt', text: 'No hosted tier', source: `${UX} Facts 4; ${MAIN}:128` },
  { tag: 'dd', text: 'There is nothing to be locked into.', source: `${UX} Facts 4; ${MAIN}:128` },

  // How it works.
  { tag: 'h2', text: 'How it works', source: `${UX} How it works (the issue's own words)` },
  { tag: 'li', text: '1', source: `${UX}: step number, aria-hidden` },
  { tag: 'h3', text: 'Run it', source: `${UX} How it works` },
  {
    tag: 'p',
    text: 'Clone the repository and start everything from its root: the first start builds the images, creates the database and applies the demo seed.',
    source: `${UX}; docs/getting-started/quick-start.md:20-35`,
  },
  { tag: 'aria-label', text: 'Run it', source: 'the step heading, naming its code block' },
  { tag: 'pre', text: 'docker compose up -d --wait', source: `${MAIN}:160; docs/getting-started/quick-start.md` },
  { tag: 'li', text: '2', source: `${UX}: step number, aria-hidden` },
  { tag: 'h3', text: 'Add the SDK', source: `${UX} How it works` },
  {
    tag: 'p',
    text: 'Sticky experiment assignment, then a conversion tracked for that experiment. The JavaScript and Python SDKs are on npm and PyPI as betas.',
    source: `${UX}; docs/sdk/javascript.md:67, :79; ${MAIN}:124`,
  },
  { tag: 'aria-label', text: 'Add the SDK', source: 'the step heading, naming its code block' },
  { tag: 'p', text: 'The whole example in the JavaScript guide →', source: `${UX} How it works` },
  { tag: 'li', text: '3', source: `${UX}: step number, aria-hidden` },
  { tag: 'h3', text: 'Read the result', source: `${UX} How it works` },
  {
    tag: 'p',
    text: 'The change on each metric with its p-value, whether the planned sample is reached, and a sample ratio check of the traffic split.',
    source: `${UX}; docs/guides/user-guide.md:231, :257-266, :298`,
  },

  // Row 1: rollouts and safety.
  { tag: 'h2', text: 'Roll features out gradually, and watch their safety signals', source: `${UX} Row 1; feature-flags/index.tsx:134` },
  {
    tag: 'p',
    text: 'Sticky assignment, gradual rollouts with configurable stages, and automated safety rollback on error-rate and latency thresholds.',
    source: `${MAIN}:112`,
  },
  {
    tag: 'li',
    // #1204: flag targeting refuses json_path, so "JSON path" left this flags
    // row; flags keep 26 of the 28 operators, so "20+" still holds.
    text: 'Targeting with 20+ operators: semantic version comparison, geo-distance, time windows and array operations',
    source: `${MAIN}:116`,
  },
  { tag: 'li', text: 'Switch a flag on and off, and find each change in the audit log', source: `${UX} Row 1; videos.json 03` },
  {
    tag: 'li',
    text: "The JavaScript and Python SDKs can also evaluate flags locally (beta), tested against cases generated by the server's own evaluator.",
    source: `${MAIN}:124`,
  },
  { tag: 'p', text: 'Gradual Rollouts in the docs →', source: `${UX} Row 1; docs/feature-flags/rollouts.md title` },

  // Row 2: statistics.
  { tag: 'h2', text: 'Statistics beyond a p-value', source: `${MAIN}:119 (#1415 finds it by name)` },
  { tag: 'p', text: "Work out a test's sample size in the power calculator, which needs no account.", source: `${UX} Row 2; videos.json 05` },
  { tag: 'li', text: 'Frequentist and Bayesian analysis', source: `${MAIN}:120` },
  { tag: 'li', text: 'Sequential testing with mSPRT for early stopping', source: `${MAIN}:120` },
  { tag: 'li', text: 'Multi-armed bandits', source: `${MAIN}:120` },
  { tag: 'li', text: 'Per-segment breakdowns with a Bonferroni correction across segments', source: `${MAIN}:120` },
  { tag: 'li', text: 'Mutual-exclusion groups and a global holdout', source: `${MAIN}:112` },
  { tag: 'p', text: 'Open the power calculator →', source: `${UX} Row 2` },

  // Row 3: teams and the audit log.
  { tag: 'h2', text: 'Invite teammates, each with a role', source: `${UX} Row 3; videos.json 05` },
  {
    tag: 'p',
    text: 'Admins and developers change experiments and flags; analysts and viewers read them.',
    source: `${UX} Row 3; docs/rbac/README.md:10-13, :78-91`,
  },
  { tag: 'li', text: 'An audit log you can filter by action, entity and date', source: `${UX} Row 3; AuditLogFilter.tsx labels` },
  { tag: 'li', text: 'Download it as CSV or JSON', source: `${UX} Row 3; AuditLogDownload.tsx:109-110` },
  { tag: 'p', text: 'Role-based access control in the docs →', source: `${UX} Row 3; docs/rbac/README.md title` },

  // SDKs (the chips are read from content/sdks.ts).
  { tag: 'h2', text: 'SDKs for the stack you have', source: `${MAIN}:123` },
  { tag: 'p', text: 'All MIT-licensed. By default, flag evaluation and experiment assignment happen on the server.', source: `${MAIN}:124` },
  {
    tag: 'p',
    text: 'The JavaScript SDK is on npm (npm install @controlvar/js-sdk) and the Python SDK is on PyPI (pip install controlvar), both as betas; the others are not on npm or the other package registries yet, and each of their guides shows how to install it from the repository.',
    source: `${MAIN}:124, verbatim`,
  },

  // Walkthroughs: the section's own words (its videos are read from the manifest).
  { tag: 'h2', text: 'Walkthroughs', source: 'Walkthroughs.tsx, unchanged (#1290; walkthroughs.test.tsx)', build: 'marketing' },
  {
    tag: 'p',
    text: 'The recordings were made on a local stack with demo data. Turn captions on in the player; the transcript is below each video.',
    source: 'Walkthroughs.tsx, unchanged (#1290; walkthroughs.test.tsx)',
    build: 'marketing',
  },

  // Deploy.
  { tag: 'h2', text: 'Deploy it yourself', source: `${MAIN}:127 (#1415 finds it by name)` },
  { tag: 'h3', text: 'Docker Compose', source: `${UX} Deploy` },
  { tag: 'p', text: 'Run the published container images on one machine.', source: `${MAIN}:128; ${UX} Deploy` },
  { tag: 'p', text: 'Docker Compose (self-hosting stack) →', source: 'mkdocs.yml nav label of deploy/compose/README.md' },
  { tag: 'h3', text: 'Kubernetes', source: `${UX} Deploy` },
  { tag: 'p', text: 'Install them with the Helm chart attached to each release.', source: `${MAIN}:128; ${UX} Deploy` },
  { tag: 'p', text: 'Self-host on Kubernetes →', source: `${MAIN}:150` },
  { tag: 'h3', text: 'Your AWS account', source: `${UX} Deploy` },
  {
    tag: 'p',
    text: 'Use the CDK app to deploy to ECS Fargate, Aurora PostgreSQL and ElastiCache in your own AWS account.',
    source: `${MAIN}:128`,
  },
  { tag: 'p', text: 'Deploy with CDK →', source: 'the approved mockup, Main.dc.html:275 (the UX names the page, not the words)' },

  // Closing band.
  { tag: 'h2', text: 'Open, and checkable', source: `${MAIN}:131` },
  {
    tag: 'p',
    text: 'The whole platform is Apache-2.0 and the SDKs are MIT. The test suites, the automated checks and the release pipeline are all in the repository, so you can read them rather than take our word for it.',
    source: `${MAIN}:132`,
  },
  { tag: 'div', text: 'Get started', source: `${MAIN}:83; ${UX} Closing band` },
  { tag: 'div', text: 'Source', source: `${UX} Closing band (the header's word)` },

  // Footer.
  { tag: 'div', text: 'CControlvar', source: 'Wordmark.tsx, unlinked' },
  { tag: 'p', text: 'Open-source A/B testing and feature flags you run on your own infrastructure.', source: `${MAIN}:63` },
  { tag: 'aria-label', text: 'Footer', source: `${UX} Footer` },
  { tag: 'p', text: 'Product', source: `${UX} Footer` },
  { tag: 'li', text: 'Quick start', source: `${UX} Footer` },
  { tag: 'li', text: 'Docs', source: `${MAIN}:192` },
  { tag: 'li', text: 'Power calculator', source: `${UX} Footer` },
  { tag: 'li', text: 'Walkthroughs', source: `${UX} Footer`, build: 'marketing' },
  { tag: 'li', text: 'Sign in', source: `${MAIN}:228`, build: 'platform' },
  { tag: 'p', text: 'Project', source: `${UX} Footer` },
  { tag: 'li', text: 'Source', source: `${MAIN}:200` },
  { tag: 'li', text: 'Releases', source: `${UX} Footer` },
  { tag: 'li', text: 'Security', source: `${UX} Footer` },
  { tag: 'li', text: 'Privacy', source: `${MAIN}:224` },
  { tag: 'p', text: 'Contact', source: `${UX} Footer` },
  { tag: 'li', text: 'hello@controlvar.com', source: 'utils/site-mode.ts CONTACT_EMAIL' },
  { tag: 'p', text: 'Apache-2.0. SDKs MIT. Built by Ashish Markanday.', source: `${MAIN}:175-188, the contact moved to its own group` },
];

type Quote = { id: string; parts?: Array<string | { link: string; page: string }>; code?: string[] };
const QUOTES: readonly Quote[] = quotes.quotes;

/** A quote's words as the page renders them. */
export const quoteWords = (id: string): string => {
  const row = QUOTES.find((q) => q.id === id);
  if (!row) throw new Error(`content/home-quotes.json has no quote ${id}`);
  return row.parts ? row.parts.map((p) => (typeof p === 'string' ? p : p.link)).join('') : (row.code ?? []).join('\n');
};

/** Rows read from data, for one build. */
export function dataRows(build: Build): Row[] {
  const rows: Row[] = [
    { tag: 'p', text: quoteWords('revenue-metric'), source: 'content/home-quotes.json revenue-metric' },
    { tag: 'p', text: quoteWords('aws-installer'), source: 'content/home-quotes.json aws-installer' },
    {
      tag: 'pre',
      text: `${SNIPPET_COMMENT}\n${quoteWords('sdk-snippet')}`,
      source: `content/home-quotes.json sdk-snippet, under ${MAIN}:124's install command`,
    },
    ...sdkChips().map((chip) => ({
      tag: 'li',
      text: chip.beta ? `${chip.name} beta` : chip.name,
      source: 'content/sdks.ts SDKS',
    })),
  ];
  if (build === 'marketing') {
    for (const screen of registry.screens) {
      rows.push({ tag: 'alt', text: screen.alt, source: `content/site-screens.json ${screen.id}` });
      if (screen.caption !== null) {
        rows.push({ tag: 'figcaption', text: screen.caption, source: `content/site-screens.json ${screen.id}` });
      }
    }
    rows.push(...videoRows());
  }
  return rows;
}

/** Each walkthrough's rows, from the manifest and its transcript, through the section's own helpers. */
export function videoRows(): Row[] {
  return WALKTHROUGHS.flatMap((video) => {
    const transcript = transcriptFor(video.slug);
    const length = lengthLabel(video.duration_seconds);
    const source = `content/videos.json ${video.slug}`;
    return [
      { tag: 'h3', text: video.title, source },
      { tag: 'p', text: video.description, source },
      { tag: 'p', text: KIND_LABEL[kindOf(transcript)], source: `${source}: its kind` },
      { tag: 'p', text: `${length.shown}${length.spoken}`, source: `${source}: duration_seconds` },
      { tag: 'summary', text: `Transcript: ${video.title}`, source },
      ...transcript.items.map((item) =>
        'on_screen' in item
          ? { tag: 'p', text: onScreenLine(item.on_screen), source: `content/walkthroughs/${video.slug}.json` }
          : 'said' in item
            ? { tag: 'p', text: item.said.join(' '), source: `content/walkthroughs/${video.slug}.json` }
            : { tag: 'pre', text: item.code.join('\n'), source: `content/walkthroughs/${video.slug}.json` },
      ),
    ];
  });
}

/** The inventory of one build: its typed rows and its data rows. */
export function inventory(build: Build): Row[] {
  return [...TYPED.filter((row) => row.build === undefined || row.build === build), ...dataRows(build)];
}

/** Typed rows that repeat a row read from data, as `tag|text`: each should be read from the data instead (EM-3). */
export function typedRepeatsData(): string[] {
  const data = new Set([...dataRows('marketing'), ...dataRows('platform')].map((r) => `${r.tag}|${r.text}`));
  return TYPED.map((r) => `${r.tag}|${r.text}`).filter((key) => data.has(key));
}
