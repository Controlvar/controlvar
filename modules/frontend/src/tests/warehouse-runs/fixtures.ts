/**
 * Response bodies in the shapes the W4 API answers with
 * (modules/backend/app/api/v1/endpoints/warehouse_analysis.py `run_out`,
 * `connection_out`, `source_out`; results from warehouse_runner._execute).
 */
import type {
  RunMetric,
  RunResults,
  WarehouseConnectionSummary,
  WarehouseRun,
  WarehouseSourceSummary,
} from '@modules/services/warehouseRuns';

export const EXPERIMENT = {
  id: '11111111-1111-4111-8111-111111111111',
  key: 'checkout-v2',
  start_date: '2026-09-01T00:00:00Z',
};

export const RUNS_PATH = `/api/v1/warehouse/analysis/experiments/${EXPERIMENT.id}/runs`;
export const runPath = (id: string) => `/api/v1/warehouse/analysis/runs/${id}`;
export const CONNECTIONS_PATH = '/api/v1/warehouse/analysis/connections';
export const SOURCES_PATH = '/api/v1/warehouse/analysis/sources';

export const CONTROL_ID = 'v-control';
export const TREATMENT_ID = 'v-blue';

export function connection(over: Partial<WarehouseConnectionSummary> = {}): WarehouseConnectionSummary {
  return {
    id: 'conn-1',
    name: 'Prod analytics',
    warehouse_type: 'bigquery',
    credentials_status: 'ok',
    enabled: true,
    max_runs_per_day: 20,
    ...over,
  };
}

export function source(over: Partial<WarehouseSourceSummary> = {}): WarehouseSourceSummary {
  return {
    id: 'src-a',
    connection_id: 'conn-1',
    kind: 'assignment',
    name: 'Exposures',
    table: 'analytics.exposures',
    metric_type: null,
    lower_is_better: over.kind === 'metric' ? false : null,
    validated_at: '2026-09-20T10:00:00Z',
    ...over,
  };
}

export const SOURCES: WarehouseSourceSummary[] = [
  source(),
  source({ id: 'src-p', kind: 'metric', name: 'Purchased', table: 'analytics.orders', metric_type: 'proportion' }),
  source({ id: 'src-s', kind: 'metric', name: 'Signed up', table: 'analytics.signups', metric_type: 'proportion' }),
  source({ id: 'src-m', kind: 'metric', name: 'Revenue per user', table: 'analytics.orders', metric_type: 'mean' }),
];

export function computedMetric(over: Partial<RunMetric> = {}): RunMetric {
  return {
    metric_source_id: 'src-p',
    name: 'Purchased',
    metric_type: 'proportion',
    is_primary: true,
    computed: true,
    not_computed_reason: null,
    message: null,
    result: {
      metric_id: 'src-p',
      metric_name: 'Purchased',
      metric_type: 'conversion',
      is_primary: true,
      lower_is_better: false,
      has_significant_result: true,
      winning_variant_id: TREATMENT_ID,
      variants: [
        {
          variant_id: CONTROL_ID,
          variant_name: 'control',
          is_control: true,
          sample_size: 10000,
          conversions: 1000,
          mean: 0.1,
          p_value: null,
          is_significant: false,
          relative_improvement_pct: null,
        },
        {
          variant_id: TREATMENT_ID,
          variant_name: 'blue',
          is_control: false,
          sample_size: 10050,
          conversions: 1206,
          mean: 0.12,
          p_value: 0.00001,
          adjusted_p_value: null,
          is_significant: true,
          relative_improvement_pct: 20,
          statistical_test_used: 'fisher_exact',
        },
      ],
    },
    ...over,
  };
}

/** A mean metric as the runner stores it (core `mean_metric_result`). */
export function meanMetric(over: Partial<RunMetric> = {}): RunMetric {
  return {
    metric_source_id: 'src-m',
    name: 'Revenue per user',
    metric_type: 'mean',
    is_primary: false,
    computed: true,
    not_computed_reason: null,
    message: null,
    result: {
      metric_id: 'src-m',
      metric_name: 'Revenue per user',
      metric_type: 'mean',
      is_primary: false,
      lower_is_better: false,
      has_significant_result: false,
      winning_variant_id: null,
      variants: [
        {
          variant_id: CONTROL_ID,
          variant_name: 'control',
          is_control: true,
          sample_size: 10000,
          conversions: null,
          mean: 12.3456,
          std_dev: 4.5,
          confidence_interval: [12.2574, 12.4338],
          p_value: null,
          is_significant: false,
          relative_improvement_pct: null,
          note: null,
        },
        {
          variant_id: TREATMENT_ID,
          variant_name: 'blue',
          is_control: false,
          sample_size: 10050,
          conversions: null,
          mean: 12.5,
          std_dev: 4.6,
          confidence_interval: [12.41, 12.59],
          p_value: 0.012,
          adjusted_p_value: null,
          is_significant: true,
          relative_improvement_pct: 1.25,
          statistical_test_used: 'welch_t_test',
          note: null,
        },
      ],
    },
    ...over,
  };
}

export function notComputedMetric(code: string, over: Partial<RunMetric> = {}): RunMetric {
  return {
    metric_source_id: 'src-s',
    name: 'Signed up',
    metric_type: 'proportion',
    is_primary: false,
    computed: false,
    not_computed_reason: code,
    message: `Not computed: API words for ${code}.`,
    result: null,
    ...over,
  };
}

export function results(over: Partial<RunResults> = {}): RunResults {
  return {
    schema: 'experimently.warehouse.results/v1',
    diagnostics: {
      exposure_rows: 20500,
      null_key_rows: 0,
      units: 20050,
      multi_variant_units: 0,
      variant_values: 2,
      session_offset: null,
    },
    variants: [
      { variant_id: CONTROL_ID, variant_name: 'control', is_control: true, labels: ['control'], units: 10000 },
      { variant_id: TREATMENT_ID, variant_name: 'blue', is_control: false, labels: ['blue'], units: 10050 },
    ],
    unmapped_labels: [],
    srm: {
      chi2: 0.12,
      p_value: 0.72,
      warning: false,
      expected: { [CONTROL_ID]: 10025, [TREATMENT_ID]: 10025 },
      observed: { [CONTROL_ID]: 10000, [TREATMENT_ID]: 10050 },
    },
    srm_skipped: null,
    metrics: [computedMetric()],
    ...over,
  };
}

export const SQL = 'SELECT variant, COUNT(*) AS n FROM `analytics.exposures` GROUP BY 1';

export function run(over: Partial<WarehouseRun> = {}): WarehouseRun {
  const status = over.status ?? 'succeeded';
  return {
    id: 'run-1',
    kind: 'analysis',
    status,
    experiment_id: EXPERIMENT.id,
    connection_id: 'conn-1',
    connection_name: 'Prod analytics',
    warehouse_type: 'bigquery',
    request: {},
    window_start: '2026-09-01T00:00:00Z',
    window_end: '2026-09-28T00:00:00Z',
    error_code: null,
    error_message: null,
    statements: [
      { kind: 'diagnostics', dialect: 'bigquery', sha256: 'a'.repeat(64), sql: SQL },
      { kind: 'metric', dialect: 'bigquery', sha256: 'b'.repeat(64), sql: 'SELECT 2' },
    ],
    results: status === 'succeeded' ? results() : null,
    job_metadata:
      status === 'succeeded'
        ? [
            { statement: 'diagnostics', job_id: 'job-1', total_bytes_processed: 1_000_000, total_bytes_billed: 10_485_760 },
            { statement: 'metric', job_id: 'job-2', total_bytes_processed: 2_000_000, total_bytes_billed: 10_485_760 },
          ]
        : null,
    created_at: '2026-09-28T09:00:00Z',
    started_at: status === 'queued' ? null : '2026-09-28T09:00:01Z',
    finished_at: status === 'succeeded' || status === 'failed' ? '2026-09-28T09:00:49Z' : null,
    ...over,
  };
}
