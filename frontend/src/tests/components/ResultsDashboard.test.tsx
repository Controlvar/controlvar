import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ResultsDashboard } from '@/components/results/ResultsDashboard/ResultsDashboard';
import { ResultsService } from '@/services/results';
import { ExperimentsService } from '@/services/experiments';
import { ApiError } from '@/services/api';
import {
  ExperimentResultsResponse,
  DailyResultsResponse,
  SampleSizeResult,
  DimensionalBreakdownResponse,
} from '@/types/results';
import { SequentialTestingResponse } from '@/types/sequential';

jest.mock('@/services/results');
jest.mock('@/services/experiments');

const mockGetResults = ResultsService.getResults as jest.Mock;
const mockGetDailyResults = ResultsService.getDailyResults as jest.Mock;
const mockGetSampleSize = ResultsService.getSampleSize as jest.Mock;
const mockGetExperiment = ExperimentsService.get as jest.Mock;

const mockResults: ExperimentResultsResponse = {
  experiment_id: 'exp-1',
  experiment_name: 'Test Experiment',
  status: 'ACTIVE',
  start_date: '2024-01-01T00:00:00Z',
  end_date: null,
  confidence_level: 0.95,
  correction_method: 'bonferroni',
  sample_size_adequate: true,
  computed_at: '2024-01-15T12:00:00Z',
  summary: {
    total_users: 2000,
    total_events: 240,
    duration_days: 7,
    has_winner: false,
    winning_variant_id: null,
    recommendation: 'CONTINUE_TESTING',
    recommendation_reason: 'No statistically significant difference yet.',
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
          sample_size: 1000,
          conversions: 120,
          mean: 0.12,
          std_dev: 0.05,
          confidence_interval: [0.10, 0.14],
          p_value: null,
          adjusted_p_value: null,
          is_significant: false,
          effect_size: null,
          effect_size_label: null,
          relative_improvement_pct: null,
          power: null,
        },
      ],
    },
  ],
};

const mockDaily: DailyResultsResponse = {
  experiment_id: 'exp-1',
  metric_id: null,
  series: [],
};

const mockSampleSize: SampleSizeResult = {
  required_sample_size_per_variant: 3000,
  current_sample_size_per_variant: 4000,
  is_adequate: true,
  achieved_power: 0.90,
  days_to_significance: null,
  projected_completion_date: null,
  baseline_rate: 0.12,
  mde: 0.02,
  confidence_level: 0.95,
  power_target: 0.8,
  baseline_source: 'observed',
  baseline_users: 4000,
  metric_id: 'metric-1',
  metric_name: 'Conversion',
  metric_type: 'conversion',
  analysed_as: 'conversion',
  alpha: 0.05,
  comparisons: 1,
  correction_method: 'none',
  mde_absolute: 0.0024,
  unavailable_reason: null,
  guide_only_reasons: [],
};

const mockBreakdown: DimensionalBreakdownResponse = {
  dimension: 'platform',
  is_exploratory: true,
  adjusted_alpha: 0.0167,
  has_heterogeneous_effects: false,
  hte_warning: null,
  segments: [
    {
      segment_value: 'ios',
      sample_size: 300,
      variants: [
        {
          variant_id: 'ctrl',
          variant_name: 'Control',
          is_control: true,
          sample_size: 150,
          conversions: 15,
          mean: 0.1,
          confidence_interval: [0.057, 0.143],
          p_value: null,
          is_significant: false,
        },
      ],
    },
  ],
};

beforeEach(() => {
  jest.clearAllMocks();
});

describe('ResultsDashboard', () => {
  it('shows loading state while fetching', () => {
    // Never resolves
    mockGetResults.mockImplementation(() => new Promise(() => {}));
    mockGetDailyResults.mockImplementation(() => new Promise(() => {}));
    mockGetSampleSize.mockImplementation(() => new Promise(() => {}));

    render(<ResultsDashboard experimentId="exp-1" />);
    expect(screen.getByTestId('loading-skeleton')).toBeInTheDocument();
  });

  it('shows error state on fetch failure', async () => {
    mockGetResults.mockRejectedValue(new Error('Network error'));
    mockGetDailyResults.mockRejectedValue(new Error('Network error'));
    mockGetSampleSize.mockRejectedValue(new Error('Network error'));

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() =>
      expect(screen.getByTestId('error-state')).toBeInTheDocument()
    );
    // Fixed copy: a plain Error's message is not the page's to show.
    expect(screen.getByText('The results could not be loaded.')).toBeInTheDocument();
    expect(screen.queryByText(/network error/i)).not.toBeInTheDocument();
  });

  it('renders ExperimentSummary on success', async () => {
    mockGetResults.mockResolvedValue(mockResults);
    mockGetDailyResults.mockResolvedValue(mockDaily);
    mockGetSampleSize.mockResolvedValue(mockSampleSize);

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() =>
      expect(screen.getByTestId('experiment-summary')).toBeInTheDocument()
    );
  });

  it('renders tab navigation', async () => {
    mockGetResults.mockResolvedValue(mockResults);
    mockGetDailyResults.mockResolvedValue(mockDaily);
    mockGetSampleSize.mockResolvedValue(mockSampleSize);

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByRole('tablist'));

    expect(screen.getByRole('tab', { name: /overview/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /trends/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /sample size/i })).toBeInTheDocument();
  });

  it('offers no CUPED tab, toggle or control: CUPED-adjusted results are API only', async () => {
    mockGetResults.mockResolvedValue(mockResults);
    mockGetDailyResults.mockResolvedValue(mockDaily);
    mockGetSampleSize.mockResolvedValue(mockSampleSize);

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByRole('tablist'));

    for (const role of ['tab', 'switch', 'checkbox', 'button', 'radio'] as const) {
      expect(screen.queryByRole(role, { name: /cuped/i })).toBeNull();
    }
    expect(screen.queryByText(/cuped/i)).toBeNull();
  });

  it('switches to trends tab when clicked', async () => {
    mockGetResults.mockResolvedValue(mockResults);
    mockGetDailyResults.mockResolvedValue(mockDaily);
    mockGetSampleSize.mockResolvedValue(mockSampleSize);

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByRole('tablist'));

    await userEvent.click(screen.getByRole('tab', { name: /trends/i }));
    expect(screen.getByRole('tab', { name: /trends/i })).toHaveAttribute(
      'aria-selected',
      'true'
    );
    // With empty series, TrendChart renders the empty state
    expect(
      screen.getByTestId('trend-empty') || screen.queryByTestId('trend-chart')
    ).toBeTruthy();
  });

  it('switches to sample size tab when clicked', async () => {
    mockGetResults.mockResolvedValue(mockResults);
    mockGetDailyResults.mockResolvedValue(mockDaily);
    mockGetSampleSize.mockResolvedValue(mockSampleSize);

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByRole('tablist'));

    await userEvent.click(screen.getByRole('tab', { name: /sample size/i }));
    expect(screen.getByTestId('sample-size-meter')).toBeInTheDocument();
  });

  it('calls all three service methods with the experiment id', async () => {
    mockGetResults.mockResolvedValue(mockResults);
    mockGetDailyResults.mockResolvedValue(mockDaily);
    mockGetSampleSize.mockResolvedValue(mockSampleSize);

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByTestId('experiment-summary'));

    expect(mockGetResults).toHaveBeenCalledWith('exp-1');
    expect(mockGetDailyResults).toHaveBeenCalledWith('exp-1');
    expect(mockGetSampleSize).toHaveBeenCalledWith('exp-1');
  });

  it('shows retry button on error and re-fetches on click', async () => {
    mockGetResults
      .mockRejectedValueOnce(new Error('fail'))
      .mockResolvedValue(mockResults);
    mockGetDailyResults
      .mockRejectedValueOnce(new Error('fail'))
      .mockResolvedValue(mockDaily);
    mockGetSampleSize
      .mockRejectedValueOnce(new Error('fail'))
      .mockResolvedValue(mockSampleSize);

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByTestId('error-state'));

    await userEvent.click(screen.getByRole('button', { name: /retry/i }));

    await waitFor(() =>
      expect(screen.getByTestId('experiment-summary')).toBeInTheDocument()
    );
  });

  // #666: the sample size loads on its own. Its failure stays inside the
  // Sample Size tab; it must not blank the page.
  describe('when the sample-size request fails', () => {
    const serverError = () =>
      new ApiError({ status: 500, detail: 'The sample size could not be computed.' });

    it('still renders the Overview, and the Sample Size tab shows the error', async () => {
      mockGetResults.mockResolvedValue(mockResults);
      mockGetDailyResults.mockResolvedValue(mockDaily);
      mockGetSampleSize.mockRejectedValue(serverError());

      render(<ResultsDashboard experimentId="exp-1" />);
      await waitFor(() =>
        expect(screen.getByTestId('experiment-summary')).toBeInTheDocument()
      );
      expect(screen.queryByTestId('error-state')).not.toBeInTheDocument();
      expect(screen.getByRole('tab', { name: /overview/i })).toHaveAttribute(
        'aria-selected',
        'true'
      );
      expect(screen.getByText('All Metrics')).toBeInTheDocument();

      await userEvent.click(screen.getByRole('tab', { name: /sample size/i }));
      const alert = await screen.findByTestId('sample-size-error');
      expect(alert).toHaveAttribute('role', 'alert');
      expect(alert).toHaveTextContent('The sample size could not be loaded.');
      expect(alert).toHaveTextContent('The sample size could not be computed.');
      expect(screen.queryByTestId('sample-size-meter')).not.toBeInTheDocument();
      // Only the tab reports it; the summary card above is untouched.
      expect(screen.getByTestId('experiment-summary')).toBeInTheDocument();
    });

    it('lets the user try the tab again without reloading the page', async () => {
      mockGetResults.mockResolvedValue(mockResults);
      mockGetDailyResults.mockResolvedValue(mockDaily);
      mockGetSampleSize
        .mockRejectedValueOnce(serverError())
        .mockResolvedValue(mockSampleSize);

      render(<ResultsDashboard experimentId="exp-1" />);
      await waitFor(() => screen.getByRole('tablist'));
      await userEvent.click(screen.getByRole('tab', { name: /sample size/i }));
      await screen.findByTestId('sample-size-error');

      await userEvent.click(screen.getByRole('button', { name: /try again/i }));
      expect(await screen.findByTestId('sample-size-meter')).toBeInTheDocument();
      expect(screen.queryByTestId('sample-size-error')).not.toBeInTheDocument();
      expect(mockGetResults).toHaveBeenCalledTimes(1);
    });
  });

  it('renders the page before the sample size answers, and the tab once it does', async () => {
    let resolveSampleSize: (v: SampleSizeResult) => void = () => {};
    mockGetResults.mockResolvedValue(mockResults);
    mockGetDailyResults.mockResolvedValue(mockDaily);
    mockGetSampleSize.mockImplementation(
      () => new Promise<SampleSizeResult>((resolve) => (resolveSampleSize = resolve))
    );

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() =>
      expect(screen.getByTestId('experiment-summary')).toBeInTheDocument()
    );
    await userEvent.click(screen.getByRole('tab', { name: /sample size/i }));
    expect(screen.getByTestId('sample-size-loading')).toBeInTheDocument();

    resolveSampleSize(mockSampleSize);
    expect(await screen.findByTestId('sample-size-meter')).toBeInTheDocument();
  });

  it('opens the Sample Size tab from the Overview card link', async () => {
    mockGetResults.mockResolvedValue(mockResults);
    mockGetDailyResults.mockResolvedValue(mockDaily);
    mockGetSampleSize.mockResolvedValue(mockSampleSize);

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByTestId('experiment-summary'));
    await userEvent.click(
      screen.getByRole('button', { name: 'See the Sample Size tab for the planned sample.' })
    );
    expect(screen.getByRole('tab', { name: /sample size/i })).toHaveAttribute(
      'aria-selected',
      'true'
    );
    expect(await screen.findByTestId('sample-size-meter')).toBeInTheDocument();
  });

  // #666 (4a): the dashboard sends no fixed values. On load it sends nothing
  // but the id, so the server plans from the observed control rate; after an
  // edit it sends exactly what the user changed, and never touches the URL.
  describe('the Sample Size tab sends only what the user changed', () => {
    it('asks with the experiment id alone on load', async () => {
      mockGetResults.mockResolvedValue(mockResults);
      mockGetDailyResults.mockResolvedValue(mockDaily);
      mockGetSampleSize.mockResolvedValue(mockSampleSize);

      render(<ResultsDashboard experimentId="exp-1" />);
      await waitFor(() => screen.getByTestId('experiment-summary'));
      expect(mockGetSampleSize).toHaveBeenCalledTimes(1);
      expect(mockGetSampleSize.mock.calls[0]).toEqual(['exp-1']);
    });

    it('recalculates with exactly the edited inputs, and leaves the URL alone', async () => {
      mockGetResults.mockResolvedValue(mockResults);
      mockGetDailyResults.mockResolvedValue(mockDaily);
      mockGetSampleSize
        .mockResolvedValueOnce(mockSampleSize)
        .mockResolvedValue({ ...mockSampleSize, mde: 0.1, baseline_rate: 0.15, baseline_source: 'request' });
      const urlBefore = window.location.href;

      render(<ResultsDashboard experimentId="exp-1" />);
      await waitFor(() => screen.getByRole('tablist'));
      await userEvent.click(screen.getByRole('tab', { name: /sample size/i }));
      await screen.findByTestId('sample-size-meter');

      const baseline = screen.getByLabelText('Baseline conversion rate (%)');
      await userEvent.clear(baseline);
      await userEvent.type(baseline, '15');
      const mde = screen.getByLabelText('Minimum detectable effect, relative (%)');
      await userEvent.clear(mde);
      await userEvent.type(mde, '10');
      await userEvent.click(screen.getByRole('button', { name: 'Recalculate' }));

      await waitFor(() => expect(mockGetSampleSize).toHaveBeenCalledTimes(2));
      expect(mockGetSampleSize).toHaveBeenLastCalledWith('exp-1', {
        baseline_conversion_rate: 0.15,
        mde: 0.1,
      });
      expect(await screen.findByTestId('sample-size-baseline-source')).toHaveTextContent(
        'Entered by you.'
      );
      expect(window.location.href).toBe(urlBefore);
      expect(mockGetResults).toHaveBeenCalledTimes(1);
    });

    // #1244: the Overview card reads the plan with the server's inputs, and
    // what the user tries on the tab does not move it.
    it('feeds the Minimum sample card the plan, and keeps it when the user recalculates', async () => {
      mockGetResults.mockResolvedValue({ ...mockResults, sample_size_adequate: true });
      mockGetDailyResults.mockResolvedValue(mockDaily);
      mockGetSampleSize
        .mockResolvedValueOnce({
          ...mockSampleSize,
          required_sample_size_per_variant: 69410,
          current_sample_size_per_variant: 203,
          is_adequate: false,
          achieved_power: 0.34,
        })
        .mockResolvedValue({ ...mockSampleSize, mde: 0.9, baseline_source: 'request' });

      render(<ResultsDashboard experimentId="exp-1" />);
      const card = await screen.findByTestId('minimum-sample');
      await waitFor(() =>
        expect(card).toHaveTextContent(/^Minimum sampleNot yet203 of 69,410 per variant$/)
      );

      await userEvent.click(screen.getByRole('tab', { name: /sample size/i }));
      await screen.findByTestId('sample-size-meter');
      const mde = screen.getByLabelText('Minimum detectable effect, relative (%)');
      await userEvent.clear(mde);
      await userEvent.type(mde, '90');
      await userEvent.click(screen.getByRole('button', { name: 'Recalculate' }));
      await waitFor(() => expect(mockGetSampleSize).toHaveBeenCalledTimes(2));
      await screen.findByTestId('sample-size-meter');
      expect(screen.getByTestId('minimum-sample')).toHaveTextContent(
        /^Minimum sampleNot yet203 of 69,410 per variant$/
      );
    });

    it('renders the meter for an experiment with no traffic yet', async () => {
      mockGetResults.mockResolvedValue(mockResults);
      mockGetDailyResults.mockResolvedValue(mockDaily);
      mockGetSampleSize.mockResolvedValue({
        ...mockSampleSize,
        required_sample_size_per_variant: null,
        current_sample_size_per_variant: 0,
        is_adequate: false,
        achieved_power: null,
        baseline_rate: null,
        baseline_source: null,
        baseline_users: null,
        mde_absolute: null,
        unavailable_reason: 'no_control_data',
      });

      render(<ResultsDashboard experimentId="exp-1" />);
      await waitFor(() => screen.getByRole('tablist'));
      await userEvent.click(screen.getByRole('tab', { name: /sample size/i }));
      expect(await screen.findByTestId('sample-size-meter')).toBeInTheDocument();
      expect(screen.getByTestId('sample-size-no-traffic')).toBeInTheDocument();
    });
  });

  // Issue #28: Breakdowns tab tests
  it('renders Breakdowns tab in the tab list', async () => {
    mockGetResults.mockResolvedValue(mockResults);
    mockGetDailyResults.mockResolvedValue(mockDaily);
    mockGetSampleSize.mockResolvedValue(mockSampleSize);

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByRole('tablist'));

    expect(screen.getByRole('tab', { name: /breakdowns/i })).toBeInTheDocument();
  });

  it('switches to the Breakdowns tab and renders the dimension selector', async () => {
    mockGetResults.mockResolvedValue(mockResults);
    mockGetDailyResults.mockResolvedValue(mockDaily);
    mockGetSampleSize.mockResolvedValue(mockSampleSize);

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByRole('tablist'));

    await userEvent.click(screen.getByRole('tab', { name: /breakdowns/i }));

    expect(screen.getByTestId('breakdown-selector')).toBeInTheDocument();
  });

  it('shows prompt to select a dimension when no dimension is selected', async () => {
    mockGetResults.mockResolvedValue(mockResults);
    mockGetDailyResults.mockResolvedValue(mockDaily);
    mockGetSampleSize.mockResolvedValue(mockSampleSize);

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByRole('tablist'));
    await userEvent.click(screen.getByRole('tab', { name: /breakdowns/i }));

    expect(
      screen.getByText(/select a dimension above to view segment-level results/i)
    ).toBeInTheDocument();
  });

  it('fetches breakdown and renders SegmentComparisonTable when a dimension is selected', async () => {
    mockGetResults
      .mockResolvedValueOnce(mockResults)
      // second call with breakdown param returns breakdown data
      .mockResolvedValueOnce({ ...mockResults, breakdown: mockBreakdown });
    mockGetDailyResults.mockResolvedValue(mockDaily);
    mockGetSampleSize.mockResolvedValue(mockSampleSize);

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByRole('tablist'));
    await userEvent.click(screen.getByRole('tab', { name: /breakdowns/i }));

    await userEvent.selectOptions(
      screen.getByTestId('breakdown-selector'),
      'platform'
    );

    await waitFor(() =>
      expect(screen.getByTestId('segment-comparison-table')).toBeInTheDocument()
    );
  });

  it('calls getResults with breakdown param when a dimension is selected', async () => {
    mockGetResults
      .mockResolvedValueOnce(mockResults)
      .mockResolvedValueOnce({ ...mockResults, breakdown: mockBreakdown });
    mockGetDailyResults.mockResolvedValue(mockDaily);
    mockGetSampleSize.mockResolvedValue(mockSampleSize);

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByRole('tablist'));
    await userEvent.click(screen.getByRole('tab', { name: /breakdowns/i }));

    await userEvent.selectOptions(
      screen.getByTestId('breakdown-selector'),
      'country'
    );

    await waitFor(() =>
      expect(mockGetResults).toHaveBeenCalledWith('exp-1', { breakdown: 'country' })
    );
  });
});

// #580 / D49: the dashboard sends the experiment's stored correction method and
// confidence level with every results request. Non-default values (none, 0.9),
// so a dashboard that sent nothing, or the defaults, would fail.
describe('the stored analysis settings', () => {
  const stored = { id: 'exp-1', correction_method: 'none', confidence_level: 0.9 };

  function variant(id: string, name: string, p: number | null, adjusted: number | null) {
    return {
      variant_id: id,
      variant_name: name,
      is_control: false,
      sample_size: 1000,
      conversions: 130,
      mean: 0.13,
      std_dev: null,
      confidence_interval: null,
      p_value: p,
      adjusted_p_value: adjusted,
      is_significant: false,
      effect_size: null,
      effect_size_label: null,
      relative_improvement_pct: 8,
      power: null,
    };
  }

  /** A response with a control and the given treatments on one metric. */
  function withTreatments(
    treatments: ReturnType<typeof variant>[],
    overrides: Partial<ExperimentResultsResponse> = {}
  ): ExperimentResultsResponse {
    return {
      ...mockResults,
      metrics: [{ ...mockResults.metrics[0], variants: [mockResults.metrics[0].variants[0], ...treatments] }],
      ...overrides,
    };
  }

  function loads(results: ExperimentResultsResponse = mockResults) {
    mockGetResults.mockResolvedValue(results);
    mockGetDailyResults.mockResolvedValue(mockDaily);
    mockGetSampleSize.mockResolvedValue(mockSampleSize);
  }

  it('sends them on the initial load and on a breakdown', async () => {
    mockGetExperiment.mockResolvedValue(stored);
    loads();
    mockGetResults.mockResolvedValueOnce(mockResults).mockResolvedValue({
      ...mockResults,
      breakdown: mockBreakdown,
    });

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByTestId('experiment-summary'));
    expect(mockGetExperiment).toHaveBeenCalledWith('exp-1');
    expect(mockGetResults).toHaveBeenCalledTimes(1);
    expect(mockGetResults.mock.calls[0]).toEqual([
      'exp-1',
      { correction_method: 'none', confidence_level: 0.9 },
    ]);

    await userEvent.click(screen.getByRole('tab', { name: /breakdowns/i }));
    await userEvent.selectOptions(screen.getByRole('combobox'), 'platform');
    await waitFor(() => expect(mockGetResults).toHaveBeenCalledTimes(2));
    expect(mockGetResults.mock.calls[1]).toEqual([
      'exp-1',
      { breakdown: 'platform', correction_method: 'none', confidence_level: 0.9 },
    ]);
  });

  it('still asks for the results, with no settings, when the experiment cannot be read (PE condition 14)', async () => {
    mockGetExperiment.mockRejectedValue(new ApiError({ status: 500, detail: 'boom' }));
    loads();

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByTestId('experiment-summary'));
    expect(screen.queryByTestId('error-state')).not.toBeInTheDocument();
    expect(mockGetResults).toHaveBeenCalledTimes(1);
    // Exactly the id: the server then uses the experiment's stored settings.
    expect(mockGetResults.mock.calls[0]).toEqual(['exp-1']);
    // The summary still says what the results were computed with.
    expect(screen.getByTestId('analysis-summary-text')).toHaveTextContent('95% confidence');
  });

  it('shows a stored 92% truthfully on the Analysis row and the Sample Size tab (PE condition 14)', async () => {
    mockGetExperiment.mockResolvedValue({ ...stored, correction_method: 'benjamini_hochberg', confidence_level: 0.92 });
    loads({ ...mockResults, confidence_level: 0.92, correction_method: 'benjamini_hochberg' });
    mockGetSampleSize.mockResolvedValue({ ...mockSampleSize, confidence_level: 0.92, alpha: 0.08 });

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByTestId('experiment-summary'));
    expect(mockGetResults.mock.calls[0]).toEqual([
      'exp-1',
      { correction_method: 'benjamini_hochberg', confidence_level: 0.92 },
    ]);
    expect(screen.getByTestId('analysis-summary-text')).toHaveTextContent(/^92% confidence/);
    expect(screen.queryByTestId('analysis-summary-override')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('tab', { name: /sample size/i }));
    await screen.findByTestId('sample-size-meter');
    const select = screen.getByLabelText('Significance (two-sided)') as HTMLSelectElement;
    expect(select.selectedOptions[0].textContent).toBe("8% (this experiment's setting)");
    expect(screen.getByTestId('sample-size-required')).toHaveTextContent('8% significance');
  });

  it('says so when the results were computed with other settings than the stored ones', async () => {
    mockGetExperiment.mockResolvedValue({ ...stored, correction_method: 'benjamini_hochberg', confidence_level: 0.95 });
    loads({ ...mockResults, correction_method: 'none', confidence_level: 0.95 });

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByTestId('experiment-summary'));
    expect(screen.getByTestId('analysis-summary-override')).toHaveTextContent(
      'Shown with 95% confidence · no correction; this experiment is set to 95% confidence · Benjamini-Hochberg correction.'
    );
  });

  describe('the Analysis row', () => {
    it.each([
      [
        'two corrected comparisons',
        [variant('b', 'B', 0.02, 0.04), variant('c', 'C', 0.04, 0.04)],
        'benjamini_hochberg',
        '95% confidence · Benjamini-Hochberg correction for the 2 comparisons with the control on each metric',
      ],
      [
        'one comparison',
        [variant('b', 'B', 0.02, 0.02)],
        'benjamini_hochberg',
        '95% confidence · one comparison with the control on each metric, so no correction is needed',
      ],
      [
        'three variants, one without a p-value (k = 1)',
        [variant('b', 'B', 0.02, 0.02), variant('c', 'C', null, null)],
        'bonferroni',
        '95% confidence · one comparison with the control on each metric, so no correction is needed',
      ],
      [
        'two comparisons and no correction',
        [variant('b', 'B', 0.02, null), variant('c', 'C', 0.04, null)],
        'none',
        '95% confidence · no correction (chosen for this experiment)',
      ],
    ])('%s', async (_label, treatments, method, text) => {
      loads(withTreatments(treatments, { correction_method: method as 'none' }));
      render(<ResultsDashboard experimentId="exp-1" />);
      await waitFor(() => screen.getByTestId('experiment-summary'));
      expect(screen.getByTestId('analysis-summary-text')).toHaveTextContent(text);
    });
  });

  // #580 showed a notice for one release, saying that multi-variant results
  // had started to use the experiment's correction. #821 removed it: two
  // corrected comparisons, the case it was shown for, now show no notice.
  it.each([
    ['Benjamini-Hochberg', [variant('b', 'B', 0.02, 0.04), variant('c', 'C', 0.04, 0.04)], 'benjamini_hochberg'],
    ['Bonferroni', [variant('b', 'B', 0.02, 0.04), variant('c', 'C', 0.04, 0.08)], 'bonferroni'],
  ])('shows no one-release notice for two comparisons corrected with %s', async (label, treatments, method) => {
    loads(withTreatments(treatments, { correction_method: method as 'none' }));
    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByTestId('experiment-summary'));
    // The case the notice was shown for: two comparisons, corrected.
    expect(screen.getByTestId('analysis-summary-text')).toHaveTextContent(
      `95% confidence · ${label} correction for the 2 comparisons with the control on each metric`
    );
    expect(screen.queryByTestId('corrected-results-notice')).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Change to these results' })).not.toBeInTheDocument();
    expect(screen.queryByText(/earlier versions showed them uncorrected/)).not.toBeInTheDocument();
  });
});

describe('the sequential block (#919)', () => {
  // Since the results response carries `sequential_testing` whenever
  // sequential testing is on, the dedicated route is asked only when the
  // experiment says it is on and the block is still missing. Asking it when
  // it is off answered 404 and logged a failed request on every results page.
  const mockGetSequential = ResultsService.getSequentialResults as jest.Mock;
  const stored = { id: 'exp-1', correction_method: 'none', confidence_level: 0.95 };

  const SEQUENTIAL: SequentialTestingResponse = {
    method: 'msprt',
    msprt_result: {
      lambda_ratio: 1.2,
      always_valid_p_value: 0.4,
      can_stop: false,
      evidence_strength: 'inconclusive',
      boundary: 20,
    },
    confidence_sequence: { lower: -0.01, upper: 0.03, width: 0.04, sample_size: 2000 },
    evidence_trajectory: [],
    alpha_spending: [],
    long_running_risk: null,
    recommended_action: 'continue',
    at_risk: false,
    analysis_status: 'beta',
    analysis_notice: 'Beta',
  };

  function loads(results: ExperimentResultsResponse) {
    mockGetResults.mockResolvedValue(results);
    mockGetDailyResults.mockResolvedValue(mockDaily);
    mockGetSampleSize.mockResolvedValue(mockSampleSize);
  }

  it('is not asked for from the dedicated route when the experiment says sequential testing is off', async () => {
    mockGetExperiment.mockResolvedValue({ ...stored, sequential_testing_enabled: false });
    loads({ ...mockResults, sequential_testing: null });

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByTestId('experiment-summary'));
    expect(mockGetSequential).not.toHaveBeenCalled();
    expect(screen.queryByRole('tab', { name: /sequential/i })).toBeNull();
  });

  it('is asked for from the dedicated route when the experiment says it is on and the results carry no block', async () => {
    mockGetExperiment.mockResolvedValue({ ...stored, sequential_testing_enabled: true });
    loads({ ...mockResults, sequential_testing: null });
    mockGetSequential.mockResolvedValue(SEQUENTIAL);

    render(<ResultsDashboard experimentId="exp-1" />);
    expect(await screen.findByRole('tab', { name: /sequential/i })).toBeInTheDocument();
    expect(mockGetSequential).toHaveBeenCalledTimes(1);
    expect(mockGetSequential).toHaveBeenCalledWith('exp-1');
  });

  it('comes from the results when they carry it, with no second request', async () => {
    mockGetExperiment.mockResolvedValue({ ...stored, sequential_testing_enabled: true });
    loads({ ...mockResults, sequential_testing: SEQUENTIAL });

    render(<ResultsDashboard experimentId="exp-1" />);
    expect(await screen.findByRole('tab', { name: /sequential/i })).toBeInTheDocument();
    expect(mockGetSequential).not.toHaveBeenCalled();
  });

  it('is not asked for when the experiment could not be read', async () => {
    mockGetExperiment.mockRejectedValue(new ApiError({ status: 500, detail: 'boom' }));
    loads({ ...mockResults, sequential_testing: null });

    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByTestId('experiment-summary'));
    expect(mockGetSequential).not.toHaveBeenCalled();
  });

  // #1147: the mSPRT stops for an effect in either direction. The banner on
  // the Sequential tab needs the primary metric's lower_is_better, which the
  // experiment carries and the results do not.
  describe('the early-stopping banner reads the direction of the primary metric (#1147)', () => {
    const stopBlock = (lower: number, upper: number): SequentialTestingResponse => ({
      ...SEQUENTIAL,
      msprt_result: {
        lambda_ratio: 40,
        always_valid_p_value: 0.025,
        can_stop: true,
        evidence_strength: 'strong_for_effect',
        boundary: 20,
      },
      confidence_sequence: { lower, upper, width: upper - lower, sample_size: 20000 },
      recommended_action: 'stop_for_effect',
    });
    const worse = stopBlock(-0.0427, -0.0173);
    const metric = (is_primary: boolean, lower_is_better?: boolean, metric_type = 'conversion') => ({
      id: is_primary ? 'm1' : 'm2',
      name: is_primary ? 'Conversion Rate' : 'Bounce',
      event_name: 'e',
      metric_type,
      is_primary,
      ...(lower_is_better === undefined ? {} : { lower_is_better }),
    });

    async function bannerFor(
      block: SequentialTestingResponse,
      metrics: ReturnType<typeof metric>[] | undefined
    ) {
      mockGetExperiment.mockResolvedValue({
        ...stored,
        sequential_testing_enabled: true,
        ...(metrics ? { metrics } : {}),
      });
      loads({ ...mockResults, sequential_testing: block });
      render(<ResultsDashboard experimentId="exp-1" />);
      await userEvent.click(await screen.findByRole('tab', { name: /sequential/i }));
      return screen.getByTestId('early-stopping-banner');
    }

    it('shows a worse variant as harm when higher is better', async () => {
      const banner = await bannerFor(worse, [metric(true, false)]);
      expect(screen.getByTestId('action-label')).not.toHaveTextContent(/ready to stop/i);
      expect(banner.className).not.toContain('bg-green-50');
      expect(screen.getByTestId('action-label')).toHaveTextContent(/significantly worse/i);
      expect(banner).toHaveAttribute('data-direction', 'worse');
    });

    it('shows the same decrease as success when lower is better', async () => {
      const banner = await bannerFor(worse, [metric(true, true)]);
      expect(banner).toHaveAttribute('data-direction', 'better');
      expect(banner.className).toContain('bg-green-50');
    });

    it('reads the primary metric, not the first one listed', async () => {
      const banner = await bannerFor(worse, [metric(false, true), metric(true, false)]);
      expect(banner).toHaveAttribute('data-direction', 'worse');
    });

    it('reads the first metric when none is primary, as the server does', async () => {
      const banner = await bannerFor(worse, [metric(false, true), metric(false, false)]);
      expect(banner).toHaveAttribute('data-direction', 'better');
    });

    it('takes a metric without the field as higher-is-better, the API default', async () => {
      const banner = await bannerFor(worse, [metric(true)]);
      expect(banner).toHaveAttribute('data-direction', 'worse');
    });

    // The analysis counts the users who sent the metric's event, whatever the
    // metric_type. For a duration metric that is lower-is-better (page load
    // time), a drop in that share can mean the page broke and stopped sending
    // the event, so the banner must not call it an improvement.
    it.each(['duration', 'count', 'revenue', 'custom'])(
      'does not judge direction for a %s metric that is lower-is-better, with a negative interval',
      async (type) => {
        const banner = await bannerFor(worse, [metric(true, true, type)]);
        expect(screen.getByTestId('action-label')).not.toHaveTextContent(/ready to stop/i);
        expect(banner.className).not.toContain('bg-green-50');
        expect(banner).toHaveAttribute('data-direction', 'unknown');
        expect(screen.getByTestId('action-label')).toHaveTextContent(/direction unknown/i);
      }
    );

    it('does not judge a non-conversion metric that is higher-is-better either', async () => {
      const banner = await bannerFor(worse, [metric(true, false, 'duration')]);
      expect(banner).toHaveAttribute('data-direction', 'unknown');
      expect(banner.className).not.toContain('bg-red-50');
    });

    it('goes by the type of the metric the analysis ran on, the primary one', async () => {
      const banner = await bannerFor(worse, [metric(false, true, 'conversion'), metric(true, true, 'duration')]);
      expect(banner).toHaveAttribute('data-direction', 'unknown');
    });

    it('keeps judging a conversion metric, whatever other metrics the experiment has', async () => {
      const banner = await bannerFor(worse, [metric(false, false, 'duration'), metric(true, true, 'conversion')]);
      expect(banner).toHaveAttribute('data-direction', 'better');
      expect(banner.className).toContain('bg-green-50');
    });

    it('does not call the change an improvement when the experiment has no metric to read', async () => {
      const banner = await bannerFor(worse, undefined);
      expect(banner).toHaveAttribute('data-direction', 'unknown');
      expect(screen.getByTestId('action-label')).not.toHaveTextContent(/ready to stop/i);
    });
  });
});
