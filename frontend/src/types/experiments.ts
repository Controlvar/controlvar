/**
 * Experiment types. Field names mirror the backend response/request schemas in
 * `backend/app/schemas/experiment.py` (`ExperimentResponse`, `ExperimentCreate`,
 * `VariantResponse`, `MetricResponse`) — do not rename them client-side.
 */
import { CorrectionMethod } from './results';

export type ExperimentStatus = 'draft' | 'active' | 'paused' | 'completed' | 'archived';
export type ExperimentType = 'a_b' | 'mv' | 'split_url' | 'bandit';
export type MetricType = 'conversion' | 'revenue' | 'count' | 'duration' | 'custom';
export type OptimizationType = 'fixed' | 'thompson_sampling' | 'ucb1' | 'epsilon_greedy';

/** `VariantResponse` */
export interface ExperimentVariant {
  id: string;
  name: string;
  description?: string | null;
  is_control: boolean;
  /** Percentage of traffic (0–100). */
  traffic_allocation: number;
  configuration?: Record<string, unknown> | null;
  experiment_id?: string;
  created_at?: string;
  updated_at?: string;
}

/** `MetricResponse` */
export interface ExperimentMetric {
  id: string;
  name: string;
  description?: string | null;
  event_name: string;
  metric_type: MetricType | string;
  is_primary: boolean;
  aggregation_method?: string;
  minimum_sample_size?: number;
  expected_effect?: number | null;
  event_value_path?: string | null;
  lower_is_better?: boolean;
  experiment_id?: string;
  created_at?: string;
  updated_at?: string;
}

/** `ExperimentResponse` */
export interface Experiment {
  id: string;
  name: string;
  key: string | null;
  description?: string | null;
  hypothesis?: string | null;
  experiment_type: ExperimentType | string;
  status: ExperimentStatus;
  targeting_rules: Record<string, unknown> | null;
  tags?: string[] | null;
  /** Null once the creator's account has been removed. */
  owner_id: string | null;
  /**
   * The owner's full name, else their username when it contains no `@`, else
   * null; never their email (#921). Only `GET /api/v1/experiments/{id}`
   * carries it: the list and the other experiment routes leave it out.
   */
  owner_name?: string | null;
  start_date: string | null;
  end_date: string | null;
  created_at: string;
  updated_at: string;
  variants: ExperimentVariant[];
  metrics: ExperimentMetric[];
  sequential_testing_enabled?: boolean;
  sequential_testing_method?: string | null;
  sequential_testing_config?: Record<string, unknown> | null;
  mutual_exclusion_group_id?: string | null;
  variance_reduction_config?: Record<string, unknown> | null;
  optimization_type?: OptimizationType | string;
  experiment_metadata?: Record<string, unknown> | null;
  split_url_config?: Record<string, unknown> | null;
  /**
   * How the results are judged (#580). Set on create, changeable only while
   * the experiment is a draft: a PUT carrying either afterwards is refused.
   */
  confidence_level?: number;
  correction_method?: CorrectionMethod;
  /** Whether results also carry a Bayesian analysis of the primary metric (#216). */
  bayesian_enabled?: boolean;
}

/** `VariantBase` (create payload) */
export interface VariantInput {
  name: string;
  description?: string;
  is_control: boolean;
  traffic_allocation: number;
  configuration?: Record<string, unknown>;
}

/** `MetricBase` (create payload) */
export interface MetricInput {
  name: string;
  description?: string;
  event_name: string;
  metric_type: MetricType;
  is_primary: boolean;
  aggregation_method?: string;
  minimum_sample_size?: number;
  expected_effect?: number;
  event_value_path?: string;
  lower_is_better?: boolean;
}

/**
 * `VariantBase` as the dashboard's design edit sends it: all five fields,
 * every time. An Optional field with no value is sent as null.
 */
export interface DesignVariant {
  name: string;
  description: string | null;
  is_control: boolean;
  traffic_allocation: number;
  configuration: Record<string, unknown> | null;
}

/**
 * `MetricBase` as the dashboard's design edit sends it. The Optional fields
 * (`description`, `expected_effect`, `event_value_path`) are sent as null when
 * they have no value; `aggregation_method` and `minimum_sample_size` have
 * non-null defaults and are left out instead, because null is refused there.
 */
export interface DesignMetric {
  name: string;
  description: string | null;
  event_name: string;
  metric_type: MetricType;
  is_primary: boolean;
  aggregation_method?: string;
  minimum_sample_size?: number;
  expected_effect: number | null;
  event_value_path: string | null;
  lower_is_better: boolean;
}

/**
 * The body of a design edit (`PUT /api/v1/experiments/{id}`): exactly the two
 * lists, both complete. The update replaces both lists wholesale, so a
 * variant, a metric or a field left out would be deleted or reset.
 */
export interface ExperimentDesignUpdate {
  variants: DesignVariant[];
  metrics: DesignMetric[];
}

/** `ExperimentCreate` */
export interface CreateExperimentRequest {
  name: string;
  key?: string;
  description?: string;
  hypothesis?: string;
  experiment_type: ExperimentType;
  targeting_rules?: Record<string, unknown> | null;
  tags?: string[];
  status?: ExperimentStatus;
  variants: VariantInput[];
  metrics: MetricInput[];
  optimization_type?: OptimizationType;
  /** 0.80 to 0.99; the API's default is 0.95. */
  confidence_level?: number;
  /** The API's default is `benjamini_hochberg`. */
  correction_method?: CorrectionMethod;
  /** Sent only when true; the API then stores its default Bayesian settings. */
  bayesian_enabled?: boolean;
}

/** `ExperimentListResponse` (offset pagination: `skip`/`limit`). */
export interface ExperimentListResponse {
  items: Experiment[];
  total: number;
  skip: number;
  limit: number;
}

export const EXPERIMENT_STATUS_LABELS: Record<ExperimentStatus, string> = {
  draft: 'Draft',
  active: 'Active',
  paused: 'Paused',
  completed: 'Completed',
  archived: 'Archived',
};

/** The traffic algorithm of an adaptive experiment, in words; `fixed` is a plain split. */
export const OPTIMIZATION_TYPE_LABELS: Record<OptimizationType, string> = {
  fixed: 'Fixed split',
  thompson_sampling: 'Thompson sampling',
  ucb1: 'UCB1',
  epsilon_greedy: 'Epsilon-greedy',
};

export const EXPERIMENT_TYPE_LABELS: Record<ExperimentType, string> = {
  a_b: 'A/B Test',
  mv: 'Multivariate',
  split_url: 'Split URL',
  bandit: 'Bandit',
};

export const METRIC_TYPE_LABELS: Record<MetricType, string> = {
  conversion: 'Conversion',
  revenue: 'Revenue',
  count: 'Count',
  duration: 'Duration',
  custom: 'Custom',
};

export const EXPERIMENT_STATUS_COLORS: Record<ExperimentStatus, string> = {
  draft: 'bg-slate-100 text-slate-700',
  active: 'bg-green-100 text-green-800',
  paused: 'bg-yellow-100 text-yellow-800',
  completed: 'bg-blue-100 text-blue-800',
  archived: 'bg-gray-100 text-gray-600',
};

/** Human label for `experiment_type`; falls back to the raw value for unknown types. */
export function experimentTypeLabel(type: string | null | undefined): string {
  if (!type) return '—';
  return EXPERIMENT_TYPE_LABELS[type as ExperimentType] ?? type;
}

/** Human label for a status; unknown values are shown as-is. */
export function experimentStatusLabel(status: string | null | undefined): string {
  if (!status) return '—';
  return EXPERIMENT_STATUS_LABELS[status as ExperimentStatus] ?? status;
}

export function experimentStatusColor(status: string | null | undefined): string {
  return EXPERIMENT_STATUS_COLORS[status as ExperimentStatus] ?? 'bg-slate-100 text-slate-700';
}
