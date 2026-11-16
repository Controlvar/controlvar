/**
 * Your own two-step sign-in (#1812, PR 3 of 5): `/account/two-factor`.
 *
 *   U3  setting it up: the key in the page as text (grouped), the QR image
 *       with its text alternative, Done held back until "I have saved these
 *       codes" is ticked; 401/403/423 (both)/503 each map to a fixed sentence;
 *   U4  the code field's attributes, and a code's leading zeros survive;
 *   setting it up asks for the current password: a current-password field
 *       whose value goes in the request body only and is cleared once the
 *       API answers; each refusal (wrong, missing, the lock, 429) has its own
 *       sentence, and an account with no password sees why in place of the
 *       form;
 *   axe over every state.
 *
 * No response text reaches the screen: a planted detail never appears.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import TwoFactorPage, {
  CODES_SHOWN_ONCE,
  DISABLED_SENTENCE,
  ENABLED_SENTENCE,
  REGENERATED_SENTENCE,
  STATUS_LOAD_FAILED,
} from '@/pages/account/two-factor';
import { TOKEN_STORAGE_KEY, navigation } from '@/services/api';
import {
  CODE_STEP_LOCKED_TEXT,
  CONFIRM_CLOCK_HINT,
  CURRENT_PASSWORD_INCORRECT_DETAIL,
  ENROL_NO_PASSWORD_DETAIL,
  ENROL_PASSWORD_HINT,
  ENROL_PASSWORD_MESSAGE,
  ENROL_PASSWORD_MISSING_DETAIL,
  NOT_LOCAL_TWO_FACTOR,
  NO_PASSWORD_TWO_FACTOR,
  QR_ALT,
  UNAVAILABLE_TEXT,
  WRONG_CODE_DETAIL,
  WRONG_CODE_MESSAGE,
  WRONG_RECOVERY_CODE_MESSAGE,
  recoveryCodesFileText,
} from '@/services/twoFactor';

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

let mockDemo = false;
jest.mock('@/contexts/AuthConfigContext', () => ({ useDemoMode: () => mockDemo }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockFetch = jest.fn();
global.fetch = mockFetch;

const STATUS = '/api/v1/users/me/2fa';
const ENROL = '/api/v1/users/me/2fa/enroll';
const CONFIRM = '/api/v1/users/me/2fa/confirm';
const DISABLE = '/api/v1/users/me/2fa/disable';
const REGENERATE = '/api/v1/users/me/2fa/recovery-codes';

const SECRET = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP';
const URI = `otpauth://totp/Controlvar:u@example.com?secret=${SECRET}&issuer=Controlvar&algorithm=SHA1&digits=6&period=30`;
const CODES = ['AAAAABBBBB', 'CCCCCDDDDD', 'EEEEEFFFFF', 'GGGGGHHHHH', 'IIIIIJJJJJ', 'KKKKKLLLLL', 'MMMMMNNNNN', 'OOOOOPPPPP', 'QQQQQRRRRR', 'SSSSSTTTTT'];

/** Text a server could send. None of it may appear on the page. */
const PLANTED = 'PLANTED-SERVER-TEXT <b>do not show</b>';

function signIn(authProvider = 'local', status: 'authenticated' | 'loading' = 'authenticated') {
  mockAuth.mockReturnValue({
    status,
    user: {
      id: 'u1',
      email: 'u@example.com',
      username: 'u',
      full_name: null,
      role: 'VIEWER',
      is_superuser: false,
      is_active: true,
      auth_provider: authProvider,
    },
  });
}

function response(status: number, body?: unknown, headers: Record<string, string> = {}): Response {
  const lower: Record<string, string> = { 'content-type': 'application/json' };
  for (const [k, v] of Object.entries(headers)) lower[k.toLowerCase()] = v;
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => lower[name.toLowerCase()] ?? null },
    text: () => Promise.resolve(body === undefined ? '' : JSON.stringify(body)),
  } as unknown as Response;
}

function respond(status: number, body?: unknown, headers: Record<string, string> = {}) {
  mockFetch.mockResolvedValueOnce(response(status, body, headers));
}

const OFF = { enabled: false, required: false, recovery_codes_remaining: 0 };
const ON = { enabled: true, required: false, recovery_codes_remaining: 10 };

function callsTo(path: string) {
  return mockFetch.mock.calls.filter(([url]) => String(url) === path);
}

async function click(testId: string) {
  await act(async () => {
    fireEvent.click(screen.getByTestId(testId));
  });
}

function type(value: string) {
  fireEvent.change(screen.getByTestId('two-factor-code-input'), { target: { value } });
}

async function outcome() {
  return waitFor(() => screen.getByTestId('two-factor-outcome'));
}

async function openOff() {
  respond(200, OFF);
  const view = render(<TwoFactorPage />);
  await screen.findByTestId('two-factor-off');
  return view;
}

async function openOn(status = ON) {
  respond(200, status);
  const view = render(<TwoFactorPage />);
  await screen.findByTestId('two-factor-on');
  return view;
}

/** The account's current password, as typed at the set-up step. */
const CURRENT_PASSWORD = 'Current-Passw0rd-1812';

function typePassword(value: string) {
  fireEvent.change(screen.getByTestId('two-factor-current-password'), { target: { value } });
}

async function startEnrolment() {
  typePassword(CURRENT_PASSWORD);
  respond(200, { secret: SECRET, otpauth_uri: URI });
  await click('two-factor-start');
  await screen.findByTestId('two-factor-enrol');
}

/** Everything in both storages and the URL, for "the password is nowhere". */
function storedAndUrl(): string {
  const values: string[] = [];
  for (const storage of [window.localStorage, window.sessionStorage]) {
    for (let i = 0; i < storage.length; i += 1) {
      const key = storage.key(i) as string;
      values.push(key, storage.getItem(key) ?? '');
    }
  }
  return `${values.join('\n')}\n${window.location.href}`;
}

const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

async function expectNoAxeViolations(container: HTMLElement) {
  const result = await axe.run(container, axeOptions);
  expect(result.violations.map((v) => v.id)).toEqual([]);
}

beforeEach(() => {
  mockFetch.mockReset();
  mockDemo = false;
  localStorage.clear();
  localStorage.setItem(TOKEN_STORAGE_KEY, 'tok');
  signIn('local');
  delete process.env.NEXT_PUBLIC_API_URL;
});

describe('TwoFactorPage: off, and setting it up', () => {
  it('says it is off and offers to set it up', async () => {
    const { container } = await openOff();
    expect(callsTo(STATUS)).toHaveLength(1);
    expect(screen.getByText('Two-step sign-in is off')).toBeInTheDocument();
    expect(screen.getByTestId('two-factor-start')).toHaveTextContent('Set up two-step sign-in');
    await expectNoAxeViolations(container);
  });

  it('U3: shows the key as text in groups of four, the QR with its alt, and the otpauth link', async () => {
    const { container } = await openOff();
    await startEnrolment();
    expect(callsTo(ENROL)[0][1].method).toBe('POST');
    expect(JSON.parse(callsTo(ENROL)[0][1].body)).toEqual({ current_password: CURRENT_PASSWORD });

    const key = screen.getByTestId('two-factor-key');
    expect(key.tagName).toBe('CODE');
    expect(key).toHaveTextContent('JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP');
    const qr = await screen.findByTestId('two-factor-qr');
    expect(qr).toHaveAttribute('alt', QR_ALT);
    expect(qr.getAttribute('src')).toMatch(/^data:image\/gif;base64,/);
    expect(screen.getByRole('img', { name: QR_ALT })).toBe(qr);
    expect(screen.getByTestId('two-factor-otpauth-link')).toHaveAttribute('href', URI);
    expect(screen.getByText('Nothing changes at sign-in until this code is accepted.')).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it('copies the key without its spaces and says so', async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    try {
      await openOff();
      await startEnrolment();
      await click('two-factor-copy-key');
      expect(writeText).toHaveBeenCalledWith(SECRET);
      await waitFor(() => expect(screen.getByTestId('two-factor-copy-status')).toHaveTextContent('Copied.'));
    } finally {
      Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
    }
  });

  it('U4: the code field is text with the one-time-code hints, and keeps the zeros of 005924', async () => {
    await openOff();
    await startEnrolment();
    const field = screen.getByLabelText('Code from your authenticator app');
    expect(field).toBe(screen.getByTestId('two-factor-code-input'));
    expect(field).toHaveAttribute('type', 'text');
    expect(field).toHaveAttribute('inputmode', 'numeric');
    expect(field).toHaveAttribute('autocomplete', 'one-time-code');
    expect(field).not.toHaveAttribute('maxlength', '6');
    type('005924');
    expect(field).toHaveValue('005924');

    respond(200, { recovery_codes: CODES });
    await click('two-factor-confirm');
    expect(JSON.parse(callsTo(CONFIRM)[0][1].body)).toEqual({ code: '005924' });
  });

  it('U3: Done waits for "I have saved these codes", then the page says it is on', async () => {
    const { container } = await openOff();
    await startEnrolment();
    type('123 456');
    respond(200, { recovery_codes: CODES });
    await click('two-factor-confirm');
    await screen.findByTestId('two-factor-codes');

    expect(screen.getByTestId('recovery-codes-warning')).toHaveTextContent(CODES_SHOWN_ONCE);
    const items = Array.from(screen.getByTestId('recovery-codes').querySelectorAll('li'));
    expect(screen.getByTestId('recovery-codes').tagName).toBe('OL');
    expect(items.map((li) => li.textContent)).toEqual(CODES.map((c) => `${c.slice(0, 5)} ${c.slice(5)}`));
    // The secret is gone from the page once it is confirmed.
    expect(document.body.textContent).not.toContain(SECRET);
    await expectNoAxeViolations(container);

    const done = screen.getByTestId('recovery-codes-done');
    expect(done).toBeDisabled();
    fireEvent.click(done);
    expect(screen.getByTestId('two-factor-codes')).toBeInTheDocument();

    const saved = screen.getByLabelText('I have saved these codes');
    expect(saved).toBe(screen.getByTestId('recovery-codes-saved'));
    fireEvent.click(saved);
    expect(done).toBeEnabled();

    respond(200, ON);
    await click('recovery-codes-done');
    await screen.findByTestId('two-factor-on');
    expect(await outcome()).toHaveTextContent(ENABLED_SENTENCE);
    expect(screen.getByTestId('recovery-codes-left')).toHaveTextContent('10 recovery codes left.');
    expect(document.body.textContent).not.toContain(CODES[0]);
  });

  it('offers the codes as a file, each grouped, never the secret', async () => {
    const createObjectURL = jest.fn(() => 'blob:codes');
    const revokeObjectURL = jest.fn();
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    const clickSpy = jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      await openOff();
      await startEnrolment();
      type('123456');
      respond(200, { recovery_codes: CODES });
      await click('two-factor-confirm');
      await screen.findByTestId('two-factor-codes');
      await click('recovery-codes-download');
      expect(createObjectURL).toHaveBeenCalledTimes(1);
      expect(clickSpy).toHaveBeenCalledTimes(1);
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:codes');
      const text = recoveryCodesFileText(CODES);
      expect(text).toContain('1. AAAAA BBBBB');
      expect(text).toContain('10. SSSSS TTTTT');
      expect(text).not.toContain(SECRET);
    } finally {
      clickSpy.mockRestore();
    }
  });

  it("a wrong first code shows the phone's clock hint and keeps the secret waiting", async () => {
    await openOff();
    await startEnrolment();
    type('111111');
    respond(403, { detail: WRONG_CODE_DETAIL });
    await click('two-factor-confirm');
    expect(await outcome()).toHaveTextContent(CONFIRM_CLOCK_HINT);
    expect(screen.getByTestId('two-factor-enrol')).toBeInTheDocument();
    expect(screen.getByTestId('two-factor-key')).toBeInTheDocument();
    expect(screen.getByTestId('two-factor-code-input')).toHaveValue('');
    expect(screen.getByTestId('two-factor-code-input')).toHaveAttribute('aria-invalid', 'true');
  });

  it('a first code for an account that is already on (409 at confirm) says so and shows it on', async () => {
    await openOff();
    await startEnrolment();
    type('123456');
    respond(409, { detail: PLANTED });
    respond(200, ON);
    await click('two-factor-confirm');
    await screen.findByTestId('two-factor-on');
    expect(await outcome()).toHaveTextContent(
      'No new secret is waiting for its first code, or two-step sign-in is already on. The page now shows how it is.',
    );
    expect(document.body.innerHTML).not.toContain('PLANTED-SERVER-TEXT');
    expect(document.body.textContent).not.toContain('JBSW');
  });

  it('Cancel goes back to the off state', async () => {
    await openOff();
    await startEnrolment();
    fireEvent.click(screen.getByTestId('two-factor-cancel'));
    expect(screen.getByTestId('two-factor-off')).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('JBSW');
  });
});

describe('TwoFactorPage: setting it up asks for the current password', () => {
  it('the field is a labelled current-password field, with why it is asked for', async () => {
    const { container } = await openOff();
    const field = screen.getByLabelText('Current password');
    expect(field).toBe(screen.getByTestId('two-factor-current-password'));
    expect(field).toHaveAttribute('type', 'password');
    expect(field).toHaveAttribute('autocomplete', 'current-password');
    expect(field).toHaveAccessibleDescription(ENROL_PASSWORD_HINT);
    await expectNoAxeViolations(container);
  });

  it('with no password typed, it says so and sends nothing', async () => {
    const { container } = await openOff();
    await click('two-factor-start');
    expect(await outcome()).toHaveTextContent(ENROL_PASSWORD_MESSAGE);
    expect(callsTo(ENROL)).toHaveLength(0);
    expect(screen.getByTestId('two-factor-current-password')).toHaveAttribute('aria-invalid', 'true');
    await expectNoAxeViolations(container);
  });

  it('U1: the password is sent in the body only, never kept in storage or the URL, and cleared once answered', async () => {
    await openOff();
    await startEnrolment();
    expect(String(callsTo(ENROL)[0][0])).not.toContain(CURRENT_PASSWORD);
    expect(storedAndUrl()).not.toContain(CURRENT_PASSWORD);
    expect(document.body.innerHTML).not.toContain(CURRENT_PASSWORD);
    fireEvent.click(screen.getByTestId('two-factor-cancel'));
    expect(screen.getByTestId('two-factor-current-password')).toHaveValue('');
  });

  it.each([
    [
      'a wrong current password (403)',
      403,
      CURRENT_PASSWORD_INCORRECT_DETAIL,
      {},
      'The current password is not correct. Check it and try again.',
      true,
    ],
    ['no current password (403)', 403, ENROL_PASSWORD_MISSING_DETAIL, {}, ENROL_PASSWORD_MESSAGE, true],
    [
      'the address lock (423 with Retry-After)',
      423,
      PLANTED,
      { 'Retry-After': '840' },
      'Too many failed attempts. Try again in 14 minutes.',
      false,
    ],
    ['too many attempts (429)', 429, PLANTED, { 'Retry-After': '60' }, 'Too many attempts. Wait a minute, then try again.', false],
    ['another 403', 403, PLANTED, {}, 'Two-step sign-in could not be changed. Ask an administrator to check your account.', false],
  ])(
    '%s at set-up shows its own sentence, keeps the form, and clears the password',
    async (_name, status, detail, headers, expected, fieldInvalid) => {
      const { container } = await openOff();
      typePassword(CURRENT_PASSWORD);
      respond(status, { detail }, headers);
      await click('two-factor-start');
      expect(await outcome()).toHaveTextContent(expected);
      expect(document.body.innerHTML).not.toContain('PLANTED-SERVER-TEXT');
      expect(screen.getByTestId('two-factor-off')).toBeInTheDocument();
      const field = screen.getByTestId('two-factor-current-password');
      expect(field).toHaveValue('');
      if (fieldInvalid) {
        expect(field).toHaveAttribute('aria-invalid', 'true');
      } else {
        expect(field).not.toHaveAttribute('aria-invalid');
      }
      await expectNoAxeViolations(container);
    },
  );

  it('an account with no password of its own sees why, in place of the form, with no retry', async () => {
    const { container } = await openOff();
    typePassword(CURRENT_PASSWORD);
    respond(403, { detail: ENROL_NO_PASSWORD_DETAIL });
    await click('two-factor-start');
    const text = await screen.findByTestId('two-factor-no-password-text');
    expect(text).toHaveTextContent(NO_PASSWORD_TWO_FACTOR);
    expect(screen.queryByTestId('two-factor-off')).not.toBeInTheDocument();
    expect(screen.queryByTestId('two-factor-start')).not.toBeInTheDocument();
    expect(screen.queryByTestId('two-factor-current-password')).not.toBeInTheDocument();
    expect(screen.queryByTestId('two-factor-outcome')).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain('could not be changed');
    expect(callsTo(ENROL)).toHaveLength(1);
    await expectNoAxeViolations(container);
  });
});

describe('TwoFactorPage: on', () => {
  it('says how many recovery codes are left', async () => {
    const { container } = await openOn({ ...ON, recovery_codes_remaining: 1 });
    expect(screen.getByTestId('recovery-codes-left')).toHaveTextContent('1 recovery code left.');
    await expectNoAxeViolations(container);
  });

  it('says when none are left', async () => {
    await openOn({ ...ON, recovery_codes_remaining: 0 });
    expect(screen.getByTestId('recovery-codes-left')).toHaveTextContent('No recovery codes left. Make a new set below.');
  });

  it('turns it off with a code', async () => {
    const { container } = await openOn();
    fireEvent.click(screen.getByTestId('two-factor-open-disable'));
    await waitFor(() => expect(screen.getByTestId('two-factor-code-input')).toHaveFocus());
    await expectNoAxeViolations(container);
    type('654321');
    respond(204);
    respond(200, OFF);
    await click('two-factor-submit');
    expect(JSON.parse(callsTo(DISABLE)[0][1].body)).toEqual({ code: '654321' });
    await screen.findByTestId('two-factor-off');
    expect(await outcome()).toHaveTextContent(DISABLED_SENTENCE);
  });

  it('turns it off with a recovery code, and says so when it is wrong', async () => {
    await openOn();
    fireEvent.click(screen.getByTestId('two-factor-open-disable'));
    fireEvent.click(screen.getByTestId('two-factor-switch'));
    const field = screen.getByLabelText('Recovery code');
    expect(field).toHaveAttribute('autocomplete', 'off');
    type('abcde-fghij');
    respond(403, { detail: WRONG_CODE_DETAIL });
    await click('two-factor-submit');
    expect(JSON.parse(callsTo(DISABLE)[0][1].body)).toEqual({ recovery_code: 'abcde-fghij' });
    expect(await outcome()).toHaveTextContent(WRONG_RECOVERY_CODE_MESSAGE);
  });

  it('makes a new set of recovery codes, behind the same saved-codes check', async () => {
    await openOn();
    fireEvent.click(screen.getByTestId('two-factor-open-regenerate'));
    type('222222');
    respond(200, { recovery_codes: CODES });
    await click('two-factor-submit');
    expect(JSON.parse(callsTo(REGENERATE)[0][1].body)).toEqual({ code: '222222' });
    await screen.findByTestId('two-factor-codes');
    expect(screen.getByTestId('recovery-codes-done')).toBeDisabled();
    fireEvent.click(screen.getByTestId('recovery-codes-saved'));
    respond(200, ON);
    await click('recovery-codes-done');
    expect(await outcome()).toHaveTextContent(REGENERATED_SENTENCE);
  });

  it.each([
    ['a wrong code (403)', 403, WRONG_CODE_DETAIL, {}, WRONG_CODE_MESSAGE],
    ['the cap (423, no Retry-After)', 423, CODE_STEP_LOCKED_TEXT, {}, CODE_STEP_LOCKED_TEXT],
    [
      'the address lock (423 with Retry-After)',
      423,
      PLANTED,
      { 'Retry-After': '840' },
      'Too many failed attempts. Try again in 14 minutes.',
    ],
    ['an unreadable secret (503)', 503, UNAVAILABLE_TEXT, {}, UNAVAILABLE_TEXT],
    ['a session that ended (401)', 401, PLANTED, {}, 'Your session has ended. Sign in again.'],
    ['another 403', 403, PLANTED, {}, 'Two-step sign-in could not be changed. Ask an administrator to check your account.'],
    ['another 503', 503, PLANTED, {}, 'Two-step sign-in could not be changed. Try again; if it keeps failing, ask an administrator.'],
  ])('U3: %s shows its fixed sentence and no response text', async (_name, status, detail, headers, expected) => {
    const assign = jest.spyOn(navigation, 'assign').mockImplementation(() => undefined);
    try {
      await openOn();
      fireEvent.click(screen.getByTestId('two-factor-open-regenerate'));
      type('333333');
      respond(status, { detail }, headers);
      await click('two-factor-submit');
      expect(await outcome()).toHaveTextContent(expected);
      expect(document.body.innerHTML).not.toContain('PLANTED-SERVER-TEXT');
    } finally {
      assign.mockRestore();
    }
  });

  it('409: the page was out of date, so it says so and shows the state as it is', async () => {
    await openOn();
    fireEvent.click(screen.getByTestId('two-factor-open-disable'));
    type('444444');
    respond(409, { detail: PLANTED });
    respond(200, OFF);
    await click('two-factor-submit');
    await screen.findByTestId('two-factor-off');
    expect(await outcome()).toHaveTextContent(
      'Two-step sign-in changed since this page was opened. The page now shows how it is.',
    );
    expect(document.body.innerHTML).not.toContain('PLANTED-SERVER-TEXT');
  });
});

describe('TwoFactorPage: elsewhere', () => {
  it('waits for the session', () => {
    signIn('local', 'loading');
    render(<TwoFactorPage />);
    expect(screen.getByTestId('two-factor-loading')).toBeInTheDocument();
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('under another provider, says where it is set up and asks the API nothing', async () => {
    signIn('cognito');
    const { container } = render(<TwoFactorPage />);
    expect(screen.getByTestId('two-factor-not-local')).toHaveTextContent(NOT_LOCAL_TWO_FACTOR);
    expect(mockFetch).not.toHaveBeenCalled();
    await expectNoAxeViolations(container);
  });

  it('in a read-only demo, shows the demo note and asks the API nothing', () => {
    mockDemo = true;
    render(<TwoFactorPage />);
    expect(screen.getByTestId('demo-note')).toHaveTextContent(
      'Setting up two-step sign-in is turned off in this read-only demo.',
    );
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('a status that cannot be read says so', async () => {
    respond(500, { detail: PLANTED });
    render(<TwoFactorPage />);
    expect(await screen.findByTestId('two-factor-load-failed')).toHaveTextContent(STATUS_LOAD_FAILED);
    expect(document.body.innerHTML).not.toContain('PLANTED-SERVER-TEXT');
  });
});
