/**
 * A 2xx whose body is there but cannot be read as JSON is an error, not a
 * success with nothing in it (#1039). An empty body is still a success, and a
 * caller's abort while the body is being read is still the caller's abort.
 */
import { apiFetch, isApiError } from '@/services/api';

const mockFetch = jest.fn();
global.fetch = mockFetch;

const PATH = '/api/v1/experiments';

/** What a read says (exact). */
const READ_TEXT =
  'The API answered /api/v1/experiments (HTTP 200), but its answer is not valid JSON (it may have been cut off ' +
  'on the way). Try again, and if it keeps happening check any proxy between the dashboard and the API.';

/** What a write says (exact): the API accepted it, so a retry would repeat it. */
function writeText(status: number): string {
  return (
    `The API accepted the request to /api/v1/experiments (HTTP ${status}), but its answer is not valid JSON ` +
    '(it may have been cut off on the way). Reload the page to see the result before you try again.'
  );
}

interface AnswerOptions {
  /** The Content-Type header; null sends none. */
  contentType?: string | null;
  requestId?: string;
}

/** A 2xx with this raw body, read the way a browser reads it. */
function answer(status: number, body: string, { contentType = 'application/json', requestId }: AnswerOptions = {}) {
  const headers: Record<string, string | null> = {
    'content-type': contentType,
    'x-request-id': requestId ?? null,
  };
  return {
    ok: true,
    status,
    statusText: 'OK',
    headers: { get: (name: string) => headers[name.toLowerCase()] ?? null },
    text: () => Promise.resolve(body),
    json: async () => JSON.parse(body) as unknown,
  } as unknown as Response;
}

/** A body read that never finishes, and rejects with an AbortError once the request's signal aborts. */
function hangUntilAborted(signal?: AbortSignal | null): Promise<never> {
  return new Promise((_resolve, reject) => {
    if (!signal) return;
    const abort = () => reject(new DOMException('The operation was aborted.', 'AbortError'));
    if (signal.aborted) abort();
    else signal.addEventListener('abort', abort);
  });
}

/** Await a promise that is expected to reject and return what it rejected with. */
async function rejection(promise: Promise<unknown>): Promise<Error & { status?: number }> {
  try {
    await promise;
  } catch (err) {
    return err as Error & { status?: number };
  }
  throw new Error('expected promise to reject');
}

beforeEach(() => {
  mockFetch.mockReset();
  localStorage.clear();
  delete process.env.NEXT_PUBLIC_API_URL;
});

describe('a 2xx whose body cannot be read as JSON (#1039)', () => {
  it.each(['GET', 'HEAD', 'OPTIONS'])('a %s answered with a cut-off body says to try again', async (method) => {
    mockFetch.mockResolvedValueOnce(answer(200, '{"a":'));
    const err = await rejection(apiFetch(PATH, { method }));
    expect(isApiError(err)).toBe(true);
    expect(err.status).toBe(200);
    expect(err.message).toBe(READ_TEXT);
  });

  it.each([
    ['POST', 201],
    ['PUT', 200],
    ['PATCH', 200],
    ['DELETE', 200],
  ])('a %s answered %i with a cut-off body says the API accepted it', async (method, status) => {
    mockFetch.mockResolvedValueOnce(answer(status, '{"id":'));
    const err = await rejection(apiFetch(PATH, { method, json: { name: 'x' } }));
    expect(isApiError(err)).toBe(true);
    expect(err.status).toBe(status);
    expect(err.message).toBe(writeText(status));
  });

  it('adds the request id when the answer carries one, and never quotes the body', async () => {
    mockFetch.mockResolvedValueOnce(answer(200, '{"secret_value": "abc', { requestId: 'req-1039' }));
    const err = await rejection(apiFetch(PATH));
    expect(err.message).toBe(`${READ_TEXT} Request ID: req-1039.`);
    expect(err.message).not.toContain('secret_value');
    expect(JSON.stringify((err as unknown as { detail: unknown }).detail)).not.toContain('secret_value');
  });

  it('a body with no Content-Type is read the same way', async () => {
    mockFetch.mockResolvedValueOnce(answer(200, '{"a":', { contentType: null }));
    const err = await rejection(apiFetch(PATH));
    expect(err.status).toBe(200);
    expect(err.message).toBe(READ_TEXT);
  });

  it('a stand-in with only json() that rejects is the same error', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: () => Promise.reject(new SyntaxError('Unexpected end of JSON input')),
    } as unknown as Response);
    const err = await rejection(apiFetch(PATH));
    expect(isApiError(err)).toBe(true);
    expect(err.message).toBe(READ_TEXT);
  });

  it('a read that fails part-way, with no abort, is the same error', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      text: () => Promise.reject(new TypeError('network error')),
    } as unknown as Response);
    const err = await rejection(apiFetch(PATH));
    expect(isApiError(err)).toBe(true);
    expect(err.message).toBe(READ_TEXT);
  });
});

describe('what stays a success (#1039)', () => {
  it.each([
    ['with a JSON Content-Type', 'application/json'],
    ['with no Content-Type', null],
  ])('pin: an empty 200 %s is undefined', async (_label, contentType) => {
    mockFetch.mockResolvedValueOnce(answer(200, '', { contentType }));
    await expect(apiFetch(PATH)).resolves.toBeUndefined();
  });

  it('pin: a 200 whose body is only white space is undefined, as the non-JSON branch already treats it', async () => {
    mockFetch.mockResolvedValueOnce(answer(200, ' \n\t '));
    await expect(apiFetch(PATH)).resolves.toBeUndefined();
  });

  it('pin: a stand-in with neither text() nor json() is undefined', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true } as Response);
    await expect(apiFetch(PATH)).resolves.toBeUndefined();
  });

  it('a body that parses is the parsed body', async () => {
    mockFetch.mockResolvedValueOnce(answer(200, '{"a":1}'));
    await expect(apiFetch(PATH)).resolves.toEqual({ a: 1 });
  });
});

describe("a caller's abort while the body is read (#1039)", () => {
  it.each(['text', 'json'])(
    'rejects with the AbortError itself, not an ApiError (a stand-in reading with %s())',
    async (reader) => {
      let reading!: () => void;
      const readStarted = new Promise<void>((resolve) => {
        reading = resolve;
      });
      mockFetch.mockImplementationOnce((_url: unknown, init?: RequestInit) =>
        Promise.resolve({
          ok: true,
          status: 200,
          headers: { get: () => 'application/json' },
          [reader]: () => {
            reading();
            return hangUntilAborted(init?.signal);
          },
        } as unknown as Response),
      );
      const controller = new AbortController();
      const call = apiFetch(PATH, { signal: controller.signal });
      // The headers are in and the body is being read: only then abort (or
      // the call has already settled without reading, and fails below).
      await Promise.race([readStarted, call.then(() => undefined, () => undefined)]);
      controller.abort();
      const err = await rejection(call);
      expect(err).toBeInstanceOf(DOMException);
      expect(err.name).toBe('AbortError');
      expect(isApiError(err)).toBe(false);
    },
  );
});
