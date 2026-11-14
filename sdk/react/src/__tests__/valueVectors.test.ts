/**
 * `useFlagValue` against `tests/sdk-contract/value-vectors.json` (#1810): for each answer of
 * `GET /api/v1/feature-flags/evaluate/{key}` in the file, what the hook returns as `value` and
 * whether it sets `error`. Each body is served raw through a mocked `fetch`, so the client parses
 * it and maps it with `evaluationFromResponse`, exactly as it does a server's answer. The served
 * half is generated from the server's own answer builder (drift-checked in the backend suite);
 * the malformed half is answers no server sends.
 *
 * The hook's `error` is the only place a fallback shows: `reported` is "`error` is set". A
 * vector's `error_code` is compared with `error.code` when it is not null; a null code is not
 * compared (this SDK reports a 404 or a 503 as `REQUEST_FAILED`).
 *
 * One test, not one per vector: it counts the vectors it answered and checked, and requires 49,
 * so an empty or truncated file fails here instead of running nothing.
 */
import React from 'react';
import { readFileSync } from 'fs';
import { join } from 'path';
import { isDeepStrictEqual } from 'util';
import { renderHook, waitFor } from '@testing-library/react';
import { ExperimentationProvider } from '../context/ExperimentationProvider';
import { useFlagValue } from '../hooks/useFlagValue';
import { FlagValueError } from '../client/flagValue';
import type { SdkConfig } from '../client/types';

interface Vector {
  name: string;
  response: { status: number; body: unknown };
  getter: 'string' | 'number' | 'json';
  default: unknown;
  expect: unknown;
  error_code: string | null;
  reported: boolean;
}

const VECTORS_PATH = join(__dirname, '..', '..', '..', '..', 'tests', 'sdk-contract', 'value-vectors.json');
/** Pinned: 35 served + 14 malformed (backend/tests/unit/services/test_sdk_value_vectors.py). */
const EXPECTED = 49;
const FLAG_KEY = 'value-vector';
const USER = { userId: 'value-vector-user' };
const config: SdkConfig = { apiKey: 'test-key', baseUrl: 'https://api.example.com' };

function load(): Vector[] {
  const text = readFileSync(VECTORS_PATH, 'utf8');
  const file = JSON.parse(text) as { served?: Vector[]; malformed?: Vector[] };
  return [...(file.served ?? []), ...(file.malformed ?? [])];
}

/** A response holding `body`'s bytes: the client parses them, as it parses a server's. */
function rawResponse(status: number, body: unknown) {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(JSON.parse(text)),
    text: () => Promise.resolve(text),
  };
}

function read(vector: Vector) {
  switch (vector.getter) {
    case 'string':
      return useFlagValue(FLAG_KEY, 'string', vector.default as string);
    case 'number':
      return useFlagValue(FLAG_KEY, 'number', vector.default as number);
    case 'json':
      return useFlagValue(FLAG_KEY, 'json', vector.default);
  }
}

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(ExperimentationProvider, { config, user: USER, children });
}

afterEach(() => {
  jest.restoreAllMocks();
});

test('useFlagValue answers each value vector as the contract says', async () => {
  const vectors = load();
  const problems: string[] = [];
  let answered = 0;
  let asserted = 0;
  for (const vector of vectors) {
    const { status, body } = vector.response;
    const fetchMock = jest.fn().mockResolvedValue(rawResponse(status, body));
    global.fetch = fetchMock;
    // A new Provider per vector: a new client, so nothing is answered from another vector's cache.
    const { result, unmount } = renderHook(() => read(vector), { wrapper });
    await waitFor(() => expect(result.current.loading).toBe(false));
    const { value: got, error } = result.current;
    unmount();
    answered += 1;

    if (fetchMock.mock.calls.length !== 1) {
      problems.push(`${vector.name}: ${fetchMock.mock.calls.length} requests, expected 1`);
    } else if (!String(fetchMock.mock.calls[0][0]).includes(`/api/v1/feature-flags/evaluate/${FLAG_KEY}?`)) {
      problems.push(`${vector.name}: asked ${String(fetchMock.mock.calls[0][0])}`);
    }
    if (vector.expect === 'default') {
      if (!Object.is(got, vector.default)) problems.push(`${vector.name}: got ${JSON.stringify(got)}, expected the default`);
    } else if (!isDeepStrictEqual(got, vector.expect)) {
      problems.push(`${vector.name}: got ${JSON.stringify(got)}, expected ${JSON.stringify(vector.expect)}`);
    }
    const reports = error === null ? 0 : 1;
    if (reports !== (vector.reported ? 1 : 0)) {
      problems.push(`${vector.name}: reported ${reports}, expected ${vector.reported ? 1 : 0}`);
    }
    if (error !== null && !(error instanceof FlagValueError)) {
      problems.push(`${vector.name}: error is ${String(error)}, not a FlagValueError`);
    }
    if (vector.error_code !== null && error?.code !== vector.error_code) {
      problems.push(`${vector.name}: code ${String(error?.code)}, expected ${vector.error_code}`);
    }
    asserted += 1;
  }
  expect(problems).toEqual([]);
  expect(`answered ${answered} of ${EXPECTED}, asserted ${asserted}`).toBe(
    `answered ${EXPECTED} of ${EXPECTED}, asserted ${EXPECTED}`
  );
}, 60000);
