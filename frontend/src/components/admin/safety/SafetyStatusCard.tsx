import React from 'react';
import { FlagHealth, FlagSafetyStatus } from '@/types/admin';

interface SafetyStatusCardProps {
  flag: FlagSafetyStatus;
  onRollback: (flagId: string) => void;
}

const STATUS_BADGE_CLASSES: Record<FlagHealth, string> = {
  healthy: 'bg-green-100 text-green-800',
  warning: 'bg-yellow-100 text-yellow-800',
  critical: 'bg-red-100 text-red-800',
};

/** `error_rate` is a 0–1 fraction on the API; show it as a percentage. */
export function formatErrorRate(value: number | null): string {
  return value === null ? 'n/a' : `${(value * 100).toFixed(2)}%`;
}

export function formatLatency(value: number | null): string {
  return value === null ? 'n/a' : `${Math.round(value)} ms`;
}

export function SafetyStatusCard({ flag, onRollback }: SafetyStatusCardProps) {
  const badgeClass = STATUS_BADGE_CLASSES[flag.health];
  const showRollback = flag.is_on && (flag.health === 'warning' || flag.health === 'critical');
  const unmeasured = flag.check.details?.unmeasured_metrics;
  const uncounted = flag.check.details?.uncounted_error_count;
  const disabled = flag.check.metrics.length === 0;

  return (
    <div
      data-testid="safety-status-card"
      data-flag-id={flag.flag_id}
      // Focus lands here when the dialog closes after its Roll back button has gone.
      tabIndex={-1}
      className="bg-white border border-slate-200 rounded-lg p-4 flex flex-col gap-3"
    >
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <span className="block font-semibold text-slate-800 text-sm truncate">{flag.flag_name}</span>
          <span className="block font-mono text-xs text-slate-500 truncate">{flag.flag_key}</span>
        </div>
        <span
          data-testid="status-badge"
          className={`text-xs font-medium px-2 py-0.5 rounded-full whitespace-nowrap ${badgeClass}`}
        >
          {flag.health}
        </span>
      </div>

      <div className="flex flex-col gap-1 text-sm text-slate-600">
        <div className="flex justify-between">
          <span>Error Rate</span>
          <span data-testid="error-rate-value" className="font-medium text-slate-800">
            {formatErrorRate(flag.error_rate)}
          </span>
        </div>
        <div className="flex justify-between">
          <span>Latency</span>
          <span data-testid="latency-value" className="font-medium text-slate-800">
            {formatLatency(flag.latency_ms)}
          </span>
        </div>
        {disabled && (
          <p data-testid="safety-disabled-note" className="text-xs text-slate-500">
            No safety metrics configured for this flag.
          </p>
        )}
        {Array.isArray(unmeasured) && unmeasured.length > 0 && (
          <p data-testid="safety-unmeasured-note" className="text-xs text-slate-500">
            Not yet measured: {unmeasured.join(', ')}
          </p>
        )}
        {typeof uncounted === 'number' && uncounted > 0 && (
          <p data-testid="safety-uncounted-note" className="text-xs text-slate-500">
            {uncounted} {uncounted === 1 ? 'report' : 'reports'} not counted
          </p>
        )}
      </div>

      {!flag.is_on && (
        <p
          data-testid="flag-off-indicator"
          className="mt-1 w-full text-center text-sm font-medium text-slate-700 border border-slate-300 rounded py-1.5"
        >
          Off
        </p>
      )}

      {showRollback && (
        <button
          type="button"
          data-testid="rollback-button"
          onClick={() => onRollback(flag.flag_id)}
          className="mt-1 w-full bg-red-600 text-white text-sm font-medium py-1.5 rounded hover:bg-red-700 transition-colors"
        >
          Roll back
        </button>
      )}
    </div>
  );
}
