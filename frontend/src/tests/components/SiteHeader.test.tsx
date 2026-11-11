/**
 * The public site's one header (#1458).
 *
 * At phone width the homepage header broke: "Docs" ran into the wordmark and
 * "Power calculator" wrapped. /docs and /power-calculator used the dashboard's
 * header instead, wider and with a border, so the logo moved about 120 px
 * between pages. Now the homepage and, on the marketing build, every page in
 * the shell render `SiteHeader`, and below Tailwind's `md` its links go behind
 * a menu button.
 *
 * jsdom applies no media queries and measures nothing, so this pins the
 * structure: the classes that pick the breakpoint, the button's ARIA, and what
 * the keyboard does. The layout at 390 px was measured in iPhone 13 emulation
 * against the static export (the pull request has the result).
 */
import fs from 'fs';
import path from 'path';
import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AuthProvider } from '@/contexts/AuthContext';
import { SITE_MENU_ID, SOURCE_URL, SiteHeader, siteLinks } from '@/components/SiteHeader';
import { AppShell } from '@/components/AppShell';
import HomePage from '@/pages/index';
import DocsPage from '@/pages/docs/index';
import BooksPage from '@/pages/books';
import WalkthroughsPage from '@/pages/walkthroughs';
import PowerCalculatorPage from '@/pages/power-calculator';
import PrivacyPage from '@/pages/privacy';
import { isMarketingSite } from '@/utils/site-mode';

jest.mock('@/utils/site-mode', () => ({
  ...jest.requireActual('@/utils/site-mode'),
  isMarketingSite: jest.fn(),
}));
jest.mock('next/head', () => {
  const H = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  H.displayName = 'Head';
  return H;
});
let mockPathname = '/docs';
jest.mock('next/router', () => ({
  useRouter: () => ({
    pathname: mockPathname,
    asPath: mockPathname,
    query: {},
    push: jest.fn(),
    replace: jest.fn(),
    isReady: true,
  }),
}));

const mode = isMarketingSite as jest.Mock;
const classesOf = (el: Element) => el.className.split(/\s+/);

beforeEach(() => {
  localStorage.clear();
  mockPathname = '/docs';
  mode.mockReset().mockReturnValue(true);
  global.fetch = jest.fn(() => Promise.reject(new Error('no API'))) as unknown as typeof fetch;
});

/**
 * The links (site design 2026-11-11, item 0): on the marketing build Docs,
 * Books, Walkthroughs and Power calculator, the first two pages and the last
 * its own; on a platform build, which has no /books or /walkthroughs page,
 * Docs and Power calculator. The call to action is outside the menu, at every
 * width (the competitor study's BORROW 7: GitHub's mark and "Source", kept on
 * a phone), or "Sign in" on a platform build.
 */
const NAV: Record<string, Array<[string, string]>> = {
  marketing: [
    ['Docs', '/docs'],
    ['Books', '/books'],
    ['Walkthroughs', '/walkthroughs'],
    ['Power calculator', '/power-calculator'],
  ],
  platform: [
    ['Docs', '/docs'],
    ['Power calculator', '/power-calculator'],
  ],
};

describe.each([
  ['marketing', true, 'Source', SOURCE_URL, 'nav-source'],
  ['platform', false, 'Sign in', '/login', 'nav-sign-in'],
])('SiteHeader on the %s build', (build, marketing, last, lastHref, lastId) => {
  beforeEach(() => mode.mockReturnValue(marketing));

  it('has the wordmark, then its links, then one call to action outside the nav', () => {
    render(<SiteHeader />);
    const nav = screen.getByRole('navigation', { name: 'Main' });
    const links = within(nav).getAllByRole('link');
    expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual(NAV[build]);
    expect(siteLinks(marketing).map((l) => [l.label, l.href])).toEqual(NAV[build]);
    const cta = screen.getByTestId(lastId);
    expect([cta.textContent, cta.getAttribute('href')]).toEqual([last, lastHref]);
    expect(cta).toHaveAccessibleName(last);
    expect(cta.closest('nav')).toBeNull();
    // After the nav, before the menu button, in the same row.
    const row = screen.getByTestId('site-header-row');
    const order = Array.from(row.querySelectorAll('nav[aria-label="Main"] a, [data-testid="nav-source"], [data-testid="nav-sign-in"], button'));
    expect(order.map((el) => el.textContent?.trim() || el.getAttribute('aria-label'))).toEqual([...NAV[build].map(([label]) => label), last, 'Menu']);
    expect(screen.getByRole('link', { name: 'Controlvar home' })).toHaveAttribute('href', '/');
  });

  it('puts the links behind the button below md, at one breakpoint, and keeps the call to action at every width', () => {
    render(<SiteHeader />);
    const nav = screen.getByRole('navigation', { name: 'Main' });
    const button = screen.getByRole('button', { name: 'Menu' });
    const menu = document.getElementById(SITE_MENU_ID) as HTMLElement;
    expect(classesOf(nav)).toEqual(expect.arrayContaining(['hidden', 'md:flex']));
    expect(classesOf(button)).toContain('md:hidden');
    expect(classesOf(menu)).toContain('md:hidden');
    // Each link keeps to one line: "Power calculator" wrapped at 390 px.
    within(nav).getAllByRole('link').forEach((a) => expect(classesOf(a)).toContain('whitespace-nowrap'));
    const cta = screen.getByTestId(lastId);
    expect(classesOf(cta)).toContain('whitespace-nowrap');
    expect(classesOf(cta).filter((c) => /^(hidden|[a-z]+:hidden)$/.test(c))).toEqual([]);
    // A tap target on a phone.
    expect(classesOf(cta)).toContain('min-h-11');
  });

  it('opens and closes the menu from the keyboard, and Escape returns focus to the button', async () => {
    const user = userEvent.setup();
    render(<SiteHeader />);
    const button = screen.getByRole('button', { name: 'Menu' });
    const menu = document.getElementById(SITE_MENU_ID) as HTMLElement;

    // A native button, in the tab order: tab to it from the top of the page.
    expect(button.tagName).toBe('BUTTON');
    expect(button).toHaveAttribute('type', 'button');
    expect(button).toHaveAttribute('aria-controls', SITE_MENU_ID);
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(menu).not.toBeVisible();
    for (let i = 0; i < 10 && document.activeElement !== button; i += 1) await user.tab();
    expect(button).toHaveFocus();

    await user.keyboard('{Enter}');
    expect(button).toHaveAttribute('aria-expanded', 'true');
    expect(menu).toBeVisible();
    // The call to action is not in the menu: it is in the row at every width.
    expect(within(menu).getAllByRole('link').map((a) => [a.textContent, a.getAttribute('href')])).toEqual(NAV[build]);

    await user.keyboard('{Escape}');
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(menu).not.toBeVisible();
    expect(button).toHaveFocus();
  });

  it('closes on Escape from inside the menu too, with focus back on the button', async () => {
    const user = userEvent.setup();
    render(<SiteHeader />);
    const button = screen.getByRole('button', { name: 'Menu' });
    await user.click(button);
    const first = within(document.getElementById(SITE_MENU_ID) as HTMLElement).getAllByRole('link')[0];
    first.focus();
    expect(first).toHaveFocus();

    await user.keyboard('{Escape}');
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(button).toHaveFocus();
  });

  it('closes when a link in the menu is followed', async () => {
    const user = userEvent.setup();
    render(<SiteHeader />);
    const button = screen.getByRole('button', { name: 'Menu' });
    await user.click(button);
    const menu = document.getElementById(SITE_MENU_ID) as HTMLElement;
    const docs = within(menu).getByRole('link', { name: 'Docs' });
    docs.addEventListener('click', (event) => event.preventDefault());
    await user.click(docs);
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(menu).not.toBeVisible();
  });
});

it("shows GitHub's mark beside \"Source\", hides the word only below 360 px, and keeps the name (BORROW 7)", () => {
  mode.mockReturnValue(true);
  render(<SiteHeader />);
  const source = screen.getByTestId('nav-source');
  const mark = source.querySelector('svg') as SVGElement;
  expect(mark).toHaveAttribute('aria-hidden', 'true');
  const word = within(source).getByText('Source');
  expect(classesOf(word)).toEqual(['max-[359px]:sr-only']);
  expect(source).toHaveAccessibleName('Source');
});

it('does not link the wordmark to the page it is on', () => {
  mockPathname = '/';
  render(<SiteHeader />);
  expect(screen.queryByRole('link', { name: 'Controlvar home' })).toBeNull();
  expect(screen.getByText('Controlvar')).toBeInTheDocument();
});

/**
 * The same header, the same row, on every page the marketing site serves, and
 * the dashboard's header on none of them.
 */
describe('one header on the marketing site', () => {
  const PAGES: Array<[string, () => React.ReactElement]> = [
    ['/', () => <HomePage />],
    ['/docs', () => <AppShell><DocsPage /></AppShell>],
    ['/books', () => <AppShell><BooksPage /></AppShell>],
    ['/walkthroughs', () => <AppShell><WalkthroughsPage /></AppShell>],
    ['/power-calculator', () => <AppShell><PowerCalculatorPage /></AppShell>],
    ['/privacy', () => <AppShell><PrivacyPage /></AppShell>],
  ];

  const rowOf = async (pathname: string, make: () => React.ReactElement) => {
    mockPathname = pathname;
    const { container, unmount } = render(<AuthProvider>{make()}</AuthProvider>);
    await waitFor(() => expect(container.querySelector('h1')).toBeInTheDocument());
    const headers = container.querySelectorAll('header');
    expect({ pathname, headers: headers.length }).toEqual({ pathname, headers: 1 });
    expect(headers[0]).toHaveAttribute('data-testid', 'site-header');
    expect(container.querySelector('nav[aria-label="Primary"]')).toBeNull();
    const row = within(headers[0] as HTMLElement).getByTestId('site-header-row');
    const result = { header: (headers[0] as HTMLElement).className, row: row.className, first: row.firstElementChild?.textContent };
    unmount();
    return result;
  };

  it('renders SiteHeader, with one row layout, on all of them', async () => {
    const rows: Array<Awaited<ReturnType<typeof rowOf>>> = [];
    for (const [pathname, make] of PAGES) rows.push(await rowOf(pathname, make));
    expect(rows[0].first).toBe('CControlvar');
    rows.slice(1).forEach((row) => expect(row).toEqual(rows[0]));
  });
});

/**
 * The dashboard's contrast rule (tests/a11y/grey-text-contrast.test.ts): no
 * text lighter than slate-500 on white. jsdom cannot measure contrast, so this
 * reads the source, which must still have slate text for the rule to bite.
 */
it('uses no text lighter than slate-500', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', '..', 'components', 'SiteHeader.tsx'), 'utf8');
  expect(source).toMatch(/\btext-slate-\d00\b/);
  expect(source.match(/\btext-slate-[1-4]00\b/g)).toBeNull();
});
