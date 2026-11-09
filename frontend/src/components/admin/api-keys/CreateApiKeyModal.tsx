import React, { useEffect, useRef, useState } from 'react';
import { Dialog } from '@/components/ui/Dialog';
import { apiFetch, UserMe } from '@/services/api';
import { useOptionalAuth } from '@/contexts/AuthContext';
import {
  MCP_READ_SCOPE,
  MCP_READ_SCOPE_HELP,
  MCP_READ_SCOPE_LABEL,
} from '@/components/admin/api-keys/mcpReadScope';
import { useEffectivePermissions } from '@/contexts/EffectivePermissionsContext';
import { EffectivePermissions, holds } from '@/utils/effectivePermissions';

/**
 * `POST /api/v1/api-keys` response. The plaintext key is returned once as
 * `key` (older builds used `key_value`; both are accepted).
 */
interface CreatedApiKey {
  id: string;
  name: string;
  key?: string;
  key_value?: string;
  prefix: string;
  created_at: string;
  expires_at?: string | null;
  is_active?: boolean;
}

function plaintextKey(created: CreatedApiKey): string {
  return created.key ?? created.key_value ?? '';
}

interface CreateApiKeyModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: (keyValue: string) => void;
}

/**
 * `sdk:ruleset` gates the server-side local-evaluation ruleset download. The
 * other scope this dialog offers is `mcp:read`. A key created with neither
 * carries no scope at all.
 */
export const SDK_RULESET_SCOPE = 'sdk:ruleset';

/**
 * Server-side evaluation keys are for users who can change feature flags: the
 * API refuses `sdk:ruleset` with 403 for anyone else, so the option is
 * disabled here for the same users, with the reason shown. "Can change
 * feature flags" is `check_permission(user, FEATURE_FLAG, UPDATE)`
 * (`may_use_server_key`): a superuser, the ADMIN and DEVELOPER roles, or the
 * effective map's `feature_flag: update` (a custom role or direct grant).
 * No signed-in user (no AuthProvider) is treated as not allowed.
 */
const ROLES_THAT_CHANGE_FLAGS = ['ADMIN', 'DEVELOPER'];

export function canCreateRulesetKey(user: UserMe | null | undefined, effective?: EffectivePermissions): boolean {
  if (!user) return false;
  if (user.is_superuser) return true;
  return ROLES_THAT_CHANGE_FLAGS.includes(user.role) || holds(effective, 'feature_flag', 'update');
}

export const RULESET_SCOPE_ROLE_REASON =
  'Only users who can change feature flags can create a key with this scope; the ADMIN and DEVELOPER roles and superusers can.';

/**
 * The Create API Key dialog, on the shared Dialog (#369): focus opens on Name,
 * Tab stays inside, the page behind is inert, and on close focus goes back to
 * the button that opened it. Escape closes it at any time, a create in flight
 * included (it is never `busy`): the create still finishes and the page gets
 * the key, the dialog does not.
 */
export function CreateApiKeyModal({ isOpen, onClose, onSuccess }: CreateApiKeyModalProps) {
  const auth = useOptionalAuth();
  const { effective, loading } = useEffectivePermissions();
  const rulesetAllowed = canCreateRulesetKey(auth?.user, effective);
  // While the effective map is on its way, neither the option nor the reason.
  const rulesetPending = !rulesetAllowed && loading;
  const [name, setName] = useState('');
  const [rulesetScope, setRulesetScope] = useState(false);
  const [mcpReadScope, setMcpReadScope] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createdKey, setCreatedKey] = useState<CreatedApiKey | null>(null);
  const [copied, setCopied] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);
  const doneRef = useRef<HTMLButtonElement>(null);
  const sessionRef = useRef(0);

  // Whenever the dialog is closed (by this component or by its parent, which
  // closes it as soon as a key is created) drop everything it held, so the
  // next open is a fresh dialog and no earlier key stays in memory.
  // The same reset runs when it opens, so a stale view can never be the first
  // thing a new open shows. Each open or close starts a new "session"; a create
  // that was submitted in an earlier one must not write into this one.
  useEffect(() => {
    sessionRef.current += 1;
    setName('');
    setRulesetScope(false);
    setMcpReadScope(false);
    setSubmitting(false);
    setError(null);
    setCreatedKey(null);
    setCopied(false);
  }, [isOpen]);

  // The form is replaced by the key view: keep focus inside the dialog.
  useEffect(() => {
    if (createdKey) doneRef.current?.focus();
  }, [createdKey]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;

    const session = sessionRef.current;
    setSubmitting(true);
    setError(null);

    const scopes: string[] = [];
    if (rulesetScope && rulesetAllowed) scopes.push(SDK_RULESET_SCOPE);
    if (mcpReadScope) scopes.push(MCP_READ_SCOPE);

    try {
      const data = await apiFetch<CreatedApiKey>('/api/v1/api-keys', {
        method: 'POST',
        json: scopes.length > 0 ? { name: name.trim(), scopes } : { name: name.trim() },
      });
      // Closed since the submit: the page still gets the key, the dialog does not.
      if (session === sessionRef.current) setCreatedKey(data);
      onSuccess(plaintextKey(data));
    } catch (err) {
      if (session === sessionRef.current) {
        setError((err as Error).message || 'Failed to create API key');
      }
    } finally {
      if (session === sessionRef.current) setSubmitting(false);
    }
  };

  const handleClose = () => {
    sessionRef.current += 1;
    setName('');
    setRulesetScope(false);
    setMcpReadScope(false);
    setError(null);
    setCreatedKey(null);
    setCopied(false);
    onClose();
  };

  const handleCopy = async () => {
    if (!createdKey) return;
    try {
      await navigator.clipboard.writeText(plaintextKey(createdKey));
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  const maskedKeyDisplay = createdKey
    ? `${plaintextKey(createdKey).slice(0, 8)}...`
    : null;

  // Never the key itself: this text is announced by a screen reader.
  const announcement = createdKey ? (copied ? 'Key copied' : 'Key created') : '';

  // Never `busy`: Escape closes it during a create too (see the docstring).
  return (
    <Dialog
      open={isOpen}
      onClose={handleClose}
      title={createdKey ? 'API Key Created' : 'Create API Key'}
      initialFocusRef={nameRef}
      testId="create-api-key-modal"
    >
      <div className="mt-3">
        <div role="status" aria-live="polite" data-testid="api-key-live-region" className="sr-only">
          {announcement}
        </div>

        {/* Key revealed state */}
        {createdKey ? (
          <div>
            <p className="text-sm text-slate-600 mb-3">
              Copy this key now — it will not be shown again.
            </p>
            <div className="bg-slate-50 border border-slate-200 rounded-md p-3 flex items-center justify-between mb-4">
              <span
                data-testid="masked-key-value"
                className="font-mono text-sm text-slate-800"
              >
                {maskedKeyDisplay}
              </span>
              <button
                data-testid="copy-key-button"
                onClick={handleCopy}
                className="ml-3 text-xs text-blue-600 hover:text-blue-800 font-medium"
              >
                {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
            <div className="flex justify-end">
              <button
                ref={doneRef}
                data-testid="modal-done-button"
                onClick={handleClose}
                className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-md hover:bg-blue-700"
              >
                Done
              </button>
            </div>
          </div>
        ) : (
          /* Creation form */
          <form onSubmit={handleSubmit} noValidate>
            {/* Name */}
            <div className="mb-4">
              <label
                htmlFor="api-key-name"
                className="block text-sm font-medium text-slate-700 mb-1"
              >
                Name <span className="text-red-500">*</span>
              </label>
              <input
                id="api-key-name"
                ref={nameRef}
                data-testid="api-key-name-input"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Production Key"
                className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>

            {/* Scopes: one checkbox each, sdk:ruleset first, then mcp:read */}
            {!rulesetPending && (
            <div className="mb-4" data-testid="api-key-ruleset-scope-section">

              <div className="flex items-start gap-2">
                <input
                  id="api-key-ruleset-scope"
                  data-testid="api-key-ruleset-scope"
                  type="checkbox"
                  checked={rulesetScope && rulesetAllowed}
                  disabled={!rulesetAllowed}
                  onChange={(e) => setRulesetScope(e.target.checked)}
                  aria-describedby={
                    rulesetAllowed
                      ? 'api-key-ruleset-scope-help'
                      : 'api-key-ruleset-scope-help api-key-ruleset-scope-reason'
                  }
                  className="mt-0.5 h-4 w-4 rounded border-slate-300 text-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
                />
                <label
                  htmlFor="api-key-ruleset-scope"
                  className={`text-sm font-medium ${rulesetAllowed ? 'text-slate-700' : 'text-slate-600'}`}
                >
                  Server-side local evaluation (sdk:ruleset)
                </label>
              </div>
              <p
                id="api-key-ruleset-scope-help"
                data-testid="api-key-ruleset-scope-help"
                className="mt-1 ml-6 text-sm text-slate-600"
              >
                For server-side SDKs that evaluate flags locally, and for a server that forwards
                error reports: reports sent with this key count toward automatic rollback. A key
                with this scope will be able to download every flag&apos;s targeting rules, so keep
                it on a server.
              </p>
              {!rulesetAllowed && (
                <p
                  id="api-key-ruleset-scope-reason"
                  data-testid="api-key-ruleset-scope-reason"
                  className="mt-1 ml-6 text-sm text-slate-700"
                >
                  {RULESET_SCOPE_ROLE_REASON}
                </p>
              )}
            </div>
            )}

            {/* mcp:read: offered to every role */}
            <div className="mb-4">
              <div className="flex items-start gap-2">
                <input
                  id="api-key-mcp-read-scope"
                  data-testid="api-key-mcp-read-scope"
                  type="checkbox"
                  checked={mcpReadScope}
                  onChange={(e) => setMcpReadScope(e.target.checked)}
                  aria-describedby="api-key-mcp-read-scope-help"
                  className="mt-0.5 h-4 w-4 rounded border-slate-300 text-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                <label
                  htmlFor="api-key-mcp-read-scope"
                  className="text-sm font-medium text-slate-700"
                >
                  {MCP_READ_SCOPE_LABEL}
                </label>
              </div>
              <p
                id="api-key-mcp-read-scope-help"
                data-testid="api-key-mcp-read-scope-help"
                className="mt-1 ml-6 text-sm text-slate-600"
              >
                {MCP_READ_SCOPE_HELP}
              </p>
            </div>

            {/* Error */}
            {error && (
              <div
                role="alert"
                data-testid="modal-error"
                className="mb-4 text-sm text-red-600 bg-red-50 border border-red-200 rounded-md p-3"
              >
                {error}
              </div>
            )}

            {/* Actions */}
            <div className="flex justify-end gap-3">
              <button
                type="button"
                data-testid="modal-cancel-button"
                onClick={handleClose}
                className="px-4 py-2 text-sm font-medium text-slate-700 border border-slate-300 rounded-md hover:bg-slate-50"
              >
                Cancel
              </button>
              <button
                type="submit"
                data-testid="create-api-key-submit"
                disabled={!name.trim() || submitting}
                className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {submitting ? 'Creating...' : 'Create Key'}
              </button>
            </div>
          </form>
        )}
      </div>
    </Dialog>
  );
}
