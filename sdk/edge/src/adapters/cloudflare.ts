/**
 * Cloudflare Workers adapter for the Edge Experimentation SDK.
 *
 * Features:
 *  - KV namespace integration: server results (flag evaluations and
 *    experiment assignments) are cached in Cloudflare KV **per user + key**
 *    so that other worker instances can reuse them without a network call
 *  - `withExperimentation` HOF: wraps a Cloudflare fetch handler and injects
 *    a pre-initialised client into the handler via the `env` object
 *
 * KV layout:
 *   Key:   "ep:flag:{userId}:{flagKey}" / "ep:assign:{userId}:{experimentKey}"
 *   Value: JSON-serialised FlagEvaluation / Assignment
 *   TTL:   cacheTtlMs (rounded up to Cloudflare's 60 s minimum)
 *
 * Usage:
 *   export default withExperimentation(handler, {
 *     apiKey: env.EP_API_KEY,
 *     baseUrl: env.EP_BASE_URL,
 *     kvNamespace: env.EP_FLAGS_KV,
 *   });
 */

import { EdgeExperimentationClient } from '../client.js';
import type { EdgeSdkConfig, EdgeStore } from '../types.js';

/** Cloudflare KV rejects `expirationTtl` values below 60 seconds. */
const KV_MIN_TTL_SECONDS = 60;
/** Namespace prefix for every key this SDK writes to KV. */
const KV_PREFIX = 'ep:';

// ---------------------------------------------------------------------------
// KVNamespace interface (matches the Cloudflare Workers type)
// ---------------------------------------------------------------------------

export interface KVNamespace {
  get(key: string, options?: { type?: string }): Promise<string | null>;
  put(key: string, value: string, options?: { expirationTtl?: number }): Promise<void>;
}

// ---------------------------------------------------------------------------
// ExecutionContext interface (minimal — matches Cloudflare Workers type)
// ---------------------------------------------------------------------------

export interface ExecutionContext {
  waitUntil(promise: Promise<unknown>): void;
  passThroughOnException(): void;
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface CloudflareConfig extends EdgeSdkConfig {
  /** Cloudflare KV namespace binding for sharing cached results across instances. */
  kvNamespace?: KVNamespace;
  /**
   * TTL in seconds for KV entries. Defaults to `cacheTtlMs / 1000`, never below
   * Cloudflare's 60 s minimum.
   */
  kvTtlSeconds?: number;
}

// ---------------------------------------------------------------------------
// KV-backed store
// ---------------------------------------------------------------------------

/** `EdgeStore` over a Cloudflare KV namespace. */
export class CloudflareKvStore implements EdgeStore {
  constructor(
    private readonly kv: KVNamespace,
    private readonly ttlSeconds?: number,
  ) {}

  get(key: string): Promise<string | null> {
    return this.kv.get(KV_PREFIX + key);
  }

  put(key: string, value: string, ttlMs: number): Promise<void> {
    const seconds = this.ttlSeconds ?? Math.ceil(ttlMs / 1000);
    return this.kv.put(KV_PREFIX + key, value, { expirationTtl: Math.max(KV_MIN_TTL_SECONDS, seconds) });
  }
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

export class CloudflareExperimentationClient extends EdgeExperimentationClient {
  constructor(config: CloudflareConfig) {
    const store =
      config.store ??
      (config.kvNamespace ? new CloudflareKvStore(config.kvNamespace, config.kvTtlSeconds) : undefined);
    super({ ...config, store });
  }

  /**
   * @deprecated There is no bootstrap payload any more: results are cached in
   * KV per user + key as they are fetched. No-op kept for API compatibility.
   */
  async loadFromKvOrApi(): Promise<void> {
    // no-op
  }

  /**
   * @deprecated Flag definitions are no longer fetched. No-op kept for API
   * compatibility; each `evaluateFlag` / `getAssignment` writes its result to KV.
   */
  async refreshAndStore(): Promise<void> {
    // no-op
  }
}

// ---------------------------------------------------------------------------
// Convenience wrapper
// ---------------------------------------------------------------------------

/**
 * Wrap a Cloudflare fetch handler with a ready-to-use client.
 *
 * The wrapped handler receives the client as `env.EP_CLIENT` (or the key
 * specified by `clientEnvKey`). When `config.kvNamespace` is not set, the
 * binding `env.KV` is used if present.
 *
 * @example
 * ```ts
 * export default withExperimentation(
 *   async (request, env, ctx) => {
 *     const client = env.EP_CLIENT as CloudflareExperimentationClient;
 *     const { enabled } = await client.evaluateFlag('new-checkout', userId);
 *     ctx.waitUntil(client.track('page_view', userId, { path: '/' }));
 *     ...
 *   },
 *   { apiKey: 'key', baseUrl: 'https://api.example.com', kvNamespace: env.FLAGS_KV }
 * );
 * ```
 */
export function withExperimentation(
  handler: (request: Request, env: Record<string, unknown>, ctx: ExecutionContext) => Promise<Response>,
  config: CloudflareConfig,
  clientEnvKey = 'EP_CLIENT',
): (request: Request, env: Record<string, unknown>, ctx: ExecutionContext) => Promise<Response> {
  return async function wrappedHandler(
    request: Request,
    env: Record<string, unknown>,
    ctx: ExecutionContext,
  ): Promise<Response> {
    const client = new CloudflareExperimentationClient({
      ...config,
      kvNamespace: config.kvNamespace ?? (env['KV'] as KVNamespace | undefined),
    });

    // Inject client into env for the handler
    const enrichedEnv = { ...env, [clientEnvKey]: client };

    return handler(request, enrichedEnv, ctx);
  };
}
