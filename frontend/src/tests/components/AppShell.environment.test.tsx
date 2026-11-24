/**
 * Where the environment switcher sits: in the dashboard's header, in a row
 * of its own under the user menu, on the flag list and a flag's page only,
 * for a signed-in user. Every other page (experiments run in Production
 * only) and the marketing build have none.
 */
import fs from 'fs';
import path from 'path';
import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { AppShell } from '@/components/AppShell';
import { AuthProvider } from '@/contexts/AuthContext';
import { FlagEnvironmentProvider } from '@/contexts/FlagEnvironmentContext';
import { ModulesProvider, __resetModulesCache } from '@/contexts/ModulesContext';
import { CORE_PROFILE } from '@/services/modules';
import { TOKEN_STORAGE_KEY, UserMe } from '@/services/api';
import { isMarketingSite } from '@/utils/site-mode';

jest.mock('@/utils/site-mode', () => ({
  ...jest.requireActual('@/utils/site-mode'),
  isMarketingSite: jest.fn(() => false),
}));

let mockPathname = '/feature-flags';
let mockQuery: Record<string, string> = {};
jest.mock('next/router', () => ({
  useRouter: () => ({
    replace: jest.fn().mockResolvedValue(true),
    push: jest.fn(),
    pathname: mockPathname,
    asPath: mockPathname,
    query: mockQuery,
    isReady: true,
    events: { on: jest.fn(), off: jest.fn() },
  }),
}));

jest.mock('next/link', () => {
  const MockLink = ({ children, href, ...rest }: { children: React.ReactNode; href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  );
  MockLink.displayName = 'MockLink';
  return MockLink;
});

const mockFetch = jest.fn();
global.fetch = mockFetch;

const USER: UserMe = {
  id: 'u-1',
  email: 'dev@example.com',
  username: 'dev',
  full_name: 'Dev Eloper',
  role: 'DEVELOPER',
  is_superuser: false,
  is_active: true,
  auth_provider: 'local',
};

function signIn() {
  localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
  mockFetch.mockResolvedValueOnce({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve(USER),
    text: () => Promise.resolve(JSON.stringify(USER)),
  } as unknown as Response);
}

function renderShell() {
  return render(
    <ModulesProvider initial={CORE_PROFILE}>
      <AuthProvider>
        <FlagEnvironmentProvider>
          <AppShell>
            <div data-testid="page-content">page</div>
          </AppShell>
        </FlagEnvironmentProvider>
      </AuthProvider>
    </ModulesProvider>,
  );
}

beforeEach(() => {
  __resetModulesCache();
  mockFetch.mockReset();
  localStorage.clear();
  mockPathname = '/feature-flags';
  mockQuery = {};
  (isMarketingSite as jest.Mock).mockReturnValue(false);
});

describe('the environment switcher in the header', () => {
  it.each(['/feature-flags', '/feature-flags/[id]'])('is in the header on %s, after the user menu', async (pathname) => {
    mockPathname = pathname;
    signIn();
    renderShell();
    const bar = await screen.findByTestId('environment-bar');
    const header = screen.getByRole('banner');
    expect(header).toContainElement(bar);
    expect(await screen.findByLabelText('Environment')).toHaveValue('production');
    // Its own row, below the row that holds the user menu.
    const userMenu = screen.getByTestId('user-menu');
    expect(userMenu.compareDocumentPosition(bar) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(bar).not.toContainElement(userMenu);
    expect(bar).toHaveAttribute('data-environment', 'production');
  });

  it('makes room for the taller sticky header, so a focused control is not scrolled under it', async () => {
    signIn();
    renderShell();
    // jsdom lays nothing out: the rule, and the element it names.
    const css = fs.readFileSync(path.resolve(__dirname, '../../styles/globals.css'), 'utf8');
    const rule = /html:has\(#([\w-]+)\)\s*\{\s*scroll-padding-top:\s*9rem;\s*\}/.exec(css);
    expect(rule).not.toBeNull();
    expect(await screen.findByTestId('environment-bar')).toHaveAttribute('id', rule![1]);
    expect(screen.getByRole('banner')).toHaveClass('sticky');
  });

  it('follows ?env= in the URL', async () => {
    mockQuery = { env: 'development' };
    signIn();
    renderShell();
    await screen.findByTestId('environment-bar');
    expect(await screen.findByLabelText('Environment')).toHaveValue('development');
    expect(screen.queryByTestId('production-marker')).toBeNull();
  });

  it.each(['/experiments', '/feature-flags/new', '/segments'])('is not on %s', async (pathname) => {
    mockPathname = pathname;
    signIn();
    renderShell();
    await screen.findByTestId('user-menu');
    expect(screen.queryByTestId('environment-bar')).toBeNull();
  });

  it('is not shown to a visitor who is not signed in', async () => {
    mockFetch.mockRejectedValue(new Error('no session'));
    renderShell();
    await waitFor(() => expect(screen.queryByTestId('user-menu-loading')).toBeNull());
    expect(screen.queryByTestId('environment-bar')).toBeNull();
  });

  it('is not on the marketing build', () => {
    (isMarketingSite as jest.Mock).mockReturnValue(true);
    renderShell();
    expect(screen.queryByTestId('environment-bar')).toBeNull();
  });
});
