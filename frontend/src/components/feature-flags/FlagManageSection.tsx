import React, { useEffect, useId, useRef, useState } from 'react';
import { useRouter } from 'next/router';
import { UNREACHABLE } from '@/components/experiments/ExperimentManageSection';
import { isFlagArchived } from '@/components/feature-flags/ArchivedFlag';
import { Dialog, useDeferredAnnouncement } from '@/components/ui/Dialog';
import { isApiError } from '@/services/api';
import { demoRefusalMessage } from '@/utils/demoMode';
import { FeatureFlag, FeatureFlagsService } from '@/services/featureFlags';

// ---------------------------------------------------------------------------
// Copy. Fixed sentences only: nothing the server sends, in an error or in a
// refused bulk-toggle result, is shown -- except a read-only demo's refusal,
// told apart by its code (`utils/demoMode.ts`).
// ---------------------------------------------------------------------------

export const MANAGE_LABEL = 'Manage this flag';

export function archiveTitle(name: string): string {
  return `Archive ${name}?`;
}

export const ARCHIVE_DESCRIPTION =
  'It stops serving: every evaluation answers it as off, with its off value. You can unarchive it later.';

/** A refused archive: a 200 whose result says `success: false`, or a failed request. */
export const ARCHIVE_REFUSED = 'Could not archive this flag.';

export function archivedStatus(name: string): string {
  return `Archived ${name}.`;
}

export const UNARCHIVED = 'Unarchived. The flag is off; turn it on when you are ready.';

export function deleteTitle(name: string): string {
  return `Delete ${name}?`;
}

/** The delete Dialog's sentences (PLAN-v1.1 DESIGN E2), word for word. */
export function deleteDescription(key: string): string {
  return (
    `This cannot be undone. Code that still asks for ${key} gets a 404 and its SDK's fallback, ` +
    'so archive it instead while any code may still ask for it. ' +
    'Its rollout schedules and safety settings are deleted with it; its events are kept.'
  );
}

/** The label of the field the key is typed into. */
export function deleteFieldLabel(key: string): string {
  return `Type ${key} to delete it`;
}

/** A failed delete that no status has its own sentence for. */
export const DELETE_REFUSED = 'Could not delete this flag.';

/** Shown on the flag list after a delete. */
export function deletedStatus(name: string): string {
  return `Deleted ${name}.`;
}

export type FlagManageAction = 'archive' | 'unarchive' | 'delete';

export const FLAG_MANAGE_ERROR_COPY: Record<FlagManageAction, Record<number, string> & { fallback: string }> = {
  archive: {
    403: 'Archiving a flag needs permission to change feature flags, which the ADMIN and DEVELOPER roles have.',
    404: 'This flag no longer exists.',
    fallback: ARCHIVE_REFUSED,
  },
  unarchive: {
    403: 'Unarchiving a flag needs permission to change feature flags, which the ADMIN and DEVELOPER roles have.',
    404: 'This flag no longer exists.',
    fallback: 'Could not unarchive this flag.',
  },
  delete: {
    403: 'Deleting a flag needs permission to delete feature flags, which the ADMIN and DEVELOPER roles have.',
    404: 'This flag no longer exists.',
    fallback: DELETE_REFUSED,
  },
};

// ---------------------------------------------------------------------------
// The sentence the flag list shows after a delete. The flag's page writes it
// here and moves to the list, which takes it once: it lives only in this
// browser tab's memory, so a reload or a link never shows it.
// ---------------------------------------------------------------------------

let deletedNotice: string | null = null;

/** Whether a delete has left the list a sentence to show; reads without taking it. */
export function hasDeletedFlagNotice(): boolean {
  return deletedNotice !== null;
}

/** The list's "Deleted {name}." once, then nothing until the next delete. */
export function takeDeletedFlagNotice(): string | null {
  const notice = deletedNotice;
  deletedNotice = null;
  return notice;
}

/**
 * The fixed sentence for a failed request, chosen by the answer's status only;
 * a time-out shows the dashboard's own time-out sentence (#1040), since the
 * change may still have been made.
 */
export function flagManageErrorCopy(action: FlagManageAction, err: unknown): string {
  if (!isApiError(err)) return FLAG_MANAGE_ERROR_COPY[action].fallback;
  if (err.timedOut) return err.message;
  if (err.status === 0) return UNREACHABLE;
  const demo = demoRefusalMessage(err);
  if (demo) return demo;
  return FLAG_MANAGE_ERROR_COPY[action][err.status] ?? FLAG_MANAGE_ERROR_COPY[action].fallback;
}

/** What the page takes from a change: the flag's status, and when the server has it. */
export interface FlagStatusChange {
  status: string;
  is_active: boolean;
  updated_at?: string;
}

interface FlagManageSectionProps {
  flag: Pick<FeatureFlag, 'id' | 'name' | 'key' | 'status'>;
  /** `canChangeFeatureFlags` (FEATURE_FLAG UPDATE): archive and unarchive check exactly that. */
  canChange: boolean;
  /** `canDeleteFeatureFlags` (FEATURE_FLAG DELETE): the delete route checks exactly that. */
  canDelete: boolean;
  onChanged: (change: FlagStatusChange) => void;
}

const BUTTON =
  'px-3 py-1.5 rounded-md text-sm font-medium border transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
const SECONDARY = `${BUTTON} bg-white text-slate-700 border-slate-300 hover:bg-slate-50`;
const DANGER = `${BUTTON} bg-white text-red-700 border-red-300 hover:bg-red-50`;

/**
 * Archive, unarchive and delete one flag (#442 E1, E2), on the flag's page.
 *
 * Each control is shown only to a user the API would let use it (ADMIN,
 * DEVELOPER, a superuser, or in the full profile a custom role or direct
 * grant with the pair the route checks): the bulk toggle and the unarchive
 * route check `can_act_on_feature_flag(..., UPDATE)`, the delete route
 * `(..., DELETE)`, and a grant can give either one alone. Anyone with
 * neither gets nothing here; the page's role note says why.
 *
 * - Archive asks first, in the shared Dialog: Cancel has focus, Escape is
 *   Cancel, and nothing is sent until Archive is pressed in it. The request
 *   is the bulk toggle for this one flag, which answers 200 for a flag it
 *   refused, so only a result with `success: true` counts as archived.
 * - Unarchive asks nothing: an archived flag and an unarchived one both
 *   serve off, and the flag comes back INACTIVE, so nothing is turned on.
 * - Delete cannot be undone, so it asks in the Dialog for the flag's key:
 *   Delete stays disabled until the typed text equals the key exactly, and
 *   nothing is sent until Delete is pressed. Cancel has focus, and Escape and
 *   Cancel send nothing. After the 204 the page moves to the flag list, which
 *   says "Deleted {name}.".
 */
export function FlagManageSection({ flag, canChange, canDelete, onChanged }: FlagManageSectionProps) {
  const router = useRouter();
  const archived = isFlagArchived(flag);
  const [confirming, setConfirming] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [typedKey, setTypedKey] = useState('');
  const [pending, setPending] = useState<FlagManageAction | null>(null);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Written after the dialog has closed, so it is not changed under an inert page.
  const [status, setStatus, announceAfterClose] = useDeferredAnnouncement();
  // The Archive or Unarchive button, whichever is shown. After a change the
  // one that was pressed is replaced by the other, so focus is moved to it
  // once it is rendered.
  const actionButton = useRef<HTMLButtonElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const deleteButton = useRef<HTMLButtonElement>(null);
  const deleteCancelButton = useRef<HTMLButtonElement>(null);
  const [focusAction, setFocusAction] = useState(false);
  const keyFieldId = useId();

  useEffect(() => {
    if (!focusAction) return;
    actionButton.current?.focus();
    setFocusAction(false);
  }, [focusAction, archived]);

  if (!canChange && !canDelete) return null;

  /** Only the exact key, as typed: no trimming, no change of case. */
  const keyMatches = typedKey === flag.key;

  const openArchive = () => {
    setError(null);
    setDialogError(null);
    setStatus('');
    setConfirming(true);
  };

  const closeArchive = () => {
    setConfirming(false);
    setDialogError(null);
  };

  const archive = async () => {
    setDialogError(null);
    setPending('archive');
    try {
      const answer = await FeatureFlagsService.archive(flag.id);
      if (answer.results?.[0]?.success !== true) {
        setDialogError(ARCHIVE_REFUSED);
        return;
      }
      setConfirming(false);
      onChanged({ status: 'archived', is_active: false });
      announceAfterClose(archivedStatus(flag.name));
      setFocusAction(true);
    } catch (err) {
      setDialogError(flagManageErrorCopy('archive', err));
    } finally {
      setPending(null);
    }
  };

  const unarchive = async () => {
    setError(null);
    setStatus('');
    setPending('unarchive');
    try {
      const updated = await FeatureFlagsService.unarchive(flag.id);
      const next = typeof updated.status === 'string' ? updated.status.toLowerCase() : 'inactive';
      onChanged({ status: next, is_active: next === 'active', updated_at: updated.updated_at });
      // The route returns a flag that was no longer archived unchanged (someone
      // else unarchived it, and may have turned it on): then the pill says so,
      // and "The flag is off" would not be true.
      setStatus(next === 'inactive' ? UNARCHIVED : '');
      setFocusAction(true);
    } catch (err) {
      setError(flagManageErrorCopy('unarchive', err));
    } finally {
      setPending(null);
    }
  };

  const openDelete = () => {
    setError(null);
    setDeleteError(null);
    setStatus('');
    setTypedKey('');
    setConfirmingDelete(true);
  };

  const closeDelete = () => {
    setConfirmingDelete(false);
    setDeleteError(null);
    setTypedKey('');
  };

  const runDelete = async () => {
    // Delete is disabled until the key matches; a click that still arrives sends nothing.
    if (!keyMatches || pending !== null) return;
    setDeleteError(null);
    setPending('delete');
    try {
      await FeatureFlagsService.delete(flag.id);
    } catch (err) {
      setDeleteError(flagManageErrorCopy('delete', err));
      setPending(null);
      return;
    }
    // The flag is gone: its page has nothing left to show. `pending` stays
    // set, so no other control here is pressed on the way to the list.
    deletedNotice = deletedStatus(flag.name);
    setConfirmingDelete(false);
    void router.push('/feature-flags');
  };

  return (
    <div className="mt-4" data-testid="flag-manage">
      <div role="group" aria-label={MANAGE_LABEL} className="flex flex-wrap items-center gap-2">
        {canChange && (
          <div data-testid="flag-archive">
            {archived ? (
              <button
                ref={actionButton}
                type="button"
                onClick={() => void unarchive()}
                disabled={pending !== null}
                className={SECONDARY}
                data-testid="flag-unarchive-button"
              >
                {pending === 'unarchive' ? 'Unarchiving…' : 'Unarchive'}
              </button>
            ) : (
              <button
                ref={actionButton}
                type="button"
                onClick={openArchive}
                disabled={pending !== null}
                className={DANGER}
                data-testid="flag-archive-button"
              >
                Archive
              </button>
            )}
          </div>
        )}
        {canDelete && (
          <div data-testid="flag-delete">
            <button
              ref={deleteButton}
              type="button"
              onClick={openDelete}
              disabled={pending !== null}
              className={DANGER}
              data-testid="flag-delete-button"
            >
              Delete
            </button>
          </div>
        )}
      </div>

      {/* Always mounted, so a screen reader announces the change. */}
      <p
        role="status"
        className={`text-sm text-green-700${status ? ' mt-3' : ''}`}
        data-testid="flag-manage-status"
      >
        {status}
      </p>

      {error && (
        <div
          role="alert"
          className="mt-3 rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
          data-testid="flag-manage-error"
        >
          {error}
        </div>
      )}

      <Dialog
        open={confirming}
        onClose={closeArchive}
        title={archiveTitle(flag.name)}
        description={ARCHIVE_DESCRIPTION}
        busy={pending === 'archive'}
        initialFocusRef={cancelButton}
        returnFocus={() => actionButton.current}
        testId="flag-archive-dialog"
      >
        {dialogError && (
          <div
            role="alert"
            className="mb-4 rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
            data-testid="flag-archive-error"
          >
            {dialogError}
          </div>
        )}
        <div className="flex justify-end gap-3">
          <button
            ref={cancelButton}
            type="button"
            onClick={closeArchive}
            disabled={pending === 'archive'}
            className="px-4 py-2 text-sm font-medium text-slate-700 border border-slate-300 rounded-md bg-white hover:bg-slate-50 disabled:opacity-50 disabled:cursor-not-allowed"
            data-testid="flag-archive-cancel"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void archive()}
            disabled={pending === 'archive'}
            className="px-4 py-2 text-sm font-medium bg-red-600 text-white rounded-md hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed"
            data-testid="flag-archive-confirm"
          >
            {pending === 'archive' ? 'Archiving…' : 'Archive'}
          </button>
        </div>
      </Dialog>

      <Dialog
        open={confirmingDelete}
        onClose={closeDelete}
        title={deleteTitle(flag.name)}
        description={deleteDescription(flag.key)}
        busy={pending === 'delete'}
        initialFocusRef={deleteCancelButton}
        returnFocus={() => deleteButton.current}
        testId="flag-delete-dialog"
      >
        {deleteError && (
          <div
            role="alert"
            className="mb-4 rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
            data-testid="flag-delete-error"
          >
            {deleteError}
          </div>
        )}
        <div className="mb-4">
          <label htmlFor={keyFieldId} className="block text-sm font-medium text-slate-700 mb-1">
            {deleteFieldLabel(flag.key)}
          </label>
          <input
            id={keyFieldId}
            type="text"
            value={typedKey}
            onChange={(event) => setTypedKey(event.target.value)}
            disabled={pending === 'delete'}
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm font-mono text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-600 disabled:opacity-50"
            data-testid="flag-delete-key-input"
          />
        </div>
        <div className="flex justify-end gap-3">
          <button
            ref={deleteCancelButton}
            type="button"
            onClick={closeDelete}
            disabled={pending === 'delete'}
            className="px-4 py-2 text-sm font-medium text-slate-700 border border-slate-300 rounded-md bg-white hover:bg-slate-50 disabled:opacity-50 disabled:cursor-not-allowed"
            data-testid="flag-delete-cancel"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void runDelete()}
            disabled={!keyMatches || pending === 'delete'}
            className="px-4 py-2 text-sm font-medium bg-red-600 text-white rounded-md hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed"
            data-testid="flag-delete-confirm"
          >
            {pending === 'delete' ? 'Deleting…' : 'Delete'}
          </button>
        </div>
      </Dialog>
    </div>
  );
}
