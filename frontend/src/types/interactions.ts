/**
 * The interaction routes' answers (`backend/app/schemas/interaction.py`).
 *
 * `GET /api/v1/interactions/scan` (stable) measures overlap only: each pair's
 * interaction, novelty and SUTVA sub-results are null, and its risk level is
 * the overlap's band. `GET /api/v1/interactions/{a}/{b}` (beta, #219) reports
 * the overlap of any two experiments and tests each treatment's lift across
 * the other experiment's variants.
 */
import { AnalysisStatusFields } from '@/types/analysis';

/** `RiskLevel`: the scan's overlap band (at or below 0.3, up to 0.6, above 0.6). */
export type InteractionRiskLevel = 'low' | 'medium' | 'high';

/** `InteractionAnalysisResponse`: one pair of active experiments in the scan. */
export interface InteractionScanPair {
  experiment_a_id: string;
  experiment_b_id: string;
  overlap_coefficient: number;
  has_significant_overlap: boolean;
  /** Always null from the scan: it does not test for an interaction. */
  interaction_result?: {
    has_interaction: boolean;
    p_value: number;
    interaction_effect_size: number;
    warning_message?: string | null;
  } | null;
  novelty_result?: unknown | null;
  sutva_result?: unknown | null;
  overall_risk: InteractionRiskLevel;
  /** Not shown: the page's own copy says what to do. */
  recommendations?: string[];
}

/** `ActiveInteractionScanResponse`. */
export interface InteractionScanResponse {
  total_active_experiments: number;
  pairs_analyzed: number;
  high_risk_pairs: number;
  analyses: InteractionScanPair[];
}

/** `InteractionArm`: one variant of the other experiment, among the users in both. */
export interface InteractionArm {
  other_variant_id: string;
  other_variant_name: string;
  n_control: number;
  converted_control: number;
  n_treatment: number;
  converted_treatment: number;
  control_rate?: number | null;
  treatment_rate?: number | null;
  /** treatment_rate - control_rate, as a rate: 0.05 is 5 percentage points. */
  effect?: number | null;
  relative_lift?: number | null;
}

/** Why a row was not tested; the API may add codes, which read as "Not tested.". */
export type InteractionUnavailableReason =
  | 'mutual_exclusion_group'
  | 'no_shared_users'
  | 'no_metric'
  | 'not_a_proportion_metric'
  | 'no_control_variant'
  | 'too_few_shared_users'
  | 'too_few_conversions';

/**
 * `InteractionRow`: one treatment of one experiment tested against the other's
 * variants. Tested exactly when `unavailable_reason` is null; a null
 * `is_significant` means not tested, never "no interaction".
 */
export interface InteractionRow {
  experiment_id: string;
  other_experiment_id: string;
  metric_id?: string | null;
  metric_name?: string | null;
  variant_id?: string | null;
  variant_name?: string | null;
  control_variant_id?: string | null;
  arms?: InteractionArm[];
  statistic?: number | null;
  degrees_of_freedom?: number | null;
  p_value?: number | null;
  corrected_p_value?: number | null;
  correction_method: string;
  confidence_level: number;
  is_significant?: boolean | null;
  unavailable_reason?: string | null;
}

/** `InteractionPairResponse` (beta). */
export interface InteractionPairResponse extends AnalysisStatusFields {
  experiment_a_id: string;
  experiment_b_id: string;
  shared_users: number;
  share_of_a: number;
  share_of_b: number;
  overlap_coefficient: number;
  has_significant_overlap: boolean;
  mutual_exclusion_group_id?: string | null;
  min_users_per_cell: number;
  min_expected_per_cell: number;
  interaction_results?: InteractionRow[];
  /** True when any row is significant; false only when every row was tested and none is; null otherwise. */
  has_interaction?: boolean | null;
  /** Not shown: the page's own copy says what to do. */
  recommendations?: string[];
}
