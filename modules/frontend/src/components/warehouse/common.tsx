/**
 * Shared pieces of the warehouse screens: the Beta chip, the role notice,
 * loading and error states, form fields, copyable code, and the confirm
 * dialog. Accessibility follows the warehouse UX draft: every field has a
 * label, help and errors are tied with aria-describedby, status is text (never
 * colour alone), and code blocks are focusable, labelled scroll regions.
 */
import React, { ReactNode, useCallback, useEffect, useId, useRef, useState } from 'react';
import Link from 'next/link';
import type { Connection, CredentialsStatus } from '@modules/services/warehouse';
import { warehouseName } from '@modules/services/warehouse';
import { MODULES } from '@/services/modules';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

/** What `withModule` shows on a deployment without the warehouse module. */
export const WAREHOUSE_MODULE_NOTICE = {
  title: 'Warehouse',
  module: MODULES.WAREHOUSE,
  description:
    'Analyse experiments with assignment and metric data that stays in your own warehouse (BigQuery, Snowflake, Amazon Athena).',
};

export const NO_CONNECTOR_TEXT =
  "No warehouse connector is available on this deployment yet. BigQuery, Snowflake and Amazon Athena are each made available once they have been checked against a real account; until then, connections can't be created.";

export const BETA_TEXT =
  'Warehouse analysis is in beta: its settings and API may change. The statistics are computed by the same engine as every other result.';

/** "Beta" with its meaning reachable by keyboard focus, not only by hover. */
export function BetaChip() {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <span className="relative inline-flex">
      <button
        type="button"
        data-testid="warehouse-beta-chip"
        aria-describedby={id}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') setOpen(false);
        }}
        className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-800 focus:outline-none focus:ring-2 focus:ring-amber-600"
      >
        Beta
      </button>
      <span
        id={id}
        role="tooltip"
        className={`${open ? 'block' : 'sr-only'} absolute left-0 top-full z-30 mt-1 w-72 rounded-md border border-slate-200 bg-white p-3 text-xs font-normal text-slate-700 shadow-lg`}
      >
        {BETA_TEXT}
      </span>
    </span>
  );
}

export interface Crumb {
  label: string;
  href?: string;
}

export function WarehouseHeader({
  title,
  crumbs = [],
  children,
}: {
  title: string;
  crumbs?: Crumb[];
  children?: ReactNode;
}) {
  return (
    <header className="mb-6">
      {crumbs.length > 0 && (
        <nav aria-label="Breadcrumb" className="mb-2 text-sm text-slate-600">
          <ol className="flex flex-wrap items-center gap-1">
            {crumbs.map((c, i) => (
              <li key={`${c.label}-${i}`} className="flex items-center gap-1">
                {c.href ? (
                  <Link href={c.href} className="text-blue-700 underline-offset-2 hover:underline">
                    {c.label}
                  </Link>
                ) : (
                  <span aria-current="page">{c.label}</span>
                )}
                {i < crumbs.length - 1 && <span aria-hidden="true">/</span>}
              </li>
            ))}
          </ol>
        </nav>
      )}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <h1 className="text-2xl font-semibold text-slate-900">{title}</h1>
          <BetaChip />
        </div>
        {children}
      </div>
    </header>
  );
}

/** Visible text in place of a control the user's role cannot use. */
export function RoleNotice({ message, testId = 'warehouse-role-notice' }: { message: string; testId?: string }) {
  return (
    <p
      data-testid={testId}
      className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700"
    >
      {message}
    </p>
  );
}

export function Loading({ label }: { label: string }) {
  return (
    <p role="status" data-testid="warehouse-loading" className="py-8 text-center text-sm text-slate-600">
      {label}
    </p>
  );
}

/** A page that could not load. Not an alert: nothing the user just did failed. */
export function LoadError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div data-testid="warehouse-load-error" className="rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-800">
      <p>{message}</p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-3 rounded-md border border-red-300 bg-white px-3 py-1.5 text-sm font-medium text-red-800 hover:bg-red-50"
      >
        Try again
      </button>
    </div>
  );
}

/** The failure of an action the user just took. */
export function ActionError({ message, testId = 'warehouse-action-error' }: { message: string; testId?: string }) {
  return (
    <p role="alert" data-testid={testId} className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
      {message}
    </p>
  );
}

export function errorText(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

/** The `field` an API refusal names, if any (`{detail: {code, message, field}}`). */
export function errorField(err: unknown): string | null {
  const detail = (err as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const field = (detail as Record<string, unknown>).field;
    if (typeof field === 'string') return field;
  }
  return null;
}

export function errorCode(err: unknown): string | null {
  const code = (err as { code?: unknown } | null)?.code;
  return typeof code === 'string' ? code : null;
}

// -- fields -------------------------------------------------------------------

interface FieldProps {
  id: string;
  label: string;
  help?: ReactNode;
  error?: string | null;
  required?: boolean;
  children: (aria: {
    id: string;
    'aria-describedby'?: string;
    'aria-invalid'?: boolean;
    'aria-required'?: boolean;
  }) => ReactNode;
}

export function Field({ id, label, help, error, required, children }: FieldProps) {
  const helpId = help ? `${id}-help` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [errorId, helpId].filter(Boolean).join(' ') || undefined;
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium text-slate-800">
        {label}
        {required && <span className="text-slate-600"> (required)</span>}
      </label>
      {children({
        id,
        'aria-describedby': describedBy,
        'aria-invalid': error ? true : undefined,
        'aria-required': required ? true : undefined,
      })}
      {error && (
        <p id={errorId} data-testid={`${id}-error`} className="text-sm text-red-700">
          {error}
        </p>
      )}
      {help && (
        <p id={helpId} className="text-xs text-slate-600">
          {help}
        </p>
      )}
    </div>
  );
}

export const INPUT_CLASS =
  'w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-600 aria-[invalid=true]:border-red-600';

export const BUTTON_PRIMARY =
  'rounded-md bg-blue-700 px-4 py-2 text-sm font-medium text-white hover:bg-blue-800 disabled:cursor-not-allowed disabled:opacity-60';
export const BUTTON_SECONDARY =
  'rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-800 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-60';
export const BUTTON_DANGER =
  'rounded-md border border-red-300 bg-white px-4 py-2 text-sm font-medium text-red-800 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-60';

// -- code and copy --------------------------------------------------------------

/** A labelled, focusable, scrollable code block with a specific Copy button. */
export function CopyBlock({ label, text, testId }: { label: string; text: string; testId?: string }) {
  const [copied, setCopied] = useState<'idle' | 'copied' | 'failed'>('idle');
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied('copied');
    } catch {
      setCopied('failed');
    }
  };
  return (
    <div className="space-y-2">
      <pre
        tabIndex={0}
        role="region"
        aria-label={label}
        data-testid={testId}
        className="max-h-60 overflow-auto whitespace-pre-wrap break-all rounded-md border border-slate-200 bg-slate-50 p-3 font-mono text-xs text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-600"
      >
        {text}
      </pre>
      <div className="flex items-center gap-3">
        <button type="button" onClick={copy} className={BUTTON_SECONDARY}>
          Copy {label}
        </button>
        <span aria-live="polite" className="text-sm text-slate-700">
          {copied === 'copied' ? 'Copied' : copied === 'failed' ? 'Copy failed: select the text and copy it' : ''}
        </span>
      </div>
    </div>
  );
}

// -- dialog ---------------------------------------------------------------------

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

/** A modal confirm: traps focus, closes on Escape, returns focus to its trigger. */
export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  busy,
  error,
  onConfirm,
  onCancel,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  busy?: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const bodyId = useId();
  useEffect(() => {
    const trigger = document.activeElement as HTMLElement | null;
    const first = ref.current?.querySelector<HTMLElement>(FOCUSABLE);
    first?.focus();
    return () => trigger?.focus?.();
  }, []);
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onCancel();
        return;
      }
      if (e.key !== 'Tab' || !ref.current) return;
      const items = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    },
    [onCancel],
  );
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4">
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        onKeyDown={onKeyDown}
        className="w-full max-w-md rounded-lg bg-white p-6 shadow-xl"
      >
        <h2 id={titleId} className="text-lg font-semibold text-slate-900">
          {title}
        </h2>
        <div id={bodyId} className="mt-2 space-y-2 text-sm text-slate-700">
          {children}
        </div>
        {error && (
          <div className="mt-3">
            <ActionError message={error} />
          </div>
        )}
        <div className="mt-5 flex justify-end gap-3">
          <button type="button" onClick={onCancel} className={BUTTON_SECONDARY}>
            Cancel
          </button>
          <button type="button" onClick={onConfirm} disabled={busy} className={BUTTON_DANGER}>
            {busy ? 'Working…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

// -- words ------------------------------------------------------------------------

export const CREDENTIALS_STATUS_TEXT: Record<CredentialsStatus, string> = {
  ok: 'Credentials stored',
  unavailable: "Credentials can't be used: this deployment has no WAREHOUSE_CREDENTIALS_KEYS set",
  needs_new_credentials: "Needs new credentials: the stored ones can't be read with this deployment's keys",
  pending_key: 'New key waiting: test the connection to switch to it',
};

export function notAvailableText(connection: Pick<Connection, 'warehouse_type'>): string {
  return `${warehouseName(connection.warehouse_type)} isn't available on this deployment yet.`;
}

/** An absolute UTC time, in a `<time>` element. */
export function UtcTime({ value }: { value: string | null | undefined }) {
  if (!value) return <span>—</span>;
  const date = parseApiTimestamp(value);
  if (Number.isNaN(date.getTime())) return <span>—</span>;
  const text = `${date.toISOString().slice(0, 16).replace('T', ' ')} UTC`;
  return <time dateTime={date.toISOString()}>{text}</time>;
}
