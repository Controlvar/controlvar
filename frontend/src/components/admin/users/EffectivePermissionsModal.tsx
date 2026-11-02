import React, { useEffect, useRef, useState } from 'react';
import { RbacService } from '@modules/rbac';

interface EffectivePermissionsModalProps {
  isOpen: boolean;
  userId: string | null;
  userEmail?: string;
  onClose: () => void;
}

const RESOURCE_LABELS: Record<string, string> = {
  experiment: 'Experiments',
  feature_flag: 'Feature flags',
  audit_log: 'Audit log',
};

/** `experiment` -> `Experiments`; a resource without a label is shown as the API names it. */
function resourceLabel(resource: string): string {
  return RESOURCE_LABELS[resource] ?? resource.charAt(0).toUpperCase() + resource.slice(1).replace(/_/g, ' ');
}

export function EffectivePermissionsModal({
  isOpen,
  userId,
  userEmail,
  onClose,
}: EffectivePermissionsModalProps) {
  const [permissions, setPermissions] = useState<Record<string, string[]>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (isOpen && userId) {
      setLoading(true);
      setError(null);
      setPermissions({});

      RbacService.getUserPermissions(userId)
        .then((data) => {
          setPermissions(data.permissions ?? {});
        })
        .catch((err: Error) => {
          setError(err.message || 'Failed to load permissions');
        })
        .finally(() => {
          setLoading(false);
        });
    }
  }, [isOpen, userId]);

  // Escape closes the dialog, as the Close buttons do.
  useEffect(() => {
    if (!isOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [isOpen, onClose]);

  // Focus moves into the dialog when it opens.
  useEffect(() => {
    if (isOpen) closeRef.current?.focus();
  }, [isOpen]);

  if (!isOpen) return null;

  const rows = Object.entries(permissions).filter(([, actions]) => actions.length > 0);

  return (
    <div
      data-testid="effective-permissions-modal"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      role="dialog"
      aria-modal="true"
      aria-labelledby="permissions-modal-title"
      aria-describedby="permissions-access-note"
    >
      <div className="bg-white rounded-lg shadow-xl w-full max-w-lg mx-4 p-6">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h2 id="permissions-modal-title" className="text-lg font-semibold text-slate-900">
              Recorded Permissions
            </h2>
            {userEmail && (
              <p className="text-sm text-slate-500 mt-0.5">{userEmail}</p>
            )}
          </div>
          <button
            type="button"
            data-testid="permissions-close-button"
            ref={closeRef}
            onClick={onClose}
            className="text-slate-600 hover:text-slate-900"
            aria-label="Close"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* No permission check reads custom roles or direct grants yet (#891). */}
        <p
          id="permissions-access-note"
          data-testid="permissions-access-note"
          className="text-sm text-slate-700"
        >
          {
            "This list adds the user's custom roles and direct grants to their built-in role. Custom roles and direct permission grants are recorded, but they do not change what anyone can do yet: each user's built-in role decides."
          }
        </p>

        {/* Loading */}
        {loading && (
          <div data-testid="permissions-loading" className="py-8 text-center text-slate-500">
            <div className="inline-block animate-spin h-6 w-6 border-2 border-blue-500 border-t-transparent rounded-full" />
            <p className="mt-2 text-sm">Loading permissions...</p>
          </div>
        )}

        {/* Error */}
        {!loading && error && (
          <div
            data-testid="permissions-error"
            className="p-4 bg-red-50 border border-red-200 rounded-md text-sm text-red-700"
          >
            {error}
          </div>
        )}

        {/* Permissions List */}
        {!loading && !error && (
          <>
            {rows.length === 0 ? (
              <p className="text-sm text-slate-600 py-4">No permissions assigned.</p>
            ) : (
              <ul data-testid="permissions-list" className="py-2 space-y-1 text-sm text-slate-800">
                {rows.map(([resource, actions]) => (
                  <li key={resource}>
                    {resourceLabel(resource)}: {actions.join(', ')}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}

        {/* Close */}
        <div className="mt-6 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-slate-700 bg-white border border-slate-300 rounded-md hover:bg-slate-50"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
