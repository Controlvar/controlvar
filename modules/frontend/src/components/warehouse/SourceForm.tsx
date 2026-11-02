/**
 * Create or edit a warehouse source: a table or view in the customer's
 * warehouse and which of its columns mean what. There is no SQL field; the
 * query is generated from this mapping. The page decides who may use it.
 */
import React, { useRef, useState } from 'react';
import {
  Connection,
  FilterOperator,
  FilterScalar,
  MetricType,
  Source,
  SourceBody,
  SourceFilter,
  SourceKind,
  warehouseName,
  warehouseService,
} from '@modules/services/warehouse';
import {
  ActionError,
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  Field,
  INPUT_CLASS,
  errorField,
  errorText,
} from '@modules/components/warehouse/common';
import {
  ASSIGNMENT_COLUMNS,
  DIALECTS,
  FieldErrors,
  LITERAL,
  LITERAL_HELP,
  MAX_FILTERS,
  MAX_IN_VALUES,
  METRIC_COLUMNS,
  checkColumn,
  checkInteger,
  checkName,
  checkTable,
} from '@modules/components/warehouse/validation';

type ValueType = 'text' | 'number' | 'boolean';

interface FilterRow {
  key: number;
  column: string;
  operator: FilterOperator;
  valueType: ValueType;
  value: string;
}

const OPERATORS: { value: FilterOperator; label: string }[] = [
  { value: 'eq', label: 'equals' },
  { value: 'ne', label: 'does not equal' },
  { value: 'in', label: 'is one of' },
  { value: 'not_in', label: 'is none of' },
  { value: 'is_null', label: 'is empty (NULL)' },
  { value: 'is_not_null', label: 'is not empty' },
];

const NO_VALUE: FilterOperator[] = ['is_null', 'is_not_null'];
const LIST_VALUE: FilterOperator[] = ['in', 'not_in'];

let filterKey = 0;

function rowsFrom(filters: SourceFilter[] | undefined): FilterRow[] {
  return (filters ?? []).map((f) => {
    const values = Array.isArray(f.value) ? f.value : f.value === undefined || f.value === null ? [] : [f.value];
    const first = values[0];
    const valueType: ValueType = typeof first === 'number' ? 'number' : typeof first === 'boolean' ? 'boolean' : 'text';
    return {
      key: ++filterKey,
      column: f.column,
      operator: f.operator,
      valueType,
      value: values.map(String).join(', '),
    };
  });
}

function parseScalar(raw: string, type: ValueType): FilterScalar | null {
  const text = raw.trim();
  if (type === 'number') return /^-?[0-9]{1,19}$/.test(text) ? Number(text) : null;
  if (type === 'boolean') return text === 'true' ? true : text === 'false' ? false : null;
  return LITERAL.test(text) ? text : null;
}

function scalarHelp(type: ValueType): string {
  if (type === 'number') return 'Use a whole number.';
  if (type === 'boolean') return 'Use true or false.';
  return LITERAL_HELP;
}

export interface SourceFormProps {
  mode: 'create' | 'edit';
  kind: SourceKind;
  connections: Connection[];
  source?: Source | null;
  /** Preselected connection on create. */
  connectionId?: string | null;
  onSaved: (source: Source) => void;
  onCancel: () => void;
}

export function SourceForm({ mode, kind, connections, source, connectionId, onSaved, onCancel }: SourceFormProps) {
  const [connection, setConnection] = useState<string>(
    source?.connection_id ?? connectionId ?? connections[0]?.id ?? '',
  );
  const [name, setName] = useState(source?.name ?? '');
  const [table, setTable] = useState(source?.table ?? '');
  const [columns, setColumns] = useState<Record<string, string>>(() => {
    const out: Record<string, string> = {};
    for (const [role, col] of Object.entries(source?.columns ?? {})) out[role] = col.name;
    return out;
  });
  const [metricType, setMetricType] = useState<MetricType>(source?.metric_type ?? 'proportion');
  // An edit starts from the stored direction: the PUT replaces the whole
  // definition, so starting unticked would set a marked source back to false.
  const [lowerIsBetter, setLowerIsBetter] = useState(source?.lower_is_better === true);
  const [windowHours, setWindowHours] = useState(String(source?.conversion_window_hours ?? 168));
  const [cap, setCap] = useState(source?.cap_value != null ? String(source.cap_value) : '');
  const [filters, setFilters] = useState<FilterRow[]>(() => rowsFrom(source?.filters));
  const [errors, setErrors] = useState<FieldErrors>({});
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const formRef = useRef<HTMLFormElement>(null);

  const selected = connections.find((c) => c.id === connection) ?? null;
  const dialect = selected ? DIALECTS[selected.warehouse_type] : null;
  const columnRoles =
    kind === 'assignment'
      ? [...ASSIGNMENT_COLUMNS]
      : [
          ...METRIC_COLUMNS,
          ...(metricType === 'mean'
            ? [{ role: 'value', label: 'Value column', help: 'A numeric column; each user’s values are summed.' }]
            : []),
        ];

  const clear = (field: string) =>
    setErrors((prev) => {
      if (!prev[field]) return prev;
      const next = { ...prev };
      delete next[field];
      return next;
    });

  const validate = (): SourceBody | null => {
    const e: FieldErrors = {};
    const put = (field: string, message: string | null) => {
      if (message) e[field] = message;
    };
    if (!selected) e.connection_id = 'Choose a connection.';
    put('name', checkName(name));
    if (selected) {
      const type = selected.warehouse_type;
      put('table', checkTable(table, type));
      for (const c of columnRoles) put(`columns.${c.role}`, checkColumn(columns[c.role] ?? '', type, c.label));
      filters.forEach((f, i) => {
        put(`filters.${i}.column`, checkColumn(f.column, type, 'Column'));
        if (NO_VALUE.includes(f.operator)) return;
        const parts = LIST_VALUE.includes(f.operator) ? f.value.split(',') : [f.value];
        if (LIST_VALUE.includes(f.operator) && parts.length > MAX_IN_VALUES) {
          e[`filters.${i}.value`] = `Use at most ${MAX_IN_VALUES} values.`;
        } else if (parts.some((p) => parseScalar(p, f.valueType) === null)) {
          e[`filters.${i}.value`] = scalarHelp(f.valueType);
        }
      });
    }
    if (kind === 'metric') {
      put('conversion_window_hours', checkInteger(windowHours, 'Conversion window', 1, 8760));
      if (metricType === 'mean' && cap.trim()) {
        const n = Number(cap);
        if (!Number.isFinite(n) || n <= 0) e.cap_value = 'The cap must be a number above 0.';
      }
    }
    setErrors(e);
    const firstKey = Object.keys(e)[0];
    if (firstKey) {
      setSubmitError(null);
      formRef.current?.querySelector<HTMLElement>(`[data-field="${firstKey}"]`)?.focus();
      return null;
    }

    const bodyFilters: SourceFilter[] = filters.map((f) => {
      if (NO_VALUE.includes(f.operator)) return { column: f.column, operator: f.operator };
      if (LIST_VALUE.includes(f.operator)) {
        return {
          column: f.column,
          operator: f.operator,
          value: f.value.split(',').map((p) => parseScalar(p, f.valueType) as FilterScalar),
        };
      }
      return { column: f.column, operator: f.operator, value: parseScalar(f.value, f.valueType) as FilterScalar };
    });
    const common = {
      name,
      table,
      filters: bodyFilters,
      ...(mode === 'create' ? { connection_id: connection } : {}),
    };
    if (kind === 'assignment') {
      return {
        ...common,
        kind: 'assignment',
        columns: {
          unit_id: columns.unit_id,
          experiment_key: columns.experiment_key,
          variant: columns.variant,
          exposed_at: columns.exposed_at,
        },
      };
    }
    return {
      ...common,
      kind: 'metric',
      columns: {
        unit_id: columns.unit_id,
        event_at: columns.event_at,
        ...(metricType === 'mean' ? { value: columns.value } : {}),
      },
      metric_type: metricType,
      conversion_window_hours: Number(windowHours),
      ...(metricType === 'mean' && cap.trim() ? { cap_value: Number(cap) } : {}),
      // Sent only when ticked: the API reads a missing key as false, and an
      // API older than the field refuses the key, so an unticked save still
      // works against one during a deploy.
      ...(lowerIsBetter ? { lower_is_better: true } : {}),
    };
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitError(null);
    const body = validate();
    if (!body) return;
    setSaving(true);
    try {
      const saved =
        mode === 'create'
          ? await warehouseService.createSource(body)
          : await warehouseService.updateSource(source!.id, body);
      onSaved(saved);
    } catch (err) {
      const field = errorField(err);
      const message = errorText(err, 'The source was not saved.');
      const target = field ? formRef.current?.querySelector<HTMLElement>(`[data-field="${field}"]`) : null;
      if (field && target) {
        setErrors({ [field]: message });
        target.focus();
      } else {
        setSubmitError(message);
      }
    } finally {
      setSaving(false);
    }
  };

  const setFilter = (index: number, patch: Partial<FilterRow>) => {
    setFilters((rows) => rows.map((r, i) => (i === index ? { ...r, ...patch } : r)));
    clear(`filters.${index}.column`);
    clear(`filters.${index}.value`);
  };

  return (
    <form ref={formRef} onSubmit={submit} noValidate className="space-y-6" data-testid="warehouse-source-form">
      {mode === 'create' ? (
        <Field id="wh-src-connection" label="Connection" required error={errors.connection_id}>
          {(aria) => (
            <select
              {...aria}
              data-field="connection_id"
              value={connection}
              onChange={(e) => {
                setConnection(e.target.value);
                clear('connection_id');
              }}
              className={INPUT_CLASS}
            >
              {connections.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name} ({warehouseName(c.warehouse_type)}
                  {c.enabled ? '' : ', not yet available'})
                </option>
              ))}
            </select>
          )}
        </Field>
      ) : (
        <p className="text-sm text-slate-700">
          Connection: <strong>{selected ? `${selected.name} (${warehouseName(selected.warehouse_type)})` : '—'}</strong>{' '}
          (a source&apos;s connection can&apos;t be changed)
        </p>
      )}

      <Field id="wh-src-name" label="Name" required error={errors.name}>
        {(aria) => (
          <input
            {...aria}
            data-field="name"
            type="text"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              clear('name');
            }}
            className={INPUT_CLASS}
          />
        )}
      </Field>

      <Field
        id="wh-src-table"
        label="Table or view"
        required
        error={errors.table}
        help={
          <>
            {dialect ? `Write it as ${dialect.shape}, e.g. ${dialect.example}. ` : ''}
            Need to reshape it? Create a view in your warehouse and use that.
          </>
        }
      >
        {(aria) => (
          <input
            {...aria}
            data-field="table"
            type="text"
            value={table}
            onChange={(e) => {
              setTable(e.target.value);
              clear('table');
            }}
            autoComplete="off"
            spellCheck={false}
            className={`${INPUT_CLASS} font-mono`}
          />
        )}
      </Field>

      {kind === 'metric' && (
        <fieldset className="space-y-2">
          <legend className="text-sm font-medium text-slate-800">Metric type</legend>
          {(
            [
              ['proportion', 'Proportion', 'The share of users with at least one event in the conversion window.'],
              ['mean', 'Mean', 'The average, per user, of the value column summed over the conversion window.'],
            ] as const
          ).map(([value, label, help]) => (
            <div key={value} className="flex items-start gap-2">
              <input
                id={`wh-src-metric-${value}`}
                type="radio"
                name="metric_type"
                value={value}
                checked={metricType === value}
                aria-describedby={`wh-src-metric-${value}-help`}
                onChange={() => setMetricType(value)}
                className="mt-1"
              />
              <div>
                <label htmlFor={`wh-src-metric-${value}`} className="text-sm text-slate-900">
                  {label}
                </label>
                <p id={`wh-src-metric-${value}-help`} className="text-xs text-slate-600">
                  {help}
                </p>
              </div>
            </div>
          ))}
        </fieldset>
      )}

      {kind === 'metric' && (
        <div className="flex items-start gap-2">
          <input
            id="wh-src-lower-is-better"
            type="checkbox"
            checked={lowerIsBetter}
            aria-describedby="wh-src-lower-is-better-help"
            onChange={(e) => setLowerIsBetter(e.target.checked)}
            className="mt-1"
          />
          <div>
            <label htmlFor="wh-src-lower-is-better" className="text-sm text-slate-900">
              Lower is better
            </label>
            <p id="wh-src-lower-is-better-help" className="text-xs text-slate-600">
              For a metric where a fall is the improvement, such as an error rate or churn: an analysis then
              calls a significant fall better and a significant rise worse. Analyses already started keep the
              direction they used.
            </p>
          </div>
        </div>
      )}

      <fieldset className="space-y-4 rounded-md border border-slate-200 p-4">
        <legend className="px-1 text-sm font-medium text-slate-800">Columns</legend>
        {columnRoles.map((c) => (
          <Field
            key={c.role}
            id={`wh-src-col-${c.role}`}
            label={c.label}
            help={c.help}
            required
            error={errors[`columns.${c.role}`]}
          >
            {(aria) => (
              <input
                {...aria}
                data-field={`columns.${c.role}`}
                type="text"
                value={columns[c.role] ?? ''}
                onChange={(e) => {
                  const value = e.target.value;
                  setColumns((prev) => ({ ...prev, [c.role]: value }));
                  clear(`columns.${c.role}`);
                }}
                autoComplete="off"
                spellCheck={false}
                className={`${INPUT_CLASS} font-mono`}
              />
            )}
          </Field>
        ))}
      </fieldset>

      {kind === 'metric' && (
        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            id="wh-src-window"
            label="Conversion window (hours)"
            required
            error={errors.conversion_window_hours}
            help="Events count from a user’s assignment until this many hours after it. Default 168 (7 days)."
          >
            {(aria) => (
              <input
                {...aria}
                data-field="conversion_window_hours"
                type="text"
                inputMode="numeric"
                value={windowHours}
                onChange={(e) => {
                  setWindowHours(e.target.value);
                  clear('conversion_window_hours');
                }}
                className={INPUT_CLASS}
              />
            )}
          </Field>
          {metricType === 'mean' && (
            <Field
              id="wh-src-cap"
              label="Cap per user (optional)"
              error={errors.cap_value}
              help="Each user’s summed value is capped here, so one outlier cannot dominate."
            >
              {(aria) => (
                <input
                  {...aria}
                  data-field="cap_value"
                  type="text"
                  inputMode="decimal"
                  value={cap}
                  onChange={(e) => {
                    setCap(e.target.value);
                    clear('cap_value');
                  }}
                  className={INPUT_CLASS}
                />
              )}
            </Field>
          )}
        </div>
      )}

      <fieldset className="space-y-3">
        <legend className="text-sm font-medium text-slate-800">Filters (optional, up to {MAX_FILTERS})</legend>
        {filters.length === 0 && <p className="text-sm text-slate-600">No filters: every row of the table is used.</p>}
        {filters.map((f, i) => (
          <div key={f.key} className="grid gap-2 rounded-md border border-slate-200 p-3 sm:grid-cols-5" data-testid="wh-src-filter">
            <Field id={`wh-src-f${i}-column`} label={`Filter ${i + 1} column`} error={errors[`filters.${i}.column`]}>
              {(aria) => (
                <input
                  {...aria}
                  data-field={`filters.${i}.column`}
                  type="text"
                  value={f.column}
                  onChange={(e) => setFilter(i, { column: e.target.value })}
                  spellCheck={false}
                  className={`${INPUT_CLASS} font-mono`}
                />
              )}
            </Field>
            <Field id={`wh-src-f${i}-operator`} label="Condition">
              {(aria) => (
                <select
                  {...aria}
                  value={f.operator}
                  onChange={(e) => setFilter(i, { operator: e.target.value as FilterOperator })}
                  className={INPUT_CLASS}
                >
                  {OPERATORS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              )}
            </Field>
            {!NO_VALUE.includes(f.operator) && (
              <>
                <Field id={`wh-src-f${i}-type`} label="Value type">
                  {(aria) => (
                    <select
                      {...aria}
                      value={f.valueType}
                      onChange={(e) => setFilter(i, { valueType: e.target.value as ValueType })}
                      className={INPUT_CLASS}
                    >
                      <option value="text">Text</option>
                      <option value="number">Whole number</option>
                      <option value="boolean">True or false</option>
                    </select>
                  )}
                </Field>
                <Field
                  id={`wh-src-f${i}-value`}
                  label={LIST_VALUE.includes(f.operator) ? 'Values (comma-separated)' : 'Value'}
                  error={errors[`filters.${i}.value`]}
                >
                  {(aria) => (
                    <input
                      {...aria}
                      data-field={`filters.${i}.value`}
                      type="text"
                      value={f.value}
                      onChange={(e) => setFilter(i, { value: e.target.value })}
                      spellCheck={false}
                      className={INPUT_CLASS}
                    />
                  )}
                </Field>
              </>
            )}
            <div className="flex items-end">
              <button
                type="button"
                onClick={() => setFilters((rows) => rows.filter((_, j) => j !== i))}
                className={BUTTON_SECONDARY}
              >
                Remove filter {i + 1}
              </button>
            </div>
          </div>
        ))}
        {filters.length < MAX_FILTERS && (
          <button
            type="button"
            onClick={() =>
              setFilters((rows) => [...rows, { key: ++filterKey, column: '', operator: 'eq', valueType: 'text', value: '' }])
            }
            className={BUTTON_SECONDARY}
          >
            Add filter
          </button>
        )}
      </fieldset>

      {mode === 'edit' && (
        <p className="text-sm text-slate-700">Saving clears the source&apos;s validation: validate it again before using it.</p>
      )}

      {submitError && <ActionError message={submitError} />}

      <div className="flex gap-3">
        <button type="submit" disabled={saving} className={BUTTON_PRIMARY}>
          {saving ? 'Saving…' : mode === 'create' ? 'Save source' : 'Save changes'}
        </button>
        <button type="button" onClick={onCancel} className={BUTTON_SECONDARY}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export default SourceForm;
