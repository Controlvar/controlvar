/**
 * No rule saved before #1775 loses an operator, on any page (#1775).
 *
 * #1775 changed the suggestion list: `device_type`, `app_version` and
 * `account_age_days` became typed suggestions, and `device.type`,
 * `app.version`, `user.age` and `session.new_user` stopped being suggested.
 * Which operators a page offers for an attribute decides whether a saved rule
 * opens in the builder or read-only (`isEditableTargeting`). So any operator
 * a page offered for a name before #1775 must still be offered, or a rule a
 * user built in the dashboard opens read-only under a note saying it was not
 * built there.
 *
 * What the pages offered at 695e51c7 is written out below, not computed with
 * the code under test: each name's type then ('custom' for a name the list
 * did not hold), and that commit's operators per type. A custom attribute got
 * the text operators, plus the version operators on the flag pages.
 */
import {
  COMMON_ATTRIBUTES,
  FORMER_SUGGESTIONS,
  OperatorType,
} from '@/types/targeting';
import { isEditableTargeting } from '@/utils/experimentTargeting';
import { isEditableFlagTargeting } from '@/utils/flagTargeting';
import {
  FLAG_OPERATOR_OPTIONS,
  OperatorOptions,
  SEGMENT_RULES_OPERATOR_OPTIONS,
  getOperatorsForAttribute,
} from '@/utils/targeting';

/** OPERATORS_BY_TYPE at 695e51c7. */
const OPERATORS_695: Record<string, OperatorType[]> = {
  string: ['equals', 'not_equals', 'contains', 'not_contains', 'starts_with', 'ends_with', 'regex', 'in', 'not_in', 'is_null', 'is_not_null'],
  number: ['equals', 'not_equals', 'greater_than', 'less_than', 'greater_than_or_equal', 'less_than_or_equal', 'in', 'not_in', 'is_null', 'is_not_null'],
  boolean: ['equals', 'not_equals', 'is_null', 'is_not_null'],
  array: ['array_contains', 'array_intersects', 'is_null', 'is_not_null'],
  semver: ['semver_eq', 'semver_gt', 'semver_lt', 'semver_gte', 'semver_lte'],
};

/** Each name's type at 695e51c7: its COMMON_ATTRIBUTES entry then, or 'custom'. */
const TYPE_695: Record<string, string> = {
  'user.country': 'string',
  'user.plan': 'string',
  'user.age': 'number',
  'user.email': 'string',
  'device.type': 'string',
  'app.version': 'semver',
  'session.new_user': 'boolean',
  'user.tags': 'array',
  account_age_days: 'custom',
  device_type: 'custom',
  app_version: 'custom',
  os_version: 'custom',
};

interface Page {
  options: OperatorOptions;
  editable: (rules: Record<string, unknown>) => boolean;
  semverOnCustom: boolean;
}

const PAGES: Record<string, Page> = {
  flag: { options: FLAG_OPERATOR_OPTIONS, editable: (r) => isEditableFlagTargeting(r), semverOnCustom: true },
  experiment: { options: {}, editable: (r) => isEditableTargeting(r), semverOnCustom: false },
  segment: {
    options: SEGMENT_RULES_OPERATOR_OPTIONS,
    editable: (r) => isEditableTargeting(r, SEGMENT_RULES_OPERATOR_OPTIONS),
    semverOnCustom: false,
  },
};

function offeredAt695(attribute: string, page: Page): OperatorType[] {
  const type = TYPE_695[attribute];
  if (type !== 'custom') return OPERATORS_695[type];
  return page.semverOnCustom ? [...OPERATORS_695.string, ...OPERATORS_695.semver] : OPERATORS_695.string;
}

function savedRule(attribute: string, operator: OperatorType): Record<string, unknown> {
  const value = operator === 'is_null' || operator === 'is_not_null' ? null : '1.2.3';
  return { logical_operator: 'AND', groups: [{ logical_operator: 'AND', conditions: [{ attribute, operator, value }] }] };
}

const ATTRIBUTES = Object.keys(TYPE_695);
const CASES = Object.keys(PAGES).flatMap((page) => ATTRIBUTES.map((attribute) => [page, attribute] as const));

describe('no rule saved before #1775 loses an operator', () => {
  it('knows what 695e51c7 offered for every name the lists hold now', () => {
    const names = [...COMMON_ATTRIBUTES, ...FORMER_SUGGESTIONS].map((a) => a.value);
    expect(names.filter((name) => !(name in TYPE_695))).toEqual([]);
  });

  it.each(CASES)('the %s page still offers %s every operator it did', (pageName, attribute) => {
    const page = PAGES[pageName];
    const now = getOperatorsForAttribute(attribute, page.options);
    expect(offeredAt695(attribute, page).filter((op) => !now.includes(op))).toEqual([]);
  });

  it.each(CASES)('a rule saved on the %s page with %s still opens in the builder, whatever its operator', (pageName, attribute) => {
    const page = PAGES[pageName];
    const readOnly = offeredAt695(attribute, page).filter((op) => !page.editable(savedRule(attribute, op)));
    expect(readOnly).toEqual([]);
  });
});
