import React from 'react';
import { ExperimentFormAction, ExperimentFormState } from './formState';
import { inputClass } from './fieldStyles';

interface BasicInfoFieldsProps {
  state: Pick<ExperimentFormState, 'name' | 'key' | 'description' | 'hypothesis'>;
  dispatch: React.Dispatch<ExperimentFormAction>;
}

/** Name, key, description and hypothesis. The caller supplies the surrounding section. */
export function BasicInfoFields({ state, dispatch }: BasicInfoFieldsProps) {
  return (
    <>
      <div>
        <label htmlFor="experiment-name" className="block text-sm font-medium text-slate-700 mb-1">
          Name <span className="text-red-500">*</span>
        </label>
        <input
          id="experiment-name"
          name="name"
          type="text"
          required
          value={state.name}
          onChange={(e) => dispatch({ type: 'setName', name: e.target.value })}
          placeholder="My Experiment"
          className={inputClass}
          data-testid="experiment-name"
        />
      </div>

      <div>
        <label htmlFor="experiment-key" className="block text-sm font-medium text-slate-700 mb-1">
          Key
        </label>
        <input
          id="experiment-key"
          name="key"
          type="text"
          value={state.key}
          onChange={(e) => dispatch({ type: 'setKey', key: e.target.value })}
          placeholder="my_experiment"
          className={`${inputClass} font-mono`}
          data-testid="experiment-key"
        />
        <p className="text-xs text-slate-500 mt-1">
          Auto-generated from the name. SDKs use it in <code className="font-mono">experiment_key</code>.
        </p>
      </div>

      <div>
        <label htmlFor="experiment-description" className="block text-sm font-medium text-slate-700 mb-1">
          Description
        </label>
        <textarea
          id="experiment-description"
          name="description"
          value={state.description}
          onChange={(e) => dispatch({ type: 'setDescription', description: e.target.value })}
          rows={2}
          placeholder="What is this experiment testing?"
          className={`${inputClass} resize-none`}
          data-testid="experiment-description"
        />
      </div>

      <div>
        <label htmlFor="experiment-hypothesis" className="block text-sm font-medium text-slate-700 mb-1">
          Hypothesis
        </label>
        <textarea
          id="experiment-hypothesis"
          name="hypothesis"
          value={state.hypothesis}
          onChange={(e) => dispatch({ type: 'setHypothesis', hypothesis: e.target.value })}
          rows={2}
          placeholder="We believe that... will result in..."
          className={`${inputClass} resize-none`}
          data-testid="experiment-hypothesis"
        />
      </div>
    </>
  );
}
