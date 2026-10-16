/**
 * Vercel Edge Functions / Edge Middleware adapter.
 *
 * Pattern: the middleware evaluates feature flags **on the server** and
 * injects the results as request headers so that origin functions
 * (serverless or edge) can read them without an additional API call.
 *
 * Header convention:
 *   X-EP-Flag-{flagKey}: "true" | "false"
 *   X-EP-User-Id: the resolved user ID (from header or cookie)
 *
 * Usage (middleware.ts):
 * ```ts
 * import { createEdgeMiddleware } from '@controlvar/edge-sdk/vercel';
 *
 * export const middleware = createEdgeMiddleware({
 *   apiKey: process.env.EP_API_KEY!,
 *   baseUrl: process.env.EP_BASE_URL!,
 *   flagKeys: ['new-checkout', 'dark-mode'],
 * });
 *
 * export const config = { matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'] };
 * ```
 */

import { EdgeExperimentationClient } from '../client.js';
import type { EdgeSdkConfig } from '../types.js';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface VercelEdgeConfig extends EdgeSdkConfig {
  /**
   * Flag keys to evaluate (in parallel, via
   * `GET /api/v1/feature-flags/evaluate/{key}?user_id=…`) and inject as request
   * headers. If omitted, every flag the server reports for the user
   * (`GET /api/v1/feature-flags/user/{user_id}`) is injected.
   */
  flagKeys?: string[];
  /**
   * Cookie name to read the user ID from. Defaults to 'ep_user_id'.
   */
  userIdCookieName?: string;
  /**
   * Header name to read the user ID from (checked before the cookie).
   * Defaults to 'X-User-Id'.
   */
  userIdHeaderName?: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Extract the user ID from the request:
 *   1. Check the configurable header
 *   2. Fall back to the configurable cookie
 *   3. Fall back to an empty string (anonymous — no flags are evaluated)
 */
export function extractUserId(request: Request, config: VercelEdgeConfig): string {
  const headerName = config.userIdHeaderName ?? 'X-User-Id';
  const fromHeader = request.headers.get(headerName);
  if (fromHeader) return fromHeader;

  const cookieName = config.userIdCookieName ?? 'ep_user_id';
  const cookieHeader = request.headers.get('Cookie') ?? '';
  for (const part of cookieHeader.split(';')) {
    const [name, ...rest] = part.trim().split('=');
    if (name.trim() === cookieName && rest.length > 0) {
      return decodeURIComponent(rest.join('='));
    }
  }

  return '';
}

/**
 * Evaluate the configured flags for `userId` and return `{flagKey: enabled}`.
 * Failures count as disabled; an empty user id evaluates nothing.
 */
export async function evaluateFlagsForRequest(
  client: EdgeExperimentationClient,
  userId: string,
  flagKeys: string[] | undefined,
): Promise<Record<string, boolean>> {
  if (!userId) return {};
  if (flagKeys && flagKeys.length > 0) {
    const results = await Promise.all(flagKeys.map((key) => client.evaluateFlag(key, userId)));
    const flags: Record<string, boolean> = {};
    for (const evaluation of results) flags[evaluation.key] = evaluation.enabled;
    return flags;
  }
  if (flagKeys) return {}; // explicitly empty list
  return client.getAllFlags(userId);
}

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

/**
 * Create a Vercel Edge middleware function that evaluates feature flags
 * and injects the results as request headers.
 *
 * The returned function has the same signature as a Vercel Edge middleware:
 *   `(request: Request) => Promise<Response>`
 *
 * The request is passed through with the injected headers; in a Next.js
 * project use `NextResponse.next({ request: { headers } })` with the same
 * headers instead of the built-in pass-through.
 */
export function createEdgeMiddleware(config: VercelEdgeConfig) {
  const client = new EdgeExperimentationClient(config);

  return async function middleware(request: Request): Promise<Response> {
    const userId = extractUserId(request, config);
    const newHeaders = new Headers(request.headers);

    // Never break the request pipeline: evaluation failures mean "disabled".
    let flags: Record<string, boolean> = {};
    try {
      flags = await evaluateFlagsForRequest(client, userId, config.flagKeys);
    } catch {
      flags = {};
    }
    for (const key of config.flagKeys ?? Object.keys(flags)) {
      newHeaders.set(`X-EP-Flag-${key}`, String(flags[key] ?? false));
    }

    // Inject the resolved user ID for downstream use
    if (userId) {
      newHeaders.set('X-EP-User-Id', userId);
    }

    return fetch(new Request(request, { headers: newHeaders }));
  };
}
