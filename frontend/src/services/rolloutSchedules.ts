import { apiFetch } from '@/services/api';
import type { RolloutSchedule, RolloutStage } from '@/services/featureFlags';

/**
 * The rollout-schedule writes the flag page sends (#1456). The list stays
 * `FeatureFlagsService.listRolloutSchedules`.
 *
 * Every route here needs UPDATE on the schedule's flag
 * (`_require_update_on_flag`): ADMIN, DEVELOPER or a superuser, which is
 * `canChangeFeatureFlags`.
 *
 * Stage dates are sent as naive UTC (`YYYY-MM-DDTHH:MM:SS`); the API takes a
 * value with no zone as UTC (`UTCDateTime`).
 */

/** The two triggers the dashboard offers. `metric_based` never starts by itself. */
export type DashboardTrigger = 'manual' | 'time_based';

/** A stage of `POST /rollout-schedules/` and `POST /{schedule_id}/stages`. */
export interface RolloutStageCreateRequest {
  name: string;
  stage_order: number;
  target_percentage: number;
  trigger_type: DashboardTrigger;
  /** Naive UTC; absent for a manual stage or a time-based one with no time. */
  start_date?: string;
}

/** `POST /api/v1/rollout-schedules/`. The schedule's own dates are not sent. */
export interface RolloutScheduleCreateRequest {
  name: string;
  description?: string;
  feature_flag_id: string;
  max_percentage: number;
  stages: RolloutStageCreateRequest[];
}

/** `PUT /api/v1/rollout-schedules/stages/{stage_id}`: only the changed fields. */
export interface RolloutStageUpdateRequest {
  name?: string;
  target_percentage?: number;
  trigger_type?: DashboardTrigger;
  /** Naive UTC, or null to clear it. */
  start_date?: string | null;
}

const BASE = '/api/v1/rollout-schedules';

export const RolloutSchedulesService = {
  async create(data: RolloutScheduleCreateRequest): Promise<RolloutSchedule> {
    return apiFetch<RolloutSchedule>(`${BASE}/`, { method: 'POST', json: data });
  },

  async addStage(scheduleId: string, data: RolloutStageCreateRequest): Promise<RolloutStage> {
    return apiFetch<RolloutStage>(`${BASE}/${scheduleId}/stages`, { method: 'POST', json: data });
  },

  async updateStage(stageId: string, data: RolloutStageUpdateRequest): Promise<RolloutStage> {
    return apiFetch<RolloutStage>(`${BASE}/stages/${stageId}`, { method: 'PUT', json: data });
  },

  async deleteStage(stageId: string): Promise<void> {
    await apiFetch<void>(`${BASE}/stages/${stageId}`, { method: 'DELETE' });
  },

  /** From draft, or from paused (a resume). */
  async activate(scheduleId: string): Promise<RolloutSchedule> {
    return apiFetch<RolloutSchedule>(`${BASE}/${scheduleId}/activate`, { method: 'POST' });
  },

  async pause(scheduleId: string): Promise<RolloutSchedule> {
    return apiFetch<RolloutSchedule>(`${BASE}/${scheduleId}/pause`, { method: 'POST' });
  },

  async cancel(scheduleId: string): Promise<RolloutSchedule> {
    return apiFetch<RolloutSchedule>(`${BASE}/${scheduleId}/cancel`, { method: 'POST' });
  },

  /**
   * Start a pending manual stage, or complete an in-progress one (which starts
   * the next pending stage at once, whatever its trigger).
   */
  async advance(stageId: string): Promise<RolloutStage> {
    return apiFetch<RolloutStage>(`${BASE}/stages/${stageId}/advance`, { method: 'POST' });
  },
};
