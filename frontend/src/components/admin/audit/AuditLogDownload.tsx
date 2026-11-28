import React, { useState } from 'react';
import { AdminService } from '@/services/admin';
import { isApiError } from '@/services/api';
import { localDayRange } from '@/utils/auditDates';
import { countCsvEntries, countJsonEntries, exportFilename } from '@/utils/auditExport';
import { AuditLogFilters } from './AuditLogFilter';

type Format = 'csv' | 'json';

const MEDIA_TYPES: Record<Format, string> = {
  csv: 'text/csv;charset=utf-8',
  json: 'application/json',
};

export const EXPORT_CAP = 50000;

export const DOWNLOAD_FAILED_MESSAGE =
  "Couldn't download the export. Try again; if it keeps failing, check the API logs.";

export function incompleteMessage(received: number | null, expected: number | null): string {
  if (received === null || expected === null) {
    return 'The download could not be checked, so nothing was saved. Try again; if it keeps failing, check the API logs.';
  }
  return `The download was incomplete: ${received.toLocaleString('en-US')} of ${expected.toLocaleString('en-US')} entries arrived, so nothing was saved. Try again.`;
}

export function capMessage(count: number): string {
  return (
    `This export would hold ${count.toLocaleString('en-US')} entries, more than the ` +
    `${EXPORT_CAP.toLocaleString('en-US')} an export can hold. Narrow the dates or choose ` +
    'an action or entity type, then download again.'
  );
}

function failureMessage(err: unknown): string {
  if (isApiError(err) && err.status === 422 && typeof err.detail === 'string') {
    const match = /^(\d+) entries match/.exec(err.detail);
    if (match) return capMessage(Number(match[1]));
  }
  return DOWNLOAD_FAILED_MESSAGE;
}

function saveFile(text: string, format: Format) {
  const url = URL.createObjectURL(new Blob([text], { type: MEDIA_TYPES[format] }));
  const link = document.createElement('a');
  link.href = url;
  link.download = exportFilename(format);
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

interface AuditLogDownloadProps {
  filters?: AuditLogFilters;
}

/**
 * Download CSV and Download JSON: every entry the filters match. The file is
 * saved only when the number of entries received equals the API's
 * `X-Total-Count`; otherwise the page says so and saves nothing.
 */
export function AuditLogDownload({ filters }: AuditLogDownloadProps) {
  const [busy, setBusy] = useState<Format | null>(null);
  const [error, setError] = useState<string | null>(null);

  const download = async (format: Format) => {
    setBusy(format);
    setError(null);
    try {
      const { from_date, to_date } = localDayRange(filters?.start_date, filters?.end_date);
      const { text, headers } = await AdminService.exportAuditLogs(format, {
        ...(filters?.user_id ? { user_id: filters.user_id } : {}),
        action_type: filters?.action_type,
        entity_type: filters?.entity_type,
        ...(filters?.entity_id ? { entity_id: filters.entity_id } : {}),
        from_date,
        to_date,
      });
      const header = headers.get('x-total-count');
      const expected = header !== null && /^\d+$/.test(header) ? Number(header) : null;
      const received = format === 'csv' ? countCsvEntries(text) : countJsonEntries(text);
      if (expected === null || received === null || received !== expected) {
        setError(incompleteMessage(received, expected));
        return;
      }
      saveFile(text, format);
    } catch (err) {
      setError(failureMessage(err));
    } finally {
      setBusy(null);
    }
  };

  const button = (format: Format, label: string) => (
    <button
      type="button"
      data-testid={`audit-download-${format}`}
      onClick={() => download(format)}
      disabled={busy !== null}
      aria-busy={busy === format}
      className="px-3 py-1.5 text-sm font-medium text-slate-700 border border-slate-300 rounded hover:bg-slate-50 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
    >
      {busy === format ? 'Downloading…' : label}
    </button>
  );

  return (
    <div data-testid="audit-log-download" className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-2 justify-end">
        {button('csv', 'Download CSV')}
        {button('json', 'Download JSON')}
      </div>
      {error && (
        <p
          data-testid="audit-download-error"
          role="alert"
          className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2"
        >
          {error}
        </p>
      )}
    </div>
  );
}
