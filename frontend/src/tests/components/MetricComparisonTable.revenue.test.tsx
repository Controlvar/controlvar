/**
 * A revenue metric analysed as revenue per user, in the metric comparison
 * table (the response's `analysis` is "mean").
 *
 * What is pinned:
 *  - the value is a plain amount per user, labelled "revenue per user", with
 *    no "%", no "$" and no "share of users";
 *  - a treatment's change cell adds the difference per user and its interval,
 *    at the response's confidence level, marked "unadjusted" when the row's
 *    p-value is the adjusted one;
 *  - before the first purchase (`analysis` "mean", no test on any treatment)
 *    the row is still revenue per user, never "0.00%";
 *  - a footnote says what revenue per user is, only when such a row exists;
 *  - when users sent the event but every total is 0, a note says the amount
 *    belongs in the event's `value`;
 *  - the table with revenue rows has no axe violations.
 */
import React from 'react';
import { render, screen, within } from '@testing-library/react';
import axe from 'axe-core';
import { MetricComparisonTable } from '@/components/results/MetricComparison/MetricComparisonTable';
import { MetricResult, VariantResult } from '@/types/results';

function variant(overrides: Partial<VariantResult>): VariantResult {
  return {
    variant_id: 'v',
    variant_name: 'V',
    is_control: false,
    sample_size: 2000,
    conversions: 120,
    mean: 0,
    std_dev: null,
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

const control = variant({
  variant_id: 'ctrl',
  variant_name: 'Control',
  is_control: true,
  conversions: 118,
  mean: 5.66,
  std_dev: 31.2,
  confidence_interval: [4.29, 7.03],
});

/** ShopLab's scale: about 6% buyers, a +21% point estimate that is not significant. */
function revenueMetric(
  treatment: Partial<VariantResult> = {},
  overrides: Partial<MetricResult> = {},
): MetricResult {
  return {
    metric_id: 'm-rev',
    metric_name: 'Order value',
    metric_type: 'revenue',
    is_primary: false,
    lower_is_better: false,
    analysis: 'mean',
    variants: [
      control,
      variant({
        variant_id: 'one-page',
        variant_name: 'one_page',
        conversions: 131,
        mean: 6.85,
        std_dev: 34.9,
        confidence_interval: [5.32, 8.38],
        p_value: 0.16,
        relative_improvement_pct: 21.02,
        statistical_test_used: 'welch_t_test',
        absolute_difference: 1.19,
        absolute_difference_ci: [-0.47, 2.84],
        ...treatment,
      }),
    ],
    ...overrides,
  };
}

const conversionMetric: MetricResult = {
  metric_id: 'm-conv',
  metric_name: 'Purchase',
  metric_type: 'conversion',
  is_primary: true,
  lower_is_better: false,
  analysis: 'proportion',
  variants: [
    variant({ variant_id: 'ctrl', variant_name: 'Control', is_control: true, mean: 0.059 }),
    variant({
      variant_id: 'one-page',
      variant_name: 'one_page',
      mean: 0.0655,
      p_value: 0.4,
      relative_improvement_pct: 11.0,
      statistical_test_used: 'fisher_exact',
    }),
  ],
};

function row(name: RegExp): HTMLElement {
  return screen.getByRole('row', { name });
}

function cells(r: HTMLElement): string[] {
  return within(r)
    .getAllByRole('cell')
    .map((c) => c.textContent ?? '');
}

describe('a revenue row reads as revenue per user', () => {
  it('shows each variant as a plain amount labelled "revenue per user", with no "%" or "$"', () => {
    const { container } = render(
      <MetricComparisonTable metrics={[revenueMetric()]} confidenceLevel={0.95} />,
    );
    expect(cells(row(/Control/))[3]).toBe('5.66revenue per user');
    expect(cells(row(/one_page/))[3]).toBe('6.85revenue per user');
    const table = within(container).getByRole('table');
    expect(table.textContent).not.toMatch(/\$|share of users|\d%\s*revenue/);
    expect(within(table).getAllByText('revenue per user')).toHaveLength(2);
  });

  it('gives the difference per user and its interval in the change cell', () => {
    render(<MetricComparisonTable metrics={[revenueMetric()]} confidenceLevel={0.95} />);
    const change = cells(row(/one_page/))[4];
    expect(change).toBe('+21.0% +1.19 per user (95% CI −0.47 to +2.84)');
    // The control has no difference of its own.
    expect(cells(row(/Control/))[4]).toBe('—');
  });

  it("states the interval at the response's level, not a fixed 95%", () => {
    render(<MetricComparisonTable metrics={[revenueMetric()]} confidenceLevel={0.92} />);
    expect(cells(row(/one_page/))[4]).toContain('+1.19 per user (92% CI −0.47 to +2.84)');
    expect(cells(row(/one_page/))[4]).not.toContain('95%');
  });

  it('says "unadjusted" when the row shows an adjusted p-value (a correction with two treatments)', () => {
    const metric = revenueMetric({ adjusted_p_value: 0.32 });
    metric.variants.push(
      variant({
        variant_id: 'two-step',
        variant_name: 'two_step',
        mean: 5.9,
        p_value: 0.8,
        adjusted_p_value: 0.8,
        relative_improvement_pct: 4.24,
        statistical_test_used: 'welch_t_test',
        absolute_difference: 0.24,
        absolute_difference_ci: [-1.6, 2.08],
      }),
    );
    render(
      <MetricComparisonTable
        metrics={[metric]}
        confidenceLevel={0.95}
        correctionMethod="benjamini_hochberg"
      />,
    );
    expect(cells(row(/one_page/))[4]).toContain(
      '+1.19 per user (95% CI, unadjusted, −0.47 to +2.84)',
    );
    expect(cells(row(/two_step/))[4]).toContain(
      '+0.24 per user (95% CI, unadjusted, −1.60 to +2.08)',
    );
  });

  it('does not say "unadjusted" when the correction changes nothing (one treatment)', () => {
    render(
      <MetricComparisonTable
        metrics={[revenueMetric({ adjusted_p_value: 0.16 })]}
        confidenceLevel={0.95}
        correctionMethod="benjamini_hochberg"
      />,
    );
    expect(cells(row(/one_page/))[4]).toContain('(95% CI −0.47 to +2.84)');
    expect(cells(row(/one_page/))[4]).not.toMatch(/unadjusted/);
  });

  it('gives the difference alone when there is no interval', () => {
    render(
      <MetricComparisonTable
        metrics={[revenueMetric({ absolute_difference_ci: null })]}
        confidenceLevel={0.95}
      />,
    );
    expect(cells(row(/one_page/))[4]).toBe('+21.0% +1.19 per user');
  });

  it('writes a significant fall with real minus signs, red and "worse" when higher is better', () => {
    render(
      <MetricComparisonTable
        metrics={[
          revenueMetric({
            mean: 4.1,
            relative_improvement_pct: -27.56,
            absolute_difference: -1.56,
            absolute_difference_ci: [-2.9, -0.22],
            p_value: 0.02,
            is_significant: true,
          }),
        ]}
        confidenceLevel={0.95}
      />,
    );
    const change = within(row(/one_page/)).getAllByRole('cell')[4];
    expect(change).toHaveTextContent('−27.6% · worse −1.56 per user (95% CI −2.90 to −0.22)');
    expect(change).toHaveClass('text-red-700');
    expect(change.textContent).not.toContain('-');
  });

  it('keeps a proportion row as it was: a share, with no per-user line', () => {
    render(
      <MetricComparisonTable metrics={[conversionMetric, revenueMetric()]} confidenceLevel={0.95} />,
    );
    const purchase = screen
      .getAllByRole('row')
      .find((r) => r.textContent?.startsWith('Purchase') && r.textContent.includes('one_page'));
    expect(purchase).toBeDefined();
    expect(cells(purchase as HTMLElement)[3]).toBe('6.55%');
    expect(cells(purchase as HTMLElement)[4]).toBe('+11.0%');
  });
});

describe('before the first purchase', () => {
  // Every user's total is 0, so no treatment has a test yet: only `analysis`
  // says the metric is a mean. Read from the test, it would be "0.00%".
  it('is still revenue per user, not a share', () => {
    const metric = revenueMetric({
      conversions: 0,
      mean: 0,
      std_dev: 0,
      p_value: null,
      relative_improvement_pct: null,
      statistical_test_used: null,
      absolute_difference: 0,
      absolute_difference_ci: null,
    });
    metric.variants[0] = { ...control, conversions: 0, mean: 0, std_dev: 0 };
    render(<MetricComparisonTable metrics={[metric]} confidenceLevel={0.95} />);
    expect(cells(row(/Control/))[3]).toBe('0.00revenue per user');
    expect(cells(row(/one_page/))[3]).toBe('0.00revenue per user');
    expect(screen.getByTestId('metric-comparison-table').textContent).not.toMatch(
      /0\.00%|share of users/,
    );
    // No one sent the event, so there is nothing to say about values.
    expect(screen.queryByTestId('revenue-no-value-note')).not.toBeInTheDocument();
  });
});

describe('the revenue footnote', () => {
  it('says what revenue per user is, under a table with a revenue row', () => {
    render(
      <MetricComparisonTable metrics={[conversionMetric, revenueMetric()]} confidenceLevel={0.95} />,
    );
    expect(screen.getByTestId('revenue-per-user-footnote')).toHaveTextContent(
      "Revenue per user: each user's total value on the metric's event, averaged over every user " +
        'assigned to the variant; a user with no such event counts as 0. Values are added as sent: ' +
        'no currency is read or converted, and no order is capped.',
    );
    expect(screen.getByTestId('revenue-per-user-footnote').textContent).not.toContain('$');
  });

  it('is not shown without a revenue row', () => {
    render(<MetricComparisonTable metrics={[conversionMetric]} confidenceLevel={0.95} />);
    expect(screen.queryByTestId('revenue-per-user-footnote')).not.toBeInTheDocument();
  });
});

describe('users sent the event with no value', () => {
  /** 312 users sent the event; every user's total is 0 in both variants. */
  function noValues(): MetricResult {
    const metric = revenueMetric({
      conversions: 160,
      mean: 0,
      std_dev: 0,
      p_value: null,
      relative_improvement_pct: null,
      absolute_difference: 0,
      absolute_difference_ci: null,
    });
    metric.variants[0] = { ...control, conversions: 152, mean: 0, std_dev: 0 };
    return metric;
  }

  it('says so, naming the event, and where the amount belongs', () => {
    render(
      <MetricComparisonTable
        metrics={[noValues()]}
        confidenceLevel={0.95}
        eventNames={{ 'm-rev': 'purchase' }}
      />,
    );
    const note = screen.getByTestId('revenue-no-value-note');
    expect(note).toHaveTextContent(
      "Order value: 312 users sent purchase, and every user's total value is 0. " +
        "Send the amount in the event's value field, not in its properties.",
    );
    expect(within(note).getByText('purchase').tagName).toBe('CODE');
  });

  it('says "its event" when the event name is not known', () => {
    render(<MetricComparisonTable metrics={[noValues()]} confidenceLevel={0.95} />);
    expect(screen.getByTestId('revenue-no-value-note')).toHaveTextContent(
      'Order value: 312 users sent its event,',
    );
  });

  it('is not shown when any user has a value', () => {
    render(
      <MetricComparisonTable
        metrics={[revenueMetric()]}
        confidenceLevel={0.95}
        eventNames={{ 'm-rev': 'purchase' }}
      />,
    );
    expect(screen.queryByTestId('revenue-no-value-note')).not.toBeInTheDocument();
  });

  it('is not shown for a metric analysed as a share', () => {
    const metric = noValues();
    render(
      <MetricComparisonTable
        metrics={[{ ...metric, analysis: 'proportion' }]}
        confidenceLevel={0.95}
      />,
    );
    expect(screen.queryByTestId('revenue-no-value-note')).not.toBeInTheDocument();
  });
});

describe('accessibility', () => {
  it('has no axe violations with revenue rows, the footnote and the note', async () => {
    const withNote = revenueMetric({ conversions: 3, mean: 0, std_dev: 0, absolute_difference: 0 });
    withNote.metric_id = 'm-rev-2';
    withNote.variants[0] = { ...control, conversions: 2, mean: 0, std_dev: 0 };
    const { container } = render(
      <MetricComparisonTable
        metrics={[conversionMetric, revenueMetric(), withNote]}
        confidenceLevel={0.95}
        eventNames={{ 'm-rev-2': 'purchase' }}
      />,
    );
    expect(screen.getByTestId('revenue-no-value-note')).toBeInTheDocument();
    const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations).toEqual([]);
  });
});
