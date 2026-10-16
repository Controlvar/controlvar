import React from 'react';
import { render, screen } from '@testing-library/react';
import { SequentialMonitor } from '@/components/results/Sequential/SequentialMonitor';
import { SequentialTestingResponse } from '@/types/sequential';

// Mock recharts
jest.mock('recharts', () => {
  const OriginalModule = jest.requireActual('recharts');
  return {
    ...OriginalModule,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="responsive-container">{children}</div>
    ),
  };
});

const fullData: SequentialTestingResponse = {
  method: 'msprt',
  msprt_result: {
    lambda_ratio: 25.0,
    always_valid_p_value: 0.04,
    can_stop: true,
    evidence_strength: 'strong_for_effect',
    boundary: 20.0,
  },
  confidence_sequence: {
    lower: 0.005,
    upper: 0.035,
    width: 0.03,
    sample_size: 5000,
  },
  evidence_trajectory: [
    { sample_size: 100, lambda_ratio: 1.5, always_valid_p_value: 0.67, can_stop: false },
    { sample_size: 500, lambda_ratio: 25.0, always_valid_p_value: 0.04, can_stop: true },
  ],
  alpha_spending: [
    { look_number: 1, cumulative_alpha: 0.001, boundary_z: 3.29, boundary_p: 0.001 },
    { look_number: 2, cumulative_alpha: 0.005, boundary_z: 2.81, boundary_p: 0.005 },
  ],
  long_running_risk: {
    is_at_risk: true,
    expected_duration_days: 14,
    actual_duration_days: 21,
    risk_ratio: 1.5,
    recommendation: 'Consider stopping the experiment.',
  },
  recommended_action: 'stop_for_effect',
};

const minimalData: SequentialTestingResponse = {
  method: 'msprt',
  msprt_result: null,
  confidence_sequence: null,
  evidence_trajectory: [],
  alpha_spending: [],
  long_running_risk: null,
  recommended_action: 'continue',
};

describe('SequentialMonitor', () => {
  it('renders the monitor container', () => {
    render(<SequentialMonitor data={fullData} />);
    expect(screen.getByTestId('sequential-monitor')).toBeInTheDocument();
  });

  it('renders EarlyStoppingBanner', () => {
    render(<SequentialMonitor data={fullData} />);
    expect(screen.getByTestId('early-stopping-banner')).toBeInTheDocument();
  });

  it('renders confidence sequence section', () => {
    render(<SequentialMonitor data={fullData} />);
    expect(screen.getByTestId('confidence-sequence')).toBeInTheDocument();
    expect(screen.getByTestId('ci-lower')).toHaveTextContent('0.0050');
    expect(screen.getByTestId('ci-upper')).toHaveTextContent('0.0350');
    expect(screen.getByTestId('ci-width')).toHaveTextContent('0.0300');
    expect(screen.getByTestId('ci-n')).toHaveTextContent('5,000');
  });

  it('renders long-running risk as an advisory note when at risk', () => {
    render(<SequentialMonitor data={fullData} />);
    const risk = screen.getByTestId('long-running-risk');
    expect(risk).toBeInTheDocument();
    expect(risk).toHaveTextContent('21');
    expect(risk).toHaveTextContent('14');
    expect(risk).toHaveTextContent('150%');
    expect(risk).toHaveTextContent(/not a reason to stop/i);
    expect(risk).toHaveAttribute('role', 'note');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows the at-risk note from the top-level at_risk flag', () => {
    render(<SequentialMonitor data={{ ...minimalData, at_risk: true }} />);
    expect(screen.getByTestId('long-running-risk')).toHaveTextContent(
      /running longer than planned/i
    );
  });

  it('lets a top-level at_risk of false win over long_running_risk', () => {
    render(<SequentialMonitor data={{ ...fullData, at_risk: false }} />);
    expect(screen.queryByTestId('long-running-risk')).not.toBeInTheDocument();
  });

  it('shows the beta notice when the analysis is beta', () => {
    render(
      <SequentialMonitor
        data={{
          ...minimalData,
          analysis_status: 'beta',
          analysis_notice:
            'Beta: the stop/continue decision is mSPRT alone, at the significance ' +
            'level shown by the boundary (1/alpha). alpha_spending is always empty: ' +
            'the planned-looks (alpha-spending) table is not computed yet. ' +
            'https://github.com/Controlvar/controlvar/issues/232',
        }}
      />
    );
    expect(screen.getByTestId('analysis-notice')).toHaveTextContent(
      /planned-looks \(alpha-spending\) table is not computed yet/i
    );
  });

  it('shows no beta notice when the analysis is ga or the fields are absent', () => {
    const { rerender } = render(
      <SequentialMonitor data={{ ...minimalData, analysis_status: 'ga', analysis_notice: null }} />
    );
    expect(screen.queryByTestId('analysis-notice')).not.toBeInTheDocument();
    rerender(<SequentialMonitor data={minimalData} />);
    expect(screen.queryByTestId('analysis-notice')).not.toBeInTheDocument();
  });

  it('renders alpha spending table', () => {
    render(<SequentialMonitor data={fullData} />);
    const table = screen.getByTestId('alpha-spending-table');
    expect(table).toBeInTheDocument();
    // 2 data rows + 1 header row
    const rows = table.querySelectorAll('tbody tr');
    expect(rows).toHaveLength(2);
  });

  it('hides confidence sequence when null', () => {
    render(<SequentialMonitor data={minimalData} />);
    expect(screen.queryByTestId('confidence-sequence')).not.toBeInTheDocument();
  });

  it('hides long-running risk when null', () => {
    render(<SequentialMonitor data={minimalData} />);
    expect(screen.queryByTestId('long-running-risk')).not.toBeInTheDocument();
  });

  it('hides alpha spending table when empty', () => {
    render(<SequentialMonitor data={minimalData} />);
    expect(screen.queryByTestId('alpha-spending-table')).not.toBeInTheDocument();
  });

  it('shows empty evidence chart when trajectory is empty', () => {
    render(<SequentialMonitor data={minimalData} />);
    expect(screen.getByTestId('evidence-chart-empty')).toBeInTheDocument();
  });

  it('hides long-running risk when not at risk', () => {
    const dataNotAtRisk: SequentialTestingResponse = {
      ...fullData,
      long_running_risk: {
        is_at_risk: false,
        expected_duration_days: 14,
        actual_duration_days: 7,
        risk_ratio: 0.5,
        recommendation: 'On track',
      },
    };
    render(<SequentialMonitor data={dataNotAtRisk} />);
    expect(screen.queryByTestId('long-running-risk')).not.toBeInTheDocument();
  });

  // #1147: the banner reads the sign of the confidence sequence together with
  // the metric's direction, which the monitor is handed.
  it('tells a worse variant from a better one using the confidence sequence', () => {
    const worse: SequentialTestingResponse = {
      ...fullData,
      confidence_sequence: { lower: -0.0427, upper: -0.0173, width: 0.0253, sample_size: 20000 },
    };
    const { rerender } = render(<SequentialMonitor data={worse} lowerIsBetter={false} />);
    expect(screen.getByTestId('early-stopping-banner')).toHaveAttribute('data-direction', 'worse');
    expect(screen.getByTestId('action-label')).toHaveTextContent(/significantly worse/i);

    rerender(<SequentialMonitor data={worse} lowerIsBetter />);
    expect(screen.getByTestId('early-stopping-banner')).toHaveAttribute('data-direction', 'better');

    rerender(<SequentialMonitor data={fullData} lowerIsBetter={false} />);
    expect(screen.getByTestId('early-stopping-banner')).toHaveAttribute('data-direction', 'better');
  });
});
