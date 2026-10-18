/**
 * @controlvar/edge-sdk
 *
 * Edge-compatible SDK for Cloudflare Workers, Vercel Edge Functions,
 * Deno Deploy, and any WinterCG-compatible JavaScript runtime.
 *
 * Key design constraints:
 *  - Zero Node.js built-ins (no `fs`, `crypto`, `Buffer`, `process`)
 *  - Pure-JS MD5 for the cross-SDK hash utility
 *  - Works in Cloudflare Workers, Vercel Edge, Deno Deploy, Node >= 18 and browsers
 *
 * Flag evaluation and experiment assignment are decided by the server
 * (`GET /api/v1/feature-flags/evaluate/{key}?user_id=…`,
 * `POST /api/v1/tracking/assign`); results are cached per user + key in
 * memory and optionally in a shared KV store.
 *
 * Quick start:
 * ```ts
 * import { EdgeExperimentationClient } from '@controlvar/edge-sdk';
 *
 * const client = new EdgeExperimentationClient({ apiKey: 'your-api-key', baseUrl: 'https://api.example.com' });
 *
 * const { enabled, config } = await client.evaluateFlag('new-checkout', userId);
 * const assignment = await client.getAssignment('checkout_flow', userId); // null on failure
 * await client.track('purchase', userId, { total: 49.99 }, { value: 49.99, experimentKey: 'checkout_flow' });
 *
 * // Sync — zero latency, serves the in-memory cache populated above
 * const enabledAgain = client.evaluateFlagSync('new-checkout', userId);
 * ```
 */

// Main client
export { EdgeExperimentationClient, EdgeApiError } from './client.js';

// Utilities
export { hashUser } from './hash.js';
export { md5, md5Hex } from './md5.js';
export { EdgeCache } from './cache.js';

// Types
export type {
  EdgeSdkConfig,
  EdgeStore,
  FlagEvaluation,
  Assignment,
  TrackOptions,
  TrackEvent,
  BatchResult,
  AssignResponse,
  FlagEvaluateResponse,
  BatchResponse,
  TrackBody,
  CacheEntry,
} from './types.js';
