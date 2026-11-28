/**
 * Types for the Controlvar OpenFeature Provider.
 *
 * The provider delegates every network call to `@controlvar/js-sdk`;
 * the flag shape it works with is that SDK's `FlagEvaluation`
 * (`{key, enabled, config, reason, value, valueType}` from
 * `GET /api/v1/feature-flags/evaluate/{key}?user_id=…&context=…`).
 */

import type { ExperimentationClient } from '@controlvar/js-sdk';

/** Options both forms of {@link ExperimentationProviderOptions} take. */
interface ExperimentationProviderBaseOptions {
  /** Backend origin (the SDK appends `/api/v1/...`). Defaults to http://localhost:8000. */
  baseUrl?: string;
  /**
   * How long a successful evaluation is reused per user + flag, in ms. Defaults to 300 000 (5 min).
   * The attributes are not part of the cache key: a user's changed attribute is seen once the
   * answer expires. 0 caches nothing.
   */
  cacheTtlMs?: number;
  /** HTTP request timeout in milliseconds. Defaults to 5 000 (5 s). */
  timeout?: number;
  /**
   * Optional fetch implementation. Allows injection of a mock fetch in tests.
   * Defaults to the global `fetch` available in Node 18+ / browsers.
   */
  fetch?: typeof fetch;
}

/**
 * Either an `apiKey`, or a `client` to reuse: `{ client }` alone is valid and `apiKey` is
 * then ignored.
 */
export type ExperimentationProviderOptions = ExperimentationProviderBaseOptions &
  (
    | {
        /** API key used in the X-API-Key header. */
        apiKey: string;
        /**
         * Reuse an existing JS SDK client (for example the one your app already uses for
         * experiment assignment and tracking). When given, the other options are ignored.
         */
        client?: ExperimentationClient;
      }
    | {
        /** Not needed with a `client`: the client carries its own key. */
        apiKey?: string;
        /**
         * Reuse an existing JS SDK client (for example the one your app already uses for
         * experiment assignment and tracking). When given, the other options are ignored.
         */
        client: ExperimentationClient;
      }
  );

/** `flagMetadata` attached to every successful resolution. */
export interface ExperimentationFlagMetadata {
  flagKey: string;
  enabled: boolean;
  [key: string]: string | number | boolean;
}

/** Resolution reasons this provider emits (a subset of OpenFeature's `StandardResolutionReasons`). */
export type EvalReason = 'TARGETING_MATCH' | 'SPLIT' | 'CACHED' | 'DISABLED' | 'UNKNOWN' | 'ERROR';
