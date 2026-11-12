/**
 * `/login` and `/` with demo mode off and on, through the real
 * `AuthConfigProvider` reading a mocked `GET /api/v1/auth/config`.
 *
 * On a read-only demo, `/login` is one "Enter the demo" button that signs in
 * with the account `/auth/config` publishes, with no password field; `/` goes
 * to `/login` once `/auth/config` has answered (the homepage shows until then,
 * as on every install).
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import LoginPage from '@/pages/login';
import HomePage from '@/pages/index';
import { AuthProvider } from '@/contexts/AuthContext';
import { AuthConfigProvider, __resetAuthConfigCache } from '@/contexts/AuthConfigContext';
import { TOKEN_STORAGE_KEY, UserMe } from '@/services/api';
import {
  DEMO_ENTER_FAILED,
  DEMO_LOGIN_HEADING,
  DEMO_LOGIN_INTRO,
  DEMO_LOGIN_SMALL_PRINT,
  DEMO_UNREACHABLE,
} from '@/utils/demoMode';
import {
  AUTH_CONFIG_PATH,
  BOTH_STATES,
  CONFIG_BODY,
  DemoState,
  FetchRoute,
  TEST_DEMO_ACCOUNT,
  jsonResponse,
  routedFetch,
} from '../helpers/demoMode';

const mockReplace = jest.fn().mockResolvedValue(true);
let mockQuery: Record<string, string | string[]> = {};
let mockPathname = '/login';

jest.mock('next/router', () => ({
  useRouter: () => ({
    replace: mockReplace,
    push: jest.fn(),
    pathname: mockPathname,
    asPath: mockPathname,
    query: mockQuery,
    isReady: true,
  }),
}));

jest.mock('next/head', () => {
  const MockHead = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  MockHead.displayName = 'MockHead';
  return MockHead;
});

const mockFetch = jest.fn();
global.fetch = mockFetch;

const viewer: UserMe = {
  id: 'u-viewer',
  email: TEST_DEMO_ACCOUNT.email,
  username: 'viewer',
  full_name: 'Demo Viewer',
  role: 'VIEWER',
  is_superuser: false,
  is_active: true,
  auth_provider: 'local',
};

const LOGIN_OK: FetchRoute = () =>
  jsonResponse(200, { access_token: 'demo-token', token_type: 'bearer', expires_in: 3600, user: viewer });

function serve(config: Record<string, unknown>, routes: Record<string, FetchRoute> = {}) {
  mockFetch.mockImplementation(routedFetch(config, routes));
}

function renderPage(page: React.ReactElement) {
  return render(
    <AuthConfigProvider>
      <AuthProvider>{page}</AuthProvider>
    </AuthConfigProvider>,
  );
}

/** The sign-in calls the page made, with their bodies. */
function loginCalls(): Array<Record<string, unknown>> {
  return mockFetch.mock.calls
    .filter(([url, init]) => String(url).endsWith('/api/v1/auth/login') && init?.method === 'POST')
    .map(([, init]) => JSON.parse(String(init.body)) as Record<string, unknown>);
}

async function enter() {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Enter the demo' }));
  });
}

beforeEach(() => {
  mockFetch.mockReset();
  mockReplace.mockClear();
  localStorage.clear();
  __resetAuthConfigCache();
  mockQuery = {};
  mockPathname = '/login';
});

describe.each(BOTH_STATES)('/login with demo mode %s', (state: DemoState) => {
  const on = state === 'on';

  it(on ? 'offers one "Enter the demo" button and no password field' : 'offers the email and password form', async () => {
    serve(CONFIG_BODY[state]);
    const { container } = renderPage(<LoginPage />);
    await waitFor(() => expect(screen.queryByTestId('login-loading')).not.toBeInTheDocument());
    if (on) {
      expect(container.querySelector('input[type="password"]')).toBeNull();
      expect(container.querySelector('input')).toBeNull();
      expect(container.querySelector('form')).toBeNull();
      const button = screen.getByRole('button', { name: 'Enter the demo' });
      expect(button.tagName).toBe('BUTTON');
      expect(button).toHaveAttribute('type', 'button');
      // At least 44 px tall (WCAG 2.5.5); jsdom does no layout, so the class is the check.
      expect(button.className).toMatch(/(^|\s)min-h-\[44px\](\s|$)/);
      expect(screen.getAllByRole('button')).toHaveLength(1);
    } else {
      expect(container.querySelector('input[type="password"]')).not.toBeNull();
      expect(screen.getByRole('button', { name: 'Sign in' })).toHaveAttribute('type', 'submit');
      expect(screen.queryByRole('button', { name: 'Enter the demo' })).not.toBeInTheDocument();
      expect(screen.queryByText(DEMO_LOGIN_HEADING)).not.toBeInTheDocument();
    }
  });
});

describe('/login on a read-only demo', () => {
  it('says what the demo is, links the two demo apps and says the README account does not work here', async () => {
    serve(CONFIG_BODY.on);
    renderPage(<LoginPage />);
    expect(await screen.findByRole('heading', { level: 1, name: DEMO_LOGIN_HEADING })).toBeInTheDocument();
    expect(screen.getByTestId('demo-login-intro')).toHaveTextContent(DEMO_LOGIN_INTRO);
    // Without the results stream the numbers do not move by themselves.
    expect(screen.getByTestId('demo-login-intro')).not.toHaveTextContent(/while you watch/i);
    expect(screen.getByRole('link', { name: 'Open ShopLab, the demo storefront' })).toHaveAttribute(
      'href',
      `http://shoplab.${window.location.host}/`,
    );
    expect(screen.getByRole('link', { name: 'Open StreamPulse, the demo phone app' })).toHaveAttribute(
      'href',
      `http://streampulse.${window.location.host}/`,
    );
    expect(screen.getByTestId('demo-login-small-print')).toHaveTextContent(DEMO_LOGIN_SMALL_PRINT);
  });

  it('signs in with the published account and opens the experiments', async () => {
    serve(CONFIG_BODY.on, { 'POST /api/v1/auth/login': LOGIN_OK });
    renderPage(<LoginPage />);
    await screen.findByRole('button', { name: 'Enter the demo' });
    await enter();
    expect(loginCalls()).toEqual([{ email: TEST_DEMO_ACCOUNT.email, password: TEST_DEMO_ACCOUNT.password }]);
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('demo-token');
    expect(mockReplace).toHaveBeenCalledWith('/experiments');
  });

  it('honours ?next=', async () => {
    mockQuery = { next: '/feature-flags?status=active' };
    serve(CONFIG_BODY.on, { 'POST /api/v1/auth/login': LOGIN_OK });
    renderPage(<LoginPage />);
    await screen.findByRole('button', { name: 'Enter the demo' });
    await enter();
    expect(mockReplace).toHaveBeenCalledWith('/feature-flags?status=active');
  });

  it('never follows a ?next= to another site', async () => {
    mockQuery = { next: '//evil.example/steal' };
    serve(CONFIG_BODY.on, { 'POST /api/v1/auth/login': LOGIN_OK });
    renderPage(<LoginPage />);
    await screen.findByRole('button', { name: 'Enter the demo' });
    await enter();
    expect(mockReplace).toHaveBeenCalledWith('/experiments');
    expect(mockReplace).not.toHaveBeenCalledWith('//evil.example/steal');
  });

  it('sends a visitor already signed in straight on', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'old-token');
    mockQuery = { next: '/segments' };
    serve(CONFIG_BODY.on, { 'GET /api/v1/auth/me': () => jsonResponse(200, viewer) });
    renderPage(<LoginPage />);
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/segments'));
    expect(loginCalls()).toEqual([]);
  });

  it('says how long to wait when too many people enter from one network', async () => {
    serve(CONFIG_BODY.on, {
      'POST /api/v1/auth/login': () => jsonResponse(429, { detail: 'Too Many Requests' }, { 'retry-after': '12' }),
    });
    renderPage(<LoginPage />);
    await screen.findByRole('button', { name: 'Enter the demo' });
    await enter();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Too many people are entering from your network. Try again in 12 seconds.');
    expect(alert).toHaveFocus();
    expect(mockReplace).not.toHaveBeenCalled();
    // The button is back, to try again.
    expect(screen.getByRole('button', { name: 'Enter the demo' })).toBeEnabled();
  });

  it('says the demo is not answering when the API cannot be reached', async () => {
    serve(CONFIG_BODY.on, {
      'POST /api/v1/auth/login': () => Promise.reject(new TypeError('Failed to fetch')),
    });
    renderPage(<LoginPage />);
    await screen.findByRole('button', { name: 'Enter the demo' });
    await enter();
    expect(await screen.findByRole('alert')).toHaveTextContent(DEMO_UNREACHABLE);
  });

  it('says the demo is not answering while it is being rebuilt (503)', async () => {
    serve(CONFIG_BODY.on, {
      'POST /api/v1/auth/login': () => jsonResponse(503, { detail: 'The demo is being rebuilt.' }),
    });
    renderPage(<LoginPage />);
    await screen.findByRole('button', { name: 'Enter the demo' });
    await enter();
    expect(await screen.findByRole('alert')).toHaveTextContent(DEMO_UNREACHABLE);
  });

  it('never shows "Email or password is incorrect" for a refused demo account', async () => {
    serve(CONFIG_BODY.on, {
      'POST /api/v1/auth/login': () => jsonResponse(401, { detail: 'Incorrect email or password' }),
    });
    renderPage(<LoginPage />);
    await screen.findByRole('button', { name: 'Enter the demo' });
    await enter();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(DEMO_ENTER_FAILED);
    expect(alert).not.toHaveTextContent(/password/i);
  });

  it('keeps the sign-in form when demo mode publishes no account (people sign in with their own)', async () => {
    serve({ provider: 'local', demo_mode: true });
    const { container } = renderPage(<LoginPage />);
    await screen.findByRole('button', { name: 'Sign in' });
    expect(container.querySelector('input[type="password"]')).not.toBeNull();
    expect(screen.queryByRole('button', { name: 'Enter the demo' })).not.toBeInTheDocument();
  });
});

describe.each(BOTH_STATES)('/ with demo mode %s', (state: DemoState) => {
  const on = state === 'on';

  it(
    on
      ? 'shows the homepage until /auth/config answers, then goes to /login'
      : 'shows the homepage before and after /auth/config answers',
    async () => {
      mockPathname = '/';
      let answer: (r: Response) => void = () => undefined;
      const config = new Promise<Response>((resolve) => {
        answer = resolve;
      });
      mockFetch.mockImplementation((url: string) =>
        String(url).endsWith(AUTH_CONFIG_PATH) ? config : Promise.resolve(jsonResponse(404, {})),
      );
      renderPage(<HomePage />);
      // Before /auth/config answers: the homepage, in both states, as the
      // static export paints it (scripts/check-platform-export.mjs needs it).
      expect(screen.getByTestId('public-home')).toBeInTheDocument();
      expect(screen.getByRole('link', { name: /sign in to the dashboard/i })).toBeInTheDocument();

      await act(async () => {
        answer(jsonResponse(200, CONFIG_BODY[state]));
        await config;
      });

      if (on) {
        await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/login'));
        expect(screen.queryByTestId('public-home')).not.toBeInTheDocument();
      } else {
        expect(screen.getByTestId('public-home')).toBeInTheDocument();
        expect(mockReplace).not.toHaveBeenCalled();
      }
    },
  );
});
