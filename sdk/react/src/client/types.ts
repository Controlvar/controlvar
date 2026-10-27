export interface SdkConfig {
  apiKey: string;
  baseUrl: string;
  /** Per-request timeout (default 5000 ms). */
  timeoutMs?: number;
  /** TTL for the per-user flag/assignment caches (default 300 000 ms). */
  cacheTtlMs?: number;
}

export interface UserContext {
  userId: string;
  /**
   * Sent as `context` on experiment assignment (POST body) and, when
   * non-empty, as the `context=<url-encoded JSON>` query parameter on flag
   * evaluation so targeting rules can evaluate against it. Assumed stable per
   * user: the caches are keyed by user + key, so call `clearCache()` after
   * changing attributes.
   */
  attributes?: Record<string, unknown>;
}

/**
 * Result of evaluating a feature flag for a user.
 *
 * `variant` is derived from the server response: `null` when the flag is off,
 * otherwise `config.variant` when that is a string, else `'on'`. The evaluate
 * route returns `config: null` for every flag, so an enabled flag's variant is
 * `'on'`.
 */
export interface FeatureFlagEvaluation {
  flagKey: string;
  variant: string | null; // null = off
  isEnabled: boolean;
  config: unknown | null;
  loading: boolean;
  error: Error | null;
  /**
   * Why the server decided as it did — `'targeting_rule'`, `'rollout'`,
   * `'inactive'` or `'error'`. `undefined` while loading, on error, or when
   * the server did not send one.
   */
  reason?: string;
}

/**
 * Result of assigning a user to an experiment variant.
 *
 * While loading or on error the defaults are
 * `variantKey: 'control', variantName: 'Control', variantId: null, isControl: true, configuration: null`.
 */
export interface ExperimentAssignment {
  experimentKey: string;
  variantKey: string;
  variantName: string;
  variantId: string | null;
  isControl: boolean;
  configuration: Record<string, unknown> | null;
  loading: boolean;
  error: Error | null;
  /**
   * `false` when the server did not enrol the user (global holdout, mutual
   * exclusion group or targeting rules) and returned the control variant
   * instead. `true` for a real assignment, also for older servers that do not
   * send the field. `undefined` while loading or on error.
   */
  assigned?: boolean;
  /**
   * Why the server decided as it did — `'assigned'`, `'holdout'`,
   * `'mutual_exclusion'` or `'targeting'`, or the status of an experiment that
   * is not ACTIVE (`'draft'`, `'paused'`, `'completed'` or `'archived'`).
   * `undefined` while loading, on error, or when the server did not send one.
   */
  reason?: string;
}

export interface TrackEventOptions {
  /** Attach the event to this experiment (sends `experiment_key`). */
  experimentKey?: string;
  /** Attach the event to this feature flag (sends `feature_flag_key`). */
  featureFlagKey?: string;
  /** Numeric value, e.g. an order total. */
  value?: number;
  /** Server `event_type`; defaults to the event name. */
  eventType?: string;
  /** Client-side timestamp; sent as an ISO-8601 string. */
  timestamp?: Date;
}

// ─── Raw backend response shapes (see backend/app/schemas/tracking.py) ──────

/** `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…[&context=<url-encoded JSON>]` */
export interface FeatureFlagEvaluateResponse {
  key: string;
  enabled: boolean;
  config: unknown | null;
  /** `targeting_rule` | `rollout` | `inactive` | `error`; absent on older servers. */
  reason?: string;
}

/** `POST /api/v1/tracking/assign` */
export interface ExperimentAssignResponse {
  experiment_key: string;
  user_id: string;
  variant_id: string;
  variant_name: string;
  is_control?: boolean;
  configuration?: Record<string, unknown> | null;
  /** `false` when the user was not enrolled (control returned); absent on older servers. */
  assigned?: boolean;
  /**
   * `assigned` | `holdout` | `mutual_exclusion` | `targeting`, or the status of an
   * experiment that is not ACTIVE (`draft` | `paused` | `completed` | `archived`);
   * absent on older servers.
   */
  reason?: string;
}
