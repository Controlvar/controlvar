/**
 * The typed getters and `useFlagValue` against `tests/sdk-contract/value-vectors.json` (#1810):
 * for each answer of `GET /api/v1/feature-flags/evaluate/{key}` in the file, what
 * `getStringValue`, `getNumberValue` or `getJsonValue` returns and what it passes to `onError`,
 * and what the hook returns for the same answer. Each body is served raw through a mocked
 * `fetch`, so the client parses it and maps it with `evaluateFlag`'s own code, exactly as it does
 * a server's answer. The served half is generated from the server's own answer builder
 * (drift-checked in the backend suite); the malformed half is answers no server sends.
 *
 * `reported` is "`onError` was called once, with `'value'`" (and, for the hook, "`error` is
 * set"). A vector's `error_code` is compared when it is not null; a null code is not compared
 * (this SDK passes a 404 or a 503 to `onError` as the request's own `ApiError`, and the hook's
 * `error` has code `REQUEST_FAILED`).
 *
 * One test, not one per vector: it counts the vectors it answered and checked, and requires 49,
 * so an empty or truncated file fails here instead of running nothing.
 */
import React from 'react';
import { Text } from 'react-native';
import { readFileSync } from 'fs';
import { join } from 'path';
import { isDeepStrictEqual } from 'util';
import { render, waitFor } from '@testing-library/react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { ExperimentationClient, FlagValueError } from '../src/client';
import { ExperimentationProvider } from '../src/context/ExperimentationProvider';
import { useFlagValue } from '../src/hooks/useFlagValue';
import type { FlagValueResult } from '../src/hooks/useFlagValue';
import type { SwallowedOperation } from '../src/types';

interface Vector {
  name: string;
  response: { status: number; body: unknown };
  getter: 'string' | 'number' | 'json';
  default: unknown;
  expect: unknown;
  error_code: string | null;
  reported: boolean;
}

const VECTORS_PATH = join(__dirname, '..', '..', '..', 'tests', 'sdk-contract', 'value-vectors.json');
/** Pinned: 35 served + 14 malformed (backend/tests/unit/services/test_sdk_value_vectors.py). */
const EXPECTED = 49;
const FLAG_KEY = 'value-vector';
const USER_ID = 'value-vector-user';

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

function getter(client: ExperimentationClient, vector: Vector): Promise<unknown> {
  switch (vector.getter) {
    case 'string':
      return client.getStringValue(FLAG_KEY, USER_ID, vector.default as string);
    case 'number':
      return client.getNumberValue(FLAG_KEY, USER_ID, vector.default as number);
    case 'json':
      return client.getJsonValue(FLAG_KEY, USER_ID, vector.default);
  }
}

function hook(vector: Vector): FlagValueResult<unknown> {
  switch (vector.getter) {
    case 'string':
      return useFlagValue(FLAG_KEY, 'string', vector.default as string);
    case 'number':
      return useFlagValue(FLAG_KEY, 'number', vector.default as number);
    case 'json':
      return useFlagValue(FLAG_KEY, 'json', vector.default);
  }
}

/** The vector's problems with one answer, `got`, and the reports that came with it. */
function judge(
  label: string,
  vector: Vector,
  got: unknown,
  reports: Array<[Error, SwallowedOperation]>
): string[] {
  const problems: string[] = [];
  if (vector.expect === 'default') {
    if (!Object.is(got, vector.default)) problems.push(`${label}: got ${JSON.stringify(got)}, expected the default`);
  } else if (!isDeepStrictEqual(got, vector.expect)) {
    problems.push(`${label}: got ${JSON.stringify(got)}, expected ${JSON.stringify(vector.expect)}`);
  }
  const values = reports.filter(([, operation]) => operation === 'value').length;
  if (values !== reports.length) problems.push(`${label}: reported as ${reports.map(([, op]) => op).join()}`);
  if (values !== (vector.reported ? 1 : 0)) {
    problems.push(`${label}: reported ${values}, expected ${vector.reported ? 1 : 0}`);
  }
  return problems;
}

beforeEach(async () => {
  jest.restoreAllMocks();
  await AsyncStorage.clear();
});

it('the typed getters and useFlagValue answer each value vector as the contract says', async () => {
  const vectors = load();
  const problems: string[] = [];
  let answered = 0;
  let asserted = 0;
  for (const vector of vectors) {
    const { status, body } = vector.response;

    // The getter: a new client (the default offlineFallback) over an empty AsyncStorage.
    await AsyncStorage.clear();
    const fetchMock = jest.fn().mockResolvedValue(rawResponse(status, body));
    global.fetch = fetchMock as unknown as typeof fetch;
    const reports: Array<[Error, SwallowedOperation]> = [];
    const client = new ExperimentationClient({
      apiKey: 'test-key',
      baseUrl: 'https://api.example.com',
      onError: (error, operation) => void reports.push([error, operation]),
    });
    const got = await getter(client, vector);
    answered += 1;

    problems.push(...judge(`${vector.name} (getter)`, vector, got, reports));
    if (fetchMock.mock.calls.length !== 1) {
      problems.push(`${vector.name}: ${fetchMock.mock.calls.length} requests, expected 1`);
    } else if (!String(fetchMock.mock.calls[0][0]).includes(`/api/v1/feature-flags/evaluate/${FLAG_KEY}?`)) {
      problems.push(`${vector.name}: asked ${String(fetchMock.mock.calls[0][0])}`);
    }
    // A request that failed is passed on as itself; otherwise the reason is a FlagValueError.
    const [reported] = reports;
    if (reported && status >= 200 && status < 300 && !(reported[0] instanceof FlagValueError)) {
      problems.push(`${vector.name}: reported ${String(reported[0])}, not a FlagValueError`);
    }
    if (vector.error_code !== null && (reported?.[0] as FlagValueError | undefined)?.code !== vector.error_code) {
      problems.push(
        `${vector.name}: code ${String((reported?.[0] as FlagValueError | undefined)?.code)}, expected ${vector.error_code}`
      );
    }

    // The hook: a new client over an empty AsyncStorage, the same raw answer.
    await AsyncStorage.clear();
    global.fetch = jest.fn().mockResolvedValue(rawResponse(status, body)) as unknown as typeof fetch;
    const hookReports: Array<[Error, SwallowedOperation]> = [];
    const hookClient = new ExperimentationClient({
      apiKey: 'test-key',
      baseUrl: 'https://api.example.com',
      onError: (error, operation) => void hookReports.push([error, operation]),
    });
    const states: Array<FlagValueResult<unknown>> = [];
    function Probe() {
      states.push(hook(vector));
      return React.createElement(Text, null, 'probe');
    }
    const { unmount } = render(
      React.createElement(ExperimentationProvider, {
        client: hookClient,
        userId: USER_ID,
        children: React.createElement(Probe),
      })
    );
    await waitFor(() => expect(states[states.length - 1].loading).toBe(false));
    const { value: hooked, error } = states[states.length - 1];
    unmount();

    problems.push(...judge(`${vector.name} (hook)`, vector, hooked, hookReports));
    if ((error !== null) !== vector.reported) {
      problems.push(`${vector.name} (hook): error ${String(error)}, expected ${vector.reported ? 'one' : 'none'}`);
    }
    if (error !== null && !(error instanceof FlagValueError)) {
      problems.push(`${vector.name} (hook): error is ${String(error)}, not a FlagValueError`);
    }
    if (vector.error_code !== null && error?.code !== vector.error_code) {
      problems.push(`${vector.name} (hook): code ${String(error?.code)}, expected ${vector.error_code}`);
    }
    asserted += 1;
  }
  expect(problems).toEqual([]);
  expect(`answered ${answered} of ${EXPECTED}, asserted ${asserted}`).toBe(
    `answered ${EXPECTED} of ${EXPECTED}, asserted ${EXPECTED}`
  );
}, 60000);
