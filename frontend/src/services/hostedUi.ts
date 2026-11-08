/**
 * Signing in through the Cognito Hosted UI (#708): the browser half.
 *
 * - {@link beginHostedUiSignIn} makes the sign-in's `state`, PKCE verifier
 *   (S256) and `nonce`, keeps them with the page to open afterwards in ONE
 *   `sessionStorage` entry that lives ten minutes, and sends the browser to
 *   `https://<hosted_ui_domain>/oauth2/authorize`.
 * - `/auth/callback` (`pages/auth/callback.tsx`) takes that entry back with
 *   {@link takePendingSignIn}, which removes it before anything else, and
 *   hands the code and the verifier to the API's
 *   `POST /api/v1/auth/cognito/exchange`. The browser never calls Cognito's
 *   token endpoint; the dashboard's Content-Security-Policy would refuse it.
 * - {@link hostedUiLogoutUrl} is Cognito's `/logout`, which ends the Hosted
 *   UI's own session. Its `logout_uri` is always this origin's `/login`.
 *
 * What the sign-in page says after a redirect it cannot carry in the URL
 * (Cognito's `/logout` returns to `logout_uri` exactly) is a notice in
 * `sessionStorage`: a fixed code, never text from a URL.
 */

import type { AuthConfig } from '@/services/authConfig';
import { ApiError, navigation, safeNextPath } from '@/services/api';

type CognitoConfig = Extract<AuthConfig, { kind: 'cognito' }>;

export const CALLBACK_PATH = '/auth/callback';
export const AFTER_SIGN_IN_DEFAULT = '/experiments';

export const PENDING_SIGN_IN_KEY = 'controlvar.hostedUi.pending';
export const SIGN_IN_NOTICE_KEY = 'controlvar.hostedUi.notice';
export const SILENT_SIGN_IN_KEY = 'controlvar.hostedUi.silent';

/** How long a started sign-in can be finished. */
export const PENDING_SIGN_IN_TTL_MS = 10 * 60 * 1000;

/** How long after one silent re-sign-in another is refused. */
export const SILENT_SIGN_IN_WINDOW_MS = 10 * 60 * 1000;

const STATE_BYTES = 32;
const VERIFIER_BYTES = 64;
const NONCE_BYTES = 32;

/** What the sign-in page can be told; each code has one fixed sentence there. */
export type SignInNoticeCode =
  | 'signed_out'
  | 'cancelled'
  | 'expired'
  | 'refused'
  | 'inactive'
  | 'rate_limited'
  | 'unreachable'
  | 'timed_out'
  | 'server_error';

const NOTICE_CODES: ReadonlySet<string> = new Set<SignInNoticeCode>([
  'signed_out',
  'cancelled',
  'expired',
  'refused',
  'inactive',
  'rate_limited',
  'unreachable',
  'timed_out',
  'server_error',
]);

export interface SignInNotice {
  code: SignInNoticeCode;
  /** The page the next sign-in opens. */
  next: string;
  /** For `server_error`: the HTTP status. */
  status?: number;
}

export interface PendingSignIn {
  state: string;
  verifier: string;
  nonce: string;
  next: string;
  /** `Date.now()` when the sign-in started. */
  ts: number;
}

// ---------------------------------------------------------------------------
// Where a sign-in may land
// ---------------------------------------------------------------------------

/**
 * `next` when it is a path on this origin with nothing unusual in it, else
 * `/experiments`. On top of `safeNextPath` (no scheme, no `//`, no `/\`), a
 * backslash, a control character or its percent-encoding anywhere is refused.
 */
export function safeAfterSignInPath(next: string | null | undefined): string {
  const fallback = AFTER_SIGN_IN_DEFAULT;
  if (typeof next !== 'string' || next.length > 2048) return fallback;
  const safe = safeNextPath(next, fallback);
  if (safe === fallback) return fallback;
  if (!/^\/[A-Za-z0-9\-._~!$&'()*+,;=:@/?#%]*$/.test(safe)) return fallback;
  if (/%(0[0-9a-f]|1[0-9a-f]|7f|5c)/i.test(safe)) return fallback;
  let decoded: string;
  try {
    decoded = decodeURIComponent(safe);
  } catch {
    return fallback;
  }
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f\\]/.test(decoded) || decoded.startsWith('//')) return fallback;
  return safe;
}

// ---------------------------------------------------------------------------
// state, PKCE, nonce
// ---------------------------------------------------------------------------

function base64Url(bytes: Uint8Array): string {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function webCrypto(): Crypto | undefined {
  return typeof globalThis !== 'undefined' ? globalThis.crypto : undefined;
}

/** `bytes` random bytes, base64url without padding. */
export function randomToken(bytes: number): string {
  const crypto = webCrypto();
  if (!crypto?.getRandomValues) throw new Error('No secure random number generator');
  const buffer = new Uint8Array(bytes);
  crypto.getRandomValues(buffer);
  return base64Url(buffer);
}

/** The S256 `code_challenge` of a verifier (RFC 7636 section 4.2). */
export async function codeChallenge(verifier: string): Promise<string> {
  const subtle = webCrypto()?.subtle;
  if (!subtle) throw new Error('No Web Crypto digest');
  // A verifier is ASCII (base64url), so its UTF-8 bytes are its char codes.
  const ascii = new Uint8Array(verifier.length);
  for (let i = 0; i < verifier.length; i += 1) ascii[i] = verifier.charCodeAt(i) & 0x7f;
  const digest = await subtle.digest('SHA-256', ascii);
  return base64Url(new Uint8Array(digest));
}

/**
 * True when this page can make a sign-in: Web Crypto's digest is there only
 * in a secure context (https, or http://localhost).
 */
export function canStartHostedUiSignIn(): boolean {
  const crypto = webCrypto();
  return !!crypto?.getRandomValues && !!crypto.subtle;
}

// ---------------------------------------------------------------------------
// URLs
// ---------------------------------------------------------------------------

function origin(): string {
  return typeof window !== 'undefined' && window.location ? window.location.origin : '';
}

/** The callback the Hosted UI returns to: this origin's `/auth/callback`, exactly. */
export function hostedUiRedirectUri(): string {
  return `${origin()}${CALLBACK_PATH}`;
}

export function hostedUiAuthorizeUrl(
  config: CognitoConfig,
  params: { state: string; challenge: string; nonce: string },
): string {
  const query = new URLSearchParams({
    response_type: 'code',
    client_id: config.clientId,
    redirect_uri: hostedUiRedirectUri(),
    scope: config.scopes.join(' '),
    state: params.state,
    code_challenge: params.challenge,
    code_challenge_method: 'S256',
    nonce: params.nonce,
  });
  return `https://${config.hostedUiDomain}/oauth2/authorize?${query.toString()}`;
}

/** Cognito's `/logout`, returning to this origin's `/login`. */
export function hostedUiLogoutUrl(config: CognitoConfig): string {
  const query = new URLSearchParams({
    client_id: config.clientId,
    logout_uri: `${origin()}/login`,
  });
  return `https://${config.hostedUiDomain}/logout?${query.toString()}`;
}

// ---------------------------------------------------------------------------
// sessionStorage
// ---------------------------------------------------------------------------

function storage(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function readJson(key: string): unknown {
  const store = storage();
  if (!store) return null;
  try {
    const raw = store.getItem(key);
    return raw === null ? null : (JSON.parse(raw) as unknown);
  } catch {
    return null;
  }
}

function write(key: string, value: unknown): boolean {
  const store = storage();
  if (!store) return false;
  try {
    store.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

function remove(key: string): void {
  try {
    storage()?.removeItem(key);
  } catch {
    /* ignore */
  }
}

function isString(value: unknown, max = 512): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= max;
}

/**
 * Take the started sign-in out of `sessionStorage`: the entry is read and
 * then removed straight away, in the same synchronous step and whatever it
 * holds, so it can be used once at most. It is returned only when it is
 * whole and younger than ten minutes.
 */
export function takePendingSignIn(now: number = Date.now()): PendingSignIn | null {
  const value = readJson(PENDING_SIGN_IN_KEY);
  remove(PENDING_SIGN_IN_KEY);
  if (!value || typeof value !== 'object') return null;
  const entry = value as Record<string, unknown>;
  if (
    !isString(entry.state) ||
    !isString(entry.verifier, 128) ||
    !isString(entry.nonce) ||
    typeof entry.next !== 'string' ||
    typeof entry.ts !== 'number' ||
    !Number.isFinite(entry.ts)
  ) {
    return null;
  }
  const age = now - entry.ts;
  if (age < 0 || age > PENDING_SIGN_IN_TTL_MS) return null;
  return {
    state: entry.state,
    verifier: entry.verifier,
    nonce: entry.nonce,
    next: safeAfterSignInPath(entry.next),
    ts: entry.ts,
  };
}

/**
 * The notice for a sign-in request (the exchange, or the account read after
 * it) that failed for a reason other than a refusal: a 429, no answer or a
 * time-out, or an error status.
 */
export function signInFailureNotice(err: unknown, next: string): SignInNotice {
  if (!(err instanceof ApiError)) return { code: 'server_error', next };
  if (err.status === 429) return { code: 'rate_limited', next };
  if (err.isNetworkError) return { code: err.timedOut ? 'timed_out' : 'unreachable', next };
  if (err.status >= 400 && err.status <= 599) return { code: 'server_error', next, status: err.status };
  return { code: 'server_error', next };
}

export function writeSignInNotice(notice: SignInNotice): void {
  write(SIGN_IN_NOTICE_KEY, { ...notice, next: safeAfterSignInPath(notice.next) });
}

/** Take the notice for the sign-in page, once. Only a known code is returned. */
export function takeSignInNotice(): SignInNotice | null {
  const value = readJson(SIGN_IN_NOTICE_KEY);
  remove(SIGN_IN_NOTICE_KEY);
  if (!value || typeof value !== 'object') return null;
  const entry = value as Record<string, unknown>;
  if (typeof entry.code !== 'string' || !NOTICE_CODES.has(entry.code)) return null;
  const notice: SignInNotice = {
    code: entry.code as SignInNoticeCode,
    next: safeAfterSignInPath(typeof entry.next === 'string' ? entry.next : null),
  };
  if (
    typeof entry.status === 'number' &&
    Number.isInteger(entry.status) &&
    entry.status >= 400 &&
    entry.status <= 599
  ) {
    notice.status = entry.status;
  }
  return notice;
}

// ---------------------------------------------------------------------------
// Starting a sign-in
// ---------------------------------------------------------------------------

/**
 * Start a sign-in: keep a new state, verifier and nonce with `next`, then
 * send the browser to the Hosted UI. Rejects, sending nothing, when this page
 * cannot make a verifier or keep it.
 */
export async function beginHostedUiSignIn(config: CognitoConfig, next: string): Promise<void> {
  const state = randomToken(STATE_BYTES);
  const verifier = randomToken(VERIFIER_BYTES);
  const nonce = randomToken(NONCE_BYTES);
  const challenge = await codeChallenge(verifier);
  const entry: PendingSignIn = { state, verifier, nonce, next: safeAfterSignInPath(next), ts: Date.now() };
  if (!write(PENDING_SIGN_IN_KEY, entry)) throw new Error('sessionStorage is not available');
  navigation.assign(hostedUiAuthorizeUrl(config, { state, challenge, nonce }));
}

// ---------------------------------------------------------------------------
// The silent re-sign-in, and its guard
// ---------------------------------------------------------------------------

/**
 * Whether a silent re-sign-in may start now: at most one in any ten
 * minutes. The Hosted UI signs a user in again without a page while its own
 * session lasts, so a user the API keeps refusing would otherwise be sent
 * round for ever.
 */
export function silentSignInAllowed(now: number = Date.now()): boolean {
  const last = readJson(SILENT_SIGN_IN_KEY);
  if (typeof last !== 'number' || !Number.isFinite(last)) return true;
  return now - last < 0 || now - last > SILENT_SIGN_IN_WINDOW_MS;
}

/** Record a silent re-sign-in, then start it. */
export async function beginSilentSignIn(config: CognitoConfig, next: string): Promise<void> {
  write(SILENT_SIGN_IN_KEY, Date.now());
  await beginHostedUiSignIn(config, next);
}

/**
 * End a refused sign-in: say why on the sign-in page, then end the Hosted
 * UI's session through Cognito's `/logout`, so its cookie cannot sign the
 * same user in again without a page.
 */
export function endRefusedSignIn(config: CognitoConfig, notice: SignInNotice): void {
  writeSignInNotice(notice);
  navigation.assign(hostedUiLogoutUrl(config));
}
