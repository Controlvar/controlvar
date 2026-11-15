/**
 * The summary card's impact line (#1496, S2-b): "Impact on {primary metric}:",
 * then one line per treatment, from server-computed metrics
 * (`fixtures/results-intervals.json`, written by
 * `python -m backend.scripts.generate_results_interval_fixtures`).
 *
 *  - ", not significant" after a treatment that is not;
 *  - the SRM qualifier when the server's check failed;
 *  - "unadjusted" on an interval exactly when that treatment's row shows an
 *    adjusted p-value;
 *  - nothing from a server that does not report the impact.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import axe from 'axe-core';
import { ExperimentSummary } from '@/components/results/ResultsDashboard/ExperimentSummary';
import { CorrectionMethod, ExperimentResultsResponse, MetricResult } from '@/types/results';
import fixtures from '@/tests/fixtures/results-intervals.json';
import { spokenText, visibleText } from '@/tests/helpers/visibleText';

interface Case {
  metric: MetricResult;
  confidence_level: number;
  correction_method: CorrectionMethod;
}

function serverCase(id: keyof typeof fixtures.cases): Case {
  return JSON.parse(JSON.stringify(fixtures.cases[id])) as Case;
}

function experiment(c: Case, overrides: Partial<ExperimentResultsResponse> = {}): ExperimentResultsResponse {
  return {
    experiment_id: 'exp-1',
    experiment_name: 'Checkout',
    status: 'ACTIVE',
    start_date: '2026-10-01T00:00:00Z',
    end_date: null,
    confidence_level: c.confidence_level,
    correction_method: c.correction_method,
    sample_size_adequate: true,
    computed_at: '2026-10-31T00:00:00Z',
    summary: {
      total_users: 2000,
      total_events: 100,
      duration_days: 14,
      has_winner: false,
      winning_variant_id: null,
      recommendation: 'CONTINUE_TESTING',
      recommendation_reason: 'Not significant yet.',
    },
    metrics: [c.metric],
    ...overrides,
  };
}

function lines(): string[] {
  return screen.getAllByTestId('summary-impact-line').map((p) => visibleText(p));
}

describe("the summary card's impact", () => {
  it('names the primary metric and gives each treatment its impact with its interval', () => {
    render(<ExperimentSummary experiment={experiment(serverCase('f1_wide_proportion'))} />);
    const block = screen.getByTestId('summary-impact');
    expect(block).toHaveTextContent(/^Impact on Signup:/);
    expect(lines()).toEqual(['treatment: +29 converting users (99% CI +16 to +46)']);
    expect(spokenText(screen.getByTestId('summary-impact-line'))).toBe(
      'treatment: +29 converting users (99% confidence interval +16 to +46)',
    );
  });

  it('adds ", not significant" to a treatment that is not', () => {
    render(<ExperimentSummary experiment={experiment(serverCase('f5_low_counts'))} />);
    expect(lines()).toEqual(['treatment: +4 converting users (95% CI −2 to +11), not significant']);
  });

  it('marks an interval "unadjusted" exactly when its row shows an adjusted p-value', () => {
    render(<ExperimentSummary experiment={experiment(serverCase('f8_three_variants_bh'))} />);
    expect(lines()).toEqual([
      'one_page: +31 converting users (95% CI, unadjusted, +3 to +59), not significant',
      'two_step: +4 converting users (95% CI, unadjusted, −23 to +31), not significant',
    ]);
  });

  it('adds the SRM qualifier when the sample-ratio check failed', () => {
    const c = serverCase('f1_wide_proportion');
    render(
      <ExperimentSummary
        experiment={experiment(c, {
          srm: { chi2: 40, p_value: 0.00001, warning: true, expected: {}, observed: {} },
        })}
      />,
    );
    expect(screen.getByTestId('summary-impact-srm')).toHaveTextContent(
      'The traffic split does not match the allocation, so treat this with caution.',
    );
  });

  it('has no SRM qualifier when the check passed or is missing', () => {
    const c = serverCase('f1_wide_proportion');
    render(
      <ExperimentSummary
        experiment={experiment(c, {
          srm: { chi2: 0.1, p_value: 0.7, warning: false, expected: {}, observed: {} },
        })}
      />,
    );
    expect(screen.queryByTestId('summary-impact-srm')).toBeNull();
  });

  it('reads "Not enough data to estimate" for a treatment with no users', () => {
    render(<ExperimentSummary experiment={experiment(serverCase('f6_empty_treatment_proportion'))} />);
    expect(lines()).toEqual(['treatment: Not enough data to estimate']);
  });

  it('is about the metric marked primary, not the first one', () => {
    const secondary = { ...serverCase('f10_count_events').metric, is_primary: false };
    const primary = serverCase('f10_revenue_values');
    render(<ExperimentSummary experiment={experiment(primary, { metrics: [secondary, primary.metric] })} />);
    expect(screen.getByTestId('summary-impact')).toHaveTextContent(/^Impact on Order value:/);
    expect(lines()).toEqual(['treatment: +7,600.00 in revenue (95% CI +4,463.73 to +10,736.27)']);
  });

  it('is not shown by a server that does not report the impact', () => {
    const c = serverCase('f1_wide_proportion');
    for (const v of c.metric.variants) {
      delete v.incremental_impact;
      delete v.incremental_impact_ci;
    }
    render(<ExperimentSummary experiment={experiment(c)} />);
    expect(screen.queryByTestId('summary-impact')).toBeNull();
  });

  it('has no axe violations', async () => {
    const { container } = render(
      <ExperimentSummary
        experiment={experiment(serverCase('f8_three_variants_bh'), {
          srm: { chi2: 40, p_value: 0.00001, warning: true, expected: {}, observed: {} },
        })}
      />,
    );
    const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations).toEqual([]);
  });
});
