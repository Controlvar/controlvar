/**
 * The password page with the real AuthProvider (#501). A change signs out
 * every session of the account, and the page changes this tab's session only
 * through the AuthContext:
 *
 * - A: an answer without a token (204) says every session has ended; Sign in
 *   signs this tab out through `logout()`, so the guard sends it to /login and
 *   the sign-in page shows its form instead of sending a still-authenticated
 *   tab back.
 * - B: another tab's sign-out clears the stored token while the change is on
 *   its way; the change's new token is stored and the session checked again,
 *   so this tab ends signed in with the new token, not signed out holding it.
 *
 * The router is the small asynchronous stand-in of
 * logout-then-sign-in.test.tsx.
 */
import React from 'react';
import { useRouter } from 'next/router';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { RequireAuth } from '@/components/RequireAuth';
import { AuthProvider, useAuth } from '@/contexts/AuthContext';
import { ModulesProvider, __resetModulesCache } from '@/contexts/ModulesContext';
import LoginPage from '@/pages/login';
import ChangePasswordPage from '@/pages/account/password';
import { CORE_PROFILE } from '@/services/modules';
import { TOKEN_STORAGE_KEY, UserMe } from '@/services/api';

interface MockRoute {
  pathname: string;
  asPath: string;
  query: Record<string, string>;
}

function mockRouteFor(url: string): MockRoute {
  const parsed = new URL(url, 'http://dashboard.test');
  const query: Record<string, string> = {};
  parsed.searchParams.forEach((value, key) => {
    query[key] = value;
  });
  return { pathname: parsed.pathname, asPath: `${parsed.pathname}${parsed.search}`, query };
}

let mockRoute: MockRoute = mockRouteFor('/account/password');
const mockListeners = new Set<() => void>();
let mockNavigation = 0;
const mockEvents = { on: () => undefined, off: () => undefined };

const mockReplace = jest.fn((url: string): Promise<boolean> => {
  const navigation = ++mockNavigation;
  return new Promise((resolve) => {
    setTimeout(() => {
      if (navigation !== mockNavigation) {
        resolve(false);
        return;
      }
      mockRoute = mockRouteFor(url);
      mockListeners.forEach((listener) => listener());
      resolve(true);
    }, 0);
  });
});

jest.mock('next/router', () => {
  const { useSyncExternalStore } = jest.requireActual('react');
  return {
    useRouter: () => {
      const route = useSyncExternalStore(
        (listener: () => void) => {
          mockListeners.add(listener);
          return () => mockListeners.delete(listener);
        },
        () => mockRoute,
      );
      return { ...route, replace: mockReplace, push: mockReplace, isReady: true, events: mockEvents };
    },
  };
});

jest.mock('next/link', () => {
  const MockLink = ({ children, href, ...rest }: { children: React.ReactNode; href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  );
  MockLink.displayName = 'MockLink';
  return MockLink;
});

jest.mock('next/head', () => {
  const MockHead = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  MockHead.displayName = 'MockHead';
  return MockHead;
});

const mockFetch = jest.fn();
global.fetch = mockFetch;

const me: UserMe = {
  id: 'u-501',
  email: 'pw501@demo.com',
  username: 'pw501',
  full_name: 'Password Person',
  role: 'DEVELOPER',
  is_superuser: false,
  is_active: true,
  auth_provider: 'local',
};

const SIGNED_OUT =
  'Your password has been changed, and every session has been signed out, this one too. ' +
  'Sign in again with the new password.';

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(body === undefined ? '' : JSON.stringify(body)),
  } as unknown as Response;
}

/** The session as the provider holds it, for the test to read. */
function Session() {
  const { status } = useAuth();
  return <output data-testid="session-status">{status}</output>;
}

/** What `_app.tsx` renders for these two routes. */
function Dashboard() {
  const router = useRouter();
  if (router.pathname === '/login') return <LoginPage />;
  return (
    <RequireAuth>
      <ChangePasswordPage />
    </RequireAuth>
  );
}

function renderDashboard() {
  return render(
    <ModulesProvider initial={CORE_PROFILE}>
      <AuthProvider>
        <Session />
        <Dashboard />
      </AuthProvider>
    </ModulesProvider>,
  );
}

function fill() {
  fireEvent.change(screen.getByTestId('current-password-input'), { target: { value: 'OldPass12' } });
  fireEvent.change(screen.getByTestId('new-password-input'), { target: { value: 'NewPass123' } });
  fireEvent.change(screen.getByTestId('confirm-password-input'), { target: { value: 'NewPass123' } });
}

function status(): string {
  return screen.getByTestId('session-status').textContent ?? '';
}

beforeEach(() => {
  __resetModulesCache();
  mockFetch.mockReset();
  mockReplace.mockClear();
  localStorage.clear();
  mockRoute = mockRouteFor('/account/password');
});

describe('changing the password with the real AuthProvider (#501)', () => {
  it('A: a 204 says every session has ended, and Sign in opens the sign-in form', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok-before');
    mockFetch.mockImplementation((url: string) => {
      if (url === '/api/v1/auth/me') return Promise.resolve(jsonResponse(200, me));
      if (url === '/api/v1/users/me/password') return Promise.resolve(jsonResponse(204, undefined));
      if (url === '/api/v1/auth/logout') return Promise.resolve(jsonResponse(204, undefined));
      return Promise.resolve(jsonResponse(404, { detail: 'Not Found' }));
    });

    renderDashboard();
    expect(await screen.findByTestId('current-password-input')).toBeInTheDocument();
    fill();
    await act(async () => {
      fireEvent.click(screen.getByTestId('change-password-submit'));
    });

    const box = await screen.findByTestId('change-password-outcome');
    expect(box).toHaveAttribute('data-outcome', 'signed-out');
    expect(box).toHaveAttribute('role', 'status');
    expect(box).toHaveTextContent(SIGNED_OUT);
    // Nothing is cleared until the user acts.
    expect(status()).toBe('authenticated');
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('tok-before');

    await act(async () => {
      fireEvent.click(screen.getByTestId('change-password-sign-in'));
    });

    await waitFor(() => expect(status()).toBe('anonymous'));
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(screen.getByTestId('login-form')).toBeInTheDocument();
    expect(mockRoute.asPath).toBe('/login');
    // The guard's one navigation to /login, and none from the sign-in page
    // sending a still-authenticated tab back.
    expect(mockReplace.mock.calls.map(([url]) => url)).toEqual(['/login']);
  });

  it('B: a sign-out in another tab before the 200 arrives still ends with this tab signed in', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok-before');
    let answer: (response: Response) => void = () => undefined;
    const meCalls: Array<string | null> = [];
    mockFetch.mockImplementation((url: string, init?: RequestInit) => {
      if (url === '/api/v1/auth/me') {
        const headers = (init?.headers ?? {}) as Record<string, string>;
        meCalls.push(headers.Authorization ?? null);
        return Promise.resolve(jsonResponse(200, me));
      }
      if (url === '/api/v1/users/me/password') {
        return new Promise<Response>((resolve) => {
          answer = resolve;
        });
      }
      return Promise.resolve(jsonResponse(404, { detail: 'Not Found' }));
    });

    render(
      <ModulesProvider initial={CORE_PROFILE}>
        <AuthProvider>
          <Session />
          <ChangePasswordPage />
        </AuthProvider>
      </ModulesProvider>,
    );
    expect(await screen.findByTestId('current-password-input')).toBeInTheDocument();
    fill();
    await act(async () => {
      fireEvent.click(screen.getByTestId('change-password-submit'));
    });

    // Another tab signs out: the shared token goes, and this tab follows.
    await act(async () => {
      localStorage.removeItem(TOKEN_STORAGE_KEY);
      window.dispatchEvent(new StorageEvent('storage', { key: TOKEN_STORAGE_KEY, newValue: null }));
    });
    expect(status()).toBe('anonymous');

    await act(async () => {
      answer(
        jsonResponse(200, { access_token: 'tok-after', token_type: 'bearer', expires_in: 43200, user: me }),
      );
    });

    await waitFor(() => expect(status()).toBe('authenticated'));
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('tok-after');
    expect(meCalls[meCalls.length - 1]).toBe('Bearer tok-after');
  });
});
