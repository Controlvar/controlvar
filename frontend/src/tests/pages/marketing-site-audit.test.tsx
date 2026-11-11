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
import PrivacyPage from '@/pages/privacy';
import NotFoundPage from '@/pages/404';
import { AppShell } from '@/components/AppShell';

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
  ['/privacy', () => <AppShell><PrivacyPage /></AppShell>],
  ['/404', () => <AppShell><NotFoundPage /></AppShell>],
];

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

    const SERVED = new Set(['/', '/docs', '/power-calculator', '/privacy', '/favicon.svg']);
    const external = (h: string) => /^[a-z][a-z0-9+.-]*:/i.test(h) || h.startsWith('//');
    // A fragment on a served page is that page (the footer's `/#walkthroughs`).
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
describe.each(PAGES.filter(([name]) => name !== '/'))('%s on a platform build', (name, make) => {
  beforeEach(() => mode.mockReturnValue(false));

  it('has no footer', async () => {
    const { container } = render(<AuthProvider>{make()}</AuthProvider>);
    await waitFor(() => expect(container.querySelector('[data-testid="app-shell"] h1')).toBeInTheDocument());
    expect({ page: name, footers: container.querySelectorAll('footer').length }).toEqual({ page: name, footers: 0 });
    expect(container.querySelector('[data-testid="site-header"]')).toBeNull();
  });
});
