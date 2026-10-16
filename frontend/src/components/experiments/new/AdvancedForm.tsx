import React from 'react';
import Link from 'next/link';
import { TargetingRuleBuilder } from '@/components/targeting';
import { ExperimentFormAction, ExperimentFormState } from './formState';
import { BasicInfoFields } from './BasicInfoFields';
import { ExperimentTypeField } from './ExperimentTypeField';
import { VariantsEditor } from './VariantsEditor';
import { MetricsEditor } from './MetricsEditor';
import { CreateError, footMessage } from './createErrors';
import { TargetingProblems } from './TargetingProblems';
import { AnalysisSettingsFields } from './AnalysisSettingsFields';
import { correctionName, formatConfidence } from '@/components/results/shared/analysisSettings';
import { formatEffect, plannedEffectFraction, plannedEffectProblem } from './estimate';

/**
 * The minimum detectable effect typed in guided setup, as this form shows it:
 * read-only, because only guided setup asks for it. Nothing when none was typed.
 */
function PlannedEffectLine({ plannedEffectPct }: { plannedEffectPct: string }) {
  if (plannedEffectPct.trim() === '') return null;
  const fraction = plannedEffectFraction(plannedEffectPct);
  return (
    <p className="text-sm text-slate-700" data-testid="advanced-planned-effect">
      {fraction !== undefined
        ? `Minimum detectable effect: ${formatEffect(fraction)} (from guided setup; saved)`
        : `The minimum detectable effect typed in guided setup is not saved: ${plannedEffectProblem(
            plannedEffectPct,
          )} Change it there, or leave it blank.`}
    </p>
  );
}

interface AdvancedFormProps {
  state: ExperimentFormState;
  dispatch: React.Dispatch<ExperimentFormAction>;
  error: CreateError | null;
  isSubmitting: boolean;
  onSubmit: (e: React.FormEvent) => void;
  /** Changed each time a create was refused before sending; see `VariantsEditor`. */
  revealProblems?: number;
}

/** Every field of a new experiment on one page, submitted with one button. */
export function AdvancedForm({
  state,
  dispatch,
  error,
  isSubmitting,
  onSubmit,
  revealProblems = 0,
}: AdvancedFormProps) {
  // The key field is on this page: take the user straight to it.
  // A targeting problem is shown at the rules; anything else the error says goes here.
  const foot = footMessage(error);

  const focusKey = () => {
    const key = document.getElementById('experiment-key');
    if (key instanceof HTMLInputElement) {
      key.focus();
      key.select();
    }
  };

  return (
    <form onSubmit={onSubmit} className="space-y-6" data-testid="new-experiment-form" noValidate>
      {/* Basic info */}
      <section className="bg-white rounded-lg border border-slate-200 p-6 space-y-4">
        <h2 className="text-base font-semibold text-slate-800">Basic Information</h2>
        <BasicInfoFields state={state} dispatch={dispatch} />
        <ExperimentTypeField value={state.type} dispatch={dispatch} />
      </section>

      {/* Variants */}
      <section className="bg-white rounded-lg border border-slate-200 p-6 space-y-4">
        <VariantsEditor variants={state.variants} dispatch={dispatch} revealProblems={revealProblems} />
      </section>

      {/* Metrics */}
      <section className="bg-white rounded-lg border border-slate-200 p-6 space-y-4" data-testid="metrics-section">
        <MetricsEditor metrics={state.metrics} dispatch={dispatch} />
        <PlannedEffectLine plannedEffectPct={state.plannedEffectPct} />
      </section>

      {/* Analysis settings: collapsed, with the current values in the summary,
          so nothing hidden changes how the results are judged. Not called
          "Advanced": this whole page is the advanced form. */}
      <details className="bg-white rounded-lg border border-slate-200 p-6" data-testid="analysis-settings-details">
        <summary className="cursor-pointer text-base font-semibold text-slate-800" data-testid="analysis-settings-summary">
          Analysis settings: {formatConfidence(state.confidenceLevel)} confidence,{' '}
          {state.correctionMethod === 'none'
            ? 'no correction'
            : `${correctionName(state.correctionMethod)} correction`}
          {state.bayesianEnabled && ', Bayesian analysis on'}
        </summary>
        <div className="mt-4">
          <AnalysisSettingsFields
            confidenceLevel={state.confidenceLevel}
            correctionMethod={state.correctionMethod}
            dispatch={dispatch}
            variantCount={state.variants.length}
            bayesianEnabled={state.bayesianEnabled}
          />
        </div>
      </details>

      {/* Targeting Rules */}
      <section className="bg-white rounded-lg border border-slate-200 p-6 space-y-4" data-testid="targeting-section">
        <TargetingRuleBuilder value={state.rules} onChange={(rules) => dispatch({ type: 'setRules', rules })} />
        <TargetingProblems error={error} />
      </section>

      {/* Error message (a targeting problem is shown at the rules; the rest here) */}
      {error && foot && (
        <div
          role="alert"
          className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
          data-testid="form-error"
        >
          {foot}
          {error.editDetails && (
            <>
              {' '}
              <button
                type="button"
                onClick={focusKey}
                className="font-medium underline hover:text-red-900 focus:outline-none focus:ring-2 focus:ring-blue-600 rounded"
                data-testid="form-error-edit-details"
              >
                Edit details
              </button>
            </>
          )}
        </div>
      )}

      {/* Submit */}
      <div className="flex gap-3 justify-end">
        <Link
          href="/experiments"
          className="px-4 py-2 rounded-lg border border-slate-300 text-sm font-medium text-slate-600 hover:bg-slate-50 transition-colors"
        >
          Cancel
        </Link>
        <button
          type="submit"
          disabled={isSubmitting}
          className="px-6 py-2 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          data-testid="submit-experiment"
        >
          {isSubmitting ? 'Creating...' : 'Create Experiment'}
        </button>
      </div>
    </form>
  );
}
