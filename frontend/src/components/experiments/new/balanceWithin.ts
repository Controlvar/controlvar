/**
 * "Balance the split within groups" (beta, #448): the form's state for it, the
 * request body, the sentence the page says about it, and how the API's
 * refusals of it are told apart by their place.
 *
 * Pure TypeScript with no React. Every sentence the dashboard says about the
 * setting is a constant here, so the wizard, the single-page form, the
 * design edit and the experiment page cannot drift apart.
 */
import { BalanceWithin } from '@/types/experiments';

// --- the words ---------------------------------------------------------------

export const BALANCE_SWITCH_LABEL = 'Balance the split within groups';
export const BALANCE_HELPER = 'Each group gets the same split as the whole experiment.';
export const BALANCE_COMES_FIRST =
  'The global holdout, the exclusion group and the targeting rules come first: only users who take part are balanced. ' +
  'A user keeps their variant and their group, even if their attribute changes later.';
export const BALANCE_GROUP_BY_LABEL = 'Group users by';
export const BALANCE_VALUES_LABEL = 'Values to keep apart (up to 20)';
export const BALANCE_VALUES_HELP =
  'Users with any other value are balanced together as other, and users without the attribute as unknown.';
export const BALANCE_CUSTOM_KEY_LABEL = 'Attribute name';
export const BALANCE_CUSTOM_KEY_HELP = 'The name of the user attribute you send when you assign a user, such as tier.';
export const BALANCE_SEGMENT_ADD_LABEL = 'Add a saved segment';
export const BALANCE_SEGMENTS_HELP =
  'A user is balanced in the first segment in this list they belong to, so the order matters. ' +
  'Users in none of them are balanced together as other.';

/** What an experiment that cannot balance says in place of the switch. */
export const BALANCE_BANDIT_LINE =
  'A bandit experiment cannot balance its split within groups, because it moves traffic between variants as results come in.';
export const BALANCE_SPLIT_URL_LINE =
  'Split URL experiments cannot balance within groups: the edge router sends no user attributes.';

/** The start of the API's refusal of a split that is not in steps of 5 (it is at the setting as a whole). */
const SPLIT_REFUSAL_START = 'To balance within groups, every share must be a multiple of 5';

// --- keys --------------------------------------------------------------------

/** The dashboard's breakdown keys, as `BreakdownSelector` offers them. */
export const BALANCE_KEY_OPTIONS = [
  { value: 'platform', label: 'Platform' },
  { value: 'country', label: 'Country' },
  { value: 'user_tier', label: 'User Tier' },
] as const;

/** Keys whose values are a fixed set, so the setting lists none (the API's own table). */
export const CLOSED_KEYS: Readonly<Record<string, readonly string[]>> = {
  web_device: ['phone', 'tablet', 'desktop'],
  web_visitor: ['new', 'returning'],
  web_browser: ['chrome', 'edge', 'firefox', 'safari', 'samsung', 'opera'],
};

function listed(values: readonly string[]): string {
  return values.length === 1 ? values[0] : `${values.slice(0, -1).join(', ')} or ${values[values.length - 1]}`;
}

/** What a closed key's page says in place of a list of values, or null for any other key. */
export function closedKeyNote(key: string): string | null {
  const set = Object.prototype.hasOwnProperty.call(CLOSED_KEYS, key) ? CLOSED_KEYS[key] : undefined;
  return set ? `${key} is ${listed(set)}: there is nothing to list.` : null;
}

// --- the form's state --------------------------------------------------------

export type BalanceGroupBy = 'platform' | 'country' | 'user_tier' | 'custom' | 'segments';

export interface BalanceFormState {
  on: boolean;
  groupBy: BalanceGroupBy;
  /** The key typed for "Another attribute…". */
  customKey: string;
  /** The values to keep apart, as typed. */
  values: string[];
  /** The saved segments, by id, in the order a user is tried against them. */
  segments: string[];
}

export function initialBalanceState(): BalanceFormState {
  return { on: false, groupBy: 'platform', customKey: '', values: [], segments: [] };
}

const BREAKDOWN_KEYS: readonly string[] = BALANCE_KEY_OPTIONS.map((o) => o.value);

/** The form's state for a stored setting (null or absent: off). */
export function balanceStateFromStored(setting: BalanceWithin | null | undefined): BalanceFormState {
  const off = initialBalanceState();
  if (!setting || typeof setting !== 'object') return off;
  if (Array.isArray(setting.segments)) {
    return { ...off, on: true, groupBy: 'segments', segments: [...setting.segments] };
  }
  const attribute = typeof setting.attribute === 'string' ? setting.attribute : '';
  const values = Array.isArray(setting.values) ? [...setting.values] : [];
  if (BREAKDOWN_KEYS.includes(attribute)) {
    return { ...off, on: true, groupBy: attribute as BalanceGroupBy, values };
  }
  return { ...off, on: true, groupBy: 'custom', customKey: attribute, values };
}

/** The attribute the form groups by (empty until a custom key is typed). */
export function balanceAttribute(state: BalanceFormState): string {
  return state.groupBy === 'custom' ? state.customKey : state.groupBy === 'segments' ? '' : state.groupBy;
}

/**
 * The `balance_within` body for the form: null when off. An open key always
 * carries its list (the API answers an empty one); a closed key carries one
 * only when it has values, because it needs none.
 */
export function buildBalancePayload(state: BalanceFormState): BalanceWithin | null {
  if (!state.on) return null;
  if (state.groupBy === 'segments') return { segments: [...state.segments] };
  const attribute = balanceAttribute(state);
  const payload: BalanceWithin = { attribute };
  if (closedKeyNote(attribute) === null || state.values.length > 0) payload.values = [...state.values];
  return payload;
}

function canonical(setting: BalanceWithin | null | undefined): string {
  if (!setting) return 'null';
  return JSON.stringify({
    attribute: setting.attribute ?? null,
    values: setting.values ?? null,
    segments: setting.segments ?? null,
  });
}

/** True when two settings are the same setting (null and absent are both "off"). */
export function sameBalance(a: BalanceWithin | null | undefined, b: BalanceWithin | null | undefined): boolean {
  return canonical(a) === canonical(b);
}

// --- the split ---------------------------------------------------------------

/** Each variant's share as the API's hash path takes it: the tail goes to the last variant. */
function effectiveShares(allocations: number[]): number[] {
  const shares: number[] = [];
  let cumulative = 0;
  for (const allocation of allocations) {
    const before = Math.min(cumulative, 100);
    cumulative += Math.trunc(allocation) || 0;
    shares.push(Math.max(0, Math.min(cumulative, 100) - before));
  }
  if (shares.length > 0) shares[shares.length - 1] += 100 - shares.reduce((a, b) => a + b, 0);
  return shares;
}

/**
 * The API's refusal of a split with a share that is not a multiple of 5,
 * for the split as typed, or null. Said only once the shares add up to 100:
 * a split still being typed has its own line.
 */
export function splitProblem(allocations: number[]): string | null {
  const total = allocations.reduce((sum, a) => sum + (Math.trunc(a) || 0), 0);
  if (total !== 100) return null;
  if (!effectiveShares(allocations).some((share) => share % 5 !== 0)) return null;
  const split = allocations.map((a) => String(Math.trunc(a) || 0)).join('/');
  return (
    `${SPLIT_REFUSAL_START}, such as 50/50, 20/80, 35/35/30 or 40/30/30; ${split} is not.`
  );
}

// --- the page's sentence -----------------------------------------------------

/**
 * "within Platform (ios, android, web)", "within segments High value, Churn
 * risk" or "off". Segments are named, never shown by id: while the names load
 * (`names` null) they are counted, and one the list does not hold is "an
 * unknown segment".
 */
export function balanceSummary(
  setting: BalanceWithin | null | undefined,
  names: ReadonlyMap<string, string> | null,
): string {
  if (!setting) return 'off';
  if (Array.isArray(setting.segments)) {
    if (setting.segments.length === 0) return 'within segments (none chosen)';
    if (names === null) {
      return `within ${setting.segments.length} saved segment${setting.segments.length === 1 ? '' : 's'}`;
    }
    return `within segments ${setting.segments.map((id) => names.get(id) ?? 'an unknown segment').join(', ')}`;
  }
  const attribute = setting.attribute ?? '';
  if (attribute === '') return 'within an attribute not named yet';
  const option = BALANCE_KEY_OPTIONS.find((o) => o.value === attribute);
  const label = option ? option.label : attribute;
  const values = setting.values ?? [];
  return values.length > 0 ? `within ${label} (${values.join(', ')})` : `within ${label}`;
}

/** Why an experiment cannot balance, as a line in place of the switch, or null when it can. */
export function balanceUnavailableLine(experiment: {
  experiment_type?: string;
  optimization_type?: string;
}): string | null {
  if (experiment.experiment_type === 'split_url') return BALANCE_SPLIT_URL_LINE;
  const optimization = experiment.optimization_type ?? 'fixed';
  if (experiment.experiment_type === 'bandit' || optimization !== 'fixed') return BALANCE_BANDIT_LINE;
  return null;
}

// --- the API's refusals ------------------------------------------------------

/** The control a refusal is about. */
export type BalanceField = 'split' | 'attribute' | 'values' | 'segments';

export interface BalanceIssue {
  /** The API's own sentence, as it sent it. */
  message: string;
  field: BalanceField;
}

function isBalanceItem(item: unknown): item is { loc: unknown[]; msg?: unknown } {
  if (!item || typeof item !== 'object') return false;
  const { loc } = item as { loc?: unknown };
  return Array.isArray(loc) && loc[0] === 'body' && loc[1] === 'balance_within';
}

/**
 * Which control a refusal at `loc` is about. The setting's own key, values
 * and segments have a place each. A refusal of the setting as a whole is
 * about the split when it says so (the API judges the split with the
 * setting), and otherwise about the values, or the segments when the setting
 * names segments.
 */
function fieldOf(loc: unknown[], message: string, state: BalanceFormState, allocations: number[]): BalanceField {
  const last = loc[2];
  if (last === 'attribute' || last === 'values' || last === 'segments') return last;
  if (splitProblem(allocations) !== null || message.startsWith(SPLIT_REFUSAL_START)) return 'split';
  return state.groupBy === 'segments' ? 'segments' : 'values';
}

/**
 * The first refusal of the setting in a 422 body, and the body without it.
 * `issue` is null when the body names none; `rest` is what is left for the
 * caller's other messages (undefined when nothing is).
 */
export function takeBalanceIssue(
  detail: unknown,
  state: BalanceFormState,
  allocations: number[],
): { issue: BalanceIssue | null; rest: unknown[] | undefined } {
  if (!Array.isArray(detail)) return { issue: null, rest: undefined };
  const found = detail.find(isBalanceItem);
  if (!found) return { issue: null, rest: undefined };
  const raw = typeof found.msg === 'string' ? found.msg : '';
  const message = raw.replace(/^Value error,\s*/, '').trim();
  if (message === '') return { issue: null, rest: undefined };
  const rest = detail.filter((item) => !isBalanceItem(item));
  return { issue: { message, field: fieldOf(found.loc, message, state, allocations) }, rest };
}
