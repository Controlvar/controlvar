import React, { useState } from 'react';
import { SchedulerHealth } from '@/types/admin';
import { SchedulerRunHistory } from './SchedulerRunHistory';
import { RUN_STATUS_BADGE_CLASSES } from './runStatus';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

interface SchedulerStatusCardProps {
  scheduler: SchedulerHealth;
}

/** A time the API sent, or a dash when the scheduler has not run (or is not scheduled). */
function formatDate(isoString: string | null): string {
  if (!isoString) return '—';
  try {
    return parseApiTimestamp(isoString).toLocaleString();
  } catch {
    return isoString;
  }
}

export function SchedulerStatusCard({ scheduler }: SchedulerStatusCardProps) {
  const [expanded, setExpanded] = useState(false);
  const lastStatus = scheduler.last_run_status;

  return (
    <div
      data-testid="scheduler-status-card"
      className="bg-white rounded-lg border border-slate-200 p-4 shadow-sm"
    >
      {/* Header row */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-3">
          <h3 className="font-semibold text-slate-900 text-sm">{scheduler.scheduler_name}</h3>
          <span
            data-testid="status-badge"
            className={[
              'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium',
              scheduler.is_running ? 'bg-blue-100 text-blue-800' : 'bg-slate-100 text-slate-600',
            ].join(' ')}
          >
            {scheduler.is_running ? 'running' : 'idle'}
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
          <p className="text-slate-400 uppercase tracking-wide mb-0.5">Last Run</p>
          <p data-testid="last-run" className="font-medium text-slate-700">
            {formatDate(scheduler.last_run_at)}
          </p>
        </div>
        <div>
          <p className="text-slate-400 uppercase tracking-wide mb-0.5">Last Run Status</p>
          {lastStatus ? (
            <span
              data-testid="last-run-status"
              className={[
                'inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium',
                RUN_STATUS_BADGE_CLASSES[lastStatus] ?? 'bg-slate-100 text-slate-600',
              ].join(' ')}
            >
              {lastStatus}
            </span>
          ) : (
            <p data-testid="last-run-status" className="font-medium text-slate-700">
              —
            </p>
          )}
        </div>
        <div>
          <p className="text-slate-400 uppercase tracking-wide mb-0.5">Next Run</p>
          <p data-testid="next-run" className="font-medium text-slate-700">
            {formatDate(scheduler.next_run_at)}
          </p>
        </div>
        <div>
          <p className="text-slate-400 uppercase tracking-wide mb-0.5">Failures in a Row</p>
          <p data-testid="consecutive-failures" className="font-semibold text-slate-800">
            {scheduler.consecutive_failures}
          </p>
        </div>
      </div>

      {/* Collapsible run history */}
      {expanded && <SchedulerRunHistory schedulerName={scheduler.scheduler_name} />}
    </div>
  );
}
