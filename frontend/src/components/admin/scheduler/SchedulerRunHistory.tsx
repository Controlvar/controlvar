import React, { useEffect, useState } from 'react';
import { AdminService } from '@/services/admin';
import { SchedulerRun } from '@/types/admin';
import { parseApiTimestamp } from '@/utils/apiTimestamp';
import { RUN_STATUS_BADGE_CLASSES } from './runStatus';

interface SchedulerRunHistoryProps {
  schedulerName: string;
}

function formatDate(isoString: string | null): string {
  if (!isoString) return '—';
  try {
    return parseApiTimestamp(isoString).toLocaleString();
  } catch {
    return isoString;
  }
}

export function SchedulerRunHistory({ schedulerName }: SchedulerRunHistoryProps) {
  const [runs, setRuns] = useState<SchedulerRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    AdminService.getSchedulerHistory(schedulerName)
      .then((data) => {
        if (!cancelled) {
          setRuns(data);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError((err as Error).message || 'Failed to load run history');
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [schedulerName]);

  return (
    <div data-testid="scheduler-run-history" className="mt-3 border-t border-slate-200 pt-3">
      {loading && (
        <div data-testid="run-history-loading" className="text-sm text-slate-500 py-2">
          Loading run history...
        </div>
      )}

      {!loading && error && (
        <div data-testid="run-history-error" className="text-sm text-red-600 py-2">
          {error}
        </div>
      )}

      {!loading && !error && runs.length === 0 && (
        <div data-testid="run-history-empty" className="text-sm text-slate-500 py-2">
          No run history available.
        </div>
      )}

      {!loading && !error && runs.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="bg-slate-50 text-slate-600 uppercase tracking-wide">
              <tr>
                <th className="px-3 py-2 text-left">Started At</th>
                <th className="px-3 py-2 text-left">Completed At</th>
                <th className="px-3 py-2 text-left">Status</th>
                <th className="px-3 py-2 text-left">Processed</th>
                <th className="px-3 py-2 text-left">Failed</th>
                <th className="px-3 py-2 text-left">Error Message</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {runs.map((run) => {
                const key = run.id ?? run.started_at;
                return (
                  <tr key={key} data-testid={`run-row-${key}`} className="hover:bg-slate-50">
                    <td className="px-3 py-2 text-slate-700">{formatDate(run.started_at)}</td>
                    <td className="px-3 py-2 text-slate-700">{formatDate(run.completed_at)}</td>
                    <td className="px-3 py-2">
                      <span
                        data-testid={`status-badge-${key}`}
                        className={[
                          'inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium',
                          RUN_STATUS_BADGE_CLASSES[run.status] ?? 'bg-slate-100 text-slate-600',
                        ].join(' ')}
                      >
                        {run.status}
                      </span>
                    </td>
                    <td data-testid={`processed-${key}`} className="px-3 py-2 text-slate-700">
                      {run.items_processed}
                    </td>
                    <td data-testid={`failed-${key}`} className="px-3 py-2 text-slate-700">
                      {run.items_failed}
                    </td>
                    <td className="px-3 py-2 text-slate-500">{run.error_message || '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
