import React from 'react';

export interface FlagToggleProps {
  on: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  /**
   * A request is in flight. The switch stays focusable (`aria-disabled`, not
   * `disabled`, so focus does not drop to the page) and presses are ignored.
   */
  pending?: boolean;
  /** Accessible name, e.g. "Turn checkout_v2 on". */
  label: string;
  'data-testid'?: string;
}

/**
 * Accessible on/off switch (`role="switch"`, `aria-checked`) used by the flag
 * list rows and the flag detail header.
 */
export function FlagToggle({ on, onChange, disabled = false, pending = false, label, ...rest }: FlagToggleProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      aria-disabled={pending ? true : undefined}
      onClick={() => {
        if (pending) return;
        onChange(!on);
      }}
      data-state={on ? 'on' : 'off'}
      data-testid={rest['data-testid'] ?? 'flag-toggle'}
      className={[
        'relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2',
        'disabled:opacity-50 disabled:cursor-not-allowed aria-disabled:opacity-50 aria-disabled:cursor-not-allowed',
        on ? 'bg-green-600' : 'bg-slate-300',
      ].join(' ')}
    >
      <span
        aria-hidden="true"
        className={[
          'inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform',
          on ? 'translate-x-5' : 'translate-x-0.5',
        ].join(' ')}
      />
    </button>
  );
}

export default FlagToggle;
