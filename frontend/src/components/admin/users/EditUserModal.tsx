import React, { useEffect, useRef, useState } from 'react';
import { AdminUser, AdminUserPatch, UserRole, USER_ROLE_LABELS } from '@/types/admin';
import { isApiError } from '@/services/api';
import { RequiresModule } from '@/contexts/ModulesContext';
import { MODULES } from '@/services/modules';

export interface EditUserCloseOptions {
  /** The list is out of date (the user was not found); reload it. */
  reload?: boolean;
}

interface EditUserModalProps {
  isOpen: boolean;
  user: AdminUser | null;
  /** The signed-in user's id. Their own row cannot change its role or be deactivated. */
  currentUserId?: string | null;
  onClose: (options?: EditUserCloseOptions) => void;
  /**
   * Save `changes` (only the fields that differ from `user`). Resolve on
   * success; reject with the request's error and the modal shows it.
   */
  onSave: (userId: string, changes: AdminUserPatch) => Promise<void>;
}

const ROLES: UserRole[] = ['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER'];

export const SELF_EDIT_NOTE =
  "You can't change your own role or deactivate yourself. Ask another administrator.";
export const DEACTIVATE_WARNING =
  "This user won't be able to sign in. Requests with a token they already hold are refused, " +
  'and API keys they created stop working — including keys your applications use.';
export const FORBIDDEN_MESSAGE =
  "You don't have permission to change users. Only a superuser can do this. If you were one a " +
  'moment ago, your access may have changed: sign in again to check.';
export const NOT_FOUND_MESSAGE =
  'This user no longer exists. Close this dialog to refresh the list.';

/**
 * What the modal shows when a save fails. 400 and 409 carry the API's own
 * sentence (the self-change and Cognito refusals); 403 and 404 get copy that
 * says what to do; 422, 5xx and an unreachable API use the message `apiFetch`
 * already built for them.
 */
export function editErrorMessage(err: unknown): string {
  if (isApiError(err)) {
    if (err.status === 403) return FORBIDDEN_MESSAGE;
    if (err.status === 404) return NOT_FOUND_MESSAGE;
    if (err.status === 422) return `Couldn't save: ${err.message}`;
    return err.message;
  }
  return (err as Error)?.message || "Couldn't save the changes.";
}

const FOCUSABLE =
  'a[href], button:not([disabled]), select:not([disabled]), input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function EditUserModal({ isOpen, user, currentUserId, onClose, onSave }: EditUserModalProps) {
  const [role, setRole] = useState<UserRole | null>(null);
  const [isActive, setIsActive] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const roleRef = useRef<HTMLSelectElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  const isSelf = !!user && !!currentUserId && user.id === currentUserId;

  // Reset the form whenever a (different) user is opened.
  useEffect(() => {
    if (user && isOpen) {
      setRole(user.role);
      setIsActive(user.is_active);
      setSaving(false);
      setError(null);
      setNotFound(false);
    }
  }, [user, isOpen]);

  // Move focus into the dialog when it opens.
  useEffect(() => {
    if (!isOpen || !user) return;
    if (roleRef.current && !roleRef.current.disabled) {
      roleRef.current.focus();
    } else {
      cancelRef.current?.focus();
    }
  }, [isOpen, user]);

  if (!isOpen || !user) return null;

  const changes: AdminUserPatch = {};
  if (role !== null && role !== user.role) changes.role = role;
  if (isActive !== user.is_active) changes.is_active = isActive;
  const hasChanges = Object.keys(changes).length > 0;
  const showDeactivateWarning = user.is_active && !isActive;

  const close = () => onClose(notFound ? { reload: true } : undefined);

  const handleSave = async () => {
    if (!hasChanges || saving) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(user.id, changes);
    } catch (err) {
      setError(editErrorMessage(err));
      if (isApiError(err) && err.status === 404) setNotFound(true);
    } finally {
      setSaving(false);
    }
  };

  // Escape closes; Tab and Shift+Tab stay inside the dialog.
  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      close();
      return;
    }
    if (e.key !== 'Tab' || !dialogRef.current) return;
    const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || !dialogRef.current.contains(active))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (active === last || !dialogRef.current.contains(active))) {
      e.preventDefault();
      first.focus();
    }
  };

  const roleDescribedBy = isSelf ? 'edit-role-help edit-self-note' : 'edit-role-help';

  return (
    <div
      data-testid="edit-user-modal"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      role="dialog"
      aria-modal="true"
      aria-labelledby="edit-modal-title"
      onKeyDown={handleKeyDown}
      ref={dialogRef}
    >
      <div className="bg-white rounded-lg shadow-xl w-full max-w-md mx-4 p-6">
        <h2 id="edit-modal-title" className="text-lg font-semibold text-slate-900 mb-1">
          Edit user
        </h2>
        <p className="text-sm text-slate-600 mb-4">
          <span className="font-medium text-slate-800">{user.username}</span>
          {' · '}
          {user.email ?? <span className="italic">No email</span>}
        </p>

        {/* Role */}
        <div className="mb-4">
          <label htmlFor="edit-role" className="block text-sm font-medium text-slate-700 mb-1">
            Role
          </label>
          <select
            id="edit-role"
            ref={roleRef}
            data-testid="edit-role-select"
            value={role ?? ''}
            disabled={isSelf}
            aria-describedby={roleDescribedBy}
            onChange={(e) => setRole(e.target.value as UserRole)}
            className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-slate-100 disabled:text-slate-500"
          >
            {role === null && (
              <option value="" disabled>
                No role set: choose one
              </option>
            )}
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {USER_ROLE_LABELS[r]}
              </option>
            ))}
          </select>
          <div id="edit-role-help" data-testid="edit-role-help" className="mt-1 text-xs text-slate-600">
            <p>Opening this admin area needs superuser access, shown in the Superuser column.</p>
            <RequiresModule name={MODULES.RBAC}>
              <p data-testid="edit-role-help-custom">
                Custom roles and direct permission grants (full profile) add to the role set here.
                Assign them through the RBAC API.
              </p>
            </RequiresModule>
            <RequiresModule name={MODULES.SSO}>
              <p data-testid="edit-role-help-sso">
                If this user signs in with SSO and one of their groups is mapped to a role, that
                sign-in replaces the role set here.
              </p>
            </RequiresModule>
          </div>
        </div>

        {/* Active toggle */}
        <div className="mb-4">
          <div className="flex items-center gap-3">
            <input
              id="edit-active"
              data-testid="edit-active-toggle"
              type="checkbox"
              checked={isActive}
              disabled={isSelf}
              aria-describedby={isSelf ? 'edit-self-note' : undefined}
              onChange={() => setIsActive((v) => !v)}
              className="h-4 w-4 text-blue-600 border-slate-300 rounded focus:ring-blue-500"
            />
            <label htmlFor="edit-active" className="text-sm font-medium text-slate-700">
              Active
            </label>
          </div>

          {isSelf && (
            <p id="edit-self-note" data-testid="edit-self-note" className="mt-2 text-sm text-slate-600">
              {SELF_EDIT_NOTE}
            </p>
          )}

          {showDeactivateWarning && (
            <div
              data-testid="deactivate-warning"
              className="mt-2 p-3 bg-amber-50 border border-amber-200 rounded-md text-sm text-amber-800"
            >
              {DEACTIVATE_WARNING}
            </div>
          )}
        </div>

        {error && (
          <div
            role="alert"
            data-testid="edit-error-message"
            className="mb-4 p-3 bg-red-50 border border-red-200 rounded-md text-sm text-red-700"
          >
            {error}
          </div>
        )}

        {/* Actions */}
        <div className="flex justify-end gap-3 mt-6">
          <button
            type="button"
            ref={cancelRef}
            data-testid="edit-cancel-button"
            onClick={close}
            className="px-4 py-2 text-sm font-medium text-slate-700 bg-white border border-slate-300 rounded-md hover:bg-slate-50"
          >
            Cancel
          </button>
          <button
            type="button"
            data-testid="edit-save-button"
            onClick={handleSave}
            disabled={!hasChanges || saving}
            aria-busy={saving}
            className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {saving ? 'Saving…' : 'Save changes'}
          </button>
        </div>
      </div>
    </div>
  );
}
