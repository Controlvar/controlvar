/**
 * Warehouse analysis API (`/api/v1/warehouse/analysis`, x-stability: beta).
 *
 * Connections and sources (W5a). Every call goes through `apiFetch`, so a
 * refusal arrives as an `ApiError` whose `detail` is `{code, message, field?}`.
 *
 * Credentials: the only secret a request can carry is a BigQuery
 * service-account key, and it travels in the JSON body of a create, an update
 * or a test -- never in a URL. No response carries it back, and nothing here
 * keeps a copy: the caller's form owns the value and clears it after a save.
 */
import { apiFetch } from '@/services/api';


export type WarehouseType = 'bigquery' | 'snowflake' | 'athena';

export const WAREHOUSE_TYPES: readonly WarehouseType[] = ['bigquery', 'snowflake', 'athena'];

/** The names the API uses in its own copy. */
export const WAREHOUSE_NAMES: Record<WarehouseType, string> = {
  bigquery: 'BigQuery',
  snowflake: 'Snowflake',
  athena: 'Amazon Athena',
};

export function warehouseName(type: string): string {
  return (WAREHOUSE_NAMES as Record<string, string>)[type] ?? type;
}

export interface Connector {
  warehouse_type: WarehouseType;
  name: string;
  enabled: boolean;
}

export type CredentialsStatus = 'ok' | 'unavailable' | 'needs_new_credentials' | 'pending_key';

export interface Connection {
  id: string;
  name: string;
  warehouse_type: WarehouseType;
  /** Non-secret parameters only (account, user, role, billing project, ...). */
  parameters: Record<string, string>;
  credentials_status: CredentialsStatus;
  public_key_fingerprint: string | null;
  pending_public_key_fingerprint: string | null;
  external_id: string | null;
  query_timeout_seconds: number;
  max_bytes_per_query: number | null;
  max_runs_per_day: number;
  worst_case_bytes_per_day: number | null;
  worst_case_seconds_per_day: number | null;
  /** False when the connection's connector is not available on this deployment. */
  enabled: boolean;
  created_at: string | null;
  updated_at: string | null;
}

/** What the customer runs in Snowflake to register a generated key. */
export interface PublicKeyStatement {
  public_key: string;
  public_key_fingerprint: string;
  statement: string;
}

export interface ConnectionCreated extends Connection {
  public_key: PublicKeyStatement | null;
}

export interface ConnectionTestResult {
  ok: boolean;
  warehouse_type: WarehouseType;
  promoted_pending_key: boolean;
}

interface LimitsBody {
  name: string;
  query_timeout_seconds: number;
  max_runs_per_day: number;
}

export interface SnowflakeBody extends LimitsBody {
  warehouse_type: 'snowflake';
  account: string;
  user: string;
  role: string;
  warehouse: string;
}

export interface BigQueryBody extends LimitsBody {
  warehouse_type: 'bigquery';
  billing_project: string;
  location: string;
  max_bytes_per_query: number;
  /** Write-only. Required on create and test; left out on update to keep the stored key. */
  service_account_json?: string;
}

export interface AthenaBody extends LimitsBody {
  warehouse_type: 'athena';
  region: string;
  role_arn: string;
  workgroup: string;
  database: string;
  max_bytes_per_query: number;
}

export type ConnectionBody = SnowflakeBody | BigQueryBody | AthenaBody;

export type SourceKind = 'assignment' | 'metric';
export type MetricType = 'proportion' | 'mean';
export type FilterOperator = 'eq' | 'ne' | 'in' | 'not_in' | 'is_null' | 'is_not_null';
export type FilterScalar = string | number | boolean;

export interface SourceFilter {
  column: string;
  operator: FilterOperator;
  value?: FilterScalar | FilterScalar[] | null;
}

export interface SourceColumn {
  name: string;
  type: string | null;
}

export interface Source {
  id: string;
  connection_id: string;
  kind: SourceKind;
  name: string;
  table: string;
  /** Role (`unit_id`, `experiment_key`, ...) -> the mapped column. */
  columns: Record<string, SourceColumn>;
  filters: SourceFilter[];
  metric_type: MetricType | null;
  conversion_window_hours: number | null;
  cap_value: number | null;
  /**
   * True when a decrease is the improvement for this metric (an error rate,
   * churn). Null for an assignment source.
   */
  lower_is_better: boolean | null;
  validated_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AssignmentSourceBody {
  kind: 'assignment';
  connection_id?: string;
  name: string;
  table: string;
  columns: { unit_id: string; experiment_key: string; variant: string; exposed_at: string };
  filters: SourceFilter[];
}

export interface MetricSourceBody {
  kind: 'metric';
  connection_id?: string;
  name: string;
  table: string;
  columns: { unit_id: string; event_at: string; value?: string };
  metric_type: MetricType;
  conversion_window_hours: number;
  cap_value?: number | null;
  /**
   * The body replaces the whole definition, so leaving this out sets false.
   * The form sends it only when ticked: an API older than the field refuses
   * it, and then only a ticked save fails.
   */
  lower_is_better?: boolean;
  filters: SourceFilter[];
}

export type SourceBody = AssignmentSourceBody | MetricSourceBody;

export interface ValidateResult {
  source: Source;
  columns: SourceColumn[];
}

export interface PreviewRequest {
  window_start?: string;
  window_end?: string;
  experiment_key?: string;
}

export interface PreviewResult {
  kind: SourceKind;
  window_start: string;
  window_end: string;
  total_rows: number;
  null_unit_rows: number;
  null_variant_rows: number | null;
  variants: { label: string; units: number }[] | null;
  null_value_rows: number | null;
  earliest: string | null;
  latest: string | null;
  run_id: string;
}

export const warehouseService = {
  listConnectors: () =>
    apiFetch<{ connectors: Connector[] }>(`/api/v1/warehouse/analysis/connectors`).then((r) => r.connectors),

  listConnections: () =>
    apiFetch<{ connections: Connection[] }>(`/api/v1/warehouse/analysis/connections`).then((r) => r.connections),

  getConnection: (id: string) =>
    apiFetch<Connection>(`/api/v1/warehouse/analysis/connections/${encodeURIComponent(id)}`),

  createConnection: (body: ConnectionBody) =>
    apiFetch<ConnectionCreated>(`/api/v1/warehouse/analysis/connections`, { method: 'POST', json: body }),

  updateConnection: (id: string, body: ConnectionBody) =>
    apiFetch<Connection>(`/api/v1/warehouse/analysis/connections/${encodeURIComponent(id)}`, {
      method: 'PUT',
      json: body,
    }),

  deleteConnection: (id: string) =>
    apiFetch<void>(`/api/v1/warehouse/analysis/connections/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  /** Test before saving (BigQuery, Athena). The body is held in memory by the API, not stored. */
  testUnsavedConnection: (body: BigQueryBody | AthenaBody) =>
    apiFetch<ConnectionTestResult>(`/api/v1/warehouse/analysis/connections/test`, { method: 'POST', json: body }),

  testConnection: (id: string) =>
    apiFetch<ConnectionTestResult>(`/api/v1/warehouse/analysis/connections/${encodeURIComponent(id)}/test`, {
      method: 'POST',
    }),

  regenerateKey: (id: string) =>
    apiFetch<ConnectionCreated>(
      `/api/v1/warehouse/analysis/connections/${encodeURIComponent(id)}/regenerate-key`,
      { method: 'POST' },
    ),

  listSources: () =>
    apiFetch<{ sources: Source[] }>(`/api/v1/warehouse/analysis/sources`).then((r) => r.sources),

  getSource: (id: string) => apiFetch<Source>(`/api/v1/warehouse/analysis/sources/${encodeURIComponent(id)}`),

  createSource: (body: SourceBody) =>
    apiFetch<Source>(`/api/v1/warehouse/analysis/sources`, { method: 'POST', json: body }),

  updateSource: (id: string, body: SourceBody) =>
    apiFetch<Source>(`/api/v1/warehouse/analysis/sources/${encodeURIComponent(id)}`, { method: 'PUT', json: body }),

  deleteSource: (id: string) =>
    apiFetch<void>(`/api/v1/warehouse/analysis/sources/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  validateSource: (id: string) =>
    apiFetch<ValidateResult>(`/api/v1/warehouse/analysis/sources/${encodeURIComponent(id)}/validate`, {
      method: 'POST',
    }),

  previewSource: (id: string, body: PreviewRequest = {}) =>
    apiFetch<PreviewResult>(`/api/v1/warehouse/analysis/sources/${encodeURIComponent(id)}/preview`, {
      method: 'POST',
      json: body,
    }),
};
