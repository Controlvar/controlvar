/**
 * Two-step sign-in (#1812): an authenticator app's 6-digit code, or a
 * single-use recovery code, after the password. Local sign-in only; under any
 * other `AUTH_PROVIDER` every route here answers 404.
 *
 * The account page (`/account/two-factor`) shows only the fixed sentences
 * below, one per outcome, as the password page does: the status, the header
 * and a few known details choose the sentence, and no other response text
 * reaches the screen. The sign-in page's code step has its own mapper beside
 * `loginErrorMessage` in `pages/login.tsx`.
 *
 * A code is sent exactly as typed: the API drops spaces and hyphens and reads
 * a recovery code in any letter case (`two_factor_service.normalise_code`).
 */
import { apiFetch, isApiError } from '@/services/api';
import { passwordOutcomeSentence } from '@/services/password';
import { demoRefusalMessage } from '@/utils/demoMode';

export const TWO_FACTOR_STATUS_PATH = '/api/v1/users/me/2fa';
export const TWO_FACTOR_ENROL_PATH = '/api/v1/users/me/2fa/enroll';
export const TWO_FACTOR_CONFIRM_PATH = '/api/v1/users/me/2fa/confirm';
export const TWO_FACTOR_DISABLE_PATH = '/api/v1/users/me/2fa/disable';
export const TWO_FACTOR_RECOVERY_CODES_PATH = '/api/v1/users/me/2fa/recovery-codes';
/** The code step of a sign-in: no session yet. */
export const MFA_VERIFY_PATH = '/api/v1/auth/mfa/verify';
/**
 * Setting it up during a sign-in, for an account whose role requires it
 * (`enrol: true` from the password step): no session yet either.
 */
export const MFA_ENROL_PATH = '/api/v1/auth/mfa/enroll';
export const MFA_ENROL_CONFIRM_PATH = '/api/v1/auth/mfa/enroll/confirm';

// ---------------------------------------------------------------------------
// The API's fixed texts, word for word (backend/app/services/two_factor_service.py
// and backend/app/api/two_factor_answers.py). backend/tests/unit/docs/
// test_two_step_sign_in_docs.py holds each to the API's constant: change one
// there and that test names the copy here.
// ---------------------------------------------------------------------------

/** A wrong, used or malformed code: 401 at the code step, 403 when signed in. */
export const WRONG_CODE_DETAIL = 'That code is not correct.';
/** The code step's 401 when the mfa_token is no longer good. */
export const SIGN_IN_EXPIRED_DETAIL = 'Sign-in expired. Start again.';
/** 100 wrong codes in a row: 423 with no Retry-After until a reset. */
export const CODE_STEP_LOCKED_TEXT =
  'Two-step sign-in is locked after too many wrong codes. Ask an administrator to reset it.';
/** The stored secret cannot be read (after SECRET_KEY changed): 503. */
export const UNAVAILABLE_TEXT =
  'Two-step sign-in is unavailable for this account. Ask an administrator to reset it.';
/** Turning it off while your role requires it: 403. */
export const TWO_FACTOR_REQUIRED_FOR_ROLE =
  'Two-step sign-in is required for your role, so it cannot be turned off.';

// ---------------------------------------------------------------------------
// The dashboard's own sentences.
// ---------------------------------------------------------------------------

export const WRONG_CODE_MESSAGE = 'That code is not correct. Check your authenticator app and try again.';
export const WRONG_RECOVERY_CODE_MESSAGE = 'That recovery code is not correct, or it has already been used.';
export const SIGN_IN_TIMED_OUT_MESSAGE = 'Your sign-in timed out. Enter your email and password again.';
/** The one place a phone's clock is mentioned: where a wrong clock is first met. */
export const CONFIRM_CLOCK_HINT =
  "That code did not match. Make sure your phone's clock is set automatically, then try again.";
/** The sign-in page's answer for an account that lost its app and its codes. */
export const LOST_DEVICE_HELP = 'Ask an administrator to reset your two-step sign-in.';
/** The account page under any provider but local. */
export const NOT_LOCAL_TWO_FACTOR =
  'Your account signs in through your identity provider, so two-step sign-in is set up there, not in Controlvar.';
/** The QR image's text alternative: the same key is always written beside it. */
export const QR_ALT = 'QR code for adding Controlvar to an authenticator app. The same key is written below.';

/** The account page while your role requires it: why Turn off is not offered. */
export const REQUIRED_FOR_ROLE_MESSAGE =
  'Your role requires two-step sign-in, so it cannot be turned off. If you lose your authenticator app ' +
  'and your recovery codes, ask an administrator to reset it.';
/** The set-up a sign-in asks for, when the account's role requires it. */
export const ENROL_STEP_INTRO =
  'Your role requires two-step sign-in. Set it up with an authenticator app on your phone to finish signing in.';

export const CODE_HINT = '6 digits, from your authenticator app. Spaces and hyphens are ignored.';
export const RECOVERY_CODE_HINT =
  'One of the recovery codes you saved. Spaces, hyphens and letter case are ignored.';

/** `GET /api/v1/users/me/2fa`. */
export interface TwoFactorStatus {
  enabled: boolean;
  /**
   * Your role must use it (`LOCAL_AUTH_2FA_REQUIRED_ROLES`): it cannot be
   * turned off. False for an account that signs in without a password.
   */
  required: boolean;
  recovery_codes_remaining: number;
}

/** `POST /api/v1/users/me/2fa/enroll`: a new secret, waiting for its first code. */
export interface TwoFactorEnrolment {
  secret: string;
  otpauth_uri: string;
}

/** A code from the app, or one recovery code: exactly one is sent. */
export type SecondStepAnswer = { code: string } | { recovery_code: string };

export function readTwoFactorStatus(): Promise<TwoFactorStatus> {
  return apiFetch<TwoFactorStatus>(TWO_FACTOR_STATUS_PATH);
}

/** A new secret; asking again replaces one that is still waiting. 409 once on. */
export function startTwoFactorEnrolment(): Promise<TwoFactorEnrolment> {
  return apiFetch<TwoFactorEnrolment>(TWO_FACTOR_ENROL_PATH, { method: 'POST' });
}

/**
 * A new secret during a sign-in, for an account whose role requires two-step
 * sign-in: the password step's `mfaToken` (with `enrol: true`). `auth: false`:
 * no session exists yet, so a 401 here clears nothing and redirects nowhere.
 */
export function startSignInEnrolment(mfaToken: string): Promise<TwoFactorEnrolment> {
  return apiFetch<TwoFactorEnrolment>(MFA_ENROL_PATH, {
    method: 'POST',
    json: { mfa_token: mfaToken },
    auth: false,
  });
}

/** Turn it on with the app's first code; resolves with the 10 recovery codes, shown once. */
export async function confirmTwoFactor(code: string): Promise<string[]> {
  const answer = await apiFetch<{ recovery_codes: string[] }>(TWO_FACTOR_CONFIRM_PATH, {
    method: 'POST',
    json: { code },
  });
  return answer.recovery_codes;
}

/** Turn it off with a code or a recovery code (204). */
export async function disableTwoFactor(answer: SecondStepAnswer): Promise<void> {
  await apiFetch<void>(TWO_FACTOR_DISABLE_PATH, { method: 'POST', json: answer });
}

/** A new set of 10 recovery codes for a code from the app; the old set stops working. */
export async function regenerateRecoveryCodes(code: string): Promise<string[]> {
  const answer = await apiFetch<{ recovery_codes: string[] }>(TWO_FACTOR_RECOVERY_CODES_PATH, {
    method: 'POST',
    json: { code },
  });
  return answer.recovery_codes;
}

/** The key as people type it: groups of four, for reading off the screen. */
export function groupKey(secret: string): string {
  return (secret.match(/.{1,4}/g) ?? []).join(' ');
}

/** A recovery code as shown: two groups of five (spaces are ignored when typed back). */
export function groupRecoveryCode(code: string): string {
  return code.length === 10 ? `${code.slice(0, 5)} ${code.slice(5)}` : code;
}

/** The text of the recovery-codes file the page offers to download. */
export function recoveryCodesFileText(codes: string[]): string {
  return (
    'Controlvar recovery codes. Each signs in once, in place of a code from your authenticator app.\n\n' +
    codes.map((code, i) => `${i + 1}. ${groupRecoveryCode(code)}`).join('\n') +
    '\n'
  );
}

interface QrCode {
  addData(data: string): void;
  make(): void;
  createDataURL(cellSize?: number, margin?: number): string;
}
type QrFactory = (typeNumber: 0, errorCorrectionLevel: 'L' | 'M' | 'Q' | 'H') => QrCode;

/**
 * The `otpauth://` link as a QR image (a GIF data URL; the image's CSP allows
 * `data:`). The QR library is loaded only here, so only the two-step pages
 * fetch it. The key and the link are always shown as well: the QR is never the
 * only way to add the account.
 */
export async function qrCodeDataUrl(text: string): Promise<string> {
  const loaded: unknown = await import('qrcode-generator');
  const factory = (
    typeof loaded === 'function' ? loaded : (loaded as { default?: unknown }).default
  ) as QrFactory;
  const qr = factory(0, 'M');
  qr.addData(text);
  qr.make();
  return qr.createDataURL(4, 4);
}

// ---------------------------------------------------------------------------
// The account page's outcomes
// ---------------------------------------------------------------------------

export type TwoFactorOutcome =
  | 'wrong-code'
  | 'wrong-recovery-code'
  | 'clock'
  | 'locked'
  | 'capped'
  | 'unavailable'
  | 'changed'
  | 'not-waiting'
  | 'required'
  | 'invalid'
  | 'session-ended'
  | 'forbidden'
  | 'not-local'
  | 'network'
  | 'timed-out'
  | 'read-only'
  | 'failed';

export interface TwoFactorFailure {
  outcome: TwoFactorOutcome;
  /** Seconds from a 423's `Retry-After`, when it sent one. */
  retryAfter?: number;
  /** For `timed-out` and `read-only`: the dashboard's own sentence, from the error. */
  message?: string;
}

export interface TwoFactorErrorContext {
  /** The answer was a recovery code. */
  recovery?: boolean;
  /** The first code of a new secret (where a phone's wrong clock shows). */
  confirming?: boolean;
}

/**
 * Class a failed request of the account page. Reads the status, the
 * `Retry-After` header and the API's fixed texts only.
 */
export function classifyTwoFactorError(err: unknown, context: TwoFactorErrorContext = {}): TwoFactorFailure {
  if (!isApiError(err)) return { outcome: 'failed' };
  if (err.timedOut) return { outcome: 'timed-out', message: err.message };
  const demo = demoRefusalMessage(err);
  if (demo) return { outcome: 'read-only', message: demo };
  switch (err.status) {
    case 0:
      return { outcome: 'network' };
    case 401:
      return { outcome: 'session-ended' };
    case 403:
      if (err.detail === WRONG_CODE_DETAIL) {
        if (context.confirming) return { outcome: 'clock' };
        return { outcome: context.recovery ? 'wrong-recovery-code' : 'wrong-code' };
      }
      // Turning it off while the role requires it: the page reloads and
      // stops offering it.
      if (err.detail === TWO_FACTOR_REQUIRED_FOR_ROLE) return { outcome: 'required' };
      return { outcome: 'forbidden' };
    case 404:
      return { outcome: 'not-local' };
    case 409:
      // At confirm: the secret is no longer waiting, which is also what an
      // account that is already on answers there.
      return { outcome: context.confirming ? 'not-waiting' : 'changed' };
    case 422:
      return { outcome: 'invalid' };
    case 423:
      // The cap first: it has no Retry-After, and waiting does not end it.
      if (err.detail === CODE_STEP_LOCKED_TEXT) return { outcome: 'capped' };
      return { outcome: 'locked', retryAfter: err.retryAfter };
    case 503:
      if (err.detail === UNAVAILABLE_TEXT) return { outcome: 'unavailable' };
      return { outcome: 'failed' };
    default:
      return { outcome: 'failed' };
  }
}

const SENTENCES: Record<Exclude<TwoFactorOutcome, 'locked' | 'timed-out' | 'read-only'>, string> = {
  'wrong-code': WRONG_CODE_MESSAGE,
  'wrong-recovery-code': WRONG_RECOVERY_CODE_MESSAGE,
  clock: CONFIRM_CLOCK_HINT,
  capped: CODE_STEP_LOCKED_TEXT,
  unavailable: UNAVAILABLE_TEXT,
  changed: 'Two-step sign-in changed since this page was opened. The page now shows how it is.',
  'not-waiting':
    'No new secret is waiting for its first code, or two-step sign-in is already on. The page now shows how it is.',
  required: REQUIRED_FOR_ROLE_MESSAGE,
  invalid: 'Enter the code as your authenticator app shows it, or a recovery code as you saved it.',
  'session-ended': 'Your session has ended. Sign in again.',
  forbidden: 'Two-step sign-in could not be changed. Ask an administrator to check your account.',
  'not-local': 'This server does not manage sign-in, so there is no two-step sign-in to set up here.',
  network: 'The API could not be reached. Check your connection and try again.',
  failed: 'Two-step sign-in could not be changed. Try again; if it keeps failing, ask an administrator.',
};

/** The fixed sentence for a failure. */
export function twoFactorFailureSentence(failure: TwoFactorFailure): string {
  if (failure.outcome === 'locked') {
    // The same lock as the password's: one count of wrong passwords and codes.
    return passwordOutcomeSentence({ outcome: 'locked', retryAfter: failure.retryAfter });
  }
  if (failure.outcome === 'timed-out' || failure.outcome === 'read-only') {
    return failure.message ?? SENTENCES.failed;
  }
  return SENTENCES[failure.outcome];
}
