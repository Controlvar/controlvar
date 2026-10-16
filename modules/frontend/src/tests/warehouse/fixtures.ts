/**
 * Shapes the warehouse API returns (responses_warehouse.py), for the screen
 * tests. Connectors ship disabled; `connectors({bigquery: true})` is what a
 * flip PR would make the API answer.
 */
import type { UserMe } from '@/services/api';
import type { Connection, Connector, Source, WarehouseType } from '@modules/services/warehouse';

export const WAREHOUSE_INFO = { profile: 'full' as const, modules: ['warehouse'], version: 'test' };

export function user(role: UserMe['role'], is_superuser = false): UserMe {
  return {
    id: `u-${role.toLowerCase()}`,
    email: `${role.toLowerCase()}@example.com`,
    username: role.toLowerCase(),
    full_name: null,
    role,
    is_superuser,
    is_active: true,
    auth_provider: 'local',
  };
}

export function connectors(enabled: Partial<Record<WarehouseType, boolean>> = {}): Connector[] {
  return [
    { warehouse_type: 'bigquery', name: 'BigQuery', enabled: !!enabled.bigquery },
    { warehouse_type: 'snowflake', name: 'Snowflake', enabled: !!enabled.snowflake },
    { warehouse_type: 'athena', name: 'Amazon Athena', enabled: !!enabled.athena },
  ];
}

export function connection(overrides: Partial<Connection> = {}): Connection {
  return {
    id: 'c-1',
    name: 'Prod analytics',
    warehouse_type: 'bigquery',
    parameters: {
      billing_project: 'acme-billing',
      location: 'EU',
      client_email: 'controlvar@acme-billing.iam.gserviceaccount.com',
    },
    credentials_status: 'ok',
    public_key_fingerprint: null,
    pending_public_key_fingerprint: null,
    external_id: null,
    query_timeout_seconds: 300,
    max_bytes_per_query: 50_000_000_000,
    max_runs_per_day: 20,
    worst_case_bytes_per_day: 11_000_000_000_000,
    worst_case_seconds_per_day: null,
    enabled: true,
    created_at: '2026-09-28T10:00:00Z',
    updated_at: '2026-09-28T10:00:00Z',
    ...overrides,
  };
}

export function snowflakeConnection(overrides: Partial<Connection> = {}): Connection {
  return connection({
    id: 'c-sf',
    name: 'Snowflake prod',
    warehouse_type: 'snowflake',
    parameters: { account: 'MYORG-MYACCOUNT', user: 'CONTROLVAR_SVC', role: 'CONTROLVAR_READER', warehouse: 'ANALYTICS_XS' },
    public_key_fingerprint: 'SHA256:abc123=',
    max_bytes_per_query: null,
    worst_case_bytes_per_day: null,
    worst_case_seconds_per_day: 66000,
    ...overrides,
  });
}

export function source(overrides: Partial<Source> = {}): Source {
  return {
    id: 's-1',
    connection_id: 'c-1',
    kind: 'assignment',
    name: 'Exposures',
    table: 'acme-billing.experiments.exposures',
    columns: {
      unit_id: { name: 'user_id', type: 'STRING' },
      experiment_key: { name: 'experiment_key', type: 'STRING' },
      variant: { name: 'variant', type: 'STRING' },
      exposed_at: { name: 'exposed_at', type: 'TIMESTAMP' },
    },
    filters: [],
    metric_type: null,
    conversion_window_hours: null,
    cap_value: null,
    validated_at: '2026-09-28T11:00:00Z',
    created_at: '2026-09-28T10:30:00Z',
    updated_at: '2026-09-28T11:00:00Z',
    ...overrides,
  };
}

export function metricSource(overrides: Partial<Source> = {}): Source {
  return source({
    id: 's-m',
    kind: 'metric',
    name: 'Purchases',
    table: 'acme-billing.experiments.purchases',
    columns: { unit_id: { name: 'user_id', type: 'STRING' }, event_at: { name: 'purchased_at', type: 'TIMESTAMP' } },
    metric_type: 'proportion',
    conversion_window_hours: 168,
    ...overrides,
  });
}

/**
 * A service-account key carrying a sentinel in its private key. The sentinel
 * must never appear in the page, the console or browser storage once the
 * form has sent it.
 */
export const KEY_SENTINEL = 'SENTINEL-PRIVATE-KEY-7f3a9c';
// Not a key: a placeholder body between PEM-style markers, built at runtime so
// no scanner mistakes this file for one.
const PEM = ['BEGIN', 'END'].map((w) => `-----${w} ${'PRIVATE'} KEY-----`);
export const SERVICE_ACCOUNT_JSON = JSON.stringify({
  type: 'service_account',
  project_id: 'acme-billing',
  private_key_id: 'kid-1',
  private_key: `${PEM[0]}\n${KEY_SENTINEL}\n${PEM[1]}\n`,
  client_email: 'controlvar@acme-billing.iam.gserviceaccount.com',
  token_uri: 'https://oauth2.googleapis.com/token',
});

/** An `ApiError`-shaped refusal, as `apiFetch` throws it. */
export function apiError(status: number, code: string, message: string, field?: string) {
  const err = new Error(message) as Error & { status: number; code: string; detail: unknown };
  err.name = 'ApiError';
  err.status = status;
  err.code = code;
  err.detail = { code, message, ...(field ? { field } : {}) };
  return err;
}
