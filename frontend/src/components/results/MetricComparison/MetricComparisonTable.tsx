import React, { useState } from 'react';
import { CorrectionMethod, MetricResult, VariantResult } from '@/types/results';
import { StatisticalBadge } from '@/components/results/shared/StatisticalBadge';
import {
  NOT_ENOUGH_DATA,
  analysedAsRate,
  changeVerdict,
  correctionLabel,
  decisivePValue,
  formatRate,
  formatSignedPct,
  isFiniteNumber,
  metricLowerIsBetter,
  rateDescription,
} from '@/components/results/shared/resultFormat';
import {
  comparisonsOf,
  correctionName,
  formatAlpha,
} from '@/components/results/shared/analysisSettings';

interface MetricComparisonTableProps {
  metrics: MetricResult[];
  confidenceLevel: number;
  /** The response's correction_method: the correction the p-values were adjusted with. */
  correctionMethod?: CorrectionMethod;
}

type SortKey = 'metric' | 'variant' | 'sample_size' | 'mean' | 'improvement' | 'p_value';
type SortDir = 'asc' | 'desc';

// Colour follows the verdict in the metric's direction (`changeVerdict`):
// green for better, red for worse. The words "better" and "worse" are in the
// cell as well, so the colour only repeats them. A significant change whose
// direction the response does not give is neutral, never green or red.
function changeClass(verdict: 'better' | 'worse' | null, variant: VariantResult): string {
  if (variant.is_control) return 'text-slate-600';
  if (verdict === 'better') return 'text-green-700';
  if (verdict === 'worse') return 'text-red-700';
  if (variant.is_significant && isFiniteNumber(variant.relative_improvement_pct)) {
    return 'text-slate-700';
  }
  return 'text-slate-500';
}

function formatImprovement(v: number | null | undefined, isControl: boolean): string {
  if (isControl) return '—';
  if (!isFiniteNumber(v)) return NOT_ENOUGH_DATA;
  return formatSignedPct(v);
}

function formatValue(metric: MetricResult, variant: VariantResult): string {
  if (analysedAsRate(metric)) return formatRate(variant.mean, variant.sample_size);
  // A mean (welch_t_test). Units arrive with the mean analysis; until then a
  // plain number, never a currency or "per user".
  if (!isFiniteNumber(variant.mean) || variant.sample_size <= 0) return NOT_ENOUGH_DATA;
  return variant.mean.toFixed(2);
}

/** Sort key for a possibly non-finite number: unknowns sort first ascending. */
function sortable(v: number | null | undefined): number {
  return isFiniteNumber(v) ? v : -Infinity;
}

/**
 * Whether this row shows an adjusted p-value: a correction applies and the
 * metric has two or more treatments with a p-value (#580). With one, the
 * correction changes nothing, so the p-value is shown plainly.
 */
function showsAdjusted(
  variant: VariantResult,
  comparisons: number,
  correctionMethod?: CorrectionMethod,
): boolean {
  return (
    correctionLabel(correctionMethod) !== null &&
    comparisons >= 2 &&
    isFiniteNumber(variant.adjusted_p_value)
  );
}

function PValueCell({
  variant,
  comparisons,
  correctionMethod,
}: {
  variant: VariantResult;
  comparisons: number;
  correctionMethod?: CorrectionMethod;
}) {
  if (variant.is_control) return <>—</>;
  const raw = variant.p_value;

  if (showsAdjusted(variant, comparisons, correctionMethod)) {
    return (
      <span className="flex flex-col">
        <span>{(variant.adjusted_p_value as number).toFixed(4)}</span>
        {isFiniteNumber(raw) && (
          <span className="text-xs text-slate-600">unadjusted {raw.toFixed(4)}</span>
        )}
      </span>
    );
  }
  if (correctionLabel(correctionMethod) !== null) {
    // One comparison on this metric: the correction leaves the p-value as it
    // is, so it is shown once, with no label. It is the value the badge uses.
    const decisive = decisivePValue(variant);
    if (!isFiniteNumber(decisive)) return <>{NOT_ENOUGH_DATA}</>;
    return <>{decisive.toFixed(4)}</>;
  }
  if (!isFiniteNumber(raw)) return <>{NOT_ENOUGH_DATA}</>;
  // No correction: the engine decided significance from this p, and the
  // label says it is not adjusted.
  return (
    <span className="flex flex-col">
      <span>{raw.toFixed(4)}</span>
      <span className="text-xs text-slate-600">unadjusted</span>
    </span>
  );
}

/**
 * The footnote under a table of adjusted p-values. "k" is the number of
 * treatments compared with the control on each metric that has two or more.
 */
export function adjustedFootnote(
  ks: number[],
  correctionMethod: CorrectionMethod,
  confidenceLevel: number,
): string {
  const distinct = Array.from(new Set(ks)).sort((a, b) => a - b);
  const k = distinct.length === 1 ? `${distinct[0]}` : `up to ${distinct[distinct.length - 1]}`;
  return (
    `Adjusted p-values account for comparing ${k} variants with the control on each metric ` +
    `(${correctionName(correctionMethod)}). A variant is significant when its adjusted p-value ` +
    `is below ${formatAlpha(confidenceLevel)}. Why is it higher than the unadjusted one? ` +
    'Testing several variants at once raises the chance that one looks like a winner by luck; ' +
    'the adjustment raises each p-value to keep that chance in check.'
  );
}

function columns(adjusted: boolean): [SortKey, string][] {
  return [
    ['metric', 'Metric'],
    ['variant', 'Variant'],
    ['sample_size', 'Sample Size'],
    ['mean', 'Value'],
    // "Change", not "Improvement": a significant rise is worse on a metric
    // where lower is better. The sort stays on the signed change.
    ['improvement', 'Change'],
    ['p_value', adjusted ? 'Adjusted p-value' : 'p-value'],
  ];
}

interface FlatRow {
  metric: MetricResult;
  variant: VariantResult;
}

export function MetricComparisonTable({
  metrics,
  confidenceLevel,
  correctionMethod,
}: MetricComparisonTableProps) {
  const [sortKey, setSortKey] = useState<SortKey>('metric');
  const [sortDir, setSortDir] = useState<SortDir>('asc');

  if (!metrics.length) {
    return (
      <p className="text-slate-500 text-sm" data-testid="no-metrics">
        No metrics available
      </p>
    );
  }

  const correction = correctionLabel(correctionMethod);
  // Treatments with a p-value, per metric: a correction changes a p-value
  // only where there are two or more.
  const comparisons = new Map(metrics.map((m) => [m.metric_id, comparisonsOf(m)]));
  const correctedKs =
    correction !== null ? metrics.map((m) => comparisonsOf(m)).filter((k) => k >= 2) : [];
  const adjustedHeader = correctedKs.length > 0;

  const rows: FlatRow[] = metrics.flatMap((m) =>
    m.variants.map((v) => ({ metric: m, variant: v }))
  );

  const ordered = [...rows].sort((a, b) => {
    let cmp = 0;
    switch (sortKey) {
      case 'metric':
        cmp = a.metric.metric_name.localeCompare(b.metric.metric_name);
        break;
      case 'variant':
        cmp = a.variant.variant_name.localeCompare(b.variant.variant_name);
        break;
      case 'sample_size':
        cmp = a.variant.sample_size - b.variant.sample_size;
        break;
      case 'mean':
        cmp = sortable(a.variant.mean) - sortable(b.variant.mean);
        break;
      case 'improvement':
        cmp =
          sortable(a.variant.relative_improvement_pct) -
          sortable(b.variant.relative_improvement_pct);
        break;
      case 'p_value': {
        const pa = decisivePValue(a.variant);
        const pb = decisivePValue(b.variant);
        cmp = (isFiniteNumber(pa) ? pa : 1) - (isFiniteNumber(pb) ? pb : 1);
        break;
      }
    }
    if (Number.isNaN(cmp)) cmp = 0;
    return sortDir === 'asc' ? cmp : -cmp;
  });

  function handleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  }

  function ariaSort(key: SortKey): 'ascending' | 'descending' | 'none' {
    if (sortKey !== key) return 'none';
    return sortDir === 'asc' ? 'ascending' : 'descending';
  }

  function sortIcon(key: SortKey) {
    if (sortKey !== key) return '↕';
    return sortDir === 'asc' ? '↑' : '↓';
  }

  return (
    <div className="overflow-x-auto" data-testid="metric-comparison-table">
      <table className="min-w-full divide-y divide-slate-200 text-sm">
        <thead className="bg-slate-50">
          <tr>
            {columns(adjustedHeader).map(([key, label]) => (
              <th
                key={key}
                scope="col"
                className="px-4 py-3 text-left font-medium text-slate-600"
                aria-sort={ariaSort(key)}
              >
                <button
                  type="button"
                  onClick={() => handleSort(key)}
                  className="inline-flex items-center gap-1 rounded hover:text-slate-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
                >
                  {label}
                  <span aria-hidden="true">{sortIcon(key)}</span>
                </button>
              </th>
            ))}
            <th scope="col" className="px-4 py-3 text-left font-medium text-slate-600">
              Significance
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 bg-white">
          {ordered.map((row, i) => {
            const { metric, variant } = row;
            const decisive = decisivePValue(variant);
            const k = comparisons.get(metric.metric_id) ?? 0;
            const adjustedShown = showsAdjusted(variant, k, correctionMethod);
            const verdict = changeVerdict(metric, variant);
            return (
              <tr
                key={`${metric.metric_id}-${variant.variant_id}`}
                className={`${i % 2 === 0 ? '' : 'bg-slate-50/50'} ${
                  metric.is_primary ? 'ring-1 ring-inset ring-blue-100' : ''
                }`}
              >
                <td className="px-4 py-3 font-medium text-slate-800">
                  {metric.metric_name}
                  {metric.is_primary && (
                    <span className="ml-1.5 text-xs text-blue-700 font-normal">primary</span>
                  )}
                  {metricLowerIsBetter(metric) === true && (
                    <span className="ml-1.5 text-xs text-slate-600 font-normal">lower is better</span>
                  )}
                </td>
                <td className="px-4 py-3 text-slate-700">
                  {variant.variant_name}
                  {variant.is_control && (
                    <span className="ml-1.5 text-xs text-slate-600">(control)</span>
                  )}
                </td>
                <td className="px-4 py-3 text-slate-700">
                  {variant.sample_size.toLocaleString()}
                </td>
                <td className="px-4 py-3 text-slate-700">
                  <span className="flex flex-col">
                    <span>{formatValue(metric, variant)}</span>
                    {metric.metric_type !== 'conversion' && (
                      <span className="text-xs text-slate-600">
                        {analysedAsRate(metric) ? rateDescription(metric) : 'mean'}
                      </span>
                    )}
                  </span>
                </td>
                <td className={`px-4 py-3 font-medium ${changeClass(verdict, variant)}`}>
                  <span>{formatImprovement(variant.relative_improvement_pct, variant.is_control)}</span>
                  {verdict && <span> · {verdict}</span>}
                </td>
                <td className="px-4 py-3 text-slate-700">
                  <PValueCell variant={variant} comparisons={k} correctionMethod={correctionMethod} />
                </td>
                <td className="px-4 py-3">
                  <StatisticalBadge
                    pValue={variant.is_control ? null : isFiniteNumber(decisive) ? decisive : NaN}
                    confidenceLevel={confidenceLevel}
                    isSignificant={variant.is_significant}
                    pLabel={adjustedShown ? 'adjusted p' : 'p'}
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {adjustedHeader && correctionMethod && (
        <p className="mt-3 text-xs text-slate-600" data-testid="adjusted-p-footnote">
          {adjustedFootnote(correctedKs, correctionMethod, confidenceLevel)}
        </p>
      )}
    </div>
  );
}
