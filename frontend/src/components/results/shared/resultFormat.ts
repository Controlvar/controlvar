import { CorrectionMethod, MetricResult, VariantResult } from '@/types/results';

/** Shown wherever a number is missing, NaN or infinite. Never "NaN", "∞" or "0.00%". */
export const NOT_ENOUGH_DATA = 'Not enough data to estimate';

export function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

/**
 * The p-value the engine decided significance from: the adjusted one when a
 * correction applied, otherwise the raw one. `is_significant`, the row
 * colour and the recommendation all come from this same value.
 */
export function decisivePValue(v: VariantResult): number | null | undefined {
  return v.adjusted_p_value !== null && v.adjusted_p_value !== undefined
    ? v.adjusted_p_value
    : v.p_value;
}

/** Short display name of a correction; null when no correction applies. */
export function correctionLabel(method: CorrectionMethod | undefined): string | null {
  switch (method) {
    case 'bonferroni':
      return 'Bonferroni';
    case 'benjamini_hochberg':
      return 'BH';
    default:
      return null;
  }
}

/**
 * True when the engine analysed this metric as a share of users with an event
 * (a conversion test), whatever its declared type. Today every metric is
 * analysed that way (the per-variant test is fisher_exact); a revenue, count
 * or duration metric is only a mean once its test is welch_t_test.
 */
export function analysedAsRate(metric: MetricResult): boolean {
  if (metric.metric_type === 'conversion') return true;
  return !metric.variants.some((v) => v.statistical_test_used === 'welch_t_test');
}

/** What the value in a metric's rate column is. */
export function rateDescription(metric: MetricResult): string {
  if (metric.metric_type === 'conversion') return 'conversion rate';
  return 'share of users with at least one event';
}

/** A rate in [0, 1] as a percentage; NOT_ENOUGH_DATA when it cannot be estimated. */
export function formatRate(v: number | null | undefined, sampleSize: number): string {
  if (!isFiniteNumber(v) || sampleSize <= 0) return NOT_ENOUGH_DATA;
  return `${(v * 100).toFixed(2)}%`;
}

/**
 * The metric's direction as the response gives it: true when a decrease is
 * the good direction (errors, latency, uninstalls). Undefined when the
 * response does not carry it, as from a server older than the direction:
 * the table then does not call a change better or worse.
 */
export function metricLowerIsBetter(metric: MetricResult): boolean | undefined {
  return typeof metric.lower_is_better === 'boolean' ? metric.lower_is_better : undefined;
}

/**
 * Whether a treatment's change is better or worse in the metric's direction.
 * Only a significant change gets a verdict, from the same `is_significant` as
 * the badge and the recommendation; null for the control, a change that is
 * not significant, not finite or zero, and a response without the direction.
 */
export function changeVerdict(
  metric: MetricResult,
  variant: VariantResult,
): 'better' | 'worse' | null {
  const lowerIsBetter = metricLowerIsBetter(metric);
  if (lowerIsBetter === undefined || variant.is_control || !variant.is_significant) return null;
  const pct = variant.relative_improvement_pct;
  if (!isFiniteNumber(pct) || pct === 0) return null;
  const fell = pct < 0;
  return fell === lowerIsBetter ? 'better' : 'worse';
}

/** A signed percentage with a real minus sign, so the direction is in the text. */
export function formatSignedPct(v: number, digits = 1): string {
  if (v > 0) return `+${v.toFixed(digits)}%`;
  if (v < 0) return `−${Math.abs(v).toFixed(digits)}%`;
  return `${v.toFixed(digits)}%`;
}
