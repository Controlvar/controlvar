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
 *        no docs page, the facts strip links files that exist, and the
 *        Controlvar books are linked from their band and the footer (#1859):
 *        the /books page on the marketing build, their repository on a
 *        platform build.
 *
 * The flag is mocked, as in `index.site-mode.test.tsx`: SITE_MODE is read at
 * module load.
 */
import fs from 'fs';
import path from 'path';
import React from 'react';
import { render } from '@testing-library/react';
import HomePage from '@/pages/index';
import { AuthProvider } from '@/contexts/AuthContext';
import { isMarketingSite } from '@/utils/site-mode';
import { WALKTHROUGHS, anchorFor } from '@/components/Walkthroughs';
import { SCREENS, screenSrc } from '@/components/homepage/Screenshot';
import { sdkChips } from '@/components/homepage/SdkChips';
import { TEASER } from '@/components/homepage/WalkthroughsTeaser';
import { CORE_PATH, bookUrl } from '@/content/books';
import { SDK_GUIDES, SDKS } from '@/content/sdks';
import * as SITE_LINKS from '@/content/site-links';
import { docsUrl } from '@/services/docs';
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

// F12 (review r1): on a phone the hero's two buttons were 124 and 221 px wide. Full width below sm, so equal.
describe.each<[string, boolean]>([
  ['platform build', false],
  ['marketing build', true],
])("the homepage's hero buttons, %s", (_name, marketing) => {
  beforeEach(() => mode.mockReturnValue(marketing));

  it('are full width below sm and their own width from it', () => {
    const hero = page().querySelector('h1')?.closest('section') as HTMLElement;
    const buttons = Array.from(hero.querySelectorAll('a'));
    expect(buttons.map((a) => a.textContent)).toEqual(['Get started', marketing ? 'Try the power calculator' : 'Sign in to the dashboard']);
    for (const a of buttons) expect(a.className.split(/\s+/)).toEqual(expect.arrayContaining(['w-full', 'justify-center', 'sm:w-auto']));
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
    expect(h2s.slice(0, 3)).toEqual(['How it works', 'The Controlvar books', 'Roll features out gradually, and watch their safety signals']);
  });

  /**
   * The facts strip (site design 2026-11-11, item 2; BORROW 1): each fact
   * links what checks it. The Compose guide is mkdocs.yml's own entry for the
   * stack's README; the licence, the changelog and the statistics tests are
   * files and a folder of this repository.
   */
  it("links the deploy fact's Compose guide to mkdocs.yml's own entry for the stack's README, which exists", () => {
    const nav = fs.readFileSync(path.join(REPO_ROOT, 'mkdocs.yml'), 'utf8');
    expect(nav).toMatch(new RegExp(`^\\s+- Docker Compose \\(self-hosting stack\\): ${SITE_LINKS.COMPOSE_GUIDE_URL.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&')}\\s*$`, 'm'));
    const file = /\/blob\/main\/(.+)$/.exec(SITE_LINKS.COMPOSE_GUIDE_URL)?.[1] ?? '';
    expect(file).toBe('deploy/compose/README.md');
    expect(fs.existsSync(path.join(REPO_ROOT, file))).toBe(true);
    const links = Array.from(page().querySelectorAll('a')).filter((a) => a.getAttribute('href') === SITE_LINKS.COMPOSE_GUIDE_URL);
    expect(links.map((a) => [a.textContent, a.closest('section')?.getAttribute('aria-label')])).toEqual([['One machine', 'At a glance']]);
  });

  it.each<[string, string, string]>([
    ['LICENSE_URL', 'LICENSE', 'LICENSE'],
    ['CHANGELOG_URL', 'CHANGELOG.md', 'CHANGELOG'],
    ['STATS_TESTS_URL', 'backend/tests/unit/stats_validation', 'Tests'],
    ['RELEASES_URL', '', 'Releases'],
  ])('links the facts strip to %s, which exists, as "%s"', (name, file, text) => {
    const url = (SITE_LINKS as Record<string, string>)[name];
    if (file) {
      expect(url).toBe(`${SITE_LINKS.REPO_URL}/${fs.statSync(path.join(REPO_ROOT, file)).isDirectory() ? 'tree' : 'blob'}/main/${file}`);
    }
    const strip = page().querySelector('section[aria-label="At a glance"]') as HTMLElement;
    const links = Array.from(strip.querySelectorAll('a')).filter((a) => a.getAttribute('href') === url);
    expect(links.map((a) => a.textContent)).toEqual([text]);
  });

  it('gives every fact of the strip a link, and the deploy fact its three guides', () => {
    const strip = page().querySelector('section[aria-label="At a glance"]') as HTMLElement;
    const facts = Array.from(strip.querySelectorAll('dd'));
    expect(facts).toHaveLength(4);
    for (const dd of facts) expect(dd.querySelectorAll('a').length).toBeGreaterThan(0);
    expect(Array.from(facts[1].querySelectorAll('a')).map((a) => a.getAttribute('href'))).toEqual([
      SITE_LINKS.COMPOSE_GUIDE_URL,
      docsUrl('self-hosting/kubernetes'),
      docsUrl('self-hosting/cdk'),
    ]);
  });
});
