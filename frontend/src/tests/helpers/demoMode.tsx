/**
 * Demo mode for tests that go through the real `AuthConfigProvider`: the
 * dashboard learns it from `GET /api/v1/auth/config`, so these helpers answer
 * that path (and any others a test names) from a mocked `global.fetch`.
 *
 * Every demo-mode test runs in both states, `off` and `on`: the API's answer
 * without demo mode, and with it.
 */
import React from 'react';
import { useAuthConfig } from '@/contexts/AuthConfigContext';
import type { DemoAccount } from '@/services/authConfig';

/**
 * Rendered inside an `AuthConfigProvider`: `config-ready` appears once
 * `/auth/config` has been read, so a test can wait for it before asserting
 * that something is absent.
 */
export function ConfigReady() {
  const { loading } = useAuthConfig();
  return loading ? null : <span data-testid="config-ready" hidden />;
}

export type DemoState = 'off' | 'on';
export const BOTH_STATES: DemoState[] = ['off', 'on'];

/** A placeholder account; the real one comes from the box's settings. */
export const TEST_DEMO_ACCOUNT: DemoAccount = {
  email: 'demo-viewer@example.test',
  password: 'published-demo-password',
};

/** `/auth/config` without demo mode, and with it (account published). */
export const CONFIG_BODY: Record<DemoState, Record<string, unknown>> = {
  off: { provider: 'local' },
  on: { provider: 'local', demo_mode: true, demo_account: TEST_DEMO_ACCOUNT },
};

export const AUTH_CONFIG_PATH = '/api/v1/auth/config';

export function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  const all: Record<string, string> = { 'content-type': 'application/json', ...headers };
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'STATUS',
    headers: { get: (name: string) => all[name.toLowerCase()] ?? null },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(body === undefined ? '' : JSON.stringify(body)),
  } as unknown as Response;
}

export type FetchRoute = (init: RequestInit | undefined) => Response | Promise<Response>;

/**
 * A `fetch` that answers by method and path (the query string is ignored).
 * `/auth/config` answers `config` unless `routes` names it; anything else not
 * named is a 404, so a request nobody expected shows up as a failure.
 */
export function routedFetch(config: Record<string, unknown>, routes: Record<string, FetchRoute> = {}) {
  return (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    const path = url.replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    const method = (init?.method ?? 'GET').toUpperCase();
    const route = routes[`${method} ${path}`];
    if (route) return Promise.resolve(route(init));
    if (method === 'GET' && path === AUTH_CONFIG_PATH) return Promise.resolve(jsonResponse(200, config));
    return Promise.resolve(jsonResponse(404, { detail: `No route for ${method} ${path}` }));
  };
}
