import React, { useEffect, useRef, useState } from 'react';
import {
  DraftProblem,
  GROUP_COPY,
  GROUP_DESCRIPTION_MAX,
  GROUP_NAME_MAX,
  GroupDraft,
  draftProblems,
} from '@/utils/exclusionGroups';

/**
 * The exclusion group's fields (#1455), shared by "New group" on the list page
 * and "Edit" on the group page. It checks the draft before anything is sent:
 * a name, one no other loaded group has (exactly, case included, as the API's
 * unique constraint compares), and a share that is a whole number from 0 to
 * 100. The caller sends the request and passes back a failure's fixed copy.
 */
export interface ExclusionGroupFormProps {
  idPrefix: string;
  heading: string;
  submitLabel: string;
  initial: GroupDraft;
  /** The loaded groups, for the name check. */
  groups: readonly { id: string; name: string }[];
  /** The group being renamed, which may keep its own name. */
  selfId?: string;
  busy: boolean;
  /** The fixed copy of the last failed request, if any. */
  error: string | null;
  onSubmit: (draft: GroupDraft) => void;
  onCancel: () => void;
}

const FIELD = 'mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-900';

export function ExclusionGroupForm({
  idPrefix,
  heading,
  submitLabel,
  initial,
  groups,
  selfId,
  busy,
  error,
  onSubmit,
  onCancel,
}: ExclusionGroupFormProps) {
  const [draft, setDraft] = useState<GroupDraft>(initial);
  const [problems, setProblems] = useState<DraftProblem[]>([]);
  const nameInput = useRef<HTMLInputElement>(null);
  const shareInput = useRef<HTMLInputElement>(null);
  const errorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    nameInput.current?.focus();
  }, []);

  useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);

  const nameProblem = problems.find((p) => p === 'name-required' || p === 'name-taken');
  const shareProblem = problems.includes('share-invalid');
  const ids = {
    heading: `${idPrefix}-heading`,
    name: `${idPrefix}-name`,
    nameError: `${idPrefix}-name-error`,
    description: `${idPrefix}-description`,
    share: `${idPrefix}-share`,
    shareHelp: `${idPrefix}-share-help`,
    shareError: `${idPrefix}-share-error`,
  };

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const found = draftProblems(draft, groups, selfId);
    setProblems(found);
    if (found.length > 0) {
      if (found[0] === 'share-invalid') shareInput.current?.focus();
      else nameInput.current?.focus();
      return;
    }
    onSubmit(draft);
  };

  return (
    <form
      onSubmit={submit}
      aria-labelledby={ids.heading}
      noValidate
      className="rounded-lg border border-slate-200 bg-white p-4 space-y-3"
      data-testid="exclusion-group-form"
    >
      <h2 id={ids.heading} className="text-base font-semibold text-slate-900">
        {heading}
      </h2>
      {/* A short summary: the field's own message, tied to it by
          aria-describedby, says what is wrong, so it is not announced twice. */}
      {problems.length > 0 && (
        <p role="alert" className="text-sm text-red-700" data-testid="exclusion-group-form-problem">
          {GROUP_COPY.checkField}
        </p>
      )}
      <div>
        <label htmlFor={ids.name} className="block text-sm font-medium text-slate-700">
          Name
        </label>
        <input
          ref={nameInput}
          id={ids.name}
          type="text"
          value={draft.name}
          maxLength={GROUP_NAME_MAX}
          aria-invalid={nameProblem ? true : undefined}
          aria-describedby={nameProblem ? ids.nameError : undefined}
          onChange={(event) => setDraft({ ...draft, name: event.target.value })}
          className={FIELD}
          data-testid="exclusion-group-name"
        />
        {nameProblem && (
          <p id={ids.nameError} className="mt-1 text-xs text-red-700" data-testid="exclusion-group-name-error">
            {nameProblem === 'name-taken' ? GROUP_COPY.nameTaken : GROUP_COPY.nameRequired}
          </p>
        )}
      </div>
      <div>
        <label htmlFor={ids.description} className="block text-sm font-medium text-slate-700">
          Description (optional)
        </label>
        <textarea
          id={ids.description}
          value={draft.description}
          maxLength={GROUP_DESCRIPTION_MAX}
          rows={2}
          onChange={(event) => setDraft({ ...draft, description: event.target.value })}
          className={FIELD}
          data-testid="exclusion-group-description"
        />
      </div>
      <div>
        <label htmlFor={ids.share} className="block text-sm font-medium text-slate-700">
          Share of traffic (%)
        </label>
        <input
          ref={shareInput}
          id={ids.share}
          type="number"
          inputMode="numeric"
          min={0}
          max={100}
          step={1}
          value={draft.share}
          aria-invalid={shareProblem ? true : undefined}
          aria-describedby={shareProblem ? `${ids.shareError} ${ids.shareHelp}` : ids.shareHelp}
          onChange={(event) => setDraft({ ...draft, share: event.target.value })}
          className={`${FIELD} sm:w-32`}
          data-testid="exclusion-group-share"
        />
        {shareProblem && (
          <p id={ids.shareError} className="mt-1 text-xs text-red-700" data-testid="exclusion-group-share-error">
            {GROUP_COPY.shareInvalid}
          </p>
        )}
        <p id={ids.shareHelp} className="mt-1 text-xs text-slate-600" data-testid="exclusion-group-share-help">
          {GROUP_COPY.shareHelp}
        </p>
      </div>
      {error && (
        <div
          ref={errorRef}
          tabIndex={-1}
          role="alert"
          className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-800"
          data-testid="exclusion-group-form-error"
        >
          {error}
        </div>
      )}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={busy}
          className="px-3 py-1.5 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
          data-testid="exclusion-group-submit"
        >
          {busy ? 'Saving…' : submitLabel}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className="px-3 py-1.5 rounded-md text-sm font-medium text-slate-700 border border-slate-300 bg-white hover:bg-slate-50"
          data-testid="exclusion-group-cancel"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
