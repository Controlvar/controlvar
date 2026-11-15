import {
  TargetingCondition,
  TargetingRuleGroup,
  TargetingRules,
  OperatorType,
  AttributeSuggestion,
  COMMON_ATTRIBUTES,
  FORMER_SUGGESTIONS,
  FORMERLY_CUSTOM,
  OPERATORS_BY_TYPE,
  SEGMENT_ATTRIBUTE,
  SEGMENT_OPERATORS,
} from '@/types/targeting';

/** Every attribute the builder has a type for: the suggestions and the former ones (#1775). */
const KNOWN_ATTRIBUTES: AttributeSuggestion[] = [...COMMON_ATTRIBUTES, ...FORMER_SUGGESTIONS];

// Simple counter-based ID generator that works in both browser and Node test environments
let _idCounter = 0;
function generateId(): string {
  _idCounter += 1;
  const timestamp = Date.now();
  return `id-${timestamp}-${_idCounter}`;
}

export function createEmptyCondition(): TargetingCondition {
  return {
    id: generateId(),
    attribute: '',
    operator: 'equals',
    value: '',
  };
}

export function createEmptyGroup(): TargetingRuleGroup {
  return {
    id: generateId(),
    logical_operator: 'AND',
    conditions: [createEmptyCondition()],
  };
}

export function createEmptyRules(): TargetingRules {
  return {
    logical_operator: 'AND',
    groups: [],
  };
}

export interface ValidationResult {
  valid: boolean;
  errors: string[];
}

export function validateRules(rules: TargetingRules): ValidationResult {
  const errors: string[] = [];

  for (let gi = 0; gi < rules.groups.length; gi++) {
    const group = rules.groups[gi];
    for (let ci = 0; ci < group.conditions.length; ci++) {
      const condition = group.conditions[ci];
      const condLabel = `Group ${gi + 1}, Condition ${ci + 1}`;

      if (!condition.attribute || condition.attribute.trim() === '') {
        errors.push(`${condLabel}: attribute is required`);
      }

      if (!condition.operator) {
        errors.push(`${condLabel}: operator is required`);
      }

      const validOperators: OperatorType[] = [
        'equals', 'not_equals', 'contains', 'not_contains',
        'starts_with', 'ends_with', 'greater_than', 'less_than',
        'greater_than_or_equal', 'less_than_or_equal',
        'in', 'not_in', 'regex', 'is_null', 'is_not_null',
        'semver_eq', 'semver_gt', 'semver_lt', 'semver_gte', 'semver_lte',
        'geo_within_radius', 'time_window', 'array_contains', 'array_intersects',
        'in_segment', 'not_in_segment',
      ];

      if (condition.operator && !validOperators.includes(condition.operator)) {
        errors.push(`${condLabel}: invalid operator "${condition.operator}"`);
      }

      // Value is required unless operator is is_null or is_not_null
      const noValueOperators: OperatorType[] = ['is_null', 'is_not_null'];
      if (!noValueOperators.includes(condition.operator)) {
        if (condition.value === null || condition.value === undefined || condition.value === '') {
          errors.push(`${condLabel}: value is required for operator "${condition.operator}"`);
        }
      }
    }
  }

  return { valid: errors.length === 0, errors };
}

interface ConditionJson {
  attribute: string;
  operator: OperatorType;
  value: string | number | boolean | string[] | null;
}

interface GroupJson {
  logical_operator: string;
  conditions: ConditionJson[];
}

interface RulesJson {
  logical_operator: string;
  groups: GroupJson[];
}

export function rulesToJson(rules: TargetingRules): RulesJson {
  return {
    logical_operator: rules.logical_operator,
    groups: rules.groups.map((group) => ({
      logical_operator: group.logical_operator,
      conditions: group.conditions.map((condition) => ({
        attribute: condition.attribute,
        operator: condition.operator,
        value: condition.value,
      })),
    })),
  };
}

export function jsonToRules(json: object | null): TargetingRules {
  if (!json) {
    return createEmptyRules();
  }

  const raw = json as Partial<RulesJson>;

  return {
    logical_operator: (raw.logical_operator as 'AND' | 'OR') || 'AND',
    groups: (raw.groups || []).map((group) => ({
      id: generateId(),
      logical_operator: (group.logical_operator as 'AND' | 'OR') || 'AND',
      conditions: (group.conditions || []).map((condition) => ({
        id: generateId(),
        attribute: condition.attribute || '',
        operator: (condition.operator as OperatorType) || 'equals',
        value: condition.value ?? '',
      })),
    })),
  };
}

export function getAttributeType(attribute: string): string {
  const found = KNOWN_ATTRIBUTES.find((a) => a.value === attribute);
  return found ? found.type : 'string';
}

/**
 * How a page's rule builder offers operators. Pages differ only here, and the
 * same options decide what a page treats as editable (`isEditableTargeting`),
 * so the dropdown and the editability check cannot disagree.
 */
export interface OperatorOptions {
  /**
   * Also offer the version operators (`semver_*`) for an attribute the builder
   * has no type for (one of neither `COMMON_ATTRIBUTES` nor
   * `FORMER_SUGGESTIONS`), such as `os_version`, and for the `FORMERLY_CUSTOM`
   * suggestions. `app_version` and the former `app.version` offer them either
   * way. The flag pages turn this on; the experiment pages do not.
   */
  semverOnCustomAttributes?: boolean;
  /**
   * Offer `in_segment` / `not_in_segment` on the attribute `segment`. On by
   * default (flag and experiment rules); a segment's own rules turn it off,
   * because a segment cannot contain a segment.
   */
  allowSegments?: boolean;
}

/** The flag pages' builder: version operators on any attribute the builder has no type for, and on `FORMERLY_CUSTOM`. */
export const FLAG_OPERATOR_OPTIONS: OperatorOptions = { semverOnCustomAttributes: true };

/** A segment's own rule builder: no segment operators (a segment cannot contain a segment). */
export const SEGMENT_RULES_OPERATOR_OPTIONS: OperatorOptions = { allowSegments: false };

/**
 * The operators the builder offers for `attribute`, under a page's options.
 *
 * The attribute `segment` also gets the two segment operators (unless
 * `allowSegments` is false). It keeps the text operators too: a context
 * attribute that happens to be called `segment` is still compared with
 * `equals` and the rest, and only the operator decides which a row is.
 */
export function getOperatorsForAttribute(attribute: string, options: OperatorOptions = {}): OperatorType[] {
  const type = getAttributeType(attribute);
  let operators = OPERATORS_BY_TYPE[type] || OPERATORS_BY_TYPE['string'];
  const custom = !KNOWN_ATTRIBUTES.some((a) => a.value === attribute);
  if (FORMERLY_CUSTOM.includes(attribute)) {
    // Typed operators first, so they lead the dropdown, then every one the
    // name had as a custom attribute before #1775, without repeats.
    const asCustom = options.semverOnCustomAttributes
      ? [...OPERATORS_BY_TYPE['string'], ...OPERATORS_BY_TYPE['semver']]
      : OPERATORS_BY_TYPE['string'];
    operators = Array.from(new Set([...operators, ...asCustom]));
  } else if (options.semverOnCustomAttributes && custom) {
    operators = [...operators, ...OPERATORS_BY_TYPE['semver']];
  }
  if (attribute === SEGMENT_ATTRIBUTE && options.allowSegments !== false) {
    operators = [...operators, ...SEGMENT_OPERATORS];
  }
  return operators;
}

/** A segment id as the API writes it: a lowercase canonical UUID. */
export const SEGMENT_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
