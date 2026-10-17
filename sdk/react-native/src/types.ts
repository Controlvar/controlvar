/**
 * Core types for the React Native Controlvar SDK.
 *
 * Flag evaluation and experiment assignment are decided by the server; the
 * shapes below mirror the public API (`GET /api/v1/feature-flags/evaluate/…`,
 * `POST /api/v1/tracking/assign`, `POST /api/v1/tracking/track|batch`).
 */

/** Operations whose failures are swallowed and reported through `onError`. */
export type SwallowedOperation = 'evaluateFlag' | 'getAssignment' | 'getAllFlags' | 'track' | 'trackBatch';

/** Configuration for {@link ExperimentationClient}. */
export interface SdkConfig {
  /** API key for authenticating with Controlvar (sent as `X-API-Key`). */
  apiKey: string;
  /** Backend origin, e.g. `'https://api.controlvar.example.com'`; the SDK appends `/api/v1/...`. */
  baseUrl: string;
  /** HTTP request timeout in milliseconds. Defaults to 5000. */
  timeoutMs?: number;
  /**
   * How long a successful evaluation/assignment is reused per user + key, in
   * milliseconds (in-memory and AsyncStorage). Defaults to 300_000 (5 minutes).
   */
  cacheTtlMs?: number;
  /**
   * When true, server results are persisted to AsyncStorage per user + key and
   * served (even past their TTL) as a fallback when the API is unreachable.
   * Defaults to true.
   */
  offlineFallback?: boolean;
  /**
   * Called whenever the client swallows a failure (`evaluateFlag`,
   * `getAssignment`, `getAllFlags`, `track`, `trackBatch` never throw).
   */
  onError?: (error: Error, operation: SwallowedOperation) => void;
}

/** Result of `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…`. */
export interface FlagEvaluation {
  key: string;
  enabled: boolean;
  config: unknown | null;
}

/** Result of `POST /api/v1/tracking/assign`, mapped to camelCase. */
export interface Assignment {
  experimentKey: string;
  userId: string;
  variantId: string | null;
  variantName: string;
  isControl: boolean;
  configuration: Record<string, unknown> | null;
}

/** Options for {@link ExperimentationClient.track}. */
export interface TrackOptions {
  /** Numeric value, e.g. an order total. */
  value?: number;
  /** Attach the event to this experiment (sends `experiment_key`). */
  experimentKey?: string;
  /** Attach the event to this feature flag (sends `feature_flag_key`). */
  featureFlagKey?: string;
  /** Server `event_type`; defaults to the event name. */
  eventType?: string;
  /** Client-side timestamp; sent as an ISO-8601 string. */
  timestamp?: Date | string;
}

/** One entry for {@link ExperimentationClient.trackBatch}. */
export interface TrackEvent extends TrackOptions {
  eventName: string;
  userId: string;
  /** Arbitrary event properties; sent as `metadata`. */
  properties?: Record<string, unknown>;
}

/** Aggregated result of `trackBatch` across all chunks. */
export interface BatchResult {
  successCount: number;
  failureCount: number;
  /** Server-reported per-event errors plus one `{message, status?}` entry per failed chunk. */
  errors: unknown[];
}

/** Return type of {@link useFlag}. */
export interface FlagState extends FlagEvaluation {
  /** Alias of `enabled` (kept for compatibility with 0.1). */
  value: boolean;
  loading: boolean;
  error: Error | null;
}

/** Return type of {@link useExperiment}. */
export interface ExperimentState {
  experimentKey: string;
  /** Assigned variant name, or `null` while loading / when assignment failed. */
  variant: string | null;
  variantId: string | null;
  variantName: string | null;
  isControl: boolean;
  configuration: Record<string, unknown> | null;
  loading: boolean;
  error: Error | null;
}

/** Internal cache entry (in-memory and AsyncStorage). */
export interface CacheEntry<T> {
  value: T;
  expiresAt: number;
}

// ─── Raw backend response shapes (see backend/app/schemas/tracking.py) ──────

export interface AssignResponse {
  experiment_key: string;
  user_id: string;
  variant_id?: string | null;
  variant_name: string;
  is_control?: boolean;
  configuration?: Record<string, unknown> | null;
}

export interface FlagEvaluateResponse {
  key: string;
  enabled: boolean;
  config?: unknown | null;
}

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
