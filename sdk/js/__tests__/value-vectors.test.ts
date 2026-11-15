/**
 * The typed getters against `tests/sdk-contract/value-vectors.json` (#1810): for each answer of
 * `GET /api/v1/feature-flags/evaluate/{key}` in the file, what `getStringValue`, `getNumberValue`
 * or `getJsonValue` returns and what it passes to `onError`. The served half is generated from the
 * server's own answer builder (drift-checked in the backend suite); the malformed half is answers
 * no server sends.
 *
 * One test, not one per vector: it counts the vectors it answered and checked, and requires 49,
 * so an empty or truncated file fails here instead of running nothing.
 */
import { readFileSync } from 'fs';
import { join } from 'path';
import { isDeepStrictEqual } from 'util';
import { ExperimentationClient } from '../src/client';
import type { ExperimentationError } from '../src/errors';
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
const USER = { userId: 'value-vector-user' };

function load(): Vector[] {
  const text = readFileSync(VECTORS_PATH, 'utf8');
  const file = JSON.parse(text) as { served?: Vector[]; malformed?: Vector[] };
  return [...(file.served ?? []), ...(file.malformed ?? [])];
}

function read(client: ExperimentationClient, vector: Vector): Promise<unknown> {
  switch (vector.getter) {
    case 'string':
      return client.getStringValue('value-vector', USER, vector.default as string);
    case 'number':
      return client.getNumberValue('value-vector', USER, vector.default as number);
    case 'json':
      return client.getJsonValue('value-vector', USER, vector.default);
  }
}

test('the typed getters answer each value vector as the contract says', async () => {
  const vectors = load();
  const problems: string[] = [];
  let answered = 0;
  let asserted = 0;
  for (const vector of vectors) {
    const { status, body } = vector.response;
    // A real Response: the client parses the body's bytes, as it does a server's.
    global.fetch = jest
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }));
    const errors: Array<[ExperimentationError, SwallowedOperation]> = [];
    const client = new ExperimentationClient({
      apiUrl: 'https://api.example.com',
      apiKey: 'test-api-key',
      onError: (error, operation) => errors.push([error, operation]),
    });
    const got = await read(client, vector);
    answered += 1;

    if (vector.expect === 'default') {
      if (!Object.is(got, vector.default)) problems.push(`${vector.name}: got ${JSON.stringify(got)}, expected the default`);
    } else if (!isDeepStrictEqual(got, vector.expect)) {
      problems.push(`${vector.name}: got ${JSON.stringify(got)}, expected ${JSON.stringify(vector.expect)}`);
    }
    const reports = errors.filter(([, operation]) => operation === 'value').length;
    if (reports !== errors.length) problems.push(`${vector.name}: reported as ${errors.map(([, op]) => op).join()}`);
    if (reports !== (vector.reported ? 1 : 0)) {
      problems.push(`${vector.name}: reported ${reports}, expected ${vector.reported ? 1 : 0}`);
    }
    // A null code is not compared (this SDK reports a 404 or a 503 as HTTP_ERROR).
    if (vector.error_code !== null && errors[0]?.[0].code !== vector.error_code) {
      problems.push(`${vector.name}: code ${String(errors[0]?.[0].code)}, expected ${vector.error_code}`);
    }
    asserted += 1;
  }
  expect(problems).toEqual([]);
  expect(`answered ${answered} of ${EXPECTED}, asserted ${asserted}`).toBe(
    `answered ${EXPECTED} of ${EXPECTED}, asserted ${EXPECTED}`
  );
});
