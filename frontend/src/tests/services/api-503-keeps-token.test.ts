/**
 * A 503 from the API keeps the stored sign-in; a 401 still discards it.
 *
 * When Cognito does not answer, the API answers a signed-in request with 503
 * and "Sign-in service is not answering. Try again in a moment.". The client
 * must not treat that as a bad token: the token stays in storage, nothing
 * navigates, and the error carries the detail and the request id.
 */
import {
  ApiError,
  TOKEN_STORAGE_KEY,
  apiFetch,
  getToken,
  navigation,
  setToken,
} from '@/services/api';

const COPY = 'Sign-in service is not answering. Try again in a moment.';
const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

function response(status: number, body: unknown, headers: Record<string, string>): Response {
  const all: Record<string, string> = { 'content-type': 'application/json', ...headers };
  return {
    ok: false,
    status,
    statusText: 'STATUS',
    headers: { get: (name: string) => all[name.toLowerCase()] ?? null },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response;
}

let assignSpy: jest.SpyInstance;

beforeEach(() => {
  mockFetch.mockReset();
  localStorage.clear();
  assignSpy = jest.spyOn(navigation, 'assign').mockImplementation(() => {});
});

afterEach(() => {
  assignSpy.mockRestore();
});

async function failure(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (err) {
    return err as ApiError;
  }
  throw new Error('expected the call to fail');
}

describe('a JSON 503 from the API', () => {
  it('keeps the stored token and goes nowhere', async () => {
    setToken('jwt-kept');
    mockFetch.mockResolvedValueOnce(
      response(503, { detail: COPY }, { 'x-request-id': 'req-503', 'retry-after': '2' }),
    );
    const err = await failure(apiFetch('/api/v1/users/me'));
    expect(err.status).toBe(503);
    expect(getToken()).toBe('jwt-kept');
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('jwt-kept');
    expect(assignSpy).not.toHaveBeenCalled();
  });

  it('carries the detail and the request id in the message', async () => {
    setToken('jwt-kept');
    mockFetch.mockResolvedValueOnce(response(503, { detail: COPY }, { 'x-request-id': 'req-503' }));
    const err = await failure(apiFetch('/api/v1/users/me'));
    expect(err.message).toBe(`${COPY} (Request ID: req-503)`);
    expect(err.detail).toBe(COPY);
  });
});

describe('a 401', () => {
  it('still clears the token and goes to the sign-in page', async () => {
    setToken('jwt-bad');
    mockFetch.mockResolvedValueOnce(response(401, { detail: 'Could not validate credentials' }, {}));
    const err = await failure(apiFetch('/api/v1/users/me'));
    expect(err.status).toBe(401);
    expect(getToken()).toBeNull();
    expect(assignSpy).toHaveBeenCalled();
  });
});
