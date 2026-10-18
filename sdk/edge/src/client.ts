/**
 * EdgeExperimentationClient — the main client for edge environments.
 *
 * Design goals:
 *  - Zero Node.js built-ins (no `fs`, no `crypto` module, no `Buffer`)
 *  - Works in Cloudflare Workers, Vercel Edge Functions, Deno Deploy, and any
 *    WinterCG-compatible runtime
 *  - AbortController-based timeouts (Web standard, supported everywhere)
 *
 * Behaviour:
 *  - **The server decides.** Flag evaluation goes to
 *    `GET /api/v1/feature-flags/evaluate/{key}?user_id=…` and assignment to
 *    `POST /api/v1/tracking/assign`. Nothing is bucketed locally; `hashUser`
 *    is exported as a utility only.
 *  - **Caching.** Successful results are cached in memory per user + key for
 *    `cacheTtlMs`, and optionally in a shared `store` (Cloudflare KV, Deno KV)
 *    so other isolates can reuse them. Failures are never cached. Concurrent
 *    calls for the same user + key share one in-flight request.
 *  - **Failure values.** `evaluateFlag` resolves to a disabled evaluation and
 *    `getAssignment` to `null` when the server cannot be reached; `track`
 *    never throws.
 */

import { EdgeCache } from './cache.js';
import type {
  Assignment,
  AssignResponse,
  BatchResponse,
  BatchResult,
  EdgeSdkConfig,
  EdgeStore,
  FlagEvaluateResponse,
  FlagEvaluation,
  TrackBody,
  TrackEvent,
  TrackOptions,
} from './types.js';

const DEFAULT_CACHE_TTL_MS = 60_000; // 1 minute
const DEFAULT_TIMEOUT_MS = 500;      // 500ms — edge functions must be fast
/** Maximum events per `POST /api/v1/tracking/batch` request. */
const BATCH_LIMIT = 100;

const FLAG_PREFIX = 'flag:';
const ASSIGN_PREFIX = 'assign:';

/** Error thrown for non-2xx responses; carries the HTTP status. */
export class EdgeApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = 'EdgeApiError';
    this.status = status;
  }
}

interface RequestInitLite {
  method?: 'GET' | 'POST';
  body?: unknown;
}

function encode(value: string): string {
  return encodeURIComponent(value);
}

function toIso(timestamp: Date | string): string {
  return timestamp instanceof Date ? timestamp.toISOString() : String(timestamp);
}

export class EdgeExperimentationClient {
  protected readonly apiKey: string;
  protected readonly baseUrl: string;
  protected readonly cacheTtlMs: number;
  protected readonly timeout: number;
  protected readonly store: EdgeStore | undefined;
  private readonly fetchImpl: typeof fetch | undefined;

  /** Per-user flag evaluations (keyed by "flag:{userId}:{flagKey}"). */
  private readonly flagCache: EdgeCache<FlagEvaluation>;
  /** Per-user assignments (keyed by "assign:{userId}:{experimentKey}"). */
  private readonly assignmentCache: EdgeCache<Assignment>;
  /** Requests currently in flight, so concurrent callers share one fetch. */
  private readonly inflight = new Map<string, Promise<unknown>>();

  constructor(config: EdgeSdkConfig) {
    if (!config || !config.apiKey) throw new Error('apiKey is required');
    // No default: the SDK talks only to the origin it is given.
    if (typeof config.baseUrl !== 'string' || !config.baseUrl.trim()) {
      throw new Error(
        "baseUrl is required: the origin of your API, such as 'https://api.example.com' " +
          '(the SDK appends /api/v1/...)'
      );
    }

    this.apiKey = config.apiKey;
    this.baseUrl = config.baseUrl.trim().replace(/\/+$/, '');
    this.cacheTtlMs = config.cacheTtlMs ?? DEFAULT_CACHE_TTL_MS;
    this.timeout = config.timeout ?? DEFAULT_TIMEOUT_MS;
    this.store = config.store;
    this.fetchImpl = config.fetch;

    this.flagCache = new EdgeCache<FlagEvaluation>(this.cacheTtlMs);
    this.assignmentCache = new EdgeCache<Assignment>(this.cacheTtlMs);
  }

  // ---------------------------------------------------------------------------
  // Synchronous access (zero latency — in-memory cache only, never the network)
  // ---------------------------------------------------------------------------

  /** Cached evaluation for this user + flag, or `null` when not cached (or expired). */
  getCachedFlag(flagKey: string, userId: string): FlagEvaluation | null {
    return this.flagCache.get(flagKey_(userId, flagKey)) ?? null;
  }

  /** Cached assignment for this user + experiment, or `null` when not cached (or expired). */
  getCachedAssignment(experimentKey: string, userId: string): Assignment | null {
    return this.assignmentCache.get(assignKey(userId, experimentKey)) ?? null;
  }

  /**
   * Whether the flag is enabled according to the in-memory cache.
   * Returns `false` when nothing is cached — call `evaluateFlag` first.
   * Use this in the critical path of edge request handling.
   */
  evaluateFlagSync(flagKey: string, userId: string): boolean {
    return this.getCachedFlag(flagKey, userId)?.enabled ?? false;
  }

  /**
   * The cached variant name for this user + experiment, or `null` when
   * nothing is cached — call `getAssignment` first.
   */
  getAssignmentSync(experimentKey: string, userId: string): string | null {
    return this.getCachedAssignment(experimentKey, userId)?.variantName ?? null;
  }

  // ---------------------------------------------------------------------------
  // Feature flags
  // ---------------------------------------------------------------------------

  /**
   * Evaluate a flag via `GET /api/v1/feature-flags/evaluate/{key}?user_id=…`.
   * Order: in-memory cache → shared store → network. Never throws: on
   * failure (404 unknown flag key, network error, timeout) resolves to
   * `{key, enabled: false, config: null}`, which is not cached.
   */
  async evaluateFlag(flagKey: string, userId: string): Promise<FlagEvaluation> {
    const cacheKey = flagKey_(userId, flagKey);
    const cached = this.flagCache.get(cacheKey);
    if (cached) return cached;

    return this.dedupe(cacheKey, async () => {
      const stored = await this.storeGet<FlagEvaluation>(cacheKey, isFlagEvaluation);
      if (stored) {
        this.flagCache.set(cacheKey, stored);
        return stored;
      }

      try {
        const data = await this.requestJson<FlagEvaluateResponse>(
          `/api/v1/feature-flags/evaluate/${encode(flagKey)}?user_id=${encode(userId)}`,
        );
        const evaluation: FlagEvaluation = {
          key: flagKey,
          enabled: Boolean(data?.enabled),
          config: data?.config === undefined ? null : data.config,
        };
        this.flagCache.set(cacheKey, evaluation);
        await this.storePut(cacheKey, evaluation);
        return evaluation;
      } catch {
        return { key: flagKey, enabled: false, config: null };
      }
    });
  }

  /** `true` only when the server says the flag is enabled for this user; `false` on any failure. */
  async isFeatureEnabled(flagKey: string, userId: string): Promise<boolean> {
    return (await this.evaluateFlag(flagKey, userId)).enabled;
  }

  /**
   * All flags for a user via `GET /api/v1/feature-flags/user/{user_id}` →
   * `{flagKey: enabled}`. Not cached and not part of the tracking fan-out
   * (it carries no `config`). Resolves to `{}` on failure.
   */
  async getAllFlags(userId: string): Promise<Record<string, boolean>> {
    try {
      const data = await this.requestJson<Record<string, unknown>>(
        `/api/v1/feature-flags/user/${encode(userId)}`,
      );
      const flags: Record<string, boolean> = {};
      if (data && typeof data === 'object') {
        for (const [key, value] of Object.entries(data)) flags[key] = Boolean(value);
      }
      return flags;
    } catch {
      return {};
    }
  }

  // ---------------------------------------------------------------------------
  // Experiments
  // ---------------------------------------------------------------------------

  /**
   * Assign the user via `POST /api/v1/tracking/assign` (sticky on the server,
   * records the exposure). `attributes` is sent as `context`. Order: in-memory
   * cache → shared store → network. Never throws: resolves to `null` when the
   * experiment is not ACTIVE (404) or the server cannot be reached.
   */
  async getAssignment(
    experimentKey: string,
    userId: string,
    attributes?: Record<string, unknown>,
  ): Promise<Assignment | null> {
    const cacheKey = assignKey(userId, experimentKey);
    const cached = this.assignmentCache.get(cacheKey);
    if (cached) return cached;

    return this.dedupe(cacheKey, async () => {
      const stored = await this.storeGet<Assignment>(cacheKey, isAssignment);
      if (stored) {
        this.assignmentCache.set(cacheKey, stored);
        return stored;
      }

      try {
        const body: Record<string, unknown> = { experiment_key: experimentKey, user_id: userId };
        if (attributes !== undefined) body.context = attributes;
        const data = await this.requestJson<AssignResponse>('/api/v1/tracking/assign', {
          method: 'POST',
          body,
        });
        if (!data || typeof data.variant_name !== 'string') return null;
        const assignment: Assignment = {
          experimentKey: typeof data.experiment_key === 'string' ? data.experiment_key : experimentKey,
          userId,
          variantId: data.variant_id ?? null,
          variantName: data.variant_name,
          isControl: Boolean(data.is_control),
          configuration: data.configuration ?? null,
        };
        this.assignmentCache.set(cacheKey, assignment);
        await this.storePut(cacheKey, assignment);
        return assignment;
      } catch {
        return null;
      }
    });
  }

  /** The assigned variant name, or `null` when assignment fails. */
  async getVariant(
    experimentKey: string,
    userId: string,
    attributes?: Record<string, unknown>,
  ): Promise<string | null> {
    return (await this.getAssignment(experimentKey, userId, attributes))?.variantName ?? null;
  }

  // ---------------------------------------------------------------------------
  // Tracking
  // ---------------------------------------------------------------------------

  /**
   * Track an event. Fire-and-forget: never throws.
   *
   * - With `options.experimentKey` / `options.featureFlagKey`: one
   *   `POST /api/v1/tracking/track`.
   * - Without keys: one `POST /api/v1/tracking/batch` (chunked at 100) with
   *   one entry per cached assignment and one per cached evaluated flag for
   *   this user (in-memory cache only). Nothing cached → nothing is sent.
   */
  async track(
    eventName: string,
    userId: string,
    properties: Record<string, unknown> = {},
    options: TrackOptions = {},
  ): Promise<void> {
    try {
      const events = this.expand({ eventName, userId, properties, ...options });
      if (events.length === 0) return;

      if (options.experimentKey || options.featureFlagKey) {
        await this.requestOk('/api/v1/tracking/track', { method: 'POST', body: events[0] });
        return;
      }

      for (let i = 0; i < events.length; i += BATCH_LIMIT) {
        await this.requestOk('/api/v1/tracking/batch', {
          method: 'POST',
          body: { events: events.slice(i, i + BATCH_LIMIT) },
        });
      }
    } catch {
      // fire-and-forget — never throw
    }
  }

  /**
   * Send many events via `POST /api/v1/tracking/batch`, chunked at 100.
   * Entries without a key are expanded with the same fan-out rule as `track`.
   * Never rejects: a failed chunk counts all its events as failures.
   */
  async trackBatch(events: TrackEvent[]): Promise<BatchResult> {
    const result: BatchResult = { successCount: 0, failureCount: 0, errors: [] };
    const bodies: TrackBody[] = [];
    for (const event of events) bodies.push(...this.expand(event));

    for (let i = 0; i < bodies.length; i += BATCH_LIMIT) {
      const chunk = bodies.slice(i, i + BATCH_LIMIT);
      try {
        const data = await this.requestJson<BatchResponse>('/api/v1/tracking/batch', {
          method: 'POST',
          body: { events: chunk },
        });
        result.successCount += Number(data?.success_count ?? 0);
        result.failureCount += Number(data?.failure_count ?? 0);
        if (Array.isArray(data?.errors)) result.errors.push(...data.errors);
      } catch (err) {
        result.failureCount += chunk.length;
        result.errors.push({
          message: err instanceof Error ? err.message : String(err),
          status: err instanceof EdgeApiError ? err.status : undefined,
        });
      }
    }
    return result;
  }

  // ---------------------------------------------------------------------------
  // Cache access
  // ---------------------------------------------------------------------------

  /** Cached (successful, unexpired) assignments for the user, in assignment order. */
  getAssignments(userId: string): Assignment[] {
    return this.assignmentCache.entries(`${ASSIGN_PREFIX}${encode(userId)}:`).map(([, a]) => a);
  }

  /** Keys of flags successfully evaluated (and still cached) for the user. */
  getEvaluatedFlags(userId: string): string[] {
    return this.flagCache.entries(`${FLAG_PREFIX}${encode(userId)}:`).map(([, e]) => e.key);
  }

  /** Drop every cached evaluation and assignment (in-memory only). */
  clearCache(): void {
    this.flagCache.clear();
    this.assignmentCache.clear();
    this.inflight.clear();
  }

  /**
   * @deprecated Flag definitions are no longer fetched or evaluated locally
   * (there is no SDK-facing list endpoint). Kept for API compatibility as a
   * no-op that resolves immediately; use `clearCache()` to force re-fetching.
   */
  async refreshFlags(): Promise<void> {
    // no-op
  }

  /** Number of flag evaluations currently cached in memory. */
  get flagCount(): number {
    return this.flagCache.size;
  }

  /** Number of experiment assignments currently cached in memory. */
  get experimentCount(): number {
    return this.assignmentCache.size;
  }

  // ---------------------------------------------------------------------------
  // Internal helpers
  // ---------------------------------------------------------------------------

  /** Build the wire bodies for one event: a single keyed body, or the fan-out list. */
  private expand(event: TrackEvent): TrackBody[] {
    const base: TrackBody = {
      event_type: event.eventType ?? event.eventName,
      event_name: event.eventName,
      user_id: event.userId,
    };
    if (event.value !== undefined) base.value = event.value;
    if (event.properties !== undefined) base.metadata = event.properties;
    if (event.timestamp !== undefined) base.timestamp = toIso(event.timestamp);

    if (event.experimentKey || event.featureFlagKey) {
      const body: TrackBody = { ...base };
      if (event.experimentKey) body.experiment_key = event.experimentKey;
      if (event.featureFlagKey) body.feature_flag_key = event.featureFlagKey;
      return [body];
    }

    return [
      ...this.getAssignments(event.userId).map((a) => ({ ...base, experiment_key: a.experimentKey })),
      ...this.getEvaluatedFlags(event.userId).map((flagKey) => ({ ...base, feature_flag_key: flagKey })),
    ];
  }

  /** Share a single pending promise between concurrent calls for the same key. */
  private dedupe<T>(key: string, run: () => Promise<T>): Promise<T> {
    const pending = this.inflight.get(key) as Promise<T> | undefined;
    if (pending) return pending;
    const promise = run().then(
      (value) => {
        this.inflight.delete(key);
        return value;
      },
      (err) => {
        this.inflight.delete(key);
        throw err;
      },
    );
    this.inflight.set(key, promise);
    return promise;
  }

  /** Read a JSON value from the shared store (best-effort, never throws). */
  private async storeGet<T>(key: string, validate: (value: unknown) => value is T): Promise<T | null> {
    if (!this.store) return null;
    try {
      const raw = await this.store.get(key);
      if (!raw) return null;
      const parsed: unknown = JSON.parse(raw);
      return validate(parsed) ? parsed : null;
    } catch {
      return null;
    }
  }

  /** Write a JSON value to the shared store (best-effort, never throws). */
  private async storePut(key: string, value: unknown): Promise<void> {
    if (!this.store) return;
    try {
      await this.store.put(key, JSON.stringify(value), this.cacheTtlMs);
    } catch {
      // best-effort
    }
  }

  private async request(path: string, init: RequestInitLite = {}): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);
    try {
      // Call fetch unbound: binding it to the client breaks some runtimes ("Illegal invocation").
      const fetchImpl = this.fetchImpl ?? globalThis.fetch;
      if (typeof fetchImpl !== 'function') {
        throw new Error('No fetch implementation available; pass config.fetch');
      }
      return await fetchImpl(`${this.baseUrl}${path}`, {
        method: init.method ?? 'GET',
        headers: {
          'X-API-Key': this.apiKey,
          'Content-Type': 'application/json',
          Accept: 'application/json',
        },
        body: init.body === undefined ? undefined : JSON.stringify(init.body),
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
  }

  /** Perform a request and throw on a non-2xx status; the body is ignored. */
  private async requestOk(path: string, init: RequestInitLite = {}): Promise<void> {
    const response = await this.request(path, init);
    if (!response.ok) throw new EdgeApiError(response.status, `API error ${response.status} (${path})`);
  }

  private async requestJson<T>(path: string, init: RequestInitLite = {}): Promise<T> {
    const response = await this.request(path, init);
    if (!response.ok) throw new EdgeApiError(response.status, `API error ${response.status} (${path})`);
    return (await response.json()) as T;
  }
}

// ---------------------------------------------------------------------------
// Cache key helpers
// ---------------------------------------------------------------------------

function flagKey_(userId: string, flagKey: string): string {
  return `${FLAG_PREFIX}${encode(userId)}:${encode(flagKey)}`;
}

function assignKey(userId: string, experimentKey: string): string {
  return `${ASSIGN_PREFIX}${encode(userId)}:${encode(experimentKey)}`;
}

function isFlagEvaluation(value: unknown): value is FlagEvaluation {
  return (
    !!value &&
    typeof value === 'object' &&
    typeof (value as FlagEvaluation).key === 'string' &&
    typeof (value as FlagEvaluation).enabled === 'boolean'
  );
}

function isAssignment(value: unknown): value is Assignment {
  return (
    !!value &&
    typeof value === 'object' &&
    typeof (value as Assignment).experimentKey === 'string' &&
    typeof (value as Assignment).variantName === 'string'
  );
}
