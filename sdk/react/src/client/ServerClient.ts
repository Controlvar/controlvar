import { ExperimentationClient, defaultAssignment, disabledEvaluation } from './ExperimentationClient';
import { SdkConfig, UserContext, FeatureFlagEvaluation, ExperimentAssignment } from './types';

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
 * `assigned: true` answer it stores), and never for a Provider's. The
 * constructor throws `onExposure must be a function` for any other value.
 */
export class ServerClient {
  private readonly inner: ExperimentationClient;

  constructor(config: SdkConfig) {
    this.inner = new ExperimentationClient(config);
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

  clearCache(): void {
    this.inner.clearCache();
  }
}

function toError(err: unknown): Error {
  return err instanceof Error ? err : new Error(String(err));
}
