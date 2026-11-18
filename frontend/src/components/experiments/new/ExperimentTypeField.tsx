import React from 'react';
import { docsUrl } from '@/services/docs';
import { ExperimentType, EXPERIMENT_TYPE_LABELS, OPTIMIZATION_TYPE_LABELS } from '@/types/experiments';
import {
  BANDIT_OPTIMIZATION_TYPES,
  BanditOptimizationType,
  ExperimentFormAction,
  FORM_EXPERIMENT_TYPES,
} from './formState';
import { inputClass } from './fieldStyles';

const EXPERIMENT_TYPES: Array<{ value: ExperimentType; label: string }> = FORM_EXPERIMENT_TYPES.map(
  (value) => ({ value, label: EXPERIMENT_TYPE_LABELS[value] }),
);

// --- a bandit's words (#1956), shared by guided setup and the single-page form ---

/** Guided setup's description of the Bandit card. */
export const BANDIT_DESCRIPTION = 'Send more new users to the variant that is doing better, as results come in.';

export const OPTIMIZATION_TYPE_LABEL = 'Optimization algorithm';

/**
 * What each algorithm does with new users, in words. Checked against
 * `backend/app/services/bandit_service.py` and docs/api/multi-armed-bandit.md:
 * UCB1 gives all the weight to the variants nobody has been assigned to while
 * any exist, and epsilon-greedy's epsilon is 0.1, not a setting.
 */
export const OPTIMIZATION_HELP: Record<BanditOptimizationType, string> = {
  thompson_sampling:
    "Each variant's share of new users is the chance that it is the best, given its conversions so far.",
  ucb1:
    "Each variant's share grows with its conversion rate and with how little it has been tried, so every " +
    'variant keeps some traffic once each has had users; until then, the variants nobody has been assigned ' +
    'to share all new users.',
  epsilon_greedy:
    'The variant with the highest conversion rate gets 90% of new users plus its equal part of the other 10%, ' +
    'which is shared evenly by all variants.',
};

/** Under the algorithm's help, whichever is chosen. */
export const BANDIT_SUCCESS_LINE =
  "The bandit counts a user as a success when they send the primary metric's event (with Lower is better on, " +
  'when they do not), and it moves new users while the experiment is active, every 5 minutes by default.';

/** Under the variants, while Bandit is chosen. */
export const BANDIT_ALLOCATION_LINE =
  'For a bandit, these allocations are the split until its first weight update; after that the bandit sets the split.';

/** On guided setup's sample size estimate, while Bandit is chosen. */
export const BANDIT_ESTIMATE_LINE =
  'A fixed sample size is a guide only for a bandit: it moves traffic between variants as results come in.';

interface OptimizationTypeFieldProps {
  value: BanditOptimizationType;
  dispatch: React.Dispatch<ExperimentFormAction>;
}

/**
 * A bandit's Optimization algorithm: a native select with what the chosen one
 * does, and how the bandit counts a success. Rendered only while Bandit is the
 * type, so its container (`bandit-create`) is there only then.
 */
export function OptimizationTypeField({ value, dispatch }: OptimizationTypeFieldProps) {
  return (
    <div className="mt-4" data-testid="bandit-create">
      <label htmlFor="optimization-type" className="block text-sm font-medium text-slate-700 mb-1">
        {OPTIMIZATION_TYPE_LABEL}
      </label>
      <select
        id="optimization-type"
        name="optimization_type"
        value={value}
        onChange={(e) =>
          dispatch({ type: 'setOptimizationType', optimizationType: e.target.value as BanditOptimizationType })
        }
        aria-describedby="optimization-type-help optimization-type-success"
        className={inputClass}
        data-testid="optimization-type"
      >
        {BANDIT_OPTIMIZATION_TYPES.map((t) => (
          <option key={t} value={t}>
            {OPTIMIZATION_TYPE_LABELS[t]}
          </option>
        ))}
      </select>
      <p id="optimization-type-help" className="text-sm text-slate-600 mt-1" data-testid="optimization-type-help">
        {OPTIMIZATION_HELP[value]}
      </p>
      <p id="optimization-type-success" className="text-sm text-slate-600 mt-1">
        {BANDIT_SUCCESS_LINE}
      </p>
    </div>
  );
}

interface ExperimentTypeFieldProps {
  value: ExperimentType;
  optimizationType: BanditOptimizationType;
  dispatch: React.Dispatch<ExperimentFormAction>;
}

/** The experiment-type select, a bandit's algorithm, and the note on the type this form cannot set up. */
export function ExperimentTypeField({ value, optimizationType, dispatch }: ExperimentTypeFieldProps) {
  return (
    <div>
      <label htmlFor="experiment-type" className="block text-sm font-medium text-slate-700 mb-1">
        Experiment Type
      </label>
      <select
        id="experiment-type"
        name="experiment_type"
        value={value}
        onChange={(e) => dispatch({ type: 'setType', experimentType: e.target.value as ExperimentType })}
        className={inputClass}
        data-testid="experiment-type"
      >
        {EXPERIMENT_TYPES.map((t) => (
          <option key={t.value} value={t.value}>
            {t.label}
          </option>
        ))}
      </select>
      {value === 'bandit' && <OptimizationTypeField value={optimizationType} dispatch={dispatch} />}
      <p className="text-xs text-slate-600 mt-1" data-testid="experiment-type-note">
        <a href={docsUrl('api/split-url')} className="text-blue-600 hover:text-blue-800 hover:underline">
          Split URL
        </a>{' '}
        experiments need a URL configuration, which this form cannot set up yet, so create them through the API.
      </p>
    </div>
  );
}
