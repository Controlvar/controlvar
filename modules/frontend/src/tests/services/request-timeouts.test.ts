/**
 * #1040 in the modules' dashboard code: the warehouse calls that wait longer
 * than the default (gate D5), and the two places that branch on "no answer"
 * (status 0) saying what a time-out means (gate D7). The core half of both
 * gates is in frontend/src/tests/services/api.test.ts, which also finds and
 * classifies these two places.
 */
import { ApiError, apiFetch, timeoutMessage } from '@/services/api';
import { exchangeFailurePath, readSsoErrorParams, ssoErrorMessage } from '@modules/services/sso';
import { PREVIEW_REQUEST_TIMEOUT_MS, warehouseService } from '@modules/services/warehouse';
import { startRefusal, warehouseRunsService } from '@modules/services/warehouseRuns';

const mockFetch = jest.fn();
global.fetch = mockFetch;

/** Written out, so the tests pin the values rather than whatever the constants say. */
const THIRTY_SECONDS = 30_000;
const TWO_MINUTES = 120_000;
const THIRTY_ONE_MINUTES = 1_860_000;

const WRITE_TEXT =
  'The API did not answer within 30 seconds, and the request may or may not have been carried out. ' +
  'Reload the page and check before you try again.';
const SIGN_IN_TEXT = 'Sign-in did not finish: the API did not answer within 30 seconds. Try again in a moment.';

/** A fetch that never answers, and rejects with an AbortError once its signal aborts, as browsers do. */
function hangUntilAborted(_url?: unknown, init?: RequestInit): Promise<Response> {
  return new Promise((_resolve, reject) => {
    const signal = init?.signal;
    if (!signal) return;
    const abort = () => reject(new DOMException('The operation was aborted.', 'AbortError'));
    if (signal.aborted) abort();
    else signal.addEventListener('abort', abort);
  });
}

function track<T>(promise: Promise<T>): { settled: boolean; error?: unknown } {
  const state: { settled: boolean; error?: unknown } = { settled: false };
  promise.then(
    () => {
      state.settled = true;
    },
    (error) => {
      state.settled = true;
      state.error = error;
    },
  );
  return state;
}

beforeEach(() => {
  mockFetch.mockReset();
  jest.useFakeTimers();
});

afterEach(() => {
  jest.clearAllTimers();
  jest.useRealTimers();
});

describe('warehouse calls that wait longer (#1040 D5)', () => {
  it('a preview waits for the largest query limit plus both margins: 1860 seconds', () => {
    expect(PREVIEW_REQUEST_TIMEOUT_MS).toBe(THIRTY_ONE_MINUTES);
  });

  const CALLS: Array<[string, () => Promise<unknown>, string, number]> = [
    [
      'testUnsavedConnection',
      () => warehouseService.testUnsavedConnection({ warehouse_type: 'bigquery' } as never),
      '/api/v1/warehouse/analysis/connections/test',
      TWO_MINUTES,
    ],
    [
      'testConnection',
      () => warehouseService.testConnection('c1'),
      '/api/v1/warehouse/analysis/connections/c1/test',
      TWO_MINUTES,
    ],
    [
      'validateSource',
      () => warehouseService.validateSource('s1'),
      '/api/v1/warehouse/analysis/sources/s1/validate',
      TWO_MINUTES,
    ],
    [
      'previewSource',
      () => warehouseService.previewSource('s1'),
      '/api/v1/warehouse/analysis/sources/s1/preview',
      THIRTY_ONE_MINUTES,
    ],
    // Answers 202 at once, so it keeps the default.
    [
      'startRun',
      () => warehouseRunsService.startRun('e1', {} as never),
      '/api/v1/warehouse/analysis/experiments/e1/runs',
      THIRTY_SECONDS,
    ],
  ];

  it.each(CALLS)('D5 %s gives up at exactly its own deadline', async (_name, call, url, timeoutMs) => {
    mockFetch.mockImplementation(hangUntilAborted);
    const pending = track(call());
    expect(mockFetch.mock.calls[0][0]).toBe(url);
    await jest.advanceTimersByTimeAsync(timeoutMs - 1);
    expect(pending.settled).toBe(false);
    await jest.advanceTimersByTimeAsync(1);
    const err = pending.error as ApiError;
    expect(err.timedOut).toBe(true);
    expect(err.message).toBe(timeoutMessage(timeoutMs, true));
  });
});

describe('the modules status-0 places say what a time-out means (#1040 D7)', () => {
  async function timedOutWrite(): Promise<ApiError> {
    mockFetch.mockImplementation(hangUntilAborted);
    const call = track(apiFetch('/api/v1/anything', { method: 'POST' }));
    await jest.advanceTimersByTimeAsync(THIRTY_SECONDS);
    return call.error as ApiError;
  }
  const unreachable = () => new ApiError({ status: 0, detail: "Can't reach the API" });

  it('D7 sso.ts: a timed-out sign-in says it did not finish, not that the API is unreachable', async () => {
    const write = await timedOutWrite();
    expect(write.message).toBe(WRITE_TEXT);
    const path = exchangeFailurePath(write);
    expect(path).toBe('/login?sso_error=sso_timed_out');
    const params = readSsoErrorParams({ sso_error: new URLSearchParams(path.split('?')[1]).get('sso_error')! });
    expect(ssoErrorMessage(params!, { rateLimited: 'RATE', unreachable: 'UNREACHABLE' })).toBe(SIGN_IN_TEXT);
    // Positive control: an unreachable API keeps its own code and copy.
    expect(exchangeFailurePath(unreachable())).toBe('/login?sso_error=sso_unreachable');
  });

  it('D7 warehouseRuns.ts: a timed-out start says the run may or may not have started', async () => {
    const write = await timedOutWrite();
    expect(startRefusal(write)).toEqual({ code: 'timed_out', message: WRITE_TEXT });
    // Positive control: an unreachable API keeps its own code.
    expect(startRefusal(unreachable()).code).toBe('unreachable');
  });
});
