import type { SafetyCheckResponse } from '@/types/safety';

export type UserRole = 'ADMIN' | 'DEVELOPER' | 'ANALYST' | 'VIEWER';

export interface AdminUser {
  id: string;
  username: string;
  /** `null` for an account created without an email address (`UserResponse.email`). */
  email: string | null;
  /** `null` for a legacy account created before roles were required (`UserResponse.role`). */
  role: UserRole | null;
  is_active: boolean;
  /**
   * What the admin area and every `/api/v1/admin` route check. Independent of
   * `role`: an Admin need not be a superuser, and a superuser may have any role.
   */
  is_superuser?: boolean;
  created_at: string;
  last_login?: string;
}

/**
 * Body of `PATCH /api/v1/admin/users/{id}` (`AdminUserPatch`). Send only the
 * keys that change; at least one is required and `null` is refused.
 */
export interface AdminUserPatch {
  role?: UserRole;
  is_active?: boolean;
}

/** `UserCreate` — body of `POST /api/v1/users/` (superuser only). */
export interface CreateUserRequest {
  username: string;
  email: string;
  /** Min 8 chars with at least one upper-case letter, one lower-case letter and one digit. */
  password: string;
  full_name?: string | null;
  is_active?: boolean;
  is_superuser?: boolean;
  role?: UserRole;
}

/** Response of `POST /api/v1/users/`. */
export interface CreatedUser {
  id: string;
  username: string;
  email: string;
  full_name: string | null;
  is_active: boolean;
  is_superuser: boolean;
  role: UserRole;
  created_at: string;
  updated_at: string;
}

/** One resource and the actions held on it: the API's `PermissionGrant`. */
export interface PermissionGrant {
  resource: string;
  actions: string[];
}

/** A custom role as `GET /api/v1/rbac/roles` returns it (`CustomRoleResponse`). */
export interface CustomRole {
  id: string;
  name: string;
  description: string | null;
  is_system_role: boolean;
  permissions: PermissionGrant[];
  created_at: string;
  /** How many users have this role. */
  user_count: number;
}

export interface AuditLog {
  id: string;
  /** Null for an entry the platform made on its own, or by a deleted user. */
  user_id: string | null;
  user_email: string;
  action_type: string;
  entity_type: string;
  entity_id: string;
  entity_name: string;
  old_value?: unknown;
  new_value?: unknown;
  reason?: string;
  timestamp: string;
  created_at: string;
  action_description: string;
}

// ---------------------------------------------------------------------------
// Safety monitoring — mirrors `backend/app/schemas/safety.py`.
// `SafetyCheckResponse` / `SafetyMetricStatus` live in `@/types/safety`.
// ---------------------------------------------------------------------------

/** `MetricThreshold`. Thresholds are in the metric's own unit
 *  (`error_rate` is a 0–1 fraction, `latency` is milliseconds). */
export interface SafetyMetricThreshold {
  warning_threshold: number | null;
  critical_threshold: number | null;
  comparison_type: 'greater_than' | 'less_than' | 'equal_to';
}

/** `SafetySettingsResponse` — `GET /api/v1/safety/settings`. */
export interface SafetySettings {
  id: string;
  enable_automatic_rollbacks: boolean;
  default_metrics: Record<string, SafetyMetricThreshold> | null;
  created_at: string;
  updated_at: string;
}

/** `SafetySettingsUpdate` — body of `POST /api/v1/safety/settings`. */
export interface SafetySettingsUpdate {
  enable_automatic_rollbacks?: boolean;
  default_metrics?: Record<string, SafetyMetricThreshold> | null;
}

/** `RollbackResponse` — `POST /api/v1/safety/feature-flags/{id}/rollback`. */
export interface RollbackResponse {
  success: boolean;
  feature_flag_id: string;
  message: string;
  trigger_type?: string | null;
  previous_percentage?: number | null;
  new_percentage?: number | null;
  rollback_record_id?: string | null;
  timestamp: string;
  details?: Record<string, unknown> | null;
}

export type FlagHealth = 'healthy' | 'warning' | 'critical';

/**
 * Admin dashboard view-model: a flag's `SafetyCheckResponse` joined with the
 * flag it describes. Built by `toFlagSafetyStatus()` in `@/services/admin`.
 */
export interface FlagSafetyStatus {
  flag_id: string;
  flag_name: string;
  flag_key: string;
  health: FlagHealth;
  /** The flag is ACTIVE. An inactive or archived flag shows "Off" instead of Roll back. */
  is_on: boolean;
  /** `error_rate` metric as a 0–1 fraction; null when not configured/measured. */
  error_rate: number | null;
  /** `latency` / `avg_latency` / `p95_latency` metric in ms; null when absent. */
  latency_ms: number | null;
  last_checked: string;
  check: SafetyCheckResponse;
}

/** A rollback performed in this session (the API has no rollback-history list). */
export interface RollbackRecord {
  id: string;
  flag_id: string;
  flag_name: string;
  rolled_back_by: string;
  reason: string;
  timestamp: string;
  previous_percentage?: number | null;
  new_percentage?: number | null;
}

export interface SchedulerHealth {
  name: string;
  status: 'running' | 'idle' | 'error';
  last_run: string;
  next_run: string;
  run_count: number;
  error_count: number;
}

export interface SchedulerRun {
  id: string;
  scheduler_name: string;
  started_at: string;
  completed_at: string;
  duration_ms: number;
  outcome: 'success' | 'error';
  error_message?: string;
}

/** `GET /api/v1/api-keys` item (`APIKeyRead`). No secret and no prefix are ever returned. */
export interface ApiKey {
  id: string;
  name: string;
  description?: string | null;
  scopes: string[];
  is_active: boolean;
  user_id: string;
  created_at: string;
  expires_at?: string | null;
  last_used_at?: string | null;
}

/**
 * `GET /api/v1/admin/stats`: counts grouped by what they count, and the
 * time they were counted.
 * `frontend/src/tests/fixtures/admin-stats.json` is an example, kept to this
 * shape by `backend/tests/unit/api/test_admin_stats_fixture.py`.
 */
export interface AdminStats {
  users: { total: number; active: number; superusers: number };
  experiments: { total: number; active: number };
  events: { total: number; daily_rate: number };
  feature_flags: { total: number; active: number };
  timestamp: string;
}

export interface UserListResponse {
  items: AdminUser[];
  total: number;
  /** Offset of the first item, as sent. */
  skip: number;
  limit: number;
}

export interface AuditLogListResponse {
  items: AuditLog[];
  total: number;
  page: number;
  limit: number;
}

export type NotificationChannel = 'slack' | 'email' | 'webhook';
export type NotificationStatus = 'sent' | 'failed' | 'skipped';

export interface NotificationPreference {
  id: string;
  user_id: string;
  notify_experiment_started: boolean;
  notify_experiment_completed: boolean;
  notify_safety_rollback: boolean;
  notify_rollout_advanced: boolean;
  slack_channel: string | null;
  email_override: string | null;
  created_at: string;
  updated_at: string;
}

export interface NotificationDeliveryLog {
  id: string;
  event_type: string;
  channel: NotificationChannel;
  recipient: string;
  subject: string | null;
  status: NotificationStatus;
  error_message: string | null;
  created_at: string;
}

export interface NotificationDeliveryLogListResponse {
  items: NotificationDeliveryLog[];
  total: number;
  page: number;
  limit: number;
}

export const ROLE_COLORS: Record<UserRole, string> = {
  ADMIN: 'bg-red-100 text-red-800',
  DEVELOPER: 'bg-blue-100 text-blue-800',
  ANALYST: 'bg-purple-100 text-purple-800',
  VIEWER: 'bg-slate-100 text-slate-700',
};

export const USER_ROLE_LABELS: Record<UserRole, string> = {
  ADMIN: 'Admin',
  DEVELOPER: 'Developer',
  ANALYST: 'Analyst',
  VIEWER: 'Viewer',
};
