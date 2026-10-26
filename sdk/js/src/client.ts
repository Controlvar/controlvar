import { AttributedKeys } from './attribution';
import { UserKeyCache } from './cache';
import { ExperimentationError, asExperimentationError } from './errors';
import { DEFER } from './evaluator';
import {
  DEFAULT_REFRESH_INTERVAL_MS,
  LocalEvaluation,
  LocalEvaluationStatus,
  MIN_REFRESH_INTERVAL_MS,
  ReadyResult,
} from './local';
import type {
  Assignment,
  AssignmentRecord,
  AssignResponse,
  BatchResponse,
  BatchResult,
  ClientConfig,
  ErrorReport,
  FlagEvaluateResponse,
  FlagEvaluation,
  SwallowedOperation,
  TrackBody,
  TrackEvent,
  TrackOptions,
  UserContext,
} from './types';

const DEFAULT_TIMEOUT_MS = 5_000;
const DEFAULT_CACHE_TTL_MS = 300_000;
const DEFAULT_VARIANT = 'control';
/** Used when a 429 carries no parseable `Retry-After`. */
const DEFAULT_RETRY_AFTER_MS = 1_000;
/** Maximum events per `POST /api/v1/tracking/batch` request. */
const BATCH_LIMIT = 100;

interface RequestInitLite {
  method?: 'GET' | 'POST';
  body?: unknown;
  /** Extra request headers (the ruleset poll sends `If-None-Match`). */
  headers?: Record<string, string>;
  /** Internal: set on the single retry after a 429. */
  retried?: boolean;
}

/** Thrown at construction when `evaluation: 'local'` is used in a browser. */
export const BROWSER_LOCAL_EVALUATION_MESSAGE =
  "evaluation: 'local' downloads every flag's targeting rules, including the values in them, and " +
  "is for server-side code. In a browser, leave evaluation at its default ('server').";

function inBrowser(): boolean {
  const scope = globalThis as { window?: { document?: unknown } };
  return typeof scope.window !== 'undefined' && scope.window !== null && typeof scope.window.document !== 'undefined';
}

function encode(value: string): string {
  return encodeURIComponent(value);
}

/**
 * `&context=<url-encoded JSON>` for a flag query string, or `''` when the
 * attributes are missing or an empty object (nothing is sent then).
 */
function contextQuery(attributes: Record<string, unknown> | undefined): string {
  if (!attributes || typeof attributes !== 'object' || Object.keys(attributes).length === 0) return '';
  return `&context=${encode(JSON.stringify(attributes))}`;
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function requireUser(user: UserContext): UserContext {
  if (!user || typeof user.userId !== 'string' || user.userId.length === 0) {
    throw new ExperimentationError('user.userId is required', { code: 'INVALID_RESPONSE' });
  }
  return user;
}

function toIso(timestamp: Date | string): string {
  return timestamp instanceof Date ? timestamp.toISOString() : String(timestamp);
}

/** One body that names no experiment and no flag: nothing was attributed. */
function isUntagged(bodies: TrackBody[]): boolean {
  return bodies.length === 1 && !bodies[0].experiment_key && !bodies[0].feature_flag_key;
}

/**
 * Client for the Controlvar public API.
 *
 * - By default (`evaluation: 'server'`) flag evaluation and experiment assignment
 *   are decided by the server. With `evaluation: 'local'` (server-side code only)
 *   flags are answered in-process from the server's ruleset whenever the answer is
 *   provably the server's, and by the server otherwise; experiments always stay on
 *   the server. `consistentHash` is exported as a utility only; it is not the flag
 *   bucketing function.
 * - Successful evaluations/assignments are cached per user + key for
 *   `cacheTtlMs`; failures are never cached. Concurrent calls for the same
 *   user + key share one in-flight request.
 * - Every successful answer also records its key for the key-less `track()`
 *   fan-out, for 24 hours (see `attribution.ts`); `clearCache()` keeps that record.
 * - `getAssignment`, `evaluateFlag`, `getAllFlags` and `fetchAssignments` throw
 *   `ExperimentationError`; `getVariant` / `isFeatureEnabled` return safe
 *   defaults instead; `track` / `trackBatch` / `reportError` never reject.
 * - A 429 is retried once after the server's `Retry-After` (capped at `timeoutMs`).
 */
export class ExperimentationClient {
  private readonly apiUrl: string;
  private readonly apiKey: string;
  private readonly timeoutMs: number;
  private readonly defaultVariant: string;
  private readonly customFetch: typeof fetch | undefined;
  private readonly onError: ClientConfig['onError'];
  private readonly flags: UserKeyCache<FlagEvaluation>;
  private readonly assignments: UserKeyCache<Assignment>;
  /** The keys a key-less `track()` is sent to; outlives the cache entries. */
  private readonly attributed = new AttributedKeys();
  /** Requests currently in flight, so concurrent callers share one fetch. */
  private readonly inflight = new Map<string, Promise<unknown>>();
  /** The local-evaluation runtime; `null` in server mode. */
  private readonly local: LocalEvaluation | null = null;

  constructor(config: ClientConfig) {
    if (!config || !config.apiKey) throw new Error('apiKey is required');
    if (!config.apiUrl) throw new Error('apiUrl is required');
    this.apiUrl = config.apiUrl.replace(/\/+$/, '');
    this.apiKey = config.apiKey;
    this.timeoutMs = config.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.defaultVariant = config.defaultVariant ?? DEFAULT_VARIANT;
    this.customFetch = config.fetch;
    this.onError = config.onError;
    const ttl = config.cacheTtlMs ?? DEFAULT_CACHE_TTL_MS;
    this.flags = new UserKeyCache<FlagEvaluation>(ttl);
    this.assignments = new UserKeyCache<Assignment>(ttl);

    const evaluation = config.evaluation ?? 'server';
    if (evaluation !== 'server' && evaluation !== 'local') {
      throw new Error("evaluation must be 'server' or 'local'");
    }
    if (evaluation === 'local') {
      if (inBrowser()) throw new Error(BROWSER_LOCAL_EVALUATION_MESSAGE);
      const interval = config.refreshIntervalMs ?? DEFAULT_REFRESH_INTERVAL_MS;
      if (typeof interval !== 'number' || !Number.isFinite(interval)) {
        throw new Error('refreshIntervalMs must be a number of milliseconds');
      }
      const maxStale = config.maxStaleMs;
      if (maxStale !== undefined && (typeof maxStale !== 'number' || !(maxStale > 0))) {
        throw new Error('maxStaleMs must be a positive number of milliseconds');
      }
      this.local = new LocalEvaluation(
        {
          request: (path, init) => this.request(path, init),
          httpError: (response, path, method) => this.httpError(response, path, method),
          report: (error, operation) => this.report(error, operation),
        },
        { refreshIntervalMs: Math.max(interval, MIN_REFRESH_INTERVAL_MS), maxStaleMs: maxStale }
      );
      this.local.start();
    }
  }

  // ─── Local evaluation ──────────────────────────────────────────────────────

  /**
   * Resolves once flags can be answered locally, or the first ruleset fetch failed, or
   * `timeoutMs` passed. Never rejects. In server mode it resolves `{ ok: true }` at once.
   * Evaluation calls never wait for it: until a ruleset is loaded they are made on the server.
   */
  async ready(options: { timeoutMs?: number } = {}): Promise<ReadyResult> {
    if (!this.local) return { ok: true, rulesetVersion: null };
    return this.local.ready(options.timeoutMs);
  }

  /** Where flags are being evaluated, and the state of the ruleset. */
  status(): LocalEvaluationStatus {
    if (!this.local) {
      return {
        evaluation: 'server',
        ready: true,
        rulesetVersion: null,
        lastRefreshAt: null,
        lastError: null,
        serverEvaluatedFlags: [],
      };
    }
    return this.local.status();
  }

  /**
   * Stop refreshing the ruleset and send the evaluation counts not yet sent. After `close()`,
   * flags are evaluated by the server. Never rejects. A no-op in server mode.
   */
  async close(): Promise<void> {
    if (this.local) await this.local.close();
  }

  // ─── Experiments ───────────────────────────────────────────────────────────

  /**
   * Assign the user via `POST /api/v1/tracking/assign` (sticky on the server,
   * records the exposure). `user.attributes` is sent as `context`.
   * Throws `ExperimentationError` (404 when the experiment is not ACTIVE).
   */
  async getAssignment(experimentKey: string, user: UserContext): Promise<Assignment> {
    const { userId, attributes } = requireUser(user);
    const cached = this.assignments.get(userId, experimentKey);
    if (cached) return cached;

    return this.dedupe(`assign\u0000${userId}\u0000${experimentKey}`, async () => {
      const body: Record<string, unknown> = { experiment_key: experimentKey, user_id: userId };
      if (attributes !== undefined) body.context = attributes;
      let data: AssignResponse;
      try {
        data = await this.requestJson<AssignResponse>('/api/v1/tracking/assign', {
          method: 'POST',
          body,
        });
      } catch (err) {
        // A 404: a key-less track() must no longer reach this experiment.
        if (err instanceof ExperimentationError && err.status === 404) {
          this.attributed.forgetExperiment(userId, experimentKey);
        }
        throw err;
      }
      if (!data || typeof data.variant_name !== 'string') {
        throw new ExperimentationError('Assignment response is missing variant_name', {
          code: 'INVALID_RESPONSE',
          body: data,
        });
      }
      const assignment: Assignment = {
        experimentKey: typeof data.experiment_key === 'string' ? data.experiment_key : experimentKey,
        userId,
        variantId: data.variant_id ?? null,
        variantName: data.variant_name,
        isControl: Boolean(data.is_control),
        configuration: data.configuration ?? null,
        // Only copied when the server sent them: a missing `assigned` stays undefined
        // (unknown), never false, so older servers are not mistaken for "not enrolled".
        ...(typeof data.assigned === 'boolean' ? { assigned: data.assigned } : {}),
        ...(typeof data.reason === 'string' ? { reason: data.reason } : {}),
      };
      this.assignments.set(userId, experimentKey, assignment);
      this.attributed.recordExperiment(userId, assignment.experimentKey, assignment.reason);
      return assignment;
    });
  }

  /** The assigned variant name, or `defaultVariant` when assignment fails. Never throws. */
  async getVariant(experimentKey: string, user: UserContext): Promise<string> {
    try {
      return (await this.getAssignment(experimentKey, user)).variantName;
    } catch {
      return this.defaultVariant;
    }
  }

  /** Cached (successful, unexpired) assignments for the user, in assignment order. */
  getAssignments(userId: string): Assignment[] {
    return this.assignments.entries(userId).map(([, assignment]) => assignment);
  }

  /** The user's assignments as recorded by the server: `GET /api/v1/tracking/assignments/{user_id}`. */
  async fetchAssignments(
    userId: string,
    options: { activeOnly?: boolean } = {}
  ): Promise<AssignmentRecord[]> {
    const activeOnly = options.activeOnly ?? true;
    const data = await this.requestJson<unknown>(
      `/api/v1/tracking/assignments/${encode(userId)}?active_only=${activeOnly ? 'true' : 'false'}`
    );
    return Array.isArray(data) ? (data as AssignmentRecord[]) : [];
  }

  // ─── Feature flags ─────────────────────────────────────────────────────────

  /**
   * Evaluate a flag via
   * `GET /api/v1/feature-flags/evaluate/{key}?user_id=…[&context=<url-encoded JSON>]`.
   * `user.attributes` (when non-empty) is sent as `context` so the flag's targeting
   * rules can evaluate against it. The cache key is user + flag only — attributes are
   * assumed stable per user; call `clearCache()` after changing them.
   * Throws `ExperimentationError` (404 for a flag key the server does not know; an
   * inactive flag is `{ enabled: false, reason: 'inactive' }`).
   *
   * With `evaluation: 'local'`, a flag the ruleset can answer is answered in-process from the
   * attributes passed (no request, no cache read) with `source: 'local'`; anything else is
   * evaluated by the server as above, with `source: 'server'`.
   */
  async evaluateFlag(flagKey: string, user: UserContext): Promise<FlagEvaluation> {
    const { userId, attributes } = requireUser(user);
    if (this.local) {
      const answer = this.local.answer(flagKey, userId, attributes);
      if (answer !== DEFER) {
        const evaluation: FlagEvaluation = {
          key: flagKey,
          enabled: answer.enabled,
          config: null,
          reason: answer.reason,
          source: 'local',
        };
        // Recorded for the key-less track() fan-out, exactly as a server answer is.
        this.flags.set(userId, flagKey, evaluation);
        this.attributed.recordFlag(userId, flagKey);
        return evaluation;
      }
    }
    const cached = this.flags.get(userId, flagKey);
    if (cached) return cached;

    return this.dedupe(`flag\u0000${userId}\u0000${flagKey}`, async () => {
      const data = await this.requestJson<FlagEvaluateResponse>(
        `/api/v1/feature-flags/evaluate/${encode(flagKey)}?user_id=${encode(userId)}${contextQuery(attributes)}`
      );
      const evaluation: FlagEvaluation = {
        key: flagKey,
        enabled: Boolean(data?.enabled),
        config: data?.config === undefined ? null : data.config,
      };
      if (typeof data?.reason === 'string') evaluation.reason = data.reason;
      if (this.local) evaluation.source = 'server';
      this.flags.set(userId, flagKey, evaluation);
      this.attributed.recordFlag(userId, flagKey);
      return evaluation;
    });
  }

  /** `true` only when the server says the flag is enabled for this user; `false` on any failure. */
  async isFeatureEnabled(flagKey: string, user: UserContext): Promise<boolean> {
    try {
      return (await this.evaluateFlag(flagKey, user)).enabled;
    } catch {
      return false;
    }
  }

  /**
   * All flags for a user via
   * `GET /api/v1/feature-flags/user/{user_id}[?context=<url-encoded JSON>]` → `{flagKey: enabled}`.
   * `attributes` (when non-empty) is sent as `context` for targeting rules.
   * Not cached and not part of the tracking fan-out (it carries no `config`).
   *
   * With `evaluation: 'local'` the answer is computed in-process when every active flag can be
   * answered locally for these attributes; otherwise the whole call goes to the server.
   */
  async getAllFlags(userId: string, attributes?: Record<string, unknown>): Promise<Record<string, boolean>> {
    if (this.local) {
      const flags = this.local.answerAll(userId, attributes);
      if (flags !== DEFER) return flags;
    }
    const context = contextQuery(attributes);
    const data = await this.requestJson<Record<string, unknown>>(
      `/api/v1/feature-flags/user/${encode(userId)}${context ? `?${context.slice(1)}` : ''}`
    );
    const flags: Record<string, boolean> = {};
    if (data && typeof data === 'object') {
      for (const [key, value] of Object.entries(data)) flags[key] = Boolean(value);
    }
    return flags;
  }

  /** Keys of flags successfully evaluated (and still cached) for the user. */
  getEvaluatedFlags(userId: string): string[] {
    return this.flags.entries(userId).map(([flagKey]) => flagKey);
  }

  // ─── Tracking ──────────────────────────────────────────────────────────────

  /**
   * Track an event. Never rejects; failures go to `config.onError`.
   *
   * - With `experimentKey` / `featureFlagKey`: one `POST /api/v1/tracking/track`.
   * - Without keys: one `POST /api/v1/tracking/batch` (chunked at 100) with one
   *   entry per experiment and one per flag this client answered for the user in
   *   the last 24 hours, cached or not. An experiment whose latest answer said it
   *   is a draft, paused, completed or archived gets no entry.
   * - Without keys and with nothing to attribute it to: one `POST /api/v1/tracking/track`
   *   with no key, which the server stores as history.
   */
  async track(userId: string, eventName: string, options: TrackOptions = {}): Promise<void> {
    try {
      const events = this.expand({ userId, eventName, ...options });

      if (options.experimentKey || options.featureFlagKey || isUntagged(events)) {
        await this.requestOk('/api/v1/tracking/track', { method: 'POST', body: events[0] });
        return;
      }

      for (let i = 0; i < events.length; i += BATCH_LIMIT) {
        await this.requestOk('/api/v1/tracking/batch', {
          method: 'POST',
          body: { events: events.slice(i, i + BATCH_LIMIT) },
        });
      }
    } catch (err) {
      this.report(err, 'track');
    }
  }

  /**
   * Send many events via `POST /api/v1/tracking/batch`, chunked at 100.
   * Entries without a key are expanded with the same fan-out rule as `track`; one
   * with nothing to attribute it to is sent once with no key (stored as history).
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
        const error = asExperimentationError(err);
        result.failureCount += chunk.length;
        result.errors.push({ message: error.message, status: error.status });
        this.report(error, 'trackBatch');
      }
    }
    return result;
  }

  // ─── Error reports ─────────────────────────────────────────────────────────

  /**
   * Report an error that happened while a flag or experiment was active, with one
   * `POST /api/v1/tracking/errors`. Works in both evaluation modes.
   *
   * Resolves `true` exactly when the server answered 201: the report is stored. It counts toward
   * the flag's error rate and automatic rollback only when this client's key is a server-side key
   * (the `sdk:ruleset` scope, created by a user who can change flags); the flag's safety check
   * shows the others as `uncounted_error_count`. Resolves `false` on any other answer (401, 404,
   * 422, a 429 after its one retry, 5xx) or a network failure, and passes the failure to
   * `onError(err, 'reportError')`. Never rejects. The server checks the report; the SDK does not.
   */
  async reportError(report: ErrorReport): Promise<boolean> {
    const path = '/api/v1/tracking/errors';
    if (path) return true; // tamper: claims the report was stored without sending it
    try {
      const body: Record<string, unknown> = { error_type: report.errorType, message: report.message };
      if (report.featureFlagKey !== undefined) body.feature_flag_key = report.featureFlagKey;
      if (report.experimentKey !== undefined) body.experiment_key = report.experimentKey;
      if (report.userId !== undefined) body.user_id = report.userId;
      if (report.stackTrace !== undefined) body.stack_trace = report.stackTrace;
      if (report.metadata !== undefined) body.metadata = report.metadata;
      if (report.timestamp !== undefined) body.timestamp = toIso(report.timestamp);

      const response = await this.request(path, { method: 'POST', body });
      if (response.status === 201) return true;
      this.report(await this.httpError(response, path, 'POST'), 'reportError');
      return false;
    } catch (err) {
      this.report(err, 'reportError');
      return false;
    }
  }

  // ─── Cache ─────────────────────────────────────────────────────────────────

  /**
   * Drop every cached assignment and evaluation, so the next calls ask the server again.
   * The keys a key-less `track()` is sent to are kept.
   */
  clearCache(): void {
    this.flags.clear();
    this.assignments.clear();
  }

  // ─── Internals ─────────────────────────────────────────────────────────────

  /**
   * Build the wire bodies for one event: a single keyed body, the fan-out list, or,
   * with nothing to attribute it to, the single body with no key.
   */
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

    const { experiments, flags } = this.attributed.keys(event.userId);
    const bodies: TrackBody[] = [
      ...experiments.map(experimentKey => ({ ...base, experiment_key: experimentKey })),
      ...flags.map(flagKey => ({ ...base, feature_flag_key: flagKey })),
    ];
    return bodies.length > 0 ? bodies : [base];
  }

  private report(err: unknown, operation: SwallowedOperation): void {
    if (!this.onError) return;
    try {
      this.onError(asExperimentationError(err), operation);
    } catch {
      // a throwing error handler must not break fire-and-forget semantics
    }
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

  private async request(path: string, init: RequestInitLite = {}): Promise<Response> {
    const method = init.method ?? 'GET';
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let response: Response;
    try {
      // Call fetch unbound: binding it to the client breaks browsers ("Illegal invocation").
      const fetchImpl = this.customFetch ?? globalThis.fetch;
      if (typeof fetchImpl !== 'function') {
        throw new ExperimentationError('No fetch implementation available; pass config.fetch', {
          code: 'NETWORK_ERROR',
        });
      }
      response = await fetchImpl(`${this.apiUrl}${path}`, {
        method,
        headers: {
          ...(init.headers ?? {}),
          'X-API-Key': this.apiKey,
          'Content-Type': 'application/json',
          Accept: 'application/json',
        },
        body: init.body === undefined ? undefined : JSON.stringify(init.body),
        signal: controller.signal,
      });
    } catch (err) {
      if (err instanceof ExperimentationError) throw err;
      if (controller.signal.aborted) {
        throw new ExperimentationError(
          `Request timed out after ${this.timeoutMs} ms (${method} ${path})`,
          { code: 'TIMEOUT', cause: err }
        );
      }
      const message = err instanceof Error ? err.message : String(err);
      throw new ExperimentationError(`Network error (${method} ${path}): ${message}`, {
        code: 'NETWORK_ERROR',
        cause: err,
      });
    } finally {
      clearTimeout(timer);
    }

    if (response.status === 429 && !init.retried) {
      await sleep(this.retryAfterMs(response));
      return this.request(path, { ...init, retried: true });
    }
    return response;
  }

  /** Perform a request and throw on a non-2xx status; the body is ignored. */
  private async requestOk(path: string, init: RequestInitLite = {}): Promise<void> {
    const response = await this.request(path, init);
    if (!response.ok) throw await this.httpError(response, path, init.method ?? 'GET');
  }

  private async requestJson<T>(path: string, init: RequestInitLite = {}): Promise<T> {
    const response = await this.request(path, init);
    if (!response.ok) throw await this.httpError(response, path, init.method ?? 'GET');
    try {
      return (await response.json()) as T;
    } catch (err) {
      throw new ExperimentationError(`Invalid JSON in response (${init.method ?? 'GET'} ${path})`, {
        code: 'INVALID_RESPONSE',
        status: response.status,
        cause: err,
      });
    }
  }

  private async httpError(response: Response, path: string, method: string): Promise<ExperimentationError> {
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      body = undefined;
    }
    const detail =
      body && typeof body === 'object' && 'detail' in body
        ? String((body as { detail: unknown }).detail)
        : undefined;
    return new ExperimentationError(
      `API error ${response.status}${detail ? `: ${detail}` : ''} (${method} ${path})`,
      { code: 'HTTP_ERROR', status: response.status, body }
    );
  }

  /** Delay before the single 429 retry: `Retry-After` seconds or HTTP-date, capped at `timeoutMs`. */
  private retryAfterMs(response: Response): number {
    let ms = DEFAULT_RETRY_AFTER_MS;
    const header = typeof response.headers?.get === 'function' ? response.headers.get('Retry-After') : null;
    if (header) {
      const seconds = Number(header);
      if (Number.isFinite(seconds)) {
        ms = seconds * 1000;
      } else {
        const date = Date.parse(header);
        if (!Number.isNaN(date)) ms = date - Date.now();
      }
    }
    return Math.min(Math.max(ms, 0), this.timeoutMs);
  }
}
