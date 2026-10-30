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
import { SITE_MENU_ID, SOURCE_URL, SiteHeader } from '@/components/SiteHeader';
import { AppShell } from '@/components/AppShell';
import HomePage from '@/pages/index';
import DocsPage from '@/pages/docs/index';
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

describe.each([
  ['marketing', true, 'Source', SOURCE_URL],
  ['platform', false, 'Sign in', '/login'],
])('SiteHeader on the %s build', (_build, marketing, last, lastHref) => {
  beforeEach(() => mode.mockReturnValue(marketing));

  it('has the wordmark, then Docs, Power calculator and one call to action', () => {
    render(<SiteHeader />);
    const nav = screen.getByRole('navigation', { name: 'Main' });
    const links = within(nav).getAllByRole('link');
    expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
      ['Docs', '/docs'],
      ['Power calculator', '/power-calculator'],
      [last, lastHref],
    ]);
    expect(screen.getByRole('link', { name: 'Controlvar home' })).toHaveAttribute('href', '/');
  });

  it('puts the links behind the button below md, all three at one breakpoint', () => {
    render(<SiteHeader />);
    const nav = screen.getByRole('navigation', { name: 'Main' });
    const button = screen.getByRole('button', { name: 'Menu' });
    const menu = document.getElementById(SITE_MENU_ID) as HTMLElement;
    expect(classesOf(nav)).toEqual(expect.arrayContaining(['hidden', 'md:flex']));
    expect(classesOf(button)).toContain('md:hidden');
    expect(classesOf(menu)).toContain('md:hidden');
    // Each link keeps to one line: "Power calculator" wrapped at 390 px.
    within(nav).getAllByRole('link').forEach((a) => expect(classesOf(a)).toContain('whitespace-nowrap'));
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
    expect(within(menu).getAllByRole('link').map((a) => a.textContent)).toEqual(['Docs', 'Power calculator', last]);

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

  it('renders SiteHeader, with one row layout, on all four', async () => {
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
