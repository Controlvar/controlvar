/**
 * The local evaluator against `tests/sdk-contract/ruleset-vectors.json`, which the server
 * generates from its own ruleset builder and flag evaluator (drift-checked in the backend suite).
 *
 * The property: for every case the SDK either defers to the server or gives exactly the server's
 * `{enabled, reason}`; every `must_local` case is answered locally; and nothing else is. The
 * number of local answers is pinned, so an SDK that defers everything fails.
 */
import { createHash } from 'crypto';
import { readFileSync } from 'fs';
import { join } from 'path';
import {
  BUCKETING,
  DEFER,
  LOCAL_OPERATORS,
  MAX_SAFE_INTEGER,
  RULESET_SCHEMA,
  WHITESPACE_CODE_POINTS,
  evaluateLocally,
  flagBucket,
  flattenContext,
  indexRuleset,
  isWellFormedString,
  toWireContext,
} from '../src/evaluator';
import { consistentHash } from '../src/hash';

interface VectorCase {
  id: string;
  flag: string;
  user_id: string;
  context: Record<string, unknown> | null;
  expected: { enabled: boolean; reason: string };
  must_local: boolean;
}

interface Vectors {
  schema: number;
  bucketing: string;
  local_operators: string[];
  max_safe_integer: number;
  whitespace_code_points: number[];
  counts: { cases: number; flags: number; must_local: number };
  ruleset: unknown;
  cases: VectorCase[];
}

const VECTORS_PATH = join(__dirname, '..', '..', '..', 'tests', 'sdk-contract', 'ruleset-vectors.json');
const vectors = JSON.parse(readFileSync(VECTORS_PATH, 'utf8')) as Vectors;

/**
 * Pinned. A change here is a change to what this SDK must answer locally: it moves only with a
 * regenerated vectors file (and the backend pin in test_sdk_ruleset_vectors.py).
 */
const EXPECTED_COUNTS = { cases: 2191, flags: 149, must_local: 1291 };

/**
 * A case's context is what the server received, so it is evaluated as parsed, not round-tripped
 * again: `10**400` parses to `Infinity` here (which must defer), whereas a JS caller can never
 * send it -- `JSON.stringify` would send `null`, which the client-level test covers.
 */
function answer(testCase: VectorCase) {
  const ruleset = indexRuleset(vectors.ruleset);
  if (!ruleset) throw new Error('the vectors ruleset does not index');
  return evaluateLocally(ruleset, testCase.flag, testCase.user_id, testCase.context);
}

describe('the local domain is the one in the vectors file', () => {
  test('the counts are pinned', () => {
    expect(vectors.counts).toEqual(EXPECTED_COUNTS);
    expect(vectors.cases).toHaveLength(EXPECTED_COUNTS.cases);
    expect(vectors.cases.filter(c => c.must_local)).toHaveLength(EXPECTED_COUNTS.must_local);
  });

  test('the local operator list is the same list', () => {
    expect([...LOCAL_OPERATORS]).toEqual(vectors.local_operators);
  });

  test('the whitespace list is the same list', () => {
    expect([...WHITESPACE_CODE_POINTS]).toEqual(vectors.whitespace_code_points);
    expect(WHITESPACE_CODE_POINTS).toHaveLength(30);
  });

  test('the schema, bucketing and number limit are the same', () => {
    expect(RULESET_SCHEMA).toBe(vectors.schema);
    expect(BUCKETING).toBe(vectors.bucketing);
    expect(MAX_SAFE_INTEGER).toBe(vectors.max_safe_integer);
    expect(MAX_SAFE_INTEGER).toBe(Number.MAX_SAFE_INTEGER);
  });
});

describe('differential: every case is deferred or answered exactly as the server did', () => {
  const results = vectors.cases.map(testCase => ({ testCase, result: answer(testCase) }));

  test('no local answer differs from the server', () => {
    const wrong = results
      .filter(({ result }) => result !== DEFER)
      .filter(({ testCase, result }) => JSON.stringify(result) !== JSON.stringify(testCase.expected))
      .map(({ testCase, result }) => `${testCase.id}: local ${JSON.stringify(result)}, server ${JSON.stringify(testCase.expected)}`);
    expect(wrong).toEqual([]);
  });

  test('every must_local case is answered locally', () => {
    const missed = results.filter(({ testCase, result }) => testCase.must_local && result === DEFER).map(r => r.testCase.id);
    expect(missed).toEqual([]);
  });

  test('nothing outside must_local is answered locally', () => {
    const extra = results.filter(({ testCase, result }) => !testCase.must_local && result !== DEFER).map(r => r.testCase.id);
    expect(extra).toEqual([]);
  });

  test('the number of local answers is exactly must_local', () => {
    expect(results.filter(({ result }) => result !== DEFER)).toHaveLength(EXPECTED_COUNTS.must_local);
  });
});

describe('named classes', () => {
  const find = (flag: string, context: unknown, userId = 'user-1'): VectorCase => {
    const form = JSON.stringify(context);
    const matches = vectors.cases.filter(
      c => c.flag === flag && c.user_id === userId && JSON.stringify(c.context) === form
    );
    expect(matches).toHaveLength(1);
    return matches[0];
  };

  test.each([
    // no short-circuit: the server evaluates the out-of-domain sibling and fails
    ['no-short-circuit-and', { b: 'yes', a: Infinity }],
    ['no-short-circuit-or', { b: 'yes', a: Infinity }],
    // numbers past 2^53-1 and non-portable types
    ['gt-0', { a: 2 ** 53 }],
    ['gt-0', { a: '10' }],
    ['eq-num-0', { a: true }],
    // whitespace the two languages disagree on
    ['presence-is_null', { a: '﻿' }],
    ['presence-is_null', { a: '\x1c' }],
    ['presence-is_null', { a: '\x85' }],
  ])('%s %j defers', (flag, context) => {
    const ruleset = indexRuleset(vectors.ruleset)!;
    expect(evaluateLocally(ruleset, flag, 'user-1', context)).toBe(DEFER);
  });

  test('prototype names are ordinary attributes', () => {
    const testCase = find('proto-dunder', { __proto__: 'x' });
    expect(testCase.must_local).toBe(true);
    expect(answer(testCase)).toEqual(testCase.expected);
    const flat = flattenContext(JSON.parse('{"__proto__": "x"}'));
    expect(flat.get('__proto__')).toBe('x');
    expect(flattenContext({}).has('constructor')).toBe(false);
  });

  test('a lone surrogate anywhere defers', () => {
    expect(isWellFormedString('user-\ud800')).toBe(false);
    expect(isWellFormedString('\udc00')).toBe(false);
    expect(isWellFormedString('ok 😀')).toBe(true);
    const ruleset = indexRuleset(vectors.ruleset)!;
    expect(evaluateLocally(ruleset, 'rollout-50', 'user-\ud800', null)).toBe(DEFER);
    expect(evaluateLocally(ruleset, 'unicode', 'user-1', { name: 'été', '\ud800': 'x' })).toBe(DEFER);
  });

  test('an unknown flag key defers (it may be newer than the ruleset)', () => {
    const ruleset = indexRuleset(vectors.ruleset)!;
    expect(evaluateLocally(ruleset, 'no-such-flag', 'user-1', null)).toBe(DEFER);
  });

  test('a ruleset in another format evaluates nothing', () => {
    const base = vectors.ruleset as Record<string, unknown>;
    expect(indexRuleset({ ...base, schema: 2 })).toBeNull();
    expect(indexRuleset({ ...base, bucketing: 'md5-mod100-v2' })).toBeNull();
  });
});

describe('md5-mod100-v1 bucketing', () => {
  const serverBucket = (userId: string, flagKey: string) =>
    Number(BigInt(`0x${createHash('md5').update(`${userId}:${flagKey}`, 'utf8').digest('hex')}`) % BigInt(100));

  test('is the server bucket: the whole digest, big-endian, mod 100', () => {
    expect(flagBucket('user-123', 'my-flag')).toBe(79);
    for (let i = 0; i < 2000; i++) {
      const userId = `user-${i}-é-${i * 7919}`;
      const flagKey = `flag-${i % 13}`;
      expect(flagBucket(userId, flagKey)).toBe(serverBucket(userId, flagKey));
    }
  });

  test('is not the cross-SDK consistentHash', () => {
    expect(Math.floor(consistentHash('user-123', 'my-flag') * 100)).toBe(69);
    expect(flagBucket('user-123', 'my-flag')).not.toBe(69);
  });
});

describe('the wire context', () => {
  test('is the JSON round trip of the attributes', () => {
    const date = new Date('2026-01-02T03:04:05.000Z');
    expect(toWireContext({ a: undefined, b: date, c: NaN, d: [1, undefined] })).toEqual({
      b: '2026-01-02T03:04:05.000Z',
      c: null,
      d: [1, null],
    });
    expect(toWireContext(undefined)).toBeNull();
  });

  test('defers on what the server path cannot send either', () => {
    expect(toWireContext({ big: BigInt(1) })).toBe(DEFER);
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    expect(toWireContext(cyclic)).toBe(DEFER);
    expect(toWireContext(['a'])).toBe(DEFER);
  });
});
