import React, { forwardRef } from 'react';
import { CODE_HINT, RECOVERY_CODE_HINT } from '@/services/twoFactor';

export interface TwoStepCodeInputProps {
  id: string;
  /** The visible label ("Code", "Recovery code", ...). */
  label: string;
  /** A recovery code rather than the app's 6 digits. */
  recovery?: boolean;
  value: string;
  onChange: (value: string) => void;
  /** The id of the error message this field is described by, while it shows. */
  errorId?: string | null;
  readOnly?: boolean;
  autoFocus?: boolean;
  testId?: string;
}

const inputClass =
  'mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm font-mono tracking-wider text-slate-900 shadow-sm focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 read-only:bg-slate-50';

/**
 * The one field of a code step: the sign-in page's second step and every
 * action of the account page. A text field, never `type="number"` (a code can
 * start with zeros: 005924 is a real one), with the keyboard and autofill
 * hints a phone and a password manager read, and no `maxLength` of 6: a pasted
 * "123 456" or a dashed recovery code is a code too, and the API reads it.
 * Above 64 characters the API answers 422, so typing stops there.
 */
export const TwoStepCodeInput = forwardRef<HTMLInputElement, TwoStepCodeInputProps>(function TwoStepCodeInput(
  { id, label, recovery = false, value, onChange, errorId, readOnly, autoFocus, testId },
  ref,
) {
  const hintId = `${id}-hint`;
  const describedBy = errorId ? `${hintId} ${errorId}` : hintId;
  return (
    <div>
      <label htmlFor={id} className="block text-sm font-medium text-slate-700">
        {label}
      </label>
      <input
        ref={ref}
        id={id}
        name={recovery ? 'recovery_code' : 'code'}
        type="text"
        inputMode={recovery ? 'text' : 'numeric'}
        autoComplete={recovery ? 'off' : 'one-time-code'}
        autoCapitalize={recovery ? 'characters' : 'off'}
        autoCorrect="off"
        spellCheck={false}
        maxLength={64}
        autoFocus={autoFocus}
        readOnly={readOnly}
        aria-invalid={errorId ? true : undefined}
        aria-describedby={describedBy}
        data-testid={testId}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className={inputClass}
      />
      <p id={hintId} className="mt-1 text-xs text-slate-600">
        {recovery ? RECOVERY_CODE_HINT : CODE_HINT}
      </p>
    </div>
  );
});

export default TwoStepCodeInput;
