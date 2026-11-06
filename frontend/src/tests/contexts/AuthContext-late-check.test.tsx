/**
 * A session check (`GET /api/v1/auth/me`) whose answer arrives after a
 * sign-out, or after the next sign-in, changes nothing (#1579): not the user,
 * not the stored token, not the sign-in page's message, not where the tab
 * goes next. Whatever the answer: a 200, a 401, a 500 or no answer at all.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { AuthProvider, useAuth } from '@/contexts/AuthContext';
import { TOKEN_STORAGE_KEY, UserMe, navigation } from '@/services/api';

const mockFetch = jest.fn();
global.fetch = mockFetch;

const ME = '/api/v1/auth/me';
const LOGIN = '/api/v1/auth/login';
const LOGOUT = '/api/v1/auth/logout';

const first: UserMe = {
  id: 'u-1',
  email: 'first@demo.com',
  username: 'first',
  full_name: 'First Person',
  role: 'DEVELOPER',
  is_superuser: false,
  is_active: true,
  auth_provider: 'local',
};

const second: UserMe = { ...first, id: 'u-2', email: 'second@demo.com', username: 'second' };

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'STATUS',
    headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(body === undefined ? '' : JSON.stringify(body)),
  } as unknown as Response;
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function Consumer() {
  const { user, status, sessionError, afterLogoutPath, login, logout, refresh } = useAuth();
  return (
    <div>
      <span data-testid="status">{status}</span>
      <span data-testid="email">{user?.email ?? 'none'}</span>
      <span data-testid="session-error">{sessionError ?? ''}</span>
      <span data-testid="after-logout">{afterLogoutPath ?? ''}</span>
      <button onClick={() => void login(second.email, 'Second1234!').catch(() => undefined)}>login</button>
      <button onClick={() => void logout()}>logout</button>
      <button onClick={() => void refresh()}>refresh</button>
    </div>
  );
}

/** The answers `/auth/me` gives, in order. */
let meAnswers: Array<() => Promise<Response>> = [];
let assignSpy: jest.SpyInstance;

beforeEach(() => {
  mockFetch.mockReset();
  localStorage.clear();
  delete process.env.NEXT_PUBLIC_API_URL;
  assignSpy = jest.spyOn(navigation, 'assign').mockImplementation(() => {});
  meAnswers = [];
  mockFetch.mockImplementation((url: string) => {
    if (url === ME) {
      const next = meAnswers.shift();
      if (!next) throw new Error('an /auth/me the test did not expect');
      return next();
    }
    if (url === LOGOUT) return Promise.resolve(jsonResponse(204, undefined));
    if (url === LOGIN) {
      return Promise.resolve(
        jsonResponse(200, { access_token: 'jwt-second', token_type: 'bearer', expires_in: 43200, user: second }),
      );
    }
    return Promise.resolve(jsonResponse(404, { detail: 'Not Found' }));
  });
});

afterEach(() => {
  assignSpy.mockRestore();
});

const text = (id: string) => screen.getByTestId(id).textContent;

async function click(name: string): Promise<void> {
  await act(async () => {
    fireEvent.click(screen.getByText(name));
  });
}

type Trigger = 'a storage event' | 'refresh()';

/**
 * Signed in as `first` (token `jwt-first`), then a session check sent by
 * `trigger` and held: the returned deferred is its answer.
 */
async function signedInWithACheckInFlight(trigger: Trigger): Promise<Deferred<Response>> {
  localStorage.setItem(TOKEN_STORAGE_KEY, 'jwt-first');
  const held = deferred<Response>();
  meAnswers = [() => Promise.resolve(jsonResponse(200, first)), () => held.promise];
  render(
    <AuthProvider>
      <Consumer />
    </AuthProvider>,
  );
  await waitFor(() => expect(text('status')).toBe('authenticated'));
  await act(async () => {
    if (trigger === 'a storage event') {
      window.dispatchEvent(new StorageEvent('storage', { key: TOKEN_STORAGE_KEY, newValue: 'jwt-first' }));
    } else {
      fireEvent.click(screen.getByText('refresh'));
    }
  });
  const checks = mockFetch.mock.calls.filter(([url]) => url === ME);
  expect(checks).toHaveLength(2);
  expect((checks[1][1] as RequestInit & { headers: Record<string, string> }).headers.Authorization).toBe(
    'Bearer jwt-first',
  );
  return held;
}

async function signOutThenSignIn(): Promise<void> {
  await click('logout');
  expect(text('status')).toBe('anonymous');
  await click('login');
  await waitFor(() => expect(text('email')).toBe(second.email));
  expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('jwt-second');
}

const FAILURES: Array<[string, (held: Deferred<Response>) => void]> = [
  ['answers 500', (held) => held.resolve(jsonResponse(500, { detail: 'boom' }))],
  ['cannot be reached', (held) => held.reject(new TypeError('Failed to fetch'))],
];

describe('a session check that answers late (#1579)', () => {
  it.each<Trigger>(['a storage event', 'refresh()'])(
    'a check sent by %s and answered after the sign-out leaves the tab signed out',
    async (trigger) => {
      const held = await signedInWithACheckInFlight(trigger);
      await click('logout');
      await act(async () => {
        held.resolve(jsonResponse(200, first));
      });
      expect(text('status')).toBe('anonymous');
      expect(text('email')).toBe('none');
      expect(text('after-logout')).toBe('/login');
      expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
      expect(assignSpy).not.toHaveBeenCalled();
    },
  );

  it('a check sent before a sign-out and the next sign-in, answered 200, leaves the new session as it is', async () => {
    const held = await signedInWithACheckInFlight('a storage event');
    await signOutThenSignIn();
    await act(async () => {
      held.resolve(jsonResponse(200, first));
    });
    expect(text('status')).toBe('authenticated');
    expect(text('email')).toBe(second.email);
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('jwt-second');
  });

  it('a check sent before a sign-out and the next sign-in, answered 401, keeps the new token', async () => {
    const held = await signedInWithACheckInFlight('a storage event');
    await signOutThenSignIn();
    await act(async () => {
      held.resolve(jsonResponse(401, { detail: 'Could not validate credentials' }));
    });
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('jwt-second');
    expect(text('status')).toBe('authenticated');
    expect(text('email')).toBe(second.email);
    expect(assignSpy).not.toHaveBeenCalled();
  });

  it.each(FAILURES)('a check that %s after the sign-out leaves no message on the sign-in page', async (_label, fail) => {
    const held = await signedInWithACheckInFlight('a storage event');
    await click('logout');
    await act(async () => {
      fail(held);
    });
    expect(text('session-error')).toBe('');
    expect(text('status')).toBe('anonymous');
    expect(text('after-logout')).toBe('/login');
  });

  it.each(FAILURES)('a check that %s after the next sign-in leaves that session signed in', async (_label, fail) => {
    const held = await signedInWithACheckInFlight('a storage event');
    await signOutThenSignIn();
    await act(async () => {
      fail(held);
    });
    expect(text('status')).toBe('authenticated');
    expect(text('email')).toBe(second.email);
    expect(text('session-error')).toBe('');
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('jwt-second');
  });
});

describe('a session check whose answer cannot be read (#1039)', () => {
  it('signs out with the session text, and keeps the token for the next reload', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'jwt-first');
    meAnswers = [
      () =>
        Promise.resolve({
          ok: true,
          status: 200,
          headers: { get: () => 'application/json' },
          text: () => Promise.resolve('{"id": "u-1", "email":'),
        } as unknown as Response),
    ];
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() => expect(text('status')).not.toBe('loading'));
    expect(text('status')).toBe('anonymous');
    expect(text('session-error')).toBe('Signed out because your session could not be checked. Sign in again.');
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('jwt-first');
  });
});
