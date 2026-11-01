import { CorrectionMethod, MetricResult, StatisticalTestUsed, VariantResult } from '@/types/results';
import {
  correctionName,
  formatAlpha,
  formatConfidence,
} from '@/components/results/shared/analysisSettings';

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
 * True when the engine analysed this metric as a mean: a revenue, count,
 * duration or custom metric's value per assigned user, compared with Welch's
 * t-test. The response's `analysis` says so, so the page reads what this
 * server did, never a list of types. A result without it, from a server older
 * than the field, is read from the test it ran: `welch_t_test` is a mean,
 * anything else a share. `analysis` comes first because before the first
 * event no treatment has a test to read.
 */
export function analysedAsMean(metric: MetricResult): boolean {
  if (metric.analysis === 'mean') return true;
  if (metric.analysis === 'proportion') return false;
  if (metric.metric_type === 'conversion') return false;
  return metric.variants.some((v) => v.statistical_test_used === 'welch_t_test');
}

/**
 * True when the engine analysed this metric as a share of users with an event
 * (a conversion test), whatever its declared type: every metric not analysed
 * as a mean (`analysedAsMean`).
 */
export function analysedAsRate(metric: MetricResult): boolean {
  return !analysedAsMean(metric);
}

/** What the value in a metric's rate column is. */
export function rateDescription(metric: MetricResult): string {
  if (metric.metric_type === 'conversion') return 'conversion rate';
  return 'share of users with at least one event';
}

/**
 * What a mean of this metric type is per user: "revenue per user", "events
 * per user" (a count: how many times each user sent the event), "duration per
 * user", "value per user" (custom), and "mean per user" for a type this
 * dashboard does not know.
 */
export function meanLabelForType(metricType: string): string {
  switch (metricType) {
    case 'revenue':
      return 'revenue per user';
    case 'count':
      return 'events per user';
    case 'duration':
      return 'duration per user';
    case 'custom':
      return 'value per user';
    default:
      return 'mean per user';
  }
}

/** What the value of a metric analysed as a mean is. */
export function meanDescription(metric: MetricResult): string {
  return meanLabelForType(metric.metric_type);
}

/** The same label with a capital, for a chart tooltip. */
export function capitalised(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * True for a metric analysed as a mean whose value is a total of its events'
 * `value`: revenue, duration and custom. Not a count, which counts its events
 * and does not read `value`.
 */
export function addsUpValues(metric: MetricResult): boolean {
  return analysedAsMean(metric) && metric.metric_type !== 'count';
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
 * Two finite numbers, or null for anything else: null, undefined, a
 * non-array, a pair holding NaN or ±Infinity. An interval that is not a
 * finite pair is not drawn, so "Infinity", "NaN" and "undefined" never reach
 * the page.
 */
export function finitePair(v: unknown): [number, number] | null {
  if (!Array.isArray(v) || v.length !== 2) return null;
  const [lower, upper] = v as unknown[];
  return isFiniteNumber(lower) && isFiniteNumber(upper) ? [lower, upper] : null;
}

/**
 * An interval as the page writes it, in pieces so that the label can be
 * marked up for a screen reader ("95% CI" is read as "95% confidence
 * interval") and each bound kept on one line. `unadjusted` is true exactly
 * when the row's p-value is shown adjusted (`showsAdjusted`): the interval is
 * not, and the label says so.
 */
export interface IntervalParts {
  /** The response's level, "95%". */
  level: string;
  unadjusted: boolean;
  lower: string;
  upper: string;
}

function interval(
  confidenceLevel: number,
  unadjusted: boolean,
  lower: string,
  upper: string,
): IntervalParts {
  return { level: formatConfidence(confidenceLevel), unadjusted, lower, upper };
}

/**
 * Whether this row shows an adjusted p-value: a correction applies and the
 * metric has two or more treatments with a p-value (#580). With one, the
 * correction changes nothing, so the p-value is shown plainly. Every interval
 * of such a row is marked "unadjusted".
 */
export function showsAdjusted(
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

/** The metric's control variant, if the response has one. */
export function controlOf(metric: MetricResult): VariantResult | undefined {
  return metric.variants.find((v) => v.is_control);
}

/**
 * True when this treatment, or the control it is compared with, has no
 * users: there is nothing to compare, whatever number the server put in the
 * row (a proportion's empty treatment arm comes back as a -100% change).
 */
export function armWithoutUsers(metric: MetricResult, variant: VariantResult): boolean {
  const control = controlOf(metric);
  return !(variant.sample_size > 0) || (control !== undefined && !(control.sample_size > 0));
}

/**
 * True when the server reports a treatment's interval on the change and its
 * impact (#1496): the variant carries `incremental_impact`, null included. A
 * server older than the fields leaves it out, and the page then renders as
 * it did before them.
 */
export function carriesImpact(variant: VariantResult): boolean {
  return variant.incremental_impact !== undefined;
}

/** True when some variant of some metric carries the impact: the Impact column is shown. */
export function reportsImpact(metrics: MetricResult[]): boolean {
  return metrics.some((m) => m.variants.some(carriesImpact));
}

/**
 * A difference from the control and its interval, in two pieces: the lead
 * ("+1.18 per user", "+1.20 percentage points") and the interval, null when
 * there is none to show.
 */
export interface DifferenceLine {
  lead: string;
  interval: IntervalParts | null;
}

function differenceOf(
  variant: VariantResult,
  confidenceLevel: number,
  unadjusted: boolean,
  scale: number,
  unit: string,
): DifferenceLine | null {
  const diff = variant.absolute_difference;
  if (variant.is_control || !isFiniteNumber(diff)) return null;
  const amount = formatSignedAmount(diff * scale);
  const lead = `${amount} ${unit}`;
  const ci = finitePair(variant.absolute_difference_ci);
  if (!ci) return amount === '0.00' ? null : { lead, interval: null };
  if (!isFiniteNumber(confidenceLevel)) return { lead, interval: null };
  return {
    lead,
    interval: interval(
      confidenceLevel,
      unadjusted,
      formatSignedAmount(ci[0] * scale),
      formatSignedAmount(ci[1] * scale),
    ),
  };
}

/**
 * A treatment's difference from the control, the change cell's last line.
 * For a metric analysed as a mean, per user: "+1.18 per user (95% CI −0.47
 * to +2.84)". For one analysed as a proportion, in percentage points (the
 * API's difference of shares times 100, to 2 places): "+1.20 percentage
 * points (95% CI +0.20 to +2.20)". The level is the response's.
 * Null when there is no difference to show: for the control, without a
 * finite difference (a proportion from a server older than #1496 has none),
 * when either arm has no users, and for a difference of 0.00 with no
 * interval. The API gives that when every user's total is the same, as before
 * the first purchase or when every total is 0; beside "Not enough data to
 * estimate" it would read as a measured 0.00.
 */
export function differenceLine(
  metric: MetricResult,
  variant: VariantResult,
  confidenceLevel: number,
  unadjusted: boolean,
): DifferenceLine | null {
  if (armWithoutUsers(metric, variant)) return null;
  return analysedAsMean(metric)
    ? differenceOf(variant, confidenceLevel, unadjusted, 1, 'per user')
    : differenceOf(variant, confidenceLevel, unadjusted, 100, 'percentage points');
}

/**
 * The largest relative bound written out in full. A bound beyond it is
 * written as a true statement about the server's number: "above
 * +1,000,000%" for an upper bound greater than 1,000,000, "below
 * −1,000,000%" for a lower bound less than −1,000,000. Exactly 1,000,000 is
 * written out. The API returns the exact number.
 */
export const PCT_BOUND_LIMIT = 1_000_000;

/**
 * A signed percentage with grouping, one decimal and a real minus sign:
 * "+26,001.9%", "−34.2%", "−100.0%", and "0.0%" for anything that rounds to
 * 0. Never an exponent: `toLocaleString` writes every digit.
 */
export function formatGroupedSignedPct(v: number): string {
  const text = Math.abs(v).toLocaleString('en-US', {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  });
  if (text === '0.0') return '0.0%';
  return `${v < 0 ? '−' : '+'}${text}%`;
}

/**
 * One bound of the relative change's interval. Formatted verbatim: no floor,
 * no cap at −100%, nothing clamped. "above" is only ever an upper bound and
 * "below" only a lower one; any other bound is written out.
 */
export function formatPctBound(v: number, side: 'lower' | 'upper'): string {
  if (side === 'upper' && v > PCT_BOUND_LIMIT) return 'above +1,000,000%';
  if (side === 'lower' && v < -PCT_BOUND_LIMIT) return 'below −1,000,000%';
  return formatGroupedSignedPct(v);
}

/**
 * The change cell's second line: the relative change's interval, or, for a
 * mean whose control is too uncertain around 0, the sentence saying why
 * there is none.
 */
export type RelativeLine =
  | { kind: 'interval'; interval: IntervalParts }
  | { kind: 'unbounded'; text: string };

/**
 * The relative change's interval line (#1496), or null for no line.
 *
 * - A finite `relative_improvement_pct_ci`: "95% CI +2.1% to +22.5%", each
 *   bound by `formatPctBound`.
 * - A mean with a finite `relative_improvement_pct`, the interval null and an
 *   interval on the difference: the server's Fieller guard (the control's
 *   mean plus or minus its margin reaches 0), so the sentence says that. Not
 *   for an interval that is null because the difference's is (no values
 *   vary, or an arm has fewer than 2 users), and not for a field that is
 *   absent (an older server).
 * - Never for a proportion: its interval is null only when the control had
 *   no conversions or an arm has no users, and line 1 says which.
 */
export function relativeLine(
  metric: MetricResult,
  variant: VariantResult,
  confidenceLevel: number,
  unadjusted: boolean,
): RelativeLine | null {
  if (variant.is_control || armWithoutUsers(metric, variant)) return null;
  if (!isFiniteNumber(confidenceLevel)) return null;
  const pair = finitePair(variant.relative_improvement_pct_ci);
  if (pair) {
    return {
      kind: 'interval',
      interval: interval(
        confidenceLevel,
        unadjusted,
        formatPctBound(pair[0], 'lower'),
        formatPctBound(pair[1], 'upper'),
      ),
    };
  }
  if (
    analysedAsMean(metric) &&
    isFiniteNumber(variant.relative_improvement_pct) &&
    variant.relative_improvement_pct_ci === null &&
    finitePair(variant.absolute_difference_ci) !== null
  ) {
    return {
      kind: 'unbounded',
      text:
        "No interval for the change: the control's average is too uncertain around 0 to " +
        `bound the relative change at the ${formatConfidence(confidenceLevel)} level.`,
    };
  }
  return null;
}

/** Line 1 of a proportion treatment whose control had no conversions. */
export const CONTROL_NO_CONVERSIONS = 'Not a percentage: the control had no conversions';
/** Line 1 of a mean treatment whose control's mean is 0. */
export const CONTROL_MEAN_ZERO = "Not a percentage: the control's mean is 0";

/**
 * The change cell's first line: the relative change ("+12.3%"), "—" for the
 * control, and otherwise why there is none. An arm with no users reads "Not
 * enough data to estimate". Where the server reports the impact, a relative
 * change that is undefined beside a difference that is not says why: the
 * control had no conversions (a proportion), or its mean is 0 (a mean).
 */
export function changeText(
  metric: MetricResult,
  variant: VariantResult,
  confidenceLevel: number,
): string {
  if (variant.is_control) return '—';
  if (armWithoutUsers(metric, variant)) return NOT_ENOUGH_DATA;
  const pct = variant.relative_improvement_pct;
  if (isFiniteNumber(pct)) return formatSignedPct(pct);
  const control = controlOf(metric);
  if (control && carriesImpact(variant)) {
    if (analysedAsMean(metric)) {
      if (control.mean === 0 && differenceLine(metric, variant, confidenceLevel, false)) {
        return CONTROL_MEAN_ZERO;
      }
    } else if (
      isFiniteNumber(variant.absolute_difference) &&
      (control.conversions === 0 ||
        ((control.conversions === null || control.conversions === undefined) && control.mean === 0))
    ) {
      return CONTROL_NO_CONVERSIONS;
    }
  }
  return NOT_ENOUGH_DATA;
}

/** A whole number of users or events: "+1,234", "−29", and "0" for anything that rounds to 0. */
function formatSignedWhole(v: number): string {
  const rounded = Math.round(Math.abs(v));
  if (rounded === 0) return '0';
  return `${v < 0 ? '−' : '+'}${rounded.toLocaleString('en-US')}`;
}

/** A value to 2 places with grouping, no currency: "+18,240.00", and "0" for anything that rounds to 0. */
function formatSignedValue(v: number): string {
  const text = Math.abs(v).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  if (text === '0.00') return '0';
  return `${v < 0 ? '−' : '+'}${text}`;
}

/**
 * What a metric's impact counts. A proportion counts users: "converting
 * users" for a conversion metric, "users who sent the event" otherwise. A
 * mean: a count metric's events, otherwise the total of the values sent, with
 * no currency or unit: "in revenue", "in duration", "in value".
 */
export function impactNoun(metric: MetricResult): string {
  if (analysedAsRate(metric)) {
    return metric.metric_type === 'conversion' ? 'converting users' : 'users who sent the event';
  }
  switch (metric.metric_type) {
    case 'count':
      return 'events';
    case 'revenue':
      return 'in revenue';
    case 'duration':
      return 'in duration';
    default:
      return 'in value';
  }
}

/** True when the impact is a number of users or events, written as a whole number. */
function impactIsWhole(metric: MetricResult): boolean {
  return analysedAsRate(metric) || metric.metric_type === 'count';
}

/** A treatment's impact: its number, what it counts, and its interval. */
export interface ImpactLine {
  value: string;
  noun: string;
  interval: IntervalParts | null;
}

/**
 * A treatment's incremental impact (#1496): the difference times its own
 * assigned users, never projected. Users and events are rounded to whole
 * numbers, values to 2 places, both with grouping; zero is "0". Null, which
 * the page writes as "Not enough data to estimate", for the control, a
 * missing or non-finite impact, an arm with no users, and an impact of 0 with
 * no interval (as for the difference: an unmeasured 0 is not shown as one).
 */
export function impactLine(
  metric: MetricResult,
  variant: VariantResult,
  confidenceLevel: number,
  unadjusted: boolean,
): ImpactLine | null {
  const impact = variant.incremental_impact;
  if (variant.is_control || !isFiniteNumber(impact) || armWithoutUsers(metric, variant)) {
    return null;
  }
  const format = impactIsWhole(metric) ? formatSignedWhole : formatSignedValue;
  const value = format(impact);
  const noun = impactNoun(metric);
  const ci = finitePair(variant.incremental_impact_ci);
  if (!ci || !isFiniteNumber(confidenceLevel)) {
    return value === '0' ? null : { value, noun, interval: null };
  }
  return {
    value,
    noun,
    interval: interval(confidenceLevel, unadjusted, format(ci[0]), format(ci[1])),
  };
}

/** The name of the test behind a p-value, for a sentence. */
function testName(test: StatisticalTestUsed | null | undefined): string {
  switch (test) {
    case 'fisher_exact':
      return "Fisher's exact test";
    case 'welch_t_test':
      return "Welch's t-test";
    case 'z_test_proportions':
      return 'the z-test for two proportions';
    default:
      return 'the test';
  }
}

/**
 * A p-value for a sentence that says which side of the threshold it is on:
 * the p-value column's 4 decimals, and more when the rounded text would not
 * be strictly on that side. A note exists only near the threshold, so a p of
 * 0.04981 that is "below 0.05" reads "0.0498", and a p of 0.0099972 that is
 * "below 0.01" reads "0.009997", never "0.0100". `alpha` is the threshold as
 * the sentence writes it (`formatAlpha`).
 */
export function formatPOnSide(p: number, alpha: number, below: boolean): string {
  for (let digits = 4; digits < 12; digits += 1) {
    const text = p.toFixed(digits);
    const shown = Number(text);
    if (below ? shown < alpha : shown >= alpha) return text;
  }
  return p.toFixed(12);
}

/**
 * The sentence under a row's badge when its interval on the difference and
 * its significance disagree: the interval is not adjusted and significance
 * is, or, near the threshold, Fisher's exact test and Newcombe's interval
 * differ. Null when they agree, for the control, and without a finite
 * interval or p-value. Each p has 4 decimals, more when needed to stay on
 * the side of the threshold the sentence states (`formatPOnSide`).
 *
 * - Shown adjusted, the interval excludes 0, not significant: "Not
 *   significant after the Benjamini-Hochberg correction for 2 comparisons
 *   (adjusted p 0.0713), although the unadjusted interval excludes 0."
 * - Otherwise, the interval excludes 0, not significant: "Not significant:
 *   Fisher's exact test gives p 0.0573, not below 0.05, although the interval
 *   excludes 0. The test decides; near the threshold the two can differ."
 * - The interval reaches 0, significant: "Significant: Fisher's exact test
 *   gives p 0.0492, below 0.05, although the interval reaches 0. The test
 *   decides; near the threshold the two can differ." The p is the one the
 *   test gave, unadjusted.
 */
export function reconciliationNote(
  variant: VariantResult,
  confidenceLevel: number,
  adjustedShown: boolean,
  correctionMethod: CorrectionMethod | undefined,
  comparisons: number,
): string | null {
  if (variant.is_control) return null;
  const ci = finitePair(variant.absolute_difference_ci);
  if (!ci || !isFiniteNumber(confidenceLevel)) return null;
  const excludesZero = ci[0] > 0 || ci[1] < 0;
  if (excludesZero === variant.is_significant) return null;
  const alpha = formatAlpha(confidenceLevel);
  const threshold = Number(alpha);
  const test = testName(variant.statistical_test_used);
  const tail = ' The test decides; near the threshold the two can differ.';
  if (excludesZero) {
    if (adjustedShown && correctionMethod) {
      const adjusted = variant.adjusted_p_value as number;
      return (
        `Not significant after the ${correctionName(correctionMethod)} correction for ` +
        `${comparisons} comparisons (adjusted p ${formatPOnSide(adjusted, threshold, false)}), ` +
        'although the unadjusted interval excludes 0.'
      );
    }
    const decisive = decisivePValue(variant);
    if (!isFiniteNumber(decisive)) return null;
    return (
      `Not significant: ${test} gives p ${formatPOnSide(decisive, threshold, false)}, not ` +
      `below ${alpha}, although the interval excludes 0.${tail}`
    );
  }
  const p = adjustedShown ? variant.p_value : decisivePValue(variant);
  if (!isFiniteNumber(p)) return null;
  return (
    `Significant: ${test} gives p ${formatPOnSide(p, threshold, true)}, below ${alpha}, ` +
    `although the interval reaches 0.${tail}`
  );
}

/**
 * The users who sent a revenue, duration or custom metric's event when every
 * user's total value is 0 in every variant: the numbers were not sent in the
 * event's `value` (most often they went into its properties). Null otherwise,
 * for a count (which does not read `value`), and for a metric analysed as a
 * share.
 */
export function buyersWithNoValue(metric: MetricResult): number | null {
  if (!addsUpValues(metric) || metric.variants.length === 0) return null;
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
