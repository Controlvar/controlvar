/**
 * The primary metric's chart for a revenue metric analysed as revenue per
 * user: the axis and the tooltip are plain amounts, never a percentage, and
 * an empty chart says there is no revenue yet.
 *
 * The shared recharts mock draws no axis or tooltip, so this file's mock
 * renders what the chart's formatters make of one value.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { ConversionChart } from '@/components/results/Visualizations/ConversionChart';
import { VariantResult } from '@/types/results';

jest.mock('recharts', () => {
  const shared = jest.requireActual('recharts');
  return {
    ...shared,
    YAxis: ({ tickFormatter }: { tickFormatter?: (v: number) => string }) => (
      <div data-testid="y-axis">{tickFormatter ? tickFormatter(5.66) : ''}</div>
    ),
    Tooltip: ({ formatter }: { formatter?: (v: unknown) => [string, string] }) => {
      const [value, label] = formatter ? formatter(5.66) : ['', ''];
      return (
        <div data-testid="tooltip">
          <span data-testid="tooltip-value">{value}</span>
          <span data-testid="tooltip-label">{label}</span>
        </div>
      );
    },
  };
});

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

const perUser = [
  variant({ variant_id: 'ctrl', variant_name: 'Control', is_control: true, mean: 5.66 }),
  variant({ variant_id: 'b', variant_name: 'one_page', mean: 6.85 }),
];

describe('ConversionChart for revenue per user', () => {
  it('plots amounts: the axis and the tooltip are plain numbers labelled "Revenue per user"', () => {
    render(<ConversionChart variants={perUser} analysis="mean" metricType="revenue" />);
    expect(screen.getAllByTestId('variant-bar')).toHaveLength(2);
    expect(screen.getByTestId('y-axis')).toHaveTextContent(/^5\.66$/);
    expect(screen.getByTestId('tooltip-value')).toHaveTextContent(/^5\.66$/);
    expect(screen.getByTestId('tooltip-label')).toHaveTextContent(/^Revenue per user$/);
    expect(screen.getByTestId('conversion-chart').textContent).not.toMatch(/%|\$|Rate/);
  });

  it('says "No revenue yet" before the first purchase', () => {
    const none = perUser.map((v) => ({ ...v, mean: 0 }));
    render(<ConversionChart variants={none} analysis="mean" metricType="revenue" />);
    expect(screen.getByTestId('conversion-chart-empty')).toHaveTextContent(/^No revenue yet$/);
    expect(screen.queryByTestId('variant-bar')).not.toBeInTheDocument();
  });

  it('plots a negative mean (refunds) as an amount, not as empty', () => {
    const refunds = [perUser[0], { ...perUser[1], mean: -0.5 }];
    render(<ConversionChart variants={refunds} analysis="mean" metricType="revenue" />);
    expect(screen.getAllByTestId('variant-bar')).toHaveLength(2);
  });

  // #439: a count, duration or custom mean is labelled by what it is per
  // user, never a bare "Mean".
  it.each([
    ['count', 'Events per user'],
    ['duration', 'Duration per user'],
    ['custom', 'Value per user'],
  ] as const)('labels a %s mean "%s"', (metricType, label) => {
    render(<ConversionChart variants={perUser} analysis="mean" metricType={metricType} />);
    expect(screen.getByTestId('tooltip-label')).toHaveTextContent(new RegExp(`^${label}$`));
    expect(screen.getByTestId('conversion-chart').textContent).not.toMatch(/%|\$|Rate|Mean/);
  });

  it.each([
    ['count', 'No events yet'],
    ['duration', 'No values yet'],
    ['custom', 'No values yet'],
  ] as const)('says what a %s chart is waiting for: "%s"', (metricType, text) => {
    const none = perUser.map((v) => ({ ...v, mean: 0 }));
    render(<ConversionChart variants={none} analysis="mean" metricType={metricType} />);
    expect(screen.getByTestId('conversion-chart-empty')).toHaveTextContent(new RegExp(`^${text}$`));
  });

  it('keeps a proportion as a percentage, as before', () => {
    const rates = perUser.map((v, i) => ({ ...v, mean: i === 0 ? 0.0566 : 0.0685 }));
    render(<ConversionChart variants={rates} metricType="revenue" />);
    expect(screen.getByTestId('y-axis')).toHaveTextContent(/^566\.0%$/);
    expect(screen.getByTestId('tooltip-label')).toHaveTextContent(/^Rate$/);
    const none = rates.map((v) => ({ ...v, mean: 0 }));
    render(<ConversionChart variants={none} />);
    expect(screen.getByTestId('conversion-chart-empty')).toHaveTextContent(/^No conversions yet$/);
  });
});
