/**
 * Shared set-up for the Cognito Hosted UI sign-in tests (#708): a fetch that
 * answers by path (the relay is a mock), the configuration, and helpers that
 * walk the browser through the Hosted UI and back.
 */
import React from 'react';
import { act, render, RenderResult } from '@testing-library/react';
import { AuthConfigProvider, __resetAuthConfigCache } from '@/contexts/AuthConfigContext';
import { AuthProvider, useAuth } from '@/contexts/AuthContext';
import CallbackPage, { __resetCallbackLatch } from '@/pages/auth/callback';
import LoginPage from '@/pages/login';
import { navigation } from '@/services/api';
import { PENDING_SIGN_IN_KEY } from '@/services/hostedUi';

export const POOL_A = {
  provider: 'cognito',
  hosted_ui_domain: 'pool-a.auth.eu-west-1.amazoncognito.com',
  client_id: 'clientpoola',
  scopes: ['openid', 'email', 'profile', 'aws.cognito.signin.user.admin'],
};

export const POOL_B = {
  provider: 'cognito',
  hosted_ui_domain: 'pool-b.auth.us-east-2.amazoncognito.com',
  client_id: 'clientpoolb',
  scopes: ['openid', 'email', 'profile', 'aws.cognito.signin.user.admin'],
};

export const ACCOUNT = {
  id: 'acc-1',
  email: 'pat@example.com',
  username: 'pat',
  full_name: 'Pat Example',
  role: 'VIEWER',
  is_superuser: false,
  is_active: true,
};

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

export function textResponse(status: number, body: string, contentType: string): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'STATUS',
    headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? contentType : null) },
    json: () => Promise.reject(new SyntaxError('not json')),
    text: () => Promise.resolve(body),
  } as unknown as Response;
}

export type Handler = (init: RequestInit, url: string) => Response | Promise<Response>;

/** The API, by path. An unlisted path answers 404 like FastAPI. */
export const api: { routes: Record<string, Handler>; calls: Array<{ path: string; init: RequestInit }> } = {
  routes: {},
  calls: [],
};

export const mockFetch = jest.fn((url: string, init: RequestInit = {}) => {
  const path = new URL(url, 'http://localhost').pathname;
  api.calls.push({ path, init });
  timeline.push(`fetch ${init.method ?? 'GET'} ${path}`);
  const handler = api.routes[path];
  if (!handler) return Promise.resolve(jsonResponse(404, { detail: 'Not Found' }));
  return Promise.resolve().then(() => handler(init, url));
});

export function callsTo(path: string) {
  return api.calls.filter((c) => c.path === path);
}

export const RELAY = '/api/v1/auth/cognito/exchange';
export const CONFIG = '/api/v1/auth/config';
export const USERS_ME = '/api/v1/users/me';
export const AUTH_ME = '/api/v1/auth/me';
export const LOGOUT = '/api/v1/auth/logout';

export const mockReplace = jest.fn().mockResolvedValue(true);
export const mockRouter = { query: {} as Record<string, string>, pathname: '/login', asPath: '/login' };

jest.mock('next/router', () => ({
  useRouter: () => ({
    replace: mockReplace,
    push: jest.fn(),
    pathname: mockRouter.pathname,
    asPath: mockRouter.asPath,
    query: mockRouter.query,
    isReady: true,
  }),
}));

jest.mock('next/head', () => {
  const MockHead = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  MockHead.displayName = 'MockHead';
  return MockHead;
});

/** Every URL the browser was sent to, in order. */
export const assigned: string[] = [];

/** Requests and navigations together, in the order they were made. */
export const timeline: string[] = [];

export function authorizeHits(): string[] {
  return assigned.filter((url) => new URL(url).pathname === '/oauth2/authorize');
}

export function logoutHits(): string[] {
  return assigned.filter((url) => new URL(url).pathname === '/logout');
}

let assignSpy: jest.SpyInstance | null = null;
let digestSpy: jest.SpyInstance | null = null;

/** Web Crypto digests started and not yet answered; `settle()` waits for them. */
const digestsInFlight = new Set<Promise<unknown>>();

/** Call from `beforeEach`. */
export function resetHarness(config: unknown = POOL_A): void {
  global.fetch = mockFetch as unknown as typeof fetch;
  mockFetch.mockClear();
  mockReplace.mockClear();
  api.calls = [];
  api.routes = { [CONFIG]: () => jsonResponse(200, config) };
  assigned.length = 0;
  timeline.length = 0;
  mockRouter.query = {};
  mockRouter.pathname = '/login';
  mockRouter.asPath = '/login';
  localStorage.clear();
  sessionStorage.clear();
  __resetAuthConfigCache();
  __resetCallbackLatch();
  window.history.replaceState(null, '', '/login');
  assignSpy?.mockRestore();
  assignSpy = jest.spyOn(navigation, 'assign').mockImplementation((url: string) => {
    assigned.push(url);
    timeline.push(`navigate ${new URL(url, 'http://localhost').pathname}`);
  });
  // Counts each digest the page starts. The call goes to the platform's own
  // digest, looked up when it is made.
  const subtle = globalThis.crypto.subtle;
  digestsInFlight.clear();
  digestSpy?.mockRestore();
  digestSpy = jest.spyOn(subtle, 'digest').mockImplementation((algorithm, data) => {
    const answer = (Object.getPrototypeOf(subtle) as SubtleCrypto).digest.call(subtle, algorithm, data);
    digestsInFlight.add(answer);
    const done = () => digestsInFlight.delete(answer);
    answer.then(done, done);
    return answer;
  });
}

export function restoreHarness(): void {
  assignSpy?.mockRestore();
  assignSpy = null;
  digestSpy?.mockRestore();
  digestSpy = null;
}

function Who() {
  const { user, status } = useAuth();
  return (
    <span data-testid="who">
      {status}:{user ? `${user.username}:${user.role}:${user.auth_provider}` : 'none'}
    </span>
  );
}

export function renderWithProviders(page: React.ReactElement): RenderResult {
  return render(
    <AuthConfigProvider>
      <AuthProvider>
        {page}
        <Who />
      </AuthProvider>
    </AuthConfigProvider>,
  );
}

async function turns(n: number): Promise<void> {
  for (let i = 0; i < n; i += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

export async function settle(): Promise<void> {
  // Web Crypto's digest settles on a later turn of the event loop, not in a
  // microtask, and Node answers it from its thread pool, so no fixed number
  // of turns is enough on a loaded machine: wait for every digest the page
  // started, then for what its answer sets off (a redirect, a render).
  await turns(12);
  for (let round = 0; round < 10 && digestsInFlight.size > 0; round += 1) {
    await act(async () => {
      await Promise.allSettled(Array.from(digestsInFlight));
    });
    await turns(12);
  }
}

/** `/login` as `_app.tsx` renders it, settled. */
export async function renderLogin(query: Record<string, string> = {}): Promise<RenderResult> {
  mockRouter.query = query;
  mockRouter.pathname = '/login';
  const qs = new URLSearchParams(query).toString();
  window.history.replaceState(null, '', qs ? `/login?${qs}` : '/login');
  const result = renderWithProviders(<LoginPage />);
  await settle();
  return result;
}

/** Arrive at `/auth/callback` with this query, as a new page load. */
export async function renderCallback(query: string): Promise<RenderResult> {
  __resetCallbackLatch();
  mockRouter.pathname = '/auth/callback';
  mockRouter.query = {};
  window.history.replaceState(null, '', `/auth/callback${query}`);
  const result = renderWithProviders(<CallbackPage />);
  await settle();
  return result;
}

/** The markup with React's generated ids named by their order (as local-sign-in-form.test.tsx). */
export function normalisedMarkup(container: HTMLElement): string {
  const ids = new Map<string, string>();
  return container.innerHTML.replace(/_r_[0-9a-z]+_|[«:]r[0-9a-z]+[»:]/g, (id) => {
    if (!ids.has(id)) ids.set(id, `id-${ids.size + 1}`);
    return ids.get(id) as string;
  });
}

/** The pending entry as stored. */
export function storedPending(): Record<string, unknown> | null {
  const raw = sessionStorage.getItem(PENDING_SIGN_IN_KEY);
  return raw === null ? null : (JSON.parse(raw) as Record<string, unknown>);
}

/** The Hosted UI's answer to the last authorize: `?code=<code>&state=<its state>`. */
export function hostedUiReturns(code = 'code-1'): string {
  const hits = authorizeHits();
  if (hits.length === 0) throw new Error('The browser was not sent to the Hosted UI');
  const last = hits[hits.length - 1];
  const state = new URL(last).searchParams.get('state') ?? '';
  return `?code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`;
}

/** A relay that exchanges any code; the account read answers `me`. */
export function apiSignsIn(me: unknown = ACCOUNT, extra: Record<string, unknown> = {}): void {
  api.routes[RELAY] = () => jsonResponse(200, { access_token: 'tok-1', expires_in: 3600, ...extra });
  api.routes[USERS_ME] = () => jsonResponse(200, me);
}
