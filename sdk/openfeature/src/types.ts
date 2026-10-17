/**
 * Types for the Controlvar OpenFeature Provider.
 *
 * The provider delegates every network call to `@controlvar/js-sdk`;
 * the flag shape it works with is that SDK's `FlagEvaluation`
 * (`{key, enabled, config}` from `GET /api/v1/feature-flags/evaluate/{key}?user_id=…`).
 */

import type { ExperimentationClient } from '@controlvar/js-sdk';

export interface ExperimentationProviderOptions {
  /** API key used in the X-API-Key header. */
  apiKey: string;
  /** Backend origin (the SDK appends `/api/v1/...`). Defaults to http://localhost:8000. */
  baseUrl?: string;
  /** How long a successful evaluation is reused per user + flag, in ms. Defaults to 300 000 (5 min). */
  cacheTtlMs?: number;
  /** HTTP request timeout in milliseconds. Defaults to 5 000 (5 s). */
  timeout?: number;
  /**
   * Optional fetch implementation. Allows injection of a mock fetch in tests.
   * Defaults to the global `fetch` available in Node 18+ / browsers.
   */
  fetch?: typeof fetch;
  /**
   * Reuse an existing JS SDK client (for example the one your app already uses for
   * experiment assignment and tracking). When given, the other options are ignored.
   */
  client?: ExperimentationClient;
}

/** `flagMetadata` attached to every successful resolution. */
export interface ExperimentationFlagMetadata {
  flagKey: string;
  enabled: boolean;
  [key: string]: string | number | boolean;
}

/** Resolution reasons this provider emits (a subset of OpenFeature's `StandardResolutionReasons`). */
export type EvalReason = 'TARGETING_MATCH' | 'CACHED' | 'DISABLED' | 'DEFAULT' | 'ERROR';
