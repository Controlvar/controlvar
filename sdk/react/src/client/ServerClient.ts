import {
  ExperimentationClient,
  RequestSettings,
  apiRequest,
  assignBody,
  assignmentFromResponse,
  defaultAssignment,
  disabledEvaluation,
  evaluatePath,
  exposureOf,
  recordedExposure,
  requestSettings,
} from './ExperimentationClient';
import { isAssignResponse, isFlagResponse } from './bootstrap';
import { callExposureHook, checkOnExposure, dropExposureError } from './exposure';
import {
  SdkConfig,
  UserContext,
  FeatureFlagEvaluation,
  ExperimentAssignment,
  ExperimentationBootstrap,
  ExperimentAssignResponse,
  FeatureFlagEvaluateResponse,
  Exposure,
} from './types';

/**
 * SSR/Node.js client for evaluating flags and assigning experiments server-side.
 *
 * Unlike ExperimentationClient, this class:
 * - Never throws — failures are returned as a disabled evaluation / default
 *   assignment with the `error` field set, so SSR rendering is always safe
 * - Uses a per-instance in-memory cache (appropriate for request-scoped use);
 *   only successful results are cached
 * - Accepts (key, user) argument order to match server-side idioms
 * - Uses no browser-specific APIs (localStorage, document, window)
 *
 * `config.onExposure`, if given, is this client's own: it is called for the
 * assignments this client makes, as `ExperimentationClient` calls it (once per
 * `assigned: true` answer it stores, and once per `assigned: true` answer
 * `bootstrap()` gets), and never for a Provider's. The constructor throws
 * `onExposure must be a function` for any other value.
 */
export class ServerClient {
  private readonly inner: ExperimentationClient;
  private readonly settings: RequestSettings;
  private readonly onExposure: ((exposure: Exposure) => unknown) | null;

  constructor(config: SdkConfig) {
    this.inner = new ExperimentationClient(config);
    this.settings = requestSettings(config);
    this.onExposure = checkOnExposure(config.onExposure);
  }

  /**
   * `GET /api/v1/feature-flags/evaluate/{key}?user_id=…[&context=<url-encoded JSON>]`
   * — never throws. `user.attributes` (when non-empty) is sent as `context`.
   */
  async evaluateFeatureFlag(flagKey: string, user: UserContext): Promise<FeatureFlagEvaluation> {
    try {
      return await this.inner.evaluateFeatureFlagDetailed(user, flagKey);
    } catch (err) {
      return disabledEvaluation(flagKey, { error: toError(err) });
    }
  }

  /** `POST /api/v1/tracking/assign` — never throws; returns the control defaults on failure. */
  async assignExperiment(experimentKey: string, user: UserContext): Promise<ExperimentAssignment> {
    try {
      return await this.inner.assignExperiment(user, experimentKey);
    } catch (err) {
      return defaultAssignment(experimentKey, { error: toError(err) });
    }
  }

  /** Evaluate several flags in parallel. Returns a map of flagKey -> evaluation. */
  async getAll(flagKeys: string[], user: UserContext): Promise<Record<string, FeatureFlagEvaluation>> {
    if (flagKeys.length === 0) return {};
    const results = await Promise.all(flagKeys.map(key => this.evaluateFeatureFlag(key, user)));
    return Object.fromEntries(flagKeys.map((key, i) => [key, results[i]]));
  }

  /**
   * The answers for `user` to pass to `ExperimentationProvider` as `bootstrap`,
   * so the page's first render shows them with no request. Asks the evaluate
   * route for each flag key and the assign route for each experiment key, all
   * in parallel, and returns each response body as the API sent it, keyed by
   * the key asked for: plain JSON, safe to pass as a prop.
   *
   * Never throws. A key whose request fails (an HTTP error, a network error, a
   * timeout, a body that is not JSON or not a response of its route) is left
   * out. Reads and fills no cache: each call asks again. Each experiment key
   * is an assignment, so the server records an exposure for it, and this
   * client's `onExposure` is called for an `assigned: true` answer: name only
   * the experiments the page renders.
   */
  async bootstrap(
    keys: { flags?: string[]; experiments?: string[] },
    user: UserContext
  ): Promise<ExperimentationBootstrap> {
    let userId = '';
    let flags: Array<[string, FeatureFlagEvaluateResponse | null]> = [];
    let experiments: Array<[string, ExperimentAssignResponse | null]> = [];
    try {
      userId = user.userId;
      const flagKeys = distinctKeys(keys?.flags);
      const experimentKeys = distinctKeys(keys?.experiments);
      const [flagBodies, assignBodies] = await Promise.all([
        Promise.all(flagKeys.map(key => this.flagBody(user, key))),
        Promise.all(experimentKeys.map(key => this.assignBody(user, key))),
      ]);
      flags = flagKeys.map((key, i) => [key, flagBodies[i]]);
      experiments = experimentKeys.map((key, i) => [key, assignBodies[i]]);
    } catch {
      // never throws: a key with no answer is left out
    }
    // In the order asked for. Object.fromEntries defines own properties, so no
    // key (not even `__proto__`) reaches a prototype.
    return {
      userId: typeof userId === 'string' ? userId : '',
      flags: Object.fromEntries(answered(flags)),
      experiments: Object.fromEntries(answered(experiments)),
    };
  }

  clearCache(): void {
    this.inner.clearCache();
  }

  /** The evaluate route's body for `key`, or `null` when the request fails. No cache. */
  private async flagBody(user: UserContext, key: string): Promise<FeatureFlagEvaluateResponse | null> {
    try {
      const response = await apiRequest(this.settings, evaluatePath(user, key));
      if (!response.ok) return null;
      const body: unknown = await response.json();
      return isFlagResponse(body) ? body : null;
    } catch {
      return null;
    }
  }

  /**
   * The assign route's body for `key`, or `null` when the request fails. No
   * cache. Calls `onExposure` for an `assigned: true` answer, as
   * `assignExperiment` does.
   */
  private async assignBody(user: UserContext, key: string): Promise<ExperimentAssignResponse | null> {
    let body: unknown;
    try {
      const response = await apiRequest(this.settings, '/api/v1/tracking/assign', {
        method: 'POST',
        body: assignBody(user, key),
      });
      if (!response.ok) return null;
      body = await response.json();
    } catch {
      return null;
    }
    if (!isAssignResponse(body)) return null;
    if (this.onExposure && recordedExposure(body)) {
      callExposureHook(this.onExposure, exposureOf(user.userId, assignmentFromResponse(key, body)), dropExposureError);
    }
    return body;
  }
}

/** The entries that have a body. */
function answered<T>(entries: Array<[string, T | null]>): Array<[string, T]> {
  return entries.filter((entry): entry is [string, T] => entry[1] !== null);
}

/** The string keys of `keys`, each once, in order; nothing for anything that is not an array. */
function distinctKeys(keys: unknown): string[] {
  if (!Array.isArray(keys)) return [];
  return Array.from(new Set(keys.filter((key): key is string => typeof key === 'string')));
}

function toError(err: unknown): Error {
  return err instanceof Error ? err : new Error(String(err));
}
