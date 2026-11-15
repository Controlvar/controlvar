/**
 * The modules probe whose headers arrive but whose body stalls (#1039). The
 * probe gives up after MODULES_PROBE_TIMEOUT_MS by aborting; that abort used
 * to read as an empty success, i.e. "core, no error", cached for five
 * minutes. It is a failure at both levels: `ModulesService.get` rejects with
 * the AbortError, and `loadModules` resolves core with `error` set and asks
 * again after 30 seconds. Unlike ModulesContext.test.tsx, nothing here is
 * mocked but `fetch`.
 */
import { __resetModulesCache, loadModules } from '@/contexts/ModulesContext';
import { isApiError } from '@/services/api';
import { CORE_PROFILE, MODULES_PROBE_TIMEOUT_MS, ModulesService } from '@/services/modules';

const mockFetch = jest.fn();
global.fetch = mockFetch;

/** How long a failed probe is reused, written out so the test pins the value. */
const THIRTY_SECONDS = 30_000;

const FULL = { profile: 'full', modules: ['rbac'], version: '1.0.0' };

function jsonHeaders() {
  return { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/json' : null) };
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

/** Headers at once, then a body (read with `reader`) that stalls until the request is aborted. */
function stalledBody(reader: 'text' | 'json') {
  return (_url: unknown, init?: RequestInit) =>
    Promise.resolve({
      ok: true,
      status: 200,
      headers: jsonHeaders(),
      [reader]: () => hangUntilAborted(init?.signal),
    } as unknown as Response);
}

function fullAnswer(): Response {
  return {
    ok: true,
    status: 200,
    headers: jsonHeaders(),
    text: () => Promise.resolve(JSON.stringify(FULL)),
  } as unknown as Response;
}

interface Tracked<T> {
  settled: boolean;
  value?: T;
  error?: unknown;
}

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

beforeEach(() => {
  jest.useFakeTimers();
  __resetModulesCache();
  mockFetch.mockReset();
  delete process.env.NEXT_PUBLIC_API_URL;
});

afterEach(() => {
  jest.clearAllTimers();
  jest.useRealTimers();
});

describe('a modules answer whose body stalls (#1039)', () => {
  it.each(['text', 'json'] as const)(
    'ModulesService.get rejects with the AbortError when the probe gives up (body read with %s())',
    async (reader) => {
      mockFetch.mockImplementationOnce(stalledBody(reader));
      const call = track(ModulesService.get());
      await jest.advanceTimersByTimeAsync(MODULES_PROBE_TIMEOUT_MS - 1);
      expect(call.settled).toBe(false);
      await jest.advanceTimersByTimeAsync(1);
      expect(call.settled).toBe(true);
      expect((call.error as Error | undefined)?.name).toBe('AbortError');
      expect(isApiError(call.error)).toBe(false);
      expect(call.value).toBeUndefined();
    },
  );

  it('loadModules resolves core with the error set, and asks again after 30 seconds', async () => {
    mockFetch.mockImplementationOnce(stalledBody('text')).mockResolvedValueOnce(fullAnswer());
    const first = track(loadModules());
    await jest.advanceTimersByTimeAsync(MODULES_PROBE_TIMEOUT_MS);
    expect(first.settled).toBe(true);
    expect(first.value?.info).toEqual(CORE_PROFILE);
    expect(first.value?.error?.name).toBe('AbortError');

    // Still inside the 30 seconds: the failure is reused, nothing is sent.
    await jest.advanceTimersByTimeAsync(THIRTY_SECONDS - 1);
    await loadModules();
    expect(mockFetch).toHaveBeenCalledTimes(1);

    // Past them: asked again, and the full answer replaces the failure.
    await jest.advanceTimersByTimeAsync(1);
    const again = await loadModules();
    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect(again.info.profile).toBe('full');
    expect(again.error).toBeNull();
  });
});
