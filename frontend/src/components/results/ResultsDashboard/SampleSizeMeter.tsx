import React, { useEffect, useId, useState } from 'react';
import { ESTIMATE_PROBLEMS } from '@/components/experiments/new/estimate';
import {
  CorrectionMethod,
  MetricAnalysis,
  SampleSizeGuideOnlyReason,
  SampleSizeOverrides,
  SampleSizeResult,
  SampleSizeUnavailableReason,
} from '@/types/results';
import { meanLabelForType } from '@/components/results/shared/resultFormat';

/**
 * The Sample Size tab (#666): the plan, every input it was calculated from and
 * where each came from, and how far the smallest variant has got.
 *
 * The inputs can be changed and recalculated. Nothing is saved, and nothing is
 * written to the URL: the server decides every input the user has not changed.
 */
interface SampleSizeMeterProps {
  data: SampleSizeResult;
  /** What the user has changed so far; the rest came from the server. */
  overrides?: SampleSizeOverrides;
  /** Asks for a new plan with these overrides. Without it the inputs are read-only. */
  onRecalculate?: (overrides: SampleSizeOverrides) => void;
  recalculating?: boolean;
  /** The planned metric's event name, when the experiment could be read. */
  eventName?: string;
  /**
   * How the results analysed the planned metric (its `analysis` in the results
   * response). Without it, as when the results are not loaded, a revenue,
   * count, duration or custom metric is taken to be a mean, which is how this
   * server analyses them.
   */
  resultsAnalysis?: MetricAnalysis;
}

/**
 * What a plan for a metric that is not a conversion is for. A revenue, count,
 * duration or custom metric's results are its mean per user ("revenue per
 * user", "events per user", ...), but its plan, like every plan, is for the
 * share of users who send its event. Only when the results analysed such a
 * metric as that share (a server older than #439) does the note say so.
 */
function metricTypeNote(
  metricName: string | null,
  metricType: string,
  eventName: string | undefined,
  resultsAnalysis: MetricAnalysis | undefined,
): React.ReactNode {
  const name = metricName ?? 'This metric';
  if (resultsAnalysis !== 'proportion') {
    const label = meanLabelForType(metricType);
    return (
      <>
        {name} is analysed as {label}, but this plan is for the share of users who send{' '}
        {eventName ? <code>{eventName}</code> : 'its event'} (a two-proportion test). A sample-size
        plan for {label} is not available, and the Power Calculator plans conversion rates only.
      </>
    );
  }
  return (
    <>
      {name} is a {metricType} metric, analysed as a conversion: the share of users who sent its
      event, so this plan is for a conversion rate. The Power Calculator plans conversion rates only,
      so it cannot plan a {metricType} metric either.
    </>
  );
}

const POWER_OPTIONS = [0.8, 0.9, 0.95];
const SIGNIFICANCE_OPTIONS = [0.05, 0.01, 0.1];
const CORRECTION_LABELS: Record<CorrectionMethod, string> = {
  none: 'None',
  bonferroni: 'Bonferroni',
  benjamini_hochberg: 'Benjamini-Hochberg',
};

const UNAVAILABLE_TEXT: Record<SampleSizeUnavailableReason, string> = {
  no_metric: 'This experiment has no metric, so there is no rate to plan from. Enter the rate you expect.',
  no_control_data: 'Not enough control data yet — enter the rate you expect.',
  no_control_conversions:
    'No control user has converted yet, so there is no rate to plan from. Enter the rate you expect.',
  rate_at_boundary:
    'Every control user has converted so far, so there is no rate to plan from. Enter the rate you expect.',
  effect_out_of_range:
    'The control rate so far raised by this effect reaches 100% or more. Lower the effect, or enter the rate you expect.',
  effect_too_small:
    'The control rate so far raised by this effect changes too little to estimate a sample size. Raise the effect, or enter the rate you expect.',
};

const GUIDE_ONLY_TEXT: Record<SampleSizeGuideOnlyReason, string> = {
  adaptive_allocation: 'it adapts how traffic is split between variants',
  unequal_allocation:
    'its variants are not split evenly, so the smallest one takes longest to reach the plan',
  sequential_testing: 'it uses sequential testing, and the Sequential tab shows when you can stop',
  bayesian: 'it is analysed with Bayesian statistics',
};

const MDE_PROBLEM_HIGH = 'Enter a minimum detectable effect below 100%.';

/** 0.1234 -> "12.34%", trailing zeros dropped. */
export function pct(value: number, digits = 2): string {
  return `${Number((value * 100).toFixed(digits))}%`;
}

/** A rate as typed into a percentage field. */
function asField(value: number | null | undefined): string {
  return value == null ? '' : String(Number((value * 100).toFixed(4)));
}

function readPercent(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === '') return null;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value / 100 : null;
}

function sameNumber(a: number, b: number): boolean {
  return Math.abs(a - b) < 1e-9;
}

export function SampleSizeMeter({
  data,
  overrides = {},
  onRecalculate,
  recalculating = false,
  eventName,
  resultsAnalysis,
}: SampleSizeMeterProps) {
  const {
    current_sample_size_per_variant: current,
    required_sample_size_per_variant: required,
    is_adequate,
    achieved_power,
    baseline_rate,
    baseline_source,
    baseline_users,
    mde,
    power_target,
    confidence_level,
    correction_method,
    comparisons,
    alpha,
    metric_name,
    metric_type,
    unavailable_reason,
    guide_only_reasons,
  } = data;

  const variants = comparisons + 1;
  const significance = Number((1 - confidence_level).toFixed(4));
  const ids = useId();
  const id = (name: string) => `${ids}-${name}`;

  const [baselineText, setBaselineText] = useState(asField(baseline_rate));
  const [mdeText, setMdeText] = useState(asField(mde));
  const [power, setPower] = useState(power_target);
  const [sig, setSig] = useState(significance);
  const [correction, setCorrection] = useState<CorrectionMethod>(correction_method);
  const [problem, setProblem] = useState<string | null>(null);

  // A new answer resets the form to the inputs it was calculated from.
  useEffect(() => {
    setBaselineText(asField(baseline_rate));
    setMdeText(asField(mde));
    setPower(power_target);
    setSig(Number((1 - confidence_level).toFixed(4)));
    setCorrection(correction_method);
    setProblem(null);
  }, [baseline_rate, mde, power_target, confidence_level, correction_method]);

  const stale =
    baselineText.trim() !== asField(baseline_rate) ||
    mdeText.trim() !== asField(mde) ||
    !sameNumber(power, power_target) ||
    !sameNumber(sig, significance) ||
    correction !== correction_method;

  function recalculate(event: React.FormEvent) {
    event.preventDefault();
    if (!onRecalculate) return;
    const next: SampleSizeOverrides = { ...overrides };

    // A field counts as the user's once its text differs from what the
    // server answered, or once it was the user's before.
    const baselineEdited = baselineText.trim() !== asField(baseline_rate);
    const baselineValue = readPercent(baselineText);
    if (baselineText.trim() === '') {
      delete next.baseline_conversion_rate;
    } else if (baselineValue === null || baselineValue < 0.0001 || baselineValue > 0.9999) {
      setProblem(ESTIMATE_PROBLEMS.baseline);
      return;
    } else if (baselineEdited || baseline_source === 'request') {
      next.baseline_conversion_rate = baselineEdited
        ? baselineValue
        : (overrides.baseline_conversion_rate ?? baselineValue);
    }

    const mdeEdited = mdeText.trim() !== asField(mde);
    const mdeValue = mdeEdited ? readPercent(mdeText) : mde;
    if (mdeValue === null || mdeValue < 0.001) {
      setProblem(ESTIMATE_PROBLEMS.mde);
      return;
    }
    if (mdeValue >= 1) {
      setProblem(MDE_PROBLEM_HIGH);
      return;
    }
    if (mdeEdited || overrides.mde !== undefined) next.mde = mdeValue;

    const plannedBaseline =
      next.baseline_conversion_rate ?? (baseline_source === 'observed' ? baseline_rate : null);
    if (plannedBaseline != null && plannedBaseline * (1 + mdeValue) >= 1) {
      setProblem(ESTIMATE_PROBLEMS.ceiling);
      return;
    }

    if (!sameNumber(power, power_target) || overrides.power_target !== undefined) {
      next.power_target = power;
    }
    if (!sameNumber(sig, significance) || overrides.confidence_level !== undefined) {
      next.confidence_level = Number((1 - sig).toFixed(4));
    }
    if (correction !== correction_method || overrides.correction_method !== undefined) {
      next.correction_method = correction;
    }
    setProblem(null);
    onRecalculate(next);
  }

  function resetToObservedRate() {
    if (!onRecalculate) return;
    const next = { ...overrides };
    delete next.baseline_conversion_rate;
    setProblem(null);
    onRecalculate(next);
  }

  const baselineSource =
    baseline_source === 'observed'
      ? `Observed in control so far: ${pct(baseline_rate ?? 0)} (${(baseline_users ?? 0).toLocaleString()} users). It can be up to five minutes newer than the rate on the Overview.`
      : baseline_source === 'request'
        ? 'Entered by you.'
        : UNAVAILABLE_TEXT[unavailable_reason ?? 'no_control_data'];
  const entered = (key: keyof SampleSizeOverrides) =>
    overrides[key] !== undefined ? 'Entered by you.' : 'Default.';
  // The confidence level and the correction come from the experiment's stored
  // settings unless the user changed them here (#580).
  const fromExperiment = (key: 'confidence_level' | 'correction_method') =>
    overrides[key] !== undefined ? 'Entered by you.' : "From the experiment's settings.";
  // Where the effect came from: the server says (#1257); an older API that
  // does not send mde_source planned for the default.
  const mdeSource =
    overrides.mde !== undefined
      ? 'Entered by you.'
      : data.mde_source === 'experiment'
        ? 'Set when this experiment was created.'
        : data.mde_source === 'request'
          ? 'Entered by you.'
          : 'Default — no effect was saved with this experiment. Change it to the smallest lift worth shipping.';

  const percentDone = required ? Math.round((current / required) * 100) : 0;
  const clamped = Math.max(0, Math.min(percentDone, 100));
  const barColor = is_adequate
    ? 'bg-green-500'
    : percentDone >= 80
      ? 'bg-amber-400'
      : 'bg-red-400';

  const powerOptions = POWER_OPTIONS.includes(power_target)
    ? POWER_OPTIONS
    : [...POWER_OPTIONS, power_target];
  const sigOptions = SIGNIFICANCE_OPTIONS.some((s) => sameNumber(s, significance))
    ? SIGNIFICANCE_OPTIONS
    : [...SIGNIFICANCE_OPTIONS, significance];
  const readOnly = !onRecalculate;

  return (
    <div className="space-y-5" data-testid="sample-size-meter">
      {/* What the plan is calculated from, and where each input came from. */}
      <form
        className="space-y-3 rounded-lg border border-slate-200 p-4"
        onSubmit={recalculate}
        aria-labelledby={id('heading')}
        data-testid="sample-size-inputs"
      >
        <div>
          <h4 id={id('heading')} className="text-sm font-semibold text-slate-800">
            Calculated from
          </h4>
          <p className="text-xs text-slate-600">
            These inputs are not saved with the experiment. Change them to see a different plan.
          </p>
        </div>

        <div>
          <label htmlFor={id('baseline')} className="block text-sm font-medium text-slate-700">
            Baseline conversion rate (%)
          </label>
          <input
            id={id('baseline')}
            type="text"
            inputMode="decimal"
            value={baselineText}
            onChange={(e) => setBaselineText(e.target.value)}
            readOnly={readOnly}
            aria-describedby={id('baseline-source')}
            className="mt-1 w-32 rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="sample-size-baseline-input"
          />
          <p id={id('baseline-source')} className="text-xs text-slate-600" data-testid="sample-size-baseline-source">
            {baselineSource}
          </p>
          {baseline_source === 'request' && onRecalculate && (
            <button
              type="button"
              onClick={resetToObservedRate}
              className="text-xs text-blue-700 underline hover:text-blue-900"
            >
              Use the observed rate
            </button>
          )}
        </div>

        <div>
          <label htmlFor={id('mde')} className="block text-sm font-medium text-slate-700">
            Minimum detectable effect, relative (%)
          </label>
          <input
            id={id('mde')}
            type="text"
            inputMode="decimal"
            value={mdeText}
            onChange={(e) => setMdeText(e.target.value)}
            readOnly={readOnly}
            aria-describedby={id('mde-source')}
            className="mt-1 w-32 rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="sample-size-mde-input"
          />
          <p id={id('mde-source')} className="text-xs text-slate-600" data-testid="sample-size-mde-source">
            {mdeSource} 5% means 12% → 12.6%, not 17%.
          </p>
        </div>

        <div className="flex flex-wrap gap-4">
          <div>
            <label htmlFor={id('power')} className="block text-sm font-medium text-slate-700">
              Power
            </label>
            <select
              id={id('power')}
              value={power}
              onChange={(e) => setPower(Number(e.target.value))}
              disabled={readOnly}
              aria-describedby={id('power-source')}
              className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
              data-testid="sample-size-power-input"
            >
              {powerOptions.map((p) => (
                <option key={p} value={p}>
                  {pct(p)}
                </option>
              ))}
            </select>
            <p id={id('power-source')} className="text-xs text-slate-600" data-testid="sample-size-power-source">
              {entered('power_target')}
            </p>
          </div>

          <div>
            <label htmlFor={id('significance')} className="block text-sm font-medium text-slate-700">
              Significance (two-sided)
            </label>
            <select
              id={id('significance')}
              value={sig}
              onChange={(e) => setSig(Number(e.target.value))}
              disabled={readOnly}
              aria-describedby={id('significance-source')}
              className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
              data-testid="sample-size-significance-input"
            >
              {sigOptions.map((s) => (
                <option key={s} value={s}>
                  {SIGNIFICANCE_OPTIONS.some((o) => sameNumber(o, s))
                    ? pct(s)
                    : `${pct(s)} (this experiment's setting)`}
                </option>
              ))}
            </select>
            <p
              id={id('significance-source')}
              className="text-xs text-slate-600"
              data-testid="sample-size-significance-source"
            >
              {fromExperiment('confidence_level')}
            </p>
          </div>

          {comparisons >= 2 && (
            <div>
              <label htmlFor={id('correction')} className="block text-sm font-medium text-slate-700">
                Correction
              </label>
              <select
                id={id('correction')}
                value={correction}
                onChange={(e) => setCorrection(e.target.value as CorrectionMethod)}
                disabled={readOnly}
                aria-describedby={id('correction-source')}
                className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
                data-testid="sample-size-correction-input"
              >
                {(Object.keys(CORRECTION_LABELS) as CorrectionMethod[]).map((m) => (
                  <option key={m} value={m}>
                    {CORRECTION_LABELS[m]}
                  </option>
                ))}
              </select>
              <p
                id={id('correction-source')}
                className="text-xs text-slate-600"
                data-testid="sample-size-correction-source"
              >
                {fromExperiment('correction_method')}
              </p>
            </div>
          )}
        </div>

        <p className="text-xs text-slate-600" data-testid="sample-size-metric">
          {metric_name
            ? `Planned for ${metric_name}, analysed as a conversion rate (a two-proportion test).`
            : 'Planned for a conversion rate (a two-proportion test).'}
          {' '}Variants: {variants} (from this experiment).
        </p>
        {metric_type && metric_type !== 'conversion' && (
          <p className="text-xs text-amber-800" data-testid="sample-size-metric-type-note">
            {metricTypeNote(metric_name, metric_type, eventName, resultsAnalysis)}
          </p>
        )}

        {onRecalculate && (
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={recalculating}
              className="rounded-lg bg-blue-600 px-3 py-1.5 text-sm text-white hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 disabled:opacity-60"
            >
              {recalculating ? 'Recalculating…' : 'Recalculate'}
            </button>
            {stale && !recalculating && (
              <span className="text-xs text-slate-700" data-testid="sample-size-stale">
                Inputs changed. Press Recalculate to update the plan.
              </span>
            )}
          </div>
        )}
        {problem && (
          <p role="alert" className="text-sm text-red-700" data-testid="sample-size-problem">
            {problem}
          </p>
        )}
      </form>

      {/* The result. */}
      <div role="status" aria-live="polite" className="space-y-3" data-testid="sample-size-result">
        {current === 0 && (
          <p className="text-sm text-slate-700" data-testid="sample-size-no-traffic">
            No users have been assigned yet. The plan uses your inputs; progress appears once traffic
            arrives.
          </p>
        )}

        {required === null ? (
          <p className="text-sm text-slate-700" data-testid="sample-size-unavailable">
            {UNAVAILABLE_TEXT[unavailable_reason ?? 'no_control_data']}
          </p>
        ) : (
          <>
            <p className="text-sm text-slate-800" data-testid="sample-size-required">
              <span className="font-semibold">{required.toLocaleString()} users per variant</span> (
              {(required * variants).toLocaleString()} in total across {variants} variants) at{' '}
              {pct(power_target)} power and {pct(significance)} significance, two-sided.
            </p>

            <div>
              <div className="mb-1 flex justify-between text-sm text-slate-600">
                <span data-testid="sample-size-label">
                  Smallest variant: {current.toLocaleString()} of {required.toLocaleString()} (
                  {percentDone}%)
                </span>
                <span
                  className={`font-semibold ${is_adequate ? 'text-green-700' : 'text-slate-700'}`}
                  data-testid="sample-size-status"
                >
                  {is_adequate ? 'Planned sample reached' : `${percentDone}% of planned sample`}
                </span>
              </div>
              <div
                className="h-4 w-full rounded-full bg-gray-200"
                role="progressbar"
                aria-valuenow={clamped}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label={`Planned sample: ${clamped}%`}
              >
                <div
                  className={`h-4 rounded-full transition-all duration-300 ${barColor}`}
                  style={{ width: `${clamped}%` }}
                  data-testid="sample-size-bar"
                />
              </div>
            </div>

            {achieved_power !== null && (
              <p className="text-sm text-slate-600" data-testid="power-label">
                Power to detect a {pct(mde)} lift at the current sample:{' '}
                <span className="font-medium text-slate-800">{pct(achieved_power, 0)}</span>
              </p>
            )}
          </>
        )}

        {comparisons >= 2 && (
          <p className="text-sm text-slate-600" data-testid="sample-size-correction-note">
            {correction_method === 'none'
              ? `With ${variants} variants, this plan makes no correction for comparing several variants with the control${overrides.correction_method === undefined ? ': the experiment is set to no correction' : ''}. Choose a correction to plan each comparison at ${pct(significance / comparisons, 3)} significance.`
              : overrides.correction_method === undefined
                ? `With ${variants} variants, this plan uses the experiment's correction (${CORRECTION_LABELS[correction_method]}): each of the ${comparisons} comparisons with the control is planned at ${pct(alpha, 3)} significance.`
                : `With ${variants} variants, each of the ${comparisons} comparisons with the control is planned at ${pct(alpha, 3)} significance (${CORRECTION_LABELS[correction_method]}).`}
          </p>
        )}

        {guide_only_reasons.length > 0 && (
          <p className="text-sm text-amber-800" data-testid="sample-size-guide-only">
            A fixed sample size is a guide only for this experiment:{' '}
            {guide_only_reasons.map((r) => GUIDE_ONLY_TEXT[r]).join('; ')}.
          </p>
        )}
      </div>
    </div>
  );
}
