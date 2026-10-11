import React, { useEffect, useRef } from 'react';
import { AuditLog } from '@/types/admin';
import { JsonDiffViewer } from './JsonDiffViewer';
import { actionLabel, actorLabel, entityLabel } from './actionLabels';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

interface AuditLogDetailPanelProps {
  log: AuditLog;
  onClose: () => void;
}

function formatTimestamp(ts: string): string {
  try {
    return parseApiTimestamp(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function AuditLogDetailPanel({ log, onClose }: AuditLogDetailPanelProps) {
  // Opening the panel moves focus into it; closing returns it to the row.
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, []);

  return (
    <div
      data-testid="audit-log-detail-panel"
      className="bg-white border border-slate-200 rounded-lg shadow-md p-5"
    >
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        {/* h2: on the plain Audit Log page it follows the page's h1 directly. */}
        <h2
          ref={heading}
          tabIndex={-1}
          data-testid="detail-panel-heading"
          className="text-base font-semibold text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-500 rounded"
        >
          Audit log detail
        </h2>
        <button
          type="button"
          data-testid="detail-panel-close"
          onClick={onClose}
          className="text-slate-400 hover:text-slate-600 text-xl leading-none"
          aria-label="Close detail panel"
        >
          &times;
        </button>
      </div>

      {/* Fields */}
      <dl className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm mb-4">
        <div>
          <dt className="text-xs font-semibold text-slate-500 uppercase">User</dt>
          <dd className="text-slate-800 mt-0.5">{actorLabel(log)}</dd>
        </div>

        <div>
          <dt className="text-xs font-semibold text-slate-500 uppercase">Timestamp</dt>
          <dd className="text-slate-800 mt-0.5">{formatTimestamp(log.timestamp)}</dd>
        </div>

        <div className="sm:col-span-2">
          <dt className="text-xs font-semibold text-slate-500 uppercase">Action</dt>
          <dd className="text-slate-800 mt-0.5">{actionLabel(log.action_type)}</dd>
        </div>

        <div className="sm:col-span-2">
          <dt className="text-xs font-semibold text-slate-500 uppercase">Action type</dt>
          <dd className="text-slate-800 mt-0.5 font-mono text-xs">{log.action_type}</dd>
        </div>

        <div>
          <dt className="text-xs font-semibold text-slate-500 uppercase">Entity</dt>
          <dd className="text-slate-800 mt-0.5">{entityLabel(log.entity_type)}</dd>
        </div>

        <div>
          <dt className="text-xs font-semibold text-slate-500 uppercase">Name</dt>
          <dd className="text-slate-800 mt-0.5">{log.entity_name}</dd>
        </div>

        {log.reason && (
          <div className="sm:col-span-2">
            <dt className="text-xs font-semibold text-slate-500 uppercase">Reason</dt>
            <dd className="text-slate-800 mt-0.5">{log.reason}</dd>
          </div>
        )}
      </dl>

      {/* Diff viewer */}
      <JsonDiffViewer oldValue={log.old_value} newValue={log.new_value} />
    </div>
  );
}
