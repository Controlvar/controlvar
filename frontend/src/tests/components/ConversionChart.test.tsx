import React from 'react';
import { render, screen } from '@testing-library/react';
import { ConversionChart } from '@/components/results/Visualizations/ConversionChart';
import { VariantResult } from '@/types/results';

const mockVariants: VariantResult[] = [
  {
    variant_id: 'control-id',
    variant_name: 'Control',
    is_control: true,
    sample_size: 1000,
    conversions: 120,
    mean: 0.12,
    std_dev: 0.05,
    confidence_interval: [0.1, 0.14],
    p_value: null,
    adjusted_p_value: null,
    is_significant: false,
    effect_size: null,
    effect_size_label: null,
    relative_improvement_pct: null,
    power: null,
  },
  {
    variant_id: 'variant-id',
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
];

describe('ConversionChart', () => {
  it('renders a bar for each variant', () => {
    render(<ConversionChart variants={mockVariants} />);
    expect(screen.getAllByTestId('variant-bar')).toHaveLength(2);
  });

  it('highlights control variant differently', () => {
    render(<ConversionChart variants={mockVariants} />);
    const controlLabel = screen.getByText('Control');
    expect(
      controlLabel.closest('[data-testid="variant-bar"]')
    ).toHaveAttribute('data-is-control', 'true');
  });

  it('marks non-control variant correctly', () => {
    render(<ConversionChart variants={mockVariants} />);
    const variantLabel = screen.getByText('Variant B');
    expect(
      variantLabel.closest('[data-testid="variant-bar"]')
    ).toHaveAttribute('data-is-control', 'false');
  });

  it('shows empty state when no variants', () => {
    render(<ConversionChart variants={[]} />);
    expect(screen.getByText(/no variant data/i)).toBeInTheDocument();
    expect(screen.queryByTestId('variant-bar')).not.toBeInTheDocument();
  });

  it('wraps chart in responsive container', () => {
    render(<ConversionChart variants={mockVariants} />);
    expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
  });

  // #1244: with no conversions there is nothing to plot; an empty bar chart
  // drew a 0-400% axis of its own.
  it('shows "No conversions yet" instead of a chart when nothing has converted', () => {
    const none = mockVariants.map((v) => ({
      ...v,
      sample_size: 0,
      conversions: 0,
      mean: 0,
      confidence_interval: null,
      p_value: null,
      adjusted_p_value: null,
      is_significant: false,
      relative_improvement_pct: null,
    }));
    render(<ConversionChart variants={none} />);
    expect(screen.getByTestId('conversion-chart-empty')).toHaveTextContent('No conversions yet');
    expect(screen.queryByTestId('conversion-chart')).not.toBeInTheDocument();
  });

  it('draws the chart when one variant has converted', () => {
    const one = mockVariants.map((v) => (v.is_control ? { ...v, conversions: 0, mean: 0 } : v));
    render(<ConversionChart variants={one} />);
    expect(screen.getByTestId('conversion-chart')).toBeInTheDocument();
    expect(screen.queryByTestId('conversion-chart-empty')).not.toBeInTheDocument();
  });
});
