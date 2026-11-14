/**
 * BreakdownSelector — Issue #28: Dimensional Analysis & Segment Breakdown
 *
 * Dropdown that lets users choose a dimension for segment breakdown.
 * Maps human-readable labels to backend API dimension keys.
 */

import React from 'react';

export interface BreakdownOption {
  label: string;
  value: string;
}

export const BREAKDOWN_OPTIONS: BreakdownOption[] = [
  { label: 'Platform', value: 'platform' },
  { label: 'Country', value: 'country' },
  { label: 'User Tier', value: 'user_tier' },
];

/**
 * The value of the balanced groups' option (#448). A breakdown key is made of
 * letters, digits, `_` and `-`, so no key can be this.
 */
export const BALANCED_BREAKDOWN = '(balanced)';

export interface BreakdownSelectorProps {
  /** Currently selected dimension, or null if none selected. */
  value: string | null;
  /** Called when the user selects a dimension. */
  onChange: (dim: string | null) => void;
  /**
   * For an experiment that balances its split within groups, the label of its
   * balanced groups ("Platform (balanced)"), listed first with the value
   * BALANCED_BREAKDOWN. Absent for any other experiment, whose list is as it was.
   */
  balancedLabel?: string;
}

export function BreakdownSelector({ value, onChange, balancedLabel }: BreakdownSelectorProps) {
  function handleChange(event: React.ChangeEvent<HTMLSelectElement>) {
    const selected = event.target.value;
    onChange(selected === '' ? null : selected);
  }

  return (
    <div className="flex items-center gap-2">
      <label
        htmlFor="breakdown-selector"
        className="text-sm font-medium text-slate-700"
      >
        Break down by:
      </label>
      <select
        id="breakdown-selector"
        data-testid="breakdown-selector"
        value={value ?? ''}
        onChange={handleChange}
        className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-900 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
      >
        {balancedLabel && <option value={BALANCED_BREAKDOWN}>{balancedLabel}</option>}
        <option value="">— None —</option>
        {BREAKDOWN_OPTIONS.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
    </div>
  );
}
