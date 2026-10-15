/**
 * The Estimate step's input checks and the query they become. The API call
 * itself is exercised through the page in experiment-new-guided.test.tsx.
 */
import {
  buildEstimateQuery,
  ESTIMATE_PROBLEMS,
  EstimateInputs,
  INITIAL_ESTIMATE_INPUTS,
  isEvenSplit,
  POWER_OPTIONS,
  estimateSignificance,
  formatEffect,
  plannedEffectFraction,
  plannedEffectProblem,
} from '@/components/experiments/new/estimate';
import { CONFIDENCE_OPTIONS } from '@/components/results/shared/analysisSettings';

const inputs = (over: Partial<EstimateInputs> = {}): EstimateInputs => ({
  ...INITIAL_ESTIMATE_INPUTS,
  baselinePct: '12',
  mdePct: '5',
  ...over,
});

describe('estimate options', () => {
  it('offers power 80/90/95%, defaulting to 80%', () => {
    expect([...POWER_OPTIONS]).toEqual([0.8, 0.9, 0.95]);
    expect(INITIAL_ESTIMATE_INPUTS.power).toBe(0.8);
  });

  it('plans at 1 minus each confidence level the form offers: 10%, 5% and 1%', () => {
    expect(CONFIDENCE_OPTIONS.map(estimateSignificance)).toEqual([0.1, 0.05, 0.01]);
  });

  it('keeps every option inside the bounds the endpoint accepts', () => {
    // statistical_power: ge=0.5, le=0.99; significance_level: ge=0.01, le=0.1.
    // A frontend-divided alpha (0.05 / 2) would be refused, so the estimate
    // never divides it (PE condition 9).
    for (const p of POWER_OPTIONS) {
      expect(p).toBeGreaterThanOrEqual(0.5);
      expect(p).toBeLessThanOrEqual(0.99);
    }
    for (const level of CONFIDENCE_OPTIONS) {
      expect(estimateSignificance(level)).toBeGreaterThanOrEqual(0.01);
      expect(estimateSignificance(level)).toBeLessThanOrEqual(0.1);
    }
  });
});

describe('buildEstimateQuery', () => {
  it('turns percentages into fractions and reads the effect as relative', () => {
    expect(buildEstimateQuery(inputs(), 2)).toEqual({
      ok: true,
      query: {
        baseline_rate: 0.12,
        minimum_detectable_effect: 0.05,
        statistical_power: 0.8,
        significance_level: 0.05,
        variant_count: 2,
      },
    });
  });

  it('plans at 1 minus the confidence level it is given, with no correction for 3 variants', () => {
    const built = buildEstimateQuery(inputs(), 3, 0.9);
    expect(built.ok && built.query.significance_level).toBe(0.1);
  });

  it('sends the variant count it is given', () => {
    const built = buildEstimateQuery(inputs(), 3);
    expect(built.ok && built.query.variant_count).toBe(3);
  });

  it('sends daily users with the share as a fraction', () => {
    const built = buildEstimateQuery(inputs({ dailyUsers: '5000', sharePct: '40' }), 2);
    expect(built.ok && built.query).toMatchObject({ daily_traffic: 5000, traffic_allocation: 0.4 });
  });

  it('counts all daily users when the share is blank, so a duration is still estimated', () => {
    const built = buildEstimateQuery(inputs({ dailyUsers: '5000' }), 2);
    expect(built.ok && built.query).toMatchObject({ daily_traffic: 5000, traffic_allocation: 1 });
  });

  it('sends neither traffic field when daily users is blank', () => {
    const built = buildEstimateQuery(inputs(), 2);
    expect(built.ok && Object.keys(built.query).sort()).toEqual([
      'baseline_rate',
      'minimum_detectable_effect',
      'significance_level',
      'statistical_power',
      'variant_count',
    ]);
  });

  it.each([
    ['fewer than two variants', inputs(), 1, ESTIMATE_PROBLEMS.variants],
    ['a blank baseline', inputs({ baselinePct: '' }), 2, ESTIMATE_PROBLEMS.baseline],
    ['a baseline of 0', inputs({ baselinePct: '0' }), 2, ESTIMATE_PROBLEMS.baseline],
    ['a baseline below 0.01%', inputs({ baselinePct: '0.001' }), 2, ESTIMATE_PROBLEMS.baseline],
    ['a baseline of 100%', inputs({ baselinePct: '100' }), 2, ESTIMATE_PROBLEMS.baseline],
    ['a blank effect', inputs({ mdePct: '' }), 2, ESTIMATE_PROBLEMS.mde],
    ['an effect below 0.1%', inputs({ mdePct: '0.0000001' }), 2, ESTIMATE_PROBLEMS.mde],
    ['a negative effect', inputs({ mdePct: '-5' }), 2, ESTIMATE_PROBLEMS.mde],
    ['a baseline and effect reaching 100%', inputs({ baselinePct: '60', mdePct: '80' }), 2, ESTIMATE_PROBLEMS.ceiling],
    ['an effect of 100%', inputs({ baselinePct: '1', mdePct: '100' }), 2, ESTIMATE_PROBLEMS.mdeHigh],
    ['an effect above 100%', inputs({ baselinePct: '1', mdePct: '150' }), 2, ESTIMATE_PROBLEMS.mdeHigh],
    ['fractional daily users', inputs({ dailyUsers: '10.5' }), 2, ESTIMATE_PROBLEMS.dailyUsers],
    ['zero daily users', inputs({ dailyUsers: '0' }), 2, ESTIMATE_PROBLEMS.dailyUsers],
    ['a share of 0%', inputs({ dailyUsers: '100', sharePct: '0' }), 2, ESTIMATE_PROBLEMS.share],
    ['a share above 100%', inputs({ dailyUsers: '100', sharePct: '101' }), 2, ESTIMATE_PROBLEMS.share],
    ['a share without daily users', inputs({ sharePct: '50' }), 2, ESTIMATE_PROBLEMS.shareWithoutUsers],
  ])('refuses %s', (_label, given, variantCount, problem) => {
    expect(buildEstimateQuery(given, variantCount)).toEqual({ ok: false, problem });
  });

  it('accepts the edges: 0.01% and 99.99% baselines, a 0.1% effect, a 100% share', () => {
    expect(buildEstimateQuery(inputs({ baselinePct: '0.01' }), 2).ok).toBe(true);
    expect(buildEstimateQuery(inputs({ baselinePct: '90', mdePct: '0.1' }), 2).ok).toBe(true);
    expect(buildEstimateQuery(inputs({ dailyUsers: '1', sharePct: '100' }), 2).ok).toBe(true);
  });
});

describe('the planned effect saved with the experiment (#1257)', () => {
  it.each([
    ['', null, undefined],
    ['10', null, 0.1],
    [' 7 ', null, 0.07],
    ['0.1', null, 0.001],
    ['99.99', null, 0.9999],
    ['28.6', null, 0.286],
    ['abc', ESTIMATE_PROBLEMS.mde, undefined],
    ['0.09', ESTIMATE_PROBLEMS.mde, undefined],
    ['-5', ESTIMATE_PROBLEMS.mde, undefined],
    ['100', ESTIMATE_PROBLEMS.mdeHigh, undefined],
    ['150', ESTIMATE_PROBLEMS.mdeHigh, undefined],
    // Rounds to 1 when saved: refused, never saved as 1.
    ['99.99999999995', ESTIMATE_PROBLEMS.mdeHigh, undefined],
    ['99.9999999', null, 0.999999999],
  ])('%j: problem %j, saved as %j', (typed, problem, fraction) => {
    expect(plannedEffectProblem(typed)).toBe(problem);
    expect(plannedEffectFraction(typed)).toBe(fraction);
  });

  it('formats a saved effect without float noise', () => {
    expect(formatEffect(0.07)).toBe('7%');
    expect(formatEffect(0.001)).toBe('0.1%');
    expect(formatEffect(0.1)).toBe('10%');
    expect(formatEffect(28.6 / 100)).toBe('28.6%');
    expect(formatEffect(0.0015)).toBe('0.15%');
  });
});

describe('isEvenSplit', () => {
  it('is true only when every variant has the same share', () => {
    expect(isEvenSplit([50, 50])).toBe(true);
    expect(isEvenSplit([25, 25, 25, 25])).toBe(true);
    expect(isEvenSplit([20, 30, 50])).toBe(false);
  });
});
