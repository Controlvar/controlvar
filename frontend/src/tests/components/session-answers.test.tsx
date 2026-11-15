/**
 * Answers that arrive late, or cannot be read, through the pieces `_app.tsx`
 * puts together: AppShell's "Log out", RequireAuth on the protected page, and
 * the sign-in page (#1579, #1039). The router is the same small stand-in as in
 * logout-then-sign-in.test.tsx: a navigation completes asynchronously, and a
 * later one cancels an earlier one still in flight.
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

const ME = '/api/v1/auth/me';
const LOGIN = '/api/v1/auth/login';
const LOGOUT = '/api/v1/auth/logout';

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

function renderDashboard(): void {
  render(
    <ModulesProvider initial={CORE_PROFILE}>
      <AuthProvider>
        <Dashboard />
      </AuthProvider>
    </ModulesProvider>,
  );
}

/** The answers `/auth/me` gives, in order. */
let meAnswers: Array<() => Promise<Response>> = [];
/** The answer the sign-in gets. */
let loginAnswer: () => Promise<Response>;

function tokenEvent(token: string | null): void {
  if (token === null) localStorage.removeItem(TOKEN_STORAGE_KEY);
  else localStorage.setItem(TOKEN_STORAGE_KEY, token);
  window.dispatchEvent(new StorageEvent('storage', { key: TOKEN_STORAGE_KEY, newValue: token }));
}

/** Let the stand-in router finish whatever navigations are pending. */
async function settleNavigations(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

const navigatedWithNext = () => mockReplace.mock.calls.map(([url]) => url).filter((url) => url.includes('next='));

beforeEach(() => {
  __resetModulesCache();
  mockFetch.mockReset();
  mockReplace.mockClear();
  localStorage.clear();
  mockRoute = mockRouteFor('/feature-flags/flag-1');
  meAnswers = [];
  loginAnswer = () =>
    Promise.resolve(
      jsonResponse(200, { access_token: 'tok-second', token_type: 'bearer', expires_in: 43200, user: nextPerson }),
    );
  mockFetch.mockImplementation((url: string) => {
    if (url === ME) {
      const next = meAnswers.shift();
      if (!next) throw new Error('an /auth/me the test did not expect');
      return next();
    }
    if (url === LOGOUT) return Promise.resolve(jsonResponse(204, undefined));
    if (url === LOGIN) return loginAnswer();
    return Promise.resolve(jsonResponse(404, { detail: 'Not Found' }));
  });
});

describe('a session check that answers late (#1579)', () => {
  it('a check in flight at the sign-out leaves the tab on the sign-in page, with no next', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok-first');
    let release!: (response: Response) => void;
    const held = new Promise<Response>((resolve) => {
      release = resolve;
    });
    meAnswers = [() => Promise.resolve(jsonResponse(200, previous)), () => held];
    renderDashboard();
    expect(await screen.findByTestId('flag-page')).toBeInTheDocument();

    // Another tab touches the token: this tab checks the session again.
    await act(async () => {
      tokenEvent('tok-first');
    });
    expect(mockFetch.mock.calls.filter(([url]) => url === ME)).toHaveLength(2);

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Log out' }));
    });
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(mockRoute.asPath).toBe('/login');

    await act(async () => {
      release(jsonResponse(200, previous));
    });
    await settleNavigations();

    expect(mockRoute.asPath).toBe('/login');
    expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(navigatedWithNext()).toEqual([]);
  });

  it('pin: after a sign-out, a sign-in from another tab and then its sign-out send the page as next', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'tok-first');
    meAnswers = [
      () => Promise.resolve(jsonResponse(200, previous)),
      () => Promise.resolve(jsonResponse(200, nextPerson)),
    ];
    renderDashboard();
    expect(await screen.findByTestId('flag-page')).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Log out' }));
    });
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(mockRoute.asPath).toBe('/login');

    // Someone signs in from another tab: this tab follows, to the default page.
    await act(async () => {
      tokenEvent('tok-second');
    });
    await waitFor(() => expect(mockRoute.asPath).toBe(DEFAULT_AFTER_LOGIN));
    expect(await screen.findByTestId('flag-page')).toBeInTheDocument();

    // ...and signs out there: this page is theirs, so it is the next.
    await act(async () => {
      tokenEvent(null);
    });
    await waitFor(() => expect(mockRoute.asPath).toBe(`/login?next=${encodeURIComponent(DEFAULT_AFTER_LOGIN)}`));
  });
});

describe('a sign-in whose answer cannot be read (#1039)', () => {
  it('says the sign-in did not finish, and stays on the sign-in page signed out', async () => {
    mockRoute = mockRouteFor('/login');
    loginAnswer = () =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => 'application/json' },
        text: () => Promise.resolve('{"access_token": "tok-sec'),
      } as unknown as Response);
    renderDashboard();
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Email'), { target: { value: nextPerson.email } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'Second1234!' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    });

    const alert = await screen.findByTestId('login-error');
    expect(alert.textContent).toBe("Sign-in did not finish: the API's answer could not be read. Try again in a moment.");
    expect(alert).toHaveAttribute('role', 'alert');
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    await settleNavigations();
    expect(mockRoute.asPath).toBe('/login');
  });
});
