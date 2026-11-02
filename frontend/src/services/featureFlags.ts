import { TargetingRules } from '@/types/targeting';
import type { SafetyCheckResponse } from '@/types/safety';
import type { ValueFlagType } from '@/utils/flagValues';
import { apiFetch } from '@/services/api';

export type FeatureFlagStatus = 'active' | 'inactive' | 'archived';

/**
 * Feature flag as returned by the API.
 *
 * Two shapes exist on the backend:
 * - `GET /feature-flags/{id}`, activate/deactivate and `PUT` return the service
 *   dict: `{ status: "active"|"inactive", rules, rollout_percentage, ... }`.
 * - `GET /feature-flags/` items are validated by `FeatureFlagReadExtended`:
 *   `{ status: "active"|"inactive"|"archived", is_active, targeting_rules, ... }`
 *   where `is_active` is derived from `status` server-side.
 * Use `isFlagOn()` / `flagRules()` instead of reading either field directly.
 */
export interface FeatureFlag {
  id: string;
  name: string;
  key: string;
  description?: string | null;
  status?: FeatureFlagStatus | string;
  is_active?: boolean;
  rollout_percentage: number;
  targeting_rules?: TargetingRules | Record<string, unknown> | null;
  rules?: TargetingRules | Record<string, unknown> | null;
  tags?: string[] | null;
  /**
   * What the flag serves (#1497): `boolean`, `string`, `number` or `json`,
   * read as any string so a type a later release adds is still shown. Absent
   * from a server without values: read it with `flagValueType()`.
   */
  value_type?: string;
  /** What the users it is on for get; `true` for a boolean flag. */
  on_value?: unknown;
  /** What everyone else gets, and everyone while it is off; `false` for a boolean flag. */
  off_value?: unknown;
  owner_id: string | null;
  created_at: string;
  updated_at: string;
}

export function isFlagOn(flag: Pick<FeatureFlag, 'status' | 'is_active'>): boolean {
  if (typeof flag.status === 'string') return flag.status.toLowerCase() === 'active';
  return flag.is_active === true;
}

export function flagRules(flag: Pick<FeatureFlag, 'targeting_rules' | 'rules'>): object | null {
  const value = flag.targeting_rules ?? flag.rules ?? null;
  return value && typeof value === 'object' ? (value as object) : null;
}

/** `FeatureFlagCreate` — `is_active` maps to `status` server-side. */
export interface CreateFeatureFlagRequest {
  name: string;
  key: string;
  description?: string;
  is_active?: boolean;
  rollout_percentage?: number;
  targeting_rules?: TargetingRules | Record<string, unknown> | null;
  tags?: string[];
  /** Left out for a boolean flag, which sends neither value. */
  value_type?: ValueFlagType;
  on_value?: unknown;
  off_value?: unknown;
}

/**
 * `FeatureFlagUpdate` — every field optional. It has no `value_type`: the
 * type is set when a flag is created and the dashboard never sends it again
 * (the API answers any other value with 422 `read_only`).
 */
export interface UpdateFeatureFlagRequest {
  name?: string;
  key?: string;
  description?: string;
  is_active?: boolean;
  rollout_percentage?: number;
  targeting_rules?: TargetingRules | Record<string, unknown> | null;
  tags?: string[];
  on_value?: unknown;
  off_value?: unknown;
}

export interface FeatureFlagListResponse {
  items: FeatureFlag[];
  total: number;
  skip: number;
  limit: number;
}

/** `ToggleResponse` from `POST /feature-flags/{id}/toggle|enable|disable`. */
export interface FeatureFlagToggleResponse {
  id: string;
  name: string;
  key: string;
  status: string;
  updated_at: string;
  audit_log_id: string | null;
}

// ---------------------------------------------------------------------------
// Rollout schedules (`/api/v1/rollout-schedules`)
// ---------------------------------------------------------------------------

export type RolloutScheduleStatus = 'draft' | 'active' | 'paused' | 'completed' | 'cancelled';
export type RolloutStageStatus = 'pending' | 'in_progress' | 'completed' | 'failed';
export type RolloutTriggerType = 'time_based' | 'metric_based' | 'manual';

export interface RolloutStage {
  id: string;
  rollout_schedule_id: string;
  name: string;
  description?: string | null;
  stage_order: number;
  target_percentage: number;
  trigger_type: RolloutTriggerType;
  trigger_configuration?: Record<string, unknown> | null;
  start_date?: string | null;
  status: RolloutStageStatus;
  completed_date?: string | null;
  created_at: string;
  updated_at: string;
}

export interface RolloutSchedule {
  id: string;
  name: string;
  description?: string | null;
  feature_flag_id: string;
  owner_id?: string | null;
  status: RolloutScheduleStatus;
  start_date?: string | null;
  end_date?: string | null;
  max_percentage: number;
  min_stage_duration?: number | null;
  config_data?: Record<string, unknown> | null;
  stages: RolloutStage[];
  created_at: string;
  updated_at: string;
}

export interface RolloutScheduleListResponse {
  items: RolloutSchedule[];
  total: number;
  skip: number;
  limit: number;
}

const BASE = '/api/v1/feature-flags';

export const FeatureFlagsService = {
  /** `status` is matched against the DB enum (`ACTIVE`/`INACTIVE`/`ARCHIVED`). */
  async list(params?: {
    status?: FeatureFlagStatus;
    skip?: number;
    limit?: number;
    search?: string;
  }): Promise<FeatureFlagListResponse> {
    return apiFetch<FeatureFlagListResponse>(BASE, {
      query: {
        status: params?.status ? params.status.toUpperCase() : undefined,
        skip: params?.skip,
        limit: params?.limit,
        search: params?.search,
      },
    });
  },

  async get(id: string): Promise<FeatureFlag> {
    return apiFetch<FeatureFlag>(`${BASE}/${id}`);
  },

  async create(data: CreateFeatureFlagRequest): Promise<FeatureFlag> {
    return apiFetch<FeatureFlag>(BASE, { method: 'POST', json: data });
  },

  async update(id: string, data: UpdateFeatureFlagRequest): Promise<FeatureFlag> {
    return apiFetch<FeatureFlag>(`${BASE}/${id}`, { method: 'PUT', json: data });
  },

  async delete(id: string): Promise<void> {
    await apiFetch<void>(`${BASE}/${id}`, { method: 'DELETE' });
  },

  /**
   * Explicit on/off. `enable`/`disable` are idempotent and audit-logged
   * (`ToggleRequest { reason? }`), unlike `/toggle` which flips whatever the
   * server currently holds and can race a stale UI.
   */
  async setEnabled(id: string, enabled: boolean, reason?: string): Promise<FeatureFlagToggleResponse> {
    return apiFetch<FeatureFlagToggleResponse>(`${BASE}/${id}/${enabled ? 'enable' : 'disable'}`, {
      method: 'POST',
      json: { reason: reason ?? null },
    });
  },

  async listRolloutSchedules(flagId: string): Promise<RolloutScheduleListResponse> {
    return apiFetch<RolloutScheduleListResponse>('/api/v1/rollout-schedules', {
      query: { feature_flag_id: flagId, limit: 50 },
    });
  },

  /**
   * `GET /api/v1/safety/feature-flags/{id}/check` — the only client for this
   * route (the admin safety dashboard uses it too).
   */
  async safetyCheck(flagId: string): Promise<SafetyCheckResponse> {
    return apiFetch<SafetyCheckResponse>(`/api/v1/safety/feature-flags/${flagId}/check`);
  },
};
