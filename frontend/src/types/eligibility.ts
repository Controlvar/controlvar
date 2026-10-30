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

/** `GlobalHoldoutResponse`, from `GET /api/v1/holdout` (null when none is active). */
export interface GlobalHoldout {
  id: string;
  name: string;
  /** Whole percent of new users kept out of every experiment. */
  holdout_percentage: number;
  is_active: boolean;
  activated_at?: string | null;
}
