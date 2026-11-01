/**
 * Sign in with SSO from the dashboard (C2b, #66).
 *
 * The flow, and what each half holds:
 *
 *   1. `/login`: the dashboard makes a 32-byte `secret` (Web Crypto), keeps it
 *      in this tab's `sessionStorage["experimently.sso"]` with `{next, domain}`,
 *      and navigates (`location.assign`, never a `<form action>` — nginx's CSP
 *      has `form-action 'self'`) to `GET /api/v1/auth/sso/login?domain=&
 *      return_to=&handoff=`, where `handoff` is base64url(SHA-256(secret)).
 *   2. The API sends the browser to the provider and back to its callback,
 *      which redirects to `{return_to}/sso/complete#code=<hand-off code>` — a
 *      60-second code bound to `handoff`, never the access token — or to
 *      `{return_to}/login?sso_error=<code>`.
 *   3. `/sso/complete` POSTs `{code, secret}` to `/api/v1/auth/sso/exchange`
 *      and gets the session a password login returns.
 *
 * The code stays in the browser's history; it is dead after 60 s and useless
 * without the secret, which leaves this tab only in the exchange body.
 */
import {
  ApiError,
  UserMe,
  apiFetch,
  apiBase,
  apiUrl,
  navigation,
  signInTimeoutMessage,
} from '@/services/api';

export const SSO_LOGIN_PATH = '/api/v1/auth/sso/login';
export const SSO_EXCHANGE_PATH = '/api/v1/auth/sso/exchange';
export const SSO_STORAGE_KEY = 'experimently.sso';
export const SSO_COMPLETE_PATH = '/sso/complete';

/** What `/sso/complete` and `/login?sso_error` need back from `/login`. */
export interface SsoPending {
  secret: string;
  next: string;
  domain: string;
}

export interface SsoExchangeResponse {
  access_token: string;
  token_type: string;
  user: UserMe;
}

// ---------------------------------------------------------------------------
// The hand-off encoding (spec-v3 §1)
// ---------------------------------------------------------------------------

/** Unpadded base64url of `bytes`. */
export function base64Url(bytes: Uint8Array): string {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** The bytes an unpadded base64url string encodes. */
export function fromBase64Url(value: string): Uint8Array<ArrayBuffer> {
  const b64 = value.replace(/-/g, '+').replace(/_/g, '/');
  const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4);
  const binary = atob(padded);
  const out = new Uint8Array(new ArrayBuffer(binary.length));
  for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i);
  return out;
}

/** 32 random bytes, as the 43-character secret. */
export function newSecret(): string {
  const bytes = new Uint8Array(32);
  globalThis.crypto.getRandomValues(bytes);
  return base64Url(bytes);
}

/** `handoff`: base64url(SHA-256(the 32 decoded bytes)) — the bytes, not the text. */
export async function handoffHash(secret: string): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest('SHA-256', fromBase64Url(secret));
  return base64Url(new Uint8Array(digest));
}

// ---------------------------------------------------------------------------
// sessionStorage
// ---------------------------------------------------------------------------

export function savePending(pending: SsoPending): void {
  try {
    window.sessionStorage.setItem(SSO_STORAGE_KEY, JSON.stringify(pending));
  } catch {
    /* storage unavailable: the exchange will be refused and say so */
  }
}

export function readPending(): SsoPending | null {
  try {
    const raw = window.sessionStorage.getItem(SSO_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<SsoPending>;
    if (typeof parsed.secret !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(parsed.secret)) {
      return null;
    }
    return {
      secret: parsed.secret,
      next: typeof parsed.next === 'string' ? parsed.next : '/',
      domain: typeof parsed.domain === 'string' ? parsed.domain : '',
    };
  } catch {
    return null;
  }
}

export function clearPending(): void {
  try {
    window.sessionStorage.removeItem(SSO_STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

// ---------------------------------------------------------------------------
// Starting a sign-in
// ---------------------------------------------------------------------------

/** The domain of a work email, lower-cased, or null if it has none the API would accept. */
export function emailDomain(email: string): string | null {
  const at = email.trim().lastIndexOf('@');
  if (at <= 0) return null;
  const domain = email.trim().slice(at + 1).toLowerCase();
  return /^[a-z0-9.-]{1,253}$/.test(domain) && domain.includes('.') ? domain : null;
}

/**
 * Keep the secret, then navigate to the API's `/login`. Full-page navigation
 * through `navigation.assign` (`window.location.assign`).
 */
export async function startSsoSignIn(email: string, next: string): Promise<void> {
  const domain = emailDomain(email);
  if (!domain) throw new Error('Enter your work email address, e.g. you@example.com.');
  const secret = newSecret();
  const handoff = await handoffHash(secret);
  savePending({ secret, next, domain });
  navigation.assign(
    apiUrl(SSO_LOGIN_PATH, { domain, return_to: window.location.origin, handoff }),
  );
}

// ---------------------------------------------------------------------------
// /sso/complete
// ---------------------------------------------------------------------------

/** `#code=<JWT-shaped, at most 2048 characters>`, else null. */
export function codeFromHash(hash: string): string | null {
  const match = /^#code=([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)$/.exec(hash);
  if (!match || match[1].length > 2048) return null;
  return match[1];
}

export async function exchangeSsoCode(code: string, secret: string): Promise<SsoExchangeResponse> {
  return apiFetch<SsoExchangeResponse>(SSO_EXCHANGE_PATH, {
    method: 'POST',
    json: { code, secret },
    auth: false,
    redirectOn401: false,
  });
}

/** Where a failed exchange sends the browser: `/login?sso_error=...`. */
export function exchangeFailurePath(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.timedOut) return loginErrorPath(SSO_TIMED_OUT);
    if (err.status === 0) return loginErrorPath(SSO_UNREACHABLE);
    if (err.status === 429) return loginErrorPath(SSO_RATE_LIMITED);
    if (err.status >= 500) return loginErrorPath('sso_failed', { request_id: err.requestId });
  }
  return loginErrorPath('sso_state');
}

// ---------------------------------------------------------------------------
// /login?sso_error=...
// ---------------------------------------------------------------------------

/** Dashboard-only codes: the exchange's 429, an unreachable API and a timed-out exchange. */
export const SSO_RATE_LIMITED = 'sso_rate_limited';
export const SSO_UNREACHABLE = 'sso_unreachable';
export const SSO_TIMED_OUT = 'sso_timed_out';

export const SSO_ERROR_PARAMS = ['sso_error', 'provider', 'idp_error', 'request_id'] as const;

export interface SsoErrorParams {
  sso_error: string;
  provider?: string;
  idp_error?: string;
  request_id?: string;
}

export function loginErrorPath(code: string, extra: Partial<Omit<SsoErrorParams, 'sso_error'>> = {}): string {
  const params = new URLSearchParams({ sso_error: code });
  for (const [key, value] of Object.entries(extra)) {
    if (value) params.set(key, value);
  }
  return `/login?${params.toString()}`;
}

const PROVIDER_NAMES: Record<string, string> = {
  okta: 'Okta',
  google: 'Google',
  github: 'GitHub',
  microsoft: 'Microsoft',
  azure_ad: 'Azure AD',
  onelogin: 'OneLogin',
};

/**
 * The four parameters from a query, each checked the way the API writes it:
 * a provider from the enum, an OAuth error code, an id-shaped request id.
 * Anything else is dropped — the URL can be hand-made, and none of it is
 * shown unless it has the shape the API would have given it.
 */
export function readSsoErrorParams(
  query: Record<string, string | string[] | undefined>,
): SsoErrorParams | null {
  const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);
  const code = first(query.sso_error);
  if (!code) return null;
  const out: SsoErrorParams = { sso_error: /^[a-z_]{1,64}$/.test(code) ? code : 'unknown' };
  const provider = first(query.provider);
  if (provider && Object.prototype.hasOwnProperty.call(PROVIDER_NAMES, provider)) {
    out.provider = provider;
  }
  const idp = first(query.idp_error);
  if (idp && /^[a-z_]{1,64}$/.test(idp)) out.idp_error = idp;
  const id = first(query.request_id);
  if (id && /^[A-Za-z0-9._:-]{1,128}$/.test(id)) out.request_id = id;
  return out;
}

/** The host the state cookie is set for: the API's, which is this page's when same-origin. */
function cookieHost(): string {
  const base = apiBase();
  if (base) {
    try {
      return new URL(base).host;
    } catch {
      /* fall through */
    }
  }
  return typeof window !== 'undefined' && window.location ? window.location.host : 'this site';
}

export interface SsoCopyContext {
  domain?: string;
  /** C1b's copy, passed in by the login page so the two never drift apart. */
  rateLimited: string;
  unreachable: string;
}

/** The copy for `/login?sso_error=...` (spec-v3 §10). The IdP's own text is never shown. */
export function ssoErrorMessage(params: SsoErrorParams, ctx: SsoCopyContext): string {
  const provider = params.provider ? PROVIDER_NAMES[params.provider] : undefined;
  const Provider = provider ?? 'Your identity provider';
  const domain = ctx.domain || 'your domain';
  const id = params.request_id;
  switch (params.sso_error) {
    case 'sso_expired':
      return 'Your sign-in took too long and expired. Start again.';
    case 'sso_state':
      return `We couldn't confirm this sign-in was started in this browser. Start again from this page. If it keeps happening, make sure cookies are allowed for ${cookieHost()}.`;
    case 'sso_idp_error':
      return (
        `${Provider} did not complete the sign-in${params.idp_error ? ` (${params.idp_error})` : ''}. ` +
        'If you expected access, contact your administrator.'
      );
    case 'sso_email':
      return id
        ? `${Provider} did not send a usable email address for your account. Ask your administrator, and give them this Request ID: ${id}.`
        : `${Provider} did not send a usable email address for your account. Ask your administrator.`;
    case 'sso_unverified':
      return (
        `${Provider} has not verified the email address on your account. Verify it with ` +
        `${provider ?? 'your identity provider'}, or ask your administrator.`
      );
    case 'sso_domain':
      return (
        `${Provider} signed you in with an account outside this organisation's domain. ` +
        'Use your work account, or ask your administrator.'
      );
    case 'sso_account':
      return id
        ? `Your account needs an administrator's attention before you can sign in. Give them this Request ID: ${id}.`
        : "Your account needs an administrator's attention before you can sign in.";
    case 'sso_inactive':
      return 'Your account is deactivated. Ask your administrator.';
    case 'sso_not_configured':
      return `Single sign-on isn't set up for ${domain}. Sign in with your password, or ask your administrator.`;
    case 'sso_saml_only':
      return `SAML sign-in isn't available yet. Ask your administrator to set up OIDC sign-in for ${ctx.domain || 'your domain'}.`;
    case 'sso_failed':
      return id
        ? `Sign-in with ${provider ?? 'your identity provider'} failed. Your administrator can find the details in the API log (Request ID: ${id}).`
        : `Sign-in with ${provider ?? 'your identity provider'} failed. Your administrator can find the details in the API log.`;
    case SSO_RATE_LIMITED:
      return ctx.rateLimited;
    case SSO_UNREACHABLE:
      return ctx.unreachable;
    case SSO_TIMED_OUT:
      // The exchange uses apiFetch's default deadline.
      return signInTimeoutMessage();
    default:
      return "Sign-in didn't complete. Start again.";
  }
}
