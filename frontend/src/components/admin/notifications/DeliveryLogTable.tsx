import React from 'react';
import { NotificationDeliveryLog } from '@/types/admin';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

interface DeliveryLogTableProps {
  logs: NotificationDeliveryLog[];
  loading?: boolean;
}

const STATUS_COLORS: Record<string, string> = {
  sent: 'bg-green-100 text-green-800',
  failed: 'bg-red-100 text-red-800',
  skipped: 'bg-slate-100 text-slate-700',
};

export function DeliveryLogTable({ logs, loading = false }: DeliveryLogTableProps) {
  if (loading) return <p data-testid="loading-indicator">Loading delivery log...</p>;
  if (logs.length === 0) return <p data-testid="empty-state">No delivery log entries found.</p>;

  return (
    <div data-testid="delivery-log-table">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-200">
            <th className="text-left py-2 font-medium text-slate-700">Event</th>
            <th className="text-left py-2 font-medium text-slate-700">Channel</th>
            <th className="text-left py-2 font-medium text-slate-700">Recipient</th>
            <th className="text-left py-2 font-medium text-slate-700">Status</th>
            <th className="text-left py-2 font-medium text-slate-700">Time</th>
          </tr>
        </thead>
        <tbody>
          {logs.map(log => (
            <tr key={log.id} data-testid={`log-row-${log.id}`} className="border-b border-slate-100">
              <td className="py-2">{log.event_type}</td>
              <td className="py-2 capitalize">{log.channel}</td>
              <td className="py-2 truncate max-w-xs">{log.recipient}</td>
              <td className="py-2">
                <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[log.status] ?? ''}`}>
                  {log.status}
                </span>
              </td>
              <td className="py-2 text-slate-500">
                {parseApiTimestamp(log.created_at).toLocaleString()}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
