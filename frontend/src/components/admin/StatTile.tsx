import React from 'react';

type StatTileColor = 'blue' | 'green' | 'yellow' | 'red' | 'slate';

interface StatTileProps {
  label: string;
  value: string | number;
  subtitle?: string;
  color?: StatTileColor;
}

const COLOR_CLASSES: Record<StatTileColor, { border: string; accent: string }> = {
  blue: { border: 'border-blue-200', accent: 'text-blue-600' },
  green: { border: 'border-green-200', accent: 'text-green-600' },
  yellow: { border: 'border-yellow-200', accent: 'text-yellow-600' },
  red: { border: 'border-red-200', accent: 'text-red-600' },
  slate: { border: 'border-slate-200', accent: 'text-slate-600' },
};

export function StatTile({ label, value, subtitle, color = 'slate' }: StatTileProps) {
  const { border, accent } = COLOR_CLASSES[color];

  return (
    <div
      data-testid="stat-tile"
      className={[
        'bg-white rounded-lg border p-5 flex flex-col gap-1',
        border,
      ].join(' ')}
    >
      <p className="text-sm font-medium text-slate-500">{label}</p>
      <p className={['text-3xl font-bold', accent].join(' ')}>{value}</p>
      {subtitle !== undefined && (
        <p data-testid="stat-tile-subtitle" className="text-xs text-slate-600 mt-1">
          {subtitle}
        </p>
      )}
    </div>
  );
}
