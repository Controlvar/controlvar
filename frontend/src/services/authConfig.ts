/**
 * How this dashboard signs in, read at run time from `GET /api/v1/auth/config`
 * (#708). One image serves every deployment, so nothing about the provider is
 * baked into the build.
 *
 * Only a well-formed Cognito answer turns the Cognito sign-in on. Every other
 * answer -- a 404 from an API that predates the route, no answer at all, a
 * body that is not JSON, the export's HTML page served with a 200, a body
 * with a field missing or of the wrong type -- is the local provider, so the
 * sign-in form people use today stays reachable whatever the API says.
 */

import { apiFetch } from '@/services/api';

export const AUTH_CONFIG_PATH = '/api/v1/auth/config';

/**
 * How long `/login` and the session check wait for the configuration before
 * going on with the local provider: shorter than the default deadline,
 * because both wait for it on every page load.
 */
export const AUTH_CONFIG_TIMEOUT_MS = 5_000;

/** The scopes asked for when the API names none (the relay's own list). */
export const DEFAULT_HOSTED_UI_SCOPES = [
  'openid',
  'email',
  'profile',
  'aws.cognito.signin.user.admin',
] as const;

export type AuthConfig =
  | { kind: 'local' }
  | { kind: 'cognito'; hostedUiDomain: string; clientId: string; scopes: string[] }
  | { kind: 'cognito-unconfigured'; missing: string[] };

export const LOCAL_AUTH_CONFIG: AuthConfig = Object.freeze({ kind: 'local' }) as AuthConfig;

/** A host name only: letters, digits, dots and hyphens, with at least one dot. */
const DOMAIN_RE = /^[a-z0-9-]+(\.[a-z0-9-]+)+$/;
/** A Cognito app client id: word characters and `+`, as Cognito defines it. */
const CLIENT_ID_RE = /^[\w+]{1,128}$/;
/** An OAuth scope token (RFC 6749 section 3.3), kept to what Cognito uses. */
const SCOPE_RE = /^[A-Za-z0-9._:/-]{1,256}$/;
/** A setting's name, as `/auth/config` lists the unset ones. */
const SETTING_NAME_RE = /^[A-Z][A-Z0-9_]{0,63}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/**
 * The configuration an answer describes. Anything that is not a well-formed
 * Cognito answer is {@link LOCAL_AUTH_CONFIG}.
 */
export function parseAuthConfig(body: unknown): AuthConfig {
  if (!isRecord(body) || body.provider !== 'cognito') return LOCAL_AUTH_CONFIG;

  if (body.configured === false) {
    const missing = Array.isArray(body.missing)
      ? body.missing.filter((name): name is string => typeof name === 'string' && SETTING_NAME_RE.test(name))
      : [];
    return { kind: 'cognito-unconfigured', missing };
  }

  const domain = body.hosted_ui_domain;
  const clientId = body.client_id;
  if (typeof domain !== 'string' || domain.length > 253 || !DOMAIN_RE.test(domain)) {
    return LOCAL_AUTH_CONFIG;
  }
  if (typeof clientId !== 'string' || !CLIENT_ID_RE.test(clientId)) return LOCAL_AUTH_CONFIG;

  let scopes: string[] = [...DEFAULT_HOSTED_UI_SCOPES];
  if (body.scopes !== undefined) {
    if (
      !Array.isArray(body.scopes) ||
      body.scopes.length === 0 ||
      !body.scopes.every((s) => typeof s === 'string' && SCOPE_RE.test(s))
    ) {
      return LOCAL_AUTH_CONFIG;
    }
    scopes = body.scopes as string[];
  }
  return { kind: 'cognito', hostedUiDomain: domain, clientId, scopes };
}

/**
 * Read the configuration. Never rejects: a failure of any kind, the
 * {@link AUTH_CONFIG_TIMEOUT_MS} deadline passing included, is the local
 * provider.
 */
export async function fetchAuthConfig(): Promise<AuthConfig> {
  try {
    const body = await apiFetch<unknown>(AUTH_CONFIG_PATH, {
      auth: false,
      redirectOn401: false,
      timeoutMs: AUTH_CONFIG_TIMEOUT_MS,
    });
    return parseAuthConfig(body);
  } catch {
    return LOCAL_AUTH_CONFIG;
  }
}
