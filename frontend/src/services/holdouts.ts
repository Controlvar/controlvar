import { apiFetch } from '@/services/api';
import { HoldoutChanges, HoldoutCreate, HoldoutDetail, HoldoutList, HoldoutResults } from '@/types/eligibility';

/**
 * The global-holdout page's calls (#1455), `/api/v1/holdout`.
 *
 * `/all`, `POST` and `PUT` are `_require_admin`: an ADMIN or a superuser. The
 * active holdout (`GET /api/v1/holdout`, `EligibilityService.activeHoldout`)
 * needs DEVELOPER or above; the results (beta) any role that reads experiment
 * results. A name another holdout has, or an activation that races another,
 * answers 409; a new percentage on a holdout that has been active, or
 * activating one that has ended, answers 422.
 */
const BASE = '/api/v1/holdout';

export const HoldoutsService = {
  /** `GET /api/v1/holdout/all`: every holdout, active and ended (ADMIN). */
  async listAll(): Promise<HoldoutList> {
    return apiFetch<HoldoutList>(`${BASE}/all`);
  },

  /** `POST /api/v1/holdout`. With `is_active: true` it ends the active one. */
  async create(body: HoldoutCreate): Promise<HoldoutDetail> {
    return apiFetch<HoldoutDetail>(BASE, { method: 'POST', json: body });
  },

  /** `PUT /api/v1/holdout/{holdout_id}` with the changed fields only; `is_active` activates or ends it. */
  async update(holdoutId: string, changes: HoldoutChanges): Promise<HoldoutDetail> {
    return apiFetch<HoldoutDetail>(`${BASE}/${holdoutId}`, { method: 'PUT', json: changes });
  },

  /** `GET /api/v1/holdout/{holdout_id}/results?metric=` (beta): one event name, matched exactly. */
  async results(holdoutId: string, metric: string): Promise<HoldoutResults> {
    return apiFetch<HoldoutResults>(`${BASE}/${holdoutId}/results`, { query: { metric } });
  },
};
