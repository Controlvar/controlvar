/**
 * Complete Cloudflare Worker example using the Edge SDK.
 *
 * This example demonstrates:
 *  1. Server-decided flag evaluation and experiment assignment from a worker
 *  2. Sharing cached results across worker instances via Cloudflare KV
 *  3. Zero-latency re-reads within the same request via the sync accessors
 *  4. Event tracking (fire-and-forget, run in ctx.waitUntil)
 *
 * Deploy with: wrangler deploy
 *
 * wrangler.toml:
 * ```toml
 * name = "my-worker"
 * main = "examples/cloudflare-worker.ts"
 * compatibility_date = "2024-01-01"
 *
 * [[kv_namespaces]]
 * binding = "KV"
 * id = "your-kv-namespace-id"
 *
 * [vars]
 * EP_API_KEY = "your-api-key"
 * EP_BASE_URL = "https://api.example.com"
 * ```
 */

import { CloudflareExperimentationClient } from '../src/adapters/cloudflare';
import type { KVNamespace, ExecutionContext } from '../src/adapters/cloudflare';

interface Env {
  KV?: KVNamespace;
  EP_API_KEY: string;
  EP_BASE_URL: string;
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const client = new CloudflareExperimentationClient({
      apiKey: env.EP_API_KEY,
      baseUrl: env.EP_BASE_URL,
      kvNamespace: env.KV,   // optional: share results across instances
      cacheTtlMs: 60_000,
      timeout: 500,
    });

    const userId = getUserId(request) ?? 'anonymous';
    const url = new URL(request.url);

    // Ask the server once (parallel) — results are cached per user + key.
    const [checkout, newCheckout, darkMode] = await Promise.all([
      client.getAssignment('checkout_flow', userId, {
        country: request.headers.get('CF-IPCountry') ?? 'unknown',
      }),
      client.evaluateFlag('new-checkout', userId),
      client.evaluateFlag('dark-mode', userId),
    ]);

    if (url.pathname === '/checkout' && newCheckout.enabled) {
      // Track the exposure without blocking the response
      ctx.waitUntil(
        client.track('checkout_viewed', userId, { path: url.pathname }, { featureFlagKey: 'new-checkout' }),
      );

      return new Response(
        JSON.stringify({
          checkout: 'new',
          variant: checkout?.variantName ?? 'control',
          configuration: checkout?.configuration ?? null,
          darkMode: darkMode.enabled,
        }),
        {
          headers: {
            'Content-Type': 'application/json',
            'X-EP-User-Id': userId,
            'X-EP-Flag-new-checkout': 'true',
          },
        },
      );
    }

    // A keyless event fans out to every cached assignment + flag for this user.
    ctx.waitUntil(client.track('page_view', userId, { path: url.pathname }));

    return new Response(
      JSON.stringify({
        checkout: 'original',
        variant: checkout?.variantName ?? 'control',
        flags: {
          // Sync reads are free once the flags above have been evaluated
          'new-checkout': client.evaluateFlagSync('new-checkout', userId),
          'dark-mode': client.evaluateFlagSync('dark-mode', userId),
        },
      }),
      { headers: { 'Content-Type': 'application/json' } },
    );
  },
};

function getUserId(request: Request): string | null {
  // Try to get userId from cookie
  const cookieHeader = request.headers.get('Cookie') ?? '';
  for (const part of cookieHeader.split(';')) {
    const [name, ...rest] = part.trim().split('=');
    if (name.trim() === 'user_id' && rest.length > 0) {
      return decodeURIComponent(rest.join('='));
    }
  }
  // Fall back to a header
  return request.headers.get('X-User-Id');
}
