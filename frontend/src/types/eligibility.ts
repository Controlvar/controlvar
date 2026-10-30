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

/** `GlobalHoldoutResponse`, from `GET /api/v1/holdout` (null when none is active). */
export interface GlobalHoldout {
  id: string;
  name: string;
  /** Whole percent of new users kept out of every experiment. */
  holdout_percentage: number;
  is_active: boolean;
  activated_at?: string | null;
}
