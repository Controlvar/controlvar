/**
 * Reading an experiment's stored `targeting_rules` on its page.
 *
 * The rule builder reads and writes one shape, the dashboard's
 * `{logical_operator, groups: [{logical_operator, conditions: [{attribute,
 * operator, value}]}]}`, and `jsonToRules` drops anything else. So a stored
 * value is offered for editing only when the builder can show it exactly as
 * stored and save it back without losing anything; any other value is shown
 * as JSON, and the only change offered is to replace it.
 */
import { ApiError } from '@/services/api';
import { describeTargetingIssue } from '@/components/experiments/new/createErrors';
import { OPERATOR_LABELS, OperatorType, TargetingRules, isSegmentOperator } from '@/types/targeting';
import { getOperatorsForAttribute, OperatorOptions, SEGMENT_ID_PATTERN, rulesToJson } from '@/utils/targeting';

/** A stored `targeting_rules` value, as the API returned it: any JSON. */
type Stored = unknown;

/**
 * `id` is the rule's id: a rule admitting only part of the users it matches
 * picks them by it, and the server gives a starting experiment's rule its own
 * id (#533). The builder does not show it and sends it back unchanged.
 */
const TOP_KEYS = new Set(['logical_operator', 'groups', 'id']);
const GROUP_KEYS = new Set(['id', 'logical_operator', 'conditions']);
const CONDITION_KEYS = new Set(['id', 'attribute', 'operator', 'value']);
const BUILDER_LOGICAL_OPERATORS = new Set(['AND', 'OR']);

export function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function keysWithin(obj: Record<string, unknown>, allowed: Set<string>): boolean {
  return Object.keys(obj).every((key) => allowed.has(key));
}

/** The stored rule id the builder carries through a save: absent, or non-empty text. */
function builderRuleId(value: unknown): boolean {
  return value === undefined || (typeof value === 'string' && value.length > 0);
}

/** A logical operator the builder's AND/OR toggle can show: absent, AND or OR. */
function builderLogicalOperator(value: unknown): boolean {
  return value === undefined || (typeof value === 'string' && BUILDER_LOGICAL_OPERATORS.has(value));
}

/** The operators that take no value; the builder hides the value input and stores null. */
const NO_VALUE_OPERATORS = new Set(['is_null', 'is_not_null']);

/**
 * A value the builder keeps exactly as stored, for this operator.
 *
 * `is_null` / `is_not_null`: only null (the builder stores null for them and
 * `targetingPayload` sends null back). Any other operator: a string, number,
 * boolean or list of strings, never null, because `jsonToRules` turns null
 * into "" and the engine does not treat the two alike (`equals` with a
 * missing attribute matches null and not ""; `contains` and the other text
 * operators never match null and match everything with "").
 * `in_segment` / `not_in_segment`: a segment id (the picker offers nothing else).
 */
function builderValue(operator: string, value: unknown): boolean {
  if (NO_VALUE_OPERATORS.has(operator)) return value === null;
  if (isSegmentOperator(operator)) return typeof value === 'string' && SEGMENT_ID_PATTERN.test(value);
  if (['string', 'number', 'boolean'].includes(typeof value)) return true;
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

/**
 * True for the values that mean "no rules, everyone is eligible": `null`,
 * `{}`, and a dashboard value whose `groups` is empty (`{"groups": []}`, with
 * or without a top-level `logical_operator`).
 */
export function isNoRules(value: Stored): boolean {
  if (value === null || value === undefined) return true;
  if (!isPlainObject(value)) return false;
  const keys = Object.keys(value);
  if (keys.length === 0) return true;
  return (
    keysWithin(value, TOP_KEYS) &&
    Array.isArray(value.groups) &&
    value.groups.length === 0 &&
    builderLogicalOperator(value.logical_operator)
  );
}

/**
 * Whether the rule builder can edit `value` without changing what it means.
 *
 * Editable: "no rules" (see `isNoRules`), or a dashboard value whose top-level
 * keys are within {logical_operator, groups, id} (an `id` being non-empty
 * text, which `targetingPayload` sends back unchanged), group keys within {id,
 * logical_operator, conditions} and condition keys within {id, attribute,
 * operator, value}; where every logical operator is AND or OR (the builder
 * has no NOT), every operator is one the builder offers for that attribute,
 * and every value is one it keeps exactly (see `builderValue`).
 *
 * `options` are the page's builder options (`OperatorOptions`): the same ones
 * its dropdown is given, so "an operator the builder offers" means the same
 * thing in both places.
 *
 * Not editable, among others: a top-level `rollout_percentage` (the API
 * accepts it, the builder has no control for it and would drop it), the
 * native `{"rules": [...]}` shape, and a flat `{"country": ["US"]}`.
 */
export function isEditableTargeting(value: Stored, options: OperatorOptions = {}): boolean {
  if (isNoRules(value)) return true;
  if (!isPlainObject(value) || !keysWithin(value, TOP_KEYS)) return false;
  if (!builderRuleId(value.id)) return false;
  if (!builderLogicalOperator(value.logical_operator) || !Array.isArray(value.groups)) return false;
  return value.groups.every((group) => {
    if (!isPlainObject(group) || !keysWithin(group, GROUP_KEYS)) return false;
    if (!builderLogicalOperator(group.logical_operator) || !Array.isArray(group.conditions)) return false;
    if (group.conditions.length === 0) return false;
    return group.conditions.every((condition) => {
      if (!isPlainObject(condition) || !keysWithin(condition, CONDITION_KEYS)) return false;
      const { attribute, operator, value: conditionValue } = condition;
      if (typeof attribute !== 'string' || typeof operator !== 'string') return false;
      if (!getOperatorsForAttribute(attribute, options).includes(operator as OperatorType)) return false;
      return builderValue(operator, conditionValue);
    });
  });
}

/** Shown after a segment id the segment list does not contain. */
export const SEGMENT_NOT_FOUND = '(segment not found)';

/**
 * One condition in words: `country is one of US, CA`.
 *
 * `segmentName` turns a segment condition's id into what to show for it (the
 * segment's name); without it the id is shown, as it is until names arrive.
 */
export function describeCondition(
  condition: Record<string, unknown>,
  segmentName?: (segmentId: string) => string,
): string {
  const attribute = String(condition.attribute ?? '');
  const operator = String(condition.operator ?? '');
  const label = OPERATOR_LABELS[operator as OperatorType] ?? operator;
  if (operator === 'is_null' || operator === 'is_not_null') return `${attribute} ${label}`;
  const value = condition.value;
  if (segmentName && isSegmentOperator(operator) && typeof value === 'string') {
    return `${attribute} ${label} ${segmentName(value)}`;
  }
  const shown = Array.isArray(value) ? value.join(', ') : String(value ?? '');
  return `${attribute} ${label} ${shown}`;
}

/** How a condition still in the rule builder names a part not filled in yet. */
export const ATTRIBUTE_NOT_SET = '(attribute not set)';
export const VALUE_NOT_SET = '(value not set)';

/**
 * A condition still in the rule builder, in words: `describeCondition`, with
 * a blank attribute or value named as not set. Blank means what
 * `validateRules` refuses (an attribute of spaces; a value of null or "" for
 * an operator that takes one), so the creation that follows says what to fix.
 * Rules already stored are described as stored, by `describeCondition`.
 */
export function describeDraftCondition(
  condition: Record<string, unknown>,
  segmentName?: (segmentId: string) => string,
): string {
  const attribute = String(condition.attribute ?? '');
  const value = condition.value;
  const blank = value === null || value === undefined || value === '';
  return describeCondition(
    {
      ...condition,
      attribute: attribute.trim() === '' ? ATTRIBUTE_NOT_SET : attribute,
      value: blank ? VALUE_NOT_SET : value,
    },
    // A value not set is not a segment id to look up.
    blank ? undefined : segmentName,
  );
}

/** "all" for AND (or absent), "any" for OR: how a list of things combines, in words. */
export function combineWord(logicalOperator: unknown): 'all' | 'any' {
  return logicalOperator === 'OR' ? 'any' : 'all';
}

/**
 * What the page sends as `targeting_rules` for the builder's rules: the
 * dashboard shape without the builder's ids, or `{}` when there are no
 * groups (no rules, everyone is eligible). The stored value's top-level `id`,
 * when it has one, is sent back unchanged: it is the rule's id, which decides
 * who a partial rollout admits (#533).
 *
 * `is_null` / `is_not_null` conditions are sent with `value: null`, as stored:
 * `jsonToRules` reads null as "" for the text input, and this puts it back.
 * So opening and saving untouched rules sends what was stored, apart from two
 * normalisations the API reads identically: the builder's group and condition
 * ids are dropped (nothing below the top level reads an id), and an absent
 * logical operator is written as AND (the API's default).
 */
export function targetingPayload(rules: TargetingRules, stored?: Stored): Record<string, unknown> {
  if (rules.groups.length === 0) return {};
  const json = rulesToJson(rules);
  const ruleId = isPlainObject(stored) && typeof stored.id === 'string' && stored.id ? { id: stored.id } : {};
  return {
    ...ruleId,
    ...json,
    groups: json.groups.map((group) => ({
      ...group,
      conditions: group.conditions.map((condition) =>
        NO_VALUE_OPERATORS.has(condition.operator) ? { ...condition, value: null } : condition,
      ),
    })),
  };
}

/** What a failed save shows: a heading, and one line per targeting problem when the API named them. */
export interface TargetingSaveError {
  message: string;
  problems?: string[];
}

/** Heading for problems found before the save was sent. */
export const TARGETING_SAVE_INCOMPLETE = 'Finish or remove these conditions before saving:';

/** Heading for rules the API refused (422 on `targeting_rules`). */
export const TARGETING_SAVE_REFUSED = 'The rules were not accepted:';

function targetingProblems(detail: unknown): string[] {
  if (!Array.isArray(detail)) return [];
  return detail
    .filter((item) => {
      const loc = isPlainObject(item) ? item.loc : undefined;
      return Array.isArray(loc) && loc[loc.length - 1] === 'targeting_rules';
    })
    .map((item) => {
      const msg = (item as { msg?: unknown }).msg;
      return describeTargetingIssue(typeof msg === 'string' ? msg : 'not valid');
    });
}

/**
 * The error a failed targeting save shows.
 *
 * - 422 naming `targeting_rules`: each problem in the builder's words
 *   ("Group 1, Condition 2: unknown operator").
 * - 400, 403 and 409: the API's own reason. A refusal for the experiment's
 *   state (400) names the state, one for the role (403) names the role, so
 *   the page does not guess which it was.
 * - anything else: the message the API client built.
 *
 * Deliberately separate from `describeCreateError`, whose 403 and 409 copy is
 * about creating an experiment (the role to create, a key already taken).
 */
export function describeTargetingSaveError(err: unknown): TargetingSaveError {
  if (err instanceof ApiError && err.status === 422) {
    const problems = targetingProblems(err.detail);
    if (problems.length > 0) return { message: TARGETING_SAVE_REFUSED, problems };
  }
  return { message: err instanceof Error && err.message ? err.message : 'Could not save the rules.' };
}
