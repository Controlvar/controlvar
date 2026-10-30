import { apiFetch } from '@/services/api';
import { GlobalHoldout, MutualExclusionGroup } from '@/types/eligibility';

/**
 * The two reads behind the experiment page's "Exclusion group" and "Global
 * holdout" rows (#1449). The experiment page only reads: groups are created
 * and changed on the exclusion-group pages (`services/exclusionGroups.ts`,
 * #1455).
 *
 * Both routes check EXPERIMENT CREATE (`_require_developer`): ADMIN, DEVELOPER
 * and superusers get 200, ANALYST and VIEWER 403. The page asks only for a
 * user `canReadEligibility` admits, and neither call carries a query.
 */
export const EligibilityService = {
  /** `GET /api/v1/mutual-exclusion-groups/{id}`. */
  async group(groupId: string): Promise<MutualExclusionGroup> {
    return apiFetch<MutualExclusionGroup>(`/api/v1/mutual-exclusion-groups/${groupId}`);
  },

  /** `GET /api/v1/holdout`: the active global holdout, or null when none is active. */
  async activeHoldout(): Promise<GlobalHoldout | null> {
    return apiFetch<GlobalHoldout | null>('/api/v1/holdout');
  },
};
