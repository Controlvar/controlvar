/**
 * A breakdown that cannot be computed is an error on the Breakdowns tab (#1776):
 * the API now answers it with a 500 instead of a 200 with the segments done so
 * far, and the dashboard says so in fixed words with a Try again button.
 */
import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
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

jest.mock('@/services/results');
jest.mock('@/services/experiments');

const mockGetResults = ResultsService.getResults as jest.Mock;
const mockGetDailyResults = ResultsService.getDailyResults as jest.Mock;
const mockGetSampleSize = ResultsService.getSampleSize as jest.Mock;
void ExperimentsService;

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
  mde_source: 'experiment',
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

// The words are asserted literally: a reworded failure is a decision, not a refactor.
const FAILED_COPY =
  'The breakdown could not be computed. Try again; if it keeps failing, check the API logs.';
// What the server answers when it cannot compute a breakdown (500, fixed text),
// plus a line that must never reach the page.
const SERVER_TEXT = 'Could not compute the experiment\'s results';
const LEAK = 'Traceback (most recent call last): secret_table';

/** `/results` answers the headline; with `breakdown=` it answers `onBreakdown`. */
function serve(onBreakdown: () => Promise<unknown>) {
  mockGetResults.mockImplementation((_id: string, opts?: { breakdown?: string }) =>
    opts?.breakdown ? onBreakdown() : Promise.resolve(mockResults)
  );
  mockGetDailyResults.mockResolvedValue(mockDaily);
  mockGetSampleSize.mockResolvedValue(mockSampleSize);
}

const failing = () =>
  Promise.reject(new ApiError({ status: 500, detail: `${SERVER_TEXT}\n${LEAK}` }));

async function openBreakdowns() {
  render(<ResultsDashboard experimentId="exp-1" />);
  await waitFor(() => screen.getByRole('tablist'));
  await userEvent.click(screen.getByRole('tab', { name: /breakdowns/i }));
}

const breakdownCalls = () =>
  mockGetResults.mock.calls.filter(([, opts]) => opts && opts.breakdown);

beforeEach(() => {
  jest.clearAllMocks();
});

describe('a failed breakdown (#1776, UI1)', () => {
  it('J1: shows one alert with the fixed words, no server text, and Try again asks for the same breakdown', async () => {
    serve(failing);
    await openBreakdowns();
    await userEvent.selectOptions(screen.getByTestId('breakdown-selector'), 'platform');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(FAILED_COPY);
    expect(alert.textContent).toBe(FAILED_COPY);
    expect(screen.getAllByRole('alert')).toHaveLength(1);
    expect(document.body.textContent).not.toContain(LEAK);
    expect(document.body.textContent).not.toContain(SERVER_TEXT);
    expect(screen.queryByText(/no breakdown data/i)).not.toBeInTheDocument();
    expect(breakdownCalls()).toHaveLength(1);

    // The retry succeeds: the table replaces the alert.
    serve(() => Promise.resolve({ ...mockResults, breakdown: mockBreakdown }));
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() =>
      expect(screen.getByTestId('segment-comparison-table')).toBeInTheDocument()
    );
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(breakdownCalls()).toHaveLength(2);
    expect(breakdownCalls()[1][1]).toEqual(breakdownCalls()[0][1]);
    expect(breakdownCalls()[1][1]).toEqual({ breakdown: 'platform' });
  });

  it('J1b: a second failure is announced again (the alert is replaced, not kept)', async () => {
    serve(failing);
    await openBreakdowns();
    await userEvent.selectOptions(screen.getByTestId('breakdown-selector'), 'platform');
    const first = await screen.findByRole('alert');
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(breakdownCalls()).toHaveLength(2));
    const second = await screen.findByRole('alert');
    expect(second).not.toBe(first);
    expect(screen.getAllByRole('alert')).toHaveLength(1);
  });

  it('a breakdown the server leaves out is "no data", not a failure', async () => {
    serve(() => Promise.resolve(mockResults));
    await openBreakdowns();
    await userEvent.selectOptions(screen.getByTestId('breakdown-selector'), 'platform');
    expect(await screen.findByText(/no breakdown data/i)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('J2: Try again is a button the keyboard reaches and works with Enter and Space', async () => {
    const user = userEvent.setup();
    serve(failing);
    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByRole('tablist'));
    await user.click(screen.getByRole('tab', { name: /breakdowns/i }));
    await user.selectOptions(screen.getByTestId('breakdown-selector'), 'platform');
    await screen.findByRole('alert');

    const select = screen.getByTestId('breakdown-selector');
    select.focus();
    await user.tab();
    const button = screen.getByRole('button', { name: 'Try again' });
    expect(button.tagName).toBe('BUTTON');
    expect(button).toHaveFocus();

    await user.keyboard('{Enter}');
    await waitFor(() => expect(breakdownCalls()).toHaveLength(2));
    await screen.findByRole('alert');
    screen.getByRole('button', { name: 'Try again' }).focus();
    await user.keyboard(' ');
    await waitFor(() => expect(breakdownCalls()).toHaveLength(3));
  });

  it('J9: activating Try again by keyboard leaves focus on the select while the retry runs, never on the page body', async () => {
    const user = userEvent.setup();
    serve(failing);
    render(<ResultsDashboard experimentId="exp-1" />);
    await waitFor(() => screen.getByRole('tablist'));
    await user.click(screen.getByRole('tab', { name: /breakdowns/i }));
    await user.selectOptions(screen.getByTestId('breakdown-selector'), 'platform');
    await screen.findByRole('alert');

    serve(() => new Promise(() => {})); // the retry stays pending
    const select = screen.getByTestId('breakdown-selector');
    select.focus();
    await user.tab();
    expect(screen.getByRole('button', { name: 'Try again' })).toHaveFocus();
    await user.keyboard('{Enter}');
    await waitFor(() => expect(breakdownCalls()).toHaveLength(2));
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument();
    expect(document.activeElement).not.toBe(document.body);
    expect(select).toHaveFocus();
  });

  it('J7: focus stays on the select while the breakdown loads and after it fails', async () => {
    let reject!: (e: unknown) => void;
    serve(() => new Promise((_, r) => { reject = r; }));
    await openBreakdowns();
    const select = screen.getByTestId('breakdown-selector');
    select.focus();
    await userEvent.selectOptions(select, 'platform');
    await waitFor(() => expect(breakdownCalls()).toHaveLength(1));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(select).toHaveFocus();
    await act(async () => {
      reject(new ApiError({ status: 500 }));
    });
    await screen.findByRole('alert');
    expect(select).toHaveFocus();
  });

  it('J8: axe finds no violation in the failure state (colour contrast is not computable in jsdom)', async () => {
    serve(failing);
    await openBreakdowns();
    await userEvent.selectOptions(screen.getByTestId('breakdown-selector'), 'platform');
    await screen.findByRole('alert');
    const result = await axe.run(screen.getByTestId('breakdown-failed'), {
      rules: { 'color-contrast': { enabled: false } },
    });
    expect(result.violations.map((v) => v.id)).toEqual([]);
  });
});
