/**
 * Create or edit a warehouse connection (ADMIN only; the page decides).
 *
 * The one secret, a BigQuery service-account key, lives in this component's
 * state and nowhere else: it is sent in the request body, it is cleared the
 * moment a save succeeds, and nothing renders it except the textarea it was
 * typed into. The API never returns it.
 *
 * Only enabled connectors can be chosen. A connector the deployment has not
 * enabled is listed, marked "Not yet available", and its option is disabled;
 * if the chosen type is somehow not enabled, nothing is sent.
 */
import React, { useMemo, useRef, useState } from 'react';
import {
  AthenaBody,
  BigQueryBody,
  Connection,
  ConnectionBody,
  ConnectionCreated,
  Connector,
  SnowflakeBody,
  WarehouseType,
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
  ATHENA_DATABASE,
  ATHENA_WORKGROUP,
  AWS_REGION,
  BIGQUERY_LOCATION,
  BIGQUERY_LOCATION_HELP,
  BIGQUERY_PROJECT,
  BIGQUERY_PROJECT_HELP,
  FieldErrors,
  checkBytesPerQuery,
  checkInteger,
  checkName,
  checkPattern,
  checkServiceAccountJson,
  checkSnowflakeAccount,
  checkSnowflakeObject,
  checkSnowflakeRole,
  formatBytes,
  gigabytesToBytes,
  ROLE_ARN,
} from '@modules/components/warehouse/validation';

type Values = Record<string, string>;

const DEFAULTS: Values = {
  name: '',
  query_timeout_seconds: '300',
  max_runs_per_day: '20',
  max_bytes_per_query: '',
  account: '',
  user: '',
  role: '',
  warehouse: '',
  billing_project: '',
  location: '',
  region: '',
  role_arn: '',
  workgroup: '',
  database: '',
};

/** Where an API `field` lands in this form. */
const FIELD_ORDER = [
  'name',
  'account',
  'user',
  'role',
  'warehouse',
  'billing_project',
  'location',
  'region',
  'role_arn',
  'workgroup',
  'database',
  'service_account_json',
  'max_bytes_per_query',
  'query_timeout_seconds',
  'max_runs_per_day',
];

function initialValues(connection?: Connection | null): Values {
  if (!connection) return { ...DEFAULTS };
  const values: Values = { ...DEFAULTS, ...connection.parameters };
  values.name = connection.name;
  values.query_timeout_seconds = String(connection.query_timeout_seconds);
  values.max_runs_per_day = String(connection.max_runs_per_day);
  values.max_bytes_per_query =
    connection.max_bytes_per_query !== null ? String(connection.max_bytes_per_query / 1e9) : '';
  return values;
}

function check(type: WarehouseType, v: Values, key: string, keyRequired: boolean): FieldErrors {
  const e: FieldErrors = {};
  const put = (field: string, message: string | null) => {
    if (message) e[field] = message;
  };
  put('name', checkName(v.name));
  put('query_timeout_seconds', checkInteger(v.query_timeout_seconds, 'Query time limit', 10, 86400));
  put('max_runs_per_day', checkInteger(v.max_runs_per_day, 'Analyses per day', 1, 10000));
  if (type === 'snowflake') {
    put('account', checkSnowflakeAccount(v.account));
    put('user', checkSnowflakeObject(v.user, 'User'));
    put('role', checkSnowflakeRole(v.role));
    put('warehouse', checkSnowflakeObject(v.warehouse, 'Warehouse'));
  } else if (type === 'bigquery') {
    put('billing_project', checkPattern(v.billing_project, BIGQUERY_PROJECT, 'Billing project', BIGQUERY_PROJECT_HELP));
    put('location', checkPattern(v.location, BIGQUERY_LOCATION, 'Location', BIGQUERY_LOCATION_HELP));
    put('max_bytes_per_query', checkBytesPerQuery(v.max_bytes_per_query));
    if (keyRequired || key.trim()) put('service_account_json', checkServiceAccountJson(key));
  } else {
    put('region', checkPattern(v.region, AWS_REGION, 'Region', 'Use an AWS region code such as eu-west-1.'));
    put(
      'role_arn',
      checkPattern(v.role_arn, ROLE_ARN, 'Role ARN', 'Use a role ARN such as arn:aws:iam::123456789012:role/ControlvarAthena.'),
    );
    put('workgroup', checkPattern(v.workgroup, ATHENA_WORKGROUP, 'Workgroup', 'Use letters, digits, . _ and - (up to 128).'));
    put('database', checkPattern(v.database, ATHENA_DATABASE, 'Database', 'Use lower-case letters, digits and _.'));
    put('max_bytes_per_query', checkBytesPerQuery(v.max_bytes_per_query));
  }
  return e;
}

function buildBody(type: WarehouseType, v: Values, key: string): ConnectionBody {
  const limits = {
    name: v.name,
    query_timeout_seconds: Number(v.query_timeout_seconds),
    max_runs_per_day: Number(v.max_runs_per_day),
  };
  if (type === 'snowflake') {
    const body: SnowflakeBody = {
      ...limits,
      warehouse_type: 'snowflake',
      account: v.account,
      user: v.user,
      role: v.role,
      warehouse: v.warehouse,
    };
    return body;
  }
  const bytes = gigabytesToBytes(v.max_bytes_per_query) ?? 0;
  if (type === 'bigquery') {
    const body: BigQueryBody = {
      ...limits,
      warehouse_type: 'bigquery',
      billing_project: v.billing_project,
      location: v.location,
      max_bytes_per_query: bytes,
    };
    if (key) body.service_account_json = key;
    return body;
  }
  const body: AthenaBody = {
    ...limits,
    warehouse_type: 'athena',
    region: v.region,
    role_arn: v.role_arn,
    workgroup: v.workgroup,
    database: v.database,
    max_bytes_per_query: bytes,
  };
  return body;
}

interface TestState {
  status: 'running' | 'passed' | 'failed';
  message: string;
  /** The edit count the test ran against; any later edit makes it out of date. */
  version: number;
}

export interface ConnectionFormProps {
  mode: 'create' | 'edit';
  connectors: Connector[];
  connection?: Connection | null;
  onSaved: (saved: ConnectionCreated | Connection) => void;
  onCancel: () => void;
}

export function ConnectionForm({ mode, connectors, connection, onSaved, onCancel }: ConnectionFormProps) {
  const enabledTypes = connectors.filter((c) => c.enabled).map((c) => c.warehouse_type);
  const [type, setType] = useState<WarehouseType | null>(
    mode === 'edit' && connection ? connection.warehouse_type : enabledTypes[0] ?? null,
  );
  const [values, setValues] = useState<Values>(() => initialValues(connection));
  // The service-account key: this state, the textarea, the request body. Nothing else.
  const [serviceAccountJson, setServiceAccountJson] = useState('');
  const [errors, setErrors] = useState<FieldErrors>({});
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [version, setVersion] = useState(0);
  const [test, setTest] = useState<TestState | null>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const resultRef = useRef<HTMLDivElement>(null);

  const typeEnabled = type !== null && enabledTypes.includes(type);
  const keyRequired = mode === 'create';
  // BigQuery only: an Athena role's trust policy needs the external ID generated on save, and a
  // Snowflake key pair is generated on save, so both are tested after saving.
  const testable = mode === 'create' && type === 'bigquery';
  // The UX rule: a BigQuery key is saved only once a test has passed with exactly these values.
  const testRequired = mode === 'create' && type === 'bigquery';
  const testCurrent = test !== null && test.version === version;
  const testPassed = testCurrent && test?.status === 'passed';

  const edited = () => setVersion((n) => n + 1);
  const set = (field: string) => (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value;
    setValues((v) => ({ ...v, [field]: value }));
    setErrors((prev) => {
      if (!prev[field]) return prev;
      const next = { ...prev };
      delete next[field];
      return next;
    });
    edited();
  };

  const focusFirstError = (found: FieldErrors) => {
    const first = FIELD_ORDER.find((f) => found[f]);
    if (first) formRef.current?.querySelector<HTMLElement>(`#wh-conn-${first}`)?.focus();
  };

  const validate = (): ConnectionBody | null => {
    if (!type || !typeEnabled) {
      setSubmitError(type ? `${warehouseName(type)} isn't available on this deployment yet.` : 'Choose a warehouse.');
      return null;
    }
    const found = check(type, values, serviceAccountJson, keyRequired);
    setErrors(found);
    if (Object.keys(found).length > 0) {
      setSubmitError(null);
      focusFirstError(found);
      return null;
    }
    return buildBody(type, values, serviceAccountJson);
  };

  const applyServerError = (err: unknown, fallback: string) => {
    const field = errorField(err);
    const message = errorText(err, fallback);
    if (field && FIELD_ORDER.includes(field)) {
      const found = { [field]: message };
      setErrors(found);
      setSubmitError(null);
      focusFirstError(found);
    } else {
      setSubmitError(message);
    }
  };

  const runTest = async () => {
    setSubmitError(null);
    const body = validate();
    if (!body || body.warehouse_type !== 'bigquery') return;
    const at = version;
    setTest({ status: 'running', message: 'Testing the connection…', version: at });
    try {
      await warehouseService.testUnsavedConnection(body);
      const time = new Date().toISOString().slice(11, 16);
      setTest({
        status: 'passed',
        message: `Test passed at ${time} UTC: signed in to ${warehouseName(body.warehouse_type)} and ran a query that reads no table.`,
        version: at,
      });
    } catch (err) {
      setTest({ status: 'failed', message: `Test failed: ${errorText(err, 'the warehouse did not answer.')}`, version: at });
    }
    resultRef.current?.focus();
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitError(null);
    const body = validate();
    if (!body) return;
    if (testRequired && !testPassed) {
      setSubmitError('Test the connection first: it is saved once a test has passed with these values.');
      return;
    }
    setSaving(true);
    try {
      const saved =
        mode === 'create'
          ? await warehouseService.createConnection(body)
          : await warehouseService.updateConnection(connection!.id, body);
      // The key has been sent; keep no copy of it.
      setServiceAccountJson('');
      onSaved(saved);
    } catch (err) {
      applyServerError(err, 'The connection was not saved.');
    } finally {
      setSaving(false);
    }
  };

  const chooseKeyFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    // Clear the input so the file's name is not kept either.
    e.target.value = '';
    if (!file) return;
    const text = await new Promise<string>((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : '');
      reader.onerror = () => resolve('');
      reader.readAsText(file);
    });
    setServiceAccountJson(text);
    setErrors((prev) => {
      const next = { ...prev };
      delete next.service_account_json;
      return next;
    });
    edited();
  };

  const worstCase = useMemo(() => {
    if (type === 'snowflake' || !type) return null;
    const bytes = gigabytesToBytes(values.max_bytes_per_query);
    const runs = Number(values.max_runs_per_day);
    if (bytes === null || !Number.isInteger(runs) || runs < 1) return null;
    return formatBytes(bytes * runs * 11);
  }, [type, values.max_bytes_per_query, values.max_runs_per_day]);

  const input = (field: string, label: string, help?: React.ReactNode, props: React.InputHTMLAttributes<HTMLInputElement> = {}) => (
    <Field id={`wh-conn-${field}`} label={label} help={help} error={errors[field]} required>
      {(aria) => (
        <input
          {...aria}
          type="text"
          value={values[field] ?? ''}
          onChange={set(field)}
          autoComplete="off"
          spellCheck={false}
          className={INPUT_CLASS}
          {...props}
        />
      )}
    </Field>
  );

  return (
    <form ref={formRef} onSubmit={submit} noValidate className="space-y-6" data-testid="warehouse-connection-form">
      {mode === 'create' ? (
        <fieldset className="space-y-2">
          <legend className="text-sm font-medium text-slate-800">Warehouse</legend>
          {connectors.map((c) => {
            const id = `wh-conn-type-${c.warehouse_type}`;
            return (
              <div key={c.warehouse_type} className="flex items-center gap-2">
                <input
                  id={id}
                  type="radio"
                  name="warehouse_type"
                  value={c.warehouse_type}
                  checked={type === c.warehouse_type}
                  disabled={!c.enabled}
                  aria-describedby={c.enabled ? undefined : `${id}-status`}
                  onChange={() => {
                    setType(c.warehouse_type);
                    setErrors({});
                    setTest(null);
                    edited();
                  }}
                />
                <label htmlFor={id} className={`text-sm ${c.enabled ? 'text-slate-900' : 'text-slate-600'}`}>
                  {c.name}
                </label>
                {!c.enabled && (
                  <span id={`${id}-status`} className="text-xs text-slate-600">
                    Not yet available
                  </span>
                )}
              </div>
            );
          })}
        </fieldset>
      ) : (
        <p className="text-sm text-slate-700">
          Warehouse: <strong>{type ? warehouseName(type) : ''}</strong> (a connection&apos;s warehouse can&apos;t be changed)
        </p>
      )}

      {type && (
        <>
          {input('name', 'Name', 'Shown wherever the connection is used, e.g. Prod analytics.')}

          {type === 'snowflake' && (
            <>
              {input('account', 'Account', 'The organisation-account identifier, e.g. MYORG-MYACCOUNT (Snowsight › Admin › Accounts).')}
              {input('user', 'User', 'A user made for this connection, e.g. CONTROLVAR_SVC.')}
              {input('role', 'Role', 'A role with read-only grants. It is always sent, so the user’s default role does not matter.')}
              {input('warehouse', 'Warehouse', 'The virtual warehouse queries run on, e.g. ANALYTICS_XS.')}
            </>
          )}

          {type === 'bigquery' && (
            <>
              {input('billing_project', 'Billing project', 'Query jobs run and are billed in this project.')}
              {input('location', 'Location', 'Where your datasets are: US, EU or a region such as europe-west2.')}
              <Field
                id="wh-conn-service_account_json"
                label={mode === 'create' ? 'Service-account key (JSON)' : 'Replace the service-account key (JSON)'}
                required={mode === 'create'}
                error={errors.service_account_json}
                help={
                  mode === 'create'
                    ? 'Paste the JSON key file, or choose it below. It is stored encrypted and never shown again.'
                    : 'Leave empty to keep the stored key. A new key replaces it when you save; it is never shown again.'
                }
              >
                {(aria) => (
                  <textarea
                    {...aria}
                    data-testid="wh-conn-service-account-json"
                    value={serviceAccountJson}
                    onChange={(e) => {
                      setServiceAccountJson(e.target.value);
                      setErrors((prev) => {
                        const next = { ...prev };
                        delete next.service_account_json;
                        return next;
                      });
                      edited();
                    }}
                    rows={6}
                    autoComplete="off"
                    spellCheck={false}
                    className={`${INPUT_CLASS} font-mono text-xs`}
                  />
                )}
              </Field>
              <div>
                <label htmlFor="wh-conn-key-file" className="block text-sm font-medium text-slate-800">
                  Or choose the key file
                </label>
                <input
                  id="wh-conn-key-file"
                  type="file"
                  accept="application/json,.json"
                  onChange={chooseKeyFile}
                  className="mt-1 text-sm text-slate-700"
                />
              </div>
            </>
          )}

          {type === 'athena' && (
            <>
              {input('region', 'Region', 'The AWS region of your Athena workgroup, e.g. eu-west-1.')}
              {input('role_arn', 'Role ARN', 'The role Controlvar assumes in your account. It must not be in this deployment’s own AWS account.')}
              {input('workgroup', 'Workgroup', 'A workgroup that enforces its settings, with a per-query data limit and a results location.')}
              {input('database', 'Database', 'The Glue database your tables are in.')}
            </>
          )}

          <fieldset className="space-y-4 rounded-md border border-slate-200 p-4">
            <legend className="px-1 text-sm font-medium text-slate-800">Limits</legend>
            {type !== 'snowflake' &&
              input(
                'max_bytes_per_query',
                'Most data a query may read (GB)',
                type === 'bigquery'
                  ? 'BigQuery refuses a query that would read more, before it runs or bills.'
                  : 'Must be at least your workgroup’s per-query data limit; runs are refused otherwise.',
                { inputMode: 'decimal' },
              )}
            {input('query_timeout_seconds', 'Query time limit (seconds)', 'The warehouse stops a query that runs longer. Default 300.', {
              inputMode: 'numeric',
            })}
            {input(
              'max_runs_per_day',
              'Analyses per day',
              <>
                Analyses and previews on this connection per day (UTC). Default 20.
                {worstCase && <> At most {worstCase} read per day.</>}
              </>,
              { inputMode: 'numeric' },
            )}
          </fieldset>

          {mode === 'create' && type === 'snowflake' && (
            <p className="text-sm text-slate-700">
              Saving generates a key pair. You then register its public key on the Snowflake user and test the
              connection. There is no password.
            </p>
          )}
          {mode === 'create' && type === 'athena' && (
            <p className="text-sm text-slate-700">
              Saving generates the external ID your role&apos;s trust policy must name; test the connection after
              saving.
            </p>
          )}

          {testable && (
            <div className="space-y-2">
              <button type="button" onClick={runTest} disabled={test?.status === 'running'} className={BUTTON_SECONDARY}>
                Test connection
              </button>
              <div
                ref={resultRef}
                tabIndex={-1}
                aria-live="polite"
                data-testid="wh-conn-test-result"
                className="text-sm focus:outline-none"
              >
                {test && (
                  <p className={test.status === 'failed' ? 'text-red-800' : 'text-slate-800'}>
                    {test.status === 'passed' ? 'Passed. ' : test.status === 'failed' ? 'Failed. ' : ''}
                    {test.message}
                    {!testCurrent && test.status !== 'running' && <strong> Out of date: test again.</strong>}
                  </p>
                )}
              </div>
            </div>
          )}

          {submitError && <ActionError message={submitError} />}

          <div className="flex gap-3">
            <button
              type="submit"
              disabled={saving || !typeEnabled}
              aria-describedby={testRequired && !testPassed ? 'wh-conn-save-note' : undefined}
              className={BUTTON_PRIMARY}
            >
              {saving ? 'Saving…' : mode === 'create' ? 'Save connection' : 'Save changes'}
            </button>
            <button type="button" onClick={onCancel} className={BUTTON_SECONDARY}>
              Cancel
            </button>
          </div>
          {testRequired && !testPassed && (
            <p id="wh-conn-save-note" className="text-xs text-slate-600">
              A BigQuery connection is saved once a test has passed with exactly these values.
            </p>
          )}
        </>
      )}
    </form>
  );
}

export default ConnectionForm;
