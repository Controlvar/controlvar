/**
 * How the dashboard decides whether a metric's numbers are a share of users
 * or a mean, and how it writes an amount per user.
 *
 * The response's `analysis` decides. A result without it, from a server older
 * than the field, is read from the test it ran: `welch_t_test` is a mean. The
 * field names are the API's, read from the stable OpenAPI snapshot.
 */
import fs from 'fs';
import path from 'path';
import {
  addsUpValues,
  analysedAsMean,
  analysedAsRate,
  buyersWithNoValue,
  differenceLine,
  formatAmount,
  formatSignedAmount,
  meanDescription,
  meanLabelForType,
} from '@/components/results/shared/resultFormat';
import { MetricAnalysis, MetricResult, StatisticalTestUsed, VariantResult } from '@/types/results';

function variant(overrides: Partial<VariantResult> = {}): VariantResult {
  return {
    variant_id: 'v',
    variant_name: 'V',
    is_control: false,
    sample_size: 100,
    conversions: 10,
    mean: 1,
    std_dev: 1,
    confidence_interval: null,
    p_value: null,
    adjusted_p_value: null,
    is_significant: false,
    effect_size: null,
    effect_size_label: null,
    relative_improvement_pct: null,
    power: null,
    ...overrides,
  };
}

function metric(
  metricType: MetricResult['metric_type'],
  analysis: MetricAnalysis | undefined,
  test: StatisticalTestUsed | null | undefined,
): MetricResult {
  const m: MetricResult = {
    metric_id: 'm',
    metric_name: 'M',
    metric_type: metricType,
    is_primary: true,
    variants: [
      variant({ is_control: true, statistical_test_used: null }),
      variant({ statistical_test_used: test }),
    ],
  };
  if (analysis !== undefined) m.analysis = analysis;
  return m;
}

describe('analysedAsMean / analysedAsRate', () => {
  it.each([
    // [type, analysis, treatment's test, a mean?]
    ['revenue', 'mean', 'welch_t_test', true],
    // Before the first purchase no treatment has a test; `analysis` still says mean.
    ['revenue', 'mean', null, true],
    ['revenue', 'mean', 'fisher_exact', true],
    // The server says proportion: that wins over a test name.
    ['revenue', 'proportion', 'welch_t_test', false],
    ['count', 'proportion', 'fisher_exact', false],
    // #439: count, duration and custom are means when the server says so.
    ['count', 'mean', 'welch_t_test', true],
    ['duration', 'mean', null, true],
    ['custom', 'mean', 'welch_t_test', true],
    ['conversion', 'proportion', 'fisher_exact', false],
    // No `analysis` (a server older than the field): read the test.
    ['revenue', undefined, 'welch_t_test', true],
    ['revenue', undefined, 'fisher_exact', false],
    ['revenue', undefined, null, false],
    ['duration', undefined, 'welch_t_test', true],
    ['conversion', undefined, 'welch_t_test', false],
    ['conversion', undefined, 'z_test_proportions', false],
  ] as const)('%s with analysis %s and test %s: mean is %s', (type, analysis, test, mean) => {
    const m = metric(type, analysis, test);
    expect(analysedAsRate(m)).toBe(!mean);
    expect(analysedAsMean(m)).toBe(mean);
  });

  // C1 (#439): each mean type is labelled by what it is per user.
  it.each([
    ['revenue', 'revenue per user'],
    ['count', 'events per user'],
    ['duration', 'duration per user'],
    ['custom', 'value per user'],
  ] as const)('labels a %s mean "%s"', (type, label) => {
    expect(meanDescription(metric(type, 'mean', 'welch_t_test'))).toBe(label);
    expect(meanLabelForType(type)).toBe(label);
  });

  it('labels a mean of a type it does not know "mean per user"', () => {
    expect(meanLabelForType('ratio')).toBe('mean per user');
  });
});

/**
 * The difference line a mean row shows (`differenceLine`), as its visible
 * text: the lead, then the interval in parentheses.
 */
function perUser(v: VariantResult, level: number, unadjusted: boolean): string | null {
  const metric: MetricResult = {
    metric_id: 'm',
    metric_name: 'Order value',
    metric_type: 'revenue',
    is_primary: true,
    analysis: 'mean',
    variants: [variant({ variant_id: 'c', is_control: true }), v],
  };
  const line = differenceLine(metric, v, level, unadjusted);
  if (!line) return null;
  const i = line.interval;
  if (!i) return line.lead;
  return `${line.lead} (${i.level} CI${i.unadjusted ? ', unadjusted,' : ''} ${i.lower} to ${i.upper})`;
}

describe('amounts per user', () => {
  it.each([
    [5.66, '5.66', '+5.66'],
    [1234.5, '1234.50', '+1234.50'],
    [-0.47, '−0.47', '−0.47'],
    [0, '0.00', '0.00'],
    [0.004, '0.00', '0.00'],
    [-0.004, '0.00', '0.00'],
  ])('%s is "%s", signed "%s"', (v, plain, signed) => {
    expect(formatAmount(v)).toBe(plain);
    expect(formatSignedAmount(v)).toBe(signed);
  });

  it('writes the difference with its interval, at the given level', () => {
    const v = variant({ absolute_difference: 0.4, absolute_difference_ci: [0.1, 0.7] });
    expect(perUser(v, 0.95, false)).toBe('+0.40 per user (95% CI +0.10 to +0.70)');
    expect(perUser(v, 0.9, true)).toBe(
      '+0.40 per user (90% CI, unadjusted, +0.10 to +0.70)',
    );
    expect(perUser(v, 0.975, false)).toBe(
      '+0.40 per user (97.5% CI +0.10 to +0.70)',
    );
  });

  it('gives nothing for the control, a missing difference or a non-finite one', () => {
    const diff = { absolute_difference: 0.4, absolute_difference_ci: [0.1, 0.7] as [number, number] };
    expect(perUser(variant({ ...diff, is_control: true }), 0.95, false)).toBeNull();
    expect(perUser(variant(), 0.95, false)).toBeNull();
    expect(perUser(variant({ absolute_difference: null }), 0.95, false)).toBeNull();
    expect(perUser(variant({ absolute_difference: NaN }), 0.95, false)).toBeNull();
  });

  it('drops an interval that is missing or not finite', () => {
    expect(
      perUser(
        variant({ absolute_difference: -1.5, absolute_difference_ci: null }),
        0.95,
        false,
      ),
    ).toBe('−1.50 per user');
    expect(
      perUser(
        variant({ absolute_difference: -1.5, absolute_difference_ci: [-Infinity, 0.2] }),
        0.95,
        false,
      ),
    ).toBe('−1.50 per user');
  });

  it("gives nothing for a 0.00 difference with no interval (every user's total is the same)", () => {
    for (const diff of [0, 0.004, -0.004]) {
      expect(
        perUser(
          variant({ absolute_difference: diff, absolute_difference_ci: null }),
          0.95,
          false,
        ),
      ).toBeNull();
    }
    // With an interval, a difference of 0.00 is an estimate and is shown.
    expect(
      perUser(
        variant({ absolute_difference: 0, absolute_difference_ci: [-0.5, 0.5] }),
        0.95,
        false,
      ),
    ).toBe('0.00 per user (95% CI −0.50 to +0.50)');
  });
});

describe('buyersWithNoValue', () => {
  function revenue(...arms: [number | null, number, number | null][]): MetricResult {
    return {
      ...metric('revenue', 'mean', 'welch_t_test'),
      variants: arms.map(([conversions, mean, sd], i) =>
        variant({ is_control: i === 0, conversions, mean, std_dev: sd }),
      ),
    };
  }

  it('counts the buyers when every user totals 0 in every variant', () => {
    expect(buyersWithNoValue(revenue([152, 0, 0], [160, 0, 0]))).toBe(312);
    // Fewer than 2 users: no standard deviation, and the one user totals 0.
    expect(buyersWithNoValue(revenue([1, 0, null], [0, 0, 0]))).toBe(1);
  });

  it('is null when there is a value, no buyer, or the metric is not revenue per user', () => {
    expect(buyersWithNoValue(revenue([152, 0, 0], [160, 0.5, 4]))).toBeNull();
    // Refunds that cancel out leave a mean of 0 but totals that vary.
    expect(buyersWithNoValue(revenue([152, 0, 3], [160, 0, 0]))).toBeNull();
    expect(buyersWithNoValue(revenue([0, 0, 0], [0, 0, 0]))).toBeNull();
    expect(buyersWithNoValue(revenue([null, 0, 0], [null, 0, 0]))).toBeNull();
    const share = { ...revenue([152, 0, 0], [160, 0, 0]), analysis: 'proportion' as const };
    expect(buyersWithNoValue(share)).toBeNull();
  });

  // C3 (#439): a duration or custom metric adds up `value` as revenue does,
  // so its senders with no value are counted; a count does not read `value`,
  // so a count of 0 is a measured 0 and gets no note.
  it.each([
    ['duration', 312],
    ['custom', 312],
    ['count', null],
  ] as const)('a %s mean with every total 0 gives %s', (type, expected) => {
    const m = { ...revenue([152, 0, 0], [160, 0, 0]), metric_type: type };
    expect(buyersWithNoValue(m)).toBe(expected);
    expect(addsUpValues(m)).toBe(type !== 'count');
  });
});

describe('the field names are the API’s', () => {
  const snapshot = JSON.parse(
    fs.readFileSync(
      path.join(__dirname, '..', '..', '..', '..', 'docs', 'api', 'openapi-v1.stable.json'),
      'utf8',
    ),
  );
  const schemas = snapshot.components.schemas;

  it('MetricResult.analysis is one of the values the dashboard reads', () => {
    const analysis = schemas.MetricResult.properties.analysis;
    const values: MetricAnalysis[] = ['proportion', 'mean'];
    expect([...analysis.enum].sort()).toEqual([...values].sort());
  });

  it('VariantResult carries absolute_difference and absolute_difference_ci', () => {
    expect(Object.keys(schemas.VariantResult.properties)).toEqual(
      expect.arrayContaining(['absolute_difference', 'absolute_difference_ci']),
    );
  });
});
