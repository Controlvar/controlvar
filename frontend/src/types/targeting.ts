export type OperatorType =
  | 'equals' | 'not_equals' | 'contains' | 'not_contains'
  | 'starts_with' | 'ends_with' | 'greater_than' | 'less_than'
  | 'greater_than_or_equal' | 'less_than_or_equal'
  | 'in' | 'not_in' | 'regex' | 'is_null' | 'is_not_null'
  | 'semver_eq' | 'semver_gt' | 'semver_lt' | 'semver_gte' | 'semver_lte'
  | 'geo_within_radius' | 'time_window' | 'array_contains' | 'array_intersects'
  | 'in_segment' | 'not_in_segment';

/**
 * Segment membership (#440). A condition `{"attribute": "segment", "operator":
 * "in_segment", "value": "<segment id>"}` asks the server whether the user is a
 * member; the value is a segment's id, chosen from a list, not typed.
 */
export const SEGMENT_OPERATORS: readonly OperatorType[] = ['in_segment', 'not_in_segment'];

/** The attribute a segment condition names. */
export const SEGMENT_ATTRIBUTE = 'segment';

/** Whether `operator` is one of the two segment operators. */
export function isSegmentOperator(operator: unknown): boolean {
  return operator === 'in_segment' || operator === 'not_in_segment';
}

export type LogicalOperator = 'AND' | 'OR';

export interface TargetingCondition {
  id: string;
  attribute: string;
  operator: OperatorType;
  value: string | number | boolean | string[] | null;
}

export interface TargetingRuleGroup {
  id: string;
  logical_operator: LogicalOperator;
  conditions: TargetingCondition[];
}

export interface TargetingRules {
  logical_operator: LogicalOperator;
  groups: TargetingRuleGroup[];
}

export interface AttributeSuggestion {
  value: string;
  label: string;
  type: 'string' | 'number' | 'boolean' | 'array' | 'semver';
  examples?: string[];
}

/**
 * The attributes the rule builder suggests (#1775). Each one is a key the
 * "Common Targeting Attributes" table in docs/getting-started/concepts.md
 * tells an application to send, or `user.<key>` for one of them (the server
 * answers `user.country` from `{"country": "US"}`), with the table's type. A
 * name the docs do not give builds a rule that matches nobody whose app
 * follows them: `device.type` never matched `{"device_type": "mobile"}`.
 * backend/tests/unit/docs/test_rule_builder_suggestions.py evaluates each one,
 * as the server does, against a context built from the table.
 */
export const COMMON_ATTRIBUTES: AttributeSuggestion[] = [
  { value: 'user.country', label: 'User Country', type: 'string', examples: ['US', 'UK', 'DE'] },
  { value: 'user.plan', label: 'User Plan', type: 'string', examples: ['free', 'pro', 'business'] },
  { value: 'account_age_days', label: 'Account Age (days)', type: 'number' },
  { value: 'user.email', label: 'User Email', type: 'string' },
  { value: 'device_type', label: 'Device Type', type: 'string', examples: ['mobile', 'desktop', 'tablet'] },
  { value: 'app_version', label: 'App Version', type: 'semver', examples: ['1.0.0', '2.3.1'] },
  { value: 'user.tags', label: 'User Tags', type: 'array' },
];

/**
 * Attributes the builder suggested before #1775 and no longer does: an app
 * that sends the keys the docs table names never answers them (`device.type`
 * answers a nested `{"device": {"type": "mobile"}}`, not `{"device_type":
 * "mobile"}`; `app.version` answers `{"app": {"version": "2.3.1"}}`, not
 * `{"app_version": "2.3.1"}`). A rule saved with one is evaluated as before,
 * and the builder keeps its type, so the rule offers the operators it was
 * built with and stays editable. They are not in the attribute list.
 */
export const FORMER_SUGGESTIONS: AttributeSuggestion[] = [
  { value: 'user.age', label: 'User Age', type: 'number' },
  { value: 'device.type', label: 'Device Type', type: 'string' },
  { value: 'app.version', label: 'App Version', type: 'semver' },
  { value: 'session.new_user', label: 'New User', type: 'boolean' },
];

/**
 * Suggestions that were custom attributes before #1775: the docs table's own
 * names, which a user could type in by hand. A custom attribute is offered the
 * text operators, and on the flag pages the version operators too. These keep
 * all of those after their typed ones (`getOperatorsForAttribute`), so a rule
 * saved with one before #1775 loses no operator and still opens in the
 * builder.
 */
export const FORMERLY_CUSTOM: readonly string[] = ['account_age_days', 'device_type', 'app_version'];

export const OPERATORS_BY_TYPE: Record<string, OperatorType[]> = {
  string: ['equals', 'not_equals', 'contains', 'not_contains', 'starts_with', 'ends_with', 'regex', 'in', 'not_in', 'is_null', 'is_not_null'],
  number: ['equals', 'not_equals', 'greater_than', 'less_than', 'greater_than_or_equal', 'less_than_or_equal', 'in', 'not_in', 'is_null', 'is_not_null'],
  boolean: ['equals', 'not_equals', 'is_null', 'is_not_null'],
  array: ['array_contains', 'array_intersects', 'is_null', 'is_not_null'],
  semver: ['semver_eq', 'semver_gt', 'semver_lt', 'semver_gte', 'semver_lte'],
};

export const OPERATOR_LABELS: Record<OperatorType, string> = {
  equals: 'equals',
  not_equals: 'does not equal',
  contains: 'contains',
  not_contains: 'does not contain',
  starts_with: 'starts with',
  ends_with: 'ends with',
  greater_than: 'greater than',
  less_than: 'less than',
  greater_than_or_equal: 'greater than or equal to',
  less_than_or_equal: 'less than or equal to',
  in: 'is one of',
  not_in: 'is not one of',
  regex: 'matches regex',
  is_null: 'is empty',
  is_not_null: 'is not empty',
  semver_eq: 'version equals',
  semver_gt: 'version greater than',
  semver_lt: 'version less than',
  semver_gte: 'version >=',
  semver_lte: 'version <=',
  geo_within_radius: 'within radius of',
  time_window: 'within time window',
  array_contains: 'array contains',
  array_intersects: 'array intersects',
  in_segment: 'is in segment',
  not_in_segment: 'is not in segment',
};
