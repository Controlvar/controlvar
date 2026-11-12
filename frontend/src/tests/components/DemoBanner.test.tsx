/**
 * Demo mode in the application shell: the banner shows only when the API
 * reports demo mode, and the user menu then offers neither account page.
 * Every case runs with demo mode off and on, through the real
 * `AuthConfigProvider` reading a mocked `GET /api/v1/auth/config`.
 */
import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { AppShell } from '@/components/AppShell';
import { AuthProvider } from '@/contexts/AuthContext';
import { AuthConfigProvider, __resetAuthConfigCache } from '@/contexts/AuthConfigContext';
import { ModulesProvider } from '@/contexts/ModulesContext';
import { CORE_PROFILE } from '@/services/modules';
import { TOKEN_STORAGE_KEY, UserMe } from '@/services/api';
import {
  DEMO_BANNER_SIGNED_IN,
  DEMO_BANNER_SIGNED_OUT,
  DEMO_RUN_YOUR_OWN_URL,
  DEMO_SOURCE_URL,
} from '@/utils/demoMode';
import { BOTH_STATES, CONFIG_BODY, DemoState, jsonResponse, routedFetch } from '../helpers/demoMode';

jest.mock('next/router', () => ({
  useRouter: () => ({
    replace: jest.fn().mockResolvedValue(true),
    push: jest.fn(),
    pathname: '/experiments',
    asPath: '/experiments',
    query: {},
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

const viewer: UserMe = {
  id: 'u-viewer',
  email: 'viewer@example.test',
  username: 'viewer',
  full_name: 'Demo Viewer',
  role: 'VIEWER',
  is_superuser: false,
  is_active: true,
  auth_provider: 'local',
};

function serve(state: DemoState, signedIn: boolean) {
  if (signedIn) localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
  mockFetch.mockImplementation(
    routedFetch(CONFIG_BODY[state], { 'GET /api/v1/auth/me': () => jsonResponse(200, viewer) }),
  );
}

function renderShell() {
  return render(
    <ModulesProvider initial={CORE_PROFILE}>
      <AuthConfigProvider>
        <AuthProvider>
          <AppShell>
            <div data-testid="page-content">page</div>
          </AppShell>
        </AuthProvider>
      </AuthConfigProvider>
    </ModulesProvider>,
  );
}

beforeEach(() => {
  mockFetch.mockReset();
  localStorage.clear();
  __resetAuthConfigCache();
});

describe.each(BOTH_STATES)('the shell with demo mode %s', (state) => {
  const on = state === 'on';

  it(on ? 'shows the demo banner on the page' : 'shows no demo banner', async () => {
    serve(state, true);
    renderShell();
    // The session check waits for /auth/config, so once the user menu is
    // there the configuration has been read: the banner's absence is settled.
    await screen.findByTestId('user-menu');
    if (on) {
      expect(screen.getByTestId('demo-banner')).toBeInTheDocument();
    } else {
      expect(screen.queryByTestId('demo-banner')).not.toBeInTheDocument();
      expect(screen.queryByText(/read-only demo/i)).not.toBeInTheDocument();
    }
  });

  it(
    on
      ? 'offers neither "Change password" nor "Your API keys" in the user menu'
      : 'offers "Change password" and "Your API keys" in the user menu',
    async () => {
      serve(state, true);
      renderShell();
      await screen.findByTestId('user-menu');
      if (on) {
        expect(screen.queryByTestId('change-password-link')).not.toBeInTheDocument();
        expect(screen.queryByTestId('own-api-keys-link')).not.toBeInTheDocument();
        expect(screen.queryByText('Change password')).not.toBeInTheDocument();
        expect(screen.queryByText('Your API keys')).not.toBeInTheDocument();
      } else {
        expect(screen.getByTestId('change-password-link')).toHaveAttribute('href', '/account/password');
        expect(screen.getByTestId('own-api-keys-link')).toHaveAttribute('href', '/account/api-keys');
      }
    },
  );
});

describe('the demo banner', () => {
  it('is a labelled region, not an alert or a status, and cannot be dismissed', async () => {
    serve('on', true);
    renderShell();
    const banner = await screen.findByRole('region', { name: 'Read-only demo' });
    expect(banner).toHaveAttribute('data-testid', 'demo-banner');
    expect(banner).not.toHaveAttribute('role', 'alert');
    expect(banner).not.toHaveAttribute('role', 'status');
    expect(banner).not.toHaveAttribute('aria-live');
    expect(within(banner).queryByRole('button')).not.toBeInTheDocument();
  });

  it('says what the demo is to a signed-in viewer, with the two links', async () => {
    serve('on', true);
    renderShell();
    await screen.findByTestId('user-menu');
    const banner = screen.getByTestId('demo-banner');
    expect(banner).toHaveTextContent(DEMO_BANNER_SIGNED_IN);
    expect(within(banner).getByRole('link', { name: 'Run your own copy' })).toHaveAttribute(
      'href',
      DEMO_RUN_YOUR_OWN_URL,
    );
    expect(within(banner).getByRole('link', { name: 'Source' })).toHaveAttribute('href', DEMO_SOURCE_URL);
  });

  it('sits before the header, after the skip link', async () => {
    serve('on', true);
    const { container } = renderShell();
    await screen.findByTestId('user-menu');
    const shell = container.querySelector('[data-testid="app-shell"]') as HTMLElement;
    const order = Array.from(shell.children).map(
      (el) => el.getAttribute('data-testid') ?? el.tagName.toLowerCase(),
    );
    expect(order.slice(0, 3)).toEqual(['a', 'demo-banner', 'header']);
  });

  it('does not say "signed in" to a visitor who is not', async () => {
    serve('on', false);
    renderShell();
    await screen.findByTestId('nav-sign-in');
    const banner = await screen.findByTestId('demo-banner');
    expect(banner).toHaveTextContent(DEMO_BANNER_SIGNED_OUT);
    expect(banner).not.toHaveTextContent(/signed in/i);
  });
});
