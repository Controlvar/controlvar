/**
 * React Native Controlvar client.
 *
 * Flag evaluation and experiment assignment are decided **by the server**
 * (`GET /api/v1/feature-flags/evaluate/{key}?user_id=…`,
 * `POST /api/v1/tracking/assign`); nothing is bucketed locally. Successful
 * results are cached in memory per user + key (configurable TTL) and, when
 * `offlineFallback` is on, persisted to AsyncStorage so they survive app
 * restarts and can be served as last-known values when the API is unreachable.
 *
 * None of the public methods throw: `evaluateFlag` resolves to a disabled
 * evaluation, `getAssignment` to `null` and the typed getters
 * (`getStringValue`, `getNumberValue`, `getJsonValue`) to your default on
 * failure (failures are never cached); `track` is fire-and-forget. Swallowed
 * failures are reported to `config.onError`.
 *
 * @example
 * ```typescript
 * const client = new ExperimentationClient({
 *   apiKey: 'your-api-key',
 *   baseUrl: 'https://api.controlvar.example.com',
 * });
 *
 * const { enabled, config } = await client.evaluateFlag('dark-mode', 'user-123');
 * const assignment = await client.getAssignment('checkout-experiment', 'user-123', { plan: 'pro' });
 * await client.track('purchase', 'user-123', { sku: 'A1' }, { value: 49.99, experimentKey: 'checkout-experiment' });
 * ```
 */

import type {
  Assignment,
  AssignResponse,
  BatchResponse,
  BatchResult,
  Exposure,
  FlagEvaluateResponse,
  FlagEvaluation,
  SdkConfig,
  SwallowedOperation,
  TrackBody,
  TrackEvent,
  TrackOptions,
} from './types';
import { EvaluationCache } from './cache';
import { callExposureHook, checkOnExposure, exposureError } from './exposure';
import { OfflineStorage } from './storage';

const DEFAULT_TIMEOUT_MS = 5_000;
const DEFAULT_CACHE_TTL_MS = 300_000; // 5 minutes
/** Maximum events per `POST /api/v1/tracking/batch` request. */
const BATCH_LIMIT = 100;

/** Error raised internally for non-2xx responses; carries the HTTP status. */
export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
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

/**
 * `&context=<url-encoded JSON>` for a flag query string, or `''` when the
 * attributes are missing or an empty object (nothing is sent then). The same
 * rule as the JavaScript SDK's `contextQuery`.
 */
function contextQuery(attributes: Record<string, unknown> | undefined): string {
  if (!attributes || typeof attributes !== 'object' || Object.keys(attributes).length === 0) return '';
  return `&context=${encode(JSON.stringify(attributes))}`;
}

/**
 * A request path without its query string, as a request error message names
 * it: the query holds the user's id and attributes.
 */
function withoutQuery(path: string): string {
  const question = path.indexOf('?');
  return question === -1 ? path : path.slice(0, question);
}

function toIso(timestamp: Date | string): string {
  return timestamp instanceof Date ? timestamp.toISOString() : String(timestamp);
}

function disabled(flagKey: string): FlagEvaluation {
  return { key: flagKey, enabled: false, config: null };
}

/** The value types the typed getters and `useFlagValue` read. A boolean flag is read with `evaluateFlag`. */
export type FlagValueType = 'string' | 'number' | 'json';

/**
 * Why a typed read did not answer with a value the server served:
 *
 * - `TYPE_MISMATCH`: the flag serves another type than the one asked for.
 * - `INVALID_RESPONSE`: the answer holds no value of that type: a server without flag values, one
 *   that cannot serve the flag's stored value, a value that is not of the type the answer names,
 *   or an answer stored by 0.1 of this SDK.
 * - `REQUEST_FAILED`: `useFlagValue`'s `error` when the request failed (a network error, a
 *   timeout, or a non-2xx status: a flag key the server does not know is a 404); `cause` is the
 *   request's error. The typed getters pass that error itself to `onError`.
 */
export type FlagValueErrorCode = 'TYPE_MISMATCH' | 'INVALID_RESPONSE' | 'REQUEST_FAILED';

/** Why a typed getter or `useFlagValue` did not answer with a value the server served. */
export class FlagValueError extends Error {
  readonly code: FlagValueErrorCode;
  /** The flag that was read. */
  readonly flagKey: string;
  /** For `REQUEST_FAILED`, the request's error; otherwise `undefined`. */
  readonly cause: unknown;

  constructor(message: string, code: FlagValueErrorCode, flagKey: string, cause?: unknown) {
    super(message);
    this.name = 'FlagValueError';
    this.code = code;
    this.flagKey = flagKey;
    this.cause = cause;
  }
}

/** The largest integer a JSON number carries exactly in every SDK: 2^53 - 1. */
const MAX_SAFE_INTEGER = 9007199254740991;

/**
 * Whether `value` is a value of `type` as the server serves one: a string; a number that is
 * finite and within ±(2^53−1), never a boolean; a JSON object or array.
 */
function isValueOfType(value: unknown, type: FlagValueType): boolean {
  switch (type) {
    case 'string':
      return typeof value === 'string';
    case 'number':
      return typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= MAX_SAFE_INTEGER;
    case 'json':
      return value !== null && typeof value === 'object';
    default:
      return false;
  }
}

/** The JSON type of a value the server sent without a `value_type`. */
function jsonTypeOf(value: unknown): string {
  return value !== null && typeof value === 'object' ? 'json' : typeof value;
}

/** A value type as the error messages name it. */
function describeType(type: string): string {
  switch (type) {
    case 'boolean':
      return 'a boolean';
    case 'string':
      return 'a string';
    case 'number':
      return 'a number';
    case 'json':
      return 'a JSON value';
    default:
      return `a value of type ${JSON.stringify(type.slice(0, 64))}`;
  }
}

/**
 * A copy of a JSON value that shares nothing with it: an object or array is copied deeply by a
 * JSON round trip (it came from JSON, so nothing is lost); anything else is returned as it is.
 */
function copyValue<T>(value: T): T {
  return value !== null && typeof value === 'object' ? (JSON.parse(JSON.stringify(value)) as T) : value;
}

/**
 * The value `evaluation` holds for a reader of `type`, read as every Controlvar SDK reads one
 * (`tests/sdk-contract/value-vectors.json`): no value -> `INVALID_RESPONSE`; the served type
 * (`valueType`, else the value's JSON type) is not `type` -> `TYPE_MISMATCH`; the value is not
 * one of `type` -> `INVALID_RESPONSE`; otherwise the value, a JSON object or array as a new copy.
 * Exported for `useFlagValue` (not from the package root).
 */
export function readValueOf(
  evaluation: FlagEvaluation,
  type: FlagValueType,
  flagKey: string,
  reader: string
): FlagValueRead {
  const { value, valueType } = evaluation;
  const flag = JSON.stringify(String(flagKey));
  if (value === undefined || value === null) {
    return notFound(
      new FlagValueError(
        `Flag ${flag} came back without a value (from a server without flag values, or one that ` +
          `cannot serve this flag's stored value); ${reader} returned your default`,
        'INVALID_RESPONSE',
        flagKey
      )
    );
  }
  const served = typeof valueType === 'string' ? valueType : jsonTypeOf(value);
  if (served !== type) {
    return notFound(
      new FlagValueError(
        `Flag ${flag} serves ${describeType(served)}; ${reader} returned your default`,
        'TYPE_MISMATCH',
        flagKey
      )
    );
  }
  if (!isValueOfType(value, type)) {
    return notFound(
      new FlagValueError(
        `Flag ${flag} serves ${describeType(type)}, but the server's value is not one; ` +
          `${reader} returned your default`,
        'INVALID_RESPONSE',
        flagKey
      )
    );
  }
  return { found: true, value: copyValue(value), error: null };
}

function notFound(error: FlagValueError): FlagValueRead {
  return { found: false, value: undefined, error };
}

/**
 * The `REQUEST_FAILED` error of a typed read whose request failed with `cause`. Exported for
 * `useFlagValue` (not from the package root).
 */
export function requestFailed(flagKey: string, cause: Error): FlagValueError {
  const flag = JSON.stringify(String(flagKey));
  return new FlagValueError(`The request for flag ${flag} failed (${cause.message})`, 'REQUEST_FAILED', flagKey, cause);
}

/** What a typed read of a flag gives: the value it serves, or why there is none. */
export interface FlagValueRead {
  /** `true` when `value` is a value the server served (or the offline fallback stored), of the type asked for. */
  readonly found: boolean;
  /** The value when `found` (a JSON value is a new copy); `undefined` otherwise. */
  readonly value: unknown;
  /** Why the read is not a value the server served just now; `null` when it is. */
  readonly error: FlagValueError | null;
}

/**
 * The method `useFlagValue` reads through: the typed getters' body without the default. Keyed by a
 * symbol the package root does not export, so it is not part of the client's API.
 */
export const readFlagValue: unique symbol = Symbol('controlvar.readFlagValue');

/**
 * One flag answer and, when it is a fallback, the failure behind it. A request shared by several
 * calls gives each the same answer; `evaluateFlag` reports the failure once per request (the
 * first call to see it sets `reported`), each typed read once per call.
 */
interface FlagAnswer {
  evaluation: FlagEvaluation;
  error: Error | null;
  reported: boolean;
}

/** `err` as an `Error`: itself, or an `Error` whose message is `String(err)`. */
export function asError(err: unknown): Error {
  return err instanceof Error ? err : new Error(String(err));
}

export class ExperimentationClient {
  private readonly apiKey: string;
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly cacheTtlMs: number;
  private readonly offlineFallback: boolean;
  private readonly onError: SdkConfig['onError'];
  private readonly onExposure: ((exposure: Exposure) => unknown) | null;

  private readonly flagCache: EvaluationCache<FlagEvaluation>;
  private readonly assignmentCache: EvaluationCache<Assignment>;
  private readonly storage: OfflineStorage;
  /** Requests currently in flight, so concurrent callers share one fetch. */
  private readonly inflight = new Map<string, Promise<unknown>>();

  constructor(config: SdkConfig, storage?: OfflineStorage) {
    if (!config.apiKey) throw new Error('ExperimentationClient: apiKey is required');
    if (!config.baseUrl) throw new Error('ExperimentationClient: baseUrl is required');

    this.apiKey = config.apiKey;
    this.baseUrl = config.baseUrl.replace(/\/+$/, '');
    this.timeoutMs = config.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.cacheTtlMs = config.cacheTtlMs ?? DEFAULT_CACHE_TTL_MS;
    this.offlineFallback = config.offlineFallback ?? true;
    this.onError = config.onError;
    this.onExposure = checkOnExposure(config.onExposure);

    this.flagCache = new EvaluationCache<FlagEvaluation>(this.cacheTtlMs);
    this.assignmentCache = new EvaluationCache<Assignment>(this.cacheTtlMs);
    this.storage = storage ?? new OfflineStorage();
  }

  // ---------------------------------------------------------------------------
  // Feature flags
  // ---------------------------------------------------------------------------

  /**
   * Evaluates `flagKey` for `userId` via `GET /api/v1/feature-flags/evaluate/{key}?user_id=…&context=…`.
   * `attributes` is sent as `context` (URL-encoded JSON, nothing for `{}`), so targeting
   * rules match. Cache, AsyncStorage and in-flight keys stay user + flag, as in the
   * JavaScript SDK, so after the attributes change call `clearCache()` and
   * `await clearStorage()`, then remount the components that read the flag; with a client
   * constructed with `offlineFallback: false`, `clearCache()` plus a remount is enough.
   * A context the server refuses (422) or that cannot be serialised goes to `onError`,
   * and nothing is cached.
   *
   * Evaluation order:
   *   1. In-memory cache (fast path).
   *   2. AsyncStorage entry that has not expired (if `offlineFallback`).
   *   3. Server; the result is cached in memory and AsyncStorage.
   *   4. On failure: the last-known AsyncStorage value, even if expired (if `offlineFallback`).
   *      A 404 (the flag was deleted) is not an outage: the stored value is removed and
   *      the flag is answered disabled.
   *   5. `{ key, enabled: false, config: null }` as the safe default (not cached).
   */
  async evaluateFlag(
    flagKey: string,
    userId: string,
    attributes?: Record<string, unknown>
  ): Promise<FlagEvaluation> {
    const answer = await this.flagAnswer(flagKey, userId, attributes);
    if (answer.error !== null && !answer.reported) {
      answer.reported = true;
      this.report(answer.error, 'evaluateFlag');
    }
    return answer.evaluation;
  }

  /**
   * `evaluateFlag`'s body: the evaluation, and the failure behind it when it is a fallback,
   * reported by the caller (see {@link FlagAnswer}).
   */
  private async flagAnswer(
    flagKey: string,
    userId: string,
    attributes: Record<string, unknown> | undefined
  ): Promise<FlagAnswer> {
    const cached = this.flagCache.get(userId, flagKey);
    if (cached) return { evaluation: cached, error: null, reported: false };

    return this.dedupe(`flag ${userId} ${flagKey}`, async (): Promise<FlagAnswer> => {
      const stored = this.offlineFallback ? await this.storage.getFlag(userId, flagKey) : null;
      if (stored && Date.now() <= stored.expiresAt) {
        this.flagCache.set(userId, flagKey, stored.value);
        return { evaluation: stored.value, error: null, reported: false };
      }

      try {
        const data = await this.requestJson<FlagEvaluateResponse>(
          `/api/v1/feature-flags/evaluate/${encode(flagKey)}?user_id=${encode(userId)}${contextQuery(attributes)}`
        );
        const evaluation: FlagEvaluation = {
          key: flagKey,
          enabled: Boolean(data?.enabled),
          config: data?.config === undefined ? null : data.config,
        };
        // Only when the server sent them: a server without flag values sends neither.
        if (data?.value !== undefined && data.value !== null) evaluation.value = data.value;
        if (typeof data?.value_type === 'string') evaluation.valueType = data.value_type;
        this.flagCache.set(userId, flagKey, evaluation);
        if (this.offlineFallback) await this.storage.setFlag(userId, flagKey, evaluation, this.cacheTtlMs);
        return { evaluation, error: null, reported: false };
      } catch (err) {
        const error = asError(err);
        // A 404 is a definitive answer (the flag was deleted), not an outage: drop the
        // stored answer so it is never served again, and answer disabled.
        if (err instanceof ApiError && err.status === 404) {
          if (this.offlineFallback) await this.storage.removeFlag(userId, flagKey);
          return { evaluation: disabled(flagKey), error, reported: false };
        }
        return { evaluation: stored ? stored.value : disabled(flagKey), error, reported: false };
      }
    });
  }

  /** `true` only when the server (or the offline fallback) says the flag is enabled; `false` on failure. */
  async isFeatureEnabled(
    flagKey: string,
    userId: string,
    attributes?: Record<string, unknown>
  ): Promise<boolean> {
    return (await this.evaluateFlag(flagKey, userId, attributes)).enabled;
  }

  /**
   * The string a string flag serves `userId`: its on value when the flag is on for them, its off
   * value otherwise. Evaluated as `evaluateFlag` does, with the same request, cache and
   * AsyncStorage entry: reading a value after `evaluateFlag` or `isFeatureEnabled` asks nothing
   * more. Reading a value records no exposure.
   *
   * Never rejects. Returns `defaultValue`, and calls `onError` once with `'value'`, when the
   * request fails (with the request's own error; a 404 for an unknown flag included), the server
   * sends no value (a server without flag values, or an answer stored by 0.1:
   * `FlagValueError` code `'INVALID_RESPONSE'`), or the flag serves another type (code
   * `'TYPE_MISMATCH'`). When the request fails and the offline fallback holds an answer with a
   * value of this type, that value is returned instead of `defaultValue`, and the request's error
   * still goes to `onError`, as `evaluateFlag` does.
   */
  async getStringValue(
    flagKey: string,
    userId: string,
    defaultValue: string,
    attributes?: Record<string, unknown>
  ): Promise<string> {
    return this.typedValue(flagKey, userId, defaultValue, 'string', attributes, 'getStringValue');
  }

  /**
   * The number a number flag serves `userId` (an integer or a decimal, as saved); see
   * `getStringValue` for when it returns `defaultValue` instead. Never rejects.
   */
  async getNumberValue(
    flagKey: string,
    userId: string,
    defaultValue: number,
    attributes?: Record<string, unknown>
  ): Promise<number> {
    return this.typedValue(flagKey, userId, defaultValue, 'number', attributes, 'getNumberValue');
  }

  /**
   * The JSON object or array a json flag serves `userId`, as a new copy on every call: changing
   * it changes nothing the client keeps. It is checked to be an object or an array, not to match
   * `T`. See `getStringValue` for when it returns `defaultValue` instead. Never rejects.
   */
  async getJsonValue<T = unknown>(
    flagKey: string,
    userId: string,
    defaultValue: T,
    attributes?: Record<string, unknown>
  ): Promise<T> {
    return this.typedValue(flagKey, userId, defaultValue, 'json', attributes, 'getJsonValue');
  }

  /** The typed getters' one body: the value read, or `defaultValue`. */
  private async typedValue<T>(
    flagKey: string,
    userId: string,
    defaultValue: T,
    type: FlagValueType,
    attributes: Record<string, unknown> | undefined,
    reader: string
  ): Promise<T> {
    const read = await this[readFlagValue](flagKey, userId, type, attributes, reader);
    return read.found ? (read.value as T) : defaultValue;
  }

  /**
   * What the typed getters and `useFlagValue` read: the value `flagKey` serves `userId` when it
   * is of `type`, or why not. Calls `onError` once with `'value'` when the request failed (its
   * own error) or the read found no value (a `FlagValueError`). Never rejects.
   */
  async [readFlagValue](
    flagKey: string,
    userId: string,
    type: FlagValueType,
    attributes: Record<string, unknown> | undefined,
    reader: string
  ): Promise<FlagValueRead> {
    let read: FlagValueRead;
    let failure: Error | null = null;
    try {
      const answer = await this.flagAnswer(flagKey, userId, attributes);
      failure = answer.error;
      read = readValueOf(answer.evaluation, type, flagKey, reader);
    } catch (err) {
      // `flagAnswer` does not reject; this keeps a getter from rejecting if it ever did.
      failure = asError(err);
      read = { found: false, value: undefined, error: null };
    }
    if (failure !== null) {
      this.report(failure, 'value');
      return { found: read.found, value: read.value, error: requestFailed(flagKey, failure) };
    }
    if (read.error !== null) this.report(read.error, 'value');
    return read;
  }

  /**
   * All flags for a user via `GET /api/v1/feature-flags/user/{user_id}` → `{flagKey: enabled}`.
   * `attributes` is sent as `?context=` the same way as in `evaluateFlag`.
   * Not cached and not part of the tracking fan-out (it carries no `config`). `{}` on failure.
   */
  async getAllFlags(userId: string, attributes?: Record<string, unknown>): Promise<Record<string, boolean>> {
    try {
      const query = contextQuery(attributes).replace('&', '?');
      const data = await this.requestJson<Record<string, unknown>>(
        `/api/v1/feature-flags/user/${encode(userId)}${query}`
      );
      const flags: Record<string, boolean> = {};
      if (data && typeof data === 'object') {
        for (const [key, value] of Object.entries(data)) flags[key] = Boolean(value);
      }
      return flags;
    } catch (err) {
      this.report(err, 'getAllFlags');
      return {};
    }
  }

  // ---------------------------------------------------------------------------
  // Experiments
  // ---------------------------------------------------------------------------

  /**
   * Assigns `userId` to `experimentKey` via `POST /api/v1/tracking/assign`
   * (sticky on the server, records the exposure). `attributes` is sent as `context`.
   *
   * Same order as `evaluateFlag`; returns `null` when no experiment has the key
   * or a draft has no control variant (404), or the server cannot be reached and
   * nothing is persisted. An experiment that is not ACTIVE answers its control
   * variant.
   *
   * When the server answers `assigned: true` and no answer was cached for the
   * user and experiment, `onExposure` is called before this resolves. An answer
   * from the memory cache or from AsyncStorage (unexpired, or the offline
   * fallback) calls nothing.
   */
  async getAssignment(
    experimentKey: string,
    userId: string,
    attributes?: Record<string, unknown>
  ): Promise<Assignment | null> {
    const cached = this.assignmentCache.get(userId, experimentKey);
    if (cached) return cached;

    return this.dedupe(`assign ${userId} ${experimentKey}`, async () => {
      const stored = this.offlineFallback ? await this.storage.getAssignment(userId, experimentKey) : null;
      if (stored && Date.now() <= stored.expiresAt) {
        // Restored from AsyncStorage: no request, so no exposure, and no onExposure.
        this.assignmentCache.set(userId, experimentKey, stored.value);
        return stored.value;
      }

      let assignment: Assignment;
      let exposed: boolean;
      try {
        const body: Record<string, unknown> = { experiment_key: experimentKey, user_id: userId };
        if (attributes !== undefined) body.context = attributes;
        const data = await this.requestJson<AssignResponse>('/api/v1/tracking/assign', { method: 'POST', body });
        if (!data || typeof data.variant_name !== 'string') {
          throw new Error('Assignment response is missing variant_name');
        }
        assignment = {
          experimentKey: typeof data.experiment_key === 'string' ? data.experiment_key : experimentKey,
          userId,
          variantId: data.variant_id ?? null,
          variantName: data.variant_name,
          isControl: Boolean(data.is_control),
          configuration: data.configuration ?? null,
        };
        // Whether this store fills an empty or expired slot, read just before it. `clearCache()`
        // also drops the in-flight requests, so a call made after it while this request is in
        // flight makes a second request: the answer stored first calls onExposure, and the other
        // finds the slot filled.
        const fresh = this.assignmentCache.get(userId, experimentKey) === undefined;
        this.assignmentCache.set(userId, experimentKey, assignment);
        if (this.offlineFallback) {
          await this.storage.setAssignment(userId, experimentKey, assignment, this.cacheTtlMs);
        }
        // The server recorded an exposure for this request only if it said `assigned: true`
        // (a missing field is not taken as true).
        exposed = fresh && data.assigned === true;
      } catch (err) {
        this.report(err, 'getAssignment');
        return stored ? stored.value : null;
      }
      // Last, once the answer is stored in memory and AsyncStorage, and outside the catch above,
      // so a failing hook can never turn the answer into a request failure.
      if (exposed) this.exposed(assignment);
      return assignment;
    });
  }

  /** The assigned variant name, or `null` when assignment fails. */
  async getVariant(
    experimentKey: string,
    userId: string,
    attributes?: Record<string, unknown>
  ): Promise<string | null> {
    return (await this.getAssignment(experimentKey, userId, attributes))?.variantName ?? null;
  }

  // ---------------------------------------------------------------------------
  // Tracking
  // ---------------------------------------------------------------------------

  /**
   * Sends a tracking event. Fire-and-forget: this method never throws.
   *
   * - With `options.experimentKey` / `options.featureFlagKey`: one
   *   `POST /api/v1/tracking/track`.
   * - Without keys: one `POST /api/v1/tracking/batch` (chunked at 100) with
   *   one entry per cached assignment and one per cached evaluated flag for
   *   this user. Nothing cached → nothing is sent.
   */
  async track(
    eventName: string,
    userId: string,
    properties?: Record<string, unknown>,
    options: TrackOptions = {}
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
    } catch (err) {
      this.report(err, 'track');
    }
  }

  /**
   * Sends many events via `POST /api/v1/tracking/batch`, chunked at 100.
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
          status: err instanceof ApiError ? err.status : undefined,
        });
        this.report(err, 'trackBatch');
      }
    }
    return result;
  }

  // ---------------------------------------------------------------------------
  // Cache access
  // ---------------------------------------------------------------------------

  /** Cached (successful, unexpired) assignments for the user, in assignment order. */
  getAssignments(userId: string): Assignment[] {
    return this.assignmentCache.entries(userId).map(([, assignment]) => assignment);
  }

  /** Keys of flags successfully evaluated (and still cached in memory) for the user. */
  getEvaluatedFlags(userId: string): string[] {
    return this.flagCache.entries(userId).map(([flagKey]) => flagKey);
  }

  /** Clears the in-memory caches (AsyncStorage is left untouched; see `clearStorage`). */
  clearCache(): void {
    this.flagCache.clear();
    this.assignmentCache.clear();
    this.inflight.clear();
  }

  /** Removes every SDK entry from AsyncStorage. */
  async clearStorage(): Promise<void> {
    await this.storage.clear();
  }

  // ---------------------------------------------------------------------------
  // Private helpers
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

  /** Give `onExposure` a new object holding the four values; its failures go to `onError`. */
  private exposed(assignment: Assignment): void {
    const hook = this.onExposure;
    if (!hook) return;
    const exposure: Exposure = {
      experimentKey: assignment.experimentKey,
      userId: assignment.userId,
      variantId: assignment.variantId,
      variantName: assignment.variantName,
    };
    callExposureHook(hook, exposure, (thrown) => this.report(exposureError(thrown), 'exposure'));
  }

  private report(err: unknown, operation: SwallowedOperation): void {
    if (!this.onError) return;
    try {
      this.onError(asError(err), operation);
    } catch {
      // a throwing error handler must not break fire-and-forget semantics
    }
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
      }
    );
    this.inflight.set(key, promise);
    return promise;
  }

  private async request(path: string, init: RequestInitLite = {}): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      return await fetch(`${this.baseUrl}${path}`, {
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
    if (!response.ok) throw new ApiError(response.status, `API error ${response.status} (${withoutQuery(path)})`);
  }

  private async requestJson<T>(path: string, init: RequestInitLite = {}): Promise<T> {
    const response = await this.request(path, init);
    if (!response.ok) throw new ApiError(response.status, `API error ${response.status} (${withoutQuery(path)})`);
    return (await response.json()) as T;
  }
}
