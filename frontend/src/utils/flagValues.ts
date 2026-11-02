/**
 * A flag's value type and its two values (#1497), as the dashboard reads and
 * writes them.
 *
 * A flag has one `value_type`: `boolean` (the default), `string`, `number` or
 * `json`. It serves `on_value` to the users it is on for and `off_value` to
 * everyone else, and to everyone while it is off. A boolean flag reads `true`
 * and `false`, and the dashboard sends neither.
 *
 * The API decides (`backend/app/core/flag_values.py`); the checks here run
 * before a request so the body holds what was typed, and each one that the
 * API also makes answers in the API's own words, at the same field:
 *
 * - a number matches the JSON number grammar, and is finite and within
 *   +/-(2^53 - 1). `Number("1e400")` is `Infinity`, which `JSON.stringify`
 *   sends as `null`, and a larger integer is rounded, so neither can be sent
 *   as typed;
 * - JSON parses, is an object or an array, is at most 10 levels deep, and
 *   every number in it is within the same range (for the same reason);
 * - the two values differ, compared as PostgreSQL compares `jsonb`
 *   (`valuesEqual`), with the refusal at the field the API puts it.
 *
 * Size (8,192 bytes as compact JSON) is left to the API: JavaScript writes
 * some numbers longer than Python does (`0.000001` against `1e-06`), so a
 * size measured here could refuse a value the API accepts. NUL and unpaired
 * surrogate characters are left to it too. Its 422 is shown at the field.
 */
import { ApiError } from '@/services/api';

export type FlagValueType = 'boolean' | 'string' | 'number' | 'json';

/** The types a value flag can have: the ones with two values to type. */
export type ValueFlagType = Exclude<FlagValueType, 'boolean'>;

export type ValueField = 'on_value' | 'off_value';

/** The two value fields, in the order the page shows them and the API reports them. */
export const VALUE_FIELDS: readonly ValueField[] = ['on_value', 'off_value'];

/** Every value type, the default first. */
export const VALUE_TYPES: readonly FlagValueType[] = ['boolean', 'string', 'number', 'json'];

export const VALUE_TYPE_LABELS: Record<FlagValueType, string> = {
  boolean: 'Boolean',
  string: 'String',
  number: 'Number',
  json: 'JSON',
};

export const VALUE_TYPE_HINTS: Record<FlagValueType, string> = {
  boolean: 'On or off. Your code reads whether the flag is on.',
  string: 'Text, such as a button label.',
  number: 'A number, such as a limit or a timeout.',
  json: 'An object or an array, such as a configuration.',
};

export const VALUE_FIELD_LABELS: Record<ValueField, string> = {
  on_value: 'On value',
  off_value: 'Off value',
};

export const VALUE_FIELD_HINTS: Record<ValueField, string> = {
  on_value:
    'What users the flag is on for get: users who match a targeting rule, and the rollout percentage of everyone else.',
  off_value: 'What everyone else gets, and what everyone gets while the flag is off.',
};

/** Under the type choice on the new-flag form. */
export const TYPE_FIXED_NOTE = 'The type cannot be changed after the flag is created.';
/** Under the type on a value flag's page. */
export const TYPE_FIXED_ON_PAGE = 'The type cannot be changed. To serve another type, create a new flag.';
/** Added to each hint of a string flag's two fields. */
export const EMPTY_STRING_HINT = 'Leave empty to serve an empty string.';
/** Under both value fields. */
export const VALUES_PUBLIC_NOTE =
  'Every application that evaluates this flag can read both values. Do not put secrets in them.';
/** Under the rollout slider, for a value flag. */
export const ROLLOUT_VALUE_HINT =
  'The share of users who match no targeting rule that get the on value. Everyone else gets the off value.';
/** A value flag's status beside its switch; a boolean flag keeps "Serving" / "Not serving". */
export const VALUE_FLAG_STATUS_OFF = 'Everyone gets the off value';
export const VALUE_FLAG_STATUS_ON = 'Rules and rollout decide who gets the on value';

export const NUMBER_INVALID = 'Enter a number, such as 10, -3 or 2.5.';
export const JSON_EMPTY = 'Enter a JSON object or array, such as {"color": "blue"}.';
export const JSON_NOT_CONTAINER =
  'A JSON value must be an object or an array. For a single text or number, choose String or Number.';
export const jsonInvalid = (detail: string): string => `Not valid JSON: ${detail}`;

/** The largest integer both Python and JavaScript represent exactly. */
export const MAX_SAFE_INTEGER = 9007199254740991;
/** Deepest nesting of objects and arrays in a value, the value itself level 1. */
export const MAX_VALUE_DEPTH = 10;

// The API's messages (flag_values.py), for the checks made on both sides.
export const outOfRange = (field: ValueField): string =>
  `${field}: a number is outside -${MAX_SAFE_INTEGER} to ${MAX_SAFE_INTEGER}`;
export const tooDeep = (field: ValueField): string =>
  `${field} nested more than ${MAX_VALUE_DEPTH} levels deep`;
export const valuesEqualMessage = (field: ValueField): string =>
  `${field} equals ${otherField(field)}; the two values must differ, or turning the flag off changes nothing`;

export function otherField(field: ValueField): ValueField {
  return field === 'on_value' ? 'off_value' : 'on_value';
}

/** The flag's value type; a flag from a server without values is boolean. */
export function flagValueType(flag: { value_type?: string | null }): string {
  return flag.value_type || 'boolean';
}

/** A type whose two values the dashboard can type: string, number or JSON. */
export function isValueFlagType(type: string): type is ValueFlagType {
  return type === 'string' || type === 'number' || type === 'json';
}

/** "String", "Number", "JSON", "Boolean"; a type this dashboard does not know, as the API wrote it. */
export function typeLabel(type: string): string {
  return (VALUE_TYPE_LABELS as Record<string, string>)[type] ?? type;
}

function isContainer(value: unknown): value is object {
  return typeof value === 'object' && value !== null;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return isContainer(value) && !Array.isArray(value);
}

/** A finite number of magnitude at most `MAX_SAFE_INTEGER`. */
export function isPortableNumber(value: number): boolean {
  return Number.isFinite(value) && Math.abs(value) <= MAX_SAFE_INTEGER;
}

/** Every number in *value* is portable. Iterative. */
function numbersPortable(value: unknown): boolean {
  const stack: unknown[] = [value];
  while (stack.length > 0) {
    const item = stack.pop();
    if (Array.isArray(item)) stack.push(...item);
    else if (isPlainObject(item)) stack.push(...Object.values(item));
    else if (typeof item === 'number' && !isPortableNumber(item)) return false;
  }
  return true;
}

/**
 * Whether *value* nests objects and arrays more than *limit* levels, the value
 * itself level 1 (a scalar has none). The API's `nested_deeper_than`. Iterative.
 */
export function nestedDeeperThan(value: unknown, limit: number): boolean {
  if (!isContainer(value)) return false;
  if (limit < 1) return true;
  const stack: Array<[object, number]> = [[value, 1]];
  while (stack.length > 0) {
    const [node, level] = stack.pop() as [object, number];
    const children = Array.isArray(node) ? node : Object.values(node);
    for (const child of children) {
      if (isContainer(child)) {
        if (level >= limit) return true;
        stack.push([child, level + 1]);
      }
    }
  }
  return false;
}

/**
 * Whether PostgreSQL's `jsonb` `=` holds two JSON values equal, as the API's
 * `values_equal` does: numbers as numbers (`3` equals `3.0`), a boolean never
 * a number, strings exactly, objects by the same keys and equal values,
 * arrays by the same length and equal items in order. Iterative.
 */
export function valuesEqual(a: unknown, b: unknown): boolean {
  const stack: Array<[unknown, unknown]> = [[a, b]];
  while (stack.length > 0) {
    const [x, y] = stack.pop() as [unknown, unknown];
    if (typeof x === 'boolean' || typeof y === 'boolean') {
      if (x !== y) return false;
    } else if (typeof x === 'number' || typeof y === 'number') {
      if (typeof x !== 'number' || typeof y !== 'number' || x !== y) return false;
    } else if (typeof x === 'string' || typeof y === 'string') {
      if (x !== y) return false;
    } else if (Array.isArray(x) || Array.isArray(y)) {
      if (!Array.isArray(x) || !Array.isArray(y) || x.length !== y.length) return false;
      x.forEach((item, i) => stack.push([item, y[i]]));
    } else if (isPlainObject(x) || isPlainObject(y)) {
      if (!isPlainObject(x) || !isPlainObject(y)) return false;
      const keys = Object.keys(x);
      if (keys.length !== Object.keys(y).length) return false;
      for (const key of keys) {
        if (!Object.prototype.hasOwnProperty.call(y, key)) return false;
        stack.push([x[key], y[key]]);
      }
    } else if (!(x === null && y === null)) {
      return false;
    }
  }
  return true;
}

/** JSON's number grammar: no leading zeros, `+`, `.5`, `5.`, `Infinity` or hex. */
const JSON_NUMBER = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$/;

export type Parsed = { ok: true; value: unknown } | { ok: false; message: string };

/**
 * The value *text* means for a *type* flag's *field*, or why it cannot be sent.
 * A string is the text as typed, empty included.
 */
export function parseValue(type: ValueFlagType, field: ValueField, text: string): Parsed {
  if (type === 'string') return { ok: true, value: text };
  if (type === 'number') {
    const trimmed = text.trim();
    if (!JSON_NUMBER.test(trimmed)) return { ok: false, message: NUMBER_INVALID };
    const value = Number(trimmed);
    if (!isPortableNumber(value)) return { ok: false, message: outOfRange(field) };
    return { ok: true, value };
  }
  if (text.trim() === '') return { ok: false, message: JSON_EMPTY };
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (err) {
    return { ok: false, message: jsonInvalid(err instanceof Error ? err.message : String(err)) };
  }
  if (!isContainer(value)) return { ok: false, message: JSON_NOT_CONTAINER };
  if (nestedDeeperThan(value, MAX_VALUE_DEPTH)) return { ok: false, message: tooDeep(field) };
  if (!numbersPortable(value)) return { ok: false, message: outOfRange(field) };
  return { ok: true, value };
}

/** The text a *type* flag's field starts with: a string as is, a number as written, JSON with 2 spaces. */
export function valueToText(type: ValueFlagType, value: unknown): string {
  if (type === 'string' && typeof value === 'string') return value;
  if (type === 'number' && typeof value === 'number') return String(value);
  if (value === undefined) return '';
  return JSON.stringify(value, null, type === 'json' ? 2 : undefined) ?? '';
}

/** A value as the read-only view shows it: JSON, so `"Buy now"`, `10`, `""` and `{...}` read as what they are. */
export function valueToDisplay(value: unknown): string {
  if (value === undefined) return '';
  return JSON.stringify(value, null, 2) ?? '';
}

export type ValueTexts = Record<ValueField, string>;
export type ValueErrors = Partial<Record<ValueField, string>>;

export interface CheckedValues {
  errors: ValueErrors;
  /** The values to send, by field: only those that changed. */
  values: Partial<Record<ValueField, unknown>>;
}

/**
 * Check a new flag's two values: each must parse, and the two must differ
 * (refused at `off_value`, where the API puts it when both are sent).
 */
export function checkNewValues(type: ValueFlagType, texts: ValueTexts): CheckedValues {
  const errors: ValueErrors = {};
  const values: Partial<Record<ValueField, unknown>> = {};
  for (const field of VALUE_FIELDS) {
    const parsed = parseValue(type, field, texts[field]);
    if (parsed.ok) values[field] = parsed.value;
    else errors[field] = parsed.message;
  }
  if (Object.keys(errors).length === 0 && valuesEqual(values.on_value, values.off_value)) {
    errors.off_value = valuesEqualMessage('off_value');
  }
  return { errors, values: Object.keys(errors).length === 0 ? values : {} };
}

/**
 * Check an edit of a flag's two values against the stored ones. A field whose
 * text is unchanged is not read, and one whose value equals the stored value
 * (JSON re-indented, `10.0` for `10`) is not sent, so a save that moves only
 * the slider never rewrites a value. The pair as it will be stored (each value
 * as sent, otherwise as stored) must differ; the refusal sits at `off_value`
 * when that is sent, otherwise at `on_value`, as the API's does.
 */
export function checkEditedValues(
  type: ValueFlagType,
  texts: ValueTexts,
  initial: ValueTexts,
  stored: Record<ValueField, unknown>,
): CheckedValues {
  const errors: ValueErrors = {};
  const values: Partial<Record<ValueField, unknown>> = {};
  for (const field of VALUE_FIELDS) {
    if (texts[field] === initial[field]) continue;
    const parsed = parseValue(type, field, texts[field]);
    if (!parsed.ok) errors[field] = parsed.message;
    else if (!valuesEqual(parsed.value, stored[field])) values[field] = parsed.value;
  }
  if (Object.keys(errors).length > 0) return { errors, values: {} };
  if (Object.keys(values).length > 0) {
    const on = 'on_value' in values ? values.on_value : stored.on_value;
    const off = 'off_value' in values ? values.off_value : stored.off_value;
    if (valuesEqual(on, off)) {
      const field: ValueField = 'off_value' in values ? 'off_value' : 'on_value';
      return { errors: { [field]: valuesEqualMessage(field) }, values: {} };
    }
  }
  return { errors, values };
}

/**
 * A 422's refusals of `on_value` and `off_value`, by field and in the API's
 * words, and an `ApiError` holding the rest of its items (or null when there
 * are none). Null when the error has no such refusal.
 *
 * Only an item at exactly `["body", "on_value"]` or `["body", "off_value"]`
 * is a value's refusal: a field of that name nested anywhere else (inside the
 * targeting rules, say) is not. The rest keeps the response's request id, so
 * whatever shows it can still name the request.
 */
export function valueRefusals(err: unknown): { fields: ValueErrors; rest: ApiError | null } | null {
  if (!(err instanceof ApiError) || err.status !== 422 || !Array.isArray(err.detail)) return null;
  const fields: ValueErrors = {};
  const rest: unknown[] = [];
  for (const item of err.detail as unknown[]) {
    const loc: unknown[] = isPlainObject(item) && Array.isArray(item.loc) ? item.loc : [];
    const field = loc.length === 2 && loc[0] === 'body' ? loc[1] : undefined;
    const msg = isPlainObject(item) && typeof item.msg === 'string' ? item.msg : '';
    if ((field === 'on_value' || field === 'off_value') && msg) {
      if (!fields[field]) fields[field] = msg;
    } else {
      rest.push(item);
    }
  }
  if (Object.keys(fields).length === 0) return null;
  const others =
    rest.length > 0
      ? new ApiError({ status: 422, detail: rest, requestId: err.requestId, retryAfter: err.retryAfter })
      : null;
  return { fields, rest: others };
}

/** The first field with an error, in page order. */
export function firstInvalid(errors: ValueErrors): ValueField | null {
  return VALUE_FIELDS.find((field) => errors[field]) ?? null;
}
