/**
 * Type definitions for the Edge SDK.
 *
 * Flag evaluation and experiment assignment are decided by the server; the
 * shapes below mirror the public API (`GET /api/v1/feature-flags/evaluate/…`,
 * `POST /api/v1/tracking/assign`, `POST /api/v1/tracking/track|batch`).
 */

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

export interface EdgeSdkConfig {
  /** API key sent as `X-API-Key`. Required. */
  apiKey: string;
  /** Backend origin, e.g. `https://api.example.com`; the SDK appends `/api/v1/...`. Required: there is no default. */
  baseUrl: string;
  /**
   * How long a successful evaluation/assignment is reused per user + key, in ms.
   * Defaults to 60_000 (1 minute) — edge instances are short-lived.
   */
  cacheTtlMs?: number;
  /** Per-request timeout in milliseconds. Defaults to 500 — edge functions must be fast. */
  timeout?: number;
  /**
   * Optional shared store (Cloudflare KV, Deno KV, …) that persists server
   * results per user + key across isolates. Consulted after the in-memory
   * cache and before the network; written on every successful fetch.
   */
  store?: EdgeStore;
  /** Custom `fetch` implementation. Defaults to the global `fetch`. */
  fetch?: typeof fetch;
}

/**
 * Minimal key/value store used to share cached server results between edge
 * isolates. Keys are `flag:{userId}:{flagKey}` and
 * `assign:{userId}:{experimentKey}` (both parts URL-encoded); implementations
 * add their own namespace. Values are JSON strings; `ttlMs` is advisory
 * (stores with a coarser minimum TTL round up).
 */
export interface EdgeStore {
  get(key: string): Promise<string | null>;
  put(key: string, value: string, ttlMs: number): Promise<void>;
}

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Tracking
// ---------------------------------------------------------------------------

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

/** One entry for `trackBatch`. */
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

// ---------------------------------------------------------------------------
// Raw backend response shapes (see backend/app/schemas/tracking.py)
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Cache entry (internal)
// ---------------------------------------------------------------------------

export interface CacheEntry<T> {
  value: T;
  expiresAt: number;
}
