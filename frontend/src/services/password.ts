/**
 * Change your own password: `POST /api/v1/users/me/password` (local sign-in
 * only; the route answers 404 under any other `AUTH_PROVIDER`).
 *
 * The page shows only the fixed sentences below, one per outcome. It never
 * shows the text of a response: the three 403 details are matched to choose a
 * sentence, and every other answer is classed by its status alone.
 */
import { apiFetch, isApiError } from '@/services/api';

export const OWN_PASSWORD_PATH = '/api/v1/users/me/password';

// The 403 details of the route (CURRENT_PASSWORD_INCORRECT_DETAIL,
// CURRENT_PASSWORD_MISSING_DETAIL and NO_LOCAL_PASSWORD_DETAIL in
// backend/app/api/v1/endpoints/users.py). If one changes there, its 403 falls
// back to the generic sentence until it is changed here too.
const DETAIL_INCORRECT = 'The current password is incorrect.';
const DETAIL_MISSING = 'Enter your current password (current_password) to set a new one.';
const DETAIL_NO_PASSWORD =
  'This account does not sign in with a password, so it has no password to change.';

export type PasswordChangeOutcome =
  | 'changed'
  | 'mismatch'
  | 'incorrect'
  | 'missing'
  | 'no-password'
  | 'forbidden'
  | 'weak'
  | 'locked'
  | 'unavailable'
  | 'network'
  | 'failed';

/** The rules, stated on the page before the user types. */
export const PASSWORD_RULES =
  'At least 8 characters and no more than 72 bytes, with an upper-case letter, a lower-case letter and a digit.';

/** What the page says when the signed-in account does not use local sign-in. */
export const NOT_LOCAL_SENTENCE =
  'Your account signs in through your identity provider, so its password is changed there, not in Controlvar.';

const SENTENCES: Record<Exclude<PasswordChangeOutcome, 'locked'>, string> = {
  changed:
    'Your password has been changed. Use the new one the next time you sign in. ' +
    'Anywhere else you are signed in stays signed in until that session expires.',
  mismatch: 'The new password and its confirmation do not match.',
  incorrect: 'The current password is not correct. Check it and try again.',
  missing: 'Enter your current password.',
  'no-password':
    'This account signs in through single sign-on and has no password to change here.',
  forbidden: 'The password could not be changed. Ask an administrator to check your account.',
  weak: `The new password does not meet the rules. ${PASSWORD_RULES}`,
  unavailable: 'This server does not manage passwords, so there is no password to change here.',
  network: 'The API could not be reached. Check your connection and try again.',
  failed: 'The password could not be changed. Try again; if it keeps failing, ask an administrator.',
};

export interface PasswordChangeResult {
  outcome: PasswordChangeOutcome;
  /** Seconds from the 423's `Retry-After` header, when it sent one. */
  retryAfter?: number;
}

/** Send the change. Resolves on 204; throws `ApiError` otherwise. */
export async function changeOwnPassword(currentPassword: string, newPassword: string): Promise<void> {
  await apiFetch<void>(OWN_PASSWORD_PATH, {
    method: 'POST',
    json: { current_password: currentPassword, new_password: newPassword },
  });
}

/** Class a failed change. Reads the status, the header and the three known 403 details only. */
export function classifyPasswordError(err: unknown): PasswordChangeResult {
  if (!isApiError(err)) return { outcome: 'failed' };
  switch (err.status) {
    case 0:
      return { outcome: 'network' };
    case 403:
      if (err.detail === DETAIL_INCORRECT) return { outcome: 'incorrect' };
      if (err.detail === DETAIL_MISSING) return { outcome: 'missing' };
      if (err.detail === DETAIL_NO_PASSWORD) return { outcome: 'no-password' };
      return { outcome: 'forbidden' };
    case 404:
      return { outcome: 'unavailable' };
    case 422:
      return { outcome: 'weak' };
    case 423:
      return { outcome: 'locked', retryAfter: err.retryAfter };
    default:
      return { outcome: 'failed' };
  }
}

/** The fixed sentence for an outcome. */
export function passwordOutcomeSentence(result: PasswordChangeResult): string {
  if (result.outcome === 'locked') {
    const seconds = result.retryAfter;
    if (seconds === undefined) {
      return 'Too many failed attempts. Wait a few minutes and try again.';
    }
    if (seconds >= 120) {
      return `Too many failed attempts. Try again in ${Math.ceil(seconds / 60)} minutes.`;
    }
    return `Too many failed attempts. Try again in ${seconds} ${seconds === 1 ? 'second' : 'seconds'}.`;
  }
  return SENTENCES[result.outcome];
}
