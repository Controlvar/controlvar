import React, { useEffect, useRef, useState } from 'react';
import { CorrectionMethod } from '@/types/results';
import {
  CONFIDENCE_OPTIONS,
  CORRECTION_OPTIONS,
  DEFAULT_CONFIDENCE_LEVEL,
  formatConfidence,
} from '@/components/results/shared/analysisSettings';
import { docsUrl } from '@/services/docs';
import {
  BAYESIAN_FIELD_LABELS,
  BAYESIAN_NUMBER_FIELDS,
  BayesianConfigFormState,
  BayesianNumberField,
  bayesianConfigProblems,
  buildBayesianConfig,
  ExperimentFormAction,
  MUST_BE_BETWEEN_0_AND_1,
  MUST_BE_POSITIVE,
} from './formState';
import { wizardInputClass } from './fieldStyles';

export const ANALYSIS_LEGEND = 'How results will be judged';
export const ANALYSIS_SUBLINE =
  'Saved with the experiment. The confidence level and the correction cannot be changed after it starts.';

export const CONFIDENCE_HELP =
  'How sure the results must be before a difference is called significant. At 95%, a variant is ' +
  'significant when its p-value is below 0.05.';

export const CORRECTION_HELP =
  'This only matters with three or more variants. Each extra variant is another chance for one to ' +
  'look like a winner by luck. Benjamini-Hochberg keeps the share of false winners among the ' +
  'variants called significant at about 5% (at 95% confidence) and finds real winners more often. ' +
  'Bonferroni keeps the chance of even one false winner at 5%: it is stricter and needs more users. ' +
  'With None, each variant is judged on its own, so with several variants a false winner is more ' +
  'likely. Either correction applies to the variants of each metric, not across metrics.';

const CORRECTION_OPTION_LABELS: Record<CorrectionMethod, string> = {
  benjamini_hochberg: 'Benjamini-Hochberg (recommended)',
  bonferroni: 'Bonferroni (stricter)',
  none: 'None',
};

function confidenceOptionLabel(level: number): string {
  const text = formatConfidence(level);
  return level === DEFAULT_CONFIDENCE_LEVEL ? `${text} (recommended)` : text;
}

export const BAYESIAN_LABEL = 'Also analyse the primary metric with Bayesian statistics';
export const BAYESIAN_HELP =
  'The results then also give, for the primary metric, the probability that each variant is the ' +
  'best, using the priors under Advanced Bayesian settings (the defaults unless you change them). ' +
  'The frequentist results and the recommendation are unchanged.';

/** Advanced Bayesian settings (#442): the disclosure's name and each field's help. */
export const BAYESIAN_CONFIG_TOGGLE = 'Advanced Bayesian settings';
export const BAYESIAN_FIELD_HELPS: Record<BayesianNumberField, string> = {
  alpha: `Default 1.0. ${MUST_BE_POSITIVE}`,
  beta: `Default 1.0. ${MUST_BE_POSITIVE}`,
  lossThreshold: `Default 0.001. ${MUST_BE_POSITIVE}`,
  credibleLevel: `Default 0.95. ${MUST_BE_BETWEEN_0_AND_1}`,
};

/** Each number field's element id (and test id); its help and error add `-help` and `-error`. */
const BAYESIAN_FIELD_IDS: Record<BayesianNumberField, string> = {
  alpha: 'bayesian-alpha',
  beta: 'bayesian-beta',
  lossThreshold: 'bayesian-loss-threshold',
  credibleLevel: 'bayesian-credible-level',
};

export const SEQUENTIAL_LABEL = 'Also test the primary metric sequentially (beta)';
export const SEQUENTIAL_HELP =
  'Check results as often as you like, and stop early once the evidence is strong. It compares ' +
  'the control with the first treatment only, on the primary metric, at its own 5% significance ' +
  'level, not the confidence level above. Saved with the experiment.';

/** The hint shown when None is chosen with three or more variants. */
export function noCorrectionHint(variantCount: number, confidenceLevel: number): string {
  const alpha = Number(((1 - confidenceLevel) * 100).toFixed(1));
  return (
    `With ${variantCount} variants and no correction, the chance that at least one looks like a ` +
    `winner by luck is higher than ${alpha}%.`
  );
}

interface AnalysisSettingsFieldsProps {
  confidenceLevel: number;
  correctionMethod: CorrectionMethod;
  dispatch: React.Dispatch<ExperimentFormAction>;
  variantCount: number;
  bayesianEnabled: boolean;
  bayesianConfig: BayesianConfigFormState;
  sequentialEnabled: boolean;
  /**
   * Changed by the caller each time Next or Create was refused for an Advanced
   * Bayesian setting. The section then opens and its first field with a
   * problem takes focus, so a problem never sits in a closed section.
   */
  revealProblems?: number;
}

/**
 * The confidence level and the correction a new experiment is saved with
 * (#580), as a fieldset. Both views of the new-experiment form use it: guided
 * setup on its Estimate step, the single-page form inside "Analysis settings".
 *
 * The sequential switch (#1454) is its own block after the Bayesian one, which
 * is left exactly as it was: video 9 frames `div:has(> #analysis-bayesian)`.
 * Advanced Bayesian settings (#442) is a block of its own between the two,
 * shown only while the Bayesian box is ticked, for the same reason.
 */
export function AnalysisSettingsFields({
  confidenceLevel,
  correctionMethod,
  dispatch,
  variantCount,
  bayesianEnabled,
  bayesianConfig,
  sequentialEnabled,
  revealProblems = 0,
}: AnalysisSettingsFieldsProps) {
  const showHint = correctionMethod === 'none' && variantCount >= 3;
  return (
    <fieldset className="space-y-4" data-testid="analysis-settings">
      <legend className="text-base font-semibold text-slate-900">{ANALYSIS_LEGEND}</legend>
      <p className="text-sm text-slate-600" data-testid="analysis-settings-subline">
        {ANALYSIS_SUBLINE}
      </p>
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor="analysis-confidence" className="block text-sm font-medium text-slate-700 mb-1">
            Confidence level
          </label>
          <select
            id="analysis-confidence"
            value={String(confidenceLevel)}
            onChange={(e) =>
              dispatch({ type: 'setConfidenceLevel', confidenceLevel: Number(e.target.value) })
            }
            aria-describedby="analysis-confidence-help"
            className={wizardInputClass}
            data-testid="analysis-confidence"
          >
            {CONFIDENCE_OPTIONS.map((level) => (
              <option key={level} value={String(level)}>
                {confidenceOptionLabel(level)}
              </option>
            ))}
          </select>
          <p id="analysis-confidence-help" className="text-xs text-slate-600 mt-1">
            {CONFIDENCE_HELP}
          </p>
        </div>
        <div>
          <label htmlFor="analysis-correction" className="block text-sm font-medium text-slate-700 mb-1">
            Correction for several variants
          </label>
          <select
            id="analysis-correction"
            value={correctionMethod}
            onChange={(e) =>
              dispatch({
                type: 'setCorrectionMethod',
                correctionMethod: e.target.value as CorrectionMethod,
              })
            }
            aria-describedby={
              showHint ? 'analysis-correction-help analysis-correction-hint' : 'analysis-correction-help'
            }
            className={wizardInputClass}
            data-testid="analysis-correction"
          >
            {CORRECTION_OPTIONS.map((method) => (
              <option key={method} value={method}>
                {CORRECTION_OPTION_LABELS[method]}
              </option>
            ))}
          </select>
          <p id="analysis-correction-help" className="text-xs text-slate-600 mt-1">
            {CORRECTION_HELP}
          </p>
          {showHint && (
            <p
              id="analysis-correction-hint"
              className="text-xs text-amber-800 mt-1"
              data-testid="analysis-no-correction-hint"
            >
              {noCorrectionHint(variantCount, confidenceLevel)}
            </p>
          )}
        </div>
      </div>
      <div className="flex items-start gap-2">
        <input
          id="analysis-bayesian"
          type="checkbox"
          checked={bayesianEnabled}
          onChange={(e) => dispatch({ type: 'setBayesianEnabled', bayesianEnabled: e.target.checked })}
          aria-describedby="analysis-bayesian-help"
          className="mt-1"
          data-testid="analysis-bayesian"
        />
        <div>
          <label htmlFor="analysis-bayesian" className="text-sm font-medium text-slate-700">
            {BAYESIAN_LABEL}
          </label>
          <p id="analysis-bayesian-help" className="text-xs text-slate-600 mt-1">
            {BAYESIAN_HELP}{' '}
            <a href={docsUrl('api/bayesian')} className="text-blue-700 underline hover:text-blue-900">
              How Bayesian analysis works
            </a>
          </p>
        </div>
      </div>
      {bayesianEnabled && (
        <BayesianConfigFields config={bayesianConfig} dispatch={dispatch} revealProblems={revealProblems} />
      )}
      <div className="flex items-start gap-2">
        <input
          id="analysis-sequential"
          type="checkbox"
          checked={sequentialEnabled}
          onChange={(e) =>
            dispatch({ type: 'setSequentialEnabled', sequentialEnabled: e.target.checked })
          }
          aria-describedby="analysis-sequential-help"
          className="mt-1"
          data-testid="analysis-sequential"
        />
        <div>
          <label htmlFor="analysis-sequential" className="text-sm font-medium text-slate-700">
            {SEQUENTIAL_LABEL}
          </label>
          <p id="analysis-sequential-help" className="text-xs text-slate-600 mt-1">
            {SEQUENTIAL_HELP}{' '}
            <a
              href={docsUrl('api/sequential-testing', 'turning-it-on')}
              className="text-blue-700 underline hover:text-blue-900"
            >
              How sequential testing works
            </a>
          </p>
        </div>
      </div>
    </fieldset>
  );
}

interface BayesianConfigFieldsProps {
  config: BayesianConfigFormState;
  dispatch: React.Dispatch<ExperimentFormAction>;
  revealProblems: number;
}

/**
 * Advanced Bayesian settings (#442): a button that shows or hides the prior's
 * alpha and beta, the loss threshold and the credible level. No prior family:
 * the analysis is always Beta-Binomial (T337). Closed by default; a section whose fields have left their defaults
 * starts open, so switching between guided setup and the single-page form
 * shows them. Each number is checked as it is typed, and a problem is shown
 * under its field and blocks Next and Create.
 */
function BayesianConfigFields({ config, dispatch, revealProblems }: BayesianConfigFieldsProps) {
  const problems = bayesianConfigProblems(config);
  // Left its defaults (a field with a problem never holds its default): starts open.
  const [open, setOpen] = useState(() => buildBayesianConfig(config) !== undefined);
  const [focusField, setFocusField] = useState<BayesianNumberField | null>(null);
  // The count this section was shown with: only a later refusal opens it.
  const revealedRef = useRef(revealProblems);

  useEffect(() => {
    if (revealProblems === revealedRef.current) return;
    revealedRef.current = revealProblems;
    const first = BAYESIAN_NUMBER_FIELDS.find((f) => problems[f] !== null);
    if (!first) return;
    setOpen(true);
    setFocusField(first);
    // Runs when the caller asks; the fields are read as they stand.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revealProblems]);

  useEffect(() => {
    if (focusField === null) return;
    const field = document.getElementById(BAYESIAN_FIELD_IDS[focusField]);
    // The single-page form keeps this inside its collapsed Analysis settings: open that too.
    const section = field?.closest('details');
    if (section && !section.open) section.open = true;
    field?.focus();
    setFocusField(null);
  }, [focusField, open]);

  return (
    <div className="pl-6">
      <button
        type="button"
        id="bayesian-config-toggle"
        onClick={() => setOpen((was) => !was)}
        aria-expanded={open}
        aria-controls="bayesian-config-fields"
        className="text-sm font-medium text-blue-700 hover:text-blue-900 underline focus:outline-none focus:ring-2 focus:ring-blue-600 rounded"
        data-testid="bayesian-config-toggle"
      >
        {BAYESIAN_CONFIG_TOGGLE}
      </button>
      {open && (
        <div
          id="bayesian-config-fields"
          role="group"
          aria-labelledby="bayesian-config-toggle"
          className="mt-3 grid gap-4 sm:grid-cols-2"
          data-testid="bayesian-config-fields"
        >
          {BAYESIAN_NUMBER_FIELDS.map((field) => {
            const id = BAYESIAN_FIELD_IDS[field];
            const problem = problems[field];
            return (
              <div key={field}>
                <label htmlFor={id} className="block text-sm font-medium text-slate-700 mb-1">
                  {BAYESIAN_FIELD_LABELS[field]}
                </label>
                <input
                  id={id}
                  type="number"
                  step="any"
                  inputMode="decimal"
                  value={config[field]}
                  onChange={(e) => dispatch({ type: 'setBayesianField', field, value: e.target.value })}
                  aria-invalid={problem ? true : undefined}
                  aria-describedby={problem ? `${id}-help ${id}-error` : `${id}-help`}
                  className={`${wizardInputClass} ${problem ? 'border-red-600' : ''}`}
                  data-testid={id}
                />
                <p id={`${id}-help`} className="text-xs text-slate-600 mt-1">
                  {BAYESIAN_FIELD_HELPS[field]}
                </p>
                {problem && (
                  <p id={`${id}-error`} className="text-xs text-red-700 mt-1" data-testid={`${id}-error`}>
                    {problem}
                  </p>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
