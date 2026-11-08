import {
  createEmptyCondition,
  createEmptyGroup,
  createEmptyRules,
  validateRules,
  rulesToJson,
  jsonToRules,
  getAttributeType,
  getOperatorsForAttribute,
  FLAG_OPERATOR_OPTIONS,
} from '@/utils/targeting';
import { COMMON_ATTRIBUTES, OPERATORS_BY_TYPE, TargetingRules } from '@/types/targeting';

describe('createEmptyCondition', () => {
  it('returns an object with id, attribute, operator, value', () => {
    const condition = createEmptyCondition();
    expect(condition).toHaveProperty('id');
    expect(condition.id).toBeTruthy();
    expect(condition.attribute).toBe('');
    expect(condition.operator).toBe('equals');
    expect(condition.value).toBe('');
  });

  it('generates unique ids on each call', () => {
    const c1 = createEmptyCondition();
    const c2 = createEmptyCondition();
    expect(c1.id).not.toBe(c2.id);
  });
});

describe('createEmptyGroup', () => {
  it('returns a group with id, AND logical_operator, and one condition', () => {
    const group = createEmptyGroup();
    expect(group).toHaveProperty('id');
    expect(group.id).toBeTruthy();
    expect(group.logical_operator).toBe('AND');
    expect(group.conditions).toHaveLength(1);
  });

  it('generates unique ids on each call', () => {
    const g1 = createEmptyGroup();
    const g2 = createEmptyGroup();
    expect(g1.id).not.toBe(g2.id);
  });
});

describe('createEmptyRules', () => {
  it('returns rules with AND operator and empty groups array', () => {
    const rules = createEmptyRules();
    expect(rules.logical_operator).toBe('AND');
    expect(rules.groups).toHaveLength(0);
  });
});

describe('validateRules', () => {
  it('returns valid=true for empty groups', () => {
    const rules = createEmptyRules();
    const result = validateRules(rules);
    expect(result.valid).toBe(true);
    expect(result.errors).toHaveLength(0);
  });

  it('returns error for empty attribute', () => {
    const rules: TargetingRules = {
      logical_operator: 'AND',
      groups: [
        {
          id: 'g1',
          logical_operator: 'AND',
          conditions: [
            { id: 'c1', attribute: '', operator: 'equals', value: 'test' },
          ],
        },
      ],
    };
    const result = validateRules(rules);
    expect(result.valid).toBe(false);
    expect(result.errors.some((e) => e.includes('attribute is required'))).toBe(true);
  });

  it('returns error for empty value when operator requires one', () => {
    const rules: TargetingRules = {
      logical_operator: 'AND',
      groups: [
        {
          id: 'g1',
          logical_operator: 'AND',
          conditions: [
            { id: 'c1', attribute: 'user.country', operator: 'equals', value: '' },
          ],
        },
      ],
    };
    const result = validateRules(rules);
    expect(result.valid).toBe(false);
    expect(result.errors.some((e) => e.includes('value is required'))).toBe(true);
  });

  it('does not require value for is_null operator', () => {
    const rules: TargetingRules = {
      logical_operator: 'AND',
      groups: [
        {
          id: 'g1',
          logical_operator: 'AND',
          conditions: [
            { id: 'c1', attribute: 'user.email', operator: 'is_null', value: null },
          ],
        },
      ],
    };
    const result = validateRules(rules);
    expect(result.valid).toBe(true);
  });

  it('does not require value for is_not_null operator', () => {
    const rules: TargetingRules = {
      logical_operator: 'AND',
      groups: [
        {
          id: 'g1',
          logical_operator: 'AND',
          conditions: [
            { id: 'c1', attribute: 'user.email', operator: 'is_not_null', value: null },
          ],
        },
      ],
    };
    const result = validateRules(rules);
    expect(result.valid).toBe(true);
  });

  it('returns valid for a properly filled condition', () => {
    const rules: TargetingRules = {
      logical_operator: 'AND',
      groups: [
        {
          id: 'g1',
          logical_operator: 'AND',
          conditions: [
            { id: 'c1', attribute: 'user.country', operator: 'equals', value: 'US' },
          ],
        },
      ],
    };
    const result = validateRules(rules);
    expect(result.valid).toBe(true);
    expect(result.errors).toHaveLength(0);
  });

  it('collects multiple errors', () => {
    const rules: TargetingRules = {
      logical_operator: 'AND',
      groups: [
        {
          id: 'g1',
          logical_operator: 'AND',
          conditions: [
            { id: 'c1', attribute: '', operator: 'equals', value: '' },
            { id: 'c2', attribute: 'user.plan', operator: 'equals', value: '' },
          ],
        },
      ],
    };
    const result = validateRules(rules);
    expect(result.valid).toBe(false);
    // At least: empty attribute for c1, empty value for c1, empty value for c2
    expect(result.errors.length).toBeGreaterThanOrEqual(2);
  });
});

describe('rulesToJson', () => {
  it('strips id fields from conditions and groups', () => {
    const rules: TargetingRules = {
      logical_operator: 'AND',
      groups: [
        {
          id: 'group-uuid-1',
          logical_operator: 'AND',
          conditions: [
            { id: 'cond-uuid-1', attribute: 'user.country', operator: 'equals', value: 'US' },
          ],
        },
      ],
    };
    const json = rulesToJson(rules);
    expect(json).not.toHaveProperty('id');
    const groups = json.groups as unknown as Array<Record<string, unknown>>;
    expect(groups[0]).not.toHaveProperty('id');
    const conditions = groups[0].conditions as unknown as Array<Record<string, unknown>>;
    expect(conditions[0]).not.toHaveProperty('id');
  });

  it('preserves attribute, operator, and value', () => {
    const rules: TargetingRules = {
      logical_operator: 'OR',
      groups: [
        {
          id: 'g1',
          logical_operator: 'AND',
          conditions: [
            { id: 'c1', attribute: 'user.plan', operator: 'in', value: 'pro,business' },
          ],
        },
      ],
    };
    const json = rulesToJson(rules);
    expect(json.logical_operator).toBe('OR');
    const groups = json.groups as unknown as Array<Record<string, unknown>>;
    expect(groups[0].logical_operator).toBe('AND');
    const conditions = groups[0].conditions as unknown as Array<Record<string, unknown>>;
    expect(conditions[0].attribute).toBe('user.plan');
    expect(conditions[0].operator).toBe('in');
    expect(conditions[0].value).toBe('pro,business');
  });
});

describe('jsonToRules', () => {
  it('returns empty rules for null input', () => {
    const rules = jsonToRules(null);
    expect(rules.logical_operator).toBe('AND');
    expect(rules.groups).toHaveLength(0);
  });

  it('adds id fields to groups and conditions when absent', () => {
    const json = {
      logical_operator: 'AND',
      groups: [
        {
          logical_operator: 'OR',
          conditions: [
            { attribute: 'user.country', operator: 'equals', value: 'US' },
          ],
        },
      ],
    };
    const rules = jsonToRules(json);
    expect(rules.groups[0]).toHaveProperty('id');
    expect(rules.groups[0].id).toBeTruthy();
    expect(rules.groups[0].conditions[0]).toHaveProperty('id');
    expect(rules.groups[0].conditions[0].id).toBeTruthy();
  });

  it('preserves logical operators, attributes, operators, values', () => {
    const json = {
      logical_operator: 'OR',
      groups: [
        {
          logical_operator: 'AND',
          conditions: [
            { attribute: 'user.plan', operator: 'in', value: 'pro' },
          ],
        },
      ],
    };
    const rules = jsonToRules(json);
    expect(rules.logical_operator).toBe('OR');
    expect(rules.groups[0].logical_operator).toBe('AND');
    expect(rules.groups[0].conditions[0].attribute).toBe('user.plan');
    expect(rules.groups[0].conditions[0].operator).toBe('in');
    expect(rules.groups[0].conditions[0].value).toBe('pro');
  });
});

describe('getAttributeType', () => {
  it('returns string for user.country', () => {
    expect(getAttributeType('user.country')).toBe('string');
  });

  it('returns number for user.age', () => {
    expect(getAttributeType('user.age')).toBe('number');
  });

  it('returns boolean for session.new_user', () => {
    expect(getAttributeType('session.new_user')).toBe('boolean');
  });

  it('returns array for user.tags', () => {
    expect(getAttributeType('user.tags')).toBe('array');
  });

  it('returns semver for app.version', () => {
    expect(getAttributeType('app.version')).toBe('semver');
  });

  it('defaults to string for unknown attributes', () => {
    expect(getAttributeType('custom.attribute')).toBe('string');
  });

  it.each([
    ['device_type', 'string'],
    ['app_version', 'semver'],
    ['account_age_days', 'number'],
  ])('returns the docs table type for the suggestion %s (#1775)', (attribute, type) => {
    expect(getAttributeType(attribute)).toBe(type);
  });
});

describe('the former suggestions (#1775)', () => {
  // No longer suggested, but a rule saved with one keeps the operators it was
  // built with on both builders, so it stays editable.
  it.each([
    ['user.age', 'number'],
    ['device.type', 'string'],
    ['app.version', 'semver'],
    ['session.new_user', 'boolean'],
  ])('%s keeps its %s type and operators', (attribute, type) => {
    expect(COMMON_ATTRIBUTES.map((a) => a.value)).not.toContain(attribute);
    expect(getAttributeType(attribute)).toBe(type);
    expect(getOperatorsForAttribute(attribute)).toEqual(OPERATORS_BY_TYPE[type]);
    expect(getOperatorsForAttribute(attribute, FLAG_OPERATOR_OPTIONS)).toEqual(OPERATORS_BY_TYPE[type]);
  });
});

describe('getOperatorsForAttribute', () => {
  it('returns string operators for user.country', () => {
    const ops = getOperatorsForAttribute('user.country');
    expect(ops).toContain('equals');
    expect(ops).toContain('contains');
    expect(ops).toContain('starts_with');
    expect(ops).not.toContain('semver_eq');
    expect(ops).not.toContain('array_contains');
  });

  it('returns semver operators for app.version', () => {
    const ops = getOperatorsForAttribute('app.version');
    expect(ops).toContain('semver_eq');
    expect(ops).toContain('semver_gt');
    expect(ops).toContain('semver_lt');
    expect(ops).toContain('semver_gte');
    expect(ops).toContain('semver_lte');
    expect(ops).not.toContain('contains');
  });

  it('returns number operators for user.age', () => {
    const ops = getOperatorsForAttribute('user.age');
    expect(ops).toContain('greater_than');
    expect(ops).toContain('less_than');
    expect(ops).toContain('equals');
    expect(ops).not.toContain('semver_eq');
  });

  it('returns array operators for user.tags', () => {
    const ops = getOperatorsForAttribute('user.tags');
    expect(ops).toContain('array_contains');
    expect(ops).toContain('array_intersects');
    expect(ops).not.toContain('equals');
  });

  it('returns boolean operators for session.new_user', () => {
    const ops = getOperatorsForAttribute('session.new_user');
    expect(ops).toContain('equals');
    expect(ops).toContain('not_equals');
    expect(ops).not.toContain('contains');
    expect(ops).not.toContain('greater_than');
  });

  it('defaults to string operators for unknown attribute', () => {
    const ops = getOperatorsForAttribute('unknown.attr');
    expect(ops).toContain('equals');
    expect(ops).toContain('contains');
  });

  it.each([
    ['app_version', 'semver_eq', ['equals', 'in', 'is_not_null']],
    ['account_age_days', 'equals', ['greater_than', 'contains', 'regex']],
    ['device_type', 'equals', ['in', 'starts_with']],
  ])('offers %s its typed operators first, then the ones it had as a custom attribute (#1775)', (attribute, first, kept) => {
    const ops = getOperatorsForAttribute(attribute);
    expect(ops[0]).toBe(first);
    expect(ops).toEqual(expect.arrayContaining(kept));
    expect(new Set(ops).size).toBe(ops.length);
  });
});
