/**
 * Changing your own password (#503). Every outcome shows the page's own fixed
 * sentence; no response text reaches the screen. A change signs out every
 * session of the account (#501): an answer with a new token keeps this tab
 * signed in through it, and one without asks the user to sign in again. The
 * AuthContext is a stand-in here; account-password-session.test.tsx runs the
 * same page with the real one.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import ChangePasswordPage from '@/pages/account/password';
import { TOKEN_STORAGE_KEY, navigation } from '@/services/api';

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockFetch = jest.fn();
global.fetch = mockFetch;

const BASE = process.env.NEXT_PUBLIC_API_URL ?? '';
const PATH = '/api/v1/users/me/password';

/** Text a server could send. None of it may appear on the page. */
const PLANTED = 'PLANTED-SERVER-TEXT <b>do not show</b>';

const CHANGED =
  'Your password has been changed. Other browsers and devices signed in to your account have been ' +
  'signed out. Use the new password the next time you sign in.';
const SIGNED_OUT =
  'Your password has been changed, and every session has been signed out, this one too. ' +
  'Sign in again with the new password.';

const ME = {
  id: 'u1',
  email: 'u@example.com',
  username: 'u',
  full_name: null,
  role: 'DEVELOPER',
  is_superuser: false,
  is_active: true,
  auth_provider: 'local',
};

/** The sign-in answer the change gives an account without two-step sign-in. */
const NEW_TOKEN_ANSWER = { access_token: 'tok2', token_type: 'bearer', expires_in: 43200, user: ME };

const mockRefresh = jest.fn();
const mockLogout = jest.fn();
/** The stored token each time the page asked for the session to be re-checked. */
let tokenAtRefresh: Array<string | null> = [];

function signIn(authProvider = 'local') {
  mockAuth.mockReturnValue({
    status: 'authenticated',
    user: { ...ME, auth_provider: authProvider },
    refresh: mockRefresh,
    logout: mockLogout,
  });
}

function respond(status: number, body?: unknown, headers: Record<string, string> = {}) {
  const lower: Record<string, string> = { 'content-type': 'application/json' };
  for (const [k, v] of Object.entries(headers)) lower[k.toLowerCase()] = v;
  mockFetch.mockResolvedValueOnce({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => lower[name.toLowerCase()] ?? null },
    text: () => Promise.resolve(body === undefined ? '' : JSON.stringify(body)),
  } as unknown as Response);
}

function fill(current: string, next: string, confirm = next) {
  fireEvent.change(screen.getByTestId('current-password-input'), { target: { value: current } });
  fireEvent.change(screen.getByTestId('new-password-input'), { target: { value: next } });
  fireEvent.change(screen.getByTestId('confirm-password-input'), { target: { value: confirm } });
}

async function submit() {
  await act(async () => {
    fireEvent.click(screen.getByTestId('change-password-submit'));
  });
}

async function outcome() {
  return waitFor(() => screen.getByTestId('change-password-outcome'));
}

const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

beforeEach(() => {
  mockFetch.mockReset();
  mockRefresh.mockReset();
  mockLogout.mockReset();
  tokenAtRefresh = [];
  mockRefresh.mockImplementation(() => {
    tokenAtRefresh.push(localStorage.getItem(TOKEN_STORAGE_KEY));
    return Promise.resolve(ME);
  });
  mockLogout.mockResolvedValue(undefined);
  localStorage.clear();
  localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
  signIn('local');
});

describe('ChangePasswordPage', () => {
  it('states the rules and marks the fields for password managers', async () => {
    const { container } = render(<ChangePasswordPage />);
    expect(screen.getByTestId('change-password-page')).toBeInTheDocument();
    expect(screen.getByTestId('change-password-rules')).toHaveTextContent(
      'At least 8 characters and no more than 72 bytes, with an upper-case letter, a lower-case letter and a digit.',
    );
    expect(screen.getByLabelText('Current password')).toHaveAttribute('autocomplete', 'current-password');
    expect(screen.getByLabelText('New password')).toHaveAttribute('autocomplete', 'new-password');
    expect(screen.getByLabelText('Confirm new password')).toHaveAttribute('autocomplete', 'new-password');
    const result = await axe.run(container, axeOptions);
    expect(result.violations.map((v) => v.id)).toEqual([]);
  });

  it('sends the change, keeps the new token, and says it is done, clearing the fields', async () => {
    render(<ChangePasswordPage />);
    respond(200, NEW_TOKEN_ANSWER);
    fill('OldPass1', 'NewPass12');
    await submit();
    const box = await outcome();
    expect(box).toHaveAttribute('data-outcome', 'changed');
    expect(box).toHaveAttribute('role', 'status');
    expect(box).toHaveTextContent(CHANGED);
    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe(`${BASE}${PATH}`);
    expect(init.method).toBe('POST');
    expect(init.headers.Authorization).toBe('Bearer tok');
    expect(JSON.parse(init.body)).toEqual({ current_password: 'OldPass1', new_password: 'NewPass12' });
    expect(screen.getByTestId('current-password-input')).toHaveValue('');
    expect(screen.getByTestId('new-password-input')).toHaveValue('');
    // The new token is stored before the session is checked again, so that
    // check, and every request after it, carries the new token.
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('tok2');
    expect(tokenAtRefresh).toEqual(['tok2']);
    expect(mockLogout).not.toHaveBeenCalled();
    expect(screen.queryByTestId('change-password-sign-in')).not.toBeInTheDocument();
  });

  it('says it is done only once the session has been checked again', async () => {
    let finish: (value: unknown) => void = () => undefined;
    mockRefresh.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    render(<ChangePasswordPage />);
    respond(200, NEW_TOKEN_ANSWER);
    fill('OldPass1', 'NewPass12');
    await submit();
    await waitFor(() => expect(mockRefresh).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId('change-password-outcome')).not.toBeInTheDocument();
    await act(async () => {
      finish(ME);
    });
    expect(await outcome()).toHaveAttribute('data-outcome', 'changed');
  });

  // 204: an account with two-step sign-in on or required. A 200 without a
  // token is read the same way: no session continues.
  const signedOutAnswers: Array<[string, number, unknown]> = [
    ['204', 204, undefined],
    ['a 200 without a token', 200, { token_type: 'bearer' }],
    ['a 200 whose token is not a string', 200, { ...NEW_TOKEN_ANSWER, access_token: 42 }],
  ];

  it.each(signedOutAnswers)(
    'on %s says every session has ended, clears nothing, and Sign in signs this tab out',
    async (_label, status, body) => {
      render(<ChangePasswordPage />);
      respond(status, body);
      fill('OldPass1', 'NewPass12');
      await submit();
      const box = await outcome();
      expect(box).toHaveAttribute('data-outcome', 'signed-out');
      expect(box).toHaveAttribute('role', 'status');
      expect(box).toHaveTextContent(SIGNED_OUT);
      expect(box).not.toHaveTextContent('Other browsers');
      expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('tok');
      expect(mockRefresh).not.toHaveBeenCalled();
      expect(mockLogout).not.toHaveBeenCalled();

      const button = screen.getByTestId('change-password-sign-in');
      expect(button).toHaveTextContent('Sign in');
      button.focus();
      expect(button).toHaveFocus();
      fireEvent.click(button);
      expect(mockLogout).toHaveBeenCalledTimes(1);
      expect(mockLogout).toHaveBeenCalledWith();
    },
  );

  it('refuses a confirmation that does not match, without a request', async () => {
    render(<ChangePasswordPage />);
    fill('OldPass1', 'NewPass12', 'NewPass13');
    await submit();
    expect(await outcome()).toHaveTextContent('The new password and its confirmation do not match.');
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('asks for the current password when it is empty, without a request', async () => {
    render(<ChangePasswordPage />);
    fill('', 'NewPass12');
    await submit();
    expect(await outcome()).toHaveTextContent('Enter your current password.');
    expect(mockFetch).not.toHaveBeenCalled();
  });

  // Each server answer, the fixed sentence it must produce, and the body the
  // server sent. The bodies carry PLANTED, or the route's own detail; neither
  // may be shown.
  const cases: Array<[string, number, unknown, Record<string, string>, string]> = [
    [
      'incorrect',
      403,
      { detail: 'The current password is incorrect.' },
      {},
      'The current password is not correct. Check it and try again.',
    ],
    [
      'missing',
      403,
      { detail: 'Enter your current password (current_password) to set a new one.' },
      {},
      'Enter your current password.',
    ],
    [
      'no-password',
      403,
      { detail: 'This account does not sign in with a password, so it has no password to change.' },
      {},
      'This account signs in through single sign-on and has no password to change here.',
    ],
    [
      'forbidden',
      403,
      { detail: PLANTED },
      {},
      'The password could not be changed. Ask an administrator to check your account.',
    ],
    [
      'weak',
      422,
      { detail: [{ loc: ['body', 'new_password'], msg: PLANTED, type: 'value_error' }] },
      {},
      'The new password does not meet the rules. At least 8 characters and no more than 72 bytes, ' +
        'with an upper-case letter, a lower-case letter and a digit.',
    ],
    [
      'locked',
      423,
      { detail: `${PLANTED} Retry in 999 seconds.` },
      { 'Retry-After': '42' },
      'Too many failed attempts. Try again in 42 seconds.',
    ],
    [
      'locked',
      423,
      { detail: PLANTED },
      { 'Retry-After': '900' },
      'Too many failed attempts. Try again in 15 minutes.',
    ],
    [
      'locked',
      423,
      { detail: PLANTED },
      {},
      'Too many failed attempts. Wait a few minutes and try again.',
    ],
    [
      'unavailable',
      404,
      { detail: PLANTED },
      {},
      'This server does not manage passwords, so there is no password to change here.',
    ],
    [
      'failed',
      500,
      { detail: PLANTED },
      {},
      'The password could not be changed. Try again; if it keeps failing, ask an administrator.',
    ],
  ];

  it.each(cases)('shows the fixed sentence for %s (HTTP %d) and no server text', async (kind, status, body, headers, sentence) => {
    render(<ChangePasswordPage />);
    respond(status, body, headers);
    fill('OldPass1', 'NewPass12');
    await submit();
    const box = await outcome();
    expect(box).toHaveAttribute('data-outcome', kind);
    expect(box).toHaveTextContent(sentence);
    const page = screen.getByTestId('change-password-page');
    expect(page).not.toHaveTextContent('PLANTED-SERVER-TEXT');
    expect(page).not.toHaveTextContent('999');
    if (typeof (body as { detail?: unknown }).detail === 'string') {
      expect(page).not.toHaveTextContent((body as { detail: string }).detail);
    }
  });

  it('shows a fixed sentence when the API cannot be reached', async () => {
    render(<ChangePasswordPage />);
    mockFetch.mockRejectedValueOnce(new TypeError(PLANTED));
    fill('OldPass1', 'NewPass12');
    await submit();
    const box = await outcome();
    expect(box).toHaveAttribute('data-outcome', 'network');
    expect(box).toHaveTextContent('The API could not be reached. Check your connection and try again.');
    expect(screen.getByTestId('change-password-page')).not.toHaveTextContent('PLANTED-SERVER-TEXT');
  });

  it('keeps the session on a 403: the token stays and nothing navigates', async () => {
    const assign = jest.spyOn(navigation, 'assign').mockImplementation(() => undefined);
    render(<ChangePasswordPage />);
    respond(403, { detail: 'The current password is incorrect.' });
    fill('Wrong1234', 'NewPass12');
    await submit();
    await outcome();
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('tok');
    expect(assign).not.toHaveBeenCalled();
    assign.mockRestore();
  });

  it('shows no form, only a fixed sentence, to a user who does not sign in locally', () => {
    signIn('cognito');
    render(<ChangePasswordPage />);
    expect(screen.getByTestId('change-password-not-local')).toHaveTextContent(
      'Your account signs in through your identity provider, so its password is changed there, not in Controlvar.',
    );
    expect(screen.queryByTestId('current-password-input')).not.toBeInTheDocument();
    expect(screen.queryByTestId('change-password-submit')).not.toBeInTheDocument();
  });
});
