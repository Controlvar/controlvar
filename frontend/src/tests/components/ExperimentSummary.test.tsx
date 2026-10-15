import React from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ExperimentSummary } from '@/components/results/ResultsDashboard/ExperimentSummary';
import { ExperimentResultsResponse, SampleSizeResult } from '@/types/results';

const baseExperiment: ExperimentResultsResponse = {
  experiment_id: 'exp-1',
  experiment_name: 'Homepage CTA Test',
  status: 'ACTIVE',
  start_date: '2024-01-01T00:00:00Z',
  end_date: null,
  confidence_level: 0.95,
  correction_method: 'bonferroni',
  sample_size_adequate: true,
  computed_at: '2024-01-15T12:00:00Z',
  summary: {
    total_users: 5000,
    total_events: 620,
    duration_days: 14,
    has_winner: false,
    winning_variant_id: null,
    recommendation: 'CONTINUE_TESTING',
    recommendation_reason: 'No statistically significant difference yet.',
  },
  metrics: [],
};

const winnerExperiment: ExperimentResultsResponse = {
  ...baseExperiment,
  summary: {
    ...baseExperiment.summary,
    has_winner: true,
    winning_variant_id: 'var-1',
    recommendation: 'SHIP_VARIANT',
  },
  metrics: [
    {
      metric_id: 'm1',
      metric_name: 'Conversion Rate',
      metric_type: 'conversion',
      is_primary: true,
      variants: [
        {
          variant_id: 'ctrl',
          variant_name: 'Control',
          is_control: true,
          sample_size: 2500,
          conversions: 250,
          mean: 0.10,
          std_dev: 0.04,
          confidence_interval: null,
          p_value: null,
          adjusted_p_value: null,
          is_significant: false,
          effect_size: null,
          effect_size_label: null,
          relative_improvement_pct: null,
          power: null,
        },
        {
          variant_id: 'var-1',
          variant_name: 'Variant B',
          is_control: false,
          sample_size: 2500,
          conversions: 370,
          mean: 0.148,
          std_dev: 0.045,
          confidence_interval: [0.13, 0.166],
          p_value: 0.001,
          adjusted_p_value: 0.001,
          is_significant: true,
          effect_size: 0.5,
          effect_size_label: 'medium',
          relative_improvement_pct: 48.0,
          power: 0.95,
        },
      ],
    },
  ],
};

describe('ExperimentSummary', () => {
  it('renders the experiment name', () => {
    render(<ExperimentSummary experiment={baseExperiment} />);
    expect(screen.getByText('Homepage CTA Test')).toBeInTheDocument();
  });

  it('renders the status badge', () => {
    render(<ExperimentSummary experiment={baseExperiment} />);
    expect(screen.getByText('ACTIVE')).toBeInTheDocument();
  });

  it('renders total users', () => {
    render(<ExperimentSummary experiment={baseExperiment} />);
    expect(screen.getByText('5,000')).toBeInTheDocument();
  });

  it('renders duration in days', () => {
    render(<ExperimentSummary experiment={baseExperiment} />);
    // Duration appears in the stat grid and the subtitle
    expect(screen.getAllByText(/14/)[0]).toBeInTheDocument();
  });

  it('renders CONTINUE_TESTING recommendation', () => {
    render(<ExperimentSummary experiment={baseExperiment} />);
    expect(screen.getByTestId('recommendation-pill')).toHaveTextContent(
      'Continue Testing'
    );
    expect(screen.getByTestId('recommendation-pill')).toHaveClass('bg-amber-100');
  });

  it('renders SHIP_VARIANT recommendation with green styling', () => {
    render(<ExperimentSummary experiment={winnerExperiment} />);
    expect(screen.getByTestId('recommendation-pill')).toHaveTextContent('Ship Variant');
    expect(screen.getByTestId('recommendation-pill')).toHaveClass('bg-green-100');
  });

  it('renders winner indicator when has_winner is true', () => {
    render(<ExperimentSummary experiment={winnerExperiment} />);
    expect(screen.getByTestId('winner-indicator')).toBeInTheDocument();
    expect(screen.getByText('Variant B')).toBeInTheDocument();
  });

  it('does not render winner indicator when no winner', () => {
    render(<ExperimentSummary experiment={baseExperiment} />);
    expect(screen.queryByTestId('winner-indicator')).not.toBeInTheDocument();
  });

  it('has the experiment-summary data-testid', () => {
    render(<ExperimentSummary experiment={baseExperiment} />);
    expect(screen.getByTestId('experiment-summary')).toBeInTheDocument();
  });

  // #1244: the card reads Reached only when the smallest variant has the
  // planned sample per variant, the Sample Size tab's figure. The server's
  // sample_size_adequate (the floor the recommendation needs, 100 users
  // unless the metric sets another) does not decide it.
  describe('Minimum sample card', () => {
    const plan = (over: Partial<SampleSizeResult>): SampleSizeResult => ({
      required_sample_size_per_variant: 3000,
      current_sample_size_per_variant: 4000,
      is_adequate: true,
      achieved_power: 0.9,
      days_to_significance: null,
      projected_completion_date: null,
      baseline_rate: 0.12,
      mde: 0.05,
      confidence_level: 0.95,
      power_target: 0.8,
      baseline_source: 'observed',
      baseline_users: 4000,
      metric_id: 'm1',
      metric_name: 'Conversion Rate',
      metric_type: 'conversion',
      analysed_as: 'conversion',
      alpha: 0.05,
      comparisons: 1,
      correction_method: 'none',
      mde_absolute: 0.006,
      unavailable_reason: null,
      guide_only_reasons: [],
      ...over,
    });

    it('reads "Reached" when the smallest variant has the planned sample', () => {
      render(<ExperimentSummary experiment={baseExperiment} plannedSample={plan({})} />);
      const card = screen.getByTestId('minimum-sample');
      expect(within(card).getByText('Minimum sample')).toBeInTheDocument();
      expect(within(card).getByText('Reached')).toHaveClass('text-green-700');
      expect(card).toHaveTextContent(/^Minimum sampleReached4,000 of 3,000 per variant$/);
    });

    it('reads "Not yet" with the progress when the plan is not met (432 users, power 0.34)', () => {
      // The issue's experiment: Control 18/203, Treatment 31/229. The floor is
      // met (sample_size_adequate true), the plan is not.
      render(
        <ExperimentSummary
          experiment={{ ...baseExperiment, sample_size_adequate: true }}
          plannedSample={plan({
            required_sample_size_per_variant: 69410,
            current_sample_size_per_variant: 203,
            is_adequate: false,
            achieved_power: 0.34,
            baseline_rate: 0.0887,
          })}
        />
      );
      const card = screen.getByTestId('minimum-sample');
      expect(card).not.toHaveTextContent('Reached');
      expect(within(card).getByText('Not yet')).toHaveClass('text-amber-700');
      expect(card).toHaveTextContent(/^Minimum sampleNot yet203 of 69,410 per variant$/);
    });

    it('never reads "Reached" at 0 events, when there is no plan yet', () => {
      render(
        <ExperimentSummary
          experiment={{
            ...baseExperiment,
            sample_size_adequate: true,
            summary: { ...baseExperiment.summary, total_events: 0, duration_days: 0 },
          }}
          plannedSample={plan({
            required_sample_size_per_variant: null,
            current_sample_size_per_variant: 0,
            is_adequate: false,
            achieved_power: null,
            baseline_rate: null,
            baseline_source: null,
            unavailable_reason: 'no_control_conversions',
          })}
        />
      );
      const card = screen.getByTestId('minimum-sample');
      expect(card).toHaveTextContent(/^Minimum sampleNot yet0 per variant, no plan yet$/);
    });

    it('never reads "Reached" while the plan loads or when it could not be loaded', () => {
      const { rerender } = render(<ExperimentSummary experiment={baseExperiment} />);
      expect(screen.getByTestId('minimum-sample')).toHaveTextContent(/^Minimum sampleLoading$/);
      rerender(<ExperimentSummary experiment={baseExperiment} plannedSample={null} />);
      expect(screen.getByTestId('minimum-sample')).toHaveTextContent(
        /^Minimum sampleNot known$/
      );
    });

    it('no longer uses the old "Sample Size: Adequate / Insufficient" wording', () => {
      const { rerender } = render(
        <ExperimentSummary experiment={baseExperiment} plannedSample={plan({})} />
      );
      const summary = screen.getByTestId('experiment-summary');
      expect(summary).not.toHaveTextContent(/Adequate|Insufficient/);
      expect(within(summary).queryByText('Sample Size')).not.toBeInTheDocument();
      rerender(
        <ExperimentSummary
          experiment={baseExperiment}
          plannedSample={plan({ is_adequate: false, current_sample_size_per_variant: 10 })}
        />
      );
      expect(screen.getByTestId('experiment-summary')).not.toHaveTextContent(
        /Adequate|Insufficient/
      );
    });

    it('offers a link to the Sample Size tab that calls the handler', async () => {
      const onOpen = jest.fn();
      render(
        <ExperimentSummary experiment={baseExperiment} onOpenSampleSize={onOpen} />
      );
      const link = screen.getByRole('button', {
        name: 'See the Sample Size tab for the planned sample.',
      });
      await userEvent.click(link);
      expect(onOpen).toHaveBeenCalledTimes(1);
    });

    it('shows no link when there is no tab to open', () => {
      render(<ExperimentSummary experiment={baseExperiment} />);
      expect(screen.queryByTestId('open-sample-size-tab')).not.toBeInTheDocument();
    });
  });
});
