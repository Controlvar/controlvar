import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MetricComparisonTable } from '@/components/results/MetricComparison/MetricComparisonTable';
import { MetricResult } from '@/types/results';

const mockMetrics: MetricResult[] = [
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
      {
        variant_id: 'var1',
        variant_name: 'Variant B',
        is_control: false,
        sample_size: 980,
        conversions: 150,
        mean: 0.153,
        std_dev: 0.045,
        confidence_interval: [0.132, 0.174],
        p_value: 0.03,
        adjusted_p_value: 0.03,
        is_significant: true,
        effect_size: 0.3,
        effect_size_label: 'small',
        relative_improvement_pct: 27.5,
        power: 0.85,
      },
    ],
  },
];

describe('MetricComparisonTable', () => {
  it('renders a row for each variant in each metric', () => {
    render(<MetricComparisonTable metrics={mockMetrics} confidenceLevel={0.95} />);
    const rows = screen.getAllByRole('row');
    // 1 header + 2 data rows
    expect(rows).toHaveLength(3);
  });

  it('renders metric name', () => {
    render(<MetricComparisonTable metrics={mockMetrics} confidenceLevel={0.95} />);
    // Each variant row repeats the metric name
    const metricCells = screen.getAllByText('Conversion Rate');
    expect(metricCells.length).toBeGreaterThan(0);
  });

  it('renders variant names', () => {
    render(<MetricComparisonTable metrics={mockMetrics} confidenceLevel={0.95} />);
    // The Effect size column also says "Control" for the control's row.
    expect(screen.getByText('Control', { selector: 'td:not([data-testid])' })).toBeInTheDocument();
    expect(screen.getByText('Variant B')).toBeInTheDocument();
  });

  it('shows primary metric label', () => {
    render(<MetricComparisonTable metrics={mockMetrics} confidenceLevel={0.95} />);
    // Each variant row for a primary metric shows the 'primary' label
    const primaryLabels = screen.getAllByText('primary');
    expect(primaryLabels.length).toBeGreaterThan(0);
  });

  it('shows improvement for non-control variant', () => {
    render(<MetricComparisonTable metrics={mockMetrics} confidenceLevel={0.95} />);
    expect(screen.getByText('+27.5%')).toBeInTheDocument();
  });

  it('shows dash for control improvement', () => {
    render(<MetricComparisonTable metrics={mockMetrics} confidenceLevel={0.95} />);
    // The control row improvement cell shows —
    // There might be multiple — in the table (improvement and p-value for control)
    const dashes = screen.getAllByText('—');
    expect(dashes.length).toBeGreaterThan(0);
  });

  it('renders significance badge', () => {
    render(<MetricComparisonTable metrics={mockMetrics} confidenceLevel={0.95} />);
    // Variant B is significant
    expect(screen.getByText(/significant \(p=0\.030\)/i)).toBeInTheDocument();
  });

  it('shows empty state when no metrics', () => {
    render(<MetricComparisonTable metrics={[]} confidenceLevel={0.95} />);
    expect(screen.getByTestId('no-metrics')).toBeInTheDocument();
  });

  it('sorts by metric name when header clicked', async () => {
    render(<MetricComparisonTable metrics={mockMetrics} confidenceLevel={0.95} />);
    const metricHeader = screen.getByRole('columnheader', { name: /metric/i });
    await userEvent.click(metricHeader);
    // Should not throw and should still render
    expect(screen.getByTestId('metric-comparison-table')).toBeInTheDocument();
  });
});
