/**
 * The code step of a local sign-in (#1812, PR 3 of 5): after the right
 * password, an account with two-step sign-in on is asked for a code from its
 * authenticator app or a recovery code, in the same card.
 *
 *   U1  the password step's `mfa_token` is never stored: no token, nothing in
 *       localStorage or sessionStorage, nothing in the URL;
 *   U2  a refused code never reads as a wrong password;
 *   U3  (the code step's half) 401/423/503 map to the fixed sentences, the
 *       cap's 423 included (CK1812-5);
 *   U4  the field's attributes, and a code's leading zeros survive.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import LoginPage, { codeStepErrorMessage, loginErrorMessage } from '@/pages/login';
import { AuthProvider } from '@/contexts/AuthContext';
import { ApiError, TOKEN_STORAGE_KEY, UserMe } from '@/services/api';
import {
  CODE_STEP_LOCKED_TEXT,
  LOST_DEVICE_HELP,
  SIGN_IN_EXPIRED_DETAIL,
  SIGN_IN_TIMED_OUT_MESSAGE,
  UNAVAILABLE_TEXT,
  WRONG_CODE_DETAIL,
  WRONG_CODE_MESSAGE,
  WRONG_RECOVERY_CODE_MESSAGE,
} from '@/services/twoFactor';

const mockReplace = jest.fn().mockResolvedValue(true);

jest.mock('next/router', () => ({
  useRouter: () => ({
    replace: mockReplace,
    push: jest.fn(),
    pathname: '/login',
    asPath: '/login',
    query: {},
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

const MFA_TOKEN = 'mfa.PLANTED-MFA-TOKEN.value';
const LOGIN = '/api/v1/auth/login';
const VERIFY = '/api/v1/auth/mfa/verify';
const PASSWORD_SENTENCE = 'Email or password is incorrect.';

const user: UserMe = {
  id: 'u-1',
  email: 'ana@example.com',
  username: 'ana',
  full_name: 'Ana',
  role: 'DEVELOPER',
  is_superuser: false,
  is_active: true,
  auth_provider: 'local',
};

function respond(status: number, body?: unknown, headers: Record<string, string> = {}): Response {
  const lower: Record<string, string> = { 'content-type': 'application/json' };
  for (const [k, v] of Object.entries(headers)) lower[k.toLowerCase()] = v;
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'STATUS',
    headers: { get: (name: string) => lower[name.toLowerCase()] ?? null },
    text: () => Promise.resolve(body === undefined ? '' : JSON.stringify(body)),
  } as unknown as Response;
}

const MFA_REQUIRED = { mfa_required: true, mfa_token: MFA_TOKEN, enrol: false, expires_in: 300 };
const SIGNED_IN = { access_token: 'full.jwt', token_type: 'bearer', expires_in: 43200, user };

function renderLogin() {
  return render(
    <AuthProvider>
      <LoginPage />
    </AuthProvider>,
  );
}

async function passwordStep() {
  mockFetch.mockResolvedValueOnce(respond(200, MFA_REQUIRED));
  renderLogin();
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'ana@example.com' } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'Secret123' } });
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
  });
  await screen.findByTestId('mfa-form');
}

async function sendCode(value: string) {
  fireEvent.change(screen.getByTestId('mfa-code-input'), { target: { value } });
  await act(async () => {
    fireEvent.click(screen.getByTestId('mfa-submit'));
  });
}

function callsTo(path: string) {
  return mockFetch.mock.calls.filter(([url]) => String(url).endsWith(path));
}

/** Every value held in either storage, as one string. */
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

const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

beforeEach(() => {
  mockFetch.mockReset();
  mockReplace.mockClear();
  localStorage.clear();
  sessionStorage.clear();
  delete process.env.NEXT_PUBLIC_API_URL;
});

describe('the code step of a sign-in', () => {
  it('U1: the password step stores no token and keeps the mfa_token out of storage and the URL', async () => {
    const setItem = jest.spyOn(Storage.prototype, 'setItem');
    try {
      await passwordStep();
      expect(screen.getByTestId('mfa-heading')).toHaveTextContent('Enter your code');
      expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
      expect(everythingStored()).not.toContain(MFA_TOKEN);
      expect(setItem.mock.calls.flat().join('\n')).not.toContain(MFA_TOKEN);
      expect(window.location.href).not.toContain(MFA_TOKEN);
      expect(mockReplace).not.toHaveBeenCalled();
      expect(document.body.innerHTML).not.toContain(MFA_TOKEN);
    } finally {
      setItem.mockRestore();
    }
  });

  it('sends the mfa_token and the code as typed, then stores the token and goes on', async () => {
    await passwordStep();
    mockFetch.mockResolvedValueOnce(respond(200, SIGNED_IN));
    await sendCode('005 924');

    const [url, init] = callsTo(VERIFY)[0];
    expect(url).toBe(VERIFY);
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ mfa_token: MFA_TOKEN, code: '005 924' });
    // No session yet: the code step carries no bearer.
    expect(init.headers.Authorization).toBeUndefined();
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/experiments'));
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('full.jwt');
    expect(everythingStored()).not.toContain(MFA_TOKEN);
  });

  it('U4: a text field with the one-time-code hints, no 6-character cap, and the zeros of 005924 kept', async () => {
    await passwordStep();
    const field = screen.getByTestId('mfa-code-input');
    expect(field).toHaveAttribute('type', 'text');
    expect(field).toHaveAttribute('inputmode', 'numeric');
    expect(field).toHaveAttribute('autocomplete', 'one-time-code');
    expect(field).not.toHaveAttribute('maxlength', '6');
    expect(screen.getByLabelText('Code')).toBe(field);
    fireEvent.change(field, { target: { value: '005924' } });
    expect(field).toHaveValue('005924');

    mockFetch.mockResolvedValueOnce(respond(200, SIGNED_IN));
    await act(async () => {
      fireEvent.click(screen.getByTestId('mfa-submit'));
    });
    expect(JSON.parse(callsTo(VERIFY)[0][1].body).code).toBe('005924');
  });

  it('moves focus to the code field, and the step passes axe', async () => {
    await passwordStep();
    await waitFor(() => expect(screen.getByTestId('mfa-code-input')).toHaveFocus());
    const result = await axe.run(document.body, axeOptions);
    expect(result.violations.map((v) => v.id)).toEqual([]);
  });

  it('U2: a wrong code says so, never that the password is wrong, and keeps the step', async () => {
    await passwordStep();
    mockFetch.mockResolvedValueOnce(respond(401, { detail: WRONG_CODE_DETAIL }, { 'WWW-Authenticate': 'Bearer' }));
    await sendCode('123456');

    const alert = await screen.findByTestId('mfa-error');
    expect(alert).toHaveTextContent(WRONG_CODE_MESSAGE);
    expect(document.body.textContent).not.toContain(PASSWORD_SENTENCE);
    expect(screen.getByTestId('mfa-form')).toBeInTheDocument();
    expect(screen.getByTestId('mfa-code-input')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByTestId('mfa-code-input').getAttribute('aria-describedby')).toContain(alert.id);
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    // The password step's mapper would have said the password was wrong.
    expect(loginErrorMessage(new ApiError({ status: 401, detail: WRONG_CODE_DETAIL }))).toBe(PASSWORD_SENTENCE);
    expect(codeStepErrorMessage(new ApiError({ status: 401, detail: WRONG_CODE_DETAIL }), false)).toBe(
      WRONG_CODE_MESSAGE,
    );
  });

  it('a recovery code: the same field relabelled, sent as recovery_code, its own wrong-code sentence', async () => {
    await passwordStep();
    fireEvent.click(screen.getByTestId('mfa-switch'));
    expect(screen.getByTestId('mfa-heading')).toHaveTextContent('Enter a recovery code');
    const field = screen.getByLabelText('Recovery code');
    expect(field).toBe(screen.getByTestId('mfa-code-input'));
    expect(screen.queryByLabelText('Code')).not.toBeInTheDocument();
    expect(field).toHaveAttribute('autocomplete', 'off');

    mockFetch.mockResolvedValueOnce(respond(401, { detail: WRONG_CODE_DETAIL }));
    await sendCode('abcde-fghij');
    expect(JSON.parse(callsTo(VERIFY)[0][1].body)).toEqual({ mfa_token: MFA_TOKEN, recovery_code: 'abcde-fghij' });
    expect(await screen.findByTestId('mfa-error')).toHaveTextContent(WRONG_RECOVERY_CODE_MESSAGE);
    expect(document.body.textContent).not.toContain(PASSWORD_SENTENCE);

    // Back to the app's code and again: the error goes with the switch.
    fireEvent.click(screen.getByTestId('mfa-switch'));
    expect(screen.getByTestId('mfa-heading')).toHaveTextContent('Enter your code');
    expect(screen.queryByTestId('mfa-error')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('mfa-switch'));

    mockFetch.mockResolvedValueOnce(respond(200, SIGNED_IN));
    await sendCode('ABCDE FGHIJ');
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/experiments'));
    expect(JSON.parse(callsTo(VERIFY)[1][1].body)).toEqual({ mfa_token: MFA_TOKEN, recovery_code: 'ABCDE FGHIJ' });
  });

  it('an expired step goes back to the password with its own sentence, and drops the token', async () => {
    await passwordStep();
    mockFetch.mockResolvedValueOnce(respond(401, { detail: SIGN_IN_EXPIRED_DETAIL }));
    await sendCode('123456');

    expect(await screen.findByTestId('login-error')).toHaveTextContent(SIGN_IN_TIMED_OUT_MESSAGE);
    expect(screen.queryByTestId('mfa-form')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Password')).toHaveValue('');
    expect(document.body.textContent).not.toContain(PASSWORD_SENTENCE);

    // The next sign-in starts from the password again, with a new token.
    mockFetch.mockResolvedValueOnce(respond(200, { ...MFA_REQUIRED, mfa_token: 'second.token' }));
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'Secret123' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    });
    mockFetch.mockResolvedValueOnce(respond(200, SIGNED_IN));
    await sendCode('123456');
    expect(JSON.parse(callsTo(VERIFY)[1][1].body).mfa_token).toBe('second.token');
  });

  it.each([
    ['the cap', 423, CODE_STEP_LOCKED_TEXT, {}, CODE_STEP_LOCKED_TEXT],
    [
      'the address lock',
      423,
      'Too many failed login attempts; account temporarily locked. Retry in 840 seconds.',
      { 'Retry-After': '840' },
      'Too many failed login attempts; account temporarily locked. Retry in 840 seconds.',
    ],
    ['an unreadable secret', 503, UNAVAILABLE_TEXT, {}, UNAVAILABLE_TEXT],
    ['the rate limit', 429, 'Rate limit exceeded', { 'Retry-After': '30' }, 'Too many attempts. Please wait a moment and try again.'],
  ])('U3: %s shows its fixed sentence', async (_name, status, detail, headers, expected) => {
    await passwordStep();
    mockFetch.mockResolvedValueOnce(respond(status, { detail }, headers));
    await sendCode('123456');
    expect(await screen.findByTestId('mfa-error')).toHaveTextContent(expected);
    expect(screen.getByTestId('mfa-form')).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(PASSWORD_SENTENCE);
  });

  it("CK1812-5: the cap's 423 never falls back to the password step's 'try again in a few minutes'", () => {
    const cap = new ApiError({ status: 423, detail: CODE_STEP_LOCKED_TEXT });
    expect(codeStepErrorMessage(cap, false)).toBe(CODE_STEP_LOCKED_TEXT);
    expect(codeStepErrorMessage(cap, true)).toBe(CODE_STEP_LOCKED_TEXT);
    expect(codeStepErrorMessage(cap, false)).not.toMatch(/few minutes/);
  });

  it('asks for a code before sending an empty one', async () => {
    await passwordStep();
    await act(async () => {
      fireEvent.click(screen.getByTestId('mfa-submit'));
    });
    expect(screen.getByTestId('mfa-error')).toHaveTextContent('Enter the 6-digit code from your authenticator app.');
    expect(callsTo(VERIFY)).toHaveLength(0);
  });

  it('shows the lost-device path, and Back to sign-in drops the token and returns focus to the email', async () => {
    await passwordStep();
    expect(screen.getByText('Lost your phone and your recovery codes?')).toBeInTheDocument();
    expect(screen.getByTestId('lost-device-help')).toHaveTextContent(LOST_DEVICE_HELP);
    // The password step's own help and the SSO form are not on the code step.
    expect(screen.queryByText('Forgot password?')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('mfa-back'));
    expect(screen.queryByTestId('mfa-form')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText('Email')).toHaveFocus());
    expect(screen.getByLabelText('Email')).toHaveValue('ana@example.com');
    expect(document.body.innerHTML).not.toContain(MFA_TOKEN);
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it('an account without two-step sign-in signs in with the password alone, as before', async () => {
    mockFetch.mockResolvedValueOnce(respond(200, SIGNED_IN));
    renderLogin();
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'ana@example.com' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'Secret123' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    });
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/experiments'));
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('full.jwt');
    expect(callsTo(LOGIN)).toHaveLength(1);
    expect(screen.queryByTestId('mfa-form')).not.toBeInTheDocument();
  });

  it('a sign-in answer with neither an access token nor mfa_required stores nothing', async () => {
    mockFetch.mockResolvedValueOnce(respond(200, { token_type: 'bearer' }));
    renderLogin();
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'ana@example.com' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'Secret123' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    });
    expect(await screen.findByTestId('login-error')).toHaveTextContent("the API's answer could not be read");
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(mockReplace).not.toHaveBeenCalled();
  });
});
