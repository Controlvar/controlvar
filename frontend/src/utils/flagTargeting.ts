/**
 * A flag's stored `targeting_rules` on the flag pages.
 *
 * The flag pages use the experiment page's reading of a stored value
 * (`isEditableTargeting`, `targetingPayload`) with the flag builder's operator
 * options, and add one rule of their own: a save sends `targeting_rules` only
 * when the rules were changed. A save that moves only the rollout percentage
 * leaves the stored rules exactly as they are, whatever shape they have.
 */
import { TargetingRules } from '@/types/targeting';
import { isEditableTargeting, isNoRules, isPlainObject, targetingPayload } from '@/utils/experimentTargeting';
import { isValueFlagType, parseValue, valueToText } from '@/utils/flagValues';
import { FLAG_OPERATOR_OPTIONS, jsonToRules } from '@/utils/targeting';

/**
 * Whether a rule's own `value` (#2012) is one the flag page's Serve control
 * can show and send back exactly: of the flag's type, as the value fields
 * read it (its text parses back to it).
 */
function servableRuleValue(valueType: string, value: unknown): boolean {
  if (!isValueFlagType(valueType) || value === null || value === undefined) return false;
  return parseValue(valueType, 'value', valueToText(valueType, value)).ok;
}

/**
 * Whether the flag page's builder can edit `stored` without changing what it means.
 *
 * The experiment page's reading (`isEditableTargeting`), plus one key the flag
 * pages alone take: the dashboard rule's own `value` (#2012), which the Serve
 * control shows and sends back. It is editable on a string, number or json
 * flag (`valueType`) whose rule has a condition, when the value is of the
 * flag's type; anywhere else a stored `value` leaves the rules read-only, so
 * nothing the page cannot show is saved over. The experiment page still reads
 * `value` as a key it does not know.
 */
export function isEditableFlagTargeting(stored: unknown, valueType?: string): boolean {
  if (isPlainObject(stored) && 'value' in stored) {
    const { value, ...rule } = stored;
    if (valueType === undefined || !servableRuleValue(valueType, value)) return false;
    if (!Array.isArray(rule.groups) || rule.groups.length === 0) return false;
    return isEditableTargeting(rule, FLAG_OPERATOR_OPTIONS);
  }
  return isEditableTargeting(stored, FLAG_OPERATOR_OPTIONS);
}

/** The stored dashboard rule's own value, or `undefined` when it has none. */
export function storedRuleValue(stored: unknown): unknown {
  return isPlainObject(stored) && 'value' in stored ? stored.value : undefined;
}

/** The builder state the flag page opens with for an editable stored value. */
export function rulesFromStored(stored: unknown): TargetingRules {
  return jsonToRules(isNoRules(stored) ? null : (stored as object));
}

/**
 * Shown over a flag's rules when NOT groups are the only reason the builder
 * cannot show them (#918). The server applies such rules as stored, so the
 * general note, which says they may not be, would be wrong for them.
 */
export const NOT_GROUPS_NOTE =
  'The rule builder cannot show NOT groups, so these rules are shown as stored. They are applied as written.';

/**
 * Whether NOT groups are the only reason the builder cannot show `stored`.
 *
 * True when the dashboard-shape value has a `logical_operator` of NOT, in any
 * case, at the top level or in a group, and a copy in which every logical
 * operator is uppercased and every NOT replaced by AND is editable
 * (`isEditableFlagTargeting`). The API accepts `and`, `or` and `not` in any
 * case and stores them as sent, while the builder reads only `AND` and `OR`,
 * so uppercasing every operator in the copy is what lets
 * `{"logical_operator": "and", "groups": [{"logical_operator": "not", ...}]}`
 * count as a NOT rule rather than as rules the builder refuses for their case.
 * The copy exists only for this test; nothing is saved from it, and the page
 * keeps showing `stored` exactly as returned.
 *
 * Any other reason the builder refuses the rules (a `rollout_percentage`, the
 * native shape, an operator the builder does not offer) answers false, with
 * or without a NOT, so those rules keep the general note.
 */
export function usesNotGroups(stored: unknown): boolean {
  if (!isPlainObject(stored)) return false;
  let sawNot = false;
  const asBuilder = (operator: unknown): unknown => {
    if (typeof operator !== 'string') return operator;
    const upper = operator.toUpperCase();
    if (upper !== 'NOT') return upper;
    sawNot = true;
    return 'AND';
  };
  const copy: Record<string, unknown> = { ...stored };
  if ('logical_operator' in copy) copy.logical_operator = asBuilder(copy.logical_operator);
  if (Array.isArray(copy.groups)) {
    copy.groups = copy.groups.map((group) =>
      isPlainObject(group) && 'logical_operator' in group
        ? { ...group, logical_operator: asBuilder(group.logical_operator) }
        : group,
    );
  }
  return sawNot && isEditableFlagTargeting(copy);
}

/** Equality of two JSON values, ignoring the order of object keys. */
export function sameJson(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) || Array.isArray(b)) {
    return (
      Array.isArray(a) &&
      Array.isArray(b) &&
      a.length === b.length &&
      a.every((item, i) => sameJson(item, b[i]))
    );
  }
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false;
  const left = a as Record<string, unknown>;
  const right = b as Record<string, unknown>;
  const keys = Object.keys(left);
  if (keys.length !== Object.keys(right).length) return false;
  return keys.every((key) => Object.prototype.hasOwnProperty.call(right, key) && sameJson(left[key], right[key]));
}

/**
 * What a flag save sends as `targeting_rules`, or `undefined` to leave it out.
 *
 * - `rules` is null (stored rules shown read-only, not replaced): left out.
 * - `replaced` (the user confirmed "Replace rules"): always sent, even when
 *   the replacement has no groups, which sends `{}` (no rules).
 * - Otherwise: sent only when `targetingPayload(rules, stored)` differs from
 *   the payload of the state the page loaded from `stored`. Comparing with
 *   `stored` itself would always see a change: the payload drops the
 *   builder's group and condition ids and writes an absent logical operator
 *   as AND.
 */
export function targetingToSend(
  rules: TargetingRules | null,
  stored: unknown,
  replaced: boolean,
): Record<string, unknown> | undefined {
  if (rules === null) return undefined;
  const payload = targetingPayload(rules, stored);
  if (replaced) return payload;
  const baseline = targetingPayload(rulesFromStored(stored), stored);
  return sameJson(payload, baseline) ? undefined : payload;
}

/**
 * What the dashboard rule serves (#2012): its default (the split, or the on
 * value when there is none), or `value`, a value of its own.
 */
export type RuleServe = { own: false } | { own: true; value: unknown };

/** *payload* with the rule's own value, which only a rule with a condition can carry. */
function withRuleValue(payload: Record<string, unknown>, rules: TargetingRules, serve: RuleServe) {
  return serve.own && rules.groups.length > 0 ? { ...payload, value: serve.value } : payload;
}

/**
 * What a flag page's save sends as `targeting_rules`, the rule's own value
 * counted as part of the rules: `targetingToSend`'s rule, with the stored
 * value in the baseline. So a rule left as it was, value included, is not
 * sent; changing only what it serves sends the rules with the new value (or
 * without one, for the default). Rules with no group carry no value: the API
 * refuses one there.
 */
export function flagTargetingToSend(
  rules: TargetingRules | null,
  serve: RuleServe,
  stored: unknown,
  replaced: boolean,
): Record<string, unknown> | undefined {
  if (rules === null) return undefined;
  const payload = withRuleValue(targetingPayload(rules, stored), rules, serve);
  if (replaced) return payload;
  const storedRules = rulesFromStored(stored);
  const storedValue = storedRuleValue(stored);
  const storedServe: RuleServe = storedValue === undefined ? { own: false } : { own: true, value: storedValue };
  const baseline = withRuleValue(targetingPayload(storedRules, stored), storedRules, storedServe);
  return sameJson(payload, baseline) ? undefined : payload;
}
