import React, { useEffect, useRef, useState } from 'react';
import {
  DESCRIPTION_MAX,
  HOLDOUT_COPY,
  HoldoutDraft,
  HoldoutProblem,
  NAME_MAX,
  SIZE_MAX,
  SIZE_MIN,
  holdoutDraftProblems,
} from '@/utils/holdouts';

/**
 * A global holdout's fields (#1455): "New holdout" and "Edit". The name is
 * checked against the loaded holdouts, exactly and case-sensitively, as the
 * API's unique constraint compares. The size is a whole percent from 1 to 20
 * while the holdout has never been active, and plain text after that (the API
 * refuses a change with 422). "Activate now" is offered on create only.
 */
export interface HoldoutFormProps {
  idPrefix: string;
  heading: string;
  submitLabel: string;
  /** Where the fields start; while `sizeLocked`, also the size shown. */
  initial: HoldoutDraft;
  holdouts: readonly { id: string; name: string }[];
  selfId?: string;
  /** The holdout has been active: its size is shown, not edited. */
  sizeLocked?: boolean;
  /** Offer "Activate now" (create only). */
  offerActivate?: boolean;
  busy: boolean;
  error: string | null;
  onSubmit: (draft: HoldoutDraft, activateNow: boolean) => void;
  onCancel: () => void;
}

const FIELD = 'mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-900';

export function HoldoutForm({
  idPrefix,
  heading,
  submitLabel,
  initial,
  holdouts,
  selfId,
  sizeLocked = false,
  offerActivate = false,
  busy,
  error,
  onSubmit,
  onCancel,
}: HoldoutFormProps) {
  const [draft, setDraft] = useState<HoldoutDraft>(initial);
  const [activateNow, setActivateNow] = useState(false);
  const [problems, setProblems] = useState<HoldoutProblem[]>([]);
  const nameInput = useRef<HTMLInputElement>(null);
  const sizeInput = useRef<HTMLInputElement>(null);
  const errorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    nameInput.current?.focus();
  }, []);

  useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);

  const nameProblem = problems.find((p) => p === 'name-required' || p === 'name-taken');
  const sizeProblem = problems.includes('size-invalid');
  const ids = {
    heading: `${idPrefix}-heading`,
    name: `${idPrefix}-name`,
    nameError: `${idPrefix}-name-error`,
    description: `${idPrefix}-description`,
    size: `${idPrefix}-size`,
    sizeHelp: `${idPrefix}-size-help`,
    sizeError: `${idPrefix}-size-error`,
    activate: `${idPrefix}-activate`,
  };
  const problemCopy = (p: HoldoutProblem) =>
    p === 'size-invalid' ? HOLDOUT_COPY.sizeInvalid : p === 'name-taken' ? HOLDOUT_COPY.nameTaken : HOLDOUT_COPY.nameRequired;

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const found = holdoutDraftProblems(draft, holdouts, selfId, sizeLocked);
    setProblems(found);
    if (found.length > 0) {
      if (found[0] === 'size-invalid') sizeInput.current?.focus();
      else nameInput.current?.focus();
      return;
    }
    onSubmit(draft, offerActivate && activateNow);
  };

  return (
    <form
      onSubmit={submit}
      aria-labelledby={ids.heading}
      noValidate
      className="rounded-lg border border-slate-200 bg-white p-4 space-y-3"
      data-testid="holdout-form"
    >
      <h2 id={ids.heading} className="text-base font-semibold text-slate-900">
        {heading}
      </h2>
      {/* A short summary: the field's own message, tied to it by
          aria-describedby, says what is wrong, so it is not announced twice. */}
      {problems.length > 0 && (
        <p role="alert" className="text-sm text-red-700" data-testid="holdout-form-problem">
          {HOLDOUT_COPY.checkField}
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
          maxLength={NAME_MAX}
          aria-invalid={nameProblem ? true : undefined}
          aria-describedby={nameProblem ? ids.nameError : undefined}
          onChange={(event) => setDraft({ ...draft, name: event.target.value })}
          className={FIELD}
          data-testid="holdout-name"
        />
        {nameProblem && (
          <p id={ids.nameError} className="mt-1 text-xs text-red-700" data-testid="holdout-name-error">
            {problemCopy(nameProblem)}
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
          maxLength={DESCRIPTION_MAX}
          rows={2}
          onChange={(event) => setDraft({ ...draft, description: event.target.value })}
          className={FIELD}
          data-testid="holdout-description"
        />
      </div>
      {sizeLocked ? (
        <div data-testid="holdout-size-locked">
          <p className="text-sm font-medium text-slate-700">Size (% of new users)</p>
          {/* The stored size, not the draft's: a holdout that became active
              while this form was open keeps the size it had, whatever was typed. */}
          <p className="mt-1 text-sm text-slate-900">{initial.size}%</p>
          <p className="mt-1 text-xs text-slate-600">{HOLDOUT_COPY.sizeFixed}</p>
        </div>
      ) : (
        <div>
          <label htmlFor={ids.size} className="block text-sm font-medium text-slate-700">
            Size (% of new users)
          </label>
          <input
            ref={sizeInput}
            id={ids.size}
            type="number"
            inputMode="numeric"
            min={SIZE_MIN}
            max={SIZE_MAX}
            step={1}
            value={draft.size}
            aria-invalid={sizeProblem ? true : undefined}
            aria-describedby={sizeProblem ? `${ids.sizeError} ${ids.sizeHelp}` : ids.sizeHelp}
            onChange={(event) => setDraft({ ...draft, size: event.target.value })}
            className={`${FIELD} sm:w-32`}
            data-testid="holdout-size"
          />
          {sizeProblem && (
            <p id={ids.sizeError} className="mt-1 text-xs text-red-700" data-testid="holdout-size-error">
              {HOLDOUT_COPY.sizeInvalid}
            </p>
          )}
          <p id={ids.sizeHelp} className="mt-1 text-xs text-slate-600" data-testid="holdout-size-help">
            {HOLDOUT_COPY.sizeHelp}
          </p>
        </div>
      )}
      {offerActivate && (
        <div className="flex items-center gap-2">
          <input
            id={ids.activate}
            type="checkbox"
            checked={activateNow}
            onChange={(event) => setActivateNow(event.target.checked)}
            className="h-4 w-4"
            data-testid="holdout-activate-now"
          />
          <label htmlFor={ids.activate} className="text-sm text-slate-700">
            Activate now
          </label>
        </div>
      )}
      {error && (
        <div
          ref={errorRef}
          tabIndex={-1}
          role="alert"
          className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-800"
          data-testid="holdout-form-error"
        >
          {error}
        </div>
      )}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={busy}
          className="px-3 py-1.5 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
          data-testid="holdout-submit"
        >
          {busy ? 'Saving…' : submitLabel}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className="px-3 py-1.5 rounded-md text-sm font-medium text-slate-700 border border-slate-300 bg-white hover:bg-slate-50"
          data-testid="holdout-cancel"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
