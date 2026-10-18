/**
 * Deno Deploy example using the Edge SDK.
 *
 * Deno Deploy runs V8 isolates globally. This example shows:
 *  1. One client per isolate (createDenoHandler) so the in-memory cache is reused
 *  2. Sharing cached server results across isolates via Deno KV
 *  3. Server-decided flag evaluation and experiment assignment
 *  4. Fire-and-forget tracking
 *
 * Deploy with: deployctl deploy --project=my-project deno-edge.ts
 */

import { createDenoHandler, DenoExperimentationClient } from '../src/adapters/deno';

// ---------------------------------------------------------------------------
// Main handler using createDenoHandler helper
// ---------------------------------------------------------------------------

/**
 * The inner handler receives the isolate's shared client and the original request.
 */
export default {
  fetch: createDenoHandler(
    async (request: Request, client: DenoExperimentationClient): Promise<Response> => {
      const url = new URL(request.url);
      const userId = request.headers.get('X-User-Id') ?? url.searchParams.get('userId') ?? 'anon';

      if (url.pathname === '/health') {
        return new Response(
          JSON.stringify({ status: 'ok', cachedFlags: client.flagCount, cachedAssignments: client.experimentCount }),
          { headers: { 'Content-Type': 'application/json' } },
        );
      }

      // Server-decided; cached per user + key in memory and in Deno KV.
      const [newSearch, betaFeatures] = await Promise.all([
        client.evaluateFlag('new-search', userId),
        client.evaluateFlag('beta-features', userId),
      ]);

      // Keyless events fan out to every cached assignment + flag for this user.
      client.track('page_view', userId, { path: url.pathname }).catch(() => {}); // fire-and-forget

      return new Response(
        JSON.stringify({
          userId,
          flags: {
            'new-search': newSearch.enabled,
            'beta-features': betaFeatures.enabled,
          },
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        },
      );
    },
    {
      apiKey: Deno.env.get('EP_API_KEY') ?? '',
      baseUrl: Deno.env.get('EP_BASE_URL') ?? 'https://api.example.com',
      // kv: await Deno.openKv(), // Uncomment to share cached results across isolates
      cacheTtlMs: 60_000,
      timeout: 500,
    },
  ),
};

// ---------------------------------------------------------------------------
// Alternative: Manual pattern for more control
// ---------------------------------------------------------------------------

/**
 * Manual Deno Deploy handler for use cases requiring custom KV or logic.
 * Demonstrates how to use the client directly with Deno.openKv().
 */
export async function manualHandler(request: Request): Promise<Response> {
  // Open Deno KV (persistent, shared across isolates in the same region)
  // const kv = await Deno.openKv(); // Uncomment in real Deno environment

  const client = new DenoExperimentationClient({
    apiKey: Deno.env.get('EP_API_KEY') ?? '',
    baseUrl: Deno.env.get('EP_BASE_URL') ?? 'https://api.example.com',
    // kv, // Uncomment to enable Deno KV caching
    kvTtlMs: 300_000, // 5 minutes
  });

  const userId = request.headers.get('X-User-Id') ?? 'anonymous';
  const assignment = await client.getAssignment('checkout_flow', userId);
  const { enabled } = await client.evaluateFlag('my-feature', userId);

  // Sync reads are free once the server has answered in this isolate
  const stillEnabled = client.evaluateFlagSync('my-feature', userId);

  return new Response(
    JSON.stringify({ enabled, stillEnabled, variant: assignment?.variantName ?? 'control' }),
    { headers: { 'Content-Type': 'application/json' } },
  );
}
