/**
 * The Estimate step of guided setup: what the user typed, and the request it
 * becomes.
 *
 * Pure TypeScript. The inputs are percentages as typed (strings, so a half-typed
 * number stays on screen); `buildEstimateQuery` checks them and converts them to
 * the fractions `GET /api/v1/experiments/analysis/sample-size` takes, or says
 * what is wrong.
 *
 * One input is saved: the minimum detectable effect, when it is usable
 * (`plannedEffectFraction`), goes in the create request as the primary
 * metric's `expected_effect`, and the results page plans the sample for it
 * (#1257). The rest of the estimate is never sent with the experiment.
 */
import { SampleSizeQuery } from '@/services/experiments';
import { DEFAULT_CONFIDENCE_LEVEL } from '@/components/results/shared/analysisSettings';

/** Statistical power offered, as fractions; the first is the default. */
export const POWER_OPTIONS = [0.8, 0.9, 0.95] as const;

/**
 * The two-sided significance the estimate plans at: 1 - the experiment's
 * confidence level, chosen in "How results will be judged" on the same step.
 * The estimate plans each comparison at this level and makes no correction for
 * comparing several variants with the control (#820 would add one).
 */
export function estimateSignificance(confidenceLevel: number): number {
  return Number((1 - confidenceLevel).toFixed(4));
}

export interface EstimateInputs {
  /** Baseline conversion rate, in percent. */
  baselinePct: string;
  /** Minimum detectable effect, RELATIVE, in percent: 5 means 12% -> 12.6%. */
  mdePct: string;
  power: number;
  /** Users per day, optional. */
  dailyUsers: string;
  /** Share of those users in the experiment, in percent; blank means all of them. */
  sharePct: string;
}

export const INITIAL_ESTIMATE_INPUTS: EstimateInputs = {
  baselinePct: '',
  mdePct: '',
  power: POWER_OPTIONS[0],
  dailyUsers: '',
  sharePct: '',
};

export type EstimateQueryResult = { ok: true; query: SampleSizeQuery } | { ok: false; problem: string };

export const ESTIMATE_PROBLEMS = {
  variants: 'The estimate needs at least two variants. Add one on the Variants step.',
  baseline: 'Enter a baseline conversion rate between 0.01% and 99.99%.',
  mde: 'Enter a minimum detectable effect of at least 0.1%.',
  mdeHigh: 'Enter a minimum detectable effect below 100%.',
  ceiling:
    'This baseline raised by this effect reaches 100% or more. Lower the baseline rate or the effect.',
  dailyUsers: 'Daily users must be a whole number of at least 1.',
  share: 'The share of users must be more than 0% and at most 100%.',
  shareWithoutUsers: 'Enter daily users as well, or leave the share blank.',
} as const;

/** A finite number from a typed field, or null for blank or unreadable input. */
function readNumber(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === '') return null;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : null;
}

/**
 * What is wrong with a typed minimum detectable effect, or null when it can be
 * saved. Blank is fine: nothing is saved and the results page plans for its
 * default. A usable effect is at least 0.1% and below 100%; the results page's
 * plan takes nothing at or above 100%.
 */
export function plannedEffectProblem(mdePct: string): string | null {
  if (mdePct.trim() === '') return null;
  const value = readNumber(mdePct);
  if (value === null || value < 0.1) return ESTIMATE_PROBLEMS.mde;
  if (value >= 100) return ESTIMATE_PROBLEMS.mdeHigh;
  return null;
}

/**
 * The typed minimum detectable effect as the fraction saved with the
 * experiment (10 -> 0.1), or undefined when it is blank or not usable.
 */
export function plannedEffectFraction(mdePct: string): number | undefined {
  if (mdePct.trim() === '' || plannedEffectProblem(mdePct) !== null) return undefined;
  // Rounded to 12 significant digits, so 28.6 is saved as 0.286, not 0.28600000000000003.
  return Number((Number(mdePct.trim()) / 100).toPrecision(12));
}

/** A saved effect as a percentage, without float noise: 0.07 -> "7%", 0.001 -> "0.1%". */
export function formatEffect(fraction: number): string {
  return `${Number((fraction * 100).toFixed(4))}%`;
}

/**
 * The sample-size query for these inputs and this many variants, or the first
 * problem with them. A refused input never reaches the API. The endpoint also
 * refuses a treatment rate of 100% or more itself, with a 422 carrying the
 * `ceiling` sentence word for word (a backend test reads it from this file).
 */
export function buildEstimateQuery(
  inputs: EstimateInputs,
  variantCount: number,
  confidenceLevel: number = DEFAULT_CONFIDENCE_LEVEL,
): EstimateQueryResult {
  if (variantCount < 2) return { ok: false, problem: ESTIMATE_PROBLEMS.variants };

  const baselinePct = readNumber(inputs.baselinePct);
  if (baselinePct === null || baselinePct < 0.01 || baselinePct > 99.99) {
    return { ok: false, problem: ESTIMATE_PROBLEMS.baseline };
  }
  const mdePct = readNumber(inputs.mdePct);
  if (mdePct === null || mdePct < 0.1) return { ok: false, problem: ESTIMATE_PROBLEMS.mde };
  if (mdePct >= 100) return { ok: false, problem: ESTIMATE_PROBLEMS.mdeHigh };

  const baseline = baselinePct / 100;
  const mde = mdePct / 100;
  if (baseline * (1 + mde) >= 1) return { ok: false, problem: ESTIMATE_PROBLEMS.ceiling };

  const query: SampleSizeQuery = {
    baseline_rate: baseline,
    minimum_detectable_effect: mde,
    statistical_power: inputs.power,
    significance_level: estimateSignificance(confidenceLevel),
    variant_count: variantCount,
  };

  const dailyUsersText = inputs.dailyUsers.trim();
  const shareText = inputs.sharePct.trim();
  if (dailyUsersText !== '') {
    const dailyUsers = readNumber(dailyUsersText);
    if (dailyUsers === null || !Number.isInteger(dailyUsers) || dailyUsers < 1) {
      return { ok: false, problem: ESTIMATE_PROBLEMS.dailyUsers };
    }
    query.daily_traffic = dailyUsers;
    // The endpoint estimates a duration only when it gets BOTH numbers, so a
    // blank share means "all of them" rather than "no duration".
    if (shareText === '') {
      query.traffic_allocation = 1;
    } else {
      const share = readNumber(shareText);
      if (share === null || share <= 0 || share > 100) {
        return { ok: false, problem: ESTIMATE_PROBLEMS.share };
      }
      query.traffic_allocation = share / 100;
    }
  } else if (shareText !== '') {
    return { ok: false, problem: ESTIMATE_PROBLEMS.shareWithoutUsers };
  }

  return { ok: true, query };
}

/** Whether every variant has the same share of traffic. */
export function isEvenSplit(allocations: number[]): boolean {
  return allocations.every((a) => (Number(a) || 0) === (Number(allocations[0]) || 0));
}
