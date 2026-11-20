/**
 * A session check that gets a 503 from `GET /api/v1/users/me` (Cognito
 * configured, Cognito not answering) leaves the dashboard anonymous, says why
 * ("HTTP 503" and the request id), and keeps the stored token so the first
 * reload after Cognito answers again restores the session.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { AuthConfigProvider } from '@/contexts/AuthConfigContext';
import { AuthProvider, useAuth } from '@/contexts/AuthContext';
import { TOKEN_STORAGE_KEY, setToken } from '@/services/api';
import {
  USERS_ME,
  api,
  callsTo,
  jsonResponse,
  resetHarness,
  restoreHarness,
  settle,
} from './hostedUiHarness';

const COPY = 'Sign-in service is not answering. Try again in a moment.';

function Probe() {
  const { status, sessionError } = useAuth();
  return (
    <div>
      <span data-testid="status">{status}</span>
      <span data-testid="session-error">{sessionError ?? ''}</span>
    </div>
  );
}

beforeEach(() => {
  resetHarness();
});

afterEach(() => {
  restoreHarness();
});

it('stays anonymous, names HTTP 503 and keeps the token', async () => {
  setToken('jwt-kept');
  api.routes[USERS_ME] = () =>
    jsonResponse(503, { detail: COPY }, { 'x-request-id': 'req-503', 'retry-after': '2' });

  render(
    <AuthConfigProvider>
      <AuthProvider>
        <Probe />
      </AuthProvider>
    </AuthConfigProvider>,
  );
  await settle();

  expect(callsTo(USERS_ME)).toHaveLength(1);
  expect(screen.getByTestId('status').textContent).toBe('anonymous');
  expect(screen.getByTestId('session-error').textContent).toContain('HTTP 503');
  expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('jwt-kept');
});
