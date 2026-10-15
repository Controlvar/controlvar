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
import { VariantResult } from '@/types/results';
import { isFiniteNumber } from '@/components/results/shared/resultFormat';

interface ConversionChartProps {
  variants: VariantResult[];
}

const CONTROL_COLOR = '#3b82f6'; // blue-500
const VARIANT_COLORS = ['#10b981', '#f59e0b', '#ef4444', '#8b5cf6'];

export function ConversionChart({ variants }: ConversionChartProps) {
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
        No conversions yet
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
          <YAxis tickFormatter={(v: number) => `${(v * 100).toFixed(1)}%`} />
          <Tooltip
            formatter={(v) => [`${((v as number) * 100).toFixed(2)}%`, 'Rate']}
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
