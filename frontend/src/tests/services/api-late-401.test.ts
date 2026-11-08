/**
 * A 401 clears the stored token and redirects to the sign-in page only while
 * the stored token is still the one the request carried (#1579). A 401 that
 * arrives after a sign-out, or after the next sign-in, is about a token that
 * is no longer stored: it clears nothing and redirects nowhere, from any
 * request, not only the session check. Both callers of the 401 handling are
 * here: an ordinary `apiFetch` GET and the audit-log download (`apiDownload`).
 */
import { AdminService } from '@/services/admin';
import { ApiError, apiFetch, clearToken, getToken, isApiError, navigation, setToken } from '@/services/api';

const mockFetch = jest.fn();
global.fetch = mockFetch;

function jsonResponse(status: number, body: unknown): Response {
  const text = body === undefined ? '' : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'STATUS',
    headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(text),
  } as unknown as Response;
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function store(token: string | null): void {
  if (token === null) clearToken();
  else setToken(token);
}

const CALLS: Array<[string, () => Promise<unknown>]> = [
  ['an ordinary GET', () => apiFetch('/api/v1/experiments')],
  ['the audit-log download', () => AdminService.exportAuditLogs('csv')],
];

/**
 * Send `call` with `sent` stored, store `now` while it is in flight, then
 * answer it 401. Resolves with what the call rejected with.
 */
async function unauthorizedAfterAChange(
  call: () => Promise<unknown>,
  sent: string | null,
  now: string | null,
): Promise<ApiError> {
  store(sent);
  const server = deferred<Response>();
  mockFetch.mockImplementationOnce(() => server.promise);
  const outcome = call().then(
    () => {
      throw new Error('expected the call to reject');
    },
    (err: unknown) => err as ApiError,
  );
  expect(mockFetch).toHaveBeenCalledTimes(1);
  const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
  expect((init.headers as Record<string, string>).Authorization).toBe(sent === null ? undefined : `Bearer ${sent}`);
  store(now);
  server.resolve(jsonResponse(401, { detail: 'Could not validate credentials' }));
  return outcome;
}

let assignSpy: jest.SpyInstance;

beforeEach(() => {
  mockFetch.mockReset();
  localStorage.clear();
  window.history.replaceState({}, '', '/experiments?page=2');
  assignSpy = jest.spyOn(navigation, 'assign').mockImplementation(() => {});
});

afterEach(() => {
  assignSpy.mockRestore();
});

describe('a 401 for a token that is no longer stored (#1579)', () => {
  describe.each(CALLS)('%s', (_name, call) => {
    it.each([
      ['the next sign-in', 'jwt-next'],
      ['a sign-out', null],
    ])('a 401 that arrives after %s clears nothing and redirects nowhere', async (_change, now) => {
      const err = await unauthorizedAfterAChange(call, 'jwt-first', now);
      expect(isApiError(err)).toBe(true);
      expect(err.status).toBe(401);
      expect(getToken()).toBe(now);
      expect(assignSpy).not.toHaveBeenCalled();
    });

    it('a 401 for a request sent with no token keeps a token stored since', async () => {
      const err = await unauthorizedAfterAChange(call, null, 'jwt-next');
      expect(err.status).toBe(401);
      expect(getToken()).toBe('jwt-next');
      expect(assignSpy).not.toHaveBeenCalled();
    });

    it('pin: a 401 for the token still stored clears it and redirects to the sign-in page', async () => {
      const err = await unauthorizedAfterAChange(call, 'jwt-first', 'jwt-first');
      expect(err.status).toBe(401);
      expect(getToken()).toBeNull();
      expect(assignSpy).toHaveBeenCalledTimes(1);
      expect(assignSpy).toHaveBeenCalledWith('/login?next=%2Fexperiments%3Fpage%3D2');
    });

    it('pin: a 401 with no token stored, before or after, still redirects to the sign-in page', async () => {
      const err = await unauthorizedAfterAChange(call, null, null);
      expect(err.status).toBe(401);
      expect(getToken()).toBeNull();
      expect(assignSpy).toHaveBeenCalledTimes(1);
      expect(assignSpy).toHaveBeenCalledWith('/login?next=%2Fexperiments%3Fpage%3D2');
    });
  });
});

describe('the header a request sent is what its 401 is judged by (#1579)', () => {
  it('a request whose own Authorization differs from the stored token leaves the stored token alone', async () => {
    setToken('jwt-first');
    mockFetch.mockImplementationOnce(() => Promise.resolve(jsonResponse(401, { detail: 'nope' })));
    const err = await apiFetch('/api/v1/experiments', { headers: { Authorization: 'Bearer jwt-other' } }).then(
      () => {
        throw new Error('expected the call to reject');
      },
      (e: unknown) => e as ApiError,
    );
    expect(err.status).toBe(401);
    expect(getToken()).toBe('jwt-first');
    expect(assignSpy).not.toHaveBeenCalled();
  });
});
