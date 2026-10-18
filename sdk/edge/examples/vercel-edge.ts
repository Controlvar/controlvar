/**
 * Vercel Edge Functions / Edge Middleware example.
 *
 * This file shows two patterns:
 *  1. Middleware pattern: evaluate flags on the server and inject them as
 *     request headers (use this in middleware.ts at the root of your Next.js project)
 *  2. Edge Function pattern: direct client usage in an API route
 *
 * Vercel Edge Middleware (middleware.ts):
 * ```ts
 * export { middleware } from './examples/vercel-edge';
 * export const config = { matcher: ['/((?!_next/static|favicon.ico).*)'] };
 * ```
 */

import { createEdgeMiddleware } from '../src/adapters/vercel';
import { EdgeExperimentationClient } from '../src/client';

// ---------------------------------------------------------------------------
// Pattern 1: Middleware (inject flag values as request headers)
// ---------------------------------------------------------------------------

/**
 * Vercel Edge Middleware that evaluates feature flags for the request's user
 * (from the `X-User-Id` header or `user_id` cookie) and injects the results
 * as request headers for downstream pages and API routes.
 *
 * Each flag becomes a header: X-EP-Flag-{flagKey}: "true" | "false"
 *
 * In your Next.js page:
 * ```ts
 * export default function Page({ headers }) {
 *   const newCheckout = headers['x-ep-flag-new-checkout'] === 'true';
 *   return newCheckout ? <NewCheckout /> : <OldCheckout />;
 * }
 * ```
 */
export const middleware = createEdgeMiddleware({
  apiKey: process.env.EP_API_KEY ?? '',
  baseUrl: process.env.EP_BASE_URL ?? 'https://api.example.com',
  flagKeys: ['new-checkout', 'dark-mode', 'beta-search'], // omit to inject every flag the server reports
  userIdCookieName: 'user_id',
  userIdHeaderName: 'X-User-Id',
  cacheTtlMs: 30_000, // 30 seconds — Vercel Edge has short-lived instances
  timeout: 500,
});

// ---------------------------------------------------------------------------
// Pattern 2: Direct client usage in an Edge API route
// ---------------------------------------------------------------------------

/**
 * Example Vercel Edge Function that directly uses the Edge SDK.
 * Place this in pages/api/feature-check.ts with:
 *   export const config = { runtime: 'edge' };
 */
export async function featureCheckHandler(request: Request): Promise<Response> {
  const url = new URL(request.url);
  const userId = url.searchParams.get('userId') ?? 'anonymous';
  const flagKey = url.searchParams.get('flag') ?? '';

  if (!flagKey) {
    return new Response(JSON.stringify({ error: 'Missing ?flag= parameter' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const client = new EdgeExperimentationClient({
    apiKey: process.env.EP_API_KEY ?? '',
    baseUrl: process.env.EP_BASE_URL ?? 'https://api.example.com',
    cacheTtlMs: 30_000,
    timeout: 500,
  });

  // Server-decided evaluation; `config` carries the flag's payload (if any).
  const { enabled, config } = await client.evaluateFlag(flagKey, userId);

  // Sticky experiment assignment with targeting attributes as `context`.
  const assignment = await client.getAssignment('checkout_flow', userId, {
    country: request.headers.get('x-vercel-ip-country') ?? 'unknown',
    city: request.headers.get('x-vercel-ip-city') ?? 'unknown',
  });

  return new Response(
    JSON.stringify({ flagKey, userId, enabled, config, variant: assignment?.variantName ?? null }),
    {
      status: 200,
      headers: {
        'Content-Type': 'application/json',
        'Cache-Control': 'no-store',
      },
    },
  );
}

// Vercel Edge runtime config
export const config = { runtime: 'edge' };
