/**
 * What the experiment page reads about who may enter an experiment, besides
 * its targeting (#1449): its exclusion group and the global holdout. Each is a
 * subset of the API's response, holding only the fields the page uses.
 */

/** `MutualExclusionGroupResponse`, from `GET /api/v1/mutual-exclusion-groups/{id}`. */
export interface MutualExclusionGroup {
  id: string;
  name: string;
  /** `active` or `archived`. An archived group keeps its experiments but no longer constrains them. */
  status: string;
}

/** An experiment as a group lists it (`ExperimentSummary`). */
export interface ExclusionGroupMember {
  id: string;
  name: string;
  /** The experiment's status as stored: `draft`, `active`, `paused`, `completed` or `archived`. */
  status: string;
}

/**
 * `MutualExclusionGroupResponse` in full, as the exclusion-group pages read it
 * (#1455). `traffic_allocation` is a FRACTION, 0.0 to 1.0: the pages show it as
 * a whole percent and send it back as a fraction.
 */
export interface ExclusionGroupDetail {
  id: string;
  name: string;
  description?: string | null;
  traffic_allocation: number;
  /** `active` or `archived`. */
  status: string;
  owner_id?: string | null;
  experiments: ExclusionGroupMember[];
  created_at?: string;
  updated_at?: string;
}

/** `MutualExclusionGroupListResponse`, from `GET /api/v1/mutual-exclusion-groups`. */
export interface ExclusionGroupList {
  items: ExclusionGroupDetail[];
  total: number;
}

/** `MutualExclusionGroupCreate`. `traffic_allocation` is a fraction, 0.0 to 1.0. */
export interface ExclusionGroupCreate {
  name: string;
  description?: string;
  traffic_allocation: number;
}

/**
 * The fields of `MutualExclusionGroupUpdate` the group page changes: only the
 * ones that changed are sent. A status change is `ExclusionGroupsService.archive`
 * or `.unarchive`, never part of this body.
 */
export interface ExclusionGroupChanges {
  name?: string;
  description?: string;
  traffic_allocation?: number;
}

/**
 * `GlobalHoldoutResponse` in full, as the global-holdout page reads it from
 * `GET /api/v1/holdout/all` (#1455). `holdout_percentage` is a WHOLE percent,
 * 1 to 20, unlike a group's fractional share.
 */
export interface HoldoutDetail {
  id: string;
  name: string;
  description?: string | null;
  holdout_percentage: number;
  is_active: boolean;
  owner_id?: string | null;
  /** When it was activated (UTC, zone-less); null if never, or active since before membership was recorded. */
  activated_at?: string | null;
  /** When it ended (UTC, zone-less); an ended holdout cannot restart. */
  deactivated_at?: string | null;
  created_at?: string;
  updated_at?: string;
}

/** `GlobalHoldoutListResponse`, from `GET /api/v1/holdout/all` (ADMIN). */
export interface HoldoutList {
  items: HoldoutDetail[];
  total: number;
}

/** `GlobalHoldoutCreate`. */
export interface HoldoutCreate {
  name: string;
  description?: string;
  holdout_percentage: number;
  is_active: boolean;
}

/** The fields of `GlobalHoldoutUpdate` the page changes: only those that changed are sent. */
export interface HoldoutChanges {
  name?: string;
  description?: string;
  holdout_percentage?: number;
  is_active?: boolean;
}

export type HoldoutUnavailableReason = 'activated_before_measurement' | 'not_activated' | 'too_few_users' | 'no_events';

/** One arm of `HoldoutResultsResponse`. */
export interface HoldoutResultsGroup {
  group: 'in_holdout' | 'not_in_holdout';
  users: number;
  conversions: number;
  /** 0 to 1; null when `users` is 0. */
  conversion_rate: number | null;
  excluded_users?: number;
}

/** "Not in holdout" minus "in holdout", as fractions of 1. */
export interface HoldoutResultsDifference {
  absolute: number;
  relative_pct?: number | null;
  ci_lower: number;
  ci_upper: number;
  p_value: number | null;
  is_significant: boolean;
}

/** `HoldoutResultsResponse`, from `GET /api/v1/holdout/{holdout_id}/results` (beta). */
export interface HoldoutResults {
  holdout_id: string;
  metric: string;
  unavailable_reason: HoldoutUnavailableReason | null;
  /** The server's sentence for `unavailable_reason`: never shown, the page has its own. */
  message?: string | null;
  groups: HoldoutResultsGroup[];
  difference: HoldoutResultsDifference | null;
  analysis_status?: string;
}

/** `GlobalHoldoutResponse`, from `GET /api/v1/holdout` (null when none is active). */
export interface GlobalHoldout {
  id: string;
  name: string;
  /** Whole percent of new users kept out of every experiment. */
  holdout_percentage: number;
  is_active: boolean;
  activated_at?: string | null;
}
