/**
 * Logging out, then the next sign-in, end to end through the pieces `_app.tsx`
 * puts together: AppShell's "Log out", RequireAuth on the protected page, and
 * the sign-in page (#1006, #1040).
 *
 * The router is a small stand-in that behaves the way Next's does where it
 * matters here: a navigation completes asynchronously, and a later one cancels
 * an earlier one still in flight. With a router that navigated synchronously,
 * the guard would unmount before it saw the signed-out state, and the race
 * these tests pin could never happen.
 */
import React from 'react';
import { useRouter } from 'next/router';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { AppShell } from '@/components/AppShell';
import { RequireAuth } from '@/components/RequireAuth';
import { AuthProvider } from '@/contexts/AuthContext';
import { ModulesProvider, __resetModulesCache } from '@/contexts/ModulesContext';
import LoginPage, { DEFAULT_AFTER_LOGIN } from '@/pages/login';
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
  const pathname = parsed.pathname.startsWith('/feature-flags/') ? '/feature-flags/[id]' : parsed.pathname;
  return { pathname, asPath: `${parsed.pathname}${parsed.search}`, query };
}

let mockRoute: MockRoute = mockRouteFor('/feature-flags/flag-1');
const mockListeners = new Set<() => void>();
let mockNavigation = 0;
const mockEvents = { on: () => undefined, off: () => undefined };

const mockReplace = jest.fn((url: string): Promise<boolean> => {
  const navigation = ++mockNavigation;
  return new Promise((resolve) => {
    setTimeout(() => {
      // A later navigation cancelled this one, as Next does.
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

const previous: UserMe = {
  id: 'u-1',
  email: 'first@demo.com',
  username: 'first',
  full_name: 'First Person',
  role: 'DEVELOPER',
  is_superuser: false,
  is_active: true,
  auth_provider: 'local',
};

const nextPerson: UserMe = { ...previous, id: 'u-2', email: 'second@demo.com', username: 'second' };

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(body === undefined ? '' : JSON.stringify(body)),
  } as unknown as Response;
}

/** What `_app.tsx` renders: the bare sign-in page, or a protected page in the shell. */
function Dashboard() {
  const router = useRouter();
  if (router.pathname === '/login') return <LoginPage />;
  return (
    <AppShell>
      <RequireAuth>
        <div data-testid="flag-page">flag</div>
      </RequireAuth>
    </AppShell>
  );
}

beforeEach(() => {
  __resetModulesCache();
  mockFetch.mockReset();
  mockReplace.mockClear();
  localStorage.clear();
  mockRoute = mockRouteFor('/feature-flags/flag-1');
});

const LOGOUT_ANSWERS: Array<[string, () => Promise<Response>]> = [
  ['answers 204', () => Promise.resolve(jsonResponse(204, undefined))],
  ['never answers', () => new Promise<Response>(() => {})],
];

describe('logging out, then the next sign-in (#1006, #1040)', () => {
  it.each(LOGOUT_ANSWERS)(
    'lands on /login with no next, and the next sign-in opens the default page, when the server %s',
    async (_label, logoutAnswer) => {
      localStorage.setItem(TOKEN_STORAGE_KEY, 'tok-first');
      mockFetch.mockImplementation((url: string) => {
        if (url === '/api/v1/auth/me') return Promise.resolve(jsonResponse(200, previous));
        if (url === '/api/v1/auth/logout') return logoutAnswer();
        if (url === '/api/v1/auth/login') {
          return Promise.resolve(
            jsonResponse(200, {
              access_token: 'tok-second',
              token_type: 'bearer',
              expires_in: 43200,
              user: nextPerson,
            }),
          );
        }
        return Promise.resolve(jsonResponse(404, { detail: 'Not Found' }));
      });

      render(
        <ModulesProvider initial={CORE_PROFILE}>
          <AuthProvider>
            <Dashboard />
          </AuthProvider>
        </ModulesProvider>,
      );
      expect(await screen.findByTestId('flag-page')).toBeInTheDocument();

      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Log out' }));
      });

      // Signed out at once, on the sign-in page, which carries no `next`.
      expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
      expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
      expect(mockRoute.asPath).toBe('/login');
      // Not even for a moment: no navigation carried the signed-out page.
      expect(mockReplace.mock.calls.map(([url]) => url).filter((url) => url.includes('next='))).toEqual([]);

      fireEvent.change(screen.getByLabelText('Email'), { target: { value: nextPerson.email } });
      fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'Second1234!' } });
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
      });

      // The next person starts at the default page, not the previous one's.
      await waitFor(() => expect(mockRoute.asPath).toBe(DEFAULT_AFTER_LOGIN));
      expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('tok-second');
    },
  );
});
