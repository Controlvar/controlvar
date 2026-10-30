import React from 'react';
import { docsUrl } from '@/services/docs';
import { ExperimentType, EXPERIMENT_TYPE_LABELS } from '@/types/experiments';
import { ExperimentFormAction, FORM_EXPERIMENT_TYPES } from './formState';
import { inputClass } from './fieldStyles';

const EXPERIMENT_TYPES: Array<{ value: ExperimentType; label: string }> = FORM_EXPERIMENT_TYPES.map(
  (value) => ({ value, label: EXPERIMENT_TYPE_LABELS[value] }),
);

interface ExperimentTypeFieldProps {
  value: ExperimentType;
  dispatch: React.Dispatch<ExperimentFormAction>;
}

/** The experiment-type select and the note on the types this form cannot set up. */
export function ExperimentTypeField({ value, dispatch }: ExperimentTypeFieldProps) {
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
      <p className="text-xs text-slate-500 mt-1" data-testid="experiment-type-note">
        <a href={docsUrl('api/split-url')} className="text-blue-600 hover:text-blue-800 hover:underline">
          Split URL
        </a>{' '}
        experiments need a URL configuration and{' '}
        <a
          href={docsUrl('api/multi-armed-bandit')}
          className="text-blue-600 hover:text-blue-800 hover:underline"
        >
          bandit
        </a>{' '}
        experiments need an optimization algorithm — neither can be set up from this form
        yet, so create them through the API or an SDK.
      </p>
    </div>
  );
}
