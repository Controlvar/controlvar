import React from 'react';
import { RollbackRecord } from '@/types/admin';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

interface RollbackHistoryTableProps {
  rollbacks: RollbackRecord[];
}

function formatTimestamp(ts: string): string {
  try {
    return parseApiTimestamp(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function RollbackHistoryTable({ rollbacks }: RollbackHistoryTableProps) {
  return (
    <div data-testid="rollback-history-table" className="flex flex-col gap-2">
      {rollbacks.length === 0 ? (
        <div
          data-testid="rollback-empty-state"
          className="bg-slate-50 border border-slate-200 rounded-lg p-8 text-center"
        >
          <p className="text-slate-500 text-sm">No rollbacks recorded</p>
        </div>
      ) : (
        <div className="overflow-x-auto border border-slate-200 rounded-lg">
          <table className="w-full text-sm text-left">
            <thead className="bg-slate-50 border-b border-slate-200">
              <tr>
                <th className="px-4 py-3 font-semibold text-slate-600">Flag</th>
                <th className="px-4 py-3 font-semibold text-slate-600">Rolled Back By</th>
                <th className="px-4 py-3 font-semibold text-slate-600">Reason</th>
                <th className="px-4 py-3 font-semibold text-slate-600">Timestamp</th>
              </tr>
            </thead>
            <tbody>
              {rollbacks.map((record) => (
                <tr
                  key={record.id}
                  data-testid={`rollback-row-${record.id}`}
                  className="border-b border-slate-100 bg-white hover:bg-slate-50 transition-colors"
                >
                  <td className="px-4 py-3 font-medium text-slate-800">{record.flag_name}</td>
                  <td className="px-4 py-3 text-slate-700">{record.rolled_back_by}</td>
                  <td className="px-4 py-3 text-slate-600">{record.reason}</td>
                  <td className="px-4 py-3 text-slate-500 whitespace-nowrap">
                    {formatTimestamp(record.timestamp)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
