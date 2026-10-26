import {
  SdkConfig,
  UserContext,
  FeatureFlagEvaluation,
  ExperimentAssignment,
  TrackEventOptions,
  FeatureFlagEvaluateResponse,
  ExperimentAssignResponse,
} from './types';
import { AttributedKeys } from './attribution';

interface CacheEntry<T> {
  value: T;
  expiresAt: number;
}

/** Per-user, per-key TTL cache. Only successful results are ever stored. */
class UserKeyCache<T> {
  private readonly users = new Map<string, Map<string, CacheEntry<T>>>();

  constructor(private readonly ttlMs: number) {}

  get(userId: string, key: string): T | null {
    const entry = this.users.get(userId)?.get(key);
    if (!entry) return null;
    if (Date.now() > entry.expiresAt) {
      this.users.get(userId)?.delete(key);
      return null;
    }
    return entry.value;
  }

  set(userId: string, key: string, value: T): void {
    let byKey = this.users.get(userId);
    if (!byKey) {
      byKey = new Map();
      this.users.set(userId, byKey);
    }
    byKey.set(key, { value, expiresAt: Date.now() + this.ttlMs });
  }

  /** All live (non-expired) entries for a user, in insertion order. */
  entries(userId: string): Array<[string, T]> {
    const byKey = this.users.get(userId);
    if (!byKey) return [];
    const now = Date.now();
    const live: Array<[string, T]> = [];
    for (const [key, entry] of byKey) {
      if (now > entry.expiresAt) byKey.delete(key);
      else live.push([key, entry.value]);
    }
    return live;
  }

  clear(): void {
    this.users.clear();
  }
}

/** Maximum events per `POST /api/v1/tracking/batch` request. */
const BATCH_LIMIT = 100;

/** Body of a `/tracking/track` request (and of each `/tracking/batch` entry). */
interface TrackBody {
  event_type: string;
  event_name: string;
  user_id: string;
  experiment_key?: string;
  feature_flag_key?: string;
  value?: number;
  metadata?: Record<string, unknown>;
  timestamp?: string;
}

/**
 * `&context=<url-encoded JSON>` for the flag-evaluation query string, or `''`
 * when the user has no attributes (or an empty object) — nothing is sent then.
 */
export function contextQuery(attributes: UserContext['attributes']): string {
  if (!attributes || typeof attributes !== 'object' || Object.keys(attributes).length === 0) {
    return '';
  }
  return `&context=${encodeURIComponent(JSON.stringify(attributes))}`;
}

export function defaultAssignment(
  experimentKey: string,
  overrides: Partial<Pick<ExperimentAssignment, 'loading' | 'error'>> = {}
): ExperimentAssignment {
  return {
    experimentKey,
    variantKey: 'control',
    variantName: 'Control',
    variantId: null,
    isControl: true,
    configuration: null,
    loading: false,
    error: null,
    ...overrides,
  };
}

export function disabledEvaluation(
  flagKey: string,
  overrides: Partial<Pick<FeatureFlagEvaluation, 'loading' | 'error'>> = {}
): FeatureFlagEvaluation {
  return {
    flagKey,
    variant: null,
    isEnabled: false,
    config: null,
    loading: false,
    error: null,
    ...overrides,
  };
}

/**
 * Browser client for the Controlvar public API.
 *
 * - Flag evaluation and experiment assignment are decided by the server; no
 *   local bucketing happens here.
 * - Successful evaluations/assignments are cached per user + key for
 *   `cacheTtlMs`; failures are never cached.
 * - Every successful answer also records its key for the key-less `trackEvent`
 *   fan-out, for 24 hours (see `attribution.ts`); `clearCache()` keeps that record.
 * - `evaluateFeatureFlag*` and `assignExperiment` throw on failure (hooks
 *   convert that into `error` state). `trackEvent` never throws.
 */
export class ExperimentationClient {
  private readonly apiKey: string;
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly flags: UserKeyCache<FeatureFlagEvaluation>;
  private readonly assignments: UserKeyCache<ExperimentAssignment>;
  /** The keys a key-less `trackEvent` is sent to; outlives the cache entries. */
  private readonly attributed = new AttributedKeys();
  /** Requests currently in flight, so concurrent callers share one fetch. */
  private readonly inflight = new Map<string, Promise<unknown>>();

  constructor(config: SdkConfig) {
    if (!config.apiKey) throw new Error('apiKey is required');
    if (!config.baseUrl) throw new Error('baseUrl is required');
    this.apiKey = config.apiKey;
    this.baseUrl = config.baseUrl.replace(/\/$/, '');
    this.timeoutMs = config.timeoutMs ?? 5000;
    const ttl = config.cacheTtlMs ?? 300_000;
    this.flags = new UserKeyCache<FeatureFlagEvaluation>(ttl);
    this.assignments = new UserKeyCache<ExperimentAssignment>(ttl);
  }

  // ─── HTTP ──────────────────────────────────────────────────────────────────

  private async request(path: string, init: { method?: string; body?: unknown } = {}): Promise<Response> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      return await fetch(`${this.baseUrl}${path}`, {
        method: init.method ?? 'GET',
        headers: {
          'X-API-Key': this.apiKey,
          'Content-Type': 'application/json',
        },
        body: init.body === undefined ? undefined : JSON.stringify(init.body),
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeout);
    }
  }

  private async requestJson<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
    const response = await this.request(path, init);
    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
    return (await response.json()) as T;
  }

  /** Share a single pending promise between concurrent calls for the same key. */
  private dedupe<T>(key: string, run: () => Promise<T>): Promise<T> {
    const pending = this.inflight.get(key) as Promise<T> | undefined;
    if (pending) return pending;
    const promise = run().then(
      value => {
        this.inflight.delete(key);
        return value;
      },
      err => {
        this.inflight.delete(key);
        throw err;
      }
    );
    this.inflight.set(key, promise);
    return promise;
  }

  // ─── Feature flags ─────────────────────────────────────────────────────────

  /**
   * Evaluate a flag and return just the variant:
   * `null` when off, else `config.variant` when it is a string, else `'on'`
   * (the evaluate route returns `config: null` for every flag, so `'on'`).
   */
  async evaluateFeatureFlag(user: UserContext, flagKey: string): Promise<string | null> {
    const evaluation = await this.evaluateFeatureFlagDetailed(user, flagKey);
    return evaluation.variant;
  }

  /**
   * Evaluate a flag via
   * `GET /api/v1/feature-flags/evaluate/{key}?user_id=…[&context=<url-encoded JSON>]`.
   * Throws on failure.
   *
   * `user.attributes` (when non-empty) is sent as `context` so the flag's
   * targeting rules can evaluate against it. The cache key is user + flag only
   * — attributes are assumed stable per user; call `clearCache()` after
   * changing them.
   */
  async evaluateFeatureFlagDetailed(user: UserContext, flagKey: string): Promise<FeatureFlagEvaluation> {
    const cached = this.flags.get(user.userId, flagKey);
    if (cached) return cached;

    return this.dedupe(`flag\u0000${user.userId}\u0000${flagKey}`, async () => {
      const path =
        `/api/v1/feature-flags/evaluate/${encodeURIComponent(flagKey)}` +
        `?user_id=${encodeURIComponent(user.userId)}` +
        contextQuery(user.attributes);
      const data = await this.requestJson<FeatureFlagEvaluateResponse>(path);

      const enabled = Boolean(data.enabled);
      const config = data.config === undefined ? null : data.config;
      const configVariant = (config as { variant?: unknown } | null)?.variant;
      const evaluation: FeatureFlagEvaluation = {
        flagKey,
        variant: enabled ? (typeof configVariant === 'string' ? configVariant : 'on') : null,
        isEnabled: enabled,
        config,
        loading: false,
        error: null,
        ...(typeof data.reason === 'string' ? { reason: data.reason } : {}),
      };
      this.flags.set(user.userId, flagKey, evaluation);
      this.attributed.recordFlag(user.userId, flagKey);
      return evaluation;
    });
  }

  // ─── Experiments ───────────────────────────────────────────────────────────

  /** Assign the user via `POST /api/v1/tracking/assign`. Sticky server-side. Throws on failure. */
  async assignExperiment(user: UserContext, experimentKey: string): Promise<ExperimentAssignment> {
    const cached = this.assignments.get(user.userId, experimentKey);
    if (cached) return cached;

    return this.dedupe(`assign\u0000${user.userId}\u0000${experimentKey}`, async () => {
      const data = await this.requestJson<ExperimentAssignResponse>('/api/v1/tracking/assign', {
        method: 'POST',
        body: {
          experiment_key: experimentKey,
          user_id: user.userId,
          context: user.attributes,
        },
      });

      const assignment: ExperimentAssignment = {
        experimentKey: data.experiment_key ?? experimentKey,
        variantKey: data.variant_name,
        variantName: data.variant_name,
        variantId: data.variant_id ?? null,
        isControl: Boolean(data.is_control),
        configuration: data.configuration ?? null,
        loading: false,
        error: null,
        // Older servers do not send `assigned`: treat their response as a real assignment.
        assigned: typeof data.assigned === 'boolean' ? data.assigned : true,
        ...(typeof data.reason === 'string' ? { reason: data.reason } : {}),
      };
      this.assignments.set(user.userId, experimentKey, assignment);
      this.attributed.recordExperiment(user.userId, assignment.experimentKey, assignment.reason);
      return assignment;
    });
  }

  // ─── Tracking ──────────────────────────────────────────────────────────────

  /**
   * Track an event. Never throws.
   *
   * - With `options.experimentKey` / `options.featureFlagKey`: one
   *   `POST /api/v1/tracking/track`.
   * - Without keys: fan out via `POST /api/v1/tracking/batch` — one entry per
   *   experiment and one per flag this client answered for the user in the last
   *   24 hours, cached or not. An experiment whose latest answer said it is a
   *   draft, paused, completed or archived gets no entry.
   * - Without keys and with nothing to attribute it to: one
   *   `POST /api/v1/tracking/track` with no key, which the server stores as history.
   */
  async trackEvent(
    userId: string,
    eventName: string,
    properties?: Record<string, unknown>,
    options: TrackEventOptions = {}
  ): Promise<void> {
    try {
      const base: TrackBody = {
        event_type: options.eventType ?? eventName,
        event_name: eventName,
        user_id: userId,
        value: options.value,
        metadata: properties,
        timestamp: options.timestamp?.toISOString(),
      };

      if (options.experimentKey || options.featureFlagKey) {
        const body: TrackBody = { ...base };
        if (options.experimentKey) body.experiment_key = options.experimentKey;
        if (options.featureFlagKey) body.feature_flag_key = options.featureFlagKey;
        await this.request('/api/v1/tracking/track', { method: 'POST', body });
        return;
      }

      const experiments = this.getAssignments(userId).map(a => a.experimentKey);
      const flags = this.getEvaluatedFlags(userId);
      const events: TrackBody[] = [
        ...experiments.map(experimentKey => ({ ...base, experiment_key: experimentKey })),
        ...flags.map(flagKey => ({ ...base, feature_flag_key: flagKey })),
      ];
      if (events.length === 0) {
        // Nothing to attribute it to: sent once with no key, stored as history.
        await this.request('/api/v1/tracking/track', { method: 'POST', body: base });
        return;
      }

      for (let i = 0; i < events.length; i += BATCH_LIMIT) {
        await this.request('/api/v1/tracking/batch', {
          method: 'POST',
          body: { events: events.slice(i, i + BATCH_LIMIT) },
        });
      }
    } catch {
      // fire-and-forget: never throw
    }
  }

  // ─── Cache access ──────────────────────────────────────────────────────────

  /** Cached (successful, unexpired) assignments for the user. */
  getAssignments(userId: string): ExperimentAssignment[] {
    return this.assignments.entries(userId).map(([, assignment]) => assignment);
  }

  /** Keys of flags successfully evaluated (and still cached) for the user. */
  getEvaluatedFlags(userId: string): string[] {
    return this.flags.entries(userId).map(([flagKey]) => flagKey);
  }

  /**
   * Drop every cached assignment and evaluation, so the next calls ask the server again.
   * The keys a key-less `trackEvent` is sent to are kept.
   */
  clearCache(): void {
    this.flags.clear();
    this.assignments.clear();
  }
}
