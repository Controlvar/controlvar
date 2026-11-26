/**
 * A flag's split and a rule's own value (#2012), as the dashboard reads,
 * edits and writes them, and every word they show.
 *
 * A string, number or json flag may serve one of several values to the users
 * it is on for: its split, 3 to 10 entries of a value and a whole-number
 * weight, the weights adding up to 100. Each environment has its own; a
 * boolean flag has none. The server picks a user's value from the user's ID
 * and the flag's key, by the entries' running totals in the order stored, so
 * the editor never reorders them. The one dashboard rule may also serve a
 * value of its own instead of the split (or the on value).
 *
 * The API decides (`backend/app/core/flag_values.py`, `split_errors` and
 * `serving_errors`). The checks here run before a request and answer in the
 * API's words, at the field the API names, so a refusal reads the same either
 * way. A save sends `split` only when the split was changed from what was
 * loaded, as `targeting_rules` is sent only when the rules were changed
 * (`flagTargeting.ts`): an older API refuses the field, and a page that loaded
 * no `split` must never clear one.
 *
 * The words live here, one file, so the tests that hold the copy read it.
 */
import { ApiError } from '@/services/api';
import type { FlagSplitEntry } from '@/services/featureFlags';
import { sameJson } from '@/utils/flagTargeting';
import { ValueFlagType, parseValue, valueToText, valuesEqual } from '@/utils/flagValues';

/** The API's limits (`MIN_SPLIT_ENTRIES`, `MAX_SPLIT_ENTRIES`, `SPLIT_TOTAL`). */
export const MIN_SPLIT_VALUES = 3;
export const MAX_SPLIT_VALUES = 10;
export const SPLIT_TOTAL = 100;

/** How many characters of a value a preview shows. */
export const PREVIEW_CHARS = 80;

// ---------------------------------------------------------------------------
// Words
// ---------------------------------------------------------------------------

/** The section's heading: "Split", or "Split in Staging". */
export function splitHeading(environmentLabel: string | null): string {
  return environmentLabel ? `Split in ${environmentLabel}` : 'Split';
}

export const SPLIT_INTRO =
  'Serve the users the flag is on for one of several values, each to a share of them. A user keeps their value while the rollout percentage changes.';
export const NO_SPLIT_TEXT = 'No split: the users the flag is on for get the on value.';
export const ADD_SPLIT = 'Add a split';
export const REMOVE_SPLIT = 'Remove the split';
export const KEEP_SPLIT = 'Keep the stored split';
export const SPLIT_REMOVED_NOTE =
  'The split is removed when you save. The users the flag is on for then get the on value.';

/** The table: its caption and column headers. */
export const SPLIT_CAPTION = 'Values and weights';
export const VALUE_COLUMN = 'Value';
export const WEIGHT_COLUMN = 'Weight (%)';
export const valueLabel = (n: number): string => `Value ${n}`;
export const weightLabel = (n: number): string => `Weight of value ${n} (%)`;
export const REMOVE_VALUE = 'Remove';
export const removeValueLabel = (n: number): string => `Remove value ${n}`;
export const ADD_VALUE = 'Add value';
export const SPREAD_EVENLY = 'Spread evenly';

export const SPLIT_ORDER_NOTE = 'Order is fixed. New values are added at the end.';
/** The API's own words for a split of too few or too many values. */
export const AT_LEAST_NOTE = `A split needs at least ${MIN_SPLIT_VALUES} values.`;
export const AT_MOST_NOTE = `A split can have at most ${MAX_SPLIT_VALUES} values.`;
/** A split of two among the users the flag is on for: a third value at weight 0. */
export const SPLIT_TWO_VALUES_NOTE =
  'Two values? Give a third value a weight of 0; it may be the off value. The users the flag is on for then get one of the other two. To serve one value to some users and the off value to the rest, use the on value with a rollout percentage.';
export const SPLIT_PUBLIC_NOTE =
  'Every application that evaluates this flag can read every value. Do not put secrets in them.';
/** The standing note, with the experiments page linked from its last words. */
export const SPLIT_MEASURES_TEXT = 'A split does not measure anything. To compare values on a metric,';
export const SPLIT_EXPERIMENT_LINK = 'create an experiment';

/** The live Total. */
export function totalText(total: number): string {
  if (total === SPLIT_TOTAL) return `Total ${total}%`;
  if (total < SPLIT_TOTAL) return `Total ${total}%: ${SPLIT_TOTAL - total}% more needed`;
  return `Total ${total}%: over by ${total - SPLIT_TOTAL}%`;
}

// The API's refusals (flag_values.py), for the checks made on both sides.
export const weightsTotalMessage = (total: number): string =>
  `Weights must add up to ${SPLIT_TOTAL}; they add up to ${total}.`;
export const WEIGHT_MESSAGE = `A weight is a whole number from 0 to ${SPLIT_TOTAL}.`;
export const VALUES_EQUAL_MESSAGE = "value equals an earlier entry's value; the values of a split must differ";
export const SERVED_VALUES_TOO_LARGE =
  "A split's values and the rules' own values can be at most 81920 bytes together in one environment.";

/** The read-only view's status line: how many values the users the flag is on for can get. */
export function servingText(count: number): string {
  return count === 1 ? 'Serving 1 value' : `Serving ${count} values`;
}

/** Above Save when the split or the rule's value stops it. */
export const SPLIT_SUMMARY_HEADING = 'Fix these before saving:';

/** A value flag's status beside its switch, while on with a split in this environment. */
export const SPLIT_STATUS_ON = 'Rules and rollout decide who gets a value of the split';
/** Under the rollout slider, with a split in this environment. */
export const SPLIT_ROLLOUT_HINT =
  'The share of users who match no targeting rule that get a value of the split. Everyone else gets the off value.';
/** The On value field's hint, with a split in Production. */
export const SPLIT_ON_VALUE_HINT =
  'While Production has a split, the users the flag is on for get its values instead. They get the on value once the split is removed, and from an API older than 0.49.0.';

/** The list's text badge for a flag with a split in the environment on screen. */
export const SPLIT_BADGE = 'Split';

// The rule's Serve control.
export const RULE_SERVE_LABEL = 'Users these rules match get';
export const RULE_SERVE_DEFAULT_SPLIT = 'A value of the split (the default)';
export const RULE_SERVE_DEFAULT_ON = 'The on value (the default)';
export const RULE_SERVE_OWN = 'A value of their own';
export const RULE_VALUE_LABEL = 'Their value';
export const RULE_SERVE_HINT =
  'Every user these rules match gets this. A rule with a percentage of its own, which only the API sets, gives the users it matches outside that percentage the off value.';
export const RULE_NO_CONDITION_NOTE =
  'Add a condition to give the users these rules match a value of their own: a rule with no condition matches nobody.';
/** The read-only line in place of the control. */
export function ruleServeText(own: boolean, hasSplit: boolean, value: unknown): string {
  if (own) return `${RULE_SERVE_LABEL}: a value of their own, ${previewText(value)}.`;
  return `${RULE_SERVE_LABEL}: ${hasSplit ? 'a value of the split' : 'the on value'}.`;
}

// ---------------------------------------------------------------------------
// A bounded preview of a value
// ---------------------------------------------------------------------------

export interface Preview {
  /** What is shown: the value as compact JSON, cut to `limit` characters. */
  text: string;
  /** The value's length, in characters, when it is cut; null when shown whole. */
  of: number | null;
}

/** A value as compact JSON, at most `limit` characters of it, with its length when cut. */
export function preview(value: unknown, limit = PREVIEW_CHARS): Preview {
  const full = value === undefined ? '' : (JSON.stringify(value) ?? '');
  if (full.length <= limit) return { text: full, of: null };
  return { text: `${full.slice(0, limit)}…`, of: full.length };
}

/** What a cut preview says of itself. */
export function previewNote(shown: number, of: number): string {
  return `(first ${shown} of ${of} characters)`;
}

/** A preview as one line of text: the shown part, and the note when it is cut. */
export function previewText(value: unknown, limit = PREVIEW_CHARS): string {
  const p = preview(value, limit);
  return p.of === null ? p.text : `${p.text} ${previewNote(limit, p.of)}`;
}

/** A split as lines of text, one per entry, `"blue": 50%`; "None" for none. */
export function describeSplit(split: unknown, limit = PREVIEW_CHARS): string[] {
  if (!Array.isArray(split) || split.length === 0) return ['None'];
  return split.map((entry) => {
    const { value, weight } = (entry ?? {}) as { value?: unknown; weight?: unknown };
    return `${previewText(value, limit)}: ${String(weight)}%`;
  });
}

// ---------------------------------------------------------------------------
// The editor's rows
// ---------------------------------------------------------------------------

/** One row of the editor: the value as typed and the weight as typed. */
export interface SplitRow {
  /** Stable while the row exists, so React keeps each row's inputs. */
  key: number;
  text: string;
  weight: string;
}

let nextKey = 1;
function rowKey(): number {
  nextKey += 1;
  return nextKey;
}

/** A split's entries as rows to edit. */
export function rowsFromSplit(type: ValueFlagType, split: FlagSplitEntry[]): SplitRow[] {
  return split.map((entry) => ({ key: rowKey(), text: valueToText(type, entry.value), weight: String(entry.weight) }));
}

/**
 * `count` whole-number weights adding up to 100, as even as they can be, the
 * extra share to the first rows: 34/33/33 for three.
 */
export function spreadEvenly(count: number): number[] {
  if (count <= 0) return [];
  const base = Math.floor(SPLIT_TOTAL / count);
  const extra = SPLIT_TOTAL - base * count;
  return Array.from({ length: count }, (_, i) => base + (i < extra ? 1 : 0));
}

/** A new split: three empty values at 34/33/33, so nothing is served until each is typed. */
export function newSplitRows(): SplitRow[] {
  return spreadEvenly(MIN_SPLIT_VALUES).map((weight) => ({ key: rowKey(), text: '', weight: String(weight) }));
}

/** A row appended at the end: an empty value at weight 0, so the total stays where it was. */
export function newRow(): SplitRow {
  return { key: rowKey(), text: '', weight: '0' };
}

/** A weight as typed, or null when it is not a whole number from 0 to 100. */
export function parseWeight(text: string): number | null {
  const trimmed = text.trim();
  if (!/^[0-9]{1,3}$/.test(trimmed)) return null;
  const weight = Number(trimmed);
  return weight <= SPLIT_TOTAL ? weight : null;
}

/** The weights typed so far, added up; a weight that is not a number counts 0. */
export function rowsTotal(rows: readonly SplitRow[]): number {
  return rows.reduce((sum, row) => sum + (parseWeight(row.weight) ?? 0), 0);
}

/** A refusal of the split, by where it is: the split as a whole, or a row's value or weight. */
export interface SplitErrors {
  /** The split as a whole: the total, the number of values; shown at the Total. */
  total?: string;
  rows: Record<number, { value?: string; weight?: string }>;
}

export function hasSplitErrors(errors: SplitErrors | null): errors is SplitErrors {
  return errors !== null && (errors.total !== undefined || Object.keys(errors.rows).length > 0);
}

/**
 * The rows as the split the API would store, or why the API would refuse it,
 * in its words and at its places: each value of the flag's type, each weight a
 * whole number from 0 to 100, no value equal to an earlier one (refused at
 * the later), 3 to 10 values, and the weights adding up to exactly 100.
 */
export function checkSplitRows(
  type: ValueFlagType,
  rows: readonly SplitRow[],
): { errors: SplitErrors | null; split: FlagSplitEntry[] } {
  const errors: SplitErrors = { rows: {} };
  const split: FlagSplitEntry[] = [];
  const accepted: unknown[] = [];
  let weightsValid = true;
  rows.forEach((row, index) => {
    const parsed = parseValue(type, 'value', row.text);
    const weight = parseWeight(row.weight);
    const at: { value?: string; weight?: string } = {};
    if (!parsed.ok) {
      at.value = parsed.message;
    } else if (accepted.some((earlier) => valuesEqual(earlier, parsed.value))) {
      at.value = VALUES_EQUAL_MESSAGE;
    } else {
      accepted.push(parsed.value);
    }
    if (weight === null) {
      at.weight = WEIGHT_MESSAGE;
      weightsValid = false;
    }
    if (at.value || at.weight) errors.rows[index] = at;
    if (parsed.ok && weight !== null) split.push({ value: parsed.value, weight });
  });
  if (rows.length < MIN_SPLIT_VALUES) errors.total = AT_LEAST_NOTE;
  else if (rows.length > MAX_SPLIT_VALUES) errors.total = AT_MOST_NOTE;
  else if (weightsValid) {
    const total = rowsTotal(rows);
    if (total !== SPLIT_TOTAL) errors.total = weightsTotalMessage(total);
  }
  return hasSplitErrors(errors) ? { errors, split: [] } : { errors: null, split };
}

/** Two splits are the same when their entries are, in order (object keys in any order). */
export function sameSplit(a: unknown, b: unknown): boolean {
  return sameJson(a ?? null, b ?? null);
}

/**
 * What a save sends as `split`.
 *
 * - `loaded` is `undefined` (the API answered no `split`, so it is older and
 *   refuses the field): never sent.
 * - Not touched since it was loaded: never sent.
 * - `rows` is null (the user removed the split): `null`, only when one was loaded.
 * - Otherwise the rows as a split, sent only when it differs from `loaded`;
 *   or the refusals, and nothing is sent.
 */
export type SplitToSend =
  | { kind: 'leave' }
  | { kind: 'send'; split: FlagSplitEntry[] | null }
  | { kind: 'refused'; errors: SplitErrors };

export function splitToSend(
  type: ValueFlagType,
  rows: readonly SplitRow[] | null,
  loaded: FlagSplitEntry[] | null | undefined,
  touched: boolean,
): SplitToSend {
  if (loaded === undefined || !touched) return { kind: 'leave' };
  if (rows === null) return loaded === null ? { kind: 'leave' } : { kind: 'send', split: null };
  const checked = checkSplitRows(type, rows);
  if (checked.errors) return { kind: 'refused', errors: checked.errors };
  return sameSplit(checked.split, loaded) ? { kind: 'leave' } : { kind: 'send', split: checked.split };
}

/** The split as stored, when it is a list of entries the editor can show; null otherwise. */
export function storedSplit(value: unknown): FlagSplitEntry[] | null {
  if (!Array.isArray(value)) return null;
  const entries = value.filter(
    (entry): entry is FlagSplitEntry =>
      typeof entry === 'object' && entry !== null && 'value' in entry && typeof (entry as { weight?: unknown }).weight === 'number',
  );
  return entries.length === value.length ? entries : null;
}

// ---------------------------------------------------------------------------
// The API's refusals, by place
// ---------------------------------------------------------------------------

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * A 422's refusals of `split`, by place and in the API's words, and an
 * `ApiError` holding the rest of its items (null when none). Null when the
 * error has none.
 *
 * - `["body", "split"]`: the split as a whole, shown at the Total;
 * - `["body", "split", i]`: entry i, shown at its value;
 * - `["body", "split", i, "value" | "weight"]`: that field of row i.
 */
export function splitRefusals(err: unknown): { errors: SplitErrors; rest: ApiError | null } | null {
  if (!(err instanceof ApiError) || err.status !== 422 || !Array.isArray(err.detail)) return null;
  const errors: SplitErrors = { rows: {} };
  const rest: unknown[] = [];
  for (const item of err.detail as unknown[]) {
    const loc: unknown[] = isPlainObject(item) && Array.isArray(item.loc) ? item.loc : [];
    const msg = isPlainObject(item) && typeof item.msg === 'string' ? item.msg : '';
    if (loc[0] !== 'body' || loc[1] !== 'split' || !msg) {
      rest.push(item);
      continue;
    }
    const index = loc[2];
    if (loc.length === 2) {
      errors.total = errors.total ?? msg;
    } else if (typeof index === 'number') {
      const field = loc.length === 4 && loc[3] === 'weight' ? 'weight' : 'value';
      const at = (errors.rows[index] = errors.rows[index] ?? {});
      at[field] = at[field] ?? msg;
    } else {
      rest.push(item);
    }
  }
  if (!hasSplitErrors(errors)) return null;
  const others =
    rest.length > 0
      ? new ApiError({ status: 422, detail: rest, requestId: err.requestId, retryAfter: err.retryAfter })
      : null;
  return { errors, rest: others };
}

/**
 * Whether a refusal of `targeting_rules` is about the rule's own value: the
 * dashboard rule's place is `value` (`value must be a string, ...`), the
 * model's check adds pydantic's "Value error, " before it, and the size of a
 * split's and the rules' values together is the one other.
 */
export function isRuleValueMessage(msg: string): boolean {
  const text = msg.replace(/^Value error, /, '');
  return /^value[ :]/.test(text) || text === SERVED_VALUES_TOO_LARGE;
}

/**
 * A 422's refusal of the rule's own value (at `["body", "targeting_rules"]`),
 * in the API's words, and the rest of its items; null when it has none.
 */
export function ruleValueRefusal(err: unknown): { message: string; rest: ApiError | null } | null {
  if (!(err instanceof ApiError) || err.status !== 422 || !Array.isArray(err.detail)) return null;
  let message: string | null = null;
  const rest: unknown[] = [];
  for (const item of err.detail as unknown[]) {
    const loc: unknown[] = isPlainObject(item) && Array.isArray(item.loc) ? item.loc : [];
    const msg = isPlainObject(item) && typeof item.msg === 'string' ? item.msg : '';
    if (loc.length === 2 && loc[0] === 'body' && loc[1] === 'targeting_rules' && isRuleValueMessage(msg)) {
      message = message ?? msg.replace(/^Value error, /, '');
    } else {
      rest.push(item);
    }
  }
  if (message === null) return null;
  const others =
    rest.length > 0
      ? new ApiError({ status: 422, detail: rest, requestId: err.requestId, retryAfter: err.retryAfter })
      : null;
  return { message, rest: others };
}

// ---------------------------------------------------------------------------
// The impact line
// ---------------------------------------------------------------------------

/** Users who change value when the weights change: from one value to another, and how many slots of 100. */
export interface SplitMove {
  from: unknown;
  to: unknown;
  /** How many of the 100 slots change owner this way: about this many in every 100 users. */
  slots: number;
}

/** Which entry owns each of the 100 slots: entry i owns the slots from the running total before it to its own. */
function owners(entries: readonly FlagSplitEntry[]): number[] {
  const out: number[] = [];
  entries.forEach((entry, index) => {
    for (let n = 0; n < entry.weight; n += 1) out.push(index);
  });
  return out;
}

/**
 * Who moves when `before` becomes `after`, read off the running totals the
 * server picks by: a slot owned by one value before and another after moves
 * its users from the first to the second. Every move is listed, in the order
 * of the slots, not only the users who move to a value whose weight rose.
 *
 * Null when the change is not a change of weights: `before`'s values must be
 * `after`'s first values, in the same order (values may be added at the end),
 * and both must add up to 100.
 */
export function splitMoves(before: readonly FlagSplitEntry[], after: readonly FlagSplitEntry[]): SplitMove[] | null {
  if (before.length > after.length) return null;
  if (!before.every((entry, i) => valuesEqual(entry.value, after[i].value))) return null;
  const was = owners(before);
  const now = owners(after);
  if (was.length !== SPLIT_TOTAL || now.length !== SPLIT_TOTAL) return null;
  const moves: SplitMove[] = [];
  const byPair = new Map<string, SplitMove>();
  for (let slot = 0; slot < SPLIT_TOTAL; slot += 1) {
    if (was[slot] === now[slot]) continue;
    const pair = `${was[slot]}>${now[slot]}`;
    let move = byPair.get(pair);
    if (!move) {
      move = { from: before[was[slot]].value, to: after[now[slot]].value, slots: 0 };
      byPair.set(pair, move);
      moves.push(move);
    }
    move.slots += 1;
  }
  return moves;
}

const SHORT = 40;

/**
 * The impact line, or null when nothing moves: "About 10 in every 100 users
 * the flag is on for move from "green" to "blue". Everyone else keeps their
 * value." With more moves, each is named.
 */
export function impactText(moves: readonly SplitMove[] | null): string | null {
  if (!moves || moves.length === 0) return null;
  const parts = moves.map(
    (move, i) =>
      `${i === 0 ? 'About' : 'about'} ${move.slots} in every 100${i === 0 ? ' users the flag is on for' : ''} move from ${previewText(move.from, SHORT)} to ${previewText(move.to, SHORT)}`,
  );
  const joined =
    parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(', ')}, and ${parts[parts.length - 1]}`;
  return `${joined}. Everyone else keeps their value.`;
}
