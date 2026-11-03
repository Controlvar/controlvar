import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { apiFetch } from '@/services/api';
import { ApiKey } from '@/types/admin';
import { parseApiTimestamp } from '@/utils/apiTimestamp';
import { KeyState, keyState } from '@/components/admin/api-keys/ApiKeyTable';
import {
  MCP_READ_SCOPE,
  MCP_READ_SCOPE_HELP,
  MCP_READ_SCOPE_LABEL,
} from '@/components/admin/api-keys/mcpReadScope';
import {
  ExpiryChoice,
  effectiveExpiry,
  expiresAtFor,
  expiryHelp,
  expiryOptions,
} from '@/components/account/ownKeyExpiry';

/**
 * The caller's own keys, on the API every role already has:
 * `GET /api/v1/api-keys` (no `all`: only the caller's keys),
 * `POST /api/v1/api-keys` (always creates for the caller) and
 * `DELETE /api/v1/api-keys/{id}` (the owner may delete their own).
 */
export const OWN_KEYS_API_PATH = '/api/v1/api-keys';

/** `POST /api/v1/api-keys` body, as this page sends it. */
interface CreateKeyBody {
  name: string;
  scopes?: string[];
  expires_at?: string;
}

/** `POST /api/v1/api-keys` response: `key` is the plaintext, shown once. */
interface CreatedKey {
  id: string;
  name: string;
  key: string;
  prefix: string;
  created_at: string;
  expires_at?: string | null;
}

const STATE_LABELS: Record<KeyState, string> = {
  active: 'Active',
  expired: 'Expired',
  inactive: 'Inactive',
};

const STATE_CLASSES: Record<KeyState, string> = {
  active: 'bg-green-100 text-green-800',
  expired: 'bg-amber-100 text-amber-900',
  inactive: 'bg-slate-100 text-slate-700',
};

const FOCUS_RING = 'focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600 focus-visible:ring-offset-2';
const PRIMARY_BUTTON = `px-4 py-2 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-60 ${FOCUS_RING}`;
const SECONDARY_BUTTON = `px-3 py-1.5 rounded-md text-sm font-medium text-slate-700 border border-slate-300 bg-white hover:bg-slate-50 ${FOCUS_RING}`;
const DANGER_BUTTON = `px-3 py-1.5 rounded-md text-sm font-medium bg-red-600 text-white hover:bg-red-700 disabled:opacity-60 ${FOCUS_RING}`;
// slate-500 borders: 4.76:1 on white, so the field's edge meets the 3:1 that
// WCAG 1.4.11 asks of a control's boundary.
const INPUT =
  'mt-1 block w-full rounded-md border border-slate-500 bg-white px-3 py-2 text-sm shadow-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-600';

export const NAME_REQUIRED = 'Enter a name for the key.';

/** An API time, read as UTC when it names no zone (`parseApiTimestamp`). */
function formatWhen(value: string): string {
  const when = parseApiTimestamp(value);
  return Number.isNaN(when.getTime()) ? value : when.toLocaleString();
}

function messageOf(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

/**
 * "Your API keys": any signed-in user lists, creates and revokes their own
 * keys. It offers one scope, `mcp:read`, with the label and help text of the
 * admin dialog's checkbox (imported, not copied), and an expiry
 * (`ownKeyExpiry.ts`). Expiry times from the API are read only through
 * `parseApiTimestamp` and `keyState`.
 */
export function OwnApiKeys() {
  const nameId = useId();
  const nameErrorId = useId();
  const mcpId = useId();
  const mcpHelpId = useId();
  const expiryId = useId();
  const expiryHelpId = useId();
  const createHeadingId = useId();
  const keysHeadingId = useId();
  const revealHeadingId = useId();
  const confirmHeadingId = useId();
  const confirmTextId = useId();

  const [keys, setKeys] = useState<ApiKey[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [name, setName] = useState('');
  const [nameError, setNameError] = useState(false);
  const [mcpRead, setMcpRead] = useState(false);
  const [pickedExpiry, setPickedExpiry] = useState<ExpiryChoice | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const [revealed, setRevealed] = useState<{ name: string; key: string } | null>(null);
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');

  const [pendingRevoke, setPendingRevoke] = useState<ApiKey | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [revokeError, setRevokeError] = useState<string | null>(null);
  const [revoked, setRevoked] = useState<{ name: string } | null>(null);

  const nameInput = useRef<HTMLInputElement>(null);
  const revealHeading = useRef<HTMLHeadingElement>(null);
  const keysHeading = useRef<HTMLHeadingElement>(null);
  const confirmRegion = useRef<HTMLDivElement>(null);
  const revokeButtons = useRef(new Map<string, HTMLButtonElement>());

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      // No `all`: the API answers with the caller's own keys only.
      const data = await apiFetch<ApiKey[]>(OWN_KEYS_API_PATH);
      setKeys(Array.isArray(data) ? data : []);
    } catch (err) {
      setLoadError(messageOf(err, 'The request failed.'));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (revealed) revealHeading.current?.focus();
  }, [revealed]);

  useEffect(() => {
    if (pendingRevoke) confirmRegion.current?.focus();
  }, [pendingRevoke]);

  useEffect(() => {
    if (revoked) keysHeading.current?.focus();
  }, [revoked]);

  const expiry = effectiveExpiry(pickedExpiry, mcpRead);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (creating) return;
    const trimmed = name.trim();
    if (!trimmed) {
      setNameError(true);
      nameInput.current?.focus();
      return;
    }
    setNameError(false);
    setCreateError(null);
    setCreating(true);
    const body: CreateKeyBody = { name: trimmed };
    if (mcpRead) body.scopes = [MCP_READ_SCOPE];
    const expiresAt = expiresAtFor(expiry);
    if (expiresAt !== undefined) body.expires_at = expiresAt;
    try {
      const created = await apiFetch<CreatedKey>(OWN_KEYS_API_PATH, { method: 'POST', json: body });
      setName('');
      setMcpRead(false);
      setPickedExpiry(null);
      setRevoked(null);
      setCopyState('idle');
      setRevealed({ name: created.name, key: created.key });
      void load();
    } catch (err) {
      setCreateError(messageOf(err, 'The request failed.'));
    } finally {
      setCreating(false);
    }
  };

  const copy = async () => {
    if (!revealed) return;
    try {
      if (!navigator.clipboard) throw new Error('No clipboard');
      await navigator.clipboard.writeText(revealed.key);
      setCopyState('copied');
    } catch {
      setCopyState('failed');
    }
  };

  const dismissReveal = () => {
    setRevealed(null);
    setCopyState('idle');
    nameInput.current?.focus();
  };

  const askRevoke = (key: ApiKey) => {
    setRevoked(null);
    setRevokeError(null);
    setPendingRevoke(key);
  };

  const cancelRevoke = () => {
    const id = pendingRevoke?.id;
    setPendingRevoke(null);
    setRevokeError(null);
    if (id) revokeButtons.current.get(id)?.focus();
  };

  const runRevoke = async () => {
    if (!pendingRevoke || revoking) return;
    const target = pendingRevoke;
    setRevoking(true);
    setRevokeError(null);
    try {
      await apiFetch<void>(`${OWN_KEYS_API_PATH}/${encodeURIComponent(target.id)}`, { method: 'DELETE' });
      setPendingRevoke(null);
      setKeys((current) => (current ? current.filter((k) => k.id !== target.id) : current));
      setRevoked({ name: target.name });
      void load();
    } catch (err) {
      setRevokeError(messageOf(err, 'The request failed.'));
    } finally {
      setRevoking(false);
    }
  };

  return (
    <div className="max-w-5xl mx-auto px-4 py-8 flex flex-col gap-6" data-testid="own-api-keys-page">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Your API keys</h1>
        <p className="mt-2 text-sm text-slate-700">
          A key acts as you: an SDK or a tool sends it in the <code className="font-mono">X-API-Key</code>{' '}
          header. This page lists only the keys you created. The full key is shown once, when you create
          it.
        </p>
      </div>

      {revealed && (
        <section
          aria-labelledby={revealHeadingId}
          data-testid="own-key-revealed"
          className="rounded-lg border border-amber-300 bg-amber-50 p-4"
        >
          <h2
            id={revealHeadingId}
            ref={revealHeading}
            tabIndex={-1}
            className="text-base font-semibold text-slate-900 focus:outline-none"
          >
            Key created: {revealed.name}
          </h2>
          <p className="mt-1 text-sm font-semibold text-amber-900" data-testid="own-key-warning">
            Copy this key now. It won&apos;t be shown again.
          </p>
          <code
            data-testid="own-key-value"
            className="mt-2 block break-all rounded-md border border-amber-200 bg-white px-3 py-2 font-mono text-sm text-slate-900 select-all"
          >
            {revealed.key}
          </code>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <button type="button" onClick={() => void copy()} className={PRIMARY_BUTTON} data-testid="own-key-copy">
              Copy
            </button>
            <button type="button" onClick={dismissReveal} className={SECONDARY_BUTTON} data-testid="own-key-done">
              Done
            </button>
            <span aria-live="polite" className="text-sm text-slate-800" data-testid="own-key-copy-status">
              {copyState === 'copied' ? 'Copied' : copyState === 'failed' ? 'Could not copy. Select the key and copy it.' : ''}
            </span>
          </div>
        </section>
      )}

      <section
        aria-labelledby={createHeadingId}
        className="rounded-lg border border-slate-200 bg-white p-4"
        data-testid="own-key-create"
      >
        <h2 id={createHeadingId} className="text-base font-semibold text-slate-900">
          Create a key
        </h2>
        <form className="mt-3 flex flex-col gap-4" onSubmit={submit} noValidate>
          <div>
            <label htmlFor={nameId} className="block text-sm font-medium text-slate-800">
              Name
            </label>
            <input
              id={nameId}
              ref={nameInput}
              type="text"
              maxLength={100}
              autoComplete="off"
              value={name}
              onChange={(e) => setName(e.target.value)}
              aria-invalid={nameError || undefined}
              aria-describedby={nameError ? nameErrorId : undefined}
              className={INPUT}
              data-testid="own-key-name"
            />
            {nameError && (
              <p id={nameErrorId} className="mt-1 text-sm text-red-700" data-testid="own-key-name-error">
                {NAME_REQUIRED}
              </p>
            )}
          </div>

          <div>
            <div className="flex items-start gap-2">
              <input
                id={mcpId}
                type="checkbox"
                checked={mcpRead}
                onChange={(e) => {
                  setMcpRead(e.target.checked);
                  // "No expiry" is not offered with mcp:read; the default takes its place.
                  if (e.target.checked && pickedExpiry === 'never') setPickedExpiry(null);
                }}
                aria-describedby={mcpHelpId}
                className="mt-0.5 h-4 w-4 accent-blue-600"
                data-testid="own-key-mcp-read"
              />
              <label htmlFor={mcpId} className="text-sm font-medium text-slate-800">
                {MCP_READ_SCOPE_LABEL}
              </label>
            </div>
            <p id={mcpHelpId} className="mt-1 ml-6 text-sm text-slate-700" data-testid="own-key-mcp-read-help">
              {MCP_READ_SCOPE_HELP}
            </p>
          </div>

          <div>
            <label htmlFor={expiryId} className="block text-sm font-medium text-slate-800">
              Expires
            </label>
            <select
              id={expiryId}
              value={expiry}
              onChange={(e) => setPickedExpiry(e.target.value as ExpiryChoice)}
              aria-describedby={expiryHelpId}
              className={`${INPUT} sm:max-w-xs`}
              data-testid="own-key-expiry"
            >
              {expiryOptions(mcpRead).map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <p id={expiryHelpId} className="mt-1 text-sm text-slate-700" data-testid="own-key-expiry-help">
              {expiryHelp(mcpRead)}
            </p>
          </div>

          {createError && (
            <p role="alert" className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800" data-testid="own-key-create-error">
              The key was not created: {createError}
            </p>
          )}

          <div>
            <button type="submit" disabled={creating} className={PRIMARY_BUTTON} data-testid="own-key-submit">
              {creating ? 'Creating…' : 'Create key'}
            </button>
          </div>
        </form>
      </section>

      <section aria-labelledby={keysHeadingId} className="rounded-lg border border-slate-200 bg-white" data-testid="own-key-list">
        <div className="p-4 border-b border-slate-200">
          <h2 id={keysHeadingId} ref={keysHeading} tabIndex={-1} className="text-base font-semibold text-slate-900 focus:outline-none">
            Your keys
          </h2>
          {revoked && (
            <p role="status" className="mt-1 text-sm text-green-800" data-testid="own-key-revoked">
              Revoked {revoked.name}.
            </p>
          )}
        </div>

        {pendingRevoke && (
          <div
            ref={confirmRegion}
            role="dialog"
            aria-labelledby={confirmHeadingId}
            aria-describedby={confirmTextId}
            tabIndex={-1}
            onKeyDown={(event) => {
              if (event.key === 'Escape') cancelRevoke();
            }}
            className="m-4 rounded-lg border border-red-200 bg-red-50 p-3 flex flex-col gap-3 focus:outline-none focus:ring-2 focus:ring-blue-600"
            data-testid="own-key-revoke-confirm"
          >
            <h3 id={confirmHeadingId} className="text-sm font-semibold text-slate-900">
              Revoke {pendingRevoke.name}?
            </h3>
            <p id={confirmTextId} className="text-sm text-red-900">
              Anything that uses this key stops working at once. This cannot be undone.
            </p>
            {revokeError && (
              <p role="alert" className="text-sm text-red-800" data-testid="own-key-revoke-error">
                The key was not revoked: {revokeError}
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              <button type="button" onClick={cancelRevoke} className={SECONDARY_BUTTON} data-testid="own-key-revoke-cancel">
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void runRevoke()}
                disabled={revoking}
                className={DANGER_BUTTON}
                data-testid="own-key-revoke-yes"
              >
                {revoking ? 'Revoking…' : 'Revoke key'}
              </button>
            </div>
          </div>
        )}

        {keys === null && !loadError && (
          <p role="status" className="p-4 text-sm text-slate-700" data-testid="own-key-loading">
            Loading your keys…
          </p>
        )}

        {loadError && (
          <div role="alert" className="p-4 text-sm text-red-800" data-testid="own-key-load-error">
            <p>Your keys could not be loaded: {loadError}</p>
            <button type="button" onClick={() => void load()} className={`mt-2 ${SECONDARY_BUTTON}`}>
              Try again
            </button>
          </div>
        )}

        {keys !== null && !loadError && keys.length === 0 && (
          <p className="p-4 text-sm text-slate-700" data-testid="own-key-empty">
            You have no API keys yet.
          </p>
        )}

        {keys !== null && !loadError && keys.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm" data-testid="own-key-table">
              <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-700">
                <tr>
                  <th scope="col" className="px-4 py-3">Name</th>
                  <th scope="col" className="px-4 py-3">Scopes</th>
                  <th scope="col" className="px-4 py-3">Created</th>
                  <th scope="col" className="px-4 py-3">Expires</th>
                  <th scope="col" className="px-4 py-3">Last used</th>
                  <th scope="col" className="px-4 py-3">Status</th>
                  <th scope="col" className="px-4 py-3">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {keys.map((key) => {
                  const state = keyState(key);
                  return (
                    <tr key={key.id} data-testid={`own-key-row-${key.id}`}>
                      <th scope="row" className="px-4 py-3 text-left font-medium text-slate-900">
                        {key.name}
                      </th>
                      <td className="px-4 py-3 font-mono text-xs text-slate-700">
                        {key.scopes && key.scopes.length > 0 ? key.scopes.join(', ') : 'None'}
                      </td>
                      <td className="px-4 py-3 text-slate-700">{formatWhen(key.created_at)}</td>
                      <td className="px-4 py-3 text-slate-700" data-testid={`own-key-expires-${key.id}`}>
                        {key.expires_at ? formatWhen(key.expires_at) : 'No expiry'}
                      </td>
                      <td className="px-4 py-3 text-slate-700">
                        {key.last_used_at ? formatWhen(key.last_used_at) : 'Never'}
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${STATE_CLASSES[state]}`}
                          data-testid={`own-key-state-${key.id}`}
                        >
                          {STATE_LABELS[state]}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <button
                          type="button"
                          ref={(el) => {
                            if (el) revokeButtons.current.set(key.id, el);
                            else revokeButtons.current.delete(key.id);
                          }}
                          onClick={() => askRevoke(key)}
                          className={`rounded-md px-2 py-1 text-sm font-medium text-red-700 hover:bg-red-50 hover:text-red-800 ${FOCUS_RING}`}
                          data-testid={`own-key-revoke-${key.id}`}
                        >
                          Revoke<span className="sr-only"> {key.name}</span>
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

export default OwnApiKeys;
