import React, { useCallback, useEffect, useState } from 'react';
import { ApiKey } from '@/types/admin';
import { apiFetch } from '@/services/api';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

interface ApiKeyTableProps {
  onCreateKey: () => void;
  /** Bump after creating a key so the list refreshes. */
  refreshToken?: number;
}

function formatDate(isoString?: string | null): string {
  if (!isoString) return '—';
  try {
    return parseApiTimestamp(isoString).toLocaleString();
  } catch {
    return isoString;
  }
}

export type KeyState = 'active' | 'expired' | 'inactive';

/** Derive the display state from the fields the API actually returns. */
export function keyState(key: ApiKey, now: Date = new Date()): KeyState {
  if (!key.is_active) return 'inactive';
  if (key.expires_at && parseApiTimestamp(key.expires_at).getTime() <= now.getTime()) return 'expired';
  return 'active';
}

const STATE_CLASSES: Record<KeyState, string> = {
  active: 'bg-green-100 text-green-800',
  expired: 'bg-amber-100 text-amber-800',
  inactive: 'bg-slate-100 text-slate-600',
};

/**
 * Lists the caller's API keys (`GET /api/v1/api-keys`). Administrators can
 * switch to every user's keys (`?all=true`). Keys carry no retrievable
 * secret and no prefix: the plaintext is shown once at creation, so rows are
 * identified by name and creation time. Deleting a key is permanent
 * (`DELETE /api/v1/api-keys/{id}`); there is no soft revoke on this route.
 */
export function ApiKeyTable({ onCreateKey, refreshToken = 0 }: ApiKeyTableProps) {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [includeInactive, setIncludeInactive] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<ApiKey | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const fetchKeys = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // A false toggle is left out (undefined), not sent as `false`, so the
      // default request is the bare path.
      const data = await apiFetch<ApiKey[]>('/api/v1/api-keys', {
        query: {
          all: showAll ? true : undefined,
          include_inactive: includeInactive ? true : undefined,
        },
      });
      setKeys(Array.isArray(data) ? data : []);
    } catch (err) {
      setError((err as Error).message || 'Failed to load API keys');
    } finally {
      setLoading(false);
    }
  }, [showAll, includeInactive]);

  useEffect(() => {
    fetchKeys();
  }, [fetchKeys, refreshToken]);

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await apiFetch<void>(`/api/v1/api-keys/${pendingDelete.id}`, { method: 'DELETE' });
      setPendingDelete(null);
      await fetchKeys();
    } catch (err) {
      setDeleteError((err as Error).message || 'Failed to delete API key');
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div
      data-testid="api-key-table"
      className="bg-white rounded-lg border border-slate-200 overflow-hidden"
    >
      {/* Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-3 p-4 border-b border-slate-200">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">API Keys</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            SDKs authenticate with <code className="font-mono">X-API-Key</code>. The key is shown
            once when created.
          </p>
        </div>
        <div className="flex items-center gap-4 text-sm text-slate-600">
          <label className="inline-flex items-center gap-1.5">
            <input
              type="checkbox"
              data-testid="show-all-keys"
              checked={showAll}
              onChange={(e) => setShowAll(e.target.checked)}
            />
            All users (admin)
          </label>
          <label className="inline-flex items-center gap-1.5">
            <input
              type="checkbox"
              data-testid="include-inactive-keys"
              checked={includeInactive}
              onChange={(e) => setIncludeInactive(e.target.checked)}
            />
            Include inactive
          </label>
          <button
            data-testid="create-api-key-button"
            onClick={onCreateKey}
            className="px-3 py-1.5 bg-blue-600 text-white text-sm font-medium rounded-md hover:bg-blue-700 transition-colors"
          >
            Create API Key
          </button>
        </div>
      </div>

      {/* Delete confirmation (inline, no window.confirm) */}
      {pendingDelete && (
        <div
          data-testid="delete-confirm"
          role="alertdialog"
          aria-label={`Delete API key ${pendingDelete.name}`}
          className="flex flex-wrap items-center justify-between gap-3 px-6 py-3 bg-red-50 border-b border-red-200 text-sm text-red-800"
        >
          <span>
            Delete <strong>{pendingDelete.name}</strong>? Applications using it stop working
            immediately. This cannot be undone.
          </span>
          <span className="flex items-center gap-3">
            {deleteError && (
              <span data-testid="delete-error" className="text-red-700">
                {deleteError}
              </span>
            )}
            <button
              data-testid="cancel-delete"
              onClick={() => {
                setPendingDelete(null);
                setDeleteError(null);
              }}
              className="px-3 py-1 rounded-md border border-slate-300 bg-white text-slate-700"
            >
              Cancel
            </button>
            <button
              data-testid="confirm-delete"
              onClick={confirmDelete}
              disabled={deleting}
              className="px-3 py-1 rounded-md bg-red-600 text-white font-medium disabled:opacity-60"
            >
              {deleting ? 'Deleting…' : 'Delete key'}
            </button>
          </span>
        </div>
      )}

      {/* Loading state */}
      {loading && (
        <div data-testid="api-key-table-loading" className="divide-y divide-slate-100">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="flex items-center gap-4 px-6 py-4 animate-pulse">
              <div className="h-4 bg-slate-200 rounded w-1/4" />
              <div className="h-4 bg-slate-200 rounded w-1/4" />
              <div className="h-4 bg-slate-200 rounded w-1/4" />
              <div className="h-5 bg-slate-200 rounded w-16" />
            </div>
          ))}
        </div>
      )}

      {/* Error state */}
      {!loading && error && (
        <div data-testid="api-key-error-state" className="p-8 text-center text-red-600">
          <p className="font-medium">Failed to load API keys</p>
          <p className="text-sm mt-1 text-red-500">{error}</p>
        </div>
      )}

      {/* Empty state */}
      {!loading && !error && keys.length === 0 && (
        <div data-testid="api-key-empty-state" className="p-8 text-center text-slate-500">
          <p>No API keys found. Create one to get started.</p>
        </div>
      )}

      {/* Table */}
      {!loading && !error && keys.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-slate-600 uppercase text-xs tracking-wide">
              <tr>
                <th className="px-6 py-3 text-left">Name</th>
                <th className="px-6 py-3 text-left">Scopes</th>
                <th className="px-6 py-3 text-left">Created</th>
                <th className="px-6 py-3 text-left">Last used</th>
                <th className="px-6 py-3 text-left">Expires</th>
                <th className="px-6 py-3 text-left">Status</th>
                <th className="px-6 py-3 text-left">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {keys.map((key) => {
                const state = keyState(key);
                return (
                  <tr key={key.id} data-testid={`api-key-row-${key.id}`} className="hover:bg-slate-50">
                    <td className="px-6 py-4">
                      <div className="font-medium text-slate-900">{key.name}</div>
                      {key.description && (
                        <div className="text-xs text-slate-500 mt-0.5">{key.description}</div>
                      )}
                    </td>
                    <td className="px-6 py-4 text-slate-600 font-mono text-xs">
                      {key.scopes && key.scopes.length > 0 ? key.scopes.join(', ') : '—'}
                    </td>
                    <td data-testid={`created-at-${key.id}`} className="px-6 py-4 text-slate-600">
                      {formatDate(key.created_at)}
                    </td>
                    <td className="px-6 py-4 text-slate-600">{formatDate(key.last_used_at)}</td>
                    <td className="px-6 py-4 text-slate-600">{formatDate(key.expires_at)}</td>
                    <td className="px-6 py-4">
                      <span
                        data-testid={`status-badge-${key.id}`}
                        className={[
                          'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium',
                          STATE_CLASSES[state],
                        ].join(' ')}
                      >
                        {state}
                      </span>
                    </td>
                    <td className="px-6 py-4">
                      <button
                        data-testid={`delete-button-${key.id}`}
                        onClick={() => {
                          setDeleteError(null);
                          setPendingDelete(key);
                        }}
                        className="text-red-600 hover:text-red-800 text-sm font-medium"
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
