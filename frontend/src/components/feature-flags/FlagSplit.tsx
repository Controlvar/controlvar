import React, { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import type { FlagSplitEntry } from '@/services/featureFlags';
import type { ValueFlagType } from '@/utils/flagValues';
import {
  ADD_SPLIT,
  ADD_VALUE,
  AT_LEAST_NOTE,
  AT_MOST_NOTE,
  KEEP_SPLIT,
  MAX_SPLIT_VALUES,
  MIN_SPLIT_VALUES,
  NO_SPLIT_TEXT,
  PREVIEW_CHARS,
  REMOVE_SPLIT,
  REMOVE_VALUE,
  RULE_NO_CONDITION_NOTE,
  RULE_SERVE_DEFAULT_ON,
  RULE_SERVE_DEFAULT_SPLIT,
  RULE_SERVE_HINT,
  RULE_SERVE_LABEL,
  RULE_SERVE_OWN,
  RULE_VALUE_LABEL,
  SPLIT_CAPTION,
  SPLIT_EXPERIMENT_LINK,
  SPLIT_INTRO,
  SPLIT_MEASURES_TEXT,
  SPLIT_ORDER_NOTE,
  SPLIT_PUBLIC_NOTE,
  SPLIT_REMOVED_NOTE,
  SPLIT_SUMMARY_HEADING,
  SPLIT_TWO_VALUES_NOTE,
  SPREAD_EVENLY,
  SplitErrors,
  SplitRow,
  VALUE_COLUMN,
  WEIGHT_COLUMN,
  checkSplitRows,
  impactText,
  newRow,
  newSplitRows,
  preview,
  previewNote,
  removeValueLabel,
  rowsTotal,
  ruleServeText,
  servingText,
  splitMoves,
  spreadEvenly,
  totalText,
  valueLabel,
  weightLabel,
} from '@/utils/flagSplit';

const CONTROL =
  'w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-600';
const INVALID = 'border-red-700';
const BUTTON =
  'rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-800 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600 aria-disabled:opacity-60 aria-disabled:cursor-not-allowed';

/**
 * The bar's colours, one per entry in order. Each is at least 4.5:1 against
 * white, and the segments are parted by a white gap, so every segment has
 * 3:1 against what is next to it. The bar is hidden from screen readers: the
 * table beside it holds the same numbers in text.
 */
const BAR_COLOURS = [
  'bg-blue-700',
  'bg-emerald-700',
  'bg-amber-700',
  'bg-violet-700',
  'bg-rose-700',
  'bg-cyan-800',
  'bg-slate-700',
  'bg-lime-800',
  'bg-fuchsia-700',
  'bg-orange-800',
];

/**
 * A value in a bounded preview: compact JSON, cut at `limit` characters, and
 * when cut it says so in words, so a long string or a JSON value never fills
 * a table or a dialog.
 */
export function ValuePreview({
  value,
  limit = PREVIEW_CHARS,
  testId,
}: {
  value: unknown;
  limit?: number;
  testId?: string;
}) {
  const shown = preview(value, limit);
  return (
    <span data-testid={testId}>
      <span className="break-all font-mono text-slate-800">{shown.text}</span>
      {shown.of !== null && <span className="text-slate-700"> {previewNote(limit, shown.of)}</span>}
    </span>
  );
}

/** The split as a stacked bar, one segment per entry with a weight. Hidden from screen readers. */
export function SplitBar({ split }: { split: FlagSplitEntry[] }) {
  return (
    <div aria-hidden="true" className="flex h-3 w-full overflow-hidden rounded bg-white" data-testid="split-bar">
      {split.map((entry, index) =>
        entry.weight > 0 ? (
          <div
            key={index}
            className={`${BAR_COLOURS[index % BAR_COLOURS.length]} h-full border-r-2 border-white last:border-r-0`}
            style={{ width: `${entry.weight}%` }}
            data-testid="split-bar-segment"
          />
        ) : null,
      )}
    </div>
  );
}

/** The standing note: a split measures nothing; the experiments page does. */
export function SplitMeasuresNote() {
  return (
    <p className="text-sm text-slate-700" data-testid="split-measures-note">
      {SPLIT_MEASURES_TEXT}{' '}
      <Link href="/experiments" className="text-blue-700 underline">
        {SPLIT_EXPERIMENT_LINK}
      </Link>
      .
    </p>
  );
}

/**
 * The split as a user who may not change flags reads it: the bar, the same
 * table read-only, how many values it serves, and the standing note.
 */
export function SplitReadOnly({ split }: { split: FlagSplitEntry[] | null }) {
  if (!split || split.length === 0) {
    return (
      <div className="space-y-3" data-testid="split-readonly">
        <p className="text-sm text-slate-700" data-testid="split-none">
          {NO_SPLIT_TEXT}
        </p>
        <SplitMeasuresNote />
      </div>
    );
  }
  const served = split.filter((entry) => entry.weight > 0).length;
  return (
    <div className="space-y-3" data-testid="split-readonly">
      <p className="text-sm font-medium text-slate-800" data-testid="split-serving">
        {servingText(served)}
      </p>
      <SplitBar split={split} />
      <div
        className="overflow-x-auto rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600"
        role="region"
        aria-label={SPLIT_CAPTION}
        tabIndex={0}
      >
        <table className="w-full text-sm" data-testid="split-readonly-table">
          <caption className="sr-only">{SPLIT_CAPTION}</caption>
          <thead className="border-b border-slate-200">
            <tr>
              <th scope="col" className="px-3 py-2 text-left font-medium text-slate-700">
                {VALUE_COLUMN}
              </th>
              <th scope="col" className="whitespace-nowrap px-3 py-2 text-right font-medium text-slate-700">
                {WEIGHT_COLUMN}
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {split.map((entry, index) => (
              <tr key={index} data-testid="split-readonly-row">
                <td className="px-3 py-2">
                  <ValuePreview value={entry.value} testId={`split-readonly-value-${index}`} />
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-800">{entry.weight}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <SplitMeasuresNote />
    </div>
  );
}

export interface SplitEditorProps {
  type: ValueFlagType;
  /** The rows as edited; null for no split. */
  rows: SplitRow[] | null;
  /** The split as loaded, which the impact line compares with; null for none. */
  stored: FlagSplitEntry[] | null;
  errors: SplitErrors;
  onChange: (rows: SplitRow[] | null) => void;
  /** Back to the split as loaded. */
  onKeepStored: () => void;
}

function describedBy(...ids: Array<string | false | undefined>): string | undefined {
  const list = ids.filter(Boolean) as string[];
  return list.length > 0 ? list.join(' ') : undefined;
}

/**
 * The Split section's editor: one row per value, in the order stored, with
 * the value (the same input as the value fields for the type) and its weight;
 * a live Total, Spread evenly, Add value and a Remove per row; the impact of
 * a change of weights; the notes.
 *
 * - A real table with a caption and column headers; every input labelled.
 * - Add value moves focus to the new row; Remove to the row before, or to
 *   Add value. Remove is explained, not disabled, at 3 values, and Add value
 *   at 10: a button that cannot be focused explains nothing.
 * - The Total is a polite status, said again when a weight is left, not at
 *   every key.
 * - A refusal is at its field: `aria-invalid` and `aria-describedby` on a
 *   row's value or weight; the split as a whole at the Total, which describes
 *   every weight.
 */
export function SplitEditor({ type, rows, stored, errors, onChange, onKeepStored }: SplitEditorProps) {
  const [said, setSaid] = useState(() => rowsTotal(rows ?? []));
  const focusNext = useRef<string | null>(null);
  const noSplit = rows === null;

  // A split added or brought back: the status says its total.
  useEffect(() => {
    if (!noSplit) setSaid(rowsTotal(rows ?? []));
    // Only when the split comes or goes; a weight is said when it is left.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [noSplit]);

  useEffect(() => {
    const id = focusNext.current;
    if (!id) return;
    focusNext.current = null;
    document.getElementById(id)?.focus();
  });

  if (rows === null) {
    return (
      <div className="space-y-3" data-testid="split-editor-empty">
        <p className="text-sm text-slate-700">{SPLIT_INTRO}</p>
        <p className="text-sm text-slate-700" data-testid="split-none">
          {stored ? SPLIT_REMOVED_NOTE : NO_SPLIT_TEXT}
        </p>
        <div className="flex flex-wrap gap-3">
          <button
            type="button"
            id="split-add"
            className={BUTTON}
            onClick={() => {
              const created = newSplitRows();
              focusNext.current = 'split-value-0';
              setSaid(rowsTotal(created));
              onChange(created);
            }}
            data-testid="split-add"
          >
            {ADD_SPLIT}
          </button>
          {stored && (
            <button
              type="button"
              className={BUTTON}
              onClick={() => {
                focusNext.current = 'split-value-0';
                onKeepStored();
              }}
              data-testid="split-keep-stored"
            >
              {KEEP_SPLIT}
            </button>
          )}
        </div>
        <p className="text-sm text-slate-700">{SPLIT_TWO_VALUES_NOTE}</p>
        <SplitMeasuresNote />
      </div>
    );
  }

  const live = rowsTotal(rows);
  const atMin = rows.length <= MIN_SPLIT_VALUES;
  const atMax = rows.length >= MAX_SPLIT_VALUES;
  const checked = checkSplitRows(type, rows);
  const impact = stored && !checked.errors ? impactText(splitMoves(stored, checked.split)) : null;

  const set = (index: number, field: 'text' | 'weight', value: string) =>
    onChange(rows.map((row, i) => (i === index ? { ...row, [field]: value } : row)));
  const remove = (index: number) => {
    if (atMin) return;
    const next = rows.filter((_, i) => i !== index);
    focusNext.current = index > 0 ? `split-value-${index - 1}` : 'split-add-value';
    setSaid(rowsTotal(next));
    onChange(next);
  };
  const add = () => {
    if (atMax) return;
    focusNext.current = `split-value-${rows.length}`;
    onChange([...rows, newRow()]);
  };
  const spread = () => {
    const weights = spreadEvenly(rows.length);
    setSaid(100);
    onChange(rows.map((row, i) => ({ ...row, weight: String(weights[i]) })));
  };

  const totalError = errors.total;
  return (
    <div className="space-y-3" data-testid="split-editor">
      <p className="text-sm text-slate-700">{SPLIT_INTRO}</p>
      <div
        className="overflow-x-auto rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600"
        role="region"
        aria-label={SPLIT_CAPTION}
        tabIndex={0}
      >
        <table className="w-full min-w-[20rem] text-sm" data-testid="split-table">
          <caption className="sr-only">{SPLIT_CAPTION}</caption>
          <thead className="border-b border-slate-200">
            <tr>
              <th scope="col" className="px-2 py-2 text-left font-medium text-slate-700">
                {VALUE_COLUMN}
              </th>
              <th scope="col" className="w-28 px-2 py-2 text-left font-medium text-slate-700">
                {WEIGHT_COLUMN}
              </th>
              <th scope="col" className="w-24 px-2 py-2 text-left font-medium text-slate-700">
                <span className="sr-only">{REMOVE_VALUE}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => {
              const n = index + 1;
              const at = errors.rows[index] ?? {};
              const valueId = `split-value-${index}`;
              const weightId = `split-weight-${index}`;
              const valueError = at.value ? `${valueId}-error` : undefined;
              const weightError = at.weight ? `${weightId}-error` : undefined;
              const common = {
                id: valueId,
                value: row.text,
                'aria-invalid': at.value ? (true as const) : undefined,
                'aria-describedby': describedBy(valueError),
                'data-testid': valueId,
              };
              return (
                <tr key={row.key} className="align-top" data-testid="split-row">
                  <td className="px-2 py-2">
                    <label htmlFor={valueId} className="sr-only">
                      {valueLabel(n)}
                    </label>
                    {type === 'number' ? (
                      <input
                        {...common}
                        type="text"
                        autoComplete="off"
                        spellCheck={false}
                        onChange={(e) => set(index, 'text', e.target.value)}
                        className={`${CONTROL} font-mono ${at.value ? INVALID : ''}`}
                      />
                    ) : (
                      <textarea
                        {...common}
                        rows={type === 'json' ? 3 : 2}
                        wrap={type === 'json' ? 'off' : undefined}
                        spellCheck={false}
                        autoComplete="off"
                        onChange={(e) => set(index, 'text', e.target.value)}
                        className={`${CONTROL} ${type === 'json' ? 'font-mono' : ''} max-h-40 resize-y overflow-auto ${at.value ? INVALID : ''}`}
                      />
                    )}
                    {at.value && (
                      <p id={valueError} className="mt-1 text-sm text-red-700" data-testid={valueError}>
                        {at.value}
                      </p>
                    )}
                  </td>
                  <td className="px-2 py-2">
                    <label htmlFor={weightId} className="sr-only">
                      {weightLabel(n)}
                    </label>
                    <input
                      id={weightId}
                      type="text"
                      inputMode="numeric"
                      autoComplete="off"
                      value={row.weight}
                      onChange={(e) => set(index, 'weight', e.target.value)}
                      onBlur={() => setSaid(rowsTotal(rows))}
                      aria-invalid={at.weight ? true : undefined}
                      aria-describedby={describedBy(weightError, totalError && 'split-total-error')}
                      className={`${CONTROL} tabular-nums ${at.weight ? INVALID : ''}`}
                      data-testid={weightId}
                    />
                    {at.weight && (
                      <p id={weightError} className="mt-1 text-sm text-red-700" data-testid={weightError}>
                        {at.weight}
                      </p>
                    )}
                  </td>
                  <td className="px-2 py-2">
                    <button
                      type="button"
                      id={`split-remove-${index}`}
                      onClick={() => remove(index)}
                      aria-label={removeValueLabel(n)}
                      aria-disabled={atMin ? true : undefined}
                      aria-describedby={atMin ? 'split-min-note' : undefined}
                      className={BUTTON}
                      data-testid={`split-remove-${index}`}
                    >
                      {REMOVE_VALUE}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div id="split-total-area">
        <p className="text-sm font-medium text-slate-800" aria-hidden="true" data-testid="split-total">
          {totalText(live)}
        </p>
        <p role="status" className="sr-only" data-testid="split-total-status">
          {totalText(said)}
        </p>
        {totalError && (
          <p id="split-total-error" className="mt-1 text-sm text-red-700" data-testid="split-total-error">
            {totalError}
          </p>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          id="split-add-value"
          onClick={add}
          aria-disabled={atMax ? true : undefined}
          aria-describedby={atMax ? 'split-max-note' : undefined}
          className={BUTTON}
          data-testid="split-add-value"
        >
          {ADD_VALUE}
        </button>
        <button type="button" onClick={spread} className={BUTTON} data-testid="split-spread">
          {SPREAD_EVENLY}
        </button>
        <button
          type="button"
          onClick={() => {
            focusNext.current = 'split-add';
            onChange(null);
          }}
          className={BUTTON}
          data-testid="split-remove-split"
        >
          {REMOVE_SPLIT}
        </button>
      </div>
      {atMin && (
        <p id="split-min-note" className="text-sm text-slate-700" data-testid="split-min-note">
          {AT_LEAST_NOTE}
        </p>
      )}
      {atMax && (
        <p id="split-max-note" className="text-sm text-slate-700" data-testid="split-max-note">
          {AT_MOST_NOTE}
        </p>
      )}
      {impact && (
        <p className="text-sm text-slate-800" data-testid="split-impact">
          {impact}
        </p>
      )}
      <p className="text-sm text-slate-700">{SPLIT_ORDER_NOTE}</p>
      <p className="text-sm text-slate-700">{SPLIT_TWO_VALUES_NOTE}</p>
      <p className="text-sm text-slate-700" data-testid="split-public-note">
        {SPLIT_PUBLIC_NOTE}
      </p>
      <SplitMeasuresNote />
    </div>
  );
}

/** The Serve control's state: the default, or a value of the rule's own as typed. */
export interface ServeState {
  own: boolean;
  text: string;
}

export interface RuleServeControlProps {
  type: ValueFlagType;
  /** What the default serves: a value of the split, or the on value. */
  hasSplit: boolean;
  serve: ServeState;
  /** False while the rules have no group: there is nothing to give a value to. */
  hasCondition: boolean;
  error: string | null;
  readOnly: boolean;
  /** The stored value, shown when read-only. */
  storedValue: unknown;
  onChange: (serve: ServeState) => void;
}

/**
 * What the one dashboard rule serves: its default (a value of the split, or
 * the on value without one) or a value of its own, typed like the value
 * fields. A native `<select>` with a visible label; a stored value of the
 * flag's type is shown and kept, whatever the choices.
 */
export function RuleServeControl({
  type,
  hasSplit,
  serve,
  hasCondition,
  error,
  readOnly,
  storedValue,
  onChange,
}: RuleServeControlProps) {
  if (readOnly) {
    if (!hasCondition) return null;
    return (
      <p className="mt-4 text-sm text-slate-700" data-testid="rule-serve-readonly">
        {ruleServeText(storedValue !== undefined, hasSplit, storedValue)}
      </p>
    );
  }
  if (!hasCondition) {
    return (
      <p className="mt-4 text-sm text-slate-700" data-testid="rule-serve-no-condition">
        {RULE_NO_CONDITION_NOTE}
      </p>
    );
  }
  const errorId = error ? 'rule-serve-value-error' : undefined;
  const common = {
    id: 'rule-serve-value',
    value: serve.text,
    'aria-invalid': error ? (true as const) : undefined,
    'aria-describedby': describedBy(errorId, 'rule-serve-hint'),
    'data-testid': 'rule-serve-value',
    onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
      onChange({ own: true, text: e.target.value }),
  };
  return (
    <div className="mt-4 space-y-2" data-testid="rule-serve-control">
      <div>
        <label htmlFor="rule-serve" className="block text-sm font-medium text-slate-700 mb-1">
          {RULE_SERVE_LABEL}
        </label>
        <select
          id="rule-serve"
          value={serve.own ? 'own' : 'default'}
          onChange={(e) => onChange({ own: e.target.value === 'own', text: serve.text })}
          aria-describedby="rule-serve-hint"
          className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-600"
          data-testid="rule-serve"
        >
          <option value="default">{hasSplit ? RULE_SERVE_DEFAULT_SPLIT : RULE_SERVE_DEFAULT_ON}</option>
          <option value="own">{RULE_SERVE_OWN}</option>
        </select>
      </div>
      {serve.own && (
        <div>
          <label htmlFor="rule-serve-value" className="block text-sm font-medium text-slate-700 mb-1">
            {RULE_VALUE_LABEL}
          </label>
          {type === 'number' ? (
            <input
              {...common}
              type="text"
              autoComplete="off"
              spellCheck={false}
              className={`${CONTROL} font-mono ${error ? INVALID : ''}`}
            />
          ) : (
            <textarea
              {...common}
              rows={type === 'json' ? 3 : 2}
              wrap={type === 'json' ? 'off' : undefined}
              spellCheck={false}
              autoComplete="off"
              className={`${CONTROL} ${type === 'json' ? 'font-mono' : ''} max-h-40 resize-y overflow-auto ${error ? INVALID : ''}`}
            />
          )}
          {error && (
            <p id={errorId} className="mt-1 text-sm text-red-700" data-testid="rule-serve-value-error">
              {error}
            </p>
          )}
        </div>
      )}
      <p id="rule-serve-hint" className="text-xs text-slate-600">
        {RULE_SERVE_HINT}
      </p>
    </div>
  );
}

/** One line of the summary: the refusal, linked to the field it is at. */
export interface SummaryItem {
  /** The id of the field, which takes focus. */
  target: string;
  text: string;
}

/**
 * Above Save when the split or the rule's value stopped it: each refusal,
 * linked to its field. The first field takes focus (the page moves it), so a
 * screen reader hears the refusal with the field; the summary is not a live
 * region, so nothing is said twice.
 */
export function SplitErrorSummary({ items }: { items: SummaryItem[] }) {
  if (items.length === 0) return null;
  return (
    <div
      className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
      aria-labelledby="split-error-summary-heading"
      role="group"
      data-testid="split-error-summary"
    >
      <p id="split-error-summary-heading" className="font-medium">
        {SPLIT_SUMMARY_HEADING}
      </p>
      <ul className="mt-1 list-disc pl-5">
        {items.map((item) => (
          <li key={`${item.target}-${item.text}`}>
            <a
              href={`#${item.target}`}
              onClick={(e) => {
                e.preventDefault();
                document.getElementById(item.target)?.focus();
              }}
              className="text-red-800 underline"
            >
              {item.text}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}
