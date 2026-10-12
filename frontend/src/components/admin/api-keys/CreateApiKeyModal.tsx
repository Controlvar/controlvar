import React, { useState } from 'react';
import { apiFetch, UserMe } from '@/services/api';
import { useOptionalAuth } from '@/contexts/AuthContext';

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
 * The one scope a key can carry. Only this scope is meant to be enforced: it
 * will gate the server-side local-evaluation ruleset download. A key created
 * without it carries no scope at all.
 */
export const SDK_RULESET_SCOPE = 'sdk:ruleset';

/**
 * Server-side evaluation keys are for roles that can change flags: the API
 * refuses `sdk:ruleset` with 403 for anyone else, so the option is disabled
 * here for the same users, with the reason shown. No signed-in user (no
 * AuthProvider) is treated as not allowed.
 */
export function canCreateRulesetKey(user: UserMe | null | undefined): boolean {
  if (!user) return false;
  return user.is_superuser || user.role === 'ADMIN' || user.role === 'DEVELOPER';
}

export const RULESET_SCOPE_ROLE_REASON =
  'Only users who can change feature flags (the ADMIN and DEVELOPER roles, or a superuser) can create a key with this scope.';

export function CreateApiKeyModal({ isOpen, onClose, onSuccess }: CreateApiKeyModalProps) {
  const auth = useOptionalAuth();
  const rulesetAllowed = canCreateRulesetKey(auth?.user);
  const [name, setName] = useState('');
  const [rulesetScope, setRulesetScope] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createdKey, setCreatedKey] = useState<CreatedApiKey | null>(null);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;

    setSubmitting(true);
    setError(null);

    try {
      const data = await apiFetch<CreatedApiKey>('/api/v1/api-keys', {
        method: 'POST',
        json: rulesetScope && rulesetAllowed
          ? { name: name.trim(), scopes: [SDK_RULESET_SCOPE] }
          : { name: name.trim() },
      });
      setCreatedKey(data);
      onSuccess(plaintextKey(data));
    } catch (err) {
      setError((err as Error).message || 'Failed to create API key');
    } finally {
      setSubmitting(false);
    }
  };

  const handleClose = () => {
    setName('');
    setRulesetScope(false);
    setError(null);
    setCreatedKey(null);
    onClose();
  };

  const maskedKeyDisplay = createdKey
    ? `${plaintextKey(createdKey).slice(0, 8)}...`
    : null;

  return (
    <div
      data-testid="create-api-key-modal"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-40"
      role="dialog"
      aria-modal="true"
    >
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4 p-6">
        <h2 className="text-lg font-semibold text-slate-900 mb-4">
          {createdKey ? 'API Key Created' : 'Create API Key'}
        </h2>

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
                onClick={() => navigator.clipboard.writeText(plaintextKey(createdKey))}
                className="ml-3 text-xs text-blue-600 hover:text-blue-800 font-medium"
              >
                Copy
              </button>
            </div>
            <div className="flex justify-end">
              <button
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
                data-testid="api-key-name-input"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Production Key"
                className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>

            {/* Scope: one checkbox, for the one scope that is meant to be enforced */}
            <div className="mb-4">
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

            {/* Error */}
            {error && (
              <div
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
    </div>
  );
}
