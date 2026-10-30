import { apiFetch } from '@/services/api';
import {
  ExclusionGroupChanges,
  ExclusionGroupCreate,
  ExclusionGroupDetail,
  ExclusionGroupList,
} from '@/types/eligibility';

/**
 * The exclusion-group pages' calls (#1455), `/api/v1/mutual-exclusion-groups`.
 *
 * Every route checks EXPERIMENT CREATE first (`_require_developer`): ADMIN,
 * DEVELOPER and superusers. Archiving (`DELETE`) and any status change
 * through `PUT` need the ADMIN role or a superuser (`_require_admin`). A name
 * another group has answers 409, compared exactly, case included.
 *
 * No call carries a query: the list reads the API's first page (100 groups).
 */
const BASE = '/api/v1/mutual-exclusion-groups';

export const ExclusionGroupsService = {
  /** `GET /api/v1/mutual-exclusion-groups`. */
  async list(): Promise<ExclusionGroupList> {
    return apiFetch<ExclusionGroupList>(BASE);
  },

  /** `GET /api/v1/mutual-exclusion-groups/{group_id}`, with its experiments. */
  async get(groupId: string): Promise<ExclusionGroupDetail> {
    return apiFetch<ExclusionGroupDetail>(`${BASE}/${groupId}`);
  },

  /** `POST /api/v1/mutual-exclusion-groups`. */
  async create(body: ExclusionGroupCreate): Promise<ExclusionGroupDetail> {
    return apiFetch<ExclusionGroupDetail>(BASE, { method: 'POST', json: body });
  },

  /** `PUT /api/v1/mutual-exclusion-groups/{group_id}` with the changed fields only. */
  async update(groupId: string, changes: ExclusionGroupChanges): Promise<ExclusionGroupDetail> {
    return apiFetch<ExclusionGroupDetail>(`${BASE}/${groupId}`, { method: 'PUT', json: changes });
  },

  /** `DELETE /api/v1/mutual-exclusion-groups/{group_id}`: archive (ADMIN). */
  async archive(groupId: string): Promise<ExclusionGroupDetail> {
    return apiFetch<ExclusionGroupDetail>(`${BASE}/${groupId}`, { method: 'DELETE' });
  },

  /** `PUT /api/v1/mutual-exclusion-groups/{group_id}` with `{status: "active"}` (ADMIN). */
  async unarchive(groupId: string): Promise<ExclusionGroupDetail> {
    return apiFetch<ExclusionGroupDetail>(`${BASE}/${groupId}`, { method: 'PUT', json: { status: 'active' } });
  },

  /** `POST /api/v1/mutual-exclusion-groups/{group_id}/experiments`. */
  async addExperiment(groupId: string, experimentId: string): Promise<unknown> {
    return apiFetch<unknown>(`${BASE}/${groupId}/experiments`, {
      method: 'POST',
      json: { experiment_id: experimentId },
    });
  },

  /** `DELETE /api/v1/mutual-exclusion-groups/{group_id}/experiments/{experiment_id}`. */
  async removeExperiment(groupId: string, experimentId: string): Promise<unknown> {
    return apiFetch<unknown>(`${BASE}/${groupId}/experiments/${experimentId}`, { method: 'DELETE' });
  },
};
