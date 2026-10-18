import React from 'react';
import Link from 'next/link';
import { ExperimentsService, SampleSizeEstimate as EstimateResponse } from '@/services/experiments';
import { isApiError } from '@/services/api';
import {
  buildEstimateQuery,
  EstimateInputs,
  estimateSignificance,
  INITIAL_ESTIMATE_INPUTS,
  isEvenSplit,
  POWER_OPTIONS,
} from './estimate';
import { wizardInputClass } from './fieldStyles';
import { CorrectionMethod } from '@/types/results';
import { correctionName } from '@/components/results/shared/analysisSettings';

/** The estimate's inputs other than the effect, which lives in the form state. */
export type EstimatePanelInputs = Omit<EstimateInputs, 'mdePct'>;

/**
 * Everything the Estimate step shows except the minimum detectable effect,
 * held by the wizard so it survives Back and Next. The effect is saved with
 * the experiment, so it lives in the form state (`plannedEffectPct`).
 */
export interface EstimatePanelState {
  inputs: EstimatePanelInputs;
  /** The last answer, and the exact query it answered (to tell when it is out of date). */
  result: { estimate: EstimateResponse; queryKey: string } | null;
  /** A refused input or a failed request; shown in the panel's own alert region. */
  problem: string | null;
  loading: boolean;
  /** Increments per request, so a slow answer to an older request is dropped. */
  requestId: number;
}

export const INITIAL_ESTIMATE_PANEL: EstimatePanelState = {
  inputs: {
    baselinePct: INITIAL_ESTIMATE_INPUTS.baselinePct,
    power: INITIAL_ESTIMATE_INPUTS.power,
    dailyUsers: INITIAL_ESTIMATE_INPUTS.dailyUsers,
    sharePct: INITIAL_ESTIMATE_INPUTS.sharePct,
  },
  result: null,
  problem: null,
  loading: false,
  requestId: 0,
};

export const SESSION_EXPIRED_ESTIMATE =
  'Your session has expired; sign in again in another tab, then come back.';
export const NO_USABLE_ESTIMATE =
  'The calculator did not return a usable estimate for these numbers. Check the baseline rate and the effect.';

interface SampleSizeEstimateProps {
  value: EstimatePanelState;
  onChange: React.Dispatch<React.SetStateAction<EstimatePanelState>>;
  /** The minimum detectable effect as typed, in percent; saved with the experiment when usable. */
  mdePct: string;
  onMdePctChange: (mdePct: string) => void;
  /** Each variant's share of traffic, in percent, from the Variants step. */
  allocations: number[];
  /** The experiment's confidence level: the estimate plans at 1 minus it. */
  confidenceLevel: number;
  /** The experiment's correction. The estimate does not apply it; the copy says so. */
  correctionMethod: CorrectionMethod;
}

const pct = (fraction: number) => `${Math.round(fraction * 1000) / 10}%`;
const count = (n: number) => n.toLocaleString('en-US');

/**
 * What the estimate says about the correction with three or more variants. The
 * estimate plans each comparison without one (PE condition 9; #820 would add it).
 */
export function noCorrectionInEstimate(
  variantCount: number,
  confidenceLevel: number,
  correctionMethod: CorrectionMethod,
): string {
  const sig = pct(estimateSignificance(confidenceLevel));
  if (correctionMethod === 'none') {
    return (
      `With ${variantCount} variants, this estimate plans each comparison with the control at ` +
      `${sig} significance and makes no correction for comparing several variants, the same as ` +
      'the results will, because no correction is chosen above.'
    );
  }
  return (
    `With ${variantCount} variants, this estimate plans each comparison with the control at ` +
    `${sig} significance and makes no correction for comparing several variants. The results ` +
    `will use the ${correctionName(correctionMethod)} correction chosen above, so the experiment ` +
    'needs more users than this.'
  );
}

/**
 * The estimate's answer when it answers exactly these inputs, or null when
 * there is none or the inputs have changed since.
 */
export function currentEstimate(
  value: EstimatePanelState,
  mdePct: string,
  variantCount: number,
  confidenceLevel: number,
): EstimateResponse | null {
  if (value.result === null) return null;
  const current = buildEstimateQuery({ ...value.inputs, mdePct }, variantCount, confidenceLevel);
  return current.ok && JSON.stringify(current.query) === value.result.queryKey ? value.result.estimate : null;
}

/**
 * A sample-size and duration estimate. It is calculated only when the button
 * is pressed and never blocks creating the experiment. Its minimum detectable
 * effect, when usable, is saved with the experiment (the wizard refuses Next
 * on one that is typed but unusable); the rest of what it holds is a guide
 * and is not sent.
 */
export function SampleSizeEstimate({
  value,
  onChange,
  mdePct,
  onMdePctChange,
  allocations,
  confidenceLevel,
  correctionMethod,
}: SampleSizeEstimateProps) {
  const variantCount = allocations.length;
  const { result, problem, loading } = value;
  const inputs: EstimateInputs = { ...value.inputs, mdePct };
  const stale = result !== null && currentEstimate(value, mdePct, variantCount, confidenceLevel) === null;

  const setInput = <K extends keyof EstimatePanelInputs>(key: K, next: EstimatePanelInputs[K]) =>
    onChange((prev) => ({ ...prev, inputs: { ...prev.inputs, [key]: next } }));

  const calculate = async () => {
    const built = buildEstimateQuery(inputs, variantCount, confidenceLevel);
    if (!built.ok) {
      onChange((prev) => ({ ...prev, problem: built.problem }));
      return;
    }
    const requestId = value.requestId + 1;
    onChange((prev) => ({ ...prev, problem: null, loading: true, requestId }));
    let estimate: EstimateResponse | null = null;
    let failure: string | null = null;
    try {
      estimate = await ExperimentsService.estimateSampleSize(built.query);
      if (!(estimate.samples_per_variant >= 1)) failure = NO_USABLE_ESTIMATE;
    } catch (err) {
      if (isApiError(err) && err.isUnauthorized) failure = SESSION_EXPIRED_ESTIMATE;
      else failure = err instanceof Error ? err.message : 'The estimate could not be calculated.';
    }
    onChange((prev) => {
      if (prev.requestId !== requestId) return prev;
      if (failure || !estimate) return { ...prev, loading: false, problem: failure, result: null };
      return {
        ...prev,
        loading: false,
        problem: null,
        result: { estimate, queryKey: JSON.stringify(built.query) },
      };
    });
  };

  const estimate = result?.estimate;
  const duration = estimate?.estimated_duration_days;

  return (
    <div className="space-y-5" data-testid="sample-size-estimate">
      <p className="text-sm text-slate-600" data-testid="estimate-intro">
        The minimum detectable effect you enter is saved with the experiment, and its results page plans
        the sample for it. The rest of this estimate is a guide and is not saved: the results page plans
        from the control variant&apos;s own conversion rate, at 80% power. This estimate plans at{' '}
        {pct(estimateSignificance(confidenceLevel))} significance, from the confidence level above.
      </p>

      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor="estimate-baseline" className="block text-sm font-medium text-slate-700 mb-1">
            Baseline conversion rate (%)
          </label>
          <input
            id="estimate-baseline"
            type="number"
            inputMode="decimal"
            step="any"
            min={0.01}
            max={99.99}
            value={inputs.baselinePct}
            onChange={(e) => setInput('baselinePct', e.target.value)}
            aria-describedby="estimate-baseline-help"
            className={wizardInputClass}
            data-testid="estimate-baseline"
          />
          <p id="estimate-baseline-help" className="text-xs text-slate-600 mt-1">
            The share of users who convert today.
          </p>
        </div>

        <div>
          <label htmlFor="estimate-mde" className="block text-sm font-medium text-slate-700 mb-1">
            Minimum detectable effect (relative) (%)
          </label>
          <input
            id="estimate-mde"
            type="number"
            inputMode="decimal"
            step="any"
            min={0.1}
            value={mdePct}
            onChange={(e) => onMdePctChange(e.target.value)}
            aria-describedby="estimate-mde-help"
            className={wizardInputClass}
            data-testid="estimate-mde"
          />
          <p id="estimate-mde-help" className="text-xs text-slate-600 mt-1">
            5% means 12% → 12.6%, not 17%. Saved with the experiment; leave it blank and the results
            page plans for 5%.
          </p>
        </div>

        <div>
          <label htmlFor="estimate-power" className="block text-sm font-medium text-slate-700 mb-1">
            Statistical power
          </label>
          <select
            id="estimate-power"
            value={String(inputs.power)}
            onChange={(e) => setInput('power', Number(e.target.value))}
            className={wizardInputClass}
            data-testid="estimate-power"
          >
            {POWER_OPTIONS.map((p) => (
              <option key={p} value={String(p)}>
                {pct(p)}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="estimate-daily-users" className="block text-sm font-medium text-slate-700 mb-1">
            Daily users (optional)
          </label>
          <input
            id="estimate-daily-users"
            type="number"
            inputMode="numeric"
            step={1}
            min={1}
            value={inputs.dailyUsers}
            onChange={(e) => setInput('dailyUsers', e.target.value)}
            aria-describedby="estimate-daily-users-help"
            className={wizardInputClass}
            data-testid="estimate-daily-users"
          />
          <p id="estimate-daily-users-help" className="text-xs text-slate-600 mt-1">
            Add this for an estimate of how many days the experiment needs.
          </p>
        </div>

        <div>
          <label htmlFor="estimate-share" className="block text-sm font-medium text-slate-700 mb-1">
            Share of those users in this experiment (%)
          </label>
          <input
            id="estimate-share"
            type="number"
            inputMode="decimal"
            step="any"
            min={0}
            max={100}
            value={inputs.sharePct}
            onChange={(e) => setInput('sharePct', e.target.value)}
            aria-describedby="estimate-share-help"
            className={wizardInputClass}
            data-testid="estimate-share"
          />
          <p id="estimate-share-help" className="text-xs text-slate-600 mt-1">
            Leave blank to count all of them. This is not saved; the split between variants is set on
            the Variants step.
          </p>
        </div>
      </div>

      <p className="text-sm text-slate-700" data-testid="estimate-variant-count">
        Variants: {variantCount} (from the Variants step)
      </p>

      <div>
        <button
          type="button"
          onClick={calculate}
          disabled={loading}
          className="px-4 py-2 rounded-lg border border-blue-600 text-sm font-medium text-blue-700 hover:bg-blue-50 focus:outline-none focus:ring-2 focus:ring-blue-600 disabled:opacity-50"
          data-testid="estimate-calculate"
        >
          {loading ? 'Calculating…' : 'Calculate estimate'}
        </button>
      </div>

      <div role="alert" data-testid="estimate-error">
        {problem && (
          <p className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700">{problem}</p>
        )}
      </div>

      <div role="status" aria-live="polite" data-testid="estimate-result">
        {estimate && (
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-4 space-y-2 text-sm text-slate-700">
            {stale && (
              <p className="font-medium text-amber-800" data-testid="estimate-stale">
                The inputs changed since this estimate. Calculate again to update it.
              </p>
            )}
            <p className="text-base font-semibold text-slate-900" data-testid="estimate-per-variant">
              {count(estimate.samples_per_variant)} users per variant
            </p>
            <p data-testid="estimate-total">
              {count(estimate.total_samples)} users in total across {variantCount} variants, at{' '}
              {pct(estimate.statistical_power)} power and {pct(estimate.significance_level)} significance.
            </p>
            {duration && (
              <p data-testid="estimate-duration">
                About {count(duration.days)} {duration.days === 1 ? 'day' : 'days'} (about{' '}
                {count(duration.weeks)} {duration.weeks === 1 ? 'week' : 'weeks'}).
              </p>
            )}
            {estimate.notes && <p data-testid="estimate-notes">{estimate.notes}</p>}
            <p data-testid="estimate-even-split">
              This assumes users are split evenly across the {variantCount} variants.
            </p>
            {!isEvenSplit(allocations) && (
              <p className="text-amber-800" data-testid="estimate-uneven-split">
                Your variants are not split evenly. The variant with the smallest share gets fewer users
                a day, so it takes longer than this to reach the number above.
              </p>
            )}
            {variantCount >= 3 && (
              <p data-testid="estimate-many-variants">
                {noCorrectionInEstimate(variantCount, confidenceLevel, correctionMethod)} The{' '}
                <Link href="/power-calculator" className="text-blue-700 underline hover:text-blue-900">
                  Power Calculator
                </Link>{' '}
                applies a Bonferroni correction, so its number is higher.
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
