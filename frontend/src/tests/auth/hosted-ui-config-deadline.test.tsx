/**
 * `GET /api/v1/auth/config` that never answers (#708): `/login` and the
 * session check wait for it, so it has a deadline of its own,
 * AUTH_CONFIG_TIMEOUT_MS, after which both go on with the local provider.
 * Fake timers; no wall clock.
 */
import { AUTH_ME, CONFIG, api, callsTo, jsonResponse, resetHarness, restoreHarness } from './hostedUiHarness';
import React from 'react';
import { act, render, screen } from '@testing-library/react';
import { AuthConfigProvider } from '@/contexts/AuthConfigContext';
import { AuthProvider, useAuth } from '@/contexts/AuthContext';
import LoginPage from '@/pages/login';
import { TOKEN_STORAGE_KEY } from '@/services/api';
import { AUTH_CONFIG_TIMEOUT_MS } from '@/services/authConfig';

let registered = 0;
function row(name: string, fn: () => unknown) {
  registered += 1;
  it(name, async () => {
    await fn();
  });
}

/** An answer that never comes: the request ends only when it is aborted. */
function hangs(init: RequestInit): Promise<Response> {
  return new Promise<Response>((_resolve, reject) => {
    init.signal?.addEventListener('abort', () => reject(new DOMException('The operation was aborted.', 'AbortError')));
  });
}

function Status() {
  const { status } = useAuth();
  return <span data-testid="status">{status}</span>;
}

function renderLogin() {
  return render(
    <AuthConfigProvider>
      <AuthProvider>
        <LoginPage />
        <Status />
      </AuthProvider>
    </AuthConfigProvider>,
  );
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await jest.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  resetHarness();
  api.routes[CONFIG] = (init) => hangs(init);
  jest.useFakeTimers();
});

afterEach(() => {
  jest.useRealTimers();
  restoreHarness();
  jest.restoreAllMocks();
});

describe('a /auth/config that never answers', () => {
  row('the deadline is five seconds, not the default thirty', () => {
    expect(AUTH_CONFIG_TIMEOUT_MS).toBe(5_000);
  });

  row('/login waits until the deadline, then shows the local form', async () => {
    renderLogin();
    await advance(AUTH_CONFIG_TIMEOUT_MS - 1);
    expect(screen.getByTestId('login-loading')).toBeInTheDocument();
    expect(screen.queryByLabelText('Password')).toBeNull();
    await advance(1);
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
    expect(screen.queryByTestId('hosted-ui-sign-in')).toBeNull();
  });

  row('the session check runs at the deadline, through the local route', async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'local-1');
    api.routes[AUTH_ME] = () =>
      jsonResponse(200, {
        id: 'u',
        email: 'pat@example.com',
        username: 'pat',
        full_name: null,
        role: 'ADMIN',
        is_superuser: true,
        is_active: true,
        auth_provider: 'local',
      });
    renderLogin();
    await advance(AUTH_CONFIG_TIMEOUT_MS - 1);
    expect(callsTo(AUTH_ME)).toHaveLength(0);
    expect(screen.getByTestId('status')).toHaveTextContent('loading');
    await advance(1);
    expect(callsTo(AUTH_ME)).toHaveLength(1);
    expect(screen.getByTestId('status')).toHaveTextContent('authenticated');
  });
});

describe('this file', () => {
  it('collects every test it declares', () => {
    expect(registered).toBe(3);
  });
});
