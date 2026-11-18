import { useEffect, useRef, useState } from 'react';
import { AdminUser, AdminUserPatch, UserRole, USER_ROLE_LABELS } from '@/types/admin';
import { isApiError } from '@/services/api';
import { demoRefusalMessage } from '@/utils/demoMode';
import { RequiresModule } from '@/contexts/ModulesContext';
import { MODULES } from '@/services/modules';
import { Dialog } from '@/components/ui/Dialog';

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
  /**
   * Turn off the user's two-step sign-in (#1812). Resolve on success; reject
   * with the request's error and the modal shows it. Without it the dialog
   * offers no reset.
   */
  onResetTwoFactor?: (userId: string) => Promise<void>;
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

/** Your own row: someone else resets your two-step sign-in (the API answers 400). */
export const TWO_FACTOR_RESET_SELF_NOTE = 'Ask another administrator to reset yours.';
/** The reset route under a provider other than local. */
export const TWO_FACTOR_NOT_LOCAL_MESSAGE =
  'This server does not manage sign-in, so two-step sign-in cannot be reset here.';

/**
 * What a reset does, said before it is confirmed. Deliberately says that the
 * session stays: a reset is not a way to sign someone out.
 */
export function twoFactorResetWarning(name: string): string {
  return (
    `${name} will sign in with their password alone until they set two-step sign-in up again. ` +
    'Their current session stays valid until it expires.'
  );
}

export function twoFactorResetDone(name: string): string {
  return `Two-step sign-in is off for ${name}.`;
}

/** What the modal shows when a reset fails. */
export function resetErrorMessage(err: unknown): string {
  if (isApiError(err) && err.status === 404) {
    return err.detail === 'User not found' ? NOT_FOUND_MESSAGE : TWO_FACTOR_NOT_LOCAL_MESSAGE;
  }
  return editErrorMessage(err);
}

/**
 * What the modal shows when a save fails. 400 and 409 carry the API's own
 * sentence (the self-change and Cognito refusals); 403 and 404 get copy that
 * says what to do; 422, 5xx and an unreachable API use the message `apiFetch`
 * already built for them.
 */
export function editErrorMessage(err: unknown): string {
  if (isApiError(err)) {
    const demo = demoRefusalMessage(err);
    if (demo) return demo;
    if (err.status === 403) return FORBIDDEN_MESSAGE;
    if (err.status === 404) return NOT_FOUND_MESSAGE;
    if (err.status === 422) return `Couldn't save: ${err.message}`;
    return err.message;
  }
  return (err as Error)?.message || "Couldn't save the changes.";
}

/**
 * Focus, the Tab trap, Escape, giving focus back and making the page inert are
 * the shared Dialog's (#369). Focus opens on Role, or on Cancel on the
 * signed-in user's own row, where Role and Active are disabled.
 */
export function EditUserModal({
  isOpen,
  user,
  currentUserId,
  onClose,
  onSave,
  onResetTwoFactor,
}: EditUserModalProps) {
  const [role, setRole] = useState<UserRole | null>(null);
  const [isActive, setIsActive] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  // Two-step sign-in reset (#1812): offered, asked to confirm, or done.
  const [reset, setReset] = useState<'offered' | 'confirming' | 'resetting' | 'done'>('offered');
  const roleRef = useRef<HTMLSelectElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const keepRef = useRef<HTMLButtonElement>(null);
  const resetDoneRef = useRef<HTMLParagraphElement>(null);

  const isSelf = !!user && !!currentUserId && user.id === currentUserId;

  // Reset the form whenever a (different) user is opened.
  useEffect(() => {
    if (user && isOpen) {
      setRole(user.role);
      setIsActive(user.is_active);
      setSaving(false);
      setError(null);
      setNotFound(false);
      setReset('offered');
    }
  }, [user, isOpen]);

  useEffect(() => {
    if (reset === 'confirming') keepRef.current?.focus();
    if (reset === 'done') resetDoneRef.current?.focus();
  }, [reset]);

  if (!isOpen || !user) return null;

  const changes: AdminUserPatch = {};
  if (role !== null && role !== user.role) changes.role = role;
  if (isActive !== user.is_active) changes.is_active = isActive;
  const hasChanges = Object.keys(changes).length > 0;
  const showDeactivateWarning = user.is_active && !isActive;
  const displayName = user.email ?? user.username;
  // Only for an account that has it on, and only where the page can reset it.
  const showTwoFactor = !!onResetTwoFactor && (user.totp_enabled === true || reset === 'done');

  // After a reset the list's copy of the user still says two-step sign-in is
  // on: reload it on the way out.
  const close = () => onClose(notFound || reset === 'done' ? { reload: true } : undefined);

  const handleReset = async () => {
    if (!onResetTwoFactor || reset === 'resetting') return;
    setReset('resetting');
    setError(null);
    try {
      await onResetTwoFactor(user.id);
      setReset('done');
    } catch (err) {
      setReset('confirming');
      setError(resetErrorMessage(err));
      if (isApiError(err) && err.status === 404 && err.detail === 'User not found') setNotFound(true);
    }
  };

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

  const roleDescribedBy = isSelf ? 'edit-role-help edit-self-note' : 'edit-role-help';

  return (
    <Dialog
      open
      onClose={close}
      title="Edit user"
      testId="edit-user-modal"
      initialFocusRef={isSelf ? cancelRef : roleRef}
    >
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

      {/* Two-step sign-in (#1812): shown only while the account has it on. */}
      {showTwoFactor && (
        <div className="mb-4 border-t border-slate-200 pt-4" data-testid="edit-two-factor">
          <p className="text-sm font-medium text-slate-700">Two-step sign-in</p>
          {reset === 'done' ? (
            <p
              ref={resetDoneRef}
              tabIndex={-1}
              role="status"
              data-testid="edit-two-factor-reset-done"
              className="mt-2 text-sm text-green-800 focus:outline-none"
            >
              {twoFactorResetDone(displayName)}
            </p>
          ) : isSelf ? (
            <p data-testid="edit-two-factor-self-note" className="mt-2 text-sm text-slate-600">
              On. {TWO_FACTOR_RESET_SELF_NOTE}
            </p>
          ) : reset === 'offered' ? (
            <div className="mt-2 flex items-center justify-between gap-3">
              <p className="text-sm text-slate-600">On.</p>
              <button
                type="button"
                data-testid="edit-two-factor-reset"
                onClick={() => {
                  setError(null);
                  setReset('confirming');
                }}
                className="px-3 py-1.5 text-sm font-medium text-red-700 bg-white border border-red-300 rounded-md hover:bg-red-50"
              >
                Reset two-step sign-in
              </button>
            </div>
          ) : (
            <div className="mt-2 p-3 bg-amber-50 border border-amber-200 rounded-md" data-testid="edit-two-factor-confirm">
              <p id="edit-two-factor-warning" className="text-sm text-amber-900">
                {twoFactorResetWarning(displayName)}
              </p>
              <div className="mt-3 flex justify-end gap-3">
                <button
                  type="button"
                  ref={keepRef}
                  data-testid="edit-two-factor-keep"
                  onClick={() => {
                    setError(null);
                    setReset('offered');
                  }}
                  disabled={reset === 'resetting'}
                  aria-describedby="edit-two-factor-warning"
                  className="px-3 py-1.5 text-sm font-medium text-slate-700 bg-white border border-slate-300 rounded-md hover:bg-slate-50 disabled:opacity-60"
                >
                  Keep it
                </button>
                <button
                  type="button"
                  data-testid="edit-two-factor-reset-confirm"
                  onClick={() => void handleReset()}
                  disabled={reset === 'resetting'}
                  aria-busy={reset === 'resetting'}
                  aria-describedby="edit-two-factor-warning"
                  className="px-3 py-1.5 text-sm font-medium text-white bg-red-600 rounded-md hover:bg-red-700 disabled:opacity-60"
                >
                  {reset === 'resetting' ? 'Resetting…' : 'Reset two-step sign-in'}
                </button>
              </div>
            </div>
          )}
        </div>
      )}

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
    </Dialog>
  );
}
