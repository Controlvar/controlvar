import { apiFetch } from '@/services/api';
import { BanditHistoryTick, BanditStatus } from '@/types/bandit';

/**
 * A bandit experiment's weights: read by any signed-in role, and set by hand
 * by a superuser or the ADMIN role ("Set weights by hand", offered only to
 * them by `canOverrideBanditWeights`). Their past updates are read too. The
 * dashboard never calls the update route (`POST /api/v1/bandit/{id}/update`).
 */
export const BanditService = {
  /** `GET /api/v1/bandit/{id}`: any signed-in role may read it. */
  async status(experimentId: string): Promise<BanditStatus> {
    return apiFetch<BanditStatus>(`/api/v1/bandit/${experimentId}`);
  },

  /**
   * `PUT /api/v1/bandit/{id}/weights` with `{"weights": {variant_id: fraction}}`:
   * each fraction 0 or more, together 1 (the API allows 1e-6 either way). It
   * answers the new status, whose pulls and successes are 0 until the
   * bandit's next update counts them again.
   */
  async override(experimentId: string, weights: Record<string, number>): Promise<BanditStatus> {
    return apiFetch<BanditStatus>(`/api/v1/bandit/${experimentId}/weights`, {
      method: 'PUT',
      json: { weights },
    });
  },

  /**
   * `GET /api/v1/bandit/{id}/history` (beta): the bandit's past weight updates,
   * newest first. Sent with no query, so the server's default (the 200 newest)
   * applies; any role that may view the experiment may read it.
   */
  async history(experimentId: string): Promise<BanditHistoryTick[]> {
    return apiFetch<BanditHistoryTick[]>(`/api/v1/bandit/${experimentId}/history`);
  },
};
