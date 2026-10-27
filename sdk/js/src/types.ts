import type { ExperimentationError } from './errors';

/**
 * Operations whose failures are swallowed and reported through `onError`:
 * `'refresh'` (a ruleset fetch), `'flush'` (sending evaluation counts) and `'evaluate'` (an
 * unexpected failure inside local evaluation; the call then goes to the server) occur only with
 * `evaluation: 'local'`. `'reportError'` is an error report the server did not store.
 */
export type SwallowedOperation = 'track' | 'trackBatch' | 'refresh' | 'flush' | 'evaluate' | 'reportError';

/** Where flags are evaluated: by the server (the default) or in-process from its ruleset. */
export type EvaluationMode = 'server' | 'local';

export interface ClientConfig {
  /** Backend origin, e.g. `https://api.example.com`. The SDK appends `/api/v1/...`. */
  apiUrl: string;
  /** API key sent as `X-API-Key`. */
  apiKey: string;
  /** Per-request timeout in milliseconds (default 5000). */
  timeoutMs?: number;
  /** How long a successful evaluation/assignment is reused, per user + key (default 300 000 ms). */
  cacheTtlMs?: number;
  /** Variant name returned by `getVariant` when assignment fails (default `'control'`). */
  defaultVariant?: string;
  /** Custom `fetch` implementation. Defaults to the global `fetch` (Node >= 18, browsers). */
  fetch?: typeof fetch;
  /**
   * Called with the error whenever `track` / `trackBatch` / `reportError` swallow a failure, and
   * in local mode when a ruleset refresh (`'refresh'`), an evaluation-count report (`'flush'`) or
   * a local evaluation (`'evaluate'`, which then goes to the server) fails.
   * Those operations never reject; this is the only way to observe their failures.
   */
  onError?: (error: ExperimentationError, operation: SwallowedOperation) => void;
  /**
   * `'server'` (default): every flag is evaluated by the server. `'local'`: flags are evaluated
   * in-process from `GET /api/v1/sdk/ruleset` (beta) whenever the answer is provably the
   * server's, and by the server otherwise. Server-side code only: the ruleset holds every flag's
   * targeting rules, the key needs the `sdk:ruleset` scope, and the constructor throws in a
   * browser. Experiments are always assigned by the server.
   */
  evaluation?: EvaluationMode;
  /**
   * Local mode: how often the ruleset is refreshed (default 30 000 ms, minimum 5 000 ms, ±10%
   * jitter). Backoff, `Retry-After` and the 10-minute retry after a refusal are not jittered.
   */
  refreshIntervalMs?: number;
  /**
   * Local mode: stop answering locally when the ruleset has not been refreshed successfully for
   * this long, and evaluate on the server instead. Default: no limit (a stale ruleset keeps
   * being served while the API is unreachable).
   */
  maxStaleMs?: number;
}

export interface UserContext {
  /** Stable identifier used by the server for sticky bucketing. */
  userId: string;
  /**
   * Sent as `context` on experiment assignment (POST body) and, when non-empty, as the
   * `context=<url-encoded JSON>` query parameter on flag evaluation (targeting rules).
   * Assumed stable per user: caches are keyed by user + key, so call `clearCache()`
   * after changing attributes.
   */
  attributes?: Record<string, unknown>;
}

/** Result of `POST /api/v1/tracking/assign`, mapped to camelCase. */
export interface Assignment {
  experimentKey: string;
  userId: string;
  variantId: string | null;
  variantName: string;
  isControl: boolean;
  configuration: Record<string, unknown> | null;
  /**
   * `true` when the server enrolled the user in the experiment; `false` when it did not
   * (global holdout, mutual exclusion group or targeting rules) and returned the control
   * variant so you render the default experience, with no exposure recorded.
   * `undefined` when the server predates the field: such a server cannot tell you, so do
   * not read `undefined` as either answer.
   */
  assigned?: boolean;
  /**
   * Why the server decided as it did: `'assigned'`, `'holdout'`, `'mutual_exclusion'` or
   * `'targeting'`. `undefined` when the server did not send one.
   */
  reason?: string;
}

/** Result of `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…[&context=…]`. */
export interface FlagEvaluation {
  key: string;
  enabled: boolean;
  config: unknown | null;
  /**
   * Why the server decided as it did — `'targeting_rule'`, `'rollout'`, `'inactive'` or
   * `'error'`. `undefined` when the server did not send one. A local answer carries the
   * reason the server would have given.
   */
  reason?: string;
  /**
   * Set only with `evaluation: 'local'`: `'local'` when answered in-process from the ruleset,
   * `'server'` when the server evaluated it.
   */
  source?: 'server' | 'local';
}

export interface TrackOptions {
  /** Numeric value, e.g. an order total. */
  value?: number;
  /** Arbitrary event properties; sent as `metadata`. */
  properties?: Record<string, unknown>;
  /** Attach the event to this experiment (sends `experiment_key`). */
  experimentKey?: string;
  /** Attach the event to this feature flag (sends `feature_flag_key`). */
  featureFlagKey?: string;
  /** Server `event_type`; defaults to the event name. */
  eventType?: string;
  /** Client-side timestamp; sent as an ISO-8601 string. */
  timestamp?: Date | string;
}

/** One entry for `trackBatch`. */
export interface TrackEvent extends TrackOptions {
  userId: string;
  eventName: string;
}

/**
 * One error report for `reportError`, sent as `POST /api/v1/tracking/errors`. The server checks
 * it: a report needs `featureFlagKey` or `experimentKey` (422 otherwise), and a key it does not
 * know answers 404.
 */
export interface ErrorReport {
  /** Error class, e.g. `'crash'` or `'payment_failed'` (1-100 characters). */
  errorType: string;
  /** What happened (not empty). */
  message: string;
  /** The flag that was on when it happened (sent as `feature_flag_key`). */
  featureFlagKey?: string;
  /** The experiment the user was in (sent as `experiment_key`). */
  experimentKey?: string;
  /** The user or device (sent as `user_id`). */
  userId?: string;
  /** Sent as `stack_trace`. */
  stackTrace?: string;
  /** Extra debugging data (os, app version, ...). */
  metadata?: Record<string, unknown>;
  /**
   * When it happened, sent as ISO-8601. The stored row is timed when the server receives it; this
   * value is kept in its metadata as `client_timestamp`.
   */
  timestamp?: Date | string;
}

/** Aggregated result of `trackBatch` across all chunks. */
export interface BatchResult {
  successCount: number;
  failureCount: number;
  /** Server-reported per-event errors plus one `{message, status?}` entry per failed chunk. */
  errors: unknown[];
}

/** One row of `GET /api/v1/tracking/assignments/{user_id}` (shape defined by the server). */
export type AssignmentRecord = Record<string, unknown>;

// ─── Raw backend response shapes (see backend/app/schemas/tracking.py) ──────

/** `POST /api/v1/tracking/assign` */
export interface AssignResponse {
  experiment_key: string;
  user_id: string;
  variant_id?: string | null;
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

/** `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…[&context=<url-encoded JSON>]` */
export interface FlagEvaluateResponse {
  key: string;
  enabled: boolean;
  config?: unknown | null;
  /** `targeting_rule` | `rollout` | `inactive` | `error`; absent on older servers. */
  reason?: string;
}

/** `POST /api/v1/tracking/batch` */
export interface BatchResponse {
  success_count: number;
  failure_count: number;
  errors?: unknown[] | null;
}

/** Body of `POST /api/v1/tracking/track` and of each `/tracking/batch` entry. */
export interface TrackBody {
  event_type: string;
  event_name: string;
  user_id: string;
  experiment_key?: string;
  feature_flag_key?: string;
  value?: number;
  metadata?: Record<string, unknown>;
  timestamp?: string;
}
