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
 *   - the walkthroughs teaser's cards: posters, titles and lengths, from
 *     `content/videos.json` through the very helpers the teaser renders with
 *     (the /walkthroughs page's rows, every title, description, kind, length
 *     and transcript, are `walkthroughsInventory`);
 *   - the books band's cards, from `content/books.ts`;
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
import { TEASER } from '@/components/homepage/WalkthroughsTeaser';
import { CORE_PATH } from '@/content/books';

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
const HEADER = 'SiteHeader.tsx (#1458)';
const BOOKS = "the Controlvar books' README (amarkanday/controlvar-books, #1859)";
/** The site design of 2026-11-11 (D376, D377): launch-readiness/site-design-2026-11-11/design-review-v1.md. */
const SD = 'site design 2026-11-11 (design-review-v1.md)';
/** Its competitor study's BORROW list, of which items 1 and 7 were taken (competitors-v1.md). */
const BORROW = 'site design 2026-11-11, competitors-v1.md BORROW';
/** D380: the capability page and the roadmap, linked from the facts strip and the footer (and the docs hub). */
const LINKS = 'D380, what Controlvar does and the roadmap';
/** The public read-only demo (D381): the hosted demo plan's UX draft (plan-hosted-demo/drafts/v1-ux-designer.md, WORDS: Site). */
const DEMO = 'D381, the hosted demo plan (v1-ux-designer.md, WORDS: Site)';

/** The JavaScript snippet's first line: the install command of main:index.tsx:124, as a comment (UX, How it works). */
export const SNIPPET_COMMENT = '// npm install @controlvar/js-sdk';

export const TYPED: readonly Row[] = [
  // The header: the row and the phone menu (hidden until opened, still in the DOM). Books and
  // Walkthroughs are the marketing build's pages (SD item 0); the call to action sits outside
  // the menu at every width (BORROW 7), so it is a link of the row's div, not of a nav.
  { tag: 'div', text: 'CControlvar', source: `${HEADER}: Wordmark` },
  { tag: 'aria-label', text: 'Main', source: `${HEADER}: the row's nav` },
  { tag: 'aria-label', text: 'Main', source: `${HEADER}: the menu's nav` },
  { tag: 'aria-label', text: 'Menu', source: `${HEADER}: the menu button` },
  { tag: 'nav', text: 'Docs', source: `${HEADER}: row` },
  { tag: 'nav', text: 'Docs', source: `${HEADER}: menu` },
  { tag: 'nav', text: 'Books', source: `${SD} item 0: row`, build: 'marketing' },
  { tag: 'nav', text: 'Books', source: `${SD} item 0: menu`, build: 'marketing' },
  { tag: 'nav', text: 'Walkthroughs', source: `${SD} item 0: row`, build: 'marketing' },
  { tag: 'nav', text: 'Walkthroughs', source: `${SD} item 0: menu`, build: 'marketing' },
  { tag: 'nav', text: 'Power calculator', source: `${HEADER}: row` },
  { tag: 'nav', text: 'Power calculator', source: `${HEADER}: menu` },
  { tag: 'div', text: 'Source', source: `${BORROW} 7: GitHub's mark and "Source", kept on a phone`, build: 'marketing' },
  { tag: 'div', text: 'Sign in', source: `${HEADER}: the platform's call to action, outside the menu`, build: 'platform' },

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
  // The marketing build's second button was "Try the power calculator" (main:90); the calculator is
  // still in the header, row 2 and the footer.
  { tag: 'div', text: 'Try the live demo', source: `${DEMO}: the hero's second button, to DEMO_URL`, build: 'marketing' },
  {
    tag: 'p',
    text: 'The demo is read-only, with simulated traffic, and is rebuilt every night.',
    source: `${DEMO}: the line under the button`,
    build: 'marketing',
  },
  { tag: 'div', text: 'Sign in to the dashboard', source: `${MAIN}:97 (index.test.tsx; check-platform-export.mjs)`, build: 'platform' },
  // Hero B (SD): the "Run it locally" box, its commands and its two lines moved to step 1.

  // Facts strip: four checkable facts, each linked (SD item 2; BORROW 1). The deploy fact took in
  // the "Deploy it yourself" cards (SD item 8) and "No hosted tier"; the SDK fact is the SDK chips'.
  { tag: 'aria-label', text: 'At a glance', source: `${UX} Facts strip` },
  { tag: 'dt', text: 'Apache-2.0, SDKs MIT', source: `${UX} Facts 1; ${MAIN}:175` },
  {
    tag: 'dd',
    text: 'The core and full builds are both free and have the same licence. Read the LICENSE.',
    source: `${MAIN}:102; ${BORROW} 1: LICENSE`,
  },
  { tag: 'dt', text: 'Compose, Helm or AWS CDK', source: `${UX} Facts 3; ${MAIN}:128` },
  {
    tag: 'dd',
    text: 'One machine, Kubernetes, or your own AWS account. There is no hosted tier.',
    source: `${UX} Facts 3 and 4; ${MAIN}:128; ${SD} item 2: the three deploy guides`,
  },
  { tag: 'dt', text: 'Tested statistics', source: `${BORROW} 1: the statistics code` },
  {
    tag: 'dd',
    text: 'Tests check the analysis against known answers: the false-positive rate of A/A tests, interval coverage and a planted lift.',
    source: 'backend/tests/unit/stats_validation: test_golden_aa_significance_rate, test_golden_interval_coverage, test_golden_planted_lift',
  },
  { tag: 'dt', text: 'Public releases', source: `${BORROW} 1: Releases/CHANGELOG` },
  {
    tag: 'dd',
    text: 'Versions are published on the Releases page, and the CHANGELOG says what changed in each.',
    source: `${BORROW} 1: Releases/CHANGELOG (release-please, release-please-config.json)`,
  },
  { tag: 'p', text: 'See everything it does →', source: `${LINKS}: the capability page, docs/capabilities.md` },

  // How it works. Step 1 spans the row and holds the hero's commands and its localhost line (SD item 3).
  { tag: 'h2', text: 'How it works', source: `${UX} How it works (the issue's own words)` },
  { tag: 'li', text: '1', source: `${UX}: step number, aria-hidden` },
  { tag: 'h3', text: 'Run it', source: `${UX} How it works` },
  {
    tag: 'p',
    text: 'Clone the repository and start it from the root. It runs four containers: Postgres, Redis, the API and the dashboard. The first start builds the images, creates the database and applies the demo seed.',
    source: `${UX}; ${MAIN}:154-155 (the hero's "Four containers" line, folded in: SD finding 7); docs/getting-started/quick-start.md:20-35`,
  },
  {
    tag: 'p',
    text: 'Then open localhost:3000. The quick start has the default credentials and what to change before anyone else can reach it.',
    source: `${MAIN}:163-167, moved from the hero (SD item 3)`,
  },
  { tag: 'aria-label', text: 'Run it', source: 'the step heading, naming its code block' },
  {
    tag: 'pre',
    text: 'git clone https://github.com/Controlvar/controlvar.git\ncd controlvar\ndocker compose up -d --wait',
    source: `${MAIN}:158-160, without $ prompts, moved from the hero (SD item 3); docs/getting-started/quick-start.md`,
  },
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
  {
    tag: 'p',
    text: 'How to read a results page, in the user guide →',
    source: 'site polish 2026-11-10 (#7): docs/guides/user-guide.md "Reading Results"',
  },

  // The Controlvar books (#1859), moved up (SD item 4): the books' own README's words, and no count
  // of books. The core path's cards are read from content/books.ts.
  { tag: 'h2', text: 'The Controlvar books', source: `${BOOKS}: its title` },
  {
    tag: 'p',
    text: "Engineering and programming books that use Controlvar's own code for their examples. They are for newcomers to a language or framework, engineers who already code, and contributors. The licence is CC BY 4.0.",
    source: `${BOOKS}: its subtitle, "Who these books are for" and "Licence"`,
  },
  { tag: 'p', text: 'All the books →', source: `${SD} item 4: the link to /books (the books repository on a platform build)` },

  // Row 1: rollouts and safety.
  { tag: 'h2', text: 'Roll features out gradually, with automatic rollback', source: `${UX} Row 1; feature-flags/index.tsx:134` },
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
  { tag: 'h2', text: 'Plan tests and analyse results', source: `${MAIN}:119 (#1415 finds it by name)` },
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
  { tag: 'h2', text: 'SDKs for web, mobile and server', source: `${MAIN}:123` },
  { tag: 'p', text: 'All MIT-licensed. By default, flag evaluation and experiment assignment happen on the server.', source: `${MAIN}:124` },
  // The closing paragraph on which SDKs have a release went (SD item 6): the beta badges say it.

  // Walkthroughs: the homepage's teaser (SD item 7); its cards are read from the manifest.
  { tag: 'h2', text: 'Walkthroughs', source: `${SD} item 7: the teaser; Walkthroughs.tsx's heading (#1290)`, build: 'marketing' },
  { tag: 'p', text: 'All the walkthroughs →', source: `${SD} item 7: the link to /walkthroughs, with no count`, build: 'marketing' },

  // The "Deploy it yourself" cards are folded into the facts strip (SD item 8).

  // Closing band.
  { tag: 'h2', text: 'Open source', source: `${MAIN}:131` },
  {
    tag: 'p',
    text: 'The whole platform is Apache-2.0 and the SDKs are MIT. The test suites, the automated checks and the release pipeline are all in the repository.',
    source: `${MAIN}:132`,
  },
  { tag: 'div', text: 'Get started', source: `${MAIN}:83; ${UX} Closing band` },
  { tag: 'div', text: 'Source', source: `${UX} Closing band (the header's word)` },

  // Footer.
  { tag: 'div', text: 'CControlvar', source: 'Wordmark.tsx, unlinked' },
  { tag: 'p', text: 'Open-source A/B testing and feature flags you run on your own infrastructure.', source: `${MAIN}:63` },
  { tag: 'aria-label', text: 'Footer', source: `${UX} Footer` },
  { tag: 'p', text: 'Product', source: `${UX} Footer` },
  { tag: 'li', text: 'What Controlvar does', source: `${LINKS}: docs/capabilities.md, the docs hub's label` },
  { tag: 'li', text: 'Quick start', source: `${UX} Footer` },
  { tag: 'li', text: 'Docs', source: `${MAIN}:192` },
  { tag: 'li', text: 'The Controlvar books', source: `#1859; ${SD} item 10: /books, or the books repository on a platform build` },
  { tag: 'li', text: 'Power calculator', source: `${UX} Footer` },
  { tag: 'li', text: 'Walkthroughs', source: `${UX} Footer; ${SD} item 10: /walkthroughs`, build: 'marketing' },
  { tag: 'li', text: 'Sign in', source: `${MAIN}:228`, build: 'platform' },
  { tag: 'p', text: 'Project', source: `${UX} Footer` },
  { tag: 'li', text: 'Source', source: `${MAIN}:200` },
  { tag: 'li', text: 'Releases', source: `${UX} Footer` },
  { tag: 'li', text: 'Roadmap', source: `${LINKS}: ROADMAP.md, the docs hub's label` },
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
    // The books band's cards: the core path (SD item 4).
    ...CORE_PATH.flatMap((book) => [
      { tag: 'h3', text: book.title, source: `content/books.ts ${book.dir}` },
      { tag: 'p', text: book.line, source: `content/books.ts ${book.dir}` },
    ]),
  ];
  if (build === 'marketing') {
    for (const screen of registry.screens) {
      rows.push({ tag: 'alt', text: screen.alt, source: `content/site-screens.json ${screen.id}` });
      if (screen.caption !== null) {
        rows.push({ tag: 'figcaption', text: screen.caption, source: `content/site-screens.json ${screen.id}` });
      }
    }
    rows.push(...teaserRows());
  }
  return rows;
}

/**
 * The homepage teaser's cards (SD item 7): each poster (`alt=""`: the title in
 * the same link names it), title and length, from the manifest, through the
 * teaser's own helpers.
 */
export function teaserRows(): Row[] {
  return TEASER.flatMap((video) => {
    const length = lengthLabel(video.duration_seconds);
    const source = `content/videos.json ${video.slug}, a teaser card`;
    return [
      { tag: 'alt', text: '', source: `${source}: its poster, named by its title` },
      { tag: 'h3', text: video.title, source },
      { tag: 'p', text: `${length.shown}${length.spoken}`, source: `${source}: duration_seconds` },
    ];
  });
}

/** Each walkthrough's rows on the /walkthroughs page, from the manifest and its transcript, through the page's own helpers. */
export function videoRows(): Row[] {
  return WALKTHROUGHS.flatMap((video) => {
    const transcript = transcriptFor(video.slug);
    const length = lengthLabel(video.duration_seconds);
    const source = `content/videos.json ${video.slug}`;
    return [
      { tag: 'h2', text: video.title, source },
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

/**
 * The /walkthroughs page's body (`Walkthroughs`), which the homepage carried
 * until the site design of 2026-11-11: its own words, unchanged since #1290,
 * and every video's rows (`videoRows`). `walkthroughs.test.tsx` renders the
 * section and holds it to this, both ways.
 */
export function walkthroughsInventory(): Row[] {
  return [
    {
      tag: 'meta',
      text: 'Videos of Controlvar at work, from getting started to a flag rollout, each with captions and a transcript.',
      source: `pages/walkthroughs.tsx (${SD}): the first and the third video's subjects`,
    },
    { tag: 'h1', text: 'Walkthroughs', source: `Walkthroughs.tsx (#1290), the page's h1 since ${SD}` },
    {
      tag: 'p',
      text: 'The recordings were made on a local stack with demo data. Turn captions on in the player; the transcript is below each video.',
      source: 'Walkthroughs.tsx, unchanged (#1290; walkthroughs.test.tsx)',
    },
    ...videoRows(),
  ];
}

/** Typed rows that repeat a row read from data, as `tag|text`: each should be read from the data instead (EM-3). */
export function typedRepeatsData(): string[] {
  const data = new Set([...dataRows('marketing'), ...dataRows('platform')].map((r) => `${r.tag}|${r.text}`));
  return TYPED.map((r) => `${r.tag}|${r.text}`).filter((key) => data.has(key));
}
