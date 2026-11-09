import React, { useEffect, useRef, useState } from 'react';
import { Dialog } from '@/components/ui/Dialog';
import { RbacService, UserPermissionsResponse } from '@modules/rbac';
import { USER_ROLE_LABELS, UserRole } from '@/types/admin';

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

/** The API's `base_role` (`analyst`) as the dashboard names it (`Analyst`). */
function builtInRoleLabel(baseRole: string | null | undefined): string {
  const key = String(baseRole ?? '').toUpperCase() as UserRole;
  return USER_ROLE_LABELS[key] ?? (baseRole || 'none');
}

/** What the dialog says above the lists (#891). */
export const PERMISSIONS_NOTE =
  'What this user can do: their built-in role, plus their custom roles and unexpired direct grants. ' +
  "Managing users, custom roles and grants, other users' API keys, holdouts and safety settings is not " +
  'listed: it depends on the built-in role or the superuser flag, never on a custom role or grant.';

/** The audit-log line: the API lists `audit_log: ["read"]` only for a user who reads other users' entries. */
export function auditLogLine(permissions: Record<string, string[]>): string {
  const actions = permissions.audit_log ?? [];
  return actions.includes('read')
    ? "Audit log: read, including other users' entries"
    : 'Audit log: own entries only';
}

/**
 * The Users page's Permissions dialog: what `GET /api/v1/rbac/users/{id}/permissions`
 * answers, which is what the permission checks allow (#891), plus the
 * built-in role, the custom roles that take effect and the superuser flag.
 */
export function EffectivePermissionsModal({
  isOpen,
  userId,
  userEmail,
  onClose,
}: EffectivePermissionsModalProps) {
  const [answer, setAnswer] = useState<UserPermissionsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (isOpen && userId) {
      setLoading(true);
      setError(null);
      setAnswer(null);

      RbacService.getUserPermissions(userId)
        .then((data) => {
          setAnswer(data);
        })
        .catch((err: Error) => {
          setError(err.message || 'Failed to load permissions');
        })
        .finally(() => {
          setLoading(false);
        });
    }
  }, [isOpen, userId]);

  const permissions = answer?.permissions ?? {};
  const rows = Object.entries(permissions).filter(
    ([resource, actions]) => resource !== 'audit_log' && actions.length > 0,
  );
  const customRoles = answer?.custom_roles ?? [];

  return (
    <Dialog
      open={isOpen}
      onClose={onClose}
      title="Effective permissions"
      subtitle={userEmail ? <p className="text-sm text-slate-500 mt-0.5">{userEmail}</p> : undefined}
      description={
        <p data-testid="permissions-access-note" className="text-sm text-slate-700">
          {PERMISSIONS_NOTE}
        </p>
      }
      headerAction={
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
      }
      initialFocusRef={closeRef}
      size="md-wide"
      testId="effective-permissions-modal"
    >
      <div>

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

        {/* What the user is, then what the checks allow */}
        {!loading && !error && answer && (
          <>
            <ul data-testid="permissions-summary" className="pt-3 space-y-1 text-sm text-slate-800">
              <li>Built-in role: {builtInRoleLabel(answer.base_role)}</li>
              <li>Custom roles: {customRoles.length > 0 ? customRoles.join(', ') : 'None'}</li>
              {answer.is_superuser && <li>Superuser: passes every permission check</li>}
            </ul>
            <ul data-testid="permissions-list" className="py-2 space-y-1 text-sm text-slate-800">
              {rows.map(([resource, actions]) => (
                <li key={resource}>
                  {resourceLabel(resource)}: {actions.join(', ')}
                </li>
              ))}
              <li data-testid="permissions-audit-log">{auditLogLine(permissions)}</li>
            </ul>
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
    </Dialog>
  );
}
