import React, { useCallback, useEffect, useRef, useState } from 'react';
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
  firstInvalid,
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
 * A page's two value fields: their text, their refusals, and the elements, so
 * focus can move to the first refused one.
 *
 * Focus moves in an effect, after React has committed the refusals: the field
 * that takes focus is already `aria-invalid` and described by its refusal, so a
 * screen reader announces the refusal with the field. Focusing in the same
 * tick as `setErrors` would land on the field as it was before.
 */
export function useFlagValueFields() {
  const [texts, setTexts] = useState<ValueTexts>({ on_value: '', off_value: '' });
  const [errors, setErrors] = useState<ValueErrors>({});
  const fieldRefs: ValueFieldRefs = useRef({});
  const focusPending = useRef(false);

  useEffect(() => {
    if (!focusPending.current) return;
    focusPending.current = false;
    const first = firstInvalid(errors);
    if (first) fieldRefs.current[first]?.focus();
  }, [errors]);

  /** Show the refusals at their fields; the first takes focus once they are rendered. */
  const showErrors = useCallback((next: ValueErrors) => {
    focusPending.current = true;
    setErrors(next);
  }, []);

  /** The user typed in *field*: keep the text, and clear that field's refusal. */
  const change = useCallback((field: ValueField, text: string) => {
    setTexts((current) => ({ ...current, [field]: text }));
    setErrors((current) => {
      if (!current[field]) return current;
      const copy = { ...current };
      delete copy[field];
      return copy;
    });
  }, []);

  return { texts, setTexts, errors, setErrors, showErrors, change, fieldRefs };
}

/**
 * The On value and Off value fields of a string, number or JSON flag (#1497).
 *
 * Each has a `<label for>`, a hint, and, when refused, the reason under it in
 * words, linked by `aria-describedby` with `aria-invalid` set.
 * - A string is a text area of two rows (it may be empty): a text input would
 *   drop the newlines of a stored string the moment its field is edited.
 * - A number is a plain text box (no `type=number`, and no `inputmode`: the
 *   iOS decimal pad has no minus sign).
 * - A JSON value is a monospace text area that scrolls long lines inside
 *   itself.
 * Neither text area takes Tab: it moves focus as usual.
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
            ) : type === 'string' ? (
              <textarea
                {...common}
                ref={(node) => {
                  fieldRefs.current[field] = node;
                }}
                onChange={(e) => onChange(field, e.target.value)}
                rows={2}
                autoComplete="off"
                className={`${CONTROL} resize-y ${error ? INVALID : ''}`}
              />
            ) : (
              <input
                {...common}
                ref={(node) => {
                  fieldRefs.current[field] = node;
                }}
                type="text"
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => onChange(field, e.target.value)}
                className={`${CONTROL} font-mono ${error ? INVALID : ''}`}
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
