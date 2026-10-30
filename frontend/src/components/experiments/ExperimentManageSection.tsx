import React, { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/router';
import { isApiError } from '@/services/api';
import { ExperimentDetailsUpdate, ExperimentsService } from '@/services/experiments';
import { Experiment } from '@/types/experiments';
import { canChangeExperiment, canCreateExperiment } from '@/utils/experimentPermissions';
import { DESIGN_HEADING, ExperimentDesignForm } from './ExperimentDesignForm';

/** The API's limits on these fields (`ExperimentUpdate`). */
export const NAME_MAX = 100;
export const TEXT_MAX = 2000;

export type ManageAction = 'clone' | 'edit' | 'delete' | 'design';

/**
 * Fixed copy for every failure. Nothing the server sends in an error is shown:
 * a detail can be stale, or a stack trace, and the page says what it knows in
 * its own words instead.
 */
export const UNREACHABLE = 'Could not reach the server. Check your connection and try again.';

export const ERROR_COPY: Record<ManageAction, Record<number, string> & { fallback: string }> = {
  clone: {
    403: 'Cloning an experiment requires the ADMIN or DEVELOPER role.',
    404: 'This experiment no longer exists, so it cannot be cloned.',
    409: "This experiment's targeting uses a segment that is not active, so it cannot be cloned.",
    501: 'This server cannot run split-URL experiments, so this one cannot be cloned.',
    fallback: 'Could not clone this experiment. Try again.',
  },
  edit: {
    400: 'This experiment is no longer a draft, so its details cannot be changed. Reload the page to see its status.',
    403: 'Changing an experiment requires the ADMIN or DEVELOPER role.',
    404: 'This experiment no longer exists.',
    422:
      'The details were not accepted. The name must be 1 to 100 characters, and the description ' +
      'and hypothesis at most 2,000 each.',
    fallback: 'Could not save the details. Try again.',
  },
  delete: {
    400: 'This experiment has started and can no longer be deleted. Archive it once it is complete.',
    403: 'Deleting an experiment requires the ADMIN or DEVELOPER role.',
    404: 'This experiment no longer exists.',
    fallback: 'Could not delete this draft. Try again.',
  },
  design: {
    400:
      'This experiment is no longer a draft, so its variants and metrics cannot be changed. ' +
      'Reload the page to see its status.',
    403: 'Changing an experiment requires the ADMIN or DEVELOPER role.',
    404: 'This experiment no longer exists.',
    422:
      'The variants and metrics were not accepted. Check that the split adds up to 100%, one ' +
      'variant is the control, and every metric has a name and an event name.',
    fallback: 'Could not save the variants and metrics. Try again.',
  },
};

export const DELETE_CONFIRM =
  'Delete this draft? It has never run, so no data is lost. This cannot be undone.';

export const EDIT_NOTE =
  'Only the name, description and hypothesis are changed here; use Edit variants and metrics for the rest.';

export const NAME_REQUIRED = 'Enter a name.';

export const SAVED = 'Details saved.';

export const DESIGN_SAVED = 'Variants and metrics saved.';

/** The fixed sentence for a failed action, chosen by the answer's status only. */
export function errorCopy(action: ManageAction, err: unknown): string {
  if (!isApiError(err)) return ERROR_COPY[action].fallback;
  if (err.status === 0) return UNREACHABLE;
  return ERROR_COPY[action][err.status] ?? ERROR_COPY[action].fallback;
}

export interface DetailsDraft {
  name: string;
  description: string;
  hypothesis: string;
}

/** A text field as the API stores it: trimmed, and null when empty. */
function optionalText(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

/**
 * The body of a details edit: exactly the fields that changed, out of name,
 * description and hypothesis. Never variants or metrics, which the update
 * route would replace wholesale, and never any other field.
 */
export function detailsChanges(experiment: Experiment, draft: DetailsDraft): ExperimentDetailsUpdate {
  const body: ExperimentDetailsUpdate = {};
  const name = draft.name.trim();
  if (name !== experiment.name) body.name = name;
  const description = optionalText(draft.description);
  if (description !== (experiment.description ?? null)) body.description = description;
  const hypothesis = optionalText(draft.hypothesis);
  if (hypothesis !== (experiment.hypothesis ?? null)) body.hypothesis = hypothesis;
  return body;
}

interface ExperimentManageSectionProps {
  experiment: Experiment;
  user: { role?: string; is_superuser?: boolean } | null | undefined;
  /** Called with the API's answer after a details or a design edit. */
  onSaved: (updated: Experiment) => void;
  /**
   * Told, in the same render, when the variants and metrics form opens or
   * closes, so the page can take its read-only Variants and Metrics cards
   * away while it is open. The form is shown on a DRAFT only, so the page
   * shows its cards again once the experiment is not one, without a call.
   */
  onDesignOpenChange?: (open: boolean) => void;
}

const BUTTON =
  'px-3 py-1.5 rounded-md text-sm font-medium border transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
const SECONDARY = `${BUTTON} bg-white text-slate-700 border-slate-300 hover:bg-slate-50`;
const DANGER = `${BUTTON} bg-white text-red-700 border-red-300 hover:bg-red-50`;

/**
 * Clone an experiment, and edit or delete a draft. It sits apart from the
 * lifecycle buttons, and offers only what the API would accept:
 * - Clone: any status, to a role that may create experiments.
 * - Edit details, Edit variants and metrics, and Delete draft: a DRAFT
 *   experiment, to a role that may change experiments. Who created the
 *   experiment is not considered.
 * ANALYST and VIEWER get nothing, so the section renders nothing for them.
 */
export function ExperimentManageSection({
  experiment,
  user,
  onSaved,
  onDesignOpenChange,
}: ExperimentManageSectionProps) {
  const router = useRouter();
  const isDraft = experiment.status === 'draft';
  const mayClone = canCreateExperiment(user);
  const mayChangeDraft = isDraft && canChangeExperiment(user);

  const [pending, setPending] = useState<ManageAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<'details' | 'design' | null>(null);
  const [draft, setDraft] = useState<DetailsDraft | null>(null);
  const [nameError, setNameError] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [designOpen, setDesignOpen] = useState(false);
  // Where focus returns once a form or confirmation closes. Set here and
  // applied after the render, when the button is enabled again.
  const [returnFocus, setReturnFocus] = useState<'edit' | 'design' | 'delete' | null>(null);

  const editButton = useRef<HTMLButtonElement>(null);
  const designButton = useRef<HTMLButtonElement>(null);
  const deleteButton = useRef<HTMLButtonElement>(null);
  const nameInput = useRef<HTMLInputElement>(null);
  const confirmRegion = useRef<HTMLDivElement>(null);

  const editing = draft !== null;
  // Only a draft is ever edited: once it starts, the form goes.
  const designing = designOpen && mayChangeDraft;

  useEffect(() => {
    if (editing) nameInput.current?.focus();
  }, [editing]);

  useEffect(() => {
    if (confirmingDelete) confirmRegion.current?.focus();
  }, [confirmingDelete]);

  useEffect(() => {
    if (returnFocus === null || pending !== null) return;
    const button = { edit: editButton, design: designButton, delete: deleteButton }[returnFocus];
    button.current?.focus();
    setReturnFocus(null);
  }, [returnFocus, pending]);

  if (!mayClone && !mayChangeDraft) return null;

  // The page hears it in the same render, so its cards and the form never
  // show together or both go missing.
  const setDesign = (open: boolean) => {
    setDesignOpen(open);
    onDesignOpenChange?.(open);
  };

  const clone = async () => {
    setError(null);
    setSaved(null);
    setPending('clone');
    try {
      const created = await ExperimentsService.clone(experiment.id);
      await router.push(`/experiments/${created.id}`);
    } catch (err) {
      setError(errorCopy('clone', err));
    } finally {
      setPending(null);
    }
  };

  const openEdit = () => {
    setError(null);
    setSaved(null);
    setNameError(false);
    setConfirmingDelete(false);
    setDesign(false);
    setDraft({
      name: experiment.name,
      description: experiment.description ?? '',
      hypothesis: experiment.hypothesis ?? '',
    });
  };

  const closeEdit = () => {
    setDraft(null);
    setNameError(false);
    setReturnFocus('edit');
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!draft) return;
    setError(null);
    if (draft.name.trim() === '') {
      setNameError(true);
      nameInput.current?.focus();
      return;
    }
    setNameError(false);
    const body = detailsChanges(experiment, draft);
    if (Object.keys(body).length === 0) {
      closeEdit();
      return;
    }
    setPending('edit');
    try {
      const updated = await ExperimentsService.updateDetails(experiment.id, body);
      onSaved(updated);
      setDraft(null);
      setSaved('details');
      setReturnFocus('edit');
    } catch (err) {
      setError(errorCopy('edit', err));
    } finally {
      setPending(null);
    }
  };

  const openDesign = () => {
    setError(null);
    setSaved(null);
    setConfirmingDelete(false);
    setDraft(null);
    setNameError(false);
    setDesign(true);
  };

  const closeDesign = () => {
    setDesign(false);
    setReturnFocus('design');
  };

  const designSaved = (updated: Experiment) => {
    onSaved(updated);
    setDesign(false);
    setSaved('design');
    setReturnFocus('design');
  };

  const askDelete = () => {
    setError(null);
    setSaved(null);
    setDraft(null);
    setDesign(false);
    setConfirmingDelete(true);
  };

  const cancelDelete = () => {
    setConfirmingDelete(false);
    setReturnFocus('delete');
  };

  const runDelete = async () => {
    setConfirmingDelete(false);
    setError(null);
    setPending('delete');
    try {
      await ExperimentsService.delete(experiment.id);
      await router.push('/experiments');
    } catch (err) {
      setError(errorCopy('delete', err));
      setPending(null);
    }
  };

  const busy = pending !== null;
  const nameErrorId = 'manage-edit-name-error';

  return (
    <div className="mt-4" data-testid="experiment-manage">
      <div role="group" aria-label="Manage this experiment" className="flex flex-wrap items-center gap-2">
        {mayClone && (
          <button
            type="button"
            onClick={() => void clone()}
            disabled={busy}
            className={SECONDARY}
            data-testid="experiment-clone"
          >
            {pending === 'clone' ? 'Cloning…' : 'Clone'}
          </button>
        )}
        {mayChangeDraft && (
          <>
            <button
              ref={editButton}
              type="button"
              onClick={openEdit}
              disabled={busy || editing}
              className={SECONDARY}
              data-testid="experiment-edit-details"
            >
              Edit details
            </button>
            <button
              ref={designButton}
              type="button"
              onClick={openDesign}
              disabled={busy || designing}
              className={SECONDARY}
              data-testid="experiment-edit-variants"
            >
              {DESIGN_HEADING}
            </button>
            <button
              ref={deleteButton}
              type="button"
              onClick={askDelete}
              disabled={busy || confirmingDelete}
              className={DANGER}
              data-testid="experiment-delete"
            >
              {pending === 'delete' ? 'Deleting…' : 'Delete draft'}
            </button>
          </>
        )}
      </div>

      {saved === 'details' && (
        <p role="status" className="mt-3 text-sm text-green-700" data-testid="manage-saved">
          {SAVED}
        </p>
      )}

      {saved === 'design' && (
        <p role="status" className="mt-3 text-sm text-green-700" data-testid="design-saved">
          {DESIGN_SAVED}
        </p>
      )}

      {designing && (
        <ExperimentDesignForm
          experiment={experiment}
          onSaved={designSaved}
          onCancel={closeDesign}
          describeError={(err) => errorCopy('design', err)}
        />
      )}

      {confirmingDelete && (
        <div
          ref={confirmRegion}
          role="dialog"
          aria-label="Confirm Delete draft"
          tabIndex={-1}
          onKeyDown={(event) => {
            if (event.key === 'Escape') cancelDelete();
          }}
          className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 focus:outline-none focus:ring-2 focus:ring-blue-600"
          data-testid="manage-delete-confirm"
        >
          <p className="text-sm text-amber-900">{DELETE_CONFIRM}</p>
          <div className="flex gap-2 shrink-0">
            <button
              type="button"
              onClick={cancelDelete}
              className="px-3 py-1.5 rounded-md text-sm font-medium text-slate-600 border border-slate-300 bg-white hover:bg-slate-50"
              data-testid="manage-delete-cancel"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void runDelete()}
              className="px-3 py-1.5 rounded-md text-sm font-medium bg-red-600 text-white hover:bg-red-700"
              data-testid="manage-delete-yes"
            >
              Delete
            </button>
          </div>
        </div>
      )}

      {draft && (
        <form
          onSubmit={(event) => void save(event)}
          aria-labelledby="manage-edit-heading"
          noValidate
          className="mt-3 rounded-lg border border-slate-200 bg-slate-50 p-4 space-y-3"
          data-testid="manage-edit-form"
        >
          <h2 id="manage-edit-heading" className="text-sm font-semibold text-slate-800">
            Edit details
          </h2>
          <p className="text-xs text-slate-600">{EDIT_NOTE}</p>
          <div>
            <label htmlFor="manage-edit-name" className="block text-sm font-medium text-slate-700">
              Name
            </label>
            <input
              ref={nameInput}
              id="manage-edit-name"
              type="text"
              value={draft.name}
              maxLength={NAME_MAX}
              aria-invalid={nameError || undefined}
              aria-describedby={nameError ? nameErrorId : undefined}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"
              data-testid="manage-edit-name"
            />
            {nameError && (
              <p id={nameErrorId} className="mt-1 text-xs text-red-700" data-testid="manage-edit-name-error">
                {NAME_REQUIRED}
              </p>
            )}
          </div>
          <div>
            <label htmlFor="manage-edit-description" className="block text-sm font-medium text-slate-700">
              Description
            </label>
            <textarea
              id="manage-edit-description"
              value={draft.description}
              maxLength={TEXT_MAX}
              rows={3}
              onChange={(event) => setDraft({ ...draft, description: event.target.value })}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"
              data-testid="manage-edit-description"
            />
          </div>
          <div>
            <label htmlFor="manage-edit-hypothesis" className="block text-sm font-medium text-slate-700">
              Hypothesis
            </label>
            <textarea
              id="manage-edit-hypothesis"
              value={draft.hypothesis}
              maxLength={TEXT_MAX}
              rows={3}
              onChange={(event) => setDraft({ ...draft, hypothesis: event.target.value })}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"
              data-testid="manage-edit-hypothesis"
            />
          </div>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={busy}
              className="px-3 py-1.5 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
              data-testid="manage-edit-save"
            >
              {pending === 'edit' ? 'Saving…' : 'Save changes'}
            </button>
            <button
              type="button"
              onClick={closeEdit}
              disabled={busy}
              className="px-3 py-1.5 rounded-md text-sm font-medium text-slate-600 border border-slate-300 bg-white hover:bg-slate-50"
              data-testid="manage-edit-cancel"
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {error && (
        <div
          role="alert"
          className="mt-3 rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
          data-testid="manage-error"
        >
          {error}
        </div>
      )}
    </div>
  );
}
