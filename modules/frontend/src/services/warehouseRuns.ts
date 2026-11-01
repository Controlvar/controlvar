/**
 * Warehouse analysis runs (beta): start an analysis of an experiment in the
 * customer's warehouse, follow it, and read its results.
 *
 * The routes are `/api/v1/warehouse/analysis/*` (the `warehouse` module). Who
 * may call what is the API's role matrix
 * (modules/backend/tests/integration/warehouse/test_roles.py):
 *
 *   start a run                               ADMIN, DEVELOPER
 *   list connections and sources              ADMIN, DEVELOPER, ANALYST
 *   list an experiment's runs, read one run   ADMIN, DEVELOPER, ANALYST (D50)
 *
 * A superuser counts as ADMIN. Everything here is pure except the service
 * object at the bottom.
 */
import { apiFetch, ApiError, isApiError, Role } from '@/services/api';
import { parseApiTimestamp } from '@/utils/apiTimestamp';

// ---------------------------------------------------------------------------
// Shapes (modules/backend/app/schemas/responses_warehouse.py)
// ---------------------------------------------------------------------------

export type RunStatus = 'queued' | 'running' | 'succeeded' | 'failed';

export interface RunStatement {
  kind: string;
  dialect?: string;
  sha256?: string;
  sql: string;
}

export interface RunJobMetadata {
  statement: string;
  job_id?: string;
  statement_handle?: string;
  location?: string;
  warehouse?: string;
  total_bytes_processed?: number;
  total_bytes_billed?: number;
  elapsed_ms?: number;
}

export interface VariantResult {
  variant_id: string;
  variant_name: string;
  is_control: boolean;
  sample_size: number;
  conversions: number | null;
  mean: number | null;
  std_dev?: number | null;
  confidence_interval?: [number, number] | number[] | null;
  p_value: number | null;
  adjusted_p_value?: number | null;
  is_significant: boolean;
  effect_size?: number | null;
  effect_size_label?: string | null;
  relative_improvement_pct: number | null;
  power?: number | null;
  statistical_test_used?: string | null;
  /** Why this variant's comparison was not computed, e.g. "Not computed: no variation". */
  note?: string | null;
}

export interface MetricResult {
  metric_id: string;
  metric_name: string;
  /** `conversion` for a proportion metric, `mean` for a mean metric. */
  metric_type: string;
  is_primary: boolean;
  /**
   * The direction this run analysed the metric in: its source's setting when
   * the run started. Absent from a result stored before the field.
   */
  lower_is_better?: boolean;
  variants: VariantResult[];
  has_significant_result: boolean;
  winning_variant_id: string | null;
}

export interface RunMetric {
  metric_source_id: string;
  name: string;
  /** The metric source's type: `proportion` or `mean`. */
  metric_type: string;
  is_primary: boolean;
  computed: boolean;
  not_computed_reason: string | null;
  message: string | null;
  result: MetricResult | null;
}

export interface RunSrm {
  chi2: number;
  p_value: number;
  warning: boolean;
  expected: Record<string, number>;
  observed: Record<string, number>;
}

export interface RunDiagnostics {
  exposure_rows: number;
  null_key_rows: number;
  units: number;
  multi_variant_units: number;
  variant_values: number;
  session_offset?: string | null;
}

export interface RunVariantSummary {
  variant_id: string;
  variant_name: string;
  is_control: boolean;
  labels: string[];
  units: number;
}

export interface RunResults {
  schema?: string;
  diagnostics: RunDiagnostics | null;
  variants: RunVariantSummary[];
  unmapped_labels: { label: string; units: number }[];
  srm: RunSrm | null;
  srm_skipped: string | null;
  metrics: RunMetric[];
}

export interface WarehouseRun {
  id: string;
  kind: string;
  status: RunStatus;
  experiment_id: string | null;
  connection_id: string | null;
  connection_name: string;
  warehouse_type: string;
  request: Record<string, unknown>;
  window_start: string | null;
  window_end: string | null;
  error_code: string | null;
  error_message: string | null;
  statements: RunStatement[] | null;
  /** Present only when the run succeeded. */
  results: RunResults | null;
  job_metadata: RunJobMetadata[] | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface WarehouseConnectionSummary {
  id: string;
  name: string;
  warehouse_type: string;
  credentials_status: string;
  enabled: boolean;
  max_runs_per_day: number;
}

export interface WarehouseSourceSummary {
  id: string;
  connection_id: string;
  kind: 'assignment' | 'metric';
  name: string;
  table: string;
  metric_type: string | null;
  /** Null for an assignment source; absent from an API older than the field. */
  lower_is_better?: boolean | null;
  validated_at: string | null;
}

export interface StartRunRequest {
  connection_id: string;
  assignment_source_id: string;
  /** The first is the primary metric. 1 to 10. */
  metric_source_ids: string[];
  window_start?: string;
  window_end?: string;
  confidence_level?: number;
  correction_method?: 'none' | 'bonferroni' | 'benjamini_hochberg';
}

// ---------------------------------------------------------------------------
// Roles
// ---------------------------------------------------------------------------

export interface RoleHolder {
  role?: Role | string | null;
  is_superuser?: boolean;
}

/** The role the API sees: a superuser counts as ADMIN. */
export function effectiveRole(user: RoleHolder | null | undefined): string | null {
  if (!user) return null;
  if (user.is_superuser) return 'ADMIN';
  return user.role ? String(user.role).toUpperCase() : null;
}

/** ADMIN and DEVELOPER start runs. */
export function canStartRun(user: RoleHolder | null | undefined): boolean {
  const role = effectiveRole(user);
  return role === 'ADMIN' || role === 'DEVELOPER';
}

// ---------------------------------------------------------------------------
// Words
// ---------------------------------------------------------------------------

export const WAREHOUSE_NAMES: Record<string, string> = {
  bigquery: 'BigQuery',
  snowflake: 'Snowflake',
  athena: 'Amazon Athena',
};

export function warehouseName(type: string | null | undefined): string {
  return (type && WAREHOUSE_NAMES[type]) || 'The warehouse';
}

export const STATUS_TEXT: Record<RunStatus, string> = {
  queued: 'Queued',
  running: 'Running',
  succeeded: 'Succeeded',
  failed: 'Failed',
};

export const BETA_EXPLANATION =
  'Warehouse analysis is in beta: its settings and API may change. The statistics are ' +
  'computed by the same engine as every other result.';

/**
 * How these numbers differ from the Results page, stated once where the
 * numbers are shown.
 */
export const RESULTS_DIFFERENCE =
  'Warehouse results count a unit as converted only for an event at or after its first ' +
  'exposure and inside the conversion window, and leave out units seen in more than one ' +
  'variant. The Results page counts every distinct converter, so the two can differ.';

export interface FailureCopy {
  title: string;
  fix?: string;
}

interface CopyContext {
  warehouse: string;
  connection: string;
  experimentKey: string;
}

/**
 * What the dashboard says about a failed run or a metric that was not
 * computed, by the API's fixed code. A code not listed here shows the API's
 * own message for it.
 */
const FAILURE_COPY: Record<string, (c: CopyContext) => FailureCopy> = {
  credentials_unavailable: () => ({
    title: 'Stored warehouse credentials can’t be used on this deployment.',
    fix: 'An operator must set WAREHOUSE_CREDENTIALS_KEYS (see Self-hosting › Warehouse).',
  }),
  auth_failed: (c) => ({
    title: `${c.warehouse} did not accept the credentials of ${c.connection}.`,
    fix: `An admin can replace them in Warehouse › Connections › ${c.connection}.`,
  }),
  key_revoked: (c) => ({
    title: `${c.warehouse} no longer accepts the key of ${c.connection}. It was probably rotated or removed.`,
    fix: `An admin can replace the key in Warehouse › Connections › ${c.connection}.`,
  }),
  permission_denied: (c) => ({
    title: `The role ${c.connection} signs in with can’t run this query.`,
    fix: 'Grant it SELECT on the assignment and metric tables or views, then start the analysis again.',
  }),
  object_not_found: () => ({
    title: 'A table, view or column the analysis names was not found.',
    fix: 'Check each source’s table and column mapping, validate it again, then start the analysis again.',
  }),
  not_a_select: () => ({
    title: 'A statement was not a single SELECT, so it was not run.',
    fix: 'Nothing was run. Report this with the run ID.',
  }),
  bytes_limit: (c) => ({
    title: `Analysis not run: it needed more than the byte limit of ${c.connection}.`,
    fix:
      c.warehouse === 'Amazon Athena'
        ? 'You are billed for the data Athena scanned before it stopped. Use a date-partitioned ' +
          'table or a view that narrows it, shorten the analysis window, or ask an admin to raise the limit.'
        : 'Nothing was run or billed. Use a date-partitioned table or a view that narrows it, ' +
          'shorten the analysis window, or ask an admin to raise the limit.',
  }),
  time_limit: (c) => ({
    title: `Analysis stopped: ${c.warehouse} did not finish within the time limit of ${c.connection}.`,
    fix:
      'The warehouse may bill for the work done before it stopped. Shorten the analysis window, ' +
      'or ask an admin to raise the limit.',
  }),
  cancelled: () => ({
    title: 'The query was cancelled.',
    fix: 'Cancelled queries may still be billed. Start the analysis again when you are ready.',
  }),
  workgroup_unsafe: () => ({
    title: 'The Athena workgroup’s settings don’t meet the requirements.',
    fix: 'An admin can check the workgroup settings listed in Warehouse › Connections.',
  }),
  timezone_not_utc: (c) => ({
    title: `The ${c.warehouse} session was not in UTC, so the results were refused.`,
    fix: 'Report this with the run ID.',
  }),
  result_invalid: (c) => ({
    title: `${c.warehouse} returned a result in an unexpected shape, so no number is shown.`,
    fix: 'Report this with the run ID.',
  }),
  too_many_variant_values: () => ({
    title: 'The variant column has more distinct values than allowed (51).',
    fix: 'Filter the assignment source to this experiment’s rows, or map the labels to variants.',
  }),
  join_key_mismatch: () => ({
    title: 'No metric rows matched an exposed unit.',
    fix: 'Check that the metric source’s unit column holds the same IDs as the assignment source’s.',
  }),
  no_units: (c) => ({
    title: `No exposures for ${c.experimentKey} were found in the window.`,
    fix: `Is the experiment key in your table exactly ${c.experimentKey}?`,
  }),
  fewer_than_2_units: () => ({
    title: 'Fewer than 2 units in a variant, so its mean can’t be compared.',
    fix: 'Analyse a longer window, or check that each variant’s label is mapped to it.',
  }),
  abandoned: () => ({
    title: 'The run stopped reporting progress and was marked as failed.',
    fix: 'Start the analysis again. An operator can find the run in the API log by its ID.',
  }),
  warehouse_busy: () => ({
    title: 'Another warehouse analysis was using this deployment’s capacity.',
    fix: 'Start the analysis again shortly.',
  }),
  unrecognised_warehouse_error: (c) => ({
    title: `${c.warehouse} returned an error we don’t recognise.`,
    fix: 'See Warehouse › Troubleshooting in the documentation.',
  }),
  unreachable: (c) => ({
    title: `${c.warehouse} could not be reached.`,
    fix: 'Start the analysis again. If it keeps failing, an admin can test the connection.',
  }),
  destination_not_allowed: (c) => ({
    title: `The address of ${c.connection} is not one this deployment connects to.`,
    fix: `An admin can check the connection’s account or region in Warehouse › Connections › ${c.connection}.`,
  }),
  redirect_refused: (c) => ({
    title: `${c.warehouse} answered with a redirect, which is not followed.`,
    fix: `An admin can check the connection’s account or region in Warehouse › Connections › ${c.connection}.`,
  }),
  run_in_progress: () => ({
    title: 'Another analysis or preview on this connection was already running.',
    fix: 'Wait for it to finish, then start the analysis again.',
  }),
  internal: () => ({
    title: 'Something went wrong on our side while talking to the warehouse.',
    fix: 'Start the analysis again. If it keeps failing, an operator can find the run in the API log by its ID.',
  }),
};

/** Every code {@link failureCopy} has its own words for. */
export const KNOWN_FAILURE_CODES = Object.keys(FAILURE_COPY);

export function isKnownFailure(code: string | null | undefined): boolean {
  return !!code && Object.prototype.hasOwnProperty.call(FAILURE_COPY, code);
}

export function failureCopy(
  code: string | null | undefined,
  apiMessage: string | null | undefined,
  context: Partial<CopyContext> = {},
): FailureCopy {
  const c: CopyContext = {
    warehouse: context.warehouse || 'The warehouse',
    connection: context.connection || 'this connection',
    experimentKey: context.experimentKey || 'this experiment',
  };
  const build = code ? FAILURE_COPY[code] : undefined;
  if (build) return build(c);
  if (apiMessage) return { title: apiMessage };
  return { title: code ? `The analysis failed (code ${code}).` : 'The analysis failed.' };
}

// ---------------------------------------------------------------------------
// Refusals of "start an analysis"
// ---------------------------------------------------------------------------

export interface StartRefusal {
  code: string;
  message: string;
  /** 429 daily_run_limit_reached: when the count resets (ISO 8601, UTC). */
  resetsAt?: string;
  limit?: number;
}

function detailRecord(err: ApiError): Record<string, unknown> {
  const d = err.detail;
  return d && typeof d === 'object' && !Array.isArray(d) ? (d as Record<string, unknown>) : {};
}

/** Hours and minutes from `now` until `iso`, e.g. `5 h 12 min`. */
export function untilText(iso: string, now: Date = new Date()): string | null {
  const at = parseApiTimestamp(iso).getTime();
  if (Number.isNaN(at)) return null;
  const minutes = Math.max(0, Math.ceil((at - now.getTime()) / 60000));
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h ? `${h} h ${m} min` : `${m} min`;
}

/** `2026-09-28 00:00 UTC` */
export function utcText(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = parseApiTimestamp(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`
  );
}

/** The words for a refused `POST …/runs`. */
export function startRefusal(err: unknown, now: Date = new Date()): StartRefusal {
  if (!isApiError(err)) {
    return {
      code: 'unknown',
      message: err instanceof Error ? err.message : 'The analysis could not be started.',
    };
  }
  const detail = detailRecord(err);
  // A time-out is not "unreachable": the run may have started (#1040). Its
  // message is the dashboard's own time-out sentence, shown by `default`.
  const code =
    err.code ?? (err.timedOut ? 'timed_out' : err.isNetworkError ? 'unreachable' : `http_${err.status}`);
  const apiMessage = typeof detail.message === 'string' ? detail.message : err.message;

  switch (code) {
    case 'daily_run_limit_reached': {
      const resetsAt = typeof detail.resets_at === 'string' ? detail.resets_at : undefined;
      const limit = typeof detail.limit === 'number' ? detail.limit : undefined;
      const wait = resetsAt ? untilText(resetsAt, now) : null;
      const message =
        `Not run: this connection has reached its limit of ${limit ?? 'its'} analyses per day ` +
        `(UTC, previews included). The count resets at ${resetsAt ? utcText(resetsAt) : '00:00 UTC'}` +
        `${wait ? `, in ${wait}` : ''}. An admin can change the limit in Warehouse › Connections.`;
      return { code, message, resetsAt, limit };
    }
    case 'warehouse_busy':
      return {
        code,
        message:
          'Not started: another warehouse analysis is using this deployment’s capacity. ' +
          'Try again in about 30 seconds.',
      };
    case 'run_in_progress':
      return {
        code,
        message:
          'Not started: an analysis or preview on this connection is already queued or running. ' +
          'Wait for it to finish, then start again.',
      };
    case 'source_not_validated':
      return {
        code,
        message: `Not started: ${apiMessage} Validate it in Warehouse › Sources.`,
      };
    case 'experiment_has_no_window':
      return {
        code,
        message:
          'Not started: this experiment has no start date, so there is no default window. ' +
          'Give a window start and end.',
      };
    case 'role_required':
      return { code, message: apiMessage };
    default:
      return { code, message: apiMessage || 'The analysis could not be started.' };
  }
}

// ---------------------------------------------------------------------------
// Numbers
// ---------------------------------------------------------------------------

/** A mean as text: 2 decimals from 1 up, 3 significant digits below 1. */
export function meanText(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return Math.abs(value) >= 1
    ? value.toLocaleString('en-US', { maximumFractionDigits: 2 })
    : value.toLocaleString('en-US', { maximumSignificantDigits: 3 });
}

/** `12.34%` from a 0..1 rate. */
export function percent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${(value * 100).toFixed(digits)}%`;
}

export function pValueText(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  if (value < 0.0001) return '< 0.0001';
  return value.toFixed(4);
}

export function bytesText(bytes: number): string {
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`;
}

export function secondsText(ms: number): string {
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)} s`;
  const m = Math.floor(s / 60);
  return `${m} min ${Math.round(s - m * 60)} s`;
}

/**
 * What the run cost, in the warehouse's own terms (never a price): bytes
 * billed for BigQuery, bytes scanned for Athena, time on the warehouse for
 * Snowflake. `null` when the warehouse reported nothing.
 */
export function costText(run: Pick<WarehouseRun, 'warehouse_type' | 'job_metadata'>): string | null {
  const jobs = run.job_metadata ?? [];
  if (!jobs.length) return null;
  const sum = (key: keyof RunJobMetadata) =>
    jobs.reduce((total, j) => total + (typeof j[key] === 'number' ? (j[key] as number) : 0), 0);
  const has = (key: keyof RunJobMetadata) => jobs.some((j) => typeof j[key] === 'number');
  if (run.warehouse_type === 'bigquery' && has('total_bytes_billed')) {
    return `Billed ${bytesText(sum('total_bytes_billed'))}`;
  }
  if (run.warehouse_type === 'athena' && has('total_bytes_processed')) {
    return `Scanned ${bytesText(sum('total_bytes_processed'))}`;
  }
  if (run.warehouse_type === 'snowflake' && has('elapsed_ms')) {
    const where = jobs.find((j) => j.warehouse)?.warehouse;
    return `Ran ${secondsText(sum('elapsed_ms'))}${where ? ` on ${where}` : ''}`;
  }
  if (has('total_bytes_processed')) return `Processed ${bytesText(sum('total_bytes_processed'))}`;
  return null;
}

export function durationText(run: Pick<WarehouseRun, 'started_at' | 'finished_at'>): string | null {
  if (!run.started_at || !run.finished_at) return null;
  const ms = parseApiTimestamp(run.finished_at).getTime() - parseApiTimestamp(run.started_at).getTime();
  return Number.isFinite(ms) && ms >= 0 ? secondsText(ms) : null;
}

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

/** How often a queued or running run is polled. */
export const RUN_POLL_MS = 3000;

export const warehouseRunsService = {
  listRuns: (experimentId: string) =>
    apiFetch<{ runs: WarehouseRun[] }>(
      `/api/v1/warehouse/analysis/experiments/${encodeURIComponent(experimentId)}/runs`,
    ),

  getRun: (runId: string) =>
    apiFetch<WarehouseRun>(`/api/v1/warehouse/analysis/runs/${encodeURIComponent(runId)}`),

  startRun: (experimentId: string, body: StartRunRequest) =>
    apiFetch<{ run_id: string; status: RunStatus }>(
      `/api/v1/warehouse/analysis/experiments/${encodeURIComponent(experimentId)}/runs`,
      { method: 'POST', json: body },
    ),

  listConnections: () =>
    apiFetch<{ connections: WarehouseConnectionSummary[] }>('/api/v1/warehouse/analysis/connections'),

  listSources: () =>
    apiFetch<{ sources: WarehouseSourceSummary[] }>('/api/v1/warehouse/analysis/sources'),
};
