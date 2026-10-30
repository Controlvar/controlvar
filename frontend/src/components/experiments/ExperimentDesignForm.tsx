import React, { useEffect, useReducer, useRef, useState } from 'react';
import { ExperimentsService } from '@/services/experiments';
import { Experiment } from '@/types/experiments';
import { MetricsEditor } from './new/MetricsEditor';
import { VariantsEditor } from './new/VariantsEditor';
import {
  buildDesignPayload,
  checkDesign,
  DesignField,
  DesignProblem,
  experimentFormReducer,
  ExperimentFormAction,
  formStateFromExperiment,
} from './new/formState';

export const DESIGN_HEADING = 'Edit variants and metrics';

export const DESIGN_NOTE = 'Saving replaces every variant and metric of this draft with what is below.';

/** Shown only when the experiment has sequential testing on. */
export const DESIGN_SEQUENTIAL_NOTE =
  'Sequential testing compares the control with the first treatment in this list.';

const ERROR_ID = 'design-error';

/** Where a refusal's field is, by the editors' own test ids. */
function fieldSelector(field: DesignField): string | null {
  if (field.field === 'configuration') return null; // VariantsEditor opens and focuses it
  if (field.field === 'add') return field.list === 'variants' ? 'add-variant' : 'add-metric';
  const ids: Record<string, string> = {
    name: field.list === 'variants' ? 'variant-name' : 'metric-name',
    traffic_allocation: 'variant-allocation',
    is_control: 'variant-control',
    event_name: 'metric-event',
    is_primary: 'metric-primary',
  };
  return `${ids[field.field]}-${field.index}`;
}

interface ExperimentDesignFormProps {
  /** A DRAFT experiment; the caller offers the form for nothing else. */
  experiment: Experiment;
  /** Called with the API's answer once the save is accepted. */
  onSaved: (updated: Experiment) => void;
  /** Cancel, and a save with nothing changed: no request is sent. */
  onCancel: () => void;
  /** The fixed sentence for a refused save, chosen by its status only. */
  describeError: (err: unknown) => string;
}

/**
 * A draft's variants and metrics, edited with guided setup's own editors and
 * saved with one `PUT /api/v1/experiments/{id}` of exactly `{variants,
 * metrics}`, both complete (`buildDesignPayload`).
 *
 * Before sending, `checkDesign`: at least two variants, then guided setup's
 * own checks, exactly one control and exactly one primary. A refusal sends
 * nothing, and focus goes to the field it is about.
 */
export function ExperimentDesignForm({ experiment, onSaved, onCancel, describeError }: ExperimentDesignFormProps) {
  const [state, dispatch] = useReducer(experimentFormReducer, experiment, formStateFromExperiment);
  // What an unchanged form would send; a save equal to it sends nothing.
  const [seeded] = useState(() => JSON.stringify(buildDesignPayload(formStateFromExperiment(experiment))));
  const [problem, setProblem] = useState<DesignProblem | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [revealProblems, setRevealProblems] = useState(0);
  const [focusField, setFocusField] = useState<DesignField | null>(null);
  const [focusSave, setFocusSave] = useState(false);

  const formRef = useRef<HTMLFormElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const saveRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  useEffect(() => {
    if (focusField === null) return;
    const id = fieldSelector(focusField);
    if (id) formRef.current?.querySelector<HTMLElement>(`[data-testid="${id}"]`)?.focus();
    setFocusField(null);
  }, [focusField]);

  useEffect(() => {
    if (!focusSave || saving) return;
    saveRef.current?.focus();
    setFocusSave(false);
  }, [focusSave, saving]);

  // Any change answers the refusal: the field it marked may be fixed now.
  const change: React.Dispatch<ExperimentFormAction> = (action) => {
    dispatch(action);
    setProblem(null);
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving) return;
    setServerError(null);
    const found = checkDesign(state);
    if (found) {
      setProblem(found);
      if (found.field.field === 'configuration') setRevealProblems((n) => n + 1);
      else setFocusField(found.field);
      return;
    }
    setProblem(null);
    const body = buildDesignPayload(state);
    if (JSON.stringify(body) === seeded) {
      onCancel();
      return;
    }
    setSaving(true);
    try {
      const updated = await ExperimentsService.updateDesign(experiment.id, body);
      onSaved(updated);
    } catch (err) {
      setServerError(describeError(err));
      setSaving(false);
      setFocusSave(true);
    }
  };

  const message = problem?.message ?? serverError;
  const invalid = problem?.field;
  const variantInvalid =
    invalid && invalid.list === 'variants' && invalid.field !== 'add' && invalid.field !== 'configuration'
      ? { index: invalid.index, field: invalid.field }
      : null;
  const metricInvalid =
    invalid && invalid.list === 'metrics' && invalid.field !== 'add'
      ? { index: invalid.index, field: invalid.field }
      : null;

  return (
    <form
      ref={formRef}
      onSubmit={(event) => void save(event)}
      aria-labelledby="design-heading"
      noValidate
      className="mt-3 rounded-lg border border-slate-200 bg-white p-4 space-y-4"
      data-testid="design-form"
    >
      <div>
        <h2
          id="design-heading"
          ref={headingRef}
          tabIndex={-1}
          className="text-sm font-semibold text-slate-800 focus:outline-none focus:ring-2 focus:ring-blue-600 rounded"
        >
          {DESIGN_HEADING}
        </h2>
        <p className="mt-1 text-xs text-slate-600" data-testid="design-note">
          {DESIGN_NOTE}
        </p>
        {experiment.sequential_testing_enabled === true && (
          <p className="mt-1 text-xs text-slate-600" data-testid="design-sequential-note">
            {DESIGN_SEQUENTIAL_NOTE}
          </p>
        )}
      </div>

      <fieldset className="min-w-0 space-y-4" data-testid="design-variants">
        <legend className="sr-only">Variants</legend>
        <VariantsEditor
          variants={state.variants}
          dispatch={change}
          revealProblems={revealProblems}
          invalid={variantInvalid}
          invalidDescribedBy={ERROR_ID}
        />
      </fieldset>

      <fieldset className="min-w-0 space-y-4" data-testid="design-metrics">
        <legend className="sr-only">Metrics</legend>
        <MetricsEditor
          metrics={state.metrics}
          dispatch={change}
          invalid={metricInvalid}
          invalidDescribedBy={ERROR_ID}
        />
      </fieldset>

      {message && (
        <div
          id={ERROR_ID}
          role="alert"
          className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
          data-testid="design-error"
        >
          {message}
        </div>
      )}

      <div className="flex gap-2">
        <button
          ref={saveRef}
          type="submit"
          disabled={saving}
          className="px-3 py-1.5 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
          data-testid="design-save"
        >
          {saving ? 'Saving…' : 'Save variants and metrics'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={saving}
          className="px-3 py-1.5 rounded-md text-sm font-medium text-slate-600 border border-slate-300 bg-white hover:bg-slate-50"
          data-testid="design-cancel"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
