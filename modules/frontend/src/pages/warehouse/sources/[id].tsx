/**
 * One warehouse source: its table and column mapping, validation against the
 * warehouse's metadata, and a preview that returns counts only (no rows).
 * Each action is offered only to the roles the API allows for this kind of
 * source; the rest see which role it needs.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/router';
import { withModule } from '@/components/ModuleNotice';
import { PageTitle } from '@/components/PageTitle';
import { useAuth } from '@/contexts/AuthContext';
import {
  Connection,
  PreviewResult,
  Source,
  SourceColumn,
  SourceFilter,
  warehouseName,
  warehouseService,
} from '@modules/services/warehouse';
import { WarehouseActionName, can, refusal, sourceAction } from '@modules/services/warehouseRoles';
import {
  BUTTON_DANGER,
  BUTTON_SECONDARY,
  ConfirmDialog,
  Field,
  INPUT_CLASS,
  LoadError,
  Loading,
  RoleNotice,
  UtcTime,
  WAREHOUSE_MODULE_NOTICE,
  WarehouseHeader,
  errorText,
  notAvailableText,
} from '@modules/components/warehouse/common';
import { SourceForm } from '@modules/components/warehouse/SourceForm';

const COLUMN_LABELS: Record<string, string> = {
  unit_id: 'User ID',
  experiment_key: 'Experiment key',
  variant: 'Variant',
  exposed_at: 'Assignment time',
  event_at: 'Event time',
  value: 'Value',
};

const OPERATOR_TEXT: Record<string, string> = {
  eq: '=',
  ne: '≠',
  in: 'is one of',
  not_in: 'is none of',
  is_null: 'is empty',
  is_not_null: 'is not empty',
};

function filterText(f: SourceFilter): string {
  const value = Array.isArray(f.value) ? f.value.join(', ') : f.value === undefined || f.value === null ? '' : String(f.value);
  return `${f.column} ${OPERATOR_TEXT[f.operator] ?? f.operator}${value ? ` ${value}` : ''}`;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-3 gap-4 py-2">
      <dt className="text-sm font-medium text-slate-700">{label}</dt>
      <dd className="col-span-2 break-all text-sm text-slate-900">{children}</dd>
    </div>
  );
}

const EXPERIMENT_KEY = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;

function PreviewPanel({ source, connection }: { source: Source; connection: Connection | null }) {
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [experimentKey, setExperimentKey] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<PreviewResult | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const resultRef = useRef<HTMLDivElement>(null);

  const run = async (e: React.FormEvent) => {
    e.preventDefault();
    const found: Record<string, string> = {};
    if (from && !DATE.test(from)) found.from = 'Use a date such as 2026-09-01.';
    if (to && !DATE.test(to)) found.to = 'Use a date such as 2026-09-08.';
    if (from && to && !found.from && !found.to && from >= to) found.to = 'The end date must be after the start date.';
    if (experimentKey && !EXPERIMENT_KEY.test(experimentKey)) {
      found.experimentKey = 'Use the experiment key: letters, digits, _ . and -, starting with a letter or digit.';
    }
    setErrors(found);
    if (Object.keys(found).length) return;
    setRunning(true);
    setFailure(null);
    setResult(null);
    try {
      setResult(
        await warehouseService.previewSource(source.id, {
          ...(from ? { window_start: `${from}T00:00:00Z` } : {}),
          ...(to ? { window_end: `${to}T00:00:00Z` } : {}),
          ...(source.kind === 'assignment' && experimentKey ? { experiment_key: experimentKey } : {}),
        }),
      );
    } catch (err) {
      setFailure(errorText(err, 'The preview did not run.'));
    } finally {
      setRunning(false);
      resultRef.current?.focus();
    }
  };

  return (
    <section aria-labelledby="wh-preview-heading" className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
      <h2 id="wh-preview-heading" className="text-base font-semibold text-slate-900">
        Preview
      </h2>
      <p className="text-sm text-slate-700">
        A preview runs one query in your warehouse and returns counts and time bounds only, never rows. It counts
        towards this connection&apos;s limit of {connection?.max_runs_per_day ?? '—'} analyses per day. With no dates it
        covers the last 7 days.
      </p>
      <form onSubmit={run} noValidate className="grid gap-3 sm:grid-cols-3">
        <Field id="wh-preview-start" label="From (UTC date)" error={errors.from}>
          {(aria) => <input {...aria} type="date" value={from} onChange={(e) => setFrom(e.target.value)} className={INPUT_CLASS} />}
        </Field>
        <Field id="wh-preview-end" label="Until (UTC date, not included)" error={errors.to}>
          {(aria) => <input {...aria} type="date" value={to} onChange={(e) => setTo(e.target.value)} className={INPUT_CLASS} />}
        </Field>
        {source.kind === 'assignment' && (
          <Field id="wh-preview-key" label="Experiment key (optional)" error={errors.experimentKey}>
            {(aria) => (
              <input
                {...aria}
                type="text"
                value={experimentKey}
                onChange={(e) => setExperimentKey(e.target.value)}
                spellCheck={false}
                className={INPUT_CLASS}
              />
            )}
          </Field>
        )}
        <div className="sm:col-span-3">
          <button type="submit" disabled={running} className={BUTTON_SECONDARY}>
            {running ? 'Running preview…' : 'Run preview'}
          </button>
        </div>
      </form>
      <div ref={resultRef} tabIndex={-1} aria-live="polite" data-testid="warehouse-preview-result" className="focus:outline-none">
        {failure && <p className="text-sm text-red-800">Preview failed: {failure}</p>}
        {result && (
          <div className="space-y-3">
            <dl className="divide-y divide-slate-100">
              <Row label="Window">
                <UtcTime value={result.window_start} /> to <UtcTime value={result.window_end} />
              </Row>
              <Row label="Rows">{result.total_rows.toLocaleString('en-GB')}</Row>
              <Row label="Rows with no user ID">{result.null_unit_rows.toLocaleString('en-GB')}</Row>
              {result.null_variant_rows !== null && (
                <Row label="Rows with no variant">{result.null_variant_rows.toLocaleString('en-GB')}</Row>
              )}
              {result.null_value_rows !== null && (
                <Row label="Rows with no value">{result.null_value_rows.toLocaleString('en-GB')}</Row>
              )}
              <Row label="Earliest">
                <UtcTime value={result.earliest} />
              </Row>
              <Row label="Latest">
                <UtcTime value={result.latest} />
              </Row>
            </dl>
            {result.variants && (
              <table className="min-w-full text-sm">
                <caption className="text-left text-sm font-medium text-slate-800">Users per variant value</caption>
                <thead>
                  <tr className="text-left text-slate-700">
                    <th scope="col" className="py-1 pr-4 font-medium">Variant value</th>
                    <th scope="col" className="py-1 pr-4 font-medium">Users</th>
                  </tr>
                </thead>
                <tbody>
                  {result.variants.map((v) => (
                    <tr key={v.label}>
                      <td className="py-1 pr-4 font-mono text-xs">{v.label}</td>
                      <td className="py-1 pr-4">{v.units.toLocaleString('en-GB')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>
    </section>
  );
}

function SourcePage() {
  const { user } = useAuth();
  const router = useRouter();
  const id = typeof router.query.id === 'string' ? router.query.id : null;
  const allowed = can(user, 'viewSources');
  const [source, setSource] = useState<Source | null>(null);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [saved, setSaved] = useState(false);
  const [validating, setValidating] = useState(false);
  const [validation, setValidation] = useState<{ ok: boolean; message: string; columns?: SourceColumn[] } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const validationRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    if (!id) return;
    setError(null);
    setSource(null);
    try {
      const [found, list] = await Promise.all([warehouseService.getSource(id), warehouseService.listConnections()]);
      setSource(found);
      setConnections(list);
    } catch (err) {
      setError(errorText(err, 'The source could not be loaded.'));
    }
  }, [id]);

  useEffect(() => {
    if (allowed) void load();
  }, [allowed, load]);

  const validate = async () => {
    if (!source) return;
    setValidating(true);
    setValidation(null);
    try {
      const result = await warehouseService.validateSource(source.id);
      setSource(result.source);
      setValidation({
        ok: true,
        message: 'Validated: the table and the mapped columns exist, and their types are supported.',
        columns: result.columns,
      });
    } catch (err) {
      setValidation({ ok: false, message: `Not validated: ${errorText(err, 'the warehouse did not answer.')}` });
    } finally {
      setValidating(false);
      validationRef.current?.focus();
    }
  };

  const remove = async () => {
    if (!source) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await warehouseService.deleteSource(source.id);
      void router.push(`/warehouse?tab=${source.kind}`);
    } catch (err) {
      setDeleteError(errorText(err, 'The source was not deleted.'));
      setDeleting(false);
    }
  };

  const connection = source ? connections.find((c) => c.id === source.connection_id) ?? null : null;
  const kindLabel = source?.kind === 'metric' ? 'Metric source' : 'Assignment source';

  let body: React.ReactNode;
  if (!allowed) body = <RoleNotice message={refusal(user, 'viewSources')} />;
  else if (error) body = <LoadError message={error} onRetry={() => void load()} />;
  else if (!source) body = <Loading label="Loading the source…" />;
  else if (editing) {
    body = (
      <SourceForm
        mode="edit"
        kind={source.kind}
        connections={connections}
        source={source}
        onSaved={(updated) => {
          setSource(updated);
          setEditing(false);
          setSaved(true);
          setValidation(null);
        }}
        onCancel={() => setEditing(false)}
      />
    );
  } else {
    const s = source;
    const act = (verb: 'edit' | 'validate' | 'preview' | 'delete'): WarehouseActionName => sourceAction(verb, s.kind);
    const canEdit = can(user, act('edit'));
    const canValidate = can(user, act('validate'));
    const canPreview = can(user, act('preview'));
    const canDelete = can(user, act('delete'));
    const usable = connection?.enabled ?? false;
    // One sentence for what this role cannot do here, naming the first action it lacks.
    const missing = (['edit', 'delete'] as const).find((verb) => !can(user, act(verb)));
    body = (
      <div className="space-y-6">
        {saved && (
          <p role="status" className="text-sm text-slate-800">
            Changes saved. Validate the source again before using it.
          </p>
        )}
        {connection && !connection.enabled && (
          <p data-testid="warehouse-connection-unavailable" className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            {notAvailableText(connection)} This source can&apos;t be validated or previewed until it is.
          </p>
        )}
        <dl className="divide-y divide-slate-100 rounded-md border border-slate-200 bg-white px-4" data-testid="warehouse-source-details">
          <Row label="Kind">{kindLabel}</Row>
          <Row label="Connection">{connection ? `${connection.name} (${warehouseName(connection.warehouse_type)})` : '—'}</Row>
          <Row label="Table or view">
            <code className="font-mono text-xs">{s.table}</code>
          </Row>
          {Object.entries(s.columns).map(([role, col]) => (
            <Row key={role} label={`${COLUMN_LABELS[role] ?? role} column`}>
              <code className="font-mono text-xs">{col.name}</code>
              {col.type && <span className="text-slate-700"> ({col.type})</span>}
            </Row>
          ))}
          {s.kind === 'metric' && (
            <>
              <Row label="Metric type">{s.metric_type === 'mean' ? 'Mean' : 'Proportion'}</Row>
              <Row label="Lower is better">{s.lower_is_better === true ? 'Yes' : 'No'}</Row>
              <Row label="Conversion window">{s.conversion_window_hours} hours after assignment</Row>
              {s.cap_value !== null && <Row label="Cap per user">{s.cap_value}</Row>}
            </>
          )}
          <Row label="Filters">{s.filters.length === 0 ? 'None' : s.filters.map(filterText).join('; ')}</Row>
          <Row label="Validated">{s.validated_at ? <UtcTime value={s.validated_at} /> : 'Not validated'}</Row>
        </dl>

        <div className="space-y-3">
          <div className="flex flex-wrap gap-3">
            {canEdit && (
              <button type="button" onClick={() => setEditing(true)} className={BUTTON_SECONDARY}>
                Edit
              </button>
            )}
            {canValidate && (
              <button type="button" onClick={validate} disabled={!usable || validating} className={BUTTON_SECONDARY}>
                {validating ? 'Validating…' : 'Validate'}
              </button>
            )}
            {canDelete && (
              <button
                type="button"
                onClick={() => {
                  setDeleteError(null);
                  setConfirmDelete(true);
                }}
                className={BUTTON_DANGER}
              >
                Delete
              </button>
            )}
          </div>
          {missing && <RoleNotice message={refusal(user, act(missing))} />}
          <div ref={validationRef} tabIndex={-1} aria-live="polite" data-testid="warehouse-validation-result" className="text-sm focus:outline-none">
            {validation && (
              <div>
                <p className={validation.ok ? 'text-slate-800' : 'text-red-800'}>{validation.message}</p>
                {validation.columns && (
                  <details className="mt-2">
                    <summary className="cursor-pointer text-slate-800">
                      Columns in {s.table} ({validation.columns.length})
                    </summary>
                    <ul className="mt-1 font-mono text-xs text-slate-800">
                      {validation.columns.map((c) => (
                        <li key={c.name}>
                          {c.name} {c.type ?? ''}
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </div>
            )}
          </div>
        </div>

        {canPreview && usable && (s.validated_at ? (
          <PreviewPanel source={s} connection={connection} />
        ) : (
          <p className="text-sm text-slate-700" data-testid="warehouse-preview-needs-validation">
            Validate the source before previewing it.
          </p>
        ))}

        {confirmDelete && (
          <ConfirmDialog
            title={`Delete “${s.name}”?`}
            confirmLabel="Delete source"
            busy={deleting}
            error={deleteError}
            onConfirm={remove}
            onCancel={() => setConfirmDelete(false)}
          >
            <p>The source is deleted. Past analyses keep their results; new analyses can&apos;t use it.</p>
          </ConfirmDialog>
        )}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl p-6">
      <PageTitle title={source ? `${source.name} · Warehouse` : 'Warehouse source'} />
      <WarehouseHeader
        title={source?.name ?? 'Source'}
        crumbs={[
          { label: 'Warehouse', href: source ? `/warehouse?tab=${source.kind}` : '/warehouse' },
          { label: source?.name ?? 'Source' },
        ]}
      />
      {body}
    </div>
  );
}

export default withModule(SourcePage, WAREHOUSE_MODULE_NOTICE);
