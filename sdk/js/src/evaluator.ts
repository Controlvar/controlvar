/**
 * Local flag evaluation from the server's ruleset (`GET /api/v1/sdk/ruleset`, beta).
 *
 * The rule: a local answer is always the answer `GET /api/v1/feature-flags/evaluate/{key}`
 * would give for the same ruleset version, or there is no local answer. Whenever this module
 * cannot be sure, it returns {@link DEFER} and the client asks the server exactly as it does in
 * server mode. It never guesses, and it never answers "not matched" for something it could not
 * evaluate.
 *
 * What is answered locally (everything else defers):
 * - the operators in {@link LOCAL_OPERATORS}, and only on the value types listed in
 *   {@link evaluateCondition}: strings against strings, numbers against numbers, no coercion;
 * - numbers of magnitude at most {@link MAX_SAFE_INTEGER} (larger ones, and non-finite ones,
 *   defer);
 * - `is_null` / `is_not_null` on a string only when it contains none of
 *   {@link WHITESPACE_CODE_POINTS} (the server trims, and the two languages trim different sets);
 * - well-formed Unicode only: a lone surrogate in the user id, a context key or a context value
 *   defers.
 *
 * Every condition and group of a rule that is tried is evaluated: a DEFER anywhere in it defers
 * the whole evaluation, even when a sibling has already decided the result, because the server
 * evaluates them all and can fail on the one this module would have skipped.
 *
 * A flag's value is answered with its `enabled`: `on_value` when enabled, `off_value` otherwise,
 * and `enabled` itself for a boolean flag (an entry with no `value_type`). An entry this SDK
 * cannot read DEFERs that one flag and leaves the rest of the document in use: an `evaluation`
 * other than `local` or `remote`, a `value_type` other than `string`, `number` or `json`, or value
 * fields missing or of the wrong type. A newer server can therefore add a flag shape without
 * stopping local evaluation for every other flag. A known shape that is malformed (an active
 * `local` entry without integer percentages, say) still rejects the whole document.
 *
 * `tests/sdk-contract/ruleset-vectors.json` is generated from the server and pins every answer;
 * `__tests__/evaluator.test.ts` runs this module against it.
 */
import { md5Bytes } from './md5';

/** The ruleset format this SDK understands. Any other `schema` evaluates nothing locally. */
export const RULESET_SCHEMA = 1;

/** The flag bucketing function this SDK implements (see {@link flagBucket}). */
export const BUCKETING = 'md5-mod100-v1';

/** The largest integer both Python and JavaScript represent exactly. */
export const MAX_SAFE_INTEGER = 9007199254740991;

/**
 * Operators this SDK evaluates itself (the server's names). A copy of `local_operators` in
 * `tests/sdk-contract/ruleset-vectors.json`; a test fails when they differ.
 */
export const LOCAL_OPERATORS: readonly string[] = [
  'contains',
  'ends_with',
  'eq',
  'gt',
  'gte',
  'in',
  'is_not_null',
  'is_null',
  'lt',
  'lte',
  'neq',
  'not_contains',
  'not_in',
  'starts_with',
];

/**
 * Code points either Python or JavaScript treats as whitespace. A copy of
 * `whitespace_code_points` in `tests/sdk-contract/ruleset-vectors.json`; a test fails when they
 * differ. Deliberately a fixed list: neither language's own whitespace test covers the other's.
 */
export const WHITESPACE_CODE_POINTS: readonly number[] = [
  0x0009, 0x000a, 0x000b, 0x000c, 0x000d,
  0x001c, 0x001d, 0x001e, 0x001f,
  0x0020, 0x0085, 0x00a0, 0x1680,
  0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a,
  0x2028, 0x2029, 0x202f, 0x205f, 0x3000, 0xfeff,
];

const LOCAL = new Set(LOCAL_OPERATORS);
const WHITESPACE = new Set(WHITESPACE_CODE_POINTS);
const STRING_OPERATORS = new Set(['contains', 'not_contains', 'starts_with', 'ends_with']);
const MATCH_OPERATORS = new Set(['eq', 'neq', 'in', 'not_in']);
const NUMERIC_OPERATORS = new Set(['gt', 'gte', 'lt', 'lte']);
const ALIAS_PREFIXES = ['user', 'device', 'app'];

/** "Ask the server": the answer cannot be given locally. */
export const DEFER: unique symbol = Symbol('defer');
export type Defer = typeof DEFER;

export type LocalReason = 'targeting_rule' | 'rollout' | 'inactive';

/**
 * The value types a ruleset entry may name in `value_type`. A boolean flag's entry names none.
 * Any other `value_type` defers that flag.
 */
export const VALUE_TYPES: readonly string[] = ['string', 'number', 'json'];

export type ValueType = 'boolean' | 'string' | 'number' | 'json';

export interface LocalAnswer {
  enabled: boolean;
  reason: LocalReason;
  /**
   * The server's `value`: the flag's `on_value` when enabled, its `off_value` otherwise; `enabled`
   * for a boolean flag. A JSON value is the ruleset's own object: copy it before handing it out.
   */
  value: unknown;
  valueType: ValueType;
}

// ─── The ruleset document (schema 1) ─────────────────────────────────────────

export interface RulesetCondition {
  attribute: string;
  operator: string;
  value: unknown;
}

export interface RulesetGroup {
  op: string;
  conditions: RulesetCondition[];
  groups: RulesetGroup[];
}

export interface RulesetRule {
  id: string;
  rollout_percentage: number;
  match: RulesetGroup;
}

export interface RulesetFlag {
  key: string;
  active: boolean;
  /** `local` or `remote`; any other value (a newer server's) defers the flag. */
  evaluation?: string;
  rollout_percentage?: number;
  rules?: RulesetRule[];
  default_rule?: { id: string; rollout_percentage: number } | null;
  /** Absent for a boolean flag; see {@link VALUE_TYPES}. */
  value_type?: unknown;
  /** On an active `local` entry of a string, number or json flag. */
  on_value?: unknown;
  /** On the active `local` and the inactive entries of a string, number or json flag. */
  off_value?: unknown;
}

export interface RulesetDocument {
  schema: number;
  version: string;
  bucketing: string;
  flags: RulesetFlag[];
}

/** A ruleset indexed for evaluation. Built once per version by {@link indexRuleset}. */
export interface IndexedRuleset {
  readonly schema: number;
  readonly bucketing: string;
  readonly version: string;
  readonly flags: ReadonlyMap<string, RulesetFlag>;
}

// ─── Validation ──────────────────────────────────────────────────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isPercentage(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value);
}

function validGroup(group: unknown, depth: number): boolean {
  if (depth > 64 || !isRecord(group)) return false;
  if (typeof group.op !== 'string' || !Array.isArray(group.conditions) || !Array.isArray(group.groups)) {
    return false;
  }
  for (const condition of group.conditions) {
    if (!isRecord(condition) || typeof condition.attribute !== 'string' || typeof condition.operator !== 'string') {
      return false;
    }
  }
  return group.groups.every(child => validGroup(child, depth + 1));
}

/**
 * Whether an entry may be indexed. Only a `local` entry's shape is checked: an inactive entry, a
 * `remote` one and one with an `evaluation` this SDK does not know are indexed and never
 * evaluated from their rules (see {@link evaluateLocally}).
 */
function validFlag(flag: unknown): flag is RulesetFlag {
  if (!isRecord(flag) || typeof flag.key !== 'string' || typeof flag.active !== 'boolean') return false;
  if (!flag.active) return true;
  if (flag.evaluation !== 'local') return true;
  if (!isPercentage(flag.rollout_percentage) || !Array.isArray(flag.rules)) return false;
  for (const rule of flag.rules) {
    if (!isRecord(rule) || typeof rule.id !== 'string' || !isPercentage(rule.rollout_percentage)) return false;
    if (!validGroup(rule.match, 0)) return false;
  }
  const defaultRule = flag.default_rule;
  if (defaultRule !== null && defaultRule !== undefined) {
    if (!isRecord(defaultRule) || !isPercentage(defaultRule.rollout_percentage)) return false;
  }
  return true;
}

/** Whether a parsed body names a format this SDK understands (checked before its shape). */
export function isSupportedFormat(body: unknown): boolean {
  return isRecord(body) && body.schema === RULESET_SCHEMA && body.bucketing === BUCKETING;
}

/**
 * Index a parsed ruleset body, or return `null` when it is not a well-formed schema-1 document
 * (a missing field, a wrong type). Call {@link isSupportedFormat} first: an unknown format is a
 * different failure from a malformed one.
 */
export function indexRuleset(body: unknown): IndexedRuleset | null {
  if (!isSupportedFormat(body) || !isRecord(body)) return null;
  if (typeof body.version !== 'string' || !Array.isArray(body.flags)) return null;
  const flags = new Map<string, RulesetFlag>();
  for (const flag of body.flags) {
    if (!validFlag(flag)) return null;
    flags.set(flag.key, flag);
  }
  return {
    schema: body.schema as number,
    bucketing: body.bucketing as string,
    version: body.version,
    flags,
  };
}

// ─── Values ──────────────────────────────────────────────────────────────────

/** A JSON number both languages compare identically: finite, not bool, |x| ≤ 2^53−1. */
export function isPortableNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= MAX_SAFE_INTEGER;
}

/** No lone surrogate (a manual scan: Node 18 has no `String.prototype.isWellFormed`). */
export function isWellFormedString(value: string): boolean {
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = i + 1 < value.length ? value.charCodeAt(i + 1) : 0;
      if (next < 0xdc00 || next > 0xdfff) return false;
      i++;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      return false;
    }
  }
  return true;
}

/** Every string in a JSON value, keys included, is well-formed. */
export function isWellFormed(value: unknown, depth = 0): boolean {
  if (depth > 256) return false;
  if (typeof value === 'string') return isWellFormedString(value);
  if (Array.isArray(value)) return value.every(item => isWellFormed(item, depth + 1));
  if (isRecord(value)) {
    for (const key of Object.keys(value)) {
      if (!isWellFormedString(key) || !isWellFormed(value[key], depth + 1)) return false;
    }
  }
  return true;
}

function containsWhitespace(value: string): boolean {
  for (const char of value) {
    if (WHITESPACE.has(char.codePointAt(0) as number)) return true;
  }
  return false;
}

// ─── Context ─────────────────────────────────────────────────────────────────

/**
 * The server's `expand_context`: nested objects flatten to dotted keys (first writer wins, in
 * key order); a top-level attribute is also visible as `user.`/`device.`/`app.` + name; and
 * `user.x` (one level) is also visible as `x`. A `Map`, so attribute names such as `__proto__`
 * or `constructor` are ordinary keys.
 */
export function flattenContext(context: Record<string, unknown> | null | undefined): Map<string, unknown> {
  const flat = new Map<string, unknown>();
  if (!context) return flat;

  const walk = (node: Record<string, unknown>, prefix: string, depth: number): void => {
    for (const key of Object.keys(node)) {
      const value = node[key];
      const full = prefix + key;
      if (!flat.has(full)) flat.set(full, value);
      if (isRecord(value) && depth < 256) walk(value, `${full}.`, depth + 1);
    }
  };
  walk(context, '', 0);

  for (const [key, value] of Array.from(flat.entries())) {
    if (key.includes('.') || isRecord(value)) continue;
    for (const prefix of ALIAS_PREFIXES) {
      const alias = `${prefix}.${key}`;
      if (!flat.has(alias)) flat.set(alias, value);
    }
  }
  for (const [key, value] of Array.from(flat.entries())) {
    const dot = key.indexOf('.');
    if (dot < 0) continue;
    const head = key.slice(0, dot);
    const tail = key.slice(dot + 1);
    if (ALIAS_PREFIXES.includes(head) && tail && !tail.includes('.') && !flat.has(tail)) {
      flat.set(tail, value);
    }
  }
  return flat;
}

// ─── Conditions and groups ───────────────────────────────────────────────────

type Outcome = boolean | Defer;

/**
 * One condition against the flattened context.
 *
 * - absent attribute: `true` for `is_null`, `false` for everything else;
 * - `is_null`/`is_not_null`: null, `""`, `[]` and `{}` are null; a string containing any
 *   whitespace code point defers; any other value is not null;
 * - an explicit null: `neq` is true, everything else false;
 * - `contains`/`not_contains`/`starts_with`/`ends_with`: string against string;
 * - `eq`/`neq`/`in`/`not_in`: string against strings, or number against numbers;
 * - `gt`/`gte`/`lt`/`lte`: number against number;
 * - any other pairing of types, and any operator not in {@link LOCAL_OPERATORS}: DEFER.
 */
export function evaluateCondition(condition: RulesetCondition, flat: Map<string, unknown>): Outcome {
  const op = condition.operator;
  const rule = condition.value;
  if (!LOCAL.has(op)) return DEFER;
  if (!flat.has(condition.attribute)) return op === 'is_null';
  const value = flat.get(condition.attribute);

  if (op === 'is_null' || op === 'is_not_null') {
    let empty: boolean;
    if (value === null || value === undefined) {
      empty = true;
    } else if (typeof value === 'string') {
      if (containsWhitespace(value)) return DEFER;
      empty = value === '';
    } else if (Array.isArray(value)) {
      empty = value.length === 0;
    } else if (isRecord(value)) {
      empty = Object.keys(value).length === 0;
    } else {
      empty = false;
    }
    return op === 'is_null' ? empty : !empty;
  }

  if (value === null || value === undefined) return op === 'neq';

  if (STRING_OPERATORS.has(op)) {
    if (typeof value !== 'string' || typeof rule !== 'string' || !isWellFormedString(rule)) return DEFER;
    switch (op) {
      case 'contains':
        return value.includes(rule);
      case 'not_contains':
        return !value.includes(rule);
      case 'starts_with':
        return value.startsWith(rule);
      default:
        return value.endsWith(rule);
    }
  }

  if (MATCH_OPERATORS.has(op)) {
    const list = op === 'in' || op === 'not_in';
    const items: unknown[] | null = list ? (Array.isArray(rule) ? rule : null) : [rule];
    if (!items || items.length === 0) return DEFER;
    let hit = false;
    if (typeof items[0] === 'string') {
      if (typeof value !== 'string') return DEFER;
      for (const item of items) {
        if (typeof item !== 'string' || !isWellFormedString(item)) return DEFER;
        if (item === value) hit = true;
      }
    } else {
      if (!isPortableNumber(value)) return DEFER;
      for (const item of items) {
        if (!isPortableNumber(item)) return DEFER;
        if (item === value) hit = true;
      }
    }
    return op === 'eq' || op === 'in' ? hit : !hit;
  }

  if (NUMERIC_OPERATORS.has(op)) {
    if (!isPortableNumber(value) || !isPortableNumber(rule)) return DEFER;
    switch (op) {
      case 'gt':
        return value > rule;
      case 'gte':
        return value >= rule;
      case 'lt':
        return value < rule;
      default:
        return value <= rule;
    }
  }

  return DEFER;
}

/**
 * A group: every condition and every child group is evaluated (no short-circuit), then any
 * DEFER defers; an empty group is true; `and` is all, `or` is any, `not` is "not all".
 */
export function evaluateGroup(group: RulesetGroup, flat: Map<string, unknown>, depth = 0): Outcome {
  if (depth > 64) return DEFER;
  const results: Outcome[] = [];
  for (const condition of group.conditions) results.push(evaluateCondition(condition, flat));
  for (const child of group.groups) results.push(evaluateGroup(child, flat, depth + 1));
  if (results.some(result => result === DEFER)) return DEFER;
  if (results.length === 0) return true;
  switch (group.op) {
    case 'and':
      return results.every(Boolean);
    case 'or':
      return results.some(Boolean);
    case 'not':
      return !results.every(Boolean);
    default:
      return DEFER;
  }
}

// ─── Bucketing ───────────────────────────────────────────────────────────────

/**
 * `md5-mod100-v1`: the 128-bit big-endian MD5 of the UTF-8 `"{userId}:{flagKey}"`, mod 100.
 * This is the server's flag bucket; it is NOT {@link consistentHash} (the cross-SDK utility).
 */
export function flagBucket(userId: string, flagKey: string): number {
  const digest = md5Bytes(`${userId}:${flagKey}`);
  let bucket = 0;
  for (let i = 0; i < digest.length; i++) bucket = (bucket * 256 + digest[i]) % 100;
  return bucket;
}

/** Whether `userId` falls inside a rollout of `percentage` for `flagKey`. */
export function inRollout(percentage: number, userId: string, flagKey: string): boolean {
  if (percentage <= 0) return false;
  if (percentage >= 100) return true;
  return flagBucket(userId, flagKey) < percentage;
}

// ─── Flag values ─────────────────────────────────────────────────────────────

/**
 * Whether `value` is a value of `type` as the server serves one: a string; a number that is
 * finite and within ±(2^53−1), never a boolean; a JSON object or array. Any other `type` is
 * `false`.
 */
export function isValueOfType(value: unknown, type: string): boolean {
  switch (type) {
    case 'string':
      return typeof value === 'string';
    case 'number':
      return isPortableNumber(value);
    case 'json':
      return value !== null && typeof value === 'object';
    default:
      return false;
  }
}

/**
 * A copy of a JSON value that shares nothing with it: an object or array is copied deeply by a
 * JSON round trip (it came from JSON, so nothing is lost, and a `__proto__` key stays an ordinary
 * key); a string, number, boolean or null is returned as it is.
 */
export function copyValue<T>(value: T): T {
  return value !== null && typeof value === 'object' ? (JSON.parse(JSON.stringify(value)) as T) : value;
}

/**
 * The type of value a flag's entry serves, or `null` when this SDK cannot read it: a
 * `value_type` it does not know, or the value the entry needs (`off_value`, and `on_value` on an
 * active entry) missing or of another type. An entry with no `value_type` is a boolean flag.
 */
export function entryValueType(flag: RulesetFlag): ValueType | null {
  const type = flag.value_type;
  if (type === undefined) return 'boolean';
  if (typeof type !== 'string' || !VALUE_TYPES.includes(type)) return null;
  if (!isValueOfType(flag.off_value, type)) return null;
  if (flag.active && !isValueOfType(flag.on_value, type)) return null;
  return type as ValueType;
}

/**
 * Whether an active flag can be answered from the ruleset at all: a `local` entry this SDK can
 * read. Every other active flag is evaluated by the server, for every user.
 */
export function readsLocally(flag: RulesetFlag): boolean {
  return flag.evaluation === 'local' && entryValueType(flag) !== null;
}

// ─── Flags ───────────────────────────────────────────────────────────────────

/**
 * Evaluate one flag locally, or {@link DEFER}.
 *
 * `context` must already be what the server would receive: the JSON round-trip of the
 * attributes (see `toWireContext`), or `null` for none.
 */
export function evaluateLocally(
  ruleset: IndexedRuleset,
  flagKey: string,
  userId: string,
  context: Record<string, unknown> | null
): LocalAnswer | Defer {
  if (ruleset.schema !== RULESET_SCHEMA || ruleset.bucketing !== BUCKETING) return DEFER;
  if (!isWellFormedString(userId) || !isWellFormedString(flagKey) || !isWellFormed(context)) return DEFER;
  const flag = ruleset.flags.get(flagKey);
  if (!flag) return DEFER;
  const valueType = entryValueType(flag);
  if (valueType === null) return DEFER;
  const answer = (enabled: boolean, reason: LocalReason): LocalAnswer => ({
    enabled,
    reason,
    value: valueType === 'boolean' ? enabled : enabled ? flag.on_value : flag.off_value,
    valueType,
  });
  if (!flag.active) return answer(false, 'inactive');
  if (flag.evaluation !== 'local') return DEFER;

  const flat = flattenContext(context);
  for (const rule of flag.rules ?? []) {
    const matched = evaluateGroup(rule.match, flat);
    if (matched === DEFER) return DEFER;
    if (matched) return answer(inRollout(rule.rollout_percentage, userId, flagKey), 'targeting_rule');
  }
  if (flag.default_rule) {
    return answer(inRollout(flag.default_rule.rollout_percentage, userId, flagKey), 'targeting_rule');
  }
  return answer(inRollout(flag.rollout_percentage ?? 0, userId, flagKey), 'rollout');
}

/**
 * The context exactly as the server would receive it: the JSON round-trip of the attributes
 * (`undefined` dropped, `Date` as a string, `NaN` as null), or `null` when there is none.
 * Returns DEFER when the attributes cannot be serialised (a `BigInt`, a cycle) or are not an
 * object; the server path then fails the same way it does today.
 */
export function toWireContext(attributes: unknown): Record<string, unknown> | null | Defer {
  if (attributes === undefined || attributes === null) return null;
  if (typeof attributes !== 'object' || Array.isArray(attributes)) return DEFER;
  let parsed: unknown;
  try {
    const text = JSON.stringify(attributes);
    if (text === undefined) return DEFER;
    parsed = JSON.parse(text);
  } catch {
    return DEFER;
  }
  if (!isRecord(parsed)) return DEFER;
  return parsed;
}
