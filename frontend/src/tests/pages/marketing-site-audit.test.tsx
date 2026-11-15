/**
 * A sweep of every page the marketing site ships, asserting none of them
 * offers something that cannot work there.
 *
 * Written because fixing them one report at a time did not converge: the
 * homepage was fixed, then the shell still showed Sign in on /docs and
 * /power-calculator, then the calculator still had an AI panel calling an
 * endpoint that 404s. Each was found by a person looking at the site.
 *
 * The rule: on the marketing build, NOTHING may link to /login or to a pruned
 * dashboard route, and nothing may call the API.
 */
import React from 'react';
import { render, waitFor } from '@testing-library/react';
import { AuthProvider } from '@/contexts/AuthContext';
import { isMarketingSite, PLATFORM_ONLY_PREFIXES } from '@/utils/site-mode';

jest.mock('@/utils/site-mode', () => ({
  ...jest.requireActual('@/utils/site-mode'),
  isMarketingSite: jest.fn(),
}));
jest.mock('next/head', () => { const H=({children}:{children:React.ReactNode})=><>{children}</>; H.displayName='H'; return H; });
jest.mock('next/router', () => ({ useRouter: () => ({ pathname:'/', asPath:'/', query:{}, push:jest.fn(), replace:jest.fn(), isReady:true }) }));

import HomePage from '@/pages/index';
import PowerCalculatorPage from '@/pages/power-calculator';
import DocsPage from '@/pages/docs/index';
import BooksPage, { getStaticProps as booksProps } from '@/pages/books';
import WalkthroughsPage, { getStaticProps as walkthroughsProps } from '@/pages/walkthroughs';
import PrivacyPage from '@/pages/privacy';
import NotFoundPage from '@/pages/404';
import { AppShell } from '@/components/AppShell';
import { H1, H2 } from '@/components/homepage/styles';
import { ROADMAP_URL } from '@/content/site-links';
import { docsUrl } from '@/services/docs';

const mode = isMarketingSite as jest.Mock;

beforeEach(() => {
  localStorage.clear();
  mode.mockReset().mockReturnValue(true);
  // Every API call fails, as on the published site.
  global.fetch = jest.fn(() => Promise.reject(new Error('no API'))) as unknown as typeof fetch;
});

// The pages the marketing build actually ships, each as it is really wrapped:
// the homepage is `bare`, the others are `open` and so sit in the shell.
const PAGES: Array<[string, () => React.ReactElement]> = [
  ['/', () => <HomePage />],
  ['/power-calculator', () => <AppShell><PowerCalculatorPage /></AppShell>],
  ['/docs', () => <AppShell><DocsPage /></AppShell>],
  // The marketing build's own pages (site design 2026-11-11).
  ['/books', () => <AppShell><BooksPage /></AppShell>],
  ['/walkthroughs', () => <AppShell><WalkthroughsPage /></AppShell>],
  ['/privacy', () => <AppShell><PrivacyPage /></AppShell>],
  ['/404', () => <AppShell><NotFoundPage /></AppShell>],
];

/** Pages a platform build does not have at all (`utils/marketing-only.ts`): no platform-build case for them. */
const MARKETING_ONLY = new Set(['/books', '/walkthroughs']);

describe.each(PAGES)('%s on the marketing site', (name, make) => {
  it('links to nothing that 404s there', async () => {
    const { container } = render(<AuthProvider>{make()}</AuthProvider>);
    await waitFor(() => expect(container.querySelector('a, h1, h2')).toBeInTheDocument());

    const hrefs = Array.from(container.querySelectorAll('a[href]'))
      .map((a) => a.getAttribute('href') ?? '');

    const dead = hrefs.filter((h) =>
      PLATFORM_ONLY_PREFIXES.some((p) => h === `/${p}` || h.startsWith(`/${p}/`) || h.startsWith(`/${p}?`)),
    );
    expect({ page: name, dead }).toEqual({ page: name, dead: [] });
  });

  // The prefix check above knows only the dashboard routes, so it passed a
  // link to a docs path the export never had (T86). This one is an allow-list
  // of what the marketing export serves: any other same-site href is dead.
  it('links on this site only to pages the export serves', async () => {
    const { container } = render(<AuthProvider>{make()}</AuthProvider>);
    await waitFor(() => expect(container.querySelector('a, h1, h2')).toBeInTheDocument());

    const SERVED = new Set(['/', '/docs', '/books', '/walkthroughs', '/power-calculator', '/privacy', '/favicon.svg']);
    const external = (h: string) => /^[a-z][a-z0-9+.-]*:/i.test(h) || h.startsWith('//');
    // A fragment on a served page is that page (the teaser's `/walkthroughs#getting-started`).
    const dead = Array.from(container.querySelectorAll('[href]'))
      .map((a) => a.getAttribute('href') ?? '')
      .filter((h) => !external(h) && !h.startsWith('#') && !h.startsWith('/_next/') && !SERVED.has(h.split('#')[0]));
    expect({ page: name, dead }).toEqual({ page: name, dead: [] });
  });

  // One footer on every page (site polish 2026-11-10, #5): /docs had a dark
  // one of its own with another tagline, /power-calculator and /privacy none.
  // The homepage renders HomeFooter itself; the shell adds it to the rest.
  it("ends with the site's footer, and has no other", async () => {
    const { container } = render(<AuthProvider>{make()}</AuthProvider>);
    await waitFor(() => expect(container.querySelector('a, h1, h2')).toBeInTheDocument());

    const footers = Array.from(container.querySelectorAll('footer')).map(
      (f) => f.querySelector('nav')?.getAttribute('aria-label') ?? '(no footer nav)',
    );
    expect({ page: name, footers }).toEqual({ page: name, footers: ['Footer'] });
    const footer = container.querySelector('footer') as HTMLElement;
    expect({ page: name, inMain: footer.closest('main') !== null }).toEqual({ page: name, inMain: false });
    expect({ page: name, last: footer.parentElement?.lastElementChild === footer }).toEqual({ page: name, last: true });
  });

  // What Controlvar does today and the roadmap (D380): the footer on every page carries both.
  it('links what Controlvar does and the roadmap from its footer', async () => {
    const { container } = render(<AuthProvider>{make()}</AuthProvider>);
    await waitFor(() => expect(container.querySelector('a, h1, h2')).toBeInTheDocument());

    const nav = container.querySelector('footer nav[aria-label="Footer"]') as HTMLElement;
    const links = Array.from(nav.querySelectorAll('a')).map((a) => [a.textContent, a.getAttribute('href')]);
    expect({ page: name, links }).toEqual({
      page: name,
      links: expect.arrayContaining([
        ['What Controlvar does', docsUrl('capabilities')],
        ['Roadmap', ROADMAP_URL],
      ]),
    });
  });

  // One type scale (site design 2026-11-11, finding 9): the h1 was 48/600 on
  // home, 36/700 on /docs, 30/700 on the calculator, 30/600 on /privacy and 404.
  it("sets its one h1, and every section h2 outside a card, in the site's one scale", async () => {
    const { container } = render(<AuthProvider>{make()}</AuthProvider>);
    await waitFor(() => expect(container.querySelector('h1')).toBeInTheDocument());
    const h1s = Array.from(container.querySelectorAll('h1'));
    expect({ page: name, h1s: h1s.length }).toEqual({ page: name, h1s: 1 });
    const missing = H1.split(' ').filter((c) => !h1s[0].className.split(/\s+/).includes(c));
    expect({ page: name, missing }).toEqual({ page: name, missing: [] });
    // A section heading (an h2 set larger than a card title) is the scale's: 24 px, and 30 from sm.
    const sizes = H2.split(' ').filter((c) => /^(sm:)?text-\d?xl$/.test(c));
    expect(sizes).toEqual(['text-2xl', 'sm:text-3xl']);
    const offScale = Array.from(container.querySelectorAll('h2'))
      .filter((h) => /(^|\s)(sm:|lg:)?text-[2-9]xl\b/.test(h.className))
      .filter((h) => !sizes.every((c) => h.className.split(/\s+/).includes(c)) || /(^|\s)lg:text-|(^|\s)text-[3-9]xl\b/.test(h.className))
      .map((h) => `${h.textContent}: ${h.className}`);
    expect({ page: name, offScale }).toEqual({ page: name, offScale: [] });
  });

  it('renders without reaching the API', async () => {
    render(<AuthProvider>{make()}</AuthProvider>);
    await waitFor(() => {
      const calls = (global.fetch as jest.Mock).mock.calls.map((c) => String(c[0]));
      // The auth probe is allowed: it is how the page learns it is anonymous,
      // and it fails closed to that. Nothing else may call the API.
      const other = calls.filter((u) => !u.includes('/auth/me'));
      expect({ page: name, other }).toEqual({ page: name, other: [] });
    });
  });
});

// The footer is the public site's. A self-hoster's dashboard shares AppShell,
// and its pages, the docs hub among them, end with no footer: the old hub's
// own footer is gone from both builds, and the shell adds one only to the
// marketing build.
describe.each(PAGES.filter(([name]) => name !== '/' && !MARKETING_ONLY.has(name)))('%s on a platform build', (name, make) => {
  beforeEach(() => mode.mockReturnValue(false));

  it('has no footer', async () => {
    const { container } = render(<AuthProvider>{make()}</AuthProvider>);
    await waitFor(() => expect(container.querySelector('[data-testid="app-shell"] h1')).toBeInTheDocument());
    expect({ page: name, footers: container.querySelectorAll('footer').length }).toEqual({ page: name, footers: 0 });
    expect(container.querySelector('[data-testid="site-header"]')).toBeNull();
  });
});

// The marketing build's own pages are not in a platform build at all: their
// getStaticProps answers notFound there, so `next build` writes no file
// (scripts/check-platform-export.mjs refuses one in a platform export).
describe.each([
  ['/books', booksProps],
  ['/walkthroughs', walkthroughsProps],
])('%s', (name, getStaticProps) => {
  it('is a page of the marketing build only', () => {
    expect(MARKETING_ONLY.has(name)).toBe(true);
    mode.mockReturnValue(true);
    expect(getStaticProps()).toEqual({ props: {} });
    mode.mockReturnValue(false);
    expect(getStaticProps()).toEqual({ notFound: true });
  });
});
