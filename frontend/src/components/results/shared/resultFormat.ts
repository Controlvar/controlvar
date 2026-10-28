import { CorrectionMethod, MetricResult, VariantResult } from '@/types/results';
import { formatConfidence } from '@/components/results/shared/analysisSettings';

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
 * True when the engine analysed this metric as a mean: a revenue metric's
 * value per assigned user, compared with Welch's t-test. The response's
 * `analysis` says so. A result without it (from a server older than the
 * field, or cached before it) is read from the test it ran: `welch_t_test`
 * is a mean, anything else a share. `analysis` comes first because before
 * the first purchase no treatment has a test to read.
 */
export function analysedAsMean(metric: MetricResult): boolean {
  if (metric.analysis === 'mean') return true;
  if (metric.analysis === 'proportion') return false;
  if (metric.metric_type === 'conversion') return false;
  return metric.variants.some((v) => v.statistical_test_used === 'welch_t_test');
}

/**
 * True when the engine analysed this metric as a share of users with an event
 * (a conversion test), whatever its declared type: every metric but a revenue
 * metric analysed as a mean (`analysedAsMean`).
 */
export function analysedAsRate(metric: MetricResult): boolean {
  return !analysedAsMean(metric);
}

/** What the value in a metric's rate column is. */
export function rateDescription(metric: MetricResult): string {
  if (metric.metric_type === 'conversion') return 'conversion rate';
  return 'share of users with at least one event';
}

/** What the value of a metric analysed as a mean is. */
export function meanDescription(metric: MetricResult): string {
  return metric.metric_type === 'revenue' ? 'revenue per user' : 'mean';
}

/** True for a revenue metric whose value is revenue per user. */
export function isRevenuePerUser(metric: MetricResult): boolean {
  return metric.metric_type === 'revenue' && analysedAsMean(metric);
}

/**
 * An amount per user, to 2 places, with a real minus sign and no currency:
 * Controlvar reads no currency, so none is shown.
 */
export function formatAmount(v: number): string {
  const fixed = Math.abs(v).toFixed(2);
  return v < 0 && Number(fixed) !== 0 ? `−${fixed}` : fixed;
}

/** A signed amount: "+1.18", "−0.47", and "0.00" for anything that rounds to 0. */
export function formatSignedAmount(v: number): string {
  const amount = formatAmount(v);
  return v > 0 && amount !== '0.00' ? `+${amount}` : amount;
}

/**
 * A treatment's difference from the control per user, with its interval:
 * "+1.18 per user (95% CI −0.47 to +2.84)". The level is the response's.
 * `unadjusted` says the interval is not adjusted for multiple comparisons,
 * shown when the row's p-value is: the badge then reads the adjusted p, and
 * an interval that excludes 0 can sit on a row that is not significant.
 * Null when there is no difference to show.
 */
export function formatDifferencePerUser(
  variant: VariantResult,
  confidenceLevel: number,
  unadjusted: boolean,
): string | null {
  const diff = variant.absolute_difference;
  if (variant.is_control || !isFiniteNumber(diff)) return null;
  const text = `${formatSignedAmount(diff)} per user`;
  const ci = variant.absolute_difference_ci;
  if (!ci || !isFiniteNumber(ci[0]) || !isFiniteNumber(ci[1]) || !isFiniteNumber(confidenceLevel)) {
    return text;
  }
  const level = `${formatConfidence(confidenceLevel)} CI`;
  const label = unadjusted ? `${level}, unadjusted,` : level;
  return `${text} (${label} ${formatSignedAmount(ci[0])} to ${formatSignedAmount(ci[1])})`;
}

/**
 * The users who sent a revenue metric's event when every user's total value
 * is 0 in every variant: the amounts were not sent in the event's `value`
 * (most often they went into its properties). Null otherwise, and for a
 * metric that is not revenue per user.
 */
export function buyersWithNoValue(metric: MetricResult): number | null {
  if (!isRevenuePerUser(metric) || metric.variants.length === 0) return null;
  const allZero = metric.variants.every(
    (v) => v.mean === 0 && (v.std_dev === null || v.std_dev === undefined || v.std_dev === 0),
  );
  if (!allZero) return null;
  const buyers = metric.variants.reduce(
    (sum, v) => sum + (isFiniteNumber(v.conversions) ? v.conversions : 0),
    0,
  );
  return buyers > 0 ? buyers : null;
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
