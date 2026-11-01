/**
 * A count, duration or custom metric analysed as its mean per assigned user
 * (#439), in the metric comparison table (the response's `analysis` is
 * "mean").
 *
 * What is pinned (UX C2, C3, C4, C8):
 *  - the value is a plain number labelled by what it is per user ("events
 *    per user", "duration per user", "value per user"), never "mean", "share
 *    of users", "%", a unit or a currency;
 *  - one footnote per mean type present, word for word, and none for a type
 *    that is absent;
 *  - the "values not sent" note shows for a duration or custom metric whose
 *    every total is 0, naming what to send, and never for a count, which does
 *    not read `value`;
 *  - the labels read the response's `analysis`, not the type: a count metric
 *    a server analysed as a share renders as a share;
 *  - a count whose events carried no value (PE-5) shows events per user
 *    above 0, not 0 against 0;
 *  - the table has no axe violations with these rows and notes.
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
    sample_size: 100,
    conversions: 75,
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

type MeanType = 'count' | 'duration' | 'custom';

const NAMES: Record<MeanType, string> = {
  count: 'Articles read',
  duration: 'Watch time',
  custom: 'Points',
};

/** F4's count data: 1.5 against 2.0 events per user, Welch p 0.0064. */
function meanMetric(
  type: MeanType,
  treatment: Partial<VariantResult> = {},
  overrides: Partial<MetricResult> = {},
): MetricResult {
  return {
    metric_id: `m-${type}`,
    metric_name: NAMES[type],
    metric_type: type,
    is_primary: type === 'count',
    lower_is_better: false,
    analysis: 'mean',
    variants: [
      variant({
        variant_id: 'ctrl',
        variant_name: 'Control',
        is_control: true,
        mean: 1.5,
        std_dev: 1.12,
      }),
      variant({
        variant_id: 'b',
        variant_name: 'treatment',
        conversions: 80,
        mean: 2.0,
        std_dev: 1.42,
        p_value: 0.0064,
        is_significant: true,
        relative_improvement_pct: 33.33,
        statistical_test_used: 'welch_t_test',
        absolute_difference: 0.5,
        absolute_difference_ci: [0.14, 0.86],
        ...treatment,
      }),
    ],
    ...overrides,
  };
}

function rowsOf(name: string): HTMLElement[] {
  return screen.getAllByRole('row').filter((r) => r.textContent?.startsWith(name));
}

function cells(r: HTMLElement): string[] {
  return within(r)
    .getAllByRole('cell')
    .map((c) => c.textContent ?? '');
}

const LABELS: Record<MeanType, string> = {
  count: 'events per user',
  duration: 'duration per user',
  custom: 'value per user',
};

const FOOTNOTES: Record<MeanType, string> = {
  count:
    "Events per user: how many times each user sent the metric's event, averaged over every user " +
    "assigned to the variant; a user with none counts as 0. The events' value is not read.",
  duration:
    "Duration per user: each user's total value on the metric's event, averaged over every user " +
    'assigned to the variant; a user with none counts as 0, so a variant whose users send the event ' +
    'less often reads as less time. Values are added in the unit they were sent in: no unit is read ' +
    'or converted, and no value is capped.',
  custom:
    "Value per user: each user's total value on the metric's event, averaged over every user " +
    'assigned to the variant; a user with none counts as 0. Values are added as sent, and no value ' +
    'is capped.',
};

describe.each(['count', 'duration', 'custom'] as const)('a %s metric analysed as a mean', (type) => {
  it('labels its value by what it is per user, with the difference per user', () => {
    render(<MetricComparisonTable metrics={[meanMetric(type)]} confidenceLevel={0.95} />);
    const [controlRow, treatmentRow] = rowsOf(NAMES[type]);
    expect(cells(controlRow)[3]).toBe(`1.50${LABELS[type]}`);
    expect(cells(treatmentRow)[3]).toBe(`2.00${LABELS[type]}`);
    expect(cells(treatmentRow)[4]).toContain('+0.50 per user (95% CI +0.14 to +0.86)');
    const text = screen.getByTestId('metric-comparison-table').textContent ?? '';
    // No "share of users", no bare "mean", no currency and no invented unit.
    expect(text).not.toMatch(/share of users|\bmean\b|\$|€|£|¥|\bseconds\b|\bminutes\b/);
  });

  it('has its own footnote, word for word, and no other type’s', () => {
    render(<MetricComparisonTable metrics={[meanMetric(type)]} confidenceLevel={0.95} />);
    expect(screen.getByTestId(`${type}-per-user-footnote`)).toHaveTextContent(FOOTNOTES[type]);
    for (const other of ['revenue', 'count', 'duration', 'custom'].filter((t) => t !== type)) {
      expect(screen.queryByTestId(`${other}-per-user-footnote`)).not.toBeInTheDocument();
    }
  });

  // C4: the labels read `analysis`, never the type.
  it('renders as a share when the server analysed it as one', () => {
    const share = meanMetric(
      type,
      {
        mean: 0.8,
        statistical_test_used: 'fisher_exact',
        absolute_difference: null,
        absolute_difference_ci: null,
      },
      { analysis: 'proportion' },
    );
    share.variants[0] = { ...share.variants[0], mean: 0.75 };
    render(<MetricComparisonTable metrics={[share]} confidenceLevel={0.95} />);
    const [controlRow] = rowsOf(NAMES[type]);
    expect(cells(controlRow)[3]).toBe('75.00%share of users with at least one event');
    expect(screen.queryByTestId(`${type}-per-user-footnote`)).not.toBeInTheDocument();
    expect(screen.getByTestId('metric-comparison-table').textContent).not.toMatch(/per user/);
  });
});

describe('one footnote per mean type present', () => {
  it('shows the count, duration and custom footnotes together, once each', () => {
    render(
      <MetricComparisonTable
        metrics={[meanMetric('count'), meanMetric('duration'), meanMetric('custom')]}
        confidenceLevel={0.95}
      />,
    );
    for (const type of ['count', 'duration', 'custom']) {
      expect(screen.getAllByTestId(`${type}-per-user-footnote`)).toHaveLength(1);
    }
    expect(screen.queryByTestId('revenue-per-user-footnote')).not.toBeInTheDocument();
  });
});

describe('users sent the event with no value', () => {
  /** 155 users sent the event; every user's total is 0 in both variants. */
  function noValues(type: MeanType): MetricResult {
    const metric = meanMetric(type, {
      mean: 0,
      std_dev: 0,
      p_value: null,
      is_significant: false,
      relative_improvement_pct: null,
      absolute_difference: 0,
      absolute_difference_ci: null,
    });
    metric.variants[0] = { ...metric.variants[0], mean: 0, std_dev: 0 };
    return metric;
  }

  it.each([
    ['duration', 'duration'],
    ['custom', 'number'],
  ] as const)('a %s metric says the %s belongs in the event’s value', (type, noun) => {
    render(
      <MetricComparisonTable
        metrics={[noValues(type)]}
        confidenceLevel={0.95}
        eventNames={{ [`m-${type}`]: 'the_event' }}
      />,
    );
    const note = screen.getByTestId('no-value-note');
    expect(note).toHaveTextContent(
      `${NAMES[type]}: 155 users sent the_event, and every user's total value is 0. ` +
        `Send the ${noun} in the event's value field, not in its properties.`,
    );
    expect(within(note).getByText('the_event').tagName).toBe('CODE');
  });

  it('a count metric gets no note: it counts events and does not read value', () => {
    render(
      <MetricComparisonTable
        metrics={[noValues('count')]}
        confidenceLevel={0.95}
        eventNames={{ 'm-count': 'the_event' }}
      />,
    );
    expect(screen.queryByTestId('no-value-note')).not.toBeInTheDocument();
  });
});

// C8 (PE-5): a count whose events carried no value is counted, so the page
// shows events per user above 0. The numbers are the server's for F4's
// dataset (backend test_a_count_metric_whose_events_carry_no_value_is_not_0_vs_0).
describe('a count whose events carried no value', () => {
  it('shows 1.50 against 2.00 events per user, not 0 against 0', () => {
    render(<MetricComparisonTable metrics={[meanMetric('count')]} confidenceLevel={0.95} />);
    const [controlRow, treatmentRow] = rowsOf(NAMES.count);
    expect(cells(controlRow)[3]).toBe('1.50events per user');
    expect(cells(treatmentRow)[3]).toBe('2.00events per user');
    expect(screen.getByTestId('metric-comparison-table').textContent).not.toContain(
      '0.00events per user',
    );
  });
});

describe('accessibility', () => {
  it('has no axe violations with count, duration and custom rows, footnotes and a note', async () => {
    const withNote = meanMetric('duration', { mean: 0, std_dev: 0, absolute_difference: 0 });
    withNote.metric_id = 'm-duration-2';
    withNote.variants[0] = { ...withNote.variants[0], mean: 0, std_dev: 0 };
    const { container } = render(
      <MetricComparisonTable
        metrics={[meanMetric('count'), meanMetric('custom'), withNote]}
        confidenceLevel={0.95}
        eventNames={{ 'm-duration-2': 'watch_session' }}
      />,
    );
    expect(screen.getByTestId('no-value-note')).toBeInTheDocument();
    const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations).toEqual([]);
  });
});
