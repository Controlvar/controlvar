import { apiFetch } from '@/services/api';
import { InteractionPairResponse, InteractionScanResponse } from '@/types/interactions';

/**
 * The interactions page's calls (#1960), under `/api/v1/interactions`.
 *
 * Both admit a superuser or a role in `INTERACTION_ROLES`
 * (`interactions._require_developer`, mirrored by `canAnalyseInteractions`);
 * any other role, and an account with no role, gets 403. Neither reads a query
 * parameter. Both are GETs, which demo mode never refuses. Each path is written
 * out whole, so url-literals.test.ts checks it against the API's routes.
 */
export const InteractionsService = {
  /** `GET /api/v1/interactions/scan` (stable): the pairs of active experiments that share many users. */
  async scan(): Promise<InteractionScanResponse> {
    return apiFetch<InteractionScanResponse>('/api/v1/interactions/scan');
  },

  /**
   * `GET /api/v1/interactions/{exp_a_id}/{exp_b_id}` (beta): the overlap of any
   * two experiments and the interaction test. The same id twice answers 422,
   * an unknown one 404.
   */
  async pair(experimentAId: string, experimentBId: string): Promise<InteractionPairResponse> {
    return apiFetch<InteractionPairResponse>(`/api/v1/interactions/${experimentAId}/${experimentBId}`);
  },
};
