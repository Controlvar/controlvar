import React from 'react';
import {
  TargetingCondition,
  OperatorType,
  COMMON_ATTRIBUTES,
  OPERATOR_LABELS,
  OPERATORS_BY_TYPE,
  SEGMENT_ATTRIBUTE,
  isSegmentOperator,
} from '@/types/targeting';
import { getAttributeType, getOperatorsForAttribute, OperatorOptions } from '@/utils/targeting';
import { SegmentPicker } from './SegmentPicker';

interface ConditionRowProps {
  condition: TargetingCondition;
  onChange: (updated: TargetingCondition) => void;
  onRemove: () => void;
  readOnly?: boolean;
  /**
   * Where this condition is, for the accessible names of its controls
   * ("Group 1, condition 2 attribute"). Labels only; it changes no behaviour.
   */
  label?: string;
  /** Which operators the dropdown offers; see `OperatorOptions`. */
  operatorOptions?: OperatorOptions;
}

const NO_VALUE_OPERATORS: OperatorType[] = ['is_null', 'is_not_null'];
const MULTI_VALUE_OPERATORS: OperatorType[] = ['in', 'not_in'];

export function ConditionRow({
  condition,
  onChange,
  onRemove,
  readOnly = false,
  label = 'Condition',
  operatorOptions,
}: ConditionRowProps) {
  const availableOperators = getOperatorsForAttribute(condition.attribute, operatorOptions);
  // The operator, not the attribute, decides what kind of row this is: a
  // context attribute called `segment` compared with `equals` is a text row.
  const isSegmentRow = isSegmentOperator(condition.operator);
  const showValueInput = !isSegmentRow && !NO_VALUE_OPERATORS.includes(condition.operator);
  const isMultiValue = MULTI_VALUE_OPERATORS.includes(condition.operator);
  const segmentsOffered = operatorOptions?.allowSegments !== false;

  const handleAttributeChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const newAttribute = e.target.value;
    const newOperators = getOperatorsForAttribute(newAttribute, operatorOptions);
    // Keep the current operator if the new attribute offers it; otherwise
    // reset to the first available. An attribute typed as a version
    // (`app_version`) keeps only a version operator: it also offers the text
    // ones, for rules saved before #1775, but starts on its first typed
    // operator, `semver_eq`, as `app.version` did.
    const versionAttribute = getAttributeType(newAttribute) === 'semver';
    const keep =
      newOperators.includes(condition.operator) &&
      (!versionAttribute || OPERATORS_BY_TYPE['semver'].includes(condition.operator));
    const newOperator = keep ? condition.operator : newOperators[0];
    // A segment id is not a text value, and text is not a segment id.
    const kindChanged = isSegmentOperator(newOperator) !== isSegmentOperator(condition.operator);
    onChange({
      ...condition,
      attribute: newAttribute,
      operator: newOperator,
      value: kindChanged ? '' : condition.value,
    });
  };

  const handleOperatorChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const newOperator = e.target.value as OperatorType;
    const kindChanged = isSegmentOperator(newOperator) !== isSegmentOperator(condition.operator);
    const newValue = NO_VALUE_OPERATORS.includes(newOperator) ? null : kindChanged ? '' : condition.value;
    onChange({ ...condition, operator: newOperator, value: newValue });
  };

  const handleValueChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    onChange({ ...condition, value: e.target.value });
  };

  const datalistId = `attr-suggestions-${condition.id}`;

  return (
    <div
      className="flex gap-2 items-center p-2 rounded border border-slate-200"
      data-testid="condition-row"
    >
      {/* Attribute input with datalist */}
      <input
        data-testid="condition-attribute"
        aria-label={`${label} attribute`}
        type="text"
        list={datalistId}
        value={condition.attribute}
        onChange={handleAttributeChange}
        disabled={readOnly}
        placeholder="attribute"
        className="flex-1 min-w-0 rounded border border-slate-300 px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400 disabled:bg-slate-100 disabled:text-slate-500"
      />
      <datalist id={datalistId}>
        {COMMON_ATTRIBUTES.map((attr) => (
          <option key={attr.value} value={attr.value}>
            {attr.label}
          </option>
        ))}
        {segmentsOffered && <option value={SEGMENT_ATTRIBUTE}>Segment</option>}
      </datalist>

      {/* Operator dropdown */}
      <select
        data-testid="condition-operator"
        aria-label={`${label} operator`}
        value={condition.operator}
        onChange={handleOperatorChange}
        disabled={readOnly}
        className="rounded border border-slate-300 px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400 disabled:bg-slate-100 disabled:text-slate-500"
      >
        {availableOperators.map((op) => (
          <option key={op} value={op}>
            {OPERATOR_LABELS[op]}
          </option>
        ))}
      </select>

      {/* A segment condition's value is a segment, chosen from a list */}
      {isSegmentRow && (
        <SegmentPicker
          label={label}
          value={typeof condition.value === 'string' ? condition.value : ''}
          onChange={(segmentId) => onChange({ ...condition, value: segmentId })}
          readOnly={readOnly}
        />
      )}

      {/* Value input (hidden for is_null / is_not_null and segment rows) */}
      {showValueInput && (
        <input
          data-testid="condition-value"
          aria-label={`${label} value`}
          type="text"
          value={condition.value === null || condition.value === undefined ? '' : String(condition.value)}
          onChange={handleValueChange}
          disabled={readOnly}
          placeholder={isMultiValue ? 'value1, value2' : 'value'}
          className="flex-1 min-w-0 rounded border border-slate-300 px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400 disabled:bg-slate-100 disabled:text-slate-500"
        />
      )}

      {/* Remove button */}
      {!readOnly && (
        <button
          data-testid="condition-remove"
          type="button"
          onClick={onRemove}
          className="flex-shrink-0 w-6 h-6 flex items-center justify-center rounded text-red-700 hover:bg-red-50 hover:text-red-800 transition-colors"
          aria-label="Remove condition"
        >
          &times;
        </button>
      )}
    </div>
  );
}
