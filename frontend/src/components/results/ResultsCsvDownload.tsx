import React, { useState } from 'react';
import { isApiError } from '@/services/api';
import { ResultsService } from '@/services/results';

export const RESULTS_CSV_NOTE =
  "The file covers the primary metric only, with the experiment's saved correction and confidence level, and has no adjusted p-value.";

export const RESULTS_CSV_FAILED = 'Could not download the results. Try again in a minute.';

// The counter is per client address and shared by every /api/v1/export/ path,
// so this does not say "you": another export from the same address spent it.
export const RESULTS_CSV_RATE_LIMITED =
  'The export limit for your address (10 a minute, shared by every export) has been reached. Wait a minute and try again.';

function failureMessage(err: unknown): string {
  return isApiError(err) && err.status === 429 ? RESULTS_CSV_RATE_LIMITED : RESULTS_CSV_FAILED;
}

function saveFile(text: string, experimentId: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = `results-${experimentId}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

interface ResultsCsvDownloadProps {
  experimentId: string;
}

/**
 * Download CSV: the experiment's variant rows (`GET
 * /api/v1/export/reports/experiments/{id}?format=csv`), saved exactly as the
 * API sent them. Every role may use it. A failure shows one fixed sentence,
 * never the server's own text.
 */
export function ResultsCsvDownload({ experimentId }: ResultsCsvDownloadProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const download = async () => {
    setBusy(true);
    setError(null);
    try {
      const { text } = await ResultsService.exportCsv(experimentId);
      saveFile(text, experimentId);
    } catch (err) {
      setError(failureMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col items-end gap-2">
      <button
        type="button"
        data-testid="results-export-csv"
        onClick={download}
        disabled={busy}
        aria-busy={busy}
        aria-describedby="results-export-csv-note"
        className="px-3 py-1.5 text-sm font-medium text-slate-700 bg-white border border-slate-300 rounded hover:bg-slate-50 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
      >
        {busy ? 'Downloading…' : 'Download CSV'}
      </button>
      <p
        id="results-export-csv-note"
        data-testid="results-export-csv-note"
        className="max-w-md text-right text-xs text-slate-600"
      >
        {RESULTS_CSV_NOTE}
      </p>
      {error && (
        <p
          data-testid="results-export-csv-error"
          role="alert"
          className="max-w-md text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2"
        >
          {error}
        </p>
      )}
    </div>
  );
}
