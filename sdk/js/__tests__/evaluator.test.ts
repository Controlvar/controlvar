/**
 * The local evaluator against `tests/sdk-contract/ruleset-vectors.json`, which the server
 * generates from its own ruleset builder and flag evaluator (drift-checked in the backend suite).
 *
 * The property: for every case the SDK either defers to the server or gives exactly the server's
 * `{enabled, reason}` and `value` (a value case's `expected_value`; `enabled` for a boolean
 * flag); every `must_local` case is answered locally; and nothing else is. The number of local
 * answers is pinned, so an SDK that defers everything fails.
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
  VALUE_TYPES,
  WHITESPACE_CODE_POINTS,
  type Defer,
  type LocalAnswer,
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
  /** What the server's `value` is, on a string, number or json flag's case only. */
  expected_value?: unknown;
  must_local: boolean;
}

/** The server's `value` for a case: `expected_value`, or `enabled` for a boolean flag. */
function serverValue(testCase: VectorCase): unknown {
  return 'expected_value' in testCase ? testCase.expected_value : testCase.expected.enabled;
}

/** Equal as JSON, so a type change ("10" for 10, 1 for true) is a difference. */
function sameJson(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
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
const EXPECTED_COUNTS = { cases: 2242, flags: 161, must_local: 1333 };

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
  const results: Array<{ testCase: VectorCase; result: LocalAnswer | Defer }> = vectors.cases.map(testCase => ({
    testCase,
    result: answer(testCase),
  }));

  test('no local answer differs from the server', () => {
    const wrong = results
      .flatMap(({ testCase, result }) =>
        result === DEFER
          ? []
          : [
              {
                id: testCase.id,
                local: { enabled: result.enabled, reason: result.reason, value: result.value },
                server: { ...testCase.expected, value: serverValue(testCase) },
              },
            ]
      )
      .filter(({ local, server }) => !sameJson(local, server))
      .map(({ id, local, server }) => `${id}: local ${JSON.stringify(local)}, server ${JSON.stringify(server)}`);
    expect(wrong).toEqual([]);
  });

  test("every value flag's must_local case is answered locally with the server's value and type", () => {
    const valueCases = results.filter(({ testCase }) => 'expected_value' in testCase && testCase.must_local);
    // Every value type the ruleset names, each as a local and an inactive/archived flag (F5's corpus).
    expect(valueCases).toHaveLength(39);
    const flags = new Map((vectors.ruleset as { flags: Array<{ key: string; value_type?: string }> }).flags.map(f => [f.key, f]));
    const types = new Set(valueCases.map(({ testCase }) => flags.get(testCase.flag)?.value_type));
    expect([...types].sort()).toEqual([...VALUE_TYPES].sort());
    const wrong = valueCases
      .filter(({ testCase, result }) => {
        if (result === DEFER) return true;
        return result.valueType !== flags.get(testCase.flag)?.value_type || !sameJson(result.value, testCase.expected_value);
      })
      .map(({ testCase, result }) => `${testCase.id}: ${result === DEFER ? 'deferred' : JSON.stringify(result)}`);
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
    expect(answer(testCase)).toEqual({ ...testCase.expected, value: testCase.expected.enabled, valueType: 'boolean' });
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

// ─── Flag values, and entries this SDK cannot read ───────────────────────────

const ON_FOR_EVERYONE = { active: true, evaluation: 'local', rollout_percentage: 100, rules: [], default_rule: null };

/** A schema-1 document holding a boolean flag `plain` (on for everyone) and the given entries. */
function documentWith(...entries: Array<Record<string, unknown>>) {
  return { schema: 1, version: 'v', bucketing: 'md5-mod100-v1', flags: [{ key: 'plain', ...ON_FOR_EVERYONE }, ...entries] };
}

const PLAIN_ANSWER = { enabled: true, reason: 'rollout', value: true, valueType: 'boolean' };

describe('flag values', () => {
  test.each([
    ['string', 'on-copy', 'off-copy'],
    ['number', 10, 2.5],
    ['json', { color: 'blue' }, ['fallback']],
  ])('a %s flag serves its on value when on, and its off value when off or turned off', (type, on, off) => {
    const values = { value_type: type, on_value: on, off_value: off };
    const ruleset = indexRuleset(
      documentWith(
        { key: 'on', ...ON_FOR_EVERYONE, ...values },
        { key: 'outside', ...ON_FOR_EVERYONE, rollout_percentage: 0, ...values },
        { key: 'killed', active: false, value_type: type, off_value: off }
      )
    )!;
    expect(evaluateLocally(ruleset, 'on', 'user-1', null)).toEqual({ enabled: true, reason: 'rollout', value: on, valueType: type });
    expect(evaluateLocally(ruleset, 'outside', 'user-1', null)).toEqual({
      enabled: false,
      reason: 'rollout',
      value: off,
      valueType: type,
    });
    expect(evaluateLocally(ruleset, 'killed', 'user-1', null)).toEqual({
      enabled: false,
      reason: 'inactive',
      value: off,
      valueType: type,
    });
  });

  test('a boolean flag (no value_type) serves enabled, whatever else its entry carries', () => {
    const ruleset = indexRuleset(documentWith({ key: 'off', active: false }, { key: 'extra', ...ON_FOR_EVERYONE, on_value: 'x' }))!;
    expect(evaluateLocally(ruleset, 'plain', 'user-1', null)).toEqual(PLAIN_ANSWER);
    expect(evaluateLocally(ruleset, 'extra', 'user-1', null)).toEqual(PLAIN_ANSWER);
    expect(evaluateLocally(ruleset, 'off', 'user-1', null)).toEqual({
      enabled: false,
      reason: 'inactive',
      value: false,
      valueType: 'boolean',
    });
  });
});

describe('an entry this SDK cannot read defers that one flag, and the document still loads (Q12)', () => {
  const unreadable: Array<[string, Record<string, unknown>]> = [
    ['an evaluation it does not know', { key: 'f', active: true, evaluation: 'local_v3', rollout_percentage: 100 }],
    ['no evaluation at all', { key: 'f', active: true }],
    // The ruleset publishes value_type as string | number | json; these are outside that set.
    ['a value_type it does not know (date)', { key: 'f', ...ON_FOR_EVERYONE, value_type: 'date', on_value: 'a', off_value: 'b' }],
    ['value_type boolean, which a ruleset never names', { key: 'f', ...ON_FOR_EVERYONE, value_type: 'boolean', on_value: true, off_value: false }],
    ['a value_type that is not a string', { key: 'f', ...ON_FOR_EVERYONE, value_type: 7, on_value: 'a', off_value: 'b' }],
    ['a null value_type', { key: 'f', ...ON_FOR_EVERYONE, value_type: null, on_value: 'a', off_value: 'b' }],
    ['an inactive entry with a value_type it does not know', { key: 'f', active: false, value_type: 'date', off_value: 'b' }],
    // Mistyped or missing values.
    ['a string flag whose on_value is a number', { key: 'f', ...ON_FOR_EVERYONE, value_type: 'string', on_value: 10, off_value: 'off' }],
    ['a string flag with no on_value', { key: 'f', ...ON_FOR_EVERYONE, value_type: 'string', off_value: 'off' }],
    ['a string flag with no off_value', { key: 'f', ...ON_FOR_EVERYONE, value_type: 'string', on_value: 'on' }],
    ['a number flag whose off_value is a string', { key: 'f', ...ON_FOR_EVERYONE, value_type: 'number', on_value: 10, off_value: '2.5' }],
    ['a number flag whose on_value is a boolean', { key: 'f', ...ON_FOR_EVERYONE, value_type: 'number', on_value: true, off_value: 2.5 }],
    ['a number flag whose on_value is past 2^53-1', { key: 'f', ...ON_FOR_EVERYONE, value_type: 'number', on_value: 2 ** 53, off_value: 1 }],
    ['a json flag whose on_value is a string', { key: 'f', ...ON_FOR_EVERYONE, value_type: 'json', on_value: '{"a":1}', off_value: [] }],
    ['a json flag whose off_value is null', { key: 'f', ...ON_FOR_EVERYONE, value_type: 'json', on_value: { a: 1 }, off_value: null }],
    ['an inactive string flag with no off_value', { key: 'f', active: false, value_type: 'string' }],
    ['an inactive number flag whose off_value is a string', { key: 'f', active: false, value_type: 'number', off_value: '1' }],
  ];

  test.each(unreadable)('%s', (_label, entry) => {
    const ruleset = indexRuleset(documentWith(entry));
    expect(ruleset).not.toBeNull();
    expect(evaluateLocally(ruleset!, 'f', 'user-1', null)).toBe(DEFER);
    expect(evaluateLocally(ruleset!, 'plain', 'user-1', null)).toEqual(PLAIN_ANSWER);
  });

  test('a number value written 1e400 parses to Infinity and defers', () => {
    const text = JSON.stringify(
      documentWith({ key: 'f', ...ON_FOR_EVERYONE, value_type: 'number', on_value: 7, off_value: 1 })
    ).replace('"on_value":7', '"on_value":1e400');
    const ruleset = indexRuleset(JSON.parse(text))!;
    expect(ruleset.flags.get('f')!.on_value).toBe(Infinity);
    expect(evaluateLocally(ruleset, 'f', 'user-1', null)).toBe(DEFER);
    expect(evaluateLocally(ruleset, 'plain', 'user-1', null)).toEqual(PLAIN_ANSWER);
  });

  test('a malformed entry of a known shape still rejects the whole document', () => {
    expect(indexRuleset(documentWith({ key: 'f', active: true, evaluation: 'local' }))).toBeNull();
    expect(indexRuleset(documentWith({ key: 'f', ...ON_FOR_EVERYONE, rollout_percentage: 50.5 }))).toBeNull();
    expect(indexRuleset(documentWith({ key: 7, active: true, evaluation: 'remote' }))).toBeNull();
    expect(indexRuleset(documentWith({ key: 'f', active: 'yes' }))).toBeNull();
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
