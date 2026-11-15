/**
 * `refresh` kept from the first render (#708).
 *
 * `/sso/complete` takes `refresh` from `useAuth()` in a mount-once effect and
 * calls it after the SSO exchange. On that first render the sign-in
 * configuration is still being read, and the session check does nothing
 * until it is known; so a `refresh` that was the check of that render never
 * asked who the user is, and the SSO sign-in landed on a protected page
 * signed out, which sent it back to /login. `refresh` is now one function
 * that runs the current check.
 */
import {
  ACCOUNT,
  AUTH_ME,
  CONFIG,
  POOL_A,
  USERS_ME,
  api,
  callsTo,
  jsonResponse,
  resetHarness,
  restoreHarness,
  settle,
} from './hostedUiHarness';
import React, { useEffect } from 'react';
import { act, render, screen } from '@testing-library/react';
import { AuthConfigProvider } from '@/contexts/AuthConfigContext';
import { AuthProvider, useAuth } from '@/contexts/AuthContext';
import { UserMe, setToken } from '@/services/api';

let registered = 0;
function row(name: string, fn: () => unknown) {
  registered += 1;
  it(name, async () => {
    await fn();
  });
}

const LOCAL_USER = {
  id: 'u-1',
  email: 'pat@example.com',
  username: 'pat',
  full_name: null,
  role: 'DEVELOPER',
  is_superuser: false,
  is_active: true,
  auth_provider: 'local',
};

let firstRefresh: (() => Promise<UserMe | null>) | null = null;

/** Keeps the `refresh` of its first render, as /sso/complete's effect does. */
function KeepsFirstRefresh() {
  const { refresh, status, user } = useAuth();
  useEffect(() => {
    firstRefresh = refresh;
    // Once per mount, like the page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <span data-testid="who">
      {status}:{user ? user.username : 'none'}
    </span>
  );
}

function renderApp() {
  return render(
    <AuthConfigProvider>
      <AuthProvider>
        <KeepsFirstRefresh />
      </AuthProvider>
    </AuthConfigProvider>,
  );
}

/** What the SSO page does once the exchange has answered: store, then refresh. */
async function signInWithTheFirstRefresh(): Promise<UserMe | null> {
  setToken('sso-1');
  let result: UserMe | null = null;
  await act(async () => {
    result = await firstRefresh!();
  });
  return result;
}

beforeEach(() => {
  firstRefresh = null;
});

afterEach(() => {
  restoreHarness();
  jest.restoreAllMocks();
});

describe('regression #708: a refresh kept from the first render signs the user in (the SSO sign-in)', () => {
  row('local provider: it reads /auth/me once the configuration is known', async () => {
    resetHarness({ provider: 'local' });
    api.routes[AUTH_ME] = () => jsonResponse(200, LOCAL_USER);
    renderApp();
    await settle();
    expect(screen.getByTestId('who')).toHaveTextContent('anonymous:none');

    const user = await signInWithTheFirstRefresh();
    expect(callsTo(AUTH_ME)).toHaveLength(1);
    expect(user?.username).toBe('pat');
    expect(screen.getByTestId('who')).toHaveTextContent('authenticated:pat');
  });

  row('Cognito: the same, through /users/me', async () => {
    resetHarness(POOL_A);
    api.routes[USERS_ME] = () => jsonResponse(200, ACCOUNT);
    renderApp();
    await settle();
    expect(screen.getByTestId('who')).toHaveTextContent('anonymous:none');

    const user = await signInWithTheFirstRefresh();
    expect(callsTo(USERS_ME)).toHaveLength(1);
    expect(callsTo(AUTH_ME)).toHaveLength(0);
    expect(user?.username).toBe('pat');
    expect(screen.getByTestId('who')).toHaveTextContent('authenticated:pat');
  });

  row('before the configuration is known it resolves null at once, and the check runs when it is', async () => {
    resetHarness({ provider: 'local' });
    let answer: () => void = () => undefined;
    api.routes[CONFIG] = () =>
      new Promise<Response>((resolve) => {
        answer = () => resolve(jsonResponse(200, { provider: 'local' }));
      });
    api.routes[AUTH_ME] = () => jsonResponse(200, LOCAL_USER);
    renderApp();
    await settle();

    expect(await signInWithTheFirstRefresh()).toBeNull();
    expect(callsTo(AUTH_ME)).toHaveLength(0);
    expect(screen.getByTestId('who')).toHaveTextContent('loading:none');

    answer();
    await settle();
    expect(callsTo(AUTH_ME)).toHaveLength(1);
    expect(screen.getByTestId('who')).toHaveTextContent('authenticated:pat');
  });
});

describe('this file', () => {
  it('collects every test it declares', () => {
    expect(registered).toBe(3);
  });
});
