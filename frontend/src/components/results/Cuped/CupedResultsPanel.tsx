/**
 * Results › CUPED (beta): `GET /results/{id}/cuped`, a conversion metric's
 * change with each user's pre-experiment history taken into account, next to
 * the same change without it.
 *
 * - Fixed copy for every state and every reason: the server's text is never
 *   shown. A comparison that could not be adjusted is listed with its reason
 *   and no numbers.
 * - Significance and the p-value are the server's: the corrected p-value when
 *   there is one, else the p-value before correction, and `is_significant`
 *   as sent. The words "adjusted p-value" are not used here: in this response
 *   `adjusted_p_value` is the CUPED-adjusted one *before* correction, the
 *   opposite of what the Overview tab's column of that name holds.
 * - The Overview tab's table is untouched and stays unadjusted.
 */
import React, { useRef } from 'react';
import { CorrectionMethod, CupedMetricResult, CupedResultsResponse } from '@/types/results';
import { StatisticalBadge } from '@/components/results/shared/StatisticalBadge';
import { LeadWithInterval } from '@/components/results/shared/IntervalText';
import {
  IntervalParts,
  NOT_ENOUGH_DATA,
  finitePair,
  formatRate,
  formatSignedAmount,
  isFiniteNumber,
} from '@/components/results/shared/resultFormat';
import { correctionName, formatConfidence } from '@/components/results/shared/analysisSettings';
import { docsUrl } from '@/services/docs';
import { CupedState } from './useCupedResults';

export const CUPED_LOADING = 'Loading the CUPED results…';
export const CUPED_BETA_LINE =
  'Beta: only conversion metrics are adjusted for now, so this tab may change when averages are added. ' +
  'Its numbers are computed as the CUPED guide describes.';
export const CUPED_HISTORY_LINK = 'How to send pre-experiment history';
export const CUPED_NOT_USED = 'This experiment does not use CUPED.';
export const CUPED_NO_METRICS = 'This experiment has no metrics to adjust.';
export const CUPED_NO_HISTORY = '0% (no history: same as without CUPED)';
export const CUPED_VARIANCE_NOTE =
  'Variance reduction is how much CUPED shrank the variance of the change: at 36% the interval is 80% as wide. ' +
  'It can be negative; then CUPED did not help that comparison, and its numbers are still valid.';
export const CUPED_UNKNOWN_REASON = 'Not adjusted for this comparison.';

/** Fixed copy per `unavailable_reason`; any code not listed reads CUPED_UNKNOWN_REASON. */
export const CUPED_REASON_COPY: Record<string, string> = {
  not_a_proportion_metric:
    'Not adjusted: only conversion metrics are adjusted for now. Its numbers without CUPED are on the Overview tab.',
  winsorization_needs_mean_metric:
    "Not adjusted: the experiment's method is winsorization, which does not apply to a conversion metric.",
  fewer_than_2_units: 'Not adjusted yet: the control or this variant has fewer than 2 users.',
  no_variation:
    'Not adjusted: in both groups nobody, or everybody, converted, so there is no standard error to compute.',
  no_residual_variation:
    "Not adjusted: history predicts every user's outcome exactly in both groups, so no p-value can be given.",
  no_control_variant: 'Not adjusted: the experiment has no control variant.',
  no_treatment_variant: 'Not adjusted: the experiment has no variant to compare with the control.',
  covariate_metric_not_found:
    "Not adjusted: the metric chosen as history is no longer one of the experiment's metrics.",
  metric_has_no_event_name: 'Not adjusted: the metric read as history has no event name.',
  result_invalid: 'Not adjusted: the counts are ones no sample could produce.',
};

export function reasonCopy(reason: string | null): string {
  if (reason && Object.prototype.hasOwnProperty.call(CUPED_REASON_COPY, reason)) {
    return CUPED_REASON_COPY[reason];
  }
  return CUPED_UNKNOWN_REASON;
}

const HEAD = 'px-4 py-3 text-xs font-medium uppercase tracking-wide text-slate-600';
const CELL = 'px-4 py-3 text-sm text-slate-800 tabular-nums align-top';
const CAPTION = 'px-4 py-2 text-left text-sm font-medium text-slate-800';

const COLUMNS: { label: string; align: string }[] = [
  { label: 'Variant', align: 'text-left' },
  { label: 'Users (variant / control)', align: 'text-right' },
  { label: 'Rate with CUPED (variant / control)', align: 'text-right' },
  { label: 'Change with CUPED', align: 'text-left' },
  { label: 'Change without CUPED', align: 'text-left' },
  { label: 'Variance reduction', align: 'text-right' },
  { label: 'Users with history', align: 'text-right' },
  { label: 'Result', align: 'text-left' },
];

function groupedCount(n: number | null): string {
  return isFiniteNumber(n) ? n.toLocaleString('en-US') : '0';
}

/** A percentage to 1 decimal with a real minus sign; a value that rounds to 0 has no sign. */
function formatPercent(v: number): string {
  const fixed = Math.abs(v).toFixed(1);
  return v < 0 && Number(fixed) !== 0 ? `−${fixed}%` : `${fixed}%`;
}

function pointsText(effect: number): string {
  return `${formatSignedAmount(effect * 100)} percentage points`;
}

/** A row with numbers: no reason, and an effect to show. */
function isAdjusted(row: CupedMetricResult): boolean {
  return !row.unavailable_reason && isFiniteNumber(row.adjusted_effect);
}

function correctionApplies(method: string | null): method is CorrectionMethod {
  return method === 'bonferroni' || method === 'benjamini_hochberg';
}

function intervalOf(
  row: CupedMetricResult,
  level: number | null,
  unadjusted: boolean,
): IntervalParts | null {
  const pair = finitePair([row.adjusted_ci_lower, row.adjusted_ci_upper]);
  if (!pair || !isFiniteNumber(level)) return null;
  return {
    level: formatConfidence(level),
    unadjusted,
    lower: formatSignedAmount(pair[0] * 100),
    upper: formatSignedAmount(pair[1] * 100),
  };
}

function MetricTable({
  metricName,
  rows,
  data,
  controlNames,
}: {
  metricName: string;
  rows: CupedMetricResult[];
  data: CupedResultsResponse;
  controlNames: Record<string, string>;
}) {
  const caption = `CUPED-adjusted results: ${metricName}`;
  const level = data.confidence_level;
  const correction = correctionApplies(data.correction_method) ? data.correction_method : null;
  const computed = rows.filter(isAdjusted).length;

  // A row with no variant (the experiment has no control or no treatment) is
  // the whole metric's reason, not a comparison.
  const metricLevel = rows.filter((r) => r.variant_id === null);
  const comparisons = rows.filter((r) => r.variant_id !== null);

  if (comparisons.length === 0) {
    return (
      <div className="space-y-1">
        <p className={CAPTION}>{caption}</p>
        {metricLevel.map((r, i) => (
          <p key={i} className="px-4 text-sm text-slate-700">
            {reasonCopy(r.unavailable_reason)}
          </p>
        ))}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {metricLevel.map((r, i) => (
        <p key={i} className="text-sm text-slate-700">
          {reasonCopy(r.unavailable_reason)}
        </p>
      ))}
      <div className="overflow-x-auto rounded-lg border border-slate-200">
        <table className="min-w-full divide-y divide-slate-200">
          <caption className={CAPTION}>{caption}</caption>
          <thead className="bg-slate-50">
            <tr>
              {COLUMNS.map((c) => (
                <th key={c.label} scope="col" className={`${HEAD} ${c.align}`}>
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 bg-white">
            {comparisons.map((row, i) => (
              <tr key={`${row.variant_id}-${i}`}>
                <th scope="row" className={`${CELL} text-left font-medium`}>
                  <span className="block">{row.variant_name ?? 'Variant'}</span>
                  <span className="block text-xs font-normal text-slate-600">
                    {`vs ${
                      (row.control_variant_id && controlNames[row.control_variant_id]) ||
                      'the control'
                    }`}
                  </span>
                </th>
                {isAdjusted(row) ? (
                  <AdjustedCells
                    row={row}
                    level={level}
                    correction={correction}
                    computed={computed}
                  />
                ) : (
                  <td colSpan={COLUMNS.length - 1} className={`${CELL} text-slate-700`}>
                    {reasonCopy(row.unavailable_reason)}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-sm text-slate-700">{CUPED_VARIANCE_NOTE}</p>
      {correction && (
        <p className="text-sm text-slate-700">
          {`p-values are corrected with ${correctionName(correction)} across each metric's variants.`}
        </p>
      )}
    </div>
  );
}

function AdjustedCells({
  row,
  level,
  correction,
  computed,
}: {
  row: CupedMetricResult;
  level: number | null;
  correction: CorrectionMethod | null;
  computed: number;
}) {
  const corrected = isFiniteNumber(row.corrected_p_value);
  // A corrected p-value is shown on a metric with two or more comparisons:
  // every interval of such a row is then marked "unadjusted" (not corrected).
  const intervalUnadjusted = correction !== null && corrected && computed >= 2;
  const pValue = corrected ? row.corrected_p_value : row.adjusted_p_value;
  const history = row.covariate_coverage_pct;
  const reduction = row.variance_reduction_pct;
  return (
    <>
      <td className={`${CELL} text-right`}>
        {`${groupedCount(row.treatment_sample_size)} / ${groupedCount(row.control_sample_size)}`}
      </td>
      <td className={`${CELL} text-right`}>
        {`${formatRate(row.adjusted_treatment_mean, row.treatment_sample_size ?? 0)} / ${formatRate(
          row.adjusted_control_mean,
          row.control_sample_size ?? 0,
        )}`}
      </td>
      <td className={CELL}>
        <LeadWithInterval
          lead={pointsText(row.adjusted_effect as number)}
          interval={intervalOf(row, level, intervalUnadjusted)}
        />
      </td>
      <td className={CELL}>
        {isFiniteNumber(row.unadjusted_effect) ? pointsText(row.unadjusted_effect) : NOT_ENOUGH_DATA}
      </td>
      <td className={`${CELL} text-right`}>
        {isFiniteNumber(reduction) ? formatPercent(reduction) : NOT_ENOUGH_DATA}
      </td>
      <td className={`${CELL} text-right`}>
        {!isFiniteNumber(history)
          ? NOT_ENOUGH_DATA
          : history === 0
            ? CUPED_NO_HISTORY
            : formatPercent(history)}
      </td>
      <td className={CELL}>
        <StatisticalBadge
          pValue={pValue}
          confidenceLevel={level ?? 0.95}
          isSignificant={row.is_significant}
          pLabel={corrected ? 'corrected p' : 'p'}
        />
      </td>
    </>
  );
}

function Loaded({
  data,
  controlNames,
}: {
  data: CupedResultsResponse;
  controlNames: Record<string, string>;
}) {
  const stale = data.method !== 'cuped' && data.method !== 'cuped_plus';
  if (stale || data.metrics.length === 0) {
    return (
      <p className="text-sm text-slate-700" data-testid="cuped-results">
        {stale ? CUPED_NOT_USED : CUPED_NO_METRICS}
      </p>
    );
  }

  // One table per metric, in the order the response lists them.
  const order: string[] = [];
  const byMetric = new Map<string, CupedMetricResult[]>();
  for (const row of data.metrics) {
    if (!byMetric.has(row.metric_id)) {
      byMetric.set(row.metric_id, []);
      order.push(row.metric_id);
    }
    byMetric.get(row.metric_id)?.push(row);
  }

  // The method the rows computed: cuped_plus is computed as CUPED for now.
  const computedAsCuped = data.method === 'cuped_plus' && data.metrics.every((r) => r.method === 'cuped');
  const methodLine =
    data.method === 'cuped_plus'
      ? computedAsCuped
        ? 'Method: CUPED+, computed as CUPED for now.'
        : 'Method: CUPED+.'
      : 'Method: CUPED.';

  return (
    <div className="space-y-6" data-testid="cuped-results">
      <p className="text-sm text-slate-700">{CUPED_BETA_LINE}</p>
      <p className="text-sm text-slate-700">
        {`CUPED narrows each interval using what each user did in the ${
          data.covariate_lookback_days ?? 'few'
        } days before they were assigned. It needs that history: an event the server received after a user was assigned does not count for that user. `}
        <a
          href={docsUrl('api/cuped', 'sending-pre-experiment-history')}
          className="text-blue-700 underline hover:text-blue-900"
        >
          {CUPED_HISTORY_LINK}
        </a>
      </p>
      <p className="text-sm text-slate-700">{methodLine}</p>
      {order.map((id) => {
        const rows = byMetric.get(id) ?? [];
        return (
          <MetricTable
            key={id}
            metricName={rows[0].metric_name}
            rows={rows}
            data={data}
            controlNames={controlNames}
          />
        );
      })}
    </div>
  );
}

export function CupedResultsPanel({
  state,
  onRetry,
  controlNames,
}: {
  state: CupedState;
  onRetry: () => void;
  /** The experiment's variant names by id, for "vs {control}". */
  controlNames: Record<string, string>;
}) {
  const heading = useRef<HTMLHeadingElement>(null);

  return (
    <section aria-label="CUPED-adjusted results" className="space-y-4">
      <h3
        ref={heading}
        tabIndex={-1}
        className="flex items-center gap-2 text-base font-semibold text-slate-800 focus:outline-none"
      >
        CUPED-adjusted results
        <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900">
          Beta
        </span>
      </h3>
      {(state.status === 'idle' || state.status === 'loading') && (
        <p role="status" className="text-sm text-slate-700">
          {CUPED_LOADING}
        </p>
      )}
      {state.status === 'error' && (
        <div className="space-y-3" data-testid="cuped-failed">
          <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-4 py-3">
            <p className="text-sm text-red-700">{state.message}</p>
          </div>
          <button
            type="button"
            onClick={() => {
              // The button unmounts while the request runs; hand focus to the
              // heading (which stays) so it never drops to the page.
              heading.current?.focus();
              onRetry();
            }}
            className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-800 hover:bg-slate-50 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            Try again
          </button>
        </div>
      )}
      {state.status === 'ready' && <Loaded data={state.data} controlNames={controlNames} />}
    </section>
  );
}
