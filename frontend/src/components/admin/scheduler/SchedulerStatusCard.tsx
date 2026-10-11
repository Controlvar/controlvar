import React, { useState } from 'react';
import { SchedulerHealth } from '@/types/admin';
import { SchedulerRunHistory } from './SchedulerRunHistory';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

interface SchedulerStatusCardProps {
  scheduler: SchedulerHealth;
}

function formatDate(isoString: string): string {
  try {
    return parseApiTimestamp(isoString).toLocaleString();
  } catch {
    return isoString;
  }
}

const STATUS_BADGE_CLASSES: Record<SchedulerHealth['status'], string> = {
  running: 'bg-blue-100 text-blue-800',
  idle: 'bg-slate-100 text-slate-600',
  error: 'bg-red-100 text-red-800',
};

export function SchedulerStatusCard({ scheduler }: SchedulerStatusCardProps) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div
      data-testid="scheduler-status-card"
      className="bg-white rounded-lg border border-slate-200 p-4 shadow-sm"
    >
      {/* Header row */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-3">
          <h3 className="font-semibold text-slate-900 text-sm">{scheduler.name}</h3>
          <span
            data-testid="status-badge"
            className={[
              'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium',
              STATUS_BADGE_CLASSES[scheduler.status],
            ].join(' ')}
          >
            {scheduler.status}
          </span>
        </div>

        <button
          data-testid="expand-button"
          onClick={() => setExpanded((prev) => !prev)}
          className="text-xs text-blue-600 hover:text-blue-800 font-medium"
          aria-expanded={expanded}
        >
          {expanded ? 'Hide History' : 'Show History'}
        </button>
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-2 gap-3 text-xs text-slate-600 sm:grid-cols-4">
        <div>
          <p className="text-slate-400 uppercase tracking-wide mb-0.5">Run Count</p>
          <p data-testid="run-count" className="font-semibold text-slate-800">
            {scheduler.run_count}
          </p>
        </div>
        <div>
          <p className="text-slate-400 uppercase tracking-wide mb-0.5">Error Count</p>
          <p data-testid="error-count" className="font-semibold text-slate-800">
            {scheduler.error_count}
          </p>
        </div>
        <div>
          <p className="text-slate-400 uppercase tracking-wide mb-0.5">Last Run</p>
          <p data-testid="last-run" className="font-medium text-slate-700">
            {formatDate(scheduler.last_run)}
          </p>
        </div>
        <div>
          <p className="text-slate-400 uppercase tracking-wide mb-0.5">Next Run</p>
          <p data-testid="next-run" className="font-medium text-slate-700">
            {formatDate(scheduler.next_run)}
          </p>
        </div>
      </div>

      {/* Collapsible run history */}
      {expanded && <SchedulerRunHistory schedulerName={scheduler.name} />}
    </div>
  );
}
