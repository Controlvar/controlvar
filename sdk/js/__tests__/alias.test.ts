/**
 * `alias(anonymousId, userId)` (#449): one `POST /api/v1/tracking/alias` with the client's key.
 *
 * - It resolves `created`; any other answer rejects with `ExperimentationError` carrying the
 *   server's status and body, as `getAssignment` does, and a 429 is retried once.
 * - On a 200 the client copies the anonymous id's experiment keys (never flag keys) to the known
 *   id, read through `keys()`, with the 24 hours starting at the alias, and drops the known id's
 *   cached assignments. The anonymous id's own events keep the anonymous id.
 *
 * `fetch` is mocked: no network.
 */
import { ExperimentationClient } from '../src/client';
import { ExperimentationError } from '../src/errors';
import type { AssignResponse, ClientConfig, FlagEvaluateResponse } from '../src/types';

const baseConfig: ClientConfig = { apiUrl: 'https://api.example.com', apiKey: 'server-key' };
const expectedHeaders = {
  'X-API-Key': 'server-key',
  'Content-Type': 'application/json',
  Accept: 'application/json',
};

const ANON = 'visitor-7f3a92';
const KNOWN = 'customer-1042';
const T0 = 1_700_000_000_000;
const HOUR = 3_600_000;

function assigned(userId: string, experimentKey = 'pricing', variant = 'treatment', reason = 'assigned'): AssignResponse {
  return {
    experiment_key: experimentKey,
    user_id: userId,
    variant_id: `var-${variant}`,
    variant_name: variant,
    is_control: variant === 'control',
    configuration: null,
    assigned: reason === 'assigned',
    reason,
  };
}
const flagOn: FlagEvaluateResponse = { key: 'new_search', enabled: true, config: null };
const linked = (created: boolean) => ({ anonymous_id: ANON, user_id: KNOWN, created });

interface Step {
  body?: unknown;
  status?: number;
  headers?: Record<string, string>;
}

function jsonResponse(step: Step) {
  const status = step.status ?? 200;
  const headers = step.headers ?? {};
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => headers[name] ?? headers[name.toLowerCase()] ?? null },
    json: () => Promise.resolve(step.body ?? {}),
  };
}

/** Queue responses in order; an Error entry rejects (network failure). */
function mockFetchSequence(...steps: Array<Step | Error>): jest.Mock {
  const mock = jest.fn();
  for (const step of steps) {
    if (step instanceof Error) mock.mockRejectedValueOnce(step);
    else mock.mockResolvedValueOnce(jsonResponse(step));
  }
  global.fetch = mock;
  return mock;
}

function call(mock: jest.Mock, index = 0) {
  const [url, init] = mock.mock.calls[index];
  return { url: url as string, init, path: new URL(url as string).pathname, body: init?.body ? JSON.parse(init.body) : undefined };
}

async function captureError(promise: Promise<unknown>): Promise<ExperimentationError> {
  try {
    await promise;
  } catch (err) {
    return err as ExperimentationError;
  }
  throw new Error('expected the promise to reject');
}

afterEach(() => {
  jest.restoreAllMocks();
});

// ─── IA-SDK1: the request ─────────────────────────────────────────────────────

describe('alias: the request', () => {
  it('sends one POST /api/v1/tracking/alias with the two ids in the body and the client key', async () => {
    const fetchMock = mockFetchSequence({ body: linked(true) });
    const client = new ExperimentationClient(baseConfig);
    await client.alias(ANON, KNOWN);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const sent = call(fetchMock);
    expect(sent.url).toBe('https://api.example.com/api/v1/tracking/alias');
    expect(sent.init.method).toBe('POST');
    expect(sent.init.headers).toEqual(expectedHeaders);
    expect(sent.body).toEqual({ anonymous_id: ANON, user_id: KNOWN });
  });

  it('resolves created as the server answered it: true, then false for the same link', async () => {
    mockFetchSequence({ body: linked(true) }, { body: linked(false) });
    const client = new ExperimentationClient(baseConfig);
    await expect(client.alias(ANON, KNOWN)).resolves.toBe(true);
    await expect(client.alias(ANON, KNOWN)).resolves.toBe(false);
  });

  it('rejects a 200 whose body has no boolean created with INVALID_RESPONSE', async () => {
    mockFetchSequence({ body: { anonymous_id: ANON, user_id: KNOWN } });
    const error = await captureError(new ExperimentationClient(baseConfig).alias(ANON, KNOWN));
    expect(error).toBeInstanceOf(ExperimentationError);
    expect(error.code).toBe('INVALID_RESPONSE');
  });
});

// ─── IA-SDK2: every other answer reaches the caller ───────────────────────────

describe('alias: a failure reaches the caller', () => {
  const NOT_SERVER_SIDE =
    "This API key does not have the 'sdk:ruleset' scope. Create a key with the 'sdk:ruleset' scope for server-side SDK use.";
  const LINKED_ELSEWHERE =
    'That anonymous_id is already linked to a different user_id. An anonymous_id can be linked to one user_id.';
  const EQUAL = [{ type: 'equal_ids', loc: ['body'], msg: 'anonymous_id and user_id must differ.' }];

  it.each([
    ['404 from a server without the route', 404, { detail: 'Not Found' }],
    ['403 for a key that is not a server-side key', 403, { detail: NOT_SERVER_SIDE }],
    ['409 when a rule refuses the link', 409, { detail: LINKED_ELSEWHERE }],
    ['422 for an id the server refuses', 422, { detail: EQUAL }],
  ])('rejects a %s with ExperimentationError carrying the status and the server body', async (_name, status, body) => {
    const fetchMock = mockFetchSequence({ status, body });
    const error = await captureError(new ExperimentationClient(baseConfig).alias(ANON, KNOWN));
    expect(error).toBeInstanceOf(ExperimentationError);
    expect(error.code).toBe('HTTP_ERROR');
    expect(error.status).toBe(status);
    expect(error.body).toEqual(body);
    expect(error.message).toContain(`API error ${status}`);
    expect(error.message).toContain('(POST /api/v1/tracking/alias)');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('rejects a network failure with NETWORK_ERROR and no status, and does not send it again', async () => {
    const fetchMock = mockFetchSequence(new TypeError('fetch failed'));
    const error = await captureError(new ExperimentationClient(baseConfig).alias(ANON, KNOWN));
    expect(error).toBeInstanceOf(ExperimentationError);
    expect(error.code).toBe('NETWORK_ERROR');
    expect(error.status).toBeUndefined();
    expect(error.message).toBe('Network error (POST /api/v1/tracking/alias): fetch failed');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('rejects a timeout with TIMEOUT and no status', async () => {
    jest.useFakeTimers();
    global.fetch = jest.fn().mockImplementation((_url: string, init: RequestInit) => {
      return new Promise((_resolve, reject) => {
        (init.signal as AbortSignal).addEventListener('abort', () => reject(new Error('The operation was aborted')));
      });
    });
    const pending = new ExperimentationClient({ ...baseConfig, timeoutMs: 1_000 }).alias(ANON, KNOWN);
    const caught = captureError(pending);
    await jest.advanceTimersByTimeAsync(1_000);
    const error = await caught;
    jest.useRealTimers();
    expect(error).toBeInstanceOf(ExperimentationError);
    expect(error.code).toBe('TIMEOUT');
    expect(error.status).toBeUndefined();
  });

  it('retries a 429 once, as every call does, and resolves the answer to the retry', async () => {
    const fetchMock = mockFetchSequence({ status: 429, headers: { 'Retry-After': '0' } }, { body: linked(true) });
    await expect(new ExperimentationClient(baseConfig).alias(ANON, KNOWN)).resolves.toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(call(fetchMock, 1).body).toEqual(call(fetchMock, 0).body);
  });

  it('rejects a 429 that is still a 429 after its one retry', async () => {
    mockFetchSequence({ status: 429, headers: { 'Retry-After': '0' } }, { status: 429, headers: { 'Retry-After': '0' } });
    const error = await captureError(new ExperimentationClient(baseConfig).alias(ANON, KNOWN));
    expect(error.status).toBe(429);
  });

  it('keeps nothing of a failed alias: calling it again sends it again, and nothing was copied', async () => {
    const fetchMock = mockFetchSequence(
      { body: assigned(ANON) },
      { status: 409, body: { detail: LINKED_ELSEWHERE } },
      { body: {} },
      { body: linked(true) }
    );
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('pricing', { userId: ANON });
    await expect(client.alias(ANON, KNOWN)).rejects.toBeInstanceOf(ExperimentationError);
    // Nothing was copied: the known id's key-less event has nothing to name.
    await client.track(KNOWN, 'purchase');
    expect(call(fetchMock, 2).path).toBe('/api/v1/tracking/track');
    expect(call(fetchMock, 2).body.experiment_key).toBeUndefined();
    await expect(client.alias(ANON, KNOWN)).resolves.toBe(true);
    expect(call(fetchMock, 3).path).toBe('/api/v1/tracking/alias');
  });
});

// ─── IA-SDK3: the attribution record follows the alias ────────────────────────

describe('alias: the key-less fan-out follows the link', () => {
  it('after alias, a key-less track for the known id carries the experiment key the anonymous id was assigned', async () => {
    const fetchMock = mockFetchSequence({ body: assigned(ANON) }, { body: linked(true) }, { body: { success_count: 1 } });
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('pricing', { userId: ANON });
    await client.alias(ANON, KNOWN);
    await client.track(KNOWN, 'purchase', { value: 30 });
    const sent = call(fetchMock, 2);
    expect(sent.path).toBe('/api/v1/tracking/batch');
    expect(sent.body.events).toEqual([
      { event_type: 'purchase', event_name: 'purchase', user_id: KNOWN, value: 30, experiment_key: 'pricing' },
    ]);
  });

  it('copies experiment keys only, never flag keys: flags are not linked', async () => {
    const fetchMock = mockFetchSequence(
      { body: assigned(ANON) },
      { body: flagOn },
      { body: linked(true) },
      { body: { success_count: 1 } }
    );
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('pricing', { userId: ANON });
    await client.evaluateFlag('new_search', { userId: ANON });
    await client.alias(ANON, KNOWN);
    await client.track(KNOWN, 'purchase');
    const events = call(fetchMock, 3).body.events as Array<Record<string, unknown>>;
    expect(events.map(e => [e.experiment_key, e.feature_flag_key])).toEqual([['pricing', undefined]]);
  });

  it("copies what keys() gives: not an experiment whose latest answer was paused, nor one past its 24 hours", async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence(
      { body: assigned(ANON, 'old_test') },
      { body: assigned(ANON, 'paused_test', 'control', 'paused') },
      { body: assigned(ANON, 'pricing') },
      { body: linked(true) },
      { body: { success_count: 1 } }
    );
    const client = new ExperimentationClient({ ...baseConfig, cacheTtlMs: 0 });
    await client.getAssignment('old_test', { userId: ANON });
    now.mockReturnValue(T0 + 25 * HOUR);
    await client.getAssignment('paused_test', { userId: ANON });
    await client.getAssignment('pricing', { userId: ANON });
    await client.alias(ANON, KNOWN);
    await client.track(KNOWN, 'purchase');
    const events = call(fetchMock, 4).body.events as Array<Record<string, unknown>>;
    expect(events.map(e => e.experiment_key)).toEqual(['pricing']);
  });

  it("the copied key's 24 hours start at the alias, not at the anonymous id's answer", async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(T0);
    const fetchMock = mockFetchSequence(
      { body: assigned(ANON) },
      { body: linked(true) },
      { body: { success_count: 1 } },
      { body: {} }
    );
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('pricing', { userId: ANON });
    now.mockReturnValue(T0 + 23 * HOUR);
    await client.alias(ANON, KNOWN);
    // 25 hours after the anonymous id's answer, 2 after the alias: still sent to the experiment.
    now.mockReturnValue(T0 + 25 * HOUR);
    await client.track(KNOWN, 'purchase');
    expect(call(fetchMock, 2).path).toBe('/api/v1/tracking/batch');
    expect(call(fetchMock, 2).body.events[0].experiment_key).toBe('pricing');
    // More than 24 hours after the alias: the key is gone.
    now.mockReturnValue(T0 + 23 * HOUR + 24 * HOUR + 1);
    await client.track(KNOWN, 'purchase');
    expect(call(fetchMock, 3).path).toBe('/api/v1/tracking/track');
    expect(call(fetchMock, 3).body.experiment_key).toBeUndefined();
  });

  it('helps only the client that assigned the anonymous id: on another client, getAssignment(known) first', async () => {
    const fetchMock = mockFetchSequence(
      { body: linked(true) },
      { body: {} },
      { body: assigned(KNOWN) },
      { body: { success_count: 1 } }
    );
    const backend = new ExperimentationClient(baseConfig); // never answered the anonymous id
    await backend.alias(ANON, KNOWN);
    await backend.track(KNOWN, 'purchase');
    expect(call(fetchMock, 1).path).toBe('/api/v1/tracking/track');
    expect(call(fetchMock, 1).body.experiment_key).toBeUndefined();
    await backend.getAssignment('pricing', { userId: KNOWN });
    await backend.track(KNOWN, 'purchase');
    expect(call(fetchMock, 3).path).toBe('/api/v1/tracking/batch');
    expect(call(fetchMock, 3).body.events[0].experiment_key).toBe('pricing');
  });
});

// ─── IA-SDK4: no stale cache for the known id ─────────────────────────────────

describe('alias: the known id is asked again', () => {
  it("drops the known id's cached assignments, so its next getAssignment asks the server", async () => {
    const fetchMock = mockFetchSequence(
      { body: assigned(KNOWN, 'pricing', 'control') },
      { body: linked(true) },
      { body: assigned(KNOWN, 'pricing', 'treatment') }
    );
    const client = new ExperimentationClient(baseConfig);
    await expect(client.getAssignment('pricing', { userId: KNOWN })).resolves.toMatchObject({ variantName: 'control' });
    await client.alias(ANON, KNOWN);
    expect(client.getAssignments(KNOWN)).toEqual([]);
    await expect(client.getAssignment('pricing', { userId: KNOWN })).resolves.toMatchObject({ variantName: 'treatment' });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("keeps the anonymous id's cached assignments and the known id's cached flags", async () => {
    const fetchMock = mockFetchSequence(
      { body: assigned(ANON) },
      { body: flagOn },
      { body: linked(true) }
    );
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('pricing', { userId: ANON });
    await client.evaluateFlag('new_search', { userId: KNOWN });
    await client.alias(ANON, KNOWN);
    await expect(client.getAssignment('pricing', { userId: ANON })).resolves.toMatchObject({ variantName: 'treatment' });
    await expect(client.evaluateFlag('new_search', { userId: KNOWN })).resolves.toMatchObject({ enabled: true });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("drops nothing when the alias fails: the known id's cached assignment is still served", async () => {
    const fetchMock = mockFetchSequence(
      { body: assigned(KNOWN, 'pricing', 'control') },
      { status: 403, body: { detail: 'no scope' } }
    );
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('pricing', { userId: KNOWN });
    await expect(client.alias(ANON, KNOWN)).rejects.toBeInstanceOf(ExperimentationError);
    await expect(client.getAssignment('pricing', { userId: KNOWN })).resolves.toMatchObject({ variantName: 'control' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

// ─── IA-SDK5: events are not rewritten ────────────────────────────────────────

// The client has no queue: an event is built and sent when track() is called. So what is pinned
// is that the link changes nothing about the anonymous id's own events.
describe('alias: events keep the id they were sent with', () => {
  it("after alias, the anonymous id's own events keep the anonymous id and its experiment", async () => {
    const fetchMock = mockFetchSequence(
      { body: assigned(ANON) },
      { body: linked(true) },
      { body: { success_count: 1 } },
      { body: { success_count: 1, failure_count: 0, errors: [] } }
    );
    const client = new ExperimentationClient(baseConfig);
    await client.getAssignment('pricing', { userId: ANON });
    await client.alias(ANON, KNOWN);
    await client.track(ANON, 'page_view');
    await client.trackBatch([{ userId: ANON, eventName: 'scroll' }]);
    expect(call(fetchMock, 2).body.events).toEqual([
      { event_type: 'page_view', event_name: 'page_view', user_id: ANON, experiment_key: 'pricing' },
    ]);
    expect(call(fetchMock, 3).body.events).toEqual([
      { event_type: 'scroll', event_name: 'scroll', user_id: ANON, experiment_key: 'pricing' },
    ]);
  });
});
