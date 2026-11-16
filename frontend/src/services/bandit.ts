import { apiFetch } from '@/services/api';
import { BanditHistoryTick, BanditStatus } from '@/types/bandit';

/**
 * Read-only access to a bandit experiment's weights. The dashboard never calls
 * the update or override routes.
 */
export const BanditService = {
  /** `GET /api/v1/bandit/{id}`: any signed-in role may read it. */
  async status(experimentId: string): Promise<BanditStatus> {
    return apiFetch<BanditStatus>(`/api/v1/bandit/${experimentId}`);
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
