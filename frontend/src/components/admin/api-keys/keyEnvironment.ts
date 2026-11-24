/**
 * An API key's environment: its names and words, and the one way the dashboard
 * creates a key. Admin -> API Keys (`CreateApiKeyModal`) and Your API keys
 * (`OwnApiKeys`) both offer the choice, and both create through
 * `createApiKey`, so neither can show a key of another environment than the
 * one picked.
 */
import { apiFetch, isApiError } from '@/services/api';

const API_KEYS_PATH = '/api/v1/api-keys';

/** The API's names, in the order the forms list them. */
export const KEY_ENVIRONMENTS = ['production', 'staging', 'development'] as const;

export type KeyEnvironment = (typeof KEY_ENVIRONMENTS)[number];

/** What a form starts with, and what the API makes of a request that names none. */
export const DEFAULT_KEY_ENVIRONMENT: KeyEnvironment = 'production';

const LABELS: Record<KeyEnvironment, string> = {
  production: 'Production',
  staging: 'Staging',
  development: 'Development',
};

/** The label of the choice, and the heading of the list column. */
export const KEY_ENVIRONMENT_LABEL = 'Environment';

/** Beside the choice. */
export const KEY_ENVIRONMENT_HELP =
  'A key works in one environment, and that cannot be changed later. Experiments and event tracking need a Production key.';

/**
 * What a list shows for a key. A dash when the API did not say: an earlier
 * release lists a key without naming its environment, whichever it is.
 */
export function keyEnvironmentLabel(environment: string | null | undefined): string {
  if (!environment) return '—';
  return LABELS[environment as KeyEnvironment] ?? environment;
}

/** `POST /api/v1/api-keys` body, as the dashboard sends it. */
export interface CreateKeyBody {
  name: string;
  environment: KeyEnvironment;
  scopes?: string[];
  expires_at?: string;
}

/**
 * `POST /api/v1/api-keys` response: `key` is the plaintext, shown once. An
 * earlier release answers without `environment`.
 */
export interface CreatedKey {
  id: string;
  name: string;
  key: string;
  prefix: string;
  created_at: string;
  expires_at?: string | null;
  environment?: string;
}

/** The API made a key, and did not say that it is of the environment asked for. */
export class KeyEnvironmentMismatch extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'KeyEnvironmentMismatch';
    // Keep `instanceof` working when the TS target is ES5 (as ApiError does).
    Object.setPrototypeOf(this, KeyEnvironmentMismatch.prototype);
  }
}

const WHY_AND_NEXT =
  'This happens while an earlier release of the API, which has no environments, is still running: create the key again when the upgrade has finished.';

function mismatchMessage(asked: KeyEnvironment, name: string, revokeError: unknown): string {
  const unconfirmed = `The API did not confirm that the new key is a ${LABELS[asked]} key`;
  if (revokeError === null) return `${unconfirmed}, so the key was revoked. ${WHY_AND_NEXT}`;
  const reason = revokeError instanceof Error && revokeError.message ? revokeError.message : 'the request failed';
  // A revoke that timed out may have been carried out.
  const exists = isApiError(revokeError) && revokeError.outcomeUnknown ? 'may still exist' : 'still exists';
  return `${unconfirmed}, and revoking it failed (${reason}). The key “${name}” ${exists}: revoke it in the list on this page. ${WHY_AND_NEXT}`;
}

/**
 * Create a key, and return it only if the API says it is of the environment
 * asked for. A release from before environments ignores the field, makes a
 * production key and answers without one. So when the answer names no
 * environment, or another one, the key is revoked, its value is dropped here,
 * and a `KeyEnvironmentMismatch` says so (and, if the revoke failed, that the
 * key still exists).
 */
export async function createApiKey(body: CreateKeyBody): Promise<CreatedKey> {
  const created = await apiFetch<CreatedKey>(API_KEYS_PATH, { method: 'POST', json: body });
  if (created.environment === body.environment) return created;
  let revokeError: unknown = null;
  try {
    await apiFetch<void>(`${API_KEYS_PATH}/${encodeURIComponent(created.id)}`, { method: 'DELETE' });
  } catch (err) {
    revokeError = err;
  }
  throw new KeyEnvironmentMismatch(mismatchMessage(body.environment, body.name, revokeError));
}

/**
 * What a form shows for a failed create. A refused scope reaches it as
 * "scopes: Value error, <sentence>"; the sentence is what a person reads.
 */
export function createKeyErrorMessage(err: unknown, fallback: string): string {
  const message = err instanceof Error && err.message ? err.message : fallback;
  return message.replace(/^scopes: Value error, /, '');
}
