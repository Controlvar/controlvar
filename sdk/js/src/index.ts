/**
 * @controlvar/js-sdk
 *
 * JavaScript/TypeScript client for the Controlvar public API.
 * Works in Node >= 18 and browsers; zero runtime dependencies (uses global `fetch`).
 *
 * @example
 * ```ts
 * import { ExperimentationClient } from '@controlvar/js-sdk';
 *
 * const client = new ExperimentationClient({ apiUrl: 'http://localhost:8000', apiKey: '...' });
 * const variant = await client.getVariant('checkout_flow', { userId: 'user-123' });
 * const enabled = await client.isFeatureEnabled('new_search', { userId: 'user-123' });
 * await client.track('user-123', 'purchase', { value: 49.99, experimentKey: 'checkout_flow' });
 * ```
 */

export { ExperimentationClient, BROWSER_LOCAL_EVALUATION_MESSAGE } from './client';
export type { ReadyResult, LocalEvaluationStatus } from './local';
export { ExperimentationError } from './errors';
export type { ExperimentationErrorCode } from './errors';
export { consistentHash, md5Hex, md5Bytes } from './hash';
export type {
  ClientConfig,
  UserContext,
  Assignment,
  FlagEvaluation,
  TrackOptions,
  TrackEvent,
  ErrorReport,
  BatchResult,
  AssignmentRecord,
  SwallowedOperation,
  EvaluationMode,
  AssignResponse,
  FlagEvaluateResponse,
  BatchResponse,
  TrackBody,
} from './types';
