import React, { useEffect, useRef, useState } from 'react';
import { BanditService } from '@/services/bandit';
import { isApiError } from '@/services/api';
import { BanditStatus } from '@/types/bandit';
import { Experiment, ExperimentStatus, ExperimentVariant } from '@/types/experiments';
import { demoRefusalMessage } from '@/utils/demoMode';

/**
 * "Set weights by hand" (#1957): an ADMIN or a superuser gives each variant a
 * whole percentage, and the form sends `PUT /api/v1/bandit/{id}/weights` with
 * each percentage divided by 100, keyed by the experiment's own variant ids.
 * `BanditWeightsSection` decides who sees it and on which statuses.
 *
 * Fixed copy. Of what the server sends in an error only the read-only demo's
 * own sentence is shown (`demoRefusalMessage`); every other refusal or failure
 * reads as the sentence for its status.
 */

/** The statuses whose weights can still matter: the bandit can still route someone new. */
export const OVERRIDE_STATUSES: readonly ExperimentStatus[] = ['draft', 'active', 'paused'];

export const OVERRIDE_LABEL = 'Set weights by hand';

export const OVERRIDE_INTRO = 'Give each variant a whole percentage. They must add up to 100%.';

export const OVERRIDE_STATUS_COPY: Record<'draft' | 'active' | 'paused', string> = {
  active:
    'The bandit replaces these weights at its next update, within minutes (every 5 minutes by default). ' +
    'Use this to correct the split for a short time, not to set it.',
  draft:
    'The bandit does not update a draft. From the start, new users follow these weights until its first ' +
    'update replaces them, within minutes.',
  paused:
    'The bandit does not update a paused experiment. When it resumes, new users follow these weights until ' +
    'its next update replaces them, within minutes.',
};

export const OVERRIDE_ALWAYS =
  'People already in the experiment keep their variant. A variant at 0% gets no new users. After you save, ' +
  "Pulls and Successes read 0 until the bandit's next update counts them again; no assignment or event is lost.";

export const OVERRIDE_INVALID = 'Enter a whole number from 0 to 100.';

export const OVERRIDE_SAVED = 'Weights saved.';

/** Shown in place of the control to a signed-in user who may not set the weights. */
export const OVERRIDE_ROLE_NOTE = "Setting a bandit's weights by hand needs the ADMIN role.";

export const OVERRIDE_ERROR_COPY = {
  403: OVERRIDE_ROLE_NOTE,
  404: 'This experiment is no longer a bandit, or it was deleted. Reload the page.',
  422: 'The weights were refused: each must be 0 or more, and together they must add up to 100%.',
  server: 'The weights could not be saved. Try again; if it keeps failing, check the API logs.',
  fallback: 'The weights could not be saved.',
} as const;

/** The sentence for a failed save. */
export function overrideErrorCopy(err: unknown): string {
  if (!isApiError(err)) return OVERRIDE_ERROR_COPY.fallback;
  // No answer arrived (unreachable or timed out): the dashboard's own sentence.
  if (err.status === 0) return err.message;
  const demo = demoRefusalMessage(err);
  if (demo) return demo;
  if (err.status === 403) return OVERRIDE_ERROR_COPY[403];
  if (err.status === 404) return OVERRIDE_ERROR_COPY[404];
  if (err.status === 422) return OVERRIDE_ERROR_COPY[422];
  if (err.status >= 500) return OVERRIDE_ERROR_COPY.server;
  return OVERRIDE_ERROR_COPY.fallback;
}

/**
 * Whole numbers that add up to `target`, each as close to its share as
 * possible: every share rounded down, then the units still missing go to the
 * largest remainders (ties to the earlier share). Plain rounding can lose or
 * add one (1/3 each rounds to 33 + 33 + 33 = 99).
 */
export function largestRemainder(shares: number[], target = 100): number[] {
  const out = shares.map((s) => Math.floor(s));
  let missing = target - out.reduce((a, b) => a + b, 0);
  const order = shares
    .map((s, i) => ({ i, rest: s - Math.floor(s) }))
    .sort((a, b) => b.rest - a.rest || a.i - b.i);
  for (let k = 0; missing > 0 && order.length > 0; k = (k + 1) % order.length, missing -= 1) {
    out[order[k].i] += 1;
  }
  return out;
}

/**
 * What the form opens with, one value per variant of the experiment, in its
 * order. Before the first update (`last_updated` null) the server fills in an
 * even split it is not using, and new users follow each variant's traffic
 * allocation, so the form opens with the allocations. Afterwards it opens with
 * the current weights as whole percentages that add up to 100, so Save is not
 * stuck at 99 or 101.
 */
export function prefillPercents(variants: ExperimentVariant[], status: BanditStatus): string[] {
  const allocations = variants.map((v) => String(v.traffic_allocation));
  if (status.last_updated === null) return allocations;
  const byId = new Map(status.current_weights.map((w) => [w.variant_id, w.current_weight]));
  const weights = variants.map((v) => Math.max(0, byId.get(v.id) ?? 0));
  const total = weights.reduce((a, b) => a + b, 0);
  if (!(total > 0)) return allocations;
  return largestRemainder(weights.map((w) => (w / total) * 100)).map(String);
}

/** A whole number from 0 to 100, or null for anything else (blank, -1, 12.5, 101). */
export function parsePercent(value: string): number | null {
  const text = value.trim();
  if (!/^\d{1,3}$/.test(text)) return null;
  const n = Number(text);
  return n <= 100 ? n : null;
}

/** The request body's map: each variant's id to its percentage divided by 100. */
export function overrideWeights(variants: ExperimentVariant[], percents: number[]): Record<string, number> {
  return Object.fromEntries(variants.map((v, i) => [v.id, percents[i] / 100]));
}

export interface BanditWeightsOverrideProps {
  experiment: Experiment;
  /** The panel's loaded status: what the form opens with. */
  status: BanditStatus;
  /** The element id the panel's button names in `aria-controls`. */
  formId: string;
  onSaved: (status: BanditStatus) => void;
  onCancel: () => void;
  /** Told whether a save is in flight, so the panel can hold Refresh until it ends. */
  onSavingChange?: (saving: boolean) => void;
}

export function BanditWeightsOverride({
  experiment,
  status,
  formId,
  onSaved,
  onCancel,
  onSavingChange,
}: BanditWeightsOverrideProps) {
  const variants = experiment.variants;
  const [values, setValues] = useState<string[]>(() => prefillPercents(variants, status));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const firstInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    firstInput.current?.focus();
  }, []);

  useEffect(() => {
    onSavingChange?.(saving);
  }, [saving, onSavingChange]);

  // The form leaving (saved, or closed) ends the save as far as the panel is concerned.
  useEffect(() => () => onSavingChange?.(false), [onSavingChange]);

  const parsed = values.map(parsePercent);
  const total = parsed.reduce<number>((sum, n) => sum + (n ?? 0), 0);
  const allValid = parsed.every((n) => n !== null);
  const canSave = allValid && total === 100 && variants.length > 0;
  const statusCopy = OVERRIDE_STATUS_COPY[experiment.status as keyof typeof OVERRIDE_STATUS_COPY];
  const headingId = `${formId}-heading`;

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSave || saving) return;
    setSaving(true);
    setError(null);
    try {
      const answer = await BanditService.override(experiment.id, overrideWeights(variants, parsed as number[]));
      onSaved(answer);
    } catch (err) {
      setError(overrideErrorCopy(err));
      setSaving(false);
    }
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'Escape' && !saving) {
      event.preventDefault();
      onCancel();
    }
  };

  return (
    <form
      id={formId}
      noValidate
      onSubmit={(event) => void save(event)}
      onKeyDown={onKeyDown}
      aria-labelledby={headingId}
      className="mt-3 mb-4 rounded-lg border border-slate-200 p-4"
      data-testid="bandit-weights-override"
    >
      <h3 id={headingId} className="text-sm font-semibold text-slate-800 mb-1">
        {OVERRIDE_LABEL}
      </h3>
      <p className="text-sm text-slate-700">{OVERRIDE_INTRO}</p>
      {statusCopy && (
        <p className="mt-2 text-sm text-slate-700" data-testid="bandit-override-status-copy">
          {statusCopy}
        </p>
      )}
      <p className="mt-2 text-sm text-slate-700" data-testid="bandit-override-always">
        {OVERRIDE_ALWAYS}
      </p>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        {variants.map((variant, i) => {
          const inputId = `${formId}-input-${i}`;
          const errorId = `${formId}-invalid-${i}`;
          const invalid = parsed[i] === null;
          return (
            <div key={variant.id}>
              <label htmlFor={inputId} className="block text-sm font-medium text-slate-700 mb-1">
                {`${variant.name} (%)`}
              </label>
              <input
                ref={i === 0 ? firstInput : undefined}
                id={inputId}
                type="number"
                inputMode="numeric"
                min={0}
                max={100}
                step={1}
                value={values[i]}
                onChange={(event) => {
                  const next = [...values];
                  next[i] = event.target.value;
                  setValues(next);
                }}
                disabled={saving}
                aria-invalid={invalid ? true : undefined}
                aria-describedby={invalid ? errorId : undefined}
                className="w-28 px-2 py-1.5 rounded-md border border-slate-300 text-sm tabular-nums"
                data-testid="bandit-override-input"
                data-variant-id={variant.id}
              />
              {invalid && (
                <p id={errorId} className="mt-1 text-xs text-red-700" data-testid="bandit-override-invalid">
                  {OVERRIDE_INVALID}
                </p>
              )}
            </div>
          );
        })}
      </div>

      <p role="status" className="mt-3 text-sm text-slate-700" data-testid="bandit-override-total">
        {total === 100 ? `Total: ${total}%.` : `Total: ${total}%. The weights must add up to 100%.`}
      </p>

      {error && (
        <div
          role="alert"
          className="mt-3 rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
          data-testid="bandit-override-error"
        >
          {error}
        </div>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="submit"
          disabled={!canSave || saving}
          className="px-3 py-1.5 rounded-md text-sm font-medium bg-slate-900 text-white hover:bg-slate-800 disabled:opacity-50"
          data-testid="bandit-override-save"
        >
          {saving ? 'Saving…' : 'Save weights'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={saving}
          className="px-3 py-1.5 rounded-md text-sm font-medium bg-white text-slate-700 border border-slate-300 hover:bg-slate-50 disabled:opacity-50"
          data-testid="bandit-override-cancel"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
