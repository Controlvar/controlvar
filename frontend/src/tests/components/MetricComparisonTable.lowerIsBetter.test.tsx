/**
 * The results table reads a metric's direction (`lower_is_better`, #1377).
 *
 * What is pinned:
 *  - a significant change is "better" (green) or "worse" (red) in the metric's
 *    direction: a fall is better when lower is better, a rise when it is not;
 *  - the words are visible text in the cell, so colour is never the only
 *    signal; a change that is not significant gets no word and stays grey;
 *  - a response without `lower_is_better` (a server older than the field)
 *    judges nothing: a significant change is neutral, neither green nor red;
 *  - the metric cell says "lower is better" only for such a metric;
 *  - the column is headed "Change", and it still sorts on the signed change;
 *  - with a lower-is-better primary, the summary's winner and the table agree;
 *  - the table has no axe violations (colour contrast is not computable in
 *    jsdom).
 */
import React from 'react';
import axe from 'axe-core';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MetricComparisonTable } from '@/components/results/MetricComparison/MetricComparisonTable';
import { ExperimentSummary } from '@/components/results/ResultsDashboard/ExperimentSummary';
import { changeVerdict, metricLowerIsBetter } from '@/components/results/shared/resultFormat';
import { ExperimentResultsResponse, MetricResult, VariantResult } from '@/types/results';

function variant(
  id: string,
  name: string,
  pct: number | null,
  significant: boolean,
  mean = 0.1,
): VariantResult {
  const isControl = id === 'ctrl';
  return {
    variant_id: id,
    variant_name: name,
    is_control: isControl,
    sample_size: 2000,
    conversions: Math.round(mean * 2000),
    mean,
    std_dev: null,
    confidence_interval: null,
    p_value: isControl ? null : significant ? 0.001 : 0.4,
    adjusted_p_value: null,
    is_significant: significant,
    effect_size: null,
    effect_size_label: null,
    relative_improvement_pct: pct,
    power: null,
    statistical_test_used: isControl ? null : 'fisher_exact',
  };
}

/** One metric, four arms: a significant fall, a significant rise, and a rise that is not significant. */
function metric(direction: { lower_is_better?: boolean }, name = 'Uninstall'): MetricResult {
  return {
    metric_id: `m-${name}`,
    metric_name: name,
    metric_type: 'conversion',
    is_primary: true,
    ...direction,
    variants: [
      variant('ctrl', 'Control', null, false, 0.1),
      variant('down', 'Fell', -50.0, true, 0.05),
      variant('up', 'Rose', 40.0, true, 0.14),
      variant('flat', 'Unclear', 4.0, false, 0.104),
    ],
  };
}

function changeCell(variantName: string): HTMLElement {
  const row = screen.getByRole('row', { name: new RegExp(`\\b${variantName}\\b`) });
  return within(row).getByText(/^[+−]\d+(\.\d+)?%$/).closest('td') as HTMLElement;
}

function renderTable(metrics: MetricResult[]) {
  return render(<MetricComparisonTable metrics={metrics} confidenceLevel={0.95} correctionMethod="none" />);
}

describe('the change column follows lower_is_better', () => {
  it('lower is better: a significant fall is green "better", a significant rise red "worse"', () => {
    renderTable([metric({ lower_is_better: true })]);

    const fell = changeCell('Fell');
    expect(fell).toHaveTextContent(/^−50\.0% · better$/);
    expect(fell).toHaveClass('text-green-700');
    expect(fell).not.toHaveClass('text-red-700');

    const rose = changeCell('Rose');
    expect(rose).toHaveTextContent(/^\+40\.0% · worse$/);
    expect(rose).toHaveClass('text-red-700');
    expect(rose).not.toHaveClass('text-green-700');
  });

  it('higher is better: a significant rise is green "better", a significant fall red "worse"', () => {
    renderTable([metric({ lower_is_better: false }, 'Purchase')]);

    const rose = changeCell('Rose');
    expect(rose).toHaveTextContent(/^\+40\.0% · better$/);
    expect(rose).toHaveClass('text-green-700');

    const fell = changeCell('Fell');
    expect(fell).toHaveTextContent(/^−50\.0% · worse$/);
    expect(fell).toHaveClass('text-red-700');
  });

  it.each([true, false])(
    'a change that is not significant gets no word and stays grey (lower_is_better %s)',
    (lowerIsBetter) => {
      renderTable([metric({ lower_is_better: lowerIsBetter })]);
      const flat = changeCell('Unclear');
      expect(flat).toHaveTextContent(/^\+4\.0%$/);
      expect(flat).toHaveClass('text-slate-500');
      expect(flat).not.toHaveClass('text-green-700');
      expect(flat).not.toHaveClass('text-red-700');
    },
  );

  it('without lower_is_better in the response, a significant change is neutral and unjudged', () => {
    renderTable([metric({})]);
    for (const name of ['Fell', 'Rose']) {
      const cell = changeCell(name);
      expect(cell).toHaveClass('text-slate-700');
      expect(cell).not.toHaveClass('text-green-700');
      expect(cell).not.toHaveClass('text-red-700');
      expect(cell).not.toHaveTextContent(/better|worse/);
    }
    expect(screen.queryByText('lower is better')).not.toBeInTheDocument();
  });

  it('says "lower is better" beside the metric only when it is', () => {
    renderTable([metric({ lower_is_better: true }), metric({ lower_is_better: false }, 'Purchase')]);
    const tags = screen.getAllByText('lower is better');
    // One per row of the lower-is-better metric, none on the other metric's rows.
    expect(tags).toHaveLength(4);
    for (const tag of tags) {
      expect(tag.closest('tr')).toHaveTextContent(/^Uninstall/);
    }
  });

  it('is headed "Change", and still sorts on the signed change', async () => {
    renderTable([metric({ lower_is_better: true })]);
    expect(screen.getByRole('columnheader', { name: /^Change/ })).toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: /Improvement/ })).not.toBeInTheDocument();
    // The header row as the experiment-results docs journey reads it.
    expect(
      screen.getByRole('row', { name: 'Metric Variant Sample Size Value Change p-value Significance' }),
    ).toBeInTheDocument();

    await userEvent.click(within(screen.getByRole('columnheader', { name: /^Change/ })).getByRole('button'));
    const order = screen
      .getAllByRole('row')
      .slice(1)
      .map((row) => within(row).getAllByRole('cell')[1].textContent);
    // Ascending by the signed change, whatever the direction: the control
    // (no change) sorts first, then -50, +4, +40.
    expect(order).toEqual(['Control(control)', 'Fell', 'Unclear', 'Rose']);
  });
});

describe('changeVerdict', () => {
  const lower = metric({ lower_is_better: true });

  it('gives no verdict for the control, a zero change or a change that cannot be estimated', () => {
    expect(changeVerdict(lower, lower.variants[0])).toBeNull();
    expect(changeVerdict(lower, { ...lower.variants[1], relative_improvement_pct: 0 })).toBeNull();
    expect(changeVerdict(lower, { ...lower.variants[1], relative_improvement_pct: null })).toBeNull();
    expect(changeVerdict(lower, { ...lower.variants[1], relative_improvement_pct: NaN })).toBeNull();
  });

  it('reads only a boolean as the direction', () => {
    expect(metricLowerIsBetter(metric({ lower_is_better: true }))).toBe(true);
    expect(metricLowerIsBetter(metric({ lower_is_better: false }))).toBe(false);
    expect(metricLowerIsBetter(metric({}))).toBeUndefined();
    const nulled = { ...metric({}), lower_is_better: null } as unknown as MetricResult;
    expect(metricLowerIsBetter(nulled)).toBeUndefined();
  });
});

describe('a lower-is-better primary: the summary and the table agree', () => {
  // The shape the API answers for an uninstall rate that fell from 10% to 5%:
  // the treatment wins, and the recommendation says why in the metric's words.
  const SENTENCE =
    'Fell shows 50.0% improvement on Uninstall (p=0.0010): it fell by 50.0%, and lower is better for this metric.';
  const primary: MetricResult = {
    ...metric({ lower_is_better: true }),
    variants: [variant('ctrl', 'Control', null, false, 0.1), variant('down', 'Fell', -50.0, true, 0.05)],
  };
  const experiment: ExperimentResultsResponse = {
    experiment_id: 'exp-1',
    experiment_name: 'Onboarding',
    status: 'ACTIVE',
    start_date: '2026-10-01T00:00:00Z',
    end_date: null,
    confidence_level: 0.95,
    correction_method: 'none',
    sample_size_adequate: true,
    computed_at: '2026-10-20T12:00:00Z',
    summary: {
      total_users: 4000,
      total_events: 300,
      duration_days: 14,
      has_winner: true,
      winning_variant_id: 'down',
      recommendation: 'SHIP_VARIANT',
      recommendation_reason: SENTENCE,
    },
    metrics: [primary],
  };

  it('ships the variant whose uninstalls fell, and the table calls that fall better', () => {
    render(
      <>
        <ExperimentSummary experiment={experiment} />
        <MetricComparisonTable metrics={experiment.metrics} confidenceLevel={0.95} correctionMethod="none" />
      </>,
    );
    expect(screen.getByTestId('recommendation-pill')).toHaveTextContent('Ship Variant ✓');
    expect(screen.getByTestId('recommendation-reason')).toHaveTextContent(SENTENCE);
    const fell = changeCell('Fell');
    expect(fell).toHaveTextContent(/^−50\.0% · better$/);
    expect(fell).toHaveClass('text-green-700');
  });
});

describe('accessibility (axe-core in jsdom; colour contrast is not computable here)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  it('the table with a lower-is-better metric has no axe violations', async () => {
    const { container } = renderTable([metric({ lower_is_better: true }), metric({}, 'Purchase')]);
    const result = await axe.run(container, axeOptions);
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toStrictEqual(
      [],
    );
  });
});
