import React, { useState } from 'react';
import { CorrectionMethod, MetricResult, VariantResult } from '@/types/results';
import { StatisticalBadge } from '@/components/results/shared/StatisticalBadge';
import {
  DifferenceLine,
  ImpactLine,
  NOT_ENOUGH_DATA,
  RelativeLine,
  analysedAsMean,
  analysedAsRate,
  buyersWithNoValue,
  carriesImpact,
  changeText,
  changeVerdict,
  correctionLabel,
  decisivePValue,
  differenceLine,
  formatAmount,
  formatRate,
  impactLine,
  isFiniteNumber,
  meanDescription,
  metricLowerIsBetter,
  rateDescription,
  reconciliationNote,
  relativeLine,
  reportsImpact,
  showsAdjusted,
} from '@/components/results/shared/resultFormat';
import { IntervalText, LeadWithInterval } from '@/components/results/shared/IntervalText';
import {
  comparisonsOf,
  correctionName,
  formatAlpha,
  formatConfidence,
} from '@/components/results/shared/analysisSettings';

interface MetricComparisonTableProps {
  metrics: MetricResult[];
  confidenceLevel: number;
  /** The response's correction_method: the correction the p-values were adjusted with. */
  correctionMethod?: CorrectionMethod;
  /**
   * Each metric's event name by metric id, from the experiment, for the note
   * on a revenue, duration or custom metric whose events carried no value.
   * Without it the note says "its event".
   */
  eventNames?: Record<string, string>;
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

function formatValue(metric: MetricResult, variant: VariantResult): string {
  if (analysedAsRate(metric)) return formatRate(variant.mean, variant.sample_size);
  // A mean: a plain amount per user, never a currency (none is read).
  if (!isFiniteNumber(variant.mean) || variant.sample_size <= 0) return NOT_ENOUGH_DATA;
  return formatAmount(variant.mean);
}

/** The words under a revenue metric's rows, so "per user" is never a guess. */
function RevenueFootnote() {
  return (
    <p className="mt-3 text-xs text-slate-600" data-testid="revenue-per-user-footnote">
      Revenue per user: each user&apos;s total <code>value</code> on the metric&apos;s event,
      averaged over every user assigned to the variant; a user with no such event counts as 0.
      Values are added as sent: no currency is read or converted, and no order is capped.
    </p>
  );
}

/** Under a count metric's rows: what "events per user" counts. */
function CountFootnote() {
  return (
    <p className="mt-3 text-xs text-slate-600" data-testid="count-per-user-footnote">
      Events per user: how many times each user sent the metric&apos;s event, averaged over every
      user assigned to the variant; a user with none counts as 0. The events&apos;{' '}
      <code>value</code> is not read.
    </p>
  );
}

/**
 * Under a duration metric's rows. A duration is time per assigned user, so a
 * variant whose users send the event less often reads as less time: the
 * footnote says so, as the metrics editor does.
 */
function DurationFootnote() {
  return (
    <p className="mt-3 text-xs text-slate-600" data-testid="duration-per-user-footnote">
      Duration per user: each user&apos;s total <code>value</code> on the metric&apos;s event,
      averaged over every user assigned to the variant; a user with none counts as 0, so a variant
      whose users send the event less often reads as less time. Values are added in the unit they
      were sent in: no unit is read or converted, and no value is capped.
    </p>
  );
}

/** Under a custom metric's rows. */
function CustomFootnote() {
  return (
    <p className="mt-3 text-xs text-slate-600" data-testid="custom-per-user-footnote">
      Value per user: each user&apos;s total <code>value</code> on the metric&apos;s event,
      averaged over every user assigned to the variant; a user with none counts as 0. Values are
      added as sent, and no value is capped.
    </p>
  );
}

/** Each footnote, by the metric type it explains. */
const MEAN_FOOTNOTES: Array<[string, () => React.ReactElement]> = [
  ['revenue', RevenueFootnote],
  ['count', CountFootnote],
  ['duration', DurationFootnote],
  ['custom', CustomFootnote],
];

/** What the note asks the integrator to put in `value`, by metric type. */
function valueNoun(metricType: string): string {
  if (metricType === 'duration') return 'duration';
  if (metricType === 'custom') return 'number';
  return 'amount';
}

/**
 * Users sent a revenue, duration or custom metric's event, yet every user's
 * total is 0: the numbers did not arrive in the event's `value`. Says so,
 * rather than leaving a row of zeros to be read as a measured 0. Never for a
 * count, which counts events and does not read `value`.
 */
function NoValueNote({
  metric,
  buyers,
  eventName,
}: {
  metric: MetricResult;
  buyers: number;
  eventName?: string;
}) {
  const who = buyers === 1 ? '1 user' : `${buyers.toLocaleString()} users`;
  return (
    <p className="mt-2 text-xs text-amber-800" data-testid="no-value-note">
      {metric.metric_name}: {who} sent {eventName ? <code>{eventName}</code> : 'its event'}, and
      every user&apos;s total <code>value</code> is 0. Send the {valueNoun(metric.metric_type)} in
      the event&apos;s <code>value</code> field, not in its properties.
    </p>
  );
}

/**
 * Under a table whose server reports the intervals on the change (#1496),
 * when any interval is shown: what they are, that they are not adjusted, and
 * that a percentage interval can be very wide.
 */
export function intervalFootnote(confidenceLevel: number): string {
  return (
    `Intervals are ${formatConfidence(confidenceLevel)} confidence intervals (CI) of the ` +
    'difference from the control. They are not adjusted for comparing several variants with ' +
    'the control; significance is, so the two can disagree, and the row says so when they do. ' +
    'A percentage interval can be very wide when counts are small.'
  );
}

/** Under a table with the Impact column: what the impact is. */
export const IMPACT_FOOTNOTE =
  "Impact: the difference per user times the variant's users (its Sample Size): how many more " +
  "converting users, events or value the variant's users had than as many users at the " +
  "control's rate or mean. Users and events are rounded to whole numbers. Values are added as " +
  'sent; no currency or unit is read.';

/** Sort key for a possibly non-finite number: unknowns sort first ascending. */
function sortable(v: number | null | undefined): number {
  return isFiniteNumber(v) ? v : -Infinity;
}

/** The change cell's second line: the relative interval, or why a mean has none. */
function RelativeIntervalLine({ line }: { line: RelativeLine }) {
  if (line.kind === 'unbounded') {
    return (
      <p className="text-xs font-normal text-slate-600" data-testid="change-no-interval">
        {line.text}
      </p>
    );
  }
  return (
    <span className="block text-xs font-normal text-slate-600" data-testid="change-interval">
      {' '}
      <IntervalText interval={line.interval} />
    </span>
  );
}

function ImpactCell({ variant, line }: { variant: VariantResult; line: ImpactLine | null }) {
  if (variant.is_control) return <>—</>;
  if (!line) return <span data-testid="impact">{NOT_ENOUGH_DATA}</span>;
  return (
    <span data-testid="impact">
      <LeadWithInterval lead={`${line.value} ${line.noun}`} interval={line.interval} />
    </span>
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

/** What one row shows, worked out once so the footnotes can ask what is on the page. */
interface RowView extends FlatRow {
  decisive: number | null | undefined;
  comparisons: number;
  adjustedShown: boolean;
  verdict: 'better' | 'worse' | null;
  change: string;
  relative: RelativeLine | null;
  difference: DifferenceLine | null;
  impact: ImpactLine | null;
  note: string | null;
}

function rowView(
  row: FlatRow,
  comparisons: number,
  confidenceLevel: number,
  correctionMethod: CorrectionMethod | undefined,
): RowView {
  const { metric, variant } = row;
  const adjustedShown = showsAdjusted(variant, comparisons, correctionMethod);
  return {
    ...row,
    decisive: decisivePValue(variant),
    comparisons,
    adjustedShown,
    verdict: changeVerdict(metric, variant),
    change: changeText(metric, variant, confidenceLevel),
    relative: relativeLine(metric, variant, confidenceLevel, adjustedShown),
    difference: differenceLine(metric, variant, confidenceLevel, adjustedShown),
    impact: impactLine(metric, variant, confidenceLevel, adjustedShown),
    // The notes #1496 added appear only where the server reports the
    // impact; a response from an older server renders as it did before.
    note: carriesImpact(variant)
      ? reconciliationNote(variant, confidenceLevel, adjustedShown, correctionMethod, comparisons)
      : null,
  };
}

/** True when the row shows at least one interval: the interval footnote then explains them. */
function showsInterval(view: RowView): boolean {
  return (
    view.relative?.kind === 'interval' ||
    Boolean(view.difference?.interval) ||
    Boolean(view.impact?.interval)
  );
}

export function MetricComparisonTable({
  metrics,
  confidenceLevel,
  correctionMethod,
  eventNames,
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

  // The Impact column, only when the server reports the impact (#1496): a
  // response from an older server has no such field, and no column.
  const impactShown = reportsImpact(metrics);
  const views = ordered.map((row) =>
    rowView(row, comparisons.get(row.metric.metric_id) ?? 0, confidenceLevel, correctionMethod),
  );
  const intervalNoted = views.some((v) => carriesImpact(v.variant) && showsInterval(v));

  return (
    <div className="overflow-x-auto" data-testid="metric-comparison-table">
      <table className="min-w-full divide-y divide-slate-200 text-sm">
        <thead className="bg-slate-50">
          <tr>
            {columns(adjustedHeader).map(([key, label]) => (
              <React.Fragment key={key}>
                <th
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
                {/* Not sortable: impacts of metrics in different units do not compare. */}
                {key === 'improvement' && impactShown && (
                  <th scope="col" className="px-4 py-3 text-left font-medium text-slate-600">
                    Impact
                  </th>
                )}
              </React.Fragment>
            ))}
            <th scope="col" className="px-4 py-3 text-left font-medium text-slate-600">
              Significance
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 bg-white">
          {views.map((view, i) => {
            const { metric, variant, decisive, adjustedShown, verdict, relative, difference } =
              view;
            const k = view.comparisons;
            return (
              <tr
                key={`${metric.metric_id}-${variant.variant_id}`}
                data-metric-id={metric.metric_id}
                data-variant-id={variant.variant_id}
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
                        {analysedAsRate(metric) ? rateDescription(metric) : meanDescription(metric)}
                      </span>
                    )}
                  </span>
                </td>
                <td className={`px-4 py-3 font-medium ${changeClass(verdict, variant)}`}>
                  <span data-testid="change">{view.change}</span>
                  {verdict && <span> · {verdict}</span>}
                  {relative && <RelativeIntervalLine line={relative} />}
                  {difference && (
                    <span
                      className="block text-xs font-normal text-slate-600"
                      data-testid="change-difference"
                    >
                      {' '}
                      <LeadWithInterval lead={difference.lead} interval={difference.interval} />
                    </span>
                  )}
                </td>
                {impactShown && (
                  <td className="px-4 py-3 text-slate-700" data-testid="impact-cell">
                    <ImpactCell variant={variant} line={view.impact} />
                  </td>
                )}
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
                  {view.note && (
                    <p className="mt-1 text-xs text-slate-600" data-testid="reconciliation-note">
                      {view.note}
                    </p>
                  )}
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
      {intervalNoted && (
        <p className="mt-3 text-xs text-slate-600" data-testid="interval-footnote">
          {intervalFootnote(confidenceLevel)}
        </p>
      )}
      {impactShown && (
        <p className="mt-3 text-xs text-slate-600" data-testid="impact-footnote">
          {IMPACT_FOOTNOTE}
        </p>
      )}
      {MEAN_FOOTNOTES.map(([type, Footnote]) =>
        metrics.some((m) => m.metric_type === type && analysedAsMean(m)) ? (
          <Footnote key={type} />
        ) : null,
      )}
      {metrics.map((m) => {
        const buyers = buyersWithNoValue(m);
        return buyers === null ? null : (
          <NoValueNote
            key={m.metric_id}
            metric={m}
            buyers={buyers}
            eventName={eventNames?.[m.metric_id]}
          />
        );
      })}
    </div>
  );
}
