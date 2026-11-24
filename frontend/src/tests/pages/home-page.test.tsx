/**
 * The homepage as #1459 built it, in both builds:
 *
 *   inventory (QA 1a, 5c; EM-3)  every rendered sentence, alt, aria-label and
 *        title, and the meta description, is a row of `helpers/homeInventory.ts`
 *        with its source, and every row is rendered: an exact multiset, so the
 *        count is the inventory's;
 *   headings (QA 7c)  one h1, no level skipped, and the h2s are the
 *        inventory's, in order;
 *   images (SPEC B; PE 10; C2)  the marketing build's `<img>`s are the
 *        registry's files with its alt, width and height; nothing loads an
 *        image any other way (`srcset`, `<picture>`, a CSS `url(`), which
 *        `check-site-media.mjs` would never see; the platform build has none;
 *   weight (QA 6; C8)  only the hero loads eagerly, and the bytes of every
 *        image that loads with the page are the pinned bytes of the hero,
 *        read from the registry, within a fixed budget: the homepage has no
 *        `<video>` since the site design of 2026-11-11, and the teaser's
 *        posters load lazily. No timings;
 *   links  the SDK chips are `SDK_GUIDES`, `content/site-links.ts` holds
 *        no docs page, and the Controlvar books are linked from their band
 *        and the footer (#1859): the /books page on the marketing build, their
 *        repository on a platform build; the roadmap from the footer;
 *   first screen, and what is specific to Controlvar (#2040)  the hero's links
 *        are an exact list of words and addresses; the section under it has
 *        exactly six items, each with its quotes and its links, which go to
 *        files and pages that exist and that the docs nav lists; axe finds no
 *        violation in either (its layout and colour rules are the site spec's).
 *
 * The flag is mocked, as in `index.site-mode.test.tsx`: SITE_MODE is read at
 * module load.
 */
import fs from 'fs';
import path from 'path';
import React from 'react';
import { render } from '@testing-library/react';
import axe from 'axe-core';
import HomePage from '@/pages/index';
import { AuthProvider } from '@/contexts/AuthContext';
import { isMarketingSite } from '@/utils/site-mode';
import { WALKTHROUGHS, anchorFor } from '@/components/Walkthroughs';
import { SCREENS, screenSrc } from '@/components/homepage/Screenshot';
import { sdkChips } from '@/components/homepage/SdkChips';
import { TEASER } from '@/components/homepage/WalkthroughsTeaser';
import { FOCUS } from '@/components/homepage/styles';
import { CORE_PATH, bookUrl } from '@/content/books';
import { SDK_GUIDES, SDKS } from '@/content/sdks';
import * as SITE_LINKS from '@/content/site-links';
import { docsUrl } from '@/services/docs';
import { navEntry } from '../content/home-quotes-rules';
import { Build, inventory, TYPED, typedRepeatsData } from './helpers/homeInventory';
import { keyOf, renderedItems, surplus } from './helpers/homeText';

jest.mock('@/utils/site-mode', () => ({
  ...jest.requireActual('@/utils/site-mode'),
  isMarketingSite: jest.fn(),
}));
jest.mock('next/head', () => { const H = ({ children }: { children: React.ReactNode }) => <>{children}</>; H.displayName = 'H'; return H; });
jest.mock('next/router', () => ({ useRouter: () => ({ pathname: '/', asPath: '/', query: {}, push: jest.fn(), replace: jest.fn(), isReady: true }) }));

const mode = isMarketingSite as jest.Mock;
const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');

/**
 * Image bytes that may load with the page, before any scrolling: the hero at
 * the screenshot cap (256 KiB). It also held eight posters at
 * check-site-media's cap (64 KiB each) while the homepage had the players,
 * whose posters load with the page whatever `preload` says; the teaser's
 * posters are lazy `<img>`s (site design 2026-11-11).
 */
const EAGER_IMAGE_BUDGET = 256 * 1024;

beforeEach(() => {
  localStorage.clear();
  mode.mockReset();
  global.fetch = jest.fn(() => Promise.reject(new Error('no API'))) as unknown as typeof fetch;
});

const page = (): HTMLElement =>
  render(
    <AuthProvider>
      <HomePage />
    </AuthProvider>,
  ).container;

describe.each<[string, Build]>([
  ['platform build', 'platform'],
  ['marketing build', 'marketing'],
])('the homepage, %s', (_name, build) => {
  beforeEach(() => mode.mockReturnValue(build === 'marketing'));

  it(`renders exactly its inventory (${inventory(build).length} items), and nothing else`, () => {
    const have = renderedItems(page()).map(keyOf);
    const want = inventory(build).map(keyOf);
    expect({ unlisted: surplus(have, want), 'listed, not rendered': surplus(want, have) }).toEqual({
      unlisted: [],
      'listed, not rendered': [],
    });
    expect(have.length).toBe(want.length);
  });

  it('has one h1, skips no heading level, and its h2s are the inventory\'s, in order', () => {
    const headings = Array.from(page().querySelectorAll('h1, h2, h3, h4, h5, h6'));
    const levels = headings.map((h) => Number(h.tagName[1]));
    expect(levels.filter((l) => l === 1)).toHaveLength(1);
    expect(levels[0]).toBe(1);
    const skips = levels.flatMap((l, i) => (i > 0 && l > levels[i - 1] + 1 ? [`h${levels[i - 1]}→h${l}: ${headings[i].textContent}`] : []));
    expect(skips).toEqual([]);
    expect(headings.filter((h) => h.tagName === 'H2').map((h) => h.textContent)).toEqual(
      inventory(build).filter((r) => r.tag === 'h2').map((r) => r.text),
    );
  });

  it('loads no image through srcset, <picture> or a CSS url()', () => {
    const container = page();
    const found = [
      ...Array.from(container.querySelectorAll('[srcset], picture, source')).map((el) => el.outerHTML.slice(0, 80)),
      ...Array.from(container.querySelectorAll('[style]'))
        .filter((el) => /url\(/i.test(el.getAttribute('style') ?? ''))
        .map((el) => el.outerHTML.slice(0, 80)),
    ];
    expect(found).toEqual([]);
  });
});

/**
 * The hero's links (#2040): the two buttons, then the row under them, each by
 * its words and its address. The row's Walkthroughs is the marketing build's
 * only: a platform build has no such page.
 */
describe.each<[string, boolean, Array<[string, string]>]>([
  [
    'platform build',
    false,
    [
      ['Get started', docsUrl('getting-started/quick-start')],
      ['Sign in to the dashboard', '/login'],
      ['Docs', '/docs'],
      ['Source', 'https://github.com/Controlvar/controlvar'],
    ],
  ],
  [
    'marketing build',
    true,
    [
      ['Get started', docsUrl('getting-started/quick-start')],
      ['Try the live demo', 'https://demo.controlvar.com'],
      ['Docs', '/docs'],
      ['Walkthroughs', '/walkthroughs'],
      ['Source', 'https://github.com/Controlvar/controlvar'],
    ],
  ],
])("the homepage's hero links, %s", (_name, marketing, want) => {
  beforeEach(() => mode.mockReturnValue(marketing));

  const links = (): HTMLElement[] => {
    const hero = page().querySelector('h1')?.closest('section') as HTMLElement;
    return Array.from(hero.querySelectorAll('a'));
  };
  const classes = (a: HTMLElement): string[] => a.className.split(/\s+/);

  it('are exactly these, in order', () => {
    expect(links().map((a) => [a.textContent, a.getAttribute('href')])).toEqual(want);
  });

  // On a phone the two buttons were 124 and 221 px wide. Full width below sm, so equal.
  it('has buttons full width below sm and their own width from it', () => {
    for (const a of links().slice(0, 2)) expect(classes(a)).toEqual(expect.arrayContaining(['w-full', 'justify-center', 'sm:w-auto']));
  });

  it('has links 44 px tall that draw a focus outline', () => {
    for (const a of links()) expect(classes(a)).toEqual(expect.arrayContaining(['min-h-11', ...FOCUS.split(' ')]));
  });
});

/**
 * The live demo (D381): on controlvar.com the hero's second button opens the
 * public read-only demo, and the line under it, which also describes the
 * button, says what the demo is. The power calculator that button used to
 * open is still linked from the header (row and menu), the statistics row and
 * the footer. A self-hoster's platform build links no demo.
 */
describe("the homepage's live demo link", () => {
  const where = (a: Element) =>
    a.closest('section')?.querySelector('h1, h2')?.textContent ?? a.closest('nav')?.getAttribute('aria-label');

  it('is the marketing hero\'s second button, described by the line under it', () => {
    mode.mockReturnValue(true);
    expect(SITE_LINKS.DEMO_URL).toBe('https://demo.controlvar.com');
    const container = page();
    const demo = Array.from(container.querySelectorAll('a')).filter((a) => a.getAttribute('href') === SITE_LINKS.DEMO_URL);
    expect(demo.map((a) => ({ in: where(a), text: a.textContent }))).toEqual([
      { in: 'A/B testing and feature flags you run yourself.', text: 'Try the live demo' },
    ]);
    expect(demo[0]).toHaveAccessibleDescription('The demo is read-only, with simulated traffic, and is rebuilt every night.');
  });

  it('leaves the power calculator linked from the header, the statistics row and the footer', () => {
    mode.mockReturnValue(true);
    const links = Array.from(page().querySelectorAll('a')).filter((a) => a.getAttribute('href') === '/power-calculator');
    expect(links.map((a) => ({ in: where(a), text: a.textContent }))).toEqual([
      { in: 'Main', text: 'Power calculator' },
      { in: 'Main', text: 'Power calculator' },
      { in: 'Plan tests and analyse results', text: 'Open the power calculator →' },
      { in: 'Footer', text: 'Power calculator' },
    ]);
  });

  it('is not on a platform build', () => {
    mode.mockReturnValue(false);
    const container = page();
    expect(Array.from(container.querySelectorAll('a')).filter((a) => a.getAttribute('href') === SITE_LINKS.DEMO_URL)).toEqual([]);
    expect(container.textContent).not.toContain('live demo');
  });
});

describe('the homepage, platform build: the rows as text only (C2)', () => {
  beforeEach(() => mode.mockReturnValue(false));

  it('renders no <img> and no /screens/ path', () => {
    const container = page();
    expect(container.querySelectorAll('img')).toHaveLength(0);
    expect(container.innerHTML).not.toContain('/screens/');
  });
});

describe('the homepage, marketing build: the screenshots', () => {
  beforeEach(() => mode.mockReturnValue(true));

  const bytesOf = (url: string): number => {
    const screen = SCREENS.find((s) => screenSrc(s) === url);
    if (screen) return screen.bytes;
    const video = WALKTHROUGHS.find((v) => `/media/${v.poster.name}` === url);
    if (video) return video.poster.bytes;
    throw new Error(`${url} is an image with no pinned size: neither a registered screenshot nor a manifest poster`);
  };

  it('renders each registered screenshot once, with its alt, width and height', () => {
    const imgs = Array.from(page().querySelectorAll('img')).filter((img) => !(img.getAttribute('src') ?? '').startsWith('/media/'));
    expect(imgs.map((img) => img.getAttribute('src'))).toEqual(SCREENS.map(screenSrc));
    for (const img of imgs) {
      const screen = SCREENS.find((s) => screenSrc(s) === img.getAttribute('src'));
      expect({ src: img.getAttribute('src'), alt: img.getAttribute('alt'), width: img.getAttribute('width'), height: img.getAttribute('height') }).toEqual({
        src: img.getAttribute('src'),
        alt: screen?.alt,
        width: String(screen?.width),
        height: String(screen?.height),
      });
      expect(img.closest('a')).toBeNull();
    }
  });

  it('loads only the hero eagerly, and every image below it lazily', () => {
    const container = page();
    const imgs = Array.from(container.querySelectorAll('img'));
    const eager = imgs.filter((img) => img.getAttribute('loading') !== 'lazy');
    expect(eager.map((img) => img.getAttribute('src'))).toEqual([screenSrc(SCREENS[0])]);
    const hero = container.querySelector('h1')?.closest('section');
    expect(hero).toBeTruthy();
    expect(eager[0].closest('section')).toBe(hero);
    expect(imgs.filter((img) => img.getAttribute('loading') === 'lazy').map((img) => img.getAttribute('src'))).toEqual([
      ...SCREENS.slice(1).map(screenSrc),
      ...TEASER.map((v) => `/media/${v.poster.name}`),
    ]);
  });

  it('loads exactly the pinned bytes of the hero with the page, within the budget, and plays nothing', () => {
    const container = page();
    const eagerImages = Array.from(container.querySelectorAll('img'))
      .filter((img) => img.getAttribute('loading') !== 'lazy')
      .map((img) => img.getAttribute('src') ?? '');
    // A <video>'s poster would load with the page whatever `preload` says; the homepage has none.
    expect(container.querySelectorAll('video')).toHaveLength(0);
    const eagerBytes = eagerImages.reduce((n, url) => n + bytesOf(url), 0);
    expect(eagerBytes).toBe(SCREENS[0].bytes);
    expect(eagerBytes).toBeLessThanOrEqual(EAGER_IMAGE_BUDGET);
  });

  it("shows the teaser's posters as images of the manifest's pinned files, each in a link to its row of /walkthroughs", () => {
    const container = page();
    const posters = Array.from(container.querySelectorAll('img')).filter((img) => (img.getAttribute('src') ?? '').startsWith('/media/'));
    expect(posters.map((img) => img.getAttribute('src'))).toEqual(TEASER.map((v) => `/media/${v.poster.name}`));
    expect(TEASER).toEqual(WALKTHROUGHS.slice(0, 3));
    posters.forEach((img, i) => {
      expect({ alt: img.getAttribute('alt'), width: img.getAttribute('width'), height: img.getAttribute('height') }).toEqual({
        alt: '',
        width: '1280',
        height: '720',
      });
      bytesOf(img.getAttribute('src') ?? '');
      const link = img.closest('a');
      expect(link?.getAttribute('href')).toBe(`/walkthroughs#${anchorFor(TEASER[i].slug)}`);
      expect(link).toHaveAccessibleName(new RegExp(`^${TEASER[i].title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')} `));
    });
  });
});

describe('the inventory', () => {
  it('types no row the data gives: those are read from the data (EM-3)', () => {
    expect(typedRepeatsData()).toEqual([]);
  });

  it('gives every typed row a source', () => {
    expect(TYPED.filter((row) => row.source.trim().length < 8).map(keyOf)).toEqual([]);
  });

  it('types no sentence that says beta: it is a row of content/home-quotes.json, held to the capability page', () => {
    expect(TYPED.filter((row) => /\bbeta\b/i.test(row.text)).map(keyOf)).toEqual([]);
  });
});

describe("the homepage's links", () => {
  beforeEach(() => mode.mockReturnValue(true));

  it('links each SDK chip to its guide in SDK_GUIDES, one chip per name', () => {
    const container = page();
    const chips = Array.from(container.querySelectorAll('#sdks-heading ~ ul > li > a'));
    expect(chips.map((a) => [a.textContent, a.getAttribute('href')])).toEqual(
      sdkChips().map((c) => [c.beta ? `${c.name} beta` : c.name, c.href]),
    );
    expect(sdkChips().map((c) => c.name).sort()).toEqual(Array.from(new Set(Object.values(SDKS))).sort());
    for (const chip of sdkChips()) expect(Object.values(SDK_GUIDES)).toContain(chip.href);
  });

  it('keeps no docs page in content/site-links.ts: a docs page goes through docsUrl', () => {
    const urls = Object.values(SITE_LINKS) as string[];
    expect(urls.length).toBeGreaterThanOrEqual(5);
    expect(urls.filter((u) => /\/(blob|tree)\/[^/]+\/docs(\/|$)|docs\.controlvar\.com/.test(u))).toEqual([]);
  });

  /**
   * The books (#1859; site design 2026-11-11): the band links the core path's
   * READMEs and then all the books, and the footer links them too. On the
   * marketing build "all the books" is its /books page, which the header
   * links as well (item 0); a platform build has no /books page, so there the
   * band and the footer go to the books' repository and the header has no
   * Books link.
   */
  it.each<[string, boolean, string]>([
    ['platform build', false, 'https://github.com/amarkanday/controlvar-books'],
    ['marketing build', true, '/books'],
  ])('links the Controlvar books from the header, their band and the footer, and nowhere else (#1859, %s)', (_name, marketing, all) => {
    mode.mockReturnValue(marketing);
    expect(SITE_LINKS.BOOKS_URL).toBe('https://github.com/amarkanday/controlvar-books');
    const container = page();
    const where = (a: Element) => a.closest('section')?.querySelector('h2')?.textContent ?? a.closest('nav')?.getAttribute('aria-label');
    const links = Array.from(container.querySelectorAll('a')).filter((a) => {
      const href = a.getAttribute('href') ?? '';
      return href === all || href.startsWith(SITE_LINKS.BOOKS_URL);
    });
    const header = marketing
      ? [
          { in: 'Main', href: '/books', text: 'Books' },
          { in: 'Main', href: '/books', text: 'Books' },
        ]
      : [];
    expect(links.map((a) => ({ in: where(a), href: a.getAttribute('href'), text: a.textContent }))).toEqual([
      ...header,
      ...CORE_PATH.map((book) => ({ in: 'The Controlvar books', href: bookUrl(book), text: `${book.title}${book.line}` })),
      { in: 'The Controlvar books', href: all, text: 'All the books →' },
      { in: 'Footer', href: all, text: 'The Controlvar books' },
    ]);
    expect(links[header.length + CORE_PATH.length]).toHaveAccessibleName('All the books');
    for (const book of CORE_PATH) expect(bookUrl(book)).toBe(`${SITE_LINKS.BOOKS_URL}/blob/main/${book.dir}/README.md`);
  });

  it('puts the books near the top: right after How it works, before the product rows (site design 2026-11-11, item 4)', () => {
    const h2s = Array.from(page().querySelectorAll('h2')).map((h) => h.textContent);
    expect(h2s.slice(0, 4)).toEqual([
      'Specific to Controlvar',
      'How it works',
      'The Controlvar books',
      'Roll features out gradually, with automatic rollback',
    ]);
  });

  /**
   * What Controlvar does today and the roadmap: the capability page from the
   * line that ends "Specific to Controlvar" and from the footer, and the
   * roadmap from the footer, labelled as the docs hub labels it, in both
   * builds.
   */
  it.each<[string, boolean]>([
    ['platform build', false],
    ['marketing build', true],
  ])('links what Controlvar does under Specific to Controlvar and in the footer, and the roadmap in the footer, and nowhere else (%s)', (_name, marketing) => {
    mode.mockReturnValue(marketing);
    const container = page();
    const where = (a: Element) => a.closest('section')?.querySelector('h2')?.textContent ?? a.closest('nav')?.getAttribute('aria-label');
    const linksTo = (href: string) =>
      Array.from(container.querySelectorAll('a'))
        .filter((a) => a.getAttribute('href') === href)
        .map((a) => ({ in: where(a), text: a.textContent }));
    expect(linksTo(docsUrl('capabilities'))).toEqual([
      { in: 'Specific to Controlvar', text: 'What Controlvar does today' },
      { in: 'Footer', text: 'What Controlvar does' },
    ]);
    expect(linksTo(SITE_LINKS.ROADMAP_URL)).toEqual([{ in: 'Footer', text: 'Roadmap' }]);
  });

  it("links the roadmap as mkdocs.yml's nav does, to ROADMAP.md, and the capability page is a page of that nav (D380)", () => {
    const nav = fs.readFileSync(path.join(REPO_ROOT, 'mkdocs.yml'), 'utf8');
    expect(SITE_LINKS.ROADMAP_URL).toBe(`${SITE_LINKS.REPO_URL}/blob/main/ROADMAP.md`);
    expect(nav).toMatch(new RegExp(`^\\s+- Roadmap: ${SITE_LINKS.ROADMAP_URL.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&')}\\s*$`, 'm'));
    expect(fs.existsSync(path.join(REPO_ROOT, 'ROADMAP.md'))).toBe(true);
    expect(nav).toMatch(/^\s+- What Controlvar Does: capabilities\.md\s*$/m);
    expect(fs.existsSync(path.join(REPO_ROOT, 'docs', 'capabilities.md'))).toBe(true);
  });
});

/**
 * Specific to Controlvar (#2040): six items under the hero, in both builds.
 *
 * The list below is typed, not read from the component or from
 * `content/home-quotes.json`: an item deleted from the page, its quotes and
 * the inventory together would pass the inventory, and fails the count here.
 * Each item has its quotes, by id (`tests/content/home-quotes.test.tsx` holds
 * their words), and its links, each outside the quote it follows.
 */
const SPECIFIC: Array<{ heading: string; quotes: string[]; links: Array<[string, string]> }> = [
  { heading: 'Apache-2.0, SDKs MIT', quotes: [], links: [['LICENSE', SITE_LINKS.LICENSE_URL]] },
  {
    heading: 'Flags that roll themselves back',
    quotes: ['specific-rollback'],
    links: [['Safety Monitoring →', docsUrl('feature-flags/safety')]],
  },
  {
    heading: 'Corrected for several variants',
    quotes: ['specific-correction'],
    links: [['How results are judged →', docsUrl('api/endpoints', 'how-results-are-judged')]],
  },
  {
    heading: 'Sequential testing',
    quotes: ['specific-sequential'],
    links: [['Sequential Testing & Early Stopping →', docsUrl('api/sequential-testing')]],
  },
  {
    heading: 'Compose, Helm or AWS CDK',
    quotes: ['specific-compose', 'specific-kubernetes', 'specific-cdk'],
    links: [
      ['Docker Compose', SITE_LINKS.COMPOSE_GUIDE_URL],
      ['Kubernetes', docsUrl('self-hosting/kubernetes')],
      ['AWS CDK', docsUrl('self-hosting/cdk')],
    ],
  },
  { heading: 'Tested statistics', quotes: [], links: [['Tests', SITE_LINKS.STATS_TESTS_URL]] },
];

describe.each<[string, boolean]>([
  ['platform build', false],
  ['marketing build', true],
])('Specific to Controlvar, %s', (_name, marketing) => {
  beforeEach(() => mode.mockReturnValue(marketing));

  const section = (container: HTMLElement): HTMLElement =>
    container.querySelector('section[aria-labelledby="specifics-heading"]') as HTMLElement;
  const wordsAndAddress = (a: Element): [string, string] => [a.textContent ?? '', a.getAttribute('href') ?? ''];

  it('is exactly six items, each with its heading, its quotes and its links', () => {
    const items = Array.from(section(page()).querySelectorAll('ul > li'));
    expect(items).toHaveLength(6);
    expect(
      items.map((li) => ({
        heading: Array.from(li.querySelectorAll('h3')).map((h) => h.textContent).join(' | '),
        quotes: Array.from(li.querySelectorAll('[data-quote]')).map((q) => q.getAttribute('data-quote')),
        links: Array.from(li.querySelectorAll('a')).map(wordsAndAddress),
      })),
    ).toEqual(SPECIFIC);
    for (const li of items) expect(li.querySelectorAll('[data-quote] a')).toHaveLength(0);
  });

  it('ends with the line that links what Controlvar does not do, and what it does', () => {
    const line = section(page()).querySelector('ul + p') as HTMLElement;
    expect(line.textContent).toBe('What Controlvar does not do is in Limitations; its main capabilities are in What Controlvar does today.');
    expect(Array.from(line.querySelectorAll('a')).map(wordsAndAddress)).toEqual([
      ['Limitations', docsUrl('getting-started/limitations')],
      ['What Controlvar does today', docsUrl('capabilities')],
    ]);
  });

  /** `docs-links.test.ts` checks that a docs page exists, and never that the docs nav lists it. */
  it('links docs pages that exist and that the nav of mkdocs.yml lists', () => {
    const nav = fs.readFileSync(path.join(REPO_ROOT, 'mkdocs.yml'), 'utf8');
    const prefix = `${SITE_LINKS.REPO_URL}/blob/main/docs/`;
    const pages = Array.from(section(page()).querySelectorAll('a'))
      .map((a) => a.getAttribute('href') ?? '')
      .filter((href) => href.startsWith(prefix))
      .map((href) => href.slice(prefix.length).replace(/\.md(#.*)?$/, ''));
    // Three items, two of the deployment item's three guides, and the closing line's two.
    expect(pages).toHaveLength(7);
    const dead = pages.filter((p) => !fs.existsSync(path.join(REPO_ROOT, 'docs', `${p}.md`)));
    const notInNav = pages.filter((p) => !navEntry(p).test(nav));
    expect({ dead, notInNav }).toEqual({ dead: [], notInNav: [] });
  });

  /**
   * The Compose guide is mkdocs.yml's own entry for the stack's README; the
   * licence and the statistics tests are a file and a folder of this
   * repository.
   */
  it('links files and folders of this repository that exist', () => {
    const nav = fs.readFileSync(path.join(REPO_ROOT, 'mkdocs.yml'), 'utf8');
    expect(nav).toMatch(new RegExp(`^\\s+- Docker Compose \\(self-hosting stack\\): ${SITE_LINKS.COMPOSE_GUIDE_URL.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&')}\\s*$`, 'm'));
    for (const [url, file] of [
      [SITE_LINKS.COMPOSE_GUIDE_URL, 'deploy/compose/README.md'],
      [SITE_LINKS.LICENSE_URL, 'LICENSE'],
      [SITE_LINKS.STATS_TESTS_URL, 'backend/tests/unit/stats_validation'],
    ]) {
      expect(url).toBe(`${SITE_LINKS.REPO_URL}/${fs.statSync(path.join(REPO_ROOT, file)).isDirectory() ? 'tree' : 'blob'}/main/${file}`);
    }
  });

  it('draws a focus outline on every link', () => {
    const links = Array.from(section(page()).querySelectorAll('a'));
    expect(links.length).toBeGreaterThanOrEqual(6);
    for (const a of links) expect(a.className.split(/\s+/)).toEqual(expect.arrayContaining(FOCUS.split(' ')));
  });

  // jsdom has no layout and no colour: those rules are the site spec's (Playwright, over the marketing export).
  it.each<[string, (container: HTMLElement) => HTMLElement]>([
    ['the hero', (container) => container.querySelector('h1')?.closest('section') as HTMLElement],
    ['this section', section],
  ])('has no axe violation in %s', async (_where, part) => {
    const result = await axe.run(part(page()), { rules: { 'color-contrast': { enabled: false } } });
    expect(result.passes.length).toBeGreaterThan(0);
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(' | ')}`)).toEqual([]);
  });
});
