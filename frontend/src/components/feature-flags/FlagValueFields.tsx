import React from 'react';
import {
  EMPTY_STRING_HINT,
  TYPE_FIXED_NOTE,
  VALUES_PUBLIC_NOTE,
  VALUE_FIELDS,
  VALUE_FIELD_HINTS,
  VALUE_FIELD_LABELS,
  VALUE_TYPES,
  VALUE_TYPE_HINTS,
  VALUE_TYPE_LABELS,
  FlagValueType,
  ValueErrors,
  ValueField,
  ValueFlagType,
  ValueTexts,
  valueToDisplay,
} from '@/utils/flagValues';

/** Where the page keeps each value field, so it can move focus to the first invalid one. */
export type ValueFieldRefs = React.MutableRefObject<
  Partial<Record<ValueField, HTMLInputElement | HTMLTextAreaElement | null>>
>;

const CONTROL =
  'w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-600';
const INVALID = 'border-red-700';

export interface FlagValueFieldsProps {
  type: ValueFlagType;
  texts: ValueTexts;
  errors: ValueErrors;
  onChange: (field: ValueField, text: string) => void;
  fieldRefs: ValueFieldRefs;
}

/**
 * The On value and Off value fields of a string, number or JSON flag (#1497).
 *
 * Each has a `<label for>`, a hint, and, when refused, the reason under it in
 * words, linked by `aria-describedby` with `aria-invalid` set. A string is a
 * one-line text box (it may be empty); a number is a plain text box (no
 * `type=number`, and no `inputmode`: the iOS decimal pad has no minus sign); a
 * JSON value is a monospace text area that leaves Tab alone and scrolls long
 * lines inside itself.
 */
export function FlagValueFields({ type, texts, errors, onChange, fieldRefs }: FlagValueFieldsProps) {
  return (
    <div className="space-y-4" data-testid="flag-value-fields">
      {VALUE_FIELDS.map((field) => {
        const id = `flag-${field.replace('_', '-')}`;
        const hintId = `${id}-hint`;
        const errorId = `${id}-error`;
        const error = errors[field];
        const describedBy = error ? `${errorId} ${hintId}` : hintId;
        const common = {
          id,
          name: field,
          value: texts[field],
          'aria-describedby': describedBy,
          'aria-invalid': error ? (true as const) : undefined,
          'aria-required': type === 'string' ? undefined : (true as const),
          'data-testid': `${id}-input`,
        };
        return (
          <div key={field}>
            <label htmlFor={id} className="block text-sm font-medium text-slate-700 mb-1">
              {VALUE_FIELD_LABELS[field]}
            </label>
            {type === 'json' ? (
              <textarea
                {...common}
                ref={(node) => {
                  fieldRefs.current[field] = node;
                }}
                onChange={(e) => onChange(field, e.target.value)}
                rows={6}
                wrap="off"
                spellCheck={false}
                className={`${CONTROL} font-mono resize-y overflow-auto ${error ? INVALID : ''}`}
              />
            ) : (
              <input
                {...common}
                ref={(node) => {
                  fieldRefs.current[field] = node;
                }}
                type="text"
                autoComplete="off"
                spellCheck={type === 'string'}
                onChange={(e) => onChange(field, e.target.value)}
                className={`${CONTROL} ${type === 'number' ? 'font-mono' : ''} ${error ? INVALID : ''}`}
              />
            )}
            {error && (
              <p id={errorId} className="mt-1 text-sm text-red-700" data-testid={errorId}>
                {error}
              </p>
            )}
            <p id={hintId} className="mt-1 text-xs text-slate-600">
              {VALUE_FIELD_HINTS[field]}
              {type === 'string' ? ` ${EMPTY_STRING_HINT}` : ''}
            </p>
          </div>
        );
      })}
      <p className="text-sm text-slate-700" data-testid="flag-values-public-note">
        {VALUES_PUBLIC_NOTE}
      </p>
    </div>
  );
}

export interface FlagTypeChoiceProps {
  value: FlagValueType;
  onChange: (type: FlagValueType) => void;
}

/**
 * The new-flag form's Type: four native radios in a fieldset, Boolean first
 * and the default. Each hint is the radio's description, so the accessible
 * names stay "Boolean", "String", "Number" and "JSON"; the arrow keys move
 * within the group as native radios do.
 */
export function FlagTypeChoice({ value, onChange }: FlagTypeChoiceProps) {
  return (
    <fieldset aria-describedby="flag-value-type-note" data-testid="flag-value-type-choice">
      <legend className="text-sm font-medium text-slate-700 mb-2">Type</legend>
      <div className="space-y-2">
        {VALUE_TYPES.map((type) => {
          const id = `flag-value-type-${type}`;
          return (
            <div key={type} className="flex items-start gap-2">
              <input
                id={id}
                type="radio"
                name="value_type"
                value={type}
                checked={value === type}
                onChange={() => onChange(type)}
                aria-describedby={`${id}-hint`}
                className="mt-0.5 h-4 w-4 shrink-0 accent-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-600 focus:ring-offset-1"
                data-testid={id}
              />
              <div className="min-w-0">
                <label htmlFor={id} className="block text-sm font-medium text-slate-800">
                  {VALUE_TYPE_LABELS[type]}
                </label>
                <p id={`${id}-hint`} className="text-xs text-slate-600">
                  {VALUE_TYPE_HINTS[type]}
                </p>
              </div>
            </div>
          );
        })}
      </div>
      <p id="flag-value-type-note" className="mt-3 text-xs text-slate-600">
        {TYPE_FIXED_NOTE}
      </p>
    </fieldset>
  );
}

/**
 * A value as an ANALYST or VIEWER reads it: JSON in a keyboard-scrollable
 * region named for its field, bounded in height, like the page's stored rules.
 */
export function FlagValueReadOnly({ field, value }: { field: ValueField; value: unknown }) {
  const id = `flag-${field.replace('_', '-')}-readonly`;
  return (
    <div>
      <p id={`${id}-label`} className="text-sm font-medium text-slate-700 mb-1">
        {VALUE_FIELD_LABELS[field]}
      </p>
      <pre
        role="region"
        aria-labelledby={`${id}-label`}
        tabIndex={0}
        className="max-h-64 overflow-auto bg-slate-50 border border-slate-200 text-xs text-slate-800 rounded-md p-3 focus:outline-none focus:ring-2 focus:ring-blue-600"
        data-testid={id}
      >
        {valueToDisplay(value)}
      </pre>
    </div>
  );
}
