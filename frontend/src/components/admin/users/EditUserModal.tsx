import { useEffect, useRef, useState } from 'react';
import { AdminUser, AdminUserPatch, ApiKey, UserRole, USER_ROLE_LABELS } from '@/types/admin';
import { isApiError } from '@/services/api';
import { demoRefusalMessage } from '@/utils/demoMode';
import { RequiresModule } from '@/contexts/ModulesContext';
import { MODULES } from '@/services/modules';
import { Dialog } from '@/components/ui/Dialog';
import { keyState } from '@/components/admin/api-keys/ApiKeyTable';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

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
  /**
   * Every user's active API keys (#657), asked for when Active is cleared on
   * someone else's account. The route has no owner filter, so the dialog keeps
   * the rows whose `user_id` is this user's. Without it the dialog shows only
   * the general warning.
   */
  listUserKeys?: (userId: string) => Promise<ApiKey[]>;
}

/** What the dialog knows of the user's active API keys while Active is cleared. */
type KeysState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'ok'; keys: ApiKey[] }
  | { kind: 'error'; message: string; busy: boolean };

const ROLES: UserRole[] = ['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER'];

export const SELF_EDIT_NOTE =
  "You can't change your own role or deactivate yourself. Ask another administrator.";
export const DEACTIVATE_WARNING =
  "This user won't be able to sign in. Requests with a token they already hold are refused, " +
  'and API keys they created stop working — including keys your applications use.';
export const DEACTIVATE_KEYS_ADVICE =
  'To keep an application running, cancel, switch it to a key created by an account that is staying, ' +
  'then deactivate this user. Reactivating this user makes these keys work again, unless they ' +
  'have expired.';
export const KEYS_LOADING = 'Checking which API keys this user has…';
export const KEYS_NONE = 'This user has no active API keys.';
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
 * session stays: a reset is not a way to sign someone out. The dialog does
 * not know which roles the server requires it for, so it names both cases.
 */
export function twoFactorResetWarning(name: string): string {
  return (
    `${name} will sign in with their password alone until they set two-step sign-in up again, ` +
    'or, if their role requires it, set it up at their next sign-in. ' +
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

/** The line above the list of keys that stop working, and the sentence read out when it settles. */
function keysSummary(state: KeysState): string {
  switch (state.kind) {
    case 'loading':
      return KEYS_LOADING;
    case 'ok':
      if (state.keys.length === 0) return KEYS_NONE;
      return state.keys.length === 1
        ? 'The API key that stops working:'
        : `The ${state.keys.length} API keys that stop working:`;
    case 'error':
      return `Couldn't list this user's API keys: ${state.message}`;
    default:
      return '';
  }
}

/** `last used 11/19/2026, 3:04:05 PM`, or `never used`. */
function keyLastUsed(key: ApiKey): string {
  if (!key.last_used_at) return 'never used';
  const when = parseApiTimestamp(key.last_used_at);
  return `last used ${Number.isNaN(when.getTime()) ? key.last_used_at : when.toLocaleString()}`;
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
  listUserKeys,
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
  // The user's active API keys (#657). Kept while the dialog stays open: a
  // success is not asked for twice, a failure is asked for again.
  const [keys, setKeys] = useState<KeysState>({ kind: 'idle' });
  const summaryRef = useRef<HTMLParagraphElement>(null);
  // Counts openings, so an answer that settles after the dialog was closed and
  // opened again is not shown in the new one.
  const openingRef = useRef(0);
  const focusSummaryRef = useRef(false);

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
      openingRef.current += 1;
      focusSummaryRef.current = false;
      setKeys({ kind: 'idle' });
    }
  }, [user, isOpen]);

  useEffect(() => {
    if (reset === 'confirming') keepRef.current?.focus();
    if (reset === 'done') resetDoneRef.current?.focus();
  }, [reset]);

  // After "Try again" succeeds, focus moves to the line that now says what was found.
  useEffect(() => {
    if (keys.kind === 'ok' && focusSummaryRef.current) {
      focusSummaryRef.current = false;
      summaryRef.current?.focus();
    }
  }, [keys]);

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

  // Asks for the keys. Nothing the administrator does waits for it: Save is not
  // blocked while it runs (an emergency deactivation must not queue behind it).
  const requestKeys = async (retry: boolean) => {
    if (!listUserKeys) return;
    const opening = openingRef.current;
    const userId = user.id;
    if (retry) setKeys((s) => (s.kind === 'error' ? { ...s, busy: true } : s));
    else setKeys({ kind: 'loading' });
    try {
      const rows = await listUserKeys(userId);
      if (opening !== openingRef.current) return;
      focusSummaryRef.current = retry;
      setKeys({
        kind: 'ok',
        keys: rows.filter((k) => k.user_id === userId && keyState(k) === 'active'),
      });
    } catch (err) {
      if (opening !== openingRef.current) return;
      setKeys({
        kind: 'error',
        message: (err as Error)?.message || 'the request failed.',
        busy: false,
      });
    }
  };

  const handleActiveChange = () => {
    const next = !isActive;
    setIsActive(next);
    // Clearing Active on an active account: find out which keys that stops. A
    // success is kept for this opening; a failure, or nothing yet, is asked for.
    if (!next && user.is_active && listUserKeys && !isSelf) {
      if (keys.kind === 'idle' || (keys.kind === 'error' && !keys.busy)) void requestKeys(false);
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
            aria-describedby={
              isSelf ? 'edit-self-note' : showDeactivateWarning ? 'edit-deactivate-warning' : undefined
            }
            onChange={handleActiveChange}
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

        {/* Read out once, when the request settles; mounted empty with the dialog. */}
        {listUserKeys && !isSelf && (
          <div role="status" className="sr-only" data-testid="deactivate-keys-status">
            {showDeactivateWarning && (keys.kind === 'ok' || keys.kind === 'error')
              ? keysSummary(keys).replace(/:$/, '.')
              : ''}
          </div>
        )}

        {showDeactivateWarning && (
          <div
            data-testid="deactivate-warning"
            className="mt-2 p-3 bg-amber-50 border border-amber-200 rounded-md text-sm text-amber-800"
          >
            <p id="edit-deactivate-warning">{DEACTIVATE_WARNING}</p>
            {listUserKeys && keys.kind !== 'idle' && (
              <div data-testid="deactivate-keys" className="mt-2">
                <p
                  id="edit-deactivate-keys-summary"
                  ref={summaryRef}
                  tabIndex={-1}
                  data-testid="deactivate-keys-summary"
                  className="font-medium text-amber-900 focus:outline-none"
                >
                  {keysSummary(keys)}
                </p>
                {keys.kind === 'error' && (
                  <button
                    type="button"
                    data-testid="deactivate-keys-retry"
                    aria-disabled={keys.busy}
                    onClick={() => {
                      if (!keys.busy) void requestKeys(true);
                    }}
                    className="mt-2 px-3 py-1.5 text-sm font-medium text-amber-900 bg-white border border-amber-200 rounded-md hover:bg-amber-50"
                  >
                    {keys.busy ? 'Checking…' : 'Try again'}
                  </button>
                )}
                {keys.kind === 'ok' && keys.keys.length > 0 && (
                  <>
                    <ul
                      aria-labelledby="edit-deactivate-keys-summary"
                      data-testid="deactivate-keys-list"
                      className="mt-1 list-disc pl-5"
                    >
                      {keys.keys.map((k) => (
                        <li key={k.id} data-testid="deactivate-key">
                          <span className="font-medium break-words">{k.name}</span>
                          {' · '}
                          <span className="font-mono text-xs">
                            {k.scopes.length > 0 ? k.scopes.join(', ') : 'no scopes'}
                          </span>
                          {' · '}
                          <span>{keyLastUsed(k)}</span>
                        </li>
                      ))}
                    </ul>
                    <p className="mt-2" data-testid="deactivate-keys-advice">
                      {DEACTIVATE_KEYS_ADVICE}
                    </p>
                  </>
                )}
              </div>
            )}
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
          aria-describedby={
            showDeactivateWarning
              ? listUserKeys && keys.kind !== 'idle'
                ? 'edit-deactivate-warning edit-deactivate-keys-summary'
                : 'edit-deactivate-warning'
              : undefined
          }
          className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {saving ? 'Saving…' : 'Save changes'}
        </button>
      </div>
    </Dialog>
  );
}
