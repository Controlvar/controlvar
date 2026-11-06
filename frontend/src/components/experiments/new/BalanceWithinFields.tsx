import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import { useSegmentList } from '@/hooks/useSegmentList';
import { unavailableLabel } from '@/components/targeting/SegmentPicker';
import {
  BALANCE_COMES_FIRST,
  BALANCE_CUSTOM_KEY_HELP,
  BALANCE_CUSTOM_KEY_LABEL,
  BALANCE_GROUP_BY_LABEL,
  BALANCE_HELPER,
  BALANCE_KEY_OPTIONS,
  BALANCE_SEGMENT_ADD_LABEL,
  BALANCE_SEGMENTS_HELP,
  BALANCE_SWITCH_LABEL,
  BALANCE_VALUES_HELP,
  BALANCE_VALUES_LABEL,
  BalanceField,
  BalanceFormState,
  BalanceGroupBy,
  BalanceIssue,
  balanceAttribute,
  closedKeyNote,
} from './balanceWithin';
import { balanceSplitProblem, ExperimentFormAction, ExperimentFormState } from './formState';
import { smallInputClass } from './fieldStyles';

const ERROR_ID = 'balance-error';

/**
 * The line to show at the split: the API's refusal when it gave one about the
 * split, else the one the page finds as the shares are typed.
 */
export function splitProblemShown(
  state: Pick<ExperimentFormState, 'balance' | 'variants'>,
  issue: BalanceIssue | null | undefined,
): string | null {
  if (issue && issue.field === 'split') return issue.message;
  return balanceSplitProblem(state);
}

/** Move focus to the control a refusal is about. */
export function focusBalanceField(field: BalanceField): void {
  const byTestId = (id: string) => document.querySelector<HTMLElement>(`[data-testid="${id}"]`);
  const target =
    field === 'split'
      ? byTestId('variant-allocation-0')
      : field === 'values'
        ? byTestId('balance-value-input')
        : field === 'segments'
          ? byTestId('balance-segment-add')
          : (byTestId('balance-custom-key') ?? byTestId('balance-group-by'));
  target?.focus();
}

interface BalanceWithinFieldsProps {
  balance: BalanceFormState;
  dispatch: React.Dispatch<ExperimentFormAction>;
  /** The API's refusal of the setting; shown at the control it is about, which takes focus. */
  issue?: BalanceIssue | null;
}

/**
 * "Balance the split within groups": the switch, what to group by, and the
 * values or segments. The caller supplies the section. The split's own refusal
 * is shown by the variants editor, under the allocations (`splitProblemShown`).
 */
export function BalanceWithinFields({ balance, dispatch, issue = null }: BalanceWithinFieldsProps) {
  const segmentsOn = balance.on && balance.groupBy === 'segments';
  const segmentList = useSegmentList(segmentsOn);
  const [draft, setDraft] = useState('');

  // A new refusal takes focus to its control; one about the split has its line at the allocations.
  useEffect(() => {
    if (issue) focusBalanceField(issue.field);
  }, [issue]);

  const attribute = balanceAttribute(balance);
  const closed = balance.on && balance.groupBy !== 'segments' ? closedKeyNote(attribute) : null;
  const shown = issue && issue.field !== 'split' ? issue : null;
  const marks = (field: BalanceField) =>
    shown && shown.field === field ? { 'aria-invalid': true as const, 'aria-describedby': ERROR_ID } : {};
  const errorAt = (field: BalanceField) =>
    shown && shown.field === field ? (
      <p id={ERROR_ID} role="alert" className="text-sm text-red-700" data-testid="balance-error">
        {shown.message}
      </p>
    ) : null;

  const addValue = () => {
    dispatch({ type: 'addBalanceValue', value: draft });
    setDraft('');
  };

  const names = new Map(segmentList.segments.map((s) => [s.id, s] as const));
  const available = segmentList.segments
    .filter((s) => s.status === 'active' && !balance.segments.includes(s.id))
    .sort((a, b) => a.name.localeCompare(b.name));

  return (
    <div className="space-y-3" data-testid="balance-within">
      <label className="inline-flex items-center gap-2 text-sm font-medium text-slate-800">
        <input
          type="checkbox"
          checked={balance.on}
          onChange={(e) => dispatch({ type: 'setBalanceOn', on: e.target.checked })}
          data-testid="balance-switch"
        />
        {BALANCE_SWITCH_LABEL}
      </label>

      {balance.on && (
        <div className="space-y-3 pl-6">
          <p className="text-sm text-slate-700">{BALANCE_HELPER}</p>

          <div>
            <label htmlFor="balance-group-by" className="block text-sm font-medium text-slate-700 mb-1">
              {BALANCE_GROUP_BY_LABEL}
            </label>
            <select
              id="balance-group-by"
              value={balance.groupBy}
              onChange={(e) => dispatch({ type: 'setBalanceGroupBy', groupBy: e.target.value as BalanceGroupBy })}
              className={smallInputClass}
              data-testid="balance-group-by"
              {...(balance.groupBy !== 'custom' && balance.groupBy !== 'segments' ? marks('attribute') : {})}
            >
              {BALANCE_KEY_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
              <option value="custom">Another attribute…</option>
              <option value="segments">Saved segments…</option>
            </select>
            {balance.groupBy !== 'custom' && balance.groupBy !== 'segments' && errorAt('attribute')}
          </div>

          {balance.groupBy === 'custom' && (
            <div className="space-y-1">
              <label htmlFor="balance-custom-key" className="block text-sm font-medium text-slate-700">
                {BALANCE_CUSTOM_KEY_LABEL}
              </label>
              <input
                id="balance-custom-key"
                type="text"
                value={balance.customKey}
                onChange={(e) => dispatch({ type: 'setBalanceCustomKey', customKey: e.target.value })}
                placeholder="tier"
                className={`${smallInputClass} font-mono`}
                data-testid="balance-custom-key"
                {...marks('attribute')}
              />
              <p className="text-xs text-slate-600">{BALANCE_CUSTOM_KEY_HELP}</p>
              {errorAt('attribute')}
            </div>
          )}

          {balance.groupBy !== 'segments' && closed && (
            <p className="text-sm text-slate-700" data-testid="balance-closed-note">
              {closed}
            </p>
          )}

          {balance.groupBy !== 'segments' && !closed && (
            <div className="space-y-1">
              <label htmlFor="balance-value-input" className="block text-sm font-medium text-slate-700">
                {BALANCE_VALUES_LABEL}
              </label>
              <div className="flex gap-2">
                <input
                  id="balance-value-input"
                  type="text"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key !== 'Enter') return;
                    // An IME confirming a composition also sends Enter.
                    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
                    e.preventDefault();
                    addValue();
                  }}
                  className={`flex-1 ${smallInputClass}`}
                  data-testid="balance-value-input"
                  {...marks('values')}
                />
                <button
                  type="button"
                  onClick={addValue}
                  className="px-3 py-1 rounded border border-slate-500 text-sm font-medium text-slate-700 hover:bg-slate-50 focus:outline-none focus:ring-2 focus:ring-blue-600"
                  data-testid="balance-value-add"
                >
                  Add value
                </button>
              </div>
              <p className="text-xs text-slate-600">{BALANCE_VALUES_HELP}</p>
              {balance.values.length > 0 && (
                <ul className="flex flex-wrap gap-2" data-testid="balance-values">
                  {balance.values.map((value, index) => (
                    <li
                      key={`${index}-${value}`}
                      className="inline-flex items-center gap-1 rounded-full bg-slate-100 pl-3 pr-1 py-0.5 text-sm text-slate-800"
                      data-testid={`balance-value-${index}`}
                    >
                      <span className="break-all">{value}</span>
                      <button
                        type="button"
                        onClick={() => dispatch({ type: 'removeBalanceValue', index })}
                        aria-label={`Remove value ${value}`}
                        className="rounded-full px-2 text-slate-700 hover:text-red-700 focus:outline-none focus:ring-2 focus:ring-blue-600"
                        data-testid={`balance-value-remove-${index}`}
                      >
                        <span aria-hidden="true">&times;</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {errorAt('values')}
            </div>
          )}

          {balance.groupBy === 'segments' && (
            <div className="space-y-2">
              <label htmlFor="balance-segment-add" className="block text-sm font-medium text-slate-700">
                {BALANCE_SEGMENT_ADD_LABEL}
              </label>
              <select
                id="balance-segment-add"
                value=""
                onChange={(e) => dispatch({ type: 'addBalanceSegment', segmentId: e.target.value })}
                className={smallInputClass}
                data-testid="balance-segment-add"
                {...marks('segments')}
              >
                <option value="">
                  {segmentList.state === 'ready'
                    ? 'Choose a segment'
                    : segmentList.state === 'error'
                      ? 'Segments not loaded'
                      : 'Loading segments...'}
                </option>
                {available.map((segment) => (
                  <option key={segment.id} value={segment.id}>
                    {`${segment.name} · ${segment.kind === 'id_list' ? 'ID list' : 'Rules'}`}
                  </option>
                ))}
              </select>
              {segmentList.state === 'error' && (
                <p className="text-xs text-red-700">
                  <span role="alert">Couldn&apos;t load segments.</span>{' '}
                  <button type="button" onClick={segmentList.retry} className="underline font-medium">
                    Retry
                  </button>
                </p>
              )}
              {segmentList.state === 'ready' && available.length === 0 && balance.segments.length === 0 && (
                <p className="text-xs text-slate-600">
                  No active segments.{' '}
                  <Link href="/segments/new" target="_blank" rel="noopener noreferrer" className="text-blue-700 underline">
                    Create one on the Segments page (opens in a new tab)
                  </Link>
                </p>
              )}
              <p className="text-xs text-slate-600">{BALANCE_SEGMENTS_HELP}</p>
              {balance.segments.length > 0 && (
                <ol className="space-y-1" data-testid="balance-segments">
                  {balance.segments.map((id, index) => {
                    const segment = names.get(id);
                    const name =
                      segmentList.state !== 'ready'
                        ? 'Saved segment'
                        : (unavailableLabel(id, segment) ?? segment?.name ?? 'Saved segment');
                    return (
                      <li
                        key={id}
                        className="flex items-center gap-2 text-sm text-slate-800"
                        data-testid={`balance-segment-${index}`}
                      >
                        <span className="flex-1 break-words">{name}</span>
                        <button
                          type="button"
                          onClick={() => dispatch({ type: 'moveBalanceSegment', index, by: -1 })}
                          disabled={index === 0}
                          aria-label={`Move ${name} up`}
                          className="px-2 rounded border border-slate-500 text-slate-700 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-blue-600"
                          data-testid={`balance-segment-up-${index}`}
                        >
                          <span aria-hidden="true">&uarr;</span>
                        </button>
                        <button
                          type="button"
                          onClick={() => dispatch({ type: 'moveBalanceSegment', index, by: 1 })}
                          disabled={index === balance.segments.length - 1}
                          aria-label={`Move ${name} down`}
                          className="px-2 rounded border border-slate-500 text-slate-700 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-blue-600"
                          data-testid={`balance-segment-down-${index}`}
                        >
                          <span aria-hidden="true">&darr;</span>
                        </button>
                        <button
                          type="button"
                          onClick={() => dispatch({ type: 'removeBalanceSegment', index })}
                          aria-label={`Remove ${name}`}
                          className="px-2 rounded border border-slate-500 text-slate-700 hover:text-red-700 focus:outline-none focus:ring-2 focus:ring-blue-600"
                          data-testid={`balance-segment-remove-${index}`}
                        >
                          <span aria-hidden="true">&times;</span>
                        </button>
                      </li>
                    );
                  })}
                </ol>
              )}
              {errorAt('segments')}
            </div>
          )}

          <p className="text-sm text-slate-700">{BALANCE_COMES_FIRST}</p>
        </div>
      )}
    </div>
  );
}

/** What a draft that cannot balance shows in place of the switch. */
export function BalanceUnavailable({ line }: { line: string }) {
  return (
    <p className="text-sm text-slate-700" data-testid="balance-unavailable">
      {line}
    </p>
  );
}
