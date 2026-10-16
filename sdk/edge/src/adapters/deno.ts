/**
 * Deno Deploy adapter for the Edge Experimentation SDK.
 *
 * Deno Deploy runs Deno-compatible JavaScript/TypeScript at the edge.
 * This adapter provides:
 *  - A `DenoExperimentationClient` that extends the base client
 *  - A `createDenoHandler` helper that creates one client per isolate
 *  - Deno KV-backed sharing of cached server results **per user + key**
 *
 * Deno KV layout:
 *   Key:   ["ep", "flag:{userId}:{flagKey}"] / ["ep", "assign:{userId}:{experimentKey}"]
 *   Value: JSON string of the FlagEvaluation / Assignment
 *   TTL:   kvTtlMs (defaults to cacheTtlMs)
 *
 * Usage:
 * ```ts
 * import { createDenoHandler } from '@controlvar/edge-sdk/deno';
 *
 * export default createDenoHandler(
 *   async (req, client) => {
 *     const userId = req.headers.get('X-User-Id') ?? 'anon';
 *     const { enabled } = await client.evaluateFlag('new-feature', userId);
 *     return new Response(enabled ? 'new' : 'old');
 *   },
 *   { apiKey: Deno.env.get('EP_API_KEY')!, kv: await Deno.openKv() }
 * );
 * ```
 */

import { EdgeExperimentationClient } from '../client.js';
import type { EdgeSdkConfig, EdgeStore } from '../types.js';

// ---------------------------------------------------------------------------
// Deno KV interface (minimal, compatible with Deno's built-in Deno.openKv)
// ---------------------------------------------------------------------------

export interface DenoKv {
  get<T>(key: string[]): Promise<{ value: T | null }>;
  set(key: string[], value: unknown, options?: { expireIn?: number }): Promise<unknown>;
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface DenoConfig extends EdgeSdkConfig {
  /**
   * Deno KV instance. If provided, server results are cached in KV per
   * user + key and shared across isolates.
   */
  kv?: DenoKv;
  /**
   * TTL for KV entries in milliseconds (Deno KV uses ms).
   * Defaults to `cacheTtlMs`.
   */
  kvTtlMs?: number;
}

// ---------------------------------------------------------------------------
// KV-backed store
// ---------------------------------------------------------------------------

const KV_NAMESPACE = 'ep';

/** `EdgeStore` over a Deno KV instance. Values are stored as JSON strings. */
export class DenoKvStore implements EdgeStore {
  constructor(
    private readonly kv: DenoKv,
    private readonly ttlMs?: number,
  ) {}

  async get(key: string): Promise<string | null> {
    const entry = await this.kv.get<string>([KV_NAMESPACE, key]);
    return typeof entry.value === 'string' ? entry.value : null;
  }

  async put(key: string, value: string, ttlMs: number): Promise<void> {
    await this.kv.set([KV_NAMESPACE, key], value, { expireIn: this.ttlMs ?? ttlMs });
  }
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

export class DenoExperimentationClient extends EdgeExperimentationClient {
  constructor(config: DenoConfig) {
    const store = config.store ?? (config.kv ? new DenoKvStore(config.kv, config.kvTtlMs) : undefined);
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
// Convenience factory for Deno.serve()
// ---------------------------------------------------------------------------

export type DenoHandler = (
  request: Request,
  client: DenoExperimentationClient,
) => Promise<Response>;

/**
 * Create a Deno.serve-compatible handler with a shared client.
 *
 * The client is created once per isolate so its in-memory cache is reused
 * across requests; the inner handler receives it with the original request.
 */
export function createDenoHandler(
  handler: DenoHandler,
  config: DenoConfig,
): (request: Request) => Promise<Response> {
  let client: DenoExperimentationClient | null = null;

  return async function denoHandler(request: Request): Promise<Response> {
    if (!client) client = new DenoExperimentationClient(config);
    return handler(request, client);
  };
}
