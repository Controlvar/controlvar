/**
 * The set-up a sign-in asks for when the account's role requires two-step
 * sign-in (#1812, PR 4 of 5): after the right password the answer says
 * `enrol: true`, and the account sets it up in the same card before it has a
 * session: a new secret (QR, the key as text, the otpauth link), the app's
 * first code, then the recovery codes; Done signs it in.
 *
 *   U1  the enrol mfa_token is never stored: nothing in localStorage or
 *       sessionStorage, nothing in the URL, and no access token is stored
 *       until the recovery codes are saved;
 *   U3  (the enrol twins) Done waits for "I have saved these codes", the key
 *       is in the page as text, the QR has its text alternative, and
 *       401/409/423 (both)/503 map to their sentences;
 *   U4  the field's attributes, and a code's leading zeros survive;
 *   axe over each screen.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import LoginPage, { ENROL_RESTART_MESSAGE, enrolStepErrorMessage } from '@/pages/login';
import { AuthProvider } from '@/contexts/AuthContext';
import { ApiError, TOKEN_STORAGE_KEY, UserMe } from '@/services/api';
import {
  CODE_STEP_LOCKED_TEXT,
  CONFIRM_CLOCK_HINT,
  ENROL_STEP_INTRO,
  QR_ALT,
  SIGN_IN_EXPIRED_DETAIL,
  SIGN_IN_TIMED_OUT_MESSAGE,
  UNAVAILABLE_TEXT,
  WRONG_CODE_DETAIL,
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

jest.mock('@/services/twoFactor', () => ({
  ...jest.requireActual('@/services/twoFactor'),
  qrCodeDataUrl: () => Promise.resolve('data:image/gif;base64,R0lGODlhAQABAAAAACw='),
}));

const mockFetch = jest.fn();
global.fetch = mockFetch;

const ENROL_TOKEN = 'mfa.PLANTED-ENROL-TOKEN.value';
const ACCESS = 'full.access.jwt';
const ENROL = '/api/v1/auth/mfa/enroll';
const CONFIRM = '/api/v1/auth/mfa/enroll/confirm';
const SECRET = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP';
const URI = `otpauth://totp/Controlvar:ana@example.com?secret=${SECRET}&issuer=Controlvar&algorithm=SHA1&digits=6&period=30`;
const CODES = ['AAAAABBBBB', 'CCCCCDDDDD', 'EEEEEFFFFF', 'GGGGGHHHHH', 'IIIIIJJJJJ', 'KKKKKLLLLL', 'MMMMMNNNNN', 'OOOOOPPPPP', 'QQQQQRRRRR', 'SSSSSTTTTT'];

const user: UserMe = {
  id: 'u-1',
  email: 'ana@example.com',
  username: 'ana',
  full_name: 'Ana',
  role: 'ADMIN',
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

const ENROL_REQUIRED = { mfa_required: true, mfa_token: ENROL_TOKEN, enrol: true, expires_in: 600 };
const ENROLMENT = { secret: SECRET, otpauth_uri: URI };
const TURNED_ON = {
  recovery_codes: CODES,
  login: { access_token: ACCESS, token_type: 'bearer', expires_in: 43200, user },
};

function callsTo(path: string) {
  return mockFetch.mock.calls.filter(([url]) => String(url).endsWith(path));
}

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

/** The password step answers `enrol: true`; then the secret arrives. */
async function passwordStep(enrolAnswer: Response = respond(200, ENROLMENT)) {
  mockFetch.mockResolvedValueOnce(respond(200, ENROL_REQUIRED));
  mockFetch.mockResolvedValueOnce(enrolAnswer);
  render(
    <AuthProvider>
      <LoginPage />
    </AuthProvider>,
  );
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'ana@example.com' } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'Secret123' } });
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
  });
}

async function sendFirstCode(value: string, answer: Response) {
  mockFetch.mockResolvedValueOnce(answer);
  fireEvent.change(screen.getByTestId('enrol-code-input'), { target: { value } });
  await act(async () => {
    fireEvent.click(screen.getByTestId('enrol-submit'));
  });
}

const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

async function expectNoAxeViolations() {
  const result = await axe.run(document.body, axeOptions);
  expect(result.violations.map((v) => v.id)).toEqual([]);
}

beforeEach(() => {
  mockFetch.mockReset();
  mockReplace.mockClear();
  localStorage.clear();
  sessionStorage.clear();
  delete process.env.NEXT_PUBLIC_API_URL;
});

describe('the set-up a sign-in asks for when the role requires it', () => {
  it('asks the API for a secret with the enrol token and no bearer, and shows it three ways', async () => {
    await passwordStep();
    await screen.findByTestId('enrol-form');
    expect(screen.getByTestId('enrol-heading')).toHaveTextContent('Set up two-step sign-in');
    expect(screen.getByTestId('enrol-intro')).toHaveTextContent(ENROL_STEP_INTRO);

    const [url, init] = callsTo(ENROL)[0];
    expect(url).toBe(ENROL);
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ mfa_token: ENROL_TOKEN });
    expect(init.headers.Authorization).toBeUndefined();

    // U3: the key as text in groups of four, the QR with its alt, the link.
    expect(screen.getByTestId('two-factor-key')).toHaveTextContent('JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP');
    expect(await screen.findByAltText(QR_ALT)).toHaveAttribute('src', expect.stringMatching(/^data:image\/gif/));
    expect(screen.getByTestId('two-factor-otpauth-link')).toHaveAttribute('href', URI);
    await waitFor(() => expect(screen.getByTestId('enrol-heading')).toHaveFocus());
    await expectNoAxeViolations();
  });

  it('U1: the enrol token is never stored, and no token is stored until the codes are saved', async () => {
    const setItem = jest.spyOn(Storage.prototype, 'setItem');
    try {
      await passwordStep();
      await screen.findByTestId('enrol-form');
      expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
      expect(everythingStored()).not.toContain(ENROL_TOKEN);
      expect(window.location.href).not.toContain(ENROL_TOKEN);
      expect(document.body.innerHTML).not.toContain(ENROL_TOKEN);

      await sendFirstCode('123456', respond(200, TURNED_ON));
      await screen.findByTestId('enrol-codes');
      // The codes come first: still nothing stored, nowhere to go yet.
      expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
      expect(mockReplace).not.toHaveBeenCalled();
      expect(setItem.mock.calls.flat().join('\n')).not.toContain(ENROL_TOKEN);
      expect(setItem.mock.calls.flat().join('\n')).not.toContain(ACCESS);
    } finally {
      setItem.mockRestore();
    }
  });

  it('U3: Done waits for "I have saved these codes", then stores the access token and goes on', async () => {
    await passwordStep();
    await screen.findByTestId('enrol-form');
    await sendFirstCode('123456', respond(200, TURNED_ON));

    const [, init] = callsTo(CONFIRM)[0];
    expect(JSON.parse(init.body)).toEqual({ mfa_token: ENROL_TOKEN, code: '123456' });
    expect(init.headers.Authorization).toBeUndefined();

    await screen.findByTestId('enrol-codes');
    expect(screen.getByTestId('enrol-heading')).toHaveTextContent('Save your recovery codes');
    expect(screen.getByTestId('recovery-codes').querySelectorAll('li')).toHaveLength(10);
    expect(screen.getByTestId('recovery-codes')).toHaveTextContent('AAAAA BBBBB');
    const done = screen.getByTestId('recovery-codes-done');
    expect(done).toBeDisabled();
    await act(async () => {
      fireEvent.click(done);
    });
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    await expectNoAxeViolations();

    fireEvent.click(screen.getByLabelText('I have saved these codes'));
    expect(done).toBeEnabled();
    await act(async () => {
      fireEvent.click(done);
    });
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/experiments'));
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe(ACCESS);
    expect(everythingStored()).not.toContain(ENROL_TOKEN);
  });

  it('U4: a text field with the one-time-code hints, and the zeros of 005924 kept', async () => {
    await passwordStep();
    await screen.findByTestId('enrol-form');
    const field = screen.getByTestId('enrol-code-input');
    expect(field).toHaveAttribute('type', 'text');
    expect(field).toHaveAttribute('inputmode', 'numeric');
    expect(field).toHaveAttribute('autocomplete', 'one-time-code');
    expect(screen.getByLabelText('Code from your authenticator app')).toBe(field);
    await sendFirstCode('005924', respond(200, TURNED_ON));
    expect(JSON.parse(callsTo(CONFIRM)[0][1].body).code).toBe('005924');
  });

  it("a wrong first code shows the phone's clock hint and keeps the set-up", async () => {
    await passwordStep();
    await screen.findByTestId('enrol-form');
    await sendFirstCode('123456', respond(401, { detail: WRONG_CODE_DETAIL }, { 'WWW-Authenticate': 'Bearer' }));
    expect(await screen.findByTestId('enrol-error')).toHaveTextContent(CONFIRM_CLOCK_HINT);
    expect(screen.getByTestId('enrol-code-input')).toHaveValue('');
    await waitFor(() => expect(screen.getByTestId('enrol-code-input')).toHaveFocus());
    expect(screen.getByTestId('two-factor-key')).toBeInTheDocument();
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it.each([
    ['the cap', 423, { detail: CODE_STEP_LOCKED_TEXT }, {}, CODE_STEP_LOCKED_TEXT],
    [
      'the address lock',
      423,
      { detail: 'Too many failed login attempts; account temporarily locked. Retry in 42 seconds.' },
      { 'Retry-After': '42' },
      'Too many failed login attempts; account temporarily locked. Retry in 42 seconds.',
    ],
    ['an unreadable secret', 503, { detail: UNAVAILABLE_TEXT }, {}, UNAVAILABLE_TEXT],
    ['a code that is not a code', 422, { detail: [] }, {}, 'Enter the 6-digit code your authenticator app shows.'],
  ])('U3: %s at the first code says so', async (_name, status, body, headers, sentence) => {
    await passwordStep();
    await screen.findByTestId('enrol-form');
    await sendFirstCode('123456', respond(status, body, headers));
    expect(await screen.findByTestId('enrol-error')).toHaveTextContent(sentence);
    expect(screen.getByTestId('enrol-form')).toBeInTheDocument();
  });

  it.each([
    ['an expired token', 401, { detail: SIGN_IN_EXPIRED_DETAIL }, SIGN_IN_TIMED_OUT_MESSAGE],
    ['no secret waiting', 409, { detail: 'No new secret is waiting for its first code.' }, ENROL_RESTART_MESSAGE],
  ])('U3: %s at the first code starts sign-in again', async (_name, status, body, sentence) => {
    await passwordStep();
    await screen.findByTestId('enrol-form');
    await sendFirstCode('123456', respond(status, body, { 'WWW-Authenticate': 'Bearer' }));
    expect(await screen.findByTestId('login-error')).toHaveTextContent(sentence);
    expect(screen.queryByTestId('enrol-step')).toBeNull();
    expect(screen.getByLabelText('Password')).toHaveValue('');
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it('an expired token when the secret is asked for starts sign-in again', async () => {
    await passwordStep(respond(401, { detail: SIGN_IN_EXPIRED_DETAIL }, { 'WWW-Authenticate': 'Bearer' }));
    expect(await screen.findByTestId('login-error')).toHaveTextContent(SIGN_IN_TIMED_OUT_MESSAGE);
    expect(screen.queryByTestId('enrol-step')).toBeNull();
  });

  it('Back to sign-in leaves the set-up and forgets the token', async () => {
    await passwordStep();
    await screen.findByTestId('enrol-form');
    fireEvent.click(screen.getByTestId('enrol-back'));
    await screen.findByTestId('login-form');
    await waitFor(() => expect(screen.getByLabelText('Email')).toHaveFocus());
    expect(document.body.innerHTML).not.toContain(SECRET.slice(0, 8));
    expect(callsTo(CONFIRM)).toHaveLength(0);
  });

  it('an answer without an access token stores nothing', async () => {
    await passwordStep();
    await screen.findByTestId('enrol-form');
    await sendFirstCode('123456', respond(200, { recovery_codes: CODES, login: { user } }));
    expect(await screen.findByTestId('enrol-error')).toBeInTheDocument();
    expect(screen.queryByTestId('enrol-codes')).toBeNull();
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });
});

describe('enrolStepErrorMessage', () => {
  it('reads a wrong first code as the clock hint, and nothing as a wrong password', () => {
    expect(enrolStepErrorMessage(new ApiError({ status: 401, message: 'x', detail: WRONG_CODE_DETAIL }))).toBe(
      CONFIRM_CLOCK_HINT,
    );
    expect(enrolStepErrorMessage(new ApiError({ status: 401, message: 'x', detail: 'Bad' }))).not.toContain(
      'Email or password',
    );
  });
});
