import fs from 'fs';
import React from 'react';
import ts from 'typescript';
import { act, fireEvent, render, screen } from '@testing-library/react';

import {
  ApiError,
  DEFAULT_REQUEST_TIMEOUT_MS,
  LONG_REQUEST_TIMEOUT_MS,
  MAX_REQUEST_TIMEOUT_MS,
  TOKEN_STORAGE_KEY,
  apiBase,
  apiFetch,
  apiUrl,
  buildQuery,
  clearToken,
  getToken,
  isApiError,
  messageForDetail,
  navigation,
  redirectToLogin,
  safeNextPath,
  serverErrorMessage,
  setToken,
  signInTimeoutMessage,
  timeoutMessage,
  unreachableMessage,
  wsBase,
} from '@/services/api';
import { AdminService } from '@/services/admin';
import { ResultsService } from '@/services/results';
import { UserTable, deleteTimedOutMessage } from '@/components/admin/users/UserTable';
import { UNREACHABLE, errorCopy } from '@/components/experiments/ExperimentManageSection';
import { SCHEDULE_ERROR_COPY, scheduleErrorCopy } from '@/components/experiments/ExperimentScheduleSection';
import { rolloutErrorCopy } from '@/components/feature-flags/RolloutScheduleManager';
import { resultsErrorMessage } from '@/components/results/ResultsDashboard/ResultsDashboard';
import { sessionErrorMessage } from '@/contexts/AuthContext';
import { loginErrorMessage } from '@/pages/login';
import { classifyPasswordError, passwordOutcomeSentence } from '@/services/password';
import { groupErrorCopy } from '@/utils/exclusionGroups';
import { holdoutErrorCopy, loadErrorCopy, resultsErrorCopy } from '@/utils/holdouts';
import { segmentErrorCopy } from '@/utils/segmentErrors';
import { MODULES_SRC_ROOT, allMatches, listSourceFiles, SRC_ROOT, sourceLabel } from '../helpers/api-literals';

const mockFetch = jest.fn();
global.fetch = mockFetch;

/** Await a promise that is expected to reject and return the ApiError. */
async function rejection(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (err) {
    return err as ApiError;
  }
  throw new Error('expected promise to reject');
}

let assignSpy: jest.SpyInstance;

beforeEach(() => {
  mockFetch.mockReset();
  localStorage.clear();
  delete process.env.NEXT_PUBLIC_API_URL;
  delete process.env.NEXT_PUBLIC_WS_URL;
  window.history.replaceState({}, '', '/experiments?page=2');
  assignSpy = jest.spyOn(navigation, 'assign').mockImplementation(() => {});
});

afterEach(() => {
  assignSpy.mockRestore();
});

function jsonResponse(status: number, body: unknown, extra: Partial<Response> = {}): Response {
  const text = body === undefined ? '' : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'STATUS',
    headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(text),
    ...extra,
  } as unknown as Response;
}

describe('apiBase / apiUrl / buildQuery', () => {
  it('defaults to same-origin (empty string)', () => {
    expect(apiBase()).toBe('');
    expect(apiUrl('/api/v1/experiments')).toBe('/api/v1/experiments');
  });

  it('falls back to localhost:8000 only under next dev', () => {
    const original = process.env.NODE_ENV;
    try {
      (process.env as Record<string, string | undefined>).NODE_ENV = 'development';
      expect(apiBase()).toBe('http://localhost:8000');
      process.env.NEXT_PUBLIC_API_URL = '';
      expect(apiBase()).toBe('');
      delete process.env.NEXT_PUBLIC_API_URL;
      (process.env as Record<string, string | undefined>).NODE_ENV = 'production';
      expect(apiBase()).toBe('');
    } finally {
      (process.env as Record<string, string | undefined>).NODE_ENV = original;
    }
  });

  it('uses NEXT_PUBLIC_API_URL and strips trailing slashes', () => {
    process.env.NEXT_PUBLIC_API_URL = 'http://localhost:8000/';
    expect(apiBase()).toBe('http://localhost:8000');
    expect(apiUrl('api/v1/x')).toBe('http://localhost:8000/api/v1/x');
  });

  it('omits undefined and null query values', () => {
    expect(buildQuery({ a: 1, b: undefined, c: null, d: 'x', e: false })).toBe('?a=1&d=x&e=false');
    expect(buildQuery({})).toBe('');
    expect(buildQuery(undefined)).toBe('');
  });

  it('derives the websocket base from the api base or location', () => {
    expect(wsBase()).toBe('ws://localhost');
    process.env.NEXT_PUBLIC_API_URL = 'https://api.example.com';
    expect(wsBase()).toBe('wss://api.example.com');
    process.env.NEXT_PUBLIC_WS_URL = 'wss://ws.example.com/';
    expect(wsBase()).toBe('wss://ws.example.com');
  });
});

describe('token storage', () => {
  it('round-trips the token through localStorage', () => {
    expect(getToken()).toBeNull();
    setToken('abc');
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('abc');
    expect(getToken()).toBe('abc');
    clearToken();
    expect(getToken()).toBeNull();
  });
});

describe('apiFetch — request shaping', () => {
  it('calls the global fetch with the prefixed URL and JSON headers', async () => {
    process.env.NEXT_PUBLIC_API_URL = 'http://localhost:8000';
    mockFetch.mockResolvedValueOnce(jsonResponse(200, { ok: 1 }));
    const result = await apiFetch<{ ok: number }>('/api/v1/experiments', { query: { page: 2 } });
    expect(result).toEqual({ ok: 1 });
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe('http://localhost:8000/api/v1/experiments?page=2');
    expect(init.headers.Accept).toBe('application/json');
    expect(init.headers.Authorization).toBeUndefined();
  });

  it('adds the bearer header when a token is stored', async () => {
    setToken('tok-123');
    mockFetch.mockResolvedValueOnce(jsonResponse(200, {}));
    await apiFetch('/api/v1/auth/me');
    const [, init] = mockFetch.mock.calls[0];
    expect(init.headers.Authorization).toBe('Bearer tok-123');
  });

  it('does not add the bearer header when auth is false', async () => {
    setToken('tok-123');
    mockFetch.mockResolvedValueOnce(jsonResponse(200, {}));
    await apiFetch('/api/v1/auth/login', { auth: false });
    const [, init] = mockFetch.mock.calls[0];
    expect(init.headers.Authorization).toBeUndefined();
  });

  it('serialises `json` and sets Content-Type', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(201, { id: '1' }));
    await apiFetch('/api/v1/experiments', { method: 'POST', json: { name: 'x' } });
    const [, init] = mockFetch.mock.calls[0];
    expect(init.method).toBe('POST');
    expect(init.body).toBe(JSON.stringify({ name: 'x' }));
    expect(init.headers['Content-Type']).toBe('application/json');
  });

  it('passes a string body through and keeps caller headers', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, {}));
    await apiFetch('/x', { method: 'PUT', body: '{"a":1}', headers: { 'X-Test': '1' } });
    const [, init] = mockFetch.mock.calls[0];
    expect(init.body).toBe('{"a":1}');
    expect(init.headers['X-Test']).toBe('1');
    expect(init.headers['Content-Type']).toBe('application/json');
  });

  it('returns undefined for 204 responses', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(204, undefined));
    await expect(apiFetch('/x', { method: 'DELETE' })).resolves.toBeUndefined();
  });

  it('tolerates minimal mocked responses without json()/headers', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true } as Response);
    await expect(apiFetch('/x')).resolves.toBeUndefined();
  });

  it('rejects a non-JSON 2xx body (e.g. an HTML shell served by a proxy)', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(200, undefined, {
        headers: { get: () => 'text/html' } as unknown as Headers,
        text: () => Promise.resolve('<html></html>'),
      }),
    );
    await expect(apiFetch('/api/v1/experiments')).rejects.toMatchObject({
      status: 200,
      message: expect.stringContaining('Expected a JSON response'),
    });
  });
});

describe('apiFetch — errors', () => {
  it('throws ApiError with the string detail as message', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(404, { detail: 'Experiment not found' }));
    const err = await rejection(apiFetch('/api/v1/experiments/x'));
    expect(isApiError(err)).toBe(true);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
    expect(err.detail).toBe('Experiment not found');
    expect(err.message).toBe('Experiment not found');
    expect(err.code).toBeUndefined();
  });

  it('exposes a typed detail.code', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(403, { detail: { code: 'workspace_role_required', message: 'Owner role required' } }),
    );
    const err = await rejection(apiFetch('/api/v1/workspaces/'));
    expect(err.status).toBe(403);
    expect(err.code).toBe('workspace_role_required');
    expect(err.isForbidden).toBe(true);
    expect(err.message).toBe('Owner role required');
    expect(assignSpy).not.toHaveBeenCalled();
  });

  it('formats pydantic validation errors', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(422, {
        detail: [{ loc: ['body', 'name'], msg: 'field required', type: 'value_error' }],
      }),
    );
    const err = await rejection(apiFetch('/x', { method: 'POST', json: {} }));
    expect(err.status).toBe(422);
    expect(err.message).toBe('name: field required');
  });

  it('falls back to statusText when a 4xx body is unreadable', async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 418, statusText: "I'm a teapot" } as Response);
    await expect(apiFetch('/x')).rejects.toThrow("I'm a teapot");
  });

  it('wraps network failures as ApiError status 0', async () => {
    mockFetch.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const err = await rejection(apiFetch('/x'));
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(0);
    expect(err.isNetworkError).toBe(true);
    expect(err.message).toContain("Can't reach the API");
  });

  it('messageForDetail handles the remaining shapes', () => {
    expect(messageForDetail(500, undefined)).toBe('Request failed with status 500');
    expect(messageForDetail(0, undefined)).toBe('Request failed');
    expect(messageForDetail(400, { msg: 'nope' })).toBe('nope');
    expect(messageForDetail(400, { code: 'x_code' })).toBe('x_code');
    expect(messageForDetail(400, ['a', 'b'])).toBe('a; b');
  });
});

describe('apiFetch — 401 handling', () => {
  it('clears the token and redirects to /login?next=<path>', async () => {
    setToken('stale');
    mockFetch.mockResolvedValueOnce(jsonResponse(401, { detail: 'Not authenticated' }));
    const err = await rejection(apiFetch('/api/v1/experiments'));
    expect(err.status).toBe(401);
    expect(err.isUnauthorized).toBe(true);
    expect(getToken()).toBeNull();
    expect(assignSpy).toHaveBeenCalledWith('/login?next=%2Fexperiments%3Fpage%3D2');
  });

  it('does not redirect when already on /login', async () => {
    window.history.replaceState({}, '', '/login');
    setToken('stale');
    mockFetch.mockResolvedValueOnce(jsonResponse(401, { detail: 'Invalid email or password' }));
    await expect(apiFetch('/api/v1/auth/me')).rejects.toThrow('Invalid email or password');
    expect(assignSpy).not.toHaveBeenCalled();
  });

  it('does not redirect or clear the token for unauthenticated requests (auth: false)', async () => {
    setToken('keep-me');
    mockFetch.mockResolvedValueOnce(jsonResponse(401, { detail: 'Invalid email or password' }));
    await expect(
      apiFetch('/api/v1/auth/login', { method: 'POST', json: {}, auth: false }),
    ).rejects.toThrow('Invalid email or password');
    expect(getToken()).toBe('keep-me');
    expect(assignSpy).not.toHaveBeenCalled();
  });

  it('honours redirectOn401: false but still clears the token', async () => {
    setToken('stale');
    mockFetch.mockResolvedValueOnce(jsonResponse(401, { detail: 'expired' }));
    await expect(apiFetch('/api/v1/auth/me', { redirectOn401: false })).rejects.toThrow('expired');
    expect(getToken()).toBeNull();
    expect(assignSpy).not.toHaveBeenCalled();
  });
});

describe('redirectToLogin / safeNextPath', () => {
  it('drops next for the root and login paths', () => {
    redirectToLogin('/');
    expect(assignSpy).toHaveBeenLastCalledWith('/login');
    redirectToLogin('/login?next=%2Fx');
    expect(assignSpy).toHaveBeenLastCalledWith('/login');
  });

  it('rejects protocol-relative and external targets', () => {
    expect(safeNextPath('//evil.com')).toBe('/');
    expect(safeNextPath('https://evil.com')).toBe('/');
    expect(safeNextPath(undefined, '/experiments')).toBe('/experiments');
    expect(safeNextPath('/feature-flags/1')).toBe('/feature-flags/1');
    redirectToLogin('//evil.com');
    expect(assignSpy).toHaveBeenLastCalledWith('/login');
  });
});

/** A failed response with a raw body and the given headers (case-insensitive). */
function rawResponse(status: number, body: string, headers: Record<string, string> = {}): Response {
  const lower = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return {
    ok: false,
    status,
    statusText: 'STATUS',
    headers: { get: (name: string) => lower[name.toLowerCase()] ?? null },
    text: () => Promise.resolve(body),
  } as unknown as Response;
}

describe('server errors (#72)', () => {
  it('a plain-text 500 says "server error" with the request id, not the body', async () => {
    mockFetch.mockResolvedValueOnce(
      rawResponse(500, 'Internal Server Error', {
        'Content-Type': 'text/plain; charset=utf-8',
        'X-Request-ID': 'req-72-abc',
      }),
    );
    const err = await rejection(apiFetch('/x'));
    expect(err.status).toBe(500);
    expect(err.isServerError).toBe(true);
    expect(err.isNetworkError).toBe(false);
    expect(err.requestId).toBe('req-72-abc');
    expect(err.message).toBe(
      'Something went wrong on the server (HTTP 500). This is not a connection problem. ' +
        'Request ID: req-72-abc. Your administrator can find the details by searching the API log for this ID.',
    );
    expect(err.message).not.toContain("Can't reach");
  });

  it('without a request id it still says server error', async () => {
    mockFetch.mockResolvedValueOnce(rawResponse(500, 'Internal Server Error'));
    const err = await rejection(apiFetch('/x'));
    expect(err.requestId).toBeUndefined();
    expect(err.message).toBe(
      'Something went wrong on the server (HTTP 500). Your administrator can find the details in the API log.',
    );
  });

  it("a proxy's HTML 502 page is not shown as the message", async () => {
    mockFetch.mockResolvedValueOnce(
      rawResponse(502, '<html><body><h1>502 Bad Gateway</h1></body></html>', { 'Content-Type': 'text/html' }),
    );
    const err = await rejection(apiFetch('/x'));
    expect(err.message).toBe(serverErrorMessage(502));
    expect(err.message).not.toContain('<');
  });

  it('a 5xx with a JSON detail keeps the detail and adds the request id', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(503, { detail: 'Module not available' }, {
        headers: {
          get: (name: string) =>
            ({ 'content-type': 'application/json', 'x-request-id': 'req-503' })[name.toLowerCase()] ?? null,
        },
      } as Partial<Response>),
    );
    const err = await rejection(apiFetch('/x'));
    expect(err.message).toBe('Module not available (Request ID: req-503)');
    expect(err.detail).toBe('Module not available');
  });

  describe('a 5xx JSON detail that already names the request id (#401)', () => {
    function withRequestId(status: number, detail: string, requestId?: string): Response {
      const headers: Record<string, string> = { 'content-type': 'application/json' };
      if (requestId !== undefined) headers['x-request-id'] = requestId;
      return jsonResponse(status, { detail }, {
        headers: { get: (name: string) => headers[name.toLowerCase()] ?? null },
      } as Partial<Response>);
    }

    it('a detail without the id still gets the suffix', async () => {
      mockFetch.mockResolvedValueOnce(withRequestId(500, 'Database unavailable', 'abc-1'));
      const err = await rejection(apiFetch('/x'));
      expect(err.message).toBe('Database unavailable (Request ID: abc-1)');
    });

    it('a detail that already contains the id is not suffixed again', async () => {
      const detail = 'Internal server error (request ID: ABC-1)';
      mockFetch.mockResolvedValueOnce(withRequestId(500, detail, 'abc-1'));
      const err = await rejection(apiFetch('/x'));
      expect(err.message).toBe(detail);
      expect(err.requestId).toBe('abc-1');
    });

    it('a detail naming a different id still gets the suffix', async () => {
      mockFetch.mockResolvedValueOnce(withRequestId(500, 'Failed (request ID: xyz-9)', 'abc-1'));
      const err = await rejection(apiFetch('/x'));
      expect(err.message).toBe('Failed (request ID: xyz-9) (Request ID: abc-1)');
    });

    it('without a request id header the detail is unchanged', async () => {
      mockFetch.mockResolvedValueOnce(withRequestId(500, 'Internal server error (request ID: abc-1)'));
      const err = await rejection(apiFetch('/x'));
      expect(err.message).toBe('Internal server error (request ID: abc-1)');
      expect(err.requestId).toBeUndefined();
    });
  });

  it('a 4xx message is unchanged by a request id', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(404, { detail: 'Not found' }, {
        headers: { get: (name: string) => (name.toLowerCase() === 'x-request-id' ? 'req-404' : null) },
      } as Partial<Response>),
    );
    const err = await rejection(apiFetch('/x'));
    expect(err.message).toBe('Not found');
    expect(err.requestId).toBe('req-404');
  });

  it('a header value that is not id-shaped is not shown', async () => {
    mockFetch.mockResolvedValueOnce(
      rawResponse(500, '', { 'X-Request-ID': 'x'.repeat(129) }),
    );
    const tooLong = await rejection(apiFetch('/x'));
    expect(tooLong.requestId).toBeUndefined();
    mockFetch.mockResolvedValueOnce(rawResponse(500, '', { 'X-Request-ID': 'a b<script>' }));
    const odd = await rejection(apiFetch('/x'));
    expect(odd.requestId).toBeUndefined();
    expect(odd.message).toBe(serverErrorMessage(500));
  });
});

describe('unreachable copy', () => {
  const env = process.env as Record<string, string | undefined>;
  const original = env.NODE_ENV;
  afterEach(() => {
    env.NODE_ENV = original;
  });

  it('names CORS_ORIGINS, because a missing CORS header looks the same as a down backend', () => {
    process.env.NEXT_PUBLIC_API_URL = 'https://app.example.com';
    env.NODE_ENV = 'production';
    expect(unreachableMessage()).toBe(
      "Can't reach the API at https://app.example.com. Check that the backend is running and that " +
        "this dashboard's origin is listed in CORS_ORIGINS.",
    );
  });

  it('points at the quick start only in development', () => {
    env.NODE_ENV = 'development';
    expect(unreachableMessage()).toContain('See docs → Quick start.');
  });
});

describe('Retry-After (#440 PR D, gates-D D5)', () => {
  function limited(retryAfter: string | null): Response {
    return jsonResponse(429, { detail: 'Rate limit exceeded' }, {
      headers: {
        get: (name: string) =>
          ({ 'content-type': 'application/json', ...(retryAfter === null ? {} : { 'retry-after': retryAfter }) } as Record<
            string,
            string
          >)[name.toLowerCase()] ?? null,
      },
    } as unknown as Partial<Response>);
  }

  it('reads whole seconds onto the error', async () => {
    mockFetch.mockResolvedValueOnce(limited('7'));
    const err = await rejection(apiFetch('/x'));
    expect(err.status).toBe(429);
    expect(err.retryAfter).toBe(7);
  });

  it('ignores a missing value, an HTTP date and junk', async () => {
    for (const value of [null, 'Wed, 21 Oct 2026 07:28:00 GMT', '1.5', '-3']) {
      mockFetch.mockResolvedValueOnce(limited(value));
      const err = await rejection(apiFetch('/x'));
      expect(err.retryAfter).toBeUndefined();
    }
  });
});

// ---------------------------------------------------------------------------
// #1040: every request has a deadline. Gates D1-D10 of the bug-sweep plan
// (B8b). The fetch stand-in rejects with an AbortError when its signal aborts,
// as browsers do, and the clock is jest's.
// ---------------------------------------------------------------------------

/** The deadlines, written out so the tests pin the values, not whatever the constants say. */
const THIRTY_SECONDS = 30_000;
const TWO_MINUTES = 120_000;

/** What a timed-out read says (exact). */
const READ_TEXT =
  'The API did not answer within 30 seconds. Try again, and if it keeps happening check that the backend is healthy.';
/** What a timed-out write says (exact). */
const WRITE_TEXT =
  'The API did not answer within 30 seconds, and the request may or may not have been carried out. ' +
  'Reload the page and check before you try again.';
/** What the sign-in form says (exact). */
const SIGN_IN_TEXT = 'Sign-in did not finish: the API did not answer within 30 seconds. Try again in a moment.';

/** A fetch that never answers, and rejects with an AbortError once its signal aborts. */
function hangUntilAborted(_url?: unknown, init?: RequestInit): Promise<Response> {
  return new Promise((_resolve, reject) => {
    const signal = init?.signal;
    if (!signal) return;
    const abort = () => reject(new DOMException('The operation was aborted.', 'AbortError'));
    if (signal.aborted) abort();
    else signal.addEventListener('abort', abort);
  });
}

interface Tracked<T> {
  settled: boolean;
  value?: T;
  error?: unknown;
}

/** Watch a promise without awaiting it, so a test can ask whether it has settled yet. */
function track<T>(promise: Promise<T>): Tracked<T> {
  const state: Tracked<T> = { settled: false };
  promise.then(
    (value) => {
      state.settled = true;
      state.value = value;
    },
    (error) => {
      state.settled = true;
      state.error = error;
    },
  );
  return state;
}

function lastInit(): RequestInit {
  return mockFetch.mock.calls[mockFetch.mock.calls.length - 1][1] as RequestInit;
}

function summary(err: unknown) {
  const e = err as ApiError;
  return {
    isApiError: e instanceof ApiError,
    status: e?.status,
    timedOut: e?.timedOut,
    outcomeUnknown: e?.outcomeUnknown,
    message: e?.message,
  };
}

function useFakeClock(): void {
  beforeEach(() => {
    jest.useFakeTimers();
  });
  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
  });
}

/** D1, also re-run by D6. */
async function firesAtExactlyTheDefault(): Promise<void> {
  mockFetch.mockImplementation(hangUntilAborted);
  const call = track(apiFetch('/api/v1/experiments'));
  await jest.advanceTimersByTimeAsync(THIRTY_SECONDS - 1);
  expect(call.error).toBeUndefined();
  expect(call.settled).toBe(false);
  expect(lastInit().signal?.aborted).toBe(false);
  await jest.advanceTimersByTimeAsync(1);
  expect(call.settled).toBe(true);
  expect(summary(call.error)).toEqual({
    isApiError: true,
    status: 0,
    timedOut: true,
    outcomeUnknown: false,
    message: READ_TEXT,
  });
  expect((call.error as ApiError).detail).toBe(READ_TEXT);
  expect(lastInit().signal?.aborted).toBe(true);
}

/** D4, also re-run by D6: the caller's abort is reported as before, not as a time-out. */
async function callerAbortIsTheCallers(): Promise<void> {
  mockFetch.mockImplementation(hangUntilAborted);
  const controller = new AbortController();
  const call = track(apiFetch('/api/v1/experiments', { signal: controller.signal }));
  await jest.advanceTimersByTimeAsync(1);
  expect(call.error).toBeUndefined();
  expect(call.settled).toBe(false);
  controller.abort();
  await jest.advanceTimersByTimeAsync(0);
  expect(call.settled).toBe(true);
  expect(summary(call.error)).toEqual({
    isApiError: true,
    status: 0,
    timedOut: false,
    outcomeUnknown: false,
    message: unreachableMessage(),
  });
  expect(lastInit().signal?.aborted).toBe(true);
}

/** D4, also re-run by D6: a caller's signal that never aborts does not switch the deadline off. */
async function quietCallerSignalStillGetsTheDefault(): Promise<void> {
  mockFetch.mockImplementation(hangUntilAborted);
  const controller = new AbortController();
  const call = track(apiFetch('/api/v1/experiments', { signal: controller.signal }));
  await jest.advanceTimersByTimeAsync(THIRTY_SECONDS - 1);
  expect(call.error).toBeUndefined();
  expect(call.settled).toBe(false);
  await jest.advanceTimersByTimeAsync(1);
  expect(summary(call.error)).toEqual({
    isApiError: true,
    status: 0,
    timedOut: true,
    outcomeUnknown: false,
    message: READ_TEXT,
  });
  // The deadline aborts the request, never the caller's own signal.
  expect(controller.signal.aborted).toBe(false);
}

describe('regression #1040: a request gives up after 30 seconds', () => {
  useFakeClock();

  it('D1 fires at exactly the default deadline, and aborts the request', firesAtExactlyTheDefault);

  it.each(['POST', 'PUT', 'PATCH', 'DELETE', 'post'])(
    'D3 a timed-out %s says the request may or may not have been carried out, once',
    async (method) => {
      setToken('tok-1040');
      mockFetch.mockImplementation(hangUntilAborted);
      const call = track(apiFetch('/api/v1/experiments/e1', { method, json: { name: 'x' } }));
      await jest.advanceTimersByTimeAsync(THIRTY_SECONDS);
      expect(summary(call.error)).toEqual({
        isApiError: true,
        status: 0,
        timedOut: true,
        outcomeUnknown: true,
        message: WRITE_TEXT,
      });
      expect((call.error as ApiError).detail).toBe(WRITE_TEXT);
      // No retry, however long we wait; the token is kept and nothing redirects.
      await jest.advanceTimersByTimeAsync(THIRTY_SECONDS * 4);
      expect(mockFetch).toHaveBeenCalledTimes(1);
      expect(getToken()).toBe('tok-1040');
      expect(assignSpy).not.toHaveBeenCalled();
    },
  );

  it.each(['GET', 'HEAD', 'OPTIONS'])('D3 a timed-out %s says only that the API did not answer', async (method) => {
    mockFetch.mockImplementation(hangUntilAborted);
    const call = track(apiFetch('/api/v1/experiments', { method }));
    await jest.advanceTimersByTimeAsync(THIRTY_SECONDS);
    expect(summary(call.error)).toEqual({
      isApiError: true,
      status: 0,
      timedOut: true,
      outcomeUnknown: false,
      message: READ_TEXT,
    });
    await jest.advanceTimersByTimeAsync(THIRTY_SECONDS * 4);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it('D1 the deadlines are 30 seconds by default and 120 seconds for long routes', () => {
    expect(DEFAULT_REQUEST_TIMEOUT_MS).toBe(THIRTY_SECONDS);
    expect(LONG_REQUEST_TIMEOUT_MS).toBe(TWO_MINUTES);
  });

  it('D3 the read and write texts are these two different sentences', () => {
    expect(timeoutMessage(THIRTY_SECONDS, false)).toBe(READ_TEXT);
    expect(timeoutMessage(THIRTY_SECONDS, true)).toBe(WRITE_TEXT);
    expect(READ_TEXT).not.toBe(WRITE_TEXT);
    expect(signInTimeoutMessage()).toBe(SIGN_IN_TEXT);
  });
});

describe('the deadline ends at the headers (#1040 D2)', () => {
  useFakeClock();

  it('D2 a body that stalls after its headers is not aborted: the stated residual', async () => {
    mockFetch.mockImplementation((_url: unknown, init?: RequestInit) =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/json' : null) },
        // As in a browser, reading the body fails once the request's signal aborts.
        json: () => hangUntilAborted(undefined, init),
      }),
    );
    const call = track(apiFetch('/api/v1/experiments'));
    await jest.advanceTimersByTimeAsync(0);
    expect(jest.getTimerCount()).toBe(0);
    await jest.advanceTimersByTimeAsync(THIRTY_SECONDS + 1);
    expect(call.settled).toBe(false);
    expect(lastInit().signal?.aborted).toBe(false);
  });
});

describe("the caller's own signal (#1040 D4)", () => {
  useFakeClock();

  it("D4 a caller's abort rejects as the caller's, not with the time-out text", callerAbortIsTheCallers);

  it("D4 a caller's signal that never aborts still gets the default deadline", quietCallerSignalStillGetsTheDefault);

  it("D4 a caller's signal that is already aborted rejects at once, as the caller's", async () => {
    mockFetch.mockImplementation(hangUntilAborted);
    const controller = new AbortController();
    controller.abort();
    const call = track(apiFetch('/api/v1/experiments', { signal: controller.signal }));
    await jest.advanceTimersByTimeAsync(0);
    expect(summary(call.error)).toEqual({
      isApiError: true,
      status: 0,
      timedOut: false,
      outcomeUnknown: false,
      message: unreachableMessage(),
    });
  });

  it("D4 the call stops listening to the caller's signal once it is over", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, { ok: 1 }));
    const controller = new AbortController();
    const add = jest.spyOn(controller.signal, 'addEventListener');
    const remove = jest.spyOn(controller.signal, 'removeEventListener');
    await apiFetch('/api/v1/experiments', { signal: controller.signal });
    expect(add).toHaveBeenCalledTimes(1);
    expect(remove).toHaveBeenCalledWith('abort', add.mock.calls[0][1]);
  });
});

/** Every place a dashboard service overrides the deadline: `file` -> the values, in order. */
const EXPECTED_OVERRIDES: Record<string, string[]> = {
  'services/results.ts': Array(5).fill('LONG_REQUEST_TIMEOUT_MS'),
  ...(fs.existsSync(MODULES_SRC_ROOT)
    ? {
        'modules/frontend/src/services/warehouse.ts': [
          'LONG_REQUEST_TIMEOUT_MS',
          'LONG_REQUEST_TIMEOUT_MS',
          'LONG_REQUEST_TIMEOUT_MS',
          'PREVIEW_REQUEST_TIMEOUT_MS',
        ],
      }
    : {}),
};

/** The `timeoutMs: X` overrides in the dashboard sources (api.ts, which defines it, aside). */
function overrideInventory(): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const file of [...listSourceFiles(SRC_ROOT), ...listSourceFiles(MODULES_SRC_ROOT)]) {
    const label = sourceLabel(file);
    if (label === 'services/api.ts') continue;
    const values = allMatches(/\btimeoutMs\s*:\s*([A-Za-z_$][\w$.]*|[\d_]+)/, fs.readFileSync(file, 'utf8')).map(
      (m) => m[1],
    );
    if (values.length) out[label] = values;
  }
  return out;
}

describe('long routes wait longer (#1040 D5)', () => {
  useFakeClock();

  // The warehouse overrides are checked the same way in
  // modules/frontend/src/tests/services/request-timeouts.test.ts.
  const LONG_CALLS: Array<[string, () => Promise<unknown>, string, number]> = [
    ['ResultsService.getResults', () => ResultsService.getResults('e1'), '/api/v1/results/e1', TWO_MINUTES],
    [
      'ResultsService.getDailyResults',
      () => ResultsService.getDailyResults('e1'),
      '/api/v1/results/e1/daily',
      TWO_MINUTES,
    ],
    [
      'ResultsService.getSampleSize',
      () => ResultsService.getSampleSize('e1'),
      '/api/v1/results/e1/sample-size',
      TWO_MINUTES,
    ],
    [
      'ResultsService.getSequentialResults',
      () => ResultsService.getSequentialResults('e1'),
      '/api/v1/results/e1/sequential',
      TWO_MINUTES,
    ],
    [
      'ResultsService.invalidateCache',
      () => ResultsService.invalidateCache('e1'),
      '/api/v1/results/e1/invalidate-cache',
      TWO_MINUTES,
    ],
    [
      'AdminService.exportAuditLogs (apiDownload)',
      () => AdminService.exportAuditLogs('csv'),
      '/api/v1/audit-logs/export?format=csv',
      TWO_MINUTES,
    ],
  ];

  it.each(LONG_CALLS)('D5 %s waits past the default, until its own deadline', async (_name, call, url, timeoutMs) => {
    mockFetch.mockImplementation(hangUntilAborted);
    const pending = track(call());
    expect(mockFetch.mock.calls[0][0]).toBe(url);
    await jest.advanceTimersByTimeAsync(timeoutMs - 1);
    expect(pending.settled).toBe(false);
    await jest.advanceTimersByTimeAsync(1);
    const err = pending.error as ApiError;
    expect(err.timedOut).toBe(true);
    expect(err.message).toBe(timeoutMessage(timeoutMs, err.outcomeUnknown));
    expect(err.message).toContain(`within ${timeoutMs / 1000} seconds`);
  });

  it('D5 the overrides are exactly these', () => {
    expect(overrideInventory()).toEqual(EXPECTED_OVERRIDES);
  });
});

describe('without AbortSignal.any (#1040 D6)', () => {
  useFakeClock();
  const statics = AbortSignal as unknown as Record<string, unknown>;
  const saved: Record<string, unknown> = {};

  beforeAll(() => {
    for (const name of ['any', 'timeout']) {
      saved[name] = statics[name];
      delete statics[name];
    }
  });
  afterAll(() => {
    for (const name of ['any', 'timeout']) {
      if (saved[name] !== undefined) statics[name] = saved[name];
    }
  });

  it('D6 the probe holds: jsdom had AbortSignal.any, and this block removed it', () => {
    expect(typeof saved.any).toBe('function');
    expect(statics.any).toBeUndefined();
    expect(statics.timeout).toBeUndefined();
  });

  it('D6 D1 still fires at exactly the default', firesAtExactlyTheDefault);

  it("D6 D4 a caller's abort is still the caller's", callerAbortIsTheCallers);

  it("D6 D4 a caller's quiet signal still gets the default", quietCallerSignalStillGetsTheDefault);
});

// ---------------------------------------------------------------------------
// D7: every place that branches on "no answer" (status 0)
// ---------------------------------------------------------------------------

/**
 * What each finder looks for, in the dashboard sources (tests and mocks
 * aside): a comparison of `status` or `x.status` with 0 (either order, any
 * of == === != !== < <= > >=), `!status` / `!x.status`, `isNetworkError`,
 * `case 0:` and an object key `0` (a status-to-copy table). Over-inclusive
 * on purpose: anything they find must be classified below.
 */
const STATUS_ZERO_SITES: Record<string, { count: number; kind: string }> = {
  // The definition: ApiError.isNetworkError and its comparison.
  'services/api.ts': { count: 2, kind: 'definition' },
  'components/experiments/ExperimentManageSection.tsx': { count: 1, kind: 'write' },
  // SCHEDULE_ERROR_COPY's `0:` entry.
  'components/experiments/ExperimentScheduleSection.tsx': { count: 1, kind: 'write' },
  'components/feature-flags/RolloutScheduleManager.tsx': { count: 1, kind: 'write' },
  // Returns `e.message` for every status 0, so the time-out text already shows.
  'components/results/ResultsDashboard/ResultsDashboard.tsx': { count: 1, kind: 'passes-message' },
  'contexts/AuthContext.tsx': { count: 1, kind: 'read' },
  'pages/login.tsx': { count: 1, kind: 'sign-in' },
  // classifyPasswordError's `case 0:`.
  'services/password.ts': { count: 1, kind: 'write' },
  'utils/exclusionGroups.ts': { count: 1, kind: 'write' },
  // holdoutErrorCopy's `case 0:`, loadErrorCopy, resultsErrorCopy.
  'utils/holdouts.ts': { count: 3, kind: 'write' },
  'utils/segmentErrors.ts': { count: 1, kind: 'write' },
  // `if (!status)` on an experiment's status label: not an HTTP status.
  'types/experiments.ts': { count: 1, kind: 'not-http' },
  ...(fs.existsSync(MODULES_SRC_ROOT)
    ? {
        'modules/frontend/src/services/sso.ts': { count: 1, kind: 'sign-in' },
        'modules/frontend/src/services/warehouseRuns.ts': { count: 1, kind: 'write' },
      }
    : {}),
};

/**
 * Not a status-0 branch, but a write path that puts `err.message` inside its
 * own fixed wording ("Couldn't delete X: ..."), so a time-out would read as a
 * confident failure (PBS-9). Checked by D10.
 */
const MESSAGE_WRAPPING_WRITE_SITES = ['components/admin/users/UserTable.tsx'];

function dashboardSources(): string[] {
  return [...listSourceFiles(SRC_ROOT), ...listSourceFiles(MODULES_SRC_ROOT)];
}

/** Comments out, line breaks kept. Not string-aware: the AST finder is, and the two must agree. */
function withoutComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:'"`\\/])\/\/[^\n]*/g, '$1');
}

const STATUS_ZERO_PATTERNS = [
  /\bstatus\s*(?:[!=]==?|[<>]=?)\s*0(?![\w.])/,
  /(?<![\w.$])0\s*(?:[!=]==?|[<>]=?)\s*(?:[\w$]+\s*\.\s*)*status\b(?!\s*[(.[])/,
  /!\s*(?:[\w$]+\s*\.\s*)*status\b(?!\s*[(.[])/,
  /\bisNetworkError\b/,
  /\bcase\s+0\s*:/,
  /(?:^|[{,])\s*(?:0|'0'|"0")\s*:/m,
];

/** Finder 1: text patterns over the comment-free source. */
function textFinder(): Record<string, number> {
  const out: Record<string, number> = {};
  for (const file of dashboardSources()) {
    const source = withoutComments(fs.readFileSync(file, 'utf8'));
    const count = STATUS_ZERO_PATTERNS.reduce((n, re) => n + allMatches(re, source).length, 0);
    if (count) out[sourceLabel(file)] = count;
  }
  return out;
}

const COMPARISONS = new Set([
  ts.SyntaxKind.EqualsEqualsToken,
  ts.SyntaxKind.EqualsEqualsEqualsToken,
  ts.SyntaxKind.ExclamationEqualsToken,
  ts.SyntaxKind.ExclamationEqualsEqualsToken,
  ts.SyntaxKind.LessThanToken,
  ts.SyntaxKind.LessThanEqualsToken,
  ts.SyntaxKind.GreaterThanToken,
  ts.SyntaxKind.GreaterThanEqualsToken,
]);

/** Finder 2: the TypeScript parser, the same rules on the syntax tree. */
function astFinder(): Record<string, number> {
  const out: Record<string, number> = {};
  const isZero = (node: ts.Node) => ts.isNumericLiteral(node) && Number(node.text) === 0;
  const isStatus = (node: ts.Node) =>
    (ts.isIdentifier(node) && node.text === 'status') ||
    (ts.isPropertyAccessExpression(node) && node.name.text === 'status');
  for (const file of dashboardSources()) {
    const sf = ts.createSourceFile(
      file,
      fs.readFileSync(file, 'utf8'),
      ts.ScriptTarget.Latest,
      true,
      file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
    );
    let count = 0;
    const visit = (node: ts.Node): void => {
      if (
        ts.isBinaryExpression(node) &&
        COMPARISONS.has(node.operatorToken.kind) &&
        ((isStatus(node.left) && isZero(node.right)) || (isZero(node.left) && isStatus(node.right)))
      ) {
        count += 1;
      } else if (
        ts.isPrefixUnaryExpression(node) &&
        node.operator === ts.SyntaxKind.ExclamationToken &&
        isStatus(node.operand)
      ) {
        count += 1;
      } else if (ts.isIdentifier(node) && node.text === 'isNetworkError') {
        count += 1;
      } else if (ts.isCaseClause(node) && isZero(node.expression)) {
        count += 1;
      } else if (
        ts.isPropertyAssignment(node) &&
        (ts.isNumericLiteral(node.name) || ts.isStringLiteral(node.name)) &&
        node.name.text === '0'
      ) {
        count += 1;
      }
      ts.forEachChild(node, visit);
    };
    visit(sf);
    if (count) out[sourceLabel(file)] = count;
  }
  return out;
}

describe('the status-0 places are found and classified (#1040 D7)', () => {
  const byText = textFinder();
  const bySyntax = astFinder();

  it('D7 two independent finders agree, file by file', () => {
    expect(byText).toEqual(bySyntax);
  });

  it('D7 they find exactly the classified places (the control is this set, not a number)', () => {
    const expected = Object.fromEntries(Object.entries(STATUS_ZERO_SITES).map(([file, site]) => [file, site.count]));
    expect(bySyntax).toEqual(expected);
  });

  it('D7 every classified branch on an HTTP status checks timedOut, and so does the wrapping write site', () => {
    const mustCheck = [
      ...Object.entries(STATUS_ZERO_SITES)
        .filter(([, site]) => ['write', 'read', 'sign-in'].includes(site.kind))
        .map(([file]) => file),
      ...MESSAGE_WRAPPING_WRITE_SITES,
    ];
    const missing = mustCheck.filter((label) => {
      const file = label.startsWith('modules/')
        ? `${MODULES_SRC_ROOT}/${label.slice('modules/frontend/src/'.length)}`
        : `${SRC_ROOT}/${label}`;
      return !/\.timedOut\b/.test(withoutComments(fs.readFileSync(file, 'utf8')));
    });
    expect(missing).toEqual([]);
  });
});

describe('each status-0 place says what a time-out means (#1040 D7)', () => {
  useFakeClock();

  /** A real time-out from apiFetch, for a write or a read. */
  async function timedOut(method: 'POST' | 'GET'): Promise<ApiError> {
    mockFetch.mockImplementation(hangUntilAborted);
    const call = track(apiFetch('/api/v1/anything', { method }));
    await jest.advanceTimersByTimeAsync(THIRTY_SECONDS);
    expect((call.error as ApiError).timedOut).toBe(true);
    return call.error as ApiError;
  }
  const unreachable = () => new ApiError({ status: 0, detail: unreachableMessage() });

  // One check per classified core file (the modules' two are in
  // modules/frontend/src/tests/services/request-timeouts.test.ts). Each says
  // the time-out text exactly, and keeps its own unreachable line otherwise.
  const CHECKS: Record<string, (write: ApiError, read: ApiError) => void> = {
    'components/experiments/ExperimentManageSection.tsx': (write) => {
      for (const action of ['clone', 'edit', 'delete', 'design'] as const) {
        expect(errorCopy(action, write)).toBe(WRITE_TEXT);
        expect(errorCopy(action, unreachable())).toBe(UNREACHABLE);
      }
    },
    'components/experiments/ExperimentScheduleSection.tsx': (write) => {
      expect(scheduleErrorCopy(write)).toBe(WRITE_TEXT);
      expect(scheduleErrorCopy(unreachable())).toBe(SCHEDULE_ERROR_COPY[0]);
    },
    'components/feature-flags/RolloutScheduleManager.tsx': (write) => {
      expect(rolloutErrorCopy(write)).toBe(WRITE_TEXT);
      expect(rolloutErrorCopy(unreachable())).toBe(UNREACHABLE);
    },
    'components/results/ResultsDashboard/ResultsDashboard.tsx': (_write, read) => {
      expect(resultsErrorMessage(read)).toBe(READ_TEXT);
      expect(resultsErrorMessage(unreachable())).toBe(unreachableMessage());
    },
    'contexts/AuthContext.tsx': (_write, read) => {
      expect(sessionErrorMessage(read)).toBe(READ_TEXT);
      expect(sessionErrorMessage(unreachable())).toBe(unreachableMessage());
    },
    'pages/login.tsx': (write) => {
      expect(loginErrorMessage(write)).toBe(SIGN_IN_TEXT);
      expect(loginErrorMessage(unreachable())).toBe(unreachableMessage());
    },
    'services/password.ts': (write) => {
      expect(classifyPasswordError(write)).toEqual({ outcome: 'timed-out', message: WRITE_TEXT });
      expect(passwordOutcomeSentence(classifyPasswordError(write))).toBe(WRITE_TEXT);
      expect(classifyPasswordError(unreachable())).toEqual({ outcome: 'network' });
    },
    'utils/exclusionGroups.ts': (write, read) => {
      for (const action of ['save', 'archive', 'add', 'remove'] as const) {
        expect(groupErrorCopy(action, write)).toBe(WRITE_TEXT);
        expect(groupErrorCopy(action, unreachable())).toBe(UNREACHABLE);
      }
      expect(groupErrorCopy('load', read)).toBe(READ_TEXT);
    },
    'utils/holdouts.ts': (write, read) => {
      expect(holdoutErrorCopy(write)).toBe(WRITE_TEXT);
      expect(holdoutErrorCopy(write, { activates: true })).toBe(WRITE_TEXT);
      expect(holdoutErrorCopy(unreachable())).toBe(UNREACHABLE);
      expect(loadErrorCopy(read)).toBe(READ_TEXT);
      expect(loadErrorCopy(unreachable())).toBe(UNREACHABLE);
      expect(resultsErrorCopy(read)).toBe(READ_TEXT);
      expect(resultsErrorCopy(unreachable())).toBe(UNREACHABLE);
    },
    'utils/segmentErrors.ts': (write, read) => {
      expect(segmentErrorCopy(write, 'create')).toBe(WRITE_TEXT);
      expect(segmentErrorCopy(write, 'members')).toBe(WRITE_TEXT);
      expect(segmentErrorCopy(read, 'load')).toBe(READ_TEXT);
      expect(segmentErrorCopy(unreachable(), 'create')).toBe(unreachableMessage());
    },
  };

  it('D7 every classified core file has a check here', () => {
    const core = Object.entries(STATUS_ZERO_SITES)
      .filter(([file, site]) => !file.startsWith('modules/') && !['definition', 'not-http'].includes(site.kind))
      .map(([file]) => file)
      .sort();
    expect(Object.keys(CHECKS).sort()).toEqual(core);
  });

  it.each(Object.keys(CHECKS))('D7 %s', async (file) => {
    const write = await timedOut('POST');
    const read = await timedOut('GET');
    expect(write.message).toBe(WRITE_TEXT);
    expect(read.message).toBe(READ_TEXT);
    CHECKS[file](write, read);
  });
});

describe('timeoutMs is checked before anything is sent (#1040 D8)', () => {
  it.each([[0], [-1], [NaN], [Infinity], ['5'], [null], [MAX_REQUEST_TIMEOUT_MS + 1]])(
    'D8 refuses %p with a TypeError naming timeoutMs',
    async (value) => {
      let caught: unknown;
      try {
        await apiFetch('/api/v1/experiments', { timeoutMs: value as unknown as number });
      } catch (err) {
        caught = err;
      }
      expect(mockFetch).not.toHaveBeenCalled();
      expect(caught).toBeInstanceOf(TypeError);
      expect((caught as Error).message).toMatch(/^timeoutMs must be a number of milliseconds above 0/);
    },
  );

  it.each([[1], [MAX_REQUEST_TIMEOUT_MS]])('D8 accepts %p', async (value) => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, { ok: 1 }));
    await expect(apiFetch('/api/v1/experiments', { timeoutMs: value })).resolves.toEqual({ ok: 1 });
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
});

describe('the timer is always cleared (#1040 D9)', () => {
  useFakeClock();

  it('D9 positive control: a request in flight holds exactly one timer', async () => {
    mockFetch.mockImplementation(hangUntilAborted);
    void apiFetch('/api/v1/experiments').catch(() => undefined);
    await jest.advanceTimersByTimeAsync(0);
    expect(jest.getTimerCount()).toBe(1);
  });

  it.each([
    ['a success', () => mockFetch.mockResolvedValueOnce(jsonResponse(200, { ok: 1 }))],
    ['an HTTP error', () => mockFetch.mockResolvedValueOnce(jsonResponse(404, { detail: 'gone' }))],
    ['a network error', () => mockFetch.mockRejectedValueOnce(new TypeError('Failed to fetch'))],
  ])('D9 after %s', async (_name, arrange) => {
    arrange();
    await apiFetch('/api/v1/experiments').catch(() => undefined);
    expect(jest.getTimerCount()).toBe(0);
  });

  it("D9 after a caller's abort", async () => {
    mockFetch.mockImplementation(hangUntilAborted);
    const controller = new AbortController();
    const call = apiFetch('/api/v1/experiments', { signal: controller.signal }).catch(() => undefined);
    await jest.advanceTimersByTimeAsync(0);
    expect(jest.getTimerCount()).toBe(1);
    controller.abort();
    await call;
    expect(jest.getTimerCount()).toBe(0);
  });

  it('D9 after a download', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, { ok: 1 }));
    await apiDownloadOk();
    expect(jest.getTimerCount()).toBe(0);
  });
});

async function apiDownloadOk(): Promise<void> {
  await AdminService.exportAuditLogs('json');
}

describe('a timed-out user delete is not reported as a failure (#1040 D10)', () => {
  useFakeClock();

  const alice = {
    id: 'u-1',
    username: 'alice',
    email: 'alice@example.com',
    role: 'DEVELOPER',
    is_active: true,
    created_at: '2024-01-01T00:00:00Z',
  };

  function serve(deleteAnswer: (url: unknown, init?: RequestInit) => Promise<Response>): void {
    mockFetch.mockImplementation((url: unknown, init?: RequestInit) =>
      (init?.method ?? 'GET') === 'DELETE'
        ? deleteAnswer(url, init)
        : Promise.resolve(jsonResponse(200, { items: [alice], total: 1, skip: 0, limit: 50 })),
    );
  }

  async function deleteAlice(): Promise<string | null> {
    window.confirm = jest.fn(() => true);
    render(React.createElement(UserTable));
    await act(async () => {
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText('alice')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('delete-user-u-1'));
    await act(async () => {
      await jest.advanceTimersByTimeAsync(THIRTY_SECONDS);
    });
    return screen.getByTestId('delete-error-message').textContent;
  }

  it('D10 shows that the user may or may not have been deleted, in exactly these words', async () => {
    serve(hangUntilAborted);
    const shown = await deleteAlice();
    expect(shown).toBe(
      'The API did not answer within 30 seconds, so "alice" may or may not have been deleted. ' +
        'Reload the list and check before you try again.',
    );
    expect(shown).toBe(deleteTimedOutMessage('alice'));
    expect(screen.getByTestId('user-table').textContent).not.toContain("Couldn't delete");
  });

  it('D10 positive control: a refused delete keeps its own wording', async () => {
    serve(() => Promise.resolve(jsonResponse(400, { detail: 'Cannot delete your own user account' })));
    expect(await deleteAlice()).toBe("Couldn't delete alice: Cannot delete your own user account");
  });
});
