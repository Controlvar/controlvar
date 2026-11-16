/**
 * `AuthContext.login` and `verifySecondStep` for an account with two-step
 * sign-in on (#1812, U1): the password step resolves `second-step` and changes
 * nothing (no token, no user, no storage); the code step stores the token as a
 * one-step sign-in does.
 */
import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import { AuthProvider, SignInResult, useAuth } from '@/contexts/AuthContext';
import * as api from '@/services/api';
import { TOKEN_STORAGE_KEY, UserMe } from '@/services/api';
import { startSignInEnrolment } from '@/services/twoFactor';

const mockFetch = jest.fn();
global.fetch = mockFetch;

const MFA_TOKEN = 'mfa.PLANTED-IN-CONTEXT.token';

const user: UserMe = {
  id: 'u-9',
  email: 'ana@example.com',
  username: 'ana',
  full_name: null,
  role: 'VIEWER',
  is_superuser: false,
  is_active: true,
  auth_provider: 'local',
};

function respond(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'STATUS',
    headers: { get: () => 'application/json' },
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response;
}

/** Every key and value held in either storage, as one string. */
function everythingStored(): string {
  const values: string[] = [];
  for (const storage of [window.localStorage, window.sessionStorage]) {
    for (let i = 0; i < storage.length; i += 1) {
      const key = storage.key(i) as string;
      values.push(key, storage.getItem(key) ?? '');
    }
  }
  return values.join('\n');
}

let captured: ReturnType<typeof useAuth> | null = null;

function Capture() {
  captured = useAuth();
  return <span data-testid="status">{captured.status}</span>;
}

async function mount() {
  render(
    <AuthProvider>
      <Capture />
    </AuthProvider>,
  );
  await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));
}

beforeEach(() => {
  mockFetch.mockReset();
  localStorage.clear();
  sessionStorage.clear();
  captured = null;
  delete process.env.NEXT_PUBLIC_API_URL;
});

it('U1: the password step of a two-step account resolves second-step and calls no setToken', async () => {
  const setToken = jest.spyOn(api, 'setToken');
  try {
    await mount();
    mockFetch.mockResolvedValueOnce(
      respond(200, { mfa_required: true, mfa_token: MFA_TOKEN, enrol: false, expires_in: 300 }),
    );
    let result: SignInResult | null = null;
    await act(async () => {
      result = await captured!.login('ana@example.com', 'Secret123');
    });
    expect(result).toEqual({ status: 'second-step', mfaToken: MFA_TOKEN, expiresIn: 300 });
    expect(setToken).not.toHaveBeenCalled();
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(everythingStored()).not.toContain(MFA_TOKEN);
    expect(window.location.href).not.toContain(MFA_TOKEN);
    expect(screen.getByTestId('status')).toHaveTextContent('anonymous');
  } finally {
    setToken.mockRestore();
  }
});

it('verifySecondStep posts the token and the answer without a bearer, then signs in', async () => {
  await mount();
  mockFetch.mockResolvedValueOnce(
    respond(200, { access_token: 'full.jwt', token_type: 'bearer', expires_in: 43200, user }),
  );
  let signedIn: UserMe | null = null;
  await act(async () => {
    signedIn = await captured!.verifySecondStep(MFA_TOKEN, { recovery_code: 'ABCDE-FGHIJ' });
  });
  const [url, init] = mockFetch.mock.calls[0];
  expect(url).toBe('/api/v1/auth/mfa/verify');
  expect(JSON.parse(init.body)).toEqual({ mfa_token: MFA_TOKEN, recovery_code: 'ABCDE-FGHIJ' });
  expect(init.headers.Authorization).toBeUndefined();
  expect(signedIn).toEqual(user);
  expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('full.jwt');
  await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));
});

it('a refused code step changes nothing, and a 401 there signs no one out', async () => {
  localStorage.setItem(TOKEN_STORAGE_KEY, 'someone-elses-tab-token');
  mockFetch.mockResolvedValueOnce(respond(401, { detail: 'Could not validate credentials' }));
  await mount();
  mockFetch.mockResolvedValueOnce(respond(401, { detail: 'That code is not correct.' }));
  localStorage.setItem(TOKEN_STORAGE_KEY, 'a-token-stored-meanwhile');
  await act(async () => {
    await expect(captured!.verifySecondStep(MFA_TOKEN, { code: '000000' })).rejects.toMatchObject({ status: 401 });
  });
  // `auth: false`: the code step's 401 is about the code, not a session.
  expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('a-token-stored-meanwhile');
});

it('a 200 with no access_token is never stored as the token', async () => {
  await mount();
  mockFetch.mockResolvedValueOnce(respond(200, { token_type: 'bearer', expires_in: 1, user }));
  await act(async () => {
    await expect(captured!.verifySecondStep(MFA_TOKEN, { code: '123456' })).rejects.toMatchObject({
      unreadableAnswer: true,
    });
  });
  expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  expect(screen.getByTestId('status')).toHaveTextContent('anonymous');
});

// A role that requires two-step sign-in (#1812, PR 4): `enrol: true`.

it('U1: the password step of a role that requires it resolves enrol-step and calls no setToken', async () => {
  const setToken = jest.spyOn(api, 'setToken');
  try {
    await mount();
    mockFetch.mockResolvedValueOnce(
      respond(200, { mfa_required: true, mfa_token: MFA_TOKEN, enrol: true, expires_in: 600 }),
    );
    let result: SignInResult | null = null;
    await act(async () => {
      result = await captured!.login('ana@example.com', 'Secret123');
    });
    expect(result).toEqual({ status: 'enrol-step', mfaToken: MFA_TOKEN, expiresIn: 600 });
    expect(setToken).not.toHaveBeenCalled();
    expect(everythingStored()).not.toContain(MFA_TOKEN);
    expect(screen.getByTestId('status')).toHaveTextContent('anonymous');
  } finally {
    setToken.mockRestore();
  }
});

it('confirmSignInEnrolment stores nothing until finish, then signs in with the access token', async () => {
  const setToken = jest.spyOn(api, 'setToken');
  try {
    await mount();
    const codes = Array.from({ length: 10 }, (_, i) => `CODE${i}ABCDE`.slice(0, 10));
    mockFetch.mockResolvedValueOnce(
      respond(200, {
        recovery_codes: codes,
        login: { access_token: 'full.jwt', token_type: 'bearer', expires_in: 43200, user },
      }),
    );
    let done: Awaited<ReturnType<NonNullable<typeof captured>['confirmSignInEnrolment']>> | null = null;
    await act(async () => {
      done = await captured!.confirmSignInEnrolment(MFA_TOKEN, '123456');
    });
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe('/api/v1/auth/mfa/enroll/confirm');
    expect(JSON.parse(init.body)).toEqual({ mfa_token: MFA_TOKEN, code: '123456' });
    expect(init.headers.Authorization).toBeUndefined();
    expect(done!.recoveryCodes).toEqual(codes);
    expect(setToken).not.toHaveBeenCalled();
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(screen.getByTestId('status')).toHaveTextContent('anonymous');

    act(() => {
      expect(done!.finish()).toEqual(user);
    });
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('full.jwt');
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));
    expect(everythingStored()).not.toContain(MFA_TOKEN);
  } finally {
    setToken.mockRestore();
  }
});

it('a turned-on answer with no access token is refused before anything is shown or stored', async () => {
  await mount();
  mockFetch.mockResolvedValueOnce(respond(200, { recovery_codes: ['AAAAABBBBB'], login: { user } }));
  await act(async () => {
    await expect(captured!.confirmSignInEnrolment(MFA_TOKEN, '123456')).rejects.toMatchObject({
      unreadableAnswer: true,
    });
  });
  expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
});

// The setup steps' refusals, in the shape of the code step's test above.

it('a refused first code of the setup changes nothing, and a 401 there signs no one out', async () => {
  const setToken = jest.spyOn(api, 'setToken');
  try {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'someone-elses-tab-token');
    mockFetch.mockResolvedValueOnce(respond(401, { detail: 'Could not validate credentials' }));
    await mount();
    mockFetch.mockResolvedValueOnce(respond(401, { detail: 'That code is not correct.' }));
    localStorage.setItem(TOKEN_STORAGE_KEY, 'a-token-stored-meanwhile');
    setToken.mockClear();
    await act(async () => {
      await expect(captured!.confirmSignInEnrolment(MFA_TOKEN, '000000')).rejects.toMatchObject({ status: 401 });
    });
    // `auth: false`: this 401 is about the code, not a session.
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('a-token-stored-meanwhile');
    expect(setToken).not.toHaveBeenCalled();
    expect(everythingStored()).not.toContain('full.jwt');
  } finally {
    setToken.mockRestore();
  }
});

it('a refused request for the setup key changes nothing, and a 401 there signs no one out', async () => {
  const setToken = jest.spyOn(api, 'setToken');
  try {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'someone-elses-tab-token');
    mockFetch.mockResolvedValueOnce(respond(401, { detail: 'Could not validate credentials' }));
    await mount();
    mockFetch.mockResolvedValueOnce(respond(401, { detail: 'Sign-in expired. Start again.' }));
    localStorage.setItem(TOKEN_STORAGE_KEY, 'a-token-stored-meanwhile');
    setToken.mockClear();
    await act(async () => {
      await expect(startSignInEnrolment(MFA_TOKEN)).rejects.toMatchObject({ status: 401 });
    });
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('a-token-stored-meanwhile');
    expect(setToken).not.toHaveBeenCalled();
    expect(screen.getByTestId('status')).toHaveTextContent('anonymous');
  } finally {
    setToken.mockRestore();
  }
});
