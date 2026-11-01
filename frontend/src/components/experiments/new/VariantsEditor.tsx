import React, { useEffect, useState } from 'react';
import {
  allocationTotalOf,
  configurationProblems,
  ExperimentFormAction,
  hasNoShare,
  VariantFormData,
  ZERO_SHARE,
} from './formState';
import { smallInputClass } from './fieldStyles';

export const CONFIGURATION_TOGGLE = 'Configuration (JSON, optional)';
export const CONFIGURATION_HELP =
  'Your app receives this object with the assignment, for example to set the button colour. ' +
  'Leave it empty if the variant name is enough.';
export const CONFIGURATION_PLACEHOLDER = '{"button_color": "green"}';

interface VariantsEditorProps {
  variants: VariantFormData[];
  dispatch: React.Dispatch<ExperimentFormAction>;
  /**
   * Changed by the caller each time Next or Create was refused. Every row
   * whose configuration has a problem then opens, and the first one's field
   * takes focus, so a problem never sits in a closed section.
   */
  revealProblems?: number;
  /**
   * The field a refusal shown elsewhere is about, and that refusal's element
   * id. The field then carries `aria-describedby` pointing at it, and a text
   * field `aria-invalid` too. Unset in guided setup, which renders as before.
   */
  invalid?: { index: number; field: 'name' | 'traffic_allocation' | 'is_control' } | null;
  invalidDescribedBy?: string;
}

const hasText = (v: VariantFormData) => (v.configuration_text ?? '').trim() !== '';

/** The variants heading, running allocation total and one row per variant. The caller supplies the section. */
export function VariantsEditor({
  variants,
  dispatch,
  revealProblems = 0,
  invalid = null,
  invalidDescribedBy,
}: VariantsEditorProps) {
  const allocationTotal = allocationTotalOf(variants);
  const problems = configurationProblems(variants);
  // A radio takes no aria-invalid; it is described by the refusal only.
  const marks = (index: number, field: 'name' | 'traffic_allocation' | 'is_control') =>
    invalid && invalidDescribedBy && invalid.index === index && invalid.field === field
      ? {
          ...(field === 'is_control' ? {} : { 'aria-invalid': true as const }),
          'aria-describedby': invalidDescribedBy,
        }
      : {};
  // Which configuration sections are open. A row that already has text starts
  // open, so switching between guided setup and the single-page form shows it.
  const [open, setOpen] = useState<boolean[]>(() => variants.map(hasText));
  const [focusRow, setFocusRow] = useState<number | null>(null);

  useEffect(() => {
    if (revealProblems === 0) return;
    const current = configurationProblems(variants);
    const first = current.findIndex((p) => p !== null);
    if (first < 0) return;
    setOpen((prev) => variants.map((_, i) => Boolean(prev[i]) || current[i] !== null));
    setFocusRow(first);
    // Runs when the caller asks; the variants are read as they stand.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revealProblems]);

  useEffect(() => {
    if (focusRow === null) return;
    document.getElementById(`variant-configuration-${focusRow}`)?.focus();
    setFocusRow(null);
  }, [focusRow, open]);

  const toggle = (index: number) =>
    setOpen((prev) => variants.map((_, i) => (i === index ? !prev[i] : Boolean(prev[i]))));

  const addVariant = () => {
    setOpen((prev) => [...variants.map((_, i) => Boolean(prev[i])), false]);
    dispatch({ type: 'addVariant' });
  };

  const removeVariant = (index: number) => {
    setOpen((prev) => variants.map((_, i) => Boolean(prev[i])).filter((_, i) => i !== index));
    dispatch({ type: 'removeVariant', index });
  };

  return (
    <>
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-slate-800">Variants</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Allocations must add up to 100%.{' '}
            <span
              className={allocationTotal === 100 ? 'text-green-700' : 'text-red-600'}
              data-testid="allocation-total"
            >
              Currently {allocationTotal}%
            </span>
          </p>
        </div>
        <button
          type="button"
          onClick={addVariant}
          className="text-sm text-blue-600 hover:text-blue-800 font-medium"
          data-testid="add-variant"
        >
          + Add Variant
        </button>
      </div>

      <div className="space-y-3">
        {variants.map((variant, index) => (
          <div
            key={index}
            className="flex gap-3 items-start p-3 rounded-lg border border-slate-200 bg-slate-50"
            data-testid={`variant-row-${index}`}
          >
            <div className="flex-1 space-y-2">
              <div className="flex gap-2">
                <input
                  type="text"
                  name={`variant_name_${index}`}
                  value={variant.name}
                  onChange={(e) =>
                    dispatch({ type: 'changeVariant', index, field: 'name', value: e.target.value })
                  }
                  placeholder="Variant name"
                  aria-label={`Variant ${index + 1} name`}
                  className={`flex-1 ${smallInputClass}`}
                  data-testid={`variant-name-${index}`}
                  {...marks(index, 'name')}
                />
                <div className="flex items-center gap-1">
                  <input
                    type="number"
                    name={`variant_allocation_${index}`}
                    min={0}
                    max={100}
                    value={variant.traffic_allocation}
                    onChange={(e) =>
                      dispatch({
                        type: 'changeVariant',
                        index,
                        field: 'traffic_allocation',
                        value: Number(e.target.value),
                      })
                    }
                    aria-label={`Variant ${index + 1} allocation`}
                    className={`w-16 text-center ${smallInputClass}`}
                    data-testid={`variant-allocation-${index}`}
                    {...marks(index, 'traffic_allocation')}
                  />
                  <span className="text-xs text-slate-500">%</span>
                </div>
              </div>
              {hasNoShare(variant) && (
                <p className="text-xs text-red-700" data-testid={`variant-no-share-${index}`}>
                  {ZERO_SHARE}
                </p>
              )}
              <label className="inline-flex items-center gap-1.5 text-xs text-slate-600">
                <input
                  type="radio"
                  name="control_variant"
                  checked={variant.is_control}
                  onChange={() => dispatch({ type: 'setControl', index })}
                  data-testid={`variant-control-${index}`}
                  {...marks(index, 'is_control')}
                />
                Control
              </label>
              <ConfigurationField
                index={index}
                variant={variant}
                problem={problems[index]}
                open={Boolean(open[index])}
                onToggle={() => toggle(index)}
                dispatch={dispatch}
              />
            </div>
            {variants.length > 1 && (
              <button
                type="button"
                onClick={() => removeVariant(index)}
                className="text-red-400 hover:text-red-600 text-sm mt-1"
                data-testid={`remove-variant-${index}`}
                aria-label="Remove variant"
              >
                &times;
              </button>
            )}
          </div>
        ))}
      </div>
    </>
  );
}

interface ConfigurationFieldProps {
  index: number;
  variant: VariantFormData;
  problem: string | null;
  open: boolean;
  onToggle: () => void;
  dispatch: React.Dispatch<ExperimentFormAction>;
}

/**
 * One variant's configuration: a button that shows or hides a JSON field.
 * The text is kept as typed and checked as it changes; the problem is shown
 * under the field and blocks Next and Create.
 */
function ConfigurationField({ index, variant, problem, open, onToggle, dispatch }: ConfigurationFieldProps) {
  const fieldId = `variant-configuration-${index}`;
  const regionId = `variant-configuration-region-${index}`;
  const helpId = `variant-configuration-help-${index}`;
  const errorId = `variant-configuration-error-${index}`;
  const label = variant.name.trim() || `variant ${index + 1}`;
  const set = hasText(variant);
  return (
    <div>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={regionId}
        className="text-xs font-medium text-blue-700 hover:text-blue-900 underline focus:outline-none focus:ring-2 focus:ring-blue-600 rounded"
        data-testid={`variant-configuration-toggle-${index}`}
      >
        {CONFIGURATION_TOGGLE}
        {!open && set && (
          <span className="text-slate-600 no-underline" data-testid={`variant-configuration-set-${index}`}>
            {problem ? ': needs fixing' : ': set'}
          </span>
        )}
      </button>
      {open && (
        <div id={regionId} className="mt-2 space-y-1">
          <label htmlFor={fieldId} className="block text-xs font-medium text-slate-700">
            Configuration (JSON) for {label}
          </label>
          <textarea
            id={fieldId}
            name={`variant_configuration_${index}`}
            value={variant.configuration_text ?? ''}
            onChange={(e) =>
              dispatch({ type: 'changeVariant', index, field: 'configuration_text', value: e.target.value })
            }
            rows={4}
            spellCheck={false}
            placeholder={CONFIGURATION_PLACEHOLDER}
            aria-invalid={problem ? true : undefined}
            aria-describedby={problem ? `${helpId} ${errorId}` : helpId}
            className={`w-full font-mono ${smallInputClass} ${problem ? 'border-red-600' : ''}`}
            data-testid={fieldId}
          />
          <p id={helpId} className="text-xs text-slate-600">
            {CONFIGURATION_HELP}
          </p>
          {problem && (
            <p id={errorId} className="text-xs text-red-700" data-testid={`variant-configuration-error-${index}`}>
              {problem}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
