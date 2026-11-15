import React from 'react';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from 'recharts';
import { MetricAnalysis, MetricResult, VariantResult } from '@/types/results';
import {
  capitalised,
  formatAmount,
  isFiniteNumber,
  meanLabelForType,
} from '@/components/results/shared/resultFormat';

interface ConversionChartProps {
  variants: VariantResult[];
  /**
   * How the metric was analysed (`analysedAsMean` on the metric): 'mean'
   * plots each variant's value per user as a plain amount, 'proportion' (the
   * default) its rate as a percentage.
   */
  analysis?: MetricAnalysis;
  /**
   * The metric's type, which names a mean: "Revenue per user", "Events per
   * user", "Duration per user" or "Value per user".
   */
  metricType?: MetricResult['metric_type'];
}

/** What the chart says before the first value. */
function emptyText(mean: boolean, metricType?: string): string {
  if (!mean) return 'No conversions yet';
  if (metricType === 'revenue') return 'No revenue yet';
  if (metricType === 'count') return 'No events yet';
  return 'No values yet';
}

const CONTROL_COLOR = '#3b82f6'; // blue-500
const VARIANT_COLORS = ['#10b981', '#f59e0b', '#ef4444', '#8b5cf6'];

export function ConversionChart({
  variants,
  analysis = 'proportion',
  metricType,
}: ConversionChartProps) {
  const mean = analysis === 'mean';
  const meanLabel = capitalised(meanLabelForType(metricType ?? ''));

  if (!variants.length) {
    return (
      <div className="flex items-center justify-center h-48 text-slate-500">
        No variant data available
      </div>
    );
  }

  // With nothing to plot, an empty chart would draw an axis of its own
  // choosing (0-400%), so say so instead (#1244).
  if (variants.every((v) => !isFiniteNumber(v.mean) || v.mean === 0)) {
    return (
      <div
        className="flex items-center justify-center h-48 text-slate-500"
        data-testid="conversion-chart-empty"
      >
        {emptyText(mean, metricType)}
      </div>
    );
  }

  // Build one data-point per variant for side-by-side comparison
  const chartData = variants.map((v, idx) => ({
    name: v.variant_name,
    value: v.mean,
    variantId: v.variant_id,
    isControl: v.is_control,
    fill: v.is_control
      ? CONTROL_COLOR
      : VARIANT_COLORS[(idx - 1) % VARIANT_COLORS.length],
  }));

  // Build one Bar per data point so tests can query individual bars
  return (
    <div className="w-full h-72" data-testid="conversion-chart">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={chartData}
          margin={{ top: 20, right: 30, left: 20, bottom: 5 }}
        >
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="name" />
          {mean ? (
            // A mean is an amount per user: plain numbers, no "%" and no currency.
            <YAxis tickFormatter={(v: number) => formatAmount(v)} />
          ) : (
            <YAxis tickFormatter={(v: number) => `${(v * 100).toFixed(1)}%`} />
          )}
          <Tooltip
            formatter={(v) =>
              mean
                ? [formatAmount(v as number), meanLabel]
                : [`${((v as number) * 100).toFixed(2)}%`, 'Rate']
            }
          />
          <Legend />
          {chartData.map((entry) => (
            <Bar
              key={entry.variantId}
              dataKey="value"
              name={entry.name}
              fill={entry.fill}
              {...({ 'data-is-control': String(entry.isControl) } as object)}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
