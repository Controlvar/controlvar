/**
 * "View SQL": the exact statements a warehouse run sent, read-only and
 * copyable, so the customer can check them or run them under their own
 * identity. Every role that can read the run can open it (the API's matrix:
 * read runs, results and the SQL sent — every role). No credential is ever in
 * a statement.
 *
 * Accessibility: the shared Dialog (focus stays inside, Escape closes, the page
 * behind is inert, focus returns to the button that opened it). Each statement
 * is a labelled, keyboard-scrollable region, and copying is announced politely.
 */
import React, { useRef, useState } from 'react';
import { Dialog } from '@/components/ui/Dialog';
import type { RunStatement } from '@modules/services/warehouseRuns';

export interface ViewSqlDialogProps {
  statements: RunStatement[];
  /** The element focus returns to on close. */
  returnFocusTo?: HTMLElement | null;
  onClose: () => void;
}

function statementLabel(statement: RunStatement, index: number, all: RunStatement[]): string {
  if (statement.kind === 'diagnostics') return 'Diagnostics statement';
  if (statement.kind === 'metric') {
    const metricIndex = all.slice(0, index + 1).filter((s) => s.kind === 'metric').length;
    return `Metric statement ${metricIndex}`;
  }
  return `Statement ${index + 1}`;
}

export default function ViewSqlDialog({ statements, returnFocusTo, onClose }: ViewSqlDialogProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const [copied, setCopied] = useState<string>('');

  const copy = async (text: string, label: string) => {
    try {
      await navigator.clipboard?.writeText(text);
      setCopied(`Copied the ${label.toLowerCase()}.`);
    } catch {
      setCopied('Copying is not available in this browser; select the text instead.');
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="SQL this analysis ran"
      subtitle={
        <p className="mt-1 text-sm text-slate-600">
          The exact statements sent to the warehouse, in order. They contain no credentials, so
          you can run them yourself to check the numbers.
        </p>
      }
      headerAction={
        <button
          ref={closeRef}
          type="button"
          onClick={onClose}
          className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
          data-testid="warehouse-sql-close"
        >
          Close
        </button>
      }
      initialFocusRef={closeRef}
      returnFocus={() => returnFocusTo}
      size="xl"
      testId="warehouse-sql-dialog"
    >
      <div className="space-y-5 border-t border-slate-200 pt-4">
        {statements.map((statement, index) => {
          const label = statementLabel(statement, index, statements);
          const id = `warehouse-sql-${index}`;
          return (
            <section key={id} aria-labelledby={`${id}-label`}>
              <div className="mb-1 flex items-center justify-between gap-2">
                <h3 id={`${id}-label`} className="text-sm font-medium text-slate-800">
                  {label}
                </h3>
                <button
                  type="button"
                  onClick={() => void copy(statement.sql, label)}
                  className="text-sm font-medium text-blue-700 underline"
                  data-testid="warehouse-sql-copy"
                >
                  Copy {label.toLowerCase()}
                </button>
              </div>
              <pre
                tabIndex={0}
                role="region"
                aria-label={`${label}, SQL`}
                className="max-h-72 overflow-auto rounded-md bg-slate-900 p-3 text-xs text-slate-100"
                data-testid="warehouse-sql-statement"
              >
                {statement.sql}
              </pre>
              {statement.sha256 && (
                <p className="mt-1 break-all text-xs text-slate-600">SHA-256 {statement.sha256}</p>
              )}
            </section>
          );
        })}
      </div>
      <p aria-live="polite" className="sr-only" data-testid="warehouse-sql-copied">
        {copied}
      </p>
    </Dialog>
  );
}
