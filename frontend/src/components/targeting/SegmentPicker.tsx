import React, { createContext, useContext } from 'react';
import Link from 'next/link';
import type { SegmentList } from '@/hooks/useSegmentList';
import type { Segment } from '@/services/segments';
import { segmentRulesAreValid } from '@/utils/segmentRules';

/** Most distinct segments one ruleset may name (the API refuses an eleventh with 422). */
export const MAX_SEGMENTS_PER_RULESET = 10;

/** What the rule builder gives its segment rows: the segments, and the ids the ruleset names. */
export interface SegmentPickerValue {
  list: SegmentList;
  used: string[];
}

export const SegmentPickerContext = createContext<SegmentPickerValue | null>(null);

const IDLE: SegmentList = { state: 'idle', segments: [], retry: () => undefined };

/** Shown under a stored segment that is not active (#440 fail closed). */
export const UNAVAILABLE_SEGMENT_WARNING =
  'No one can be matched against an inactive, archived or unknown segment. A flag that uses it ' +
  'answers every user “off” (reason “error”), and an experiment that uses it enrols no new users, ' +
  'with “is in segment” and “is not in segment” alike. Rules that name it cannot be saved; ' +
  'choose an active segment.';

export const SEGMENT_LIMIT_HINT = `A ruleset can use at most ${MAX_SEGMENTS_PER_RULESET} segments.`;

function kindLabel(segment: Segment): string {
  return segment.kind === 'id_list' ? 'ID list' : 'Rules';
}

/** The label of a stored value whose segment is not active, or null. */
export function unavailableLabel(value: string, segment: Segment | undefined): string | null {
  if (!value) return null;
  if (!segment) return 'Unknown segment (deleted or from another installation)';
  if (segment.status === 'archived') return `Archived segment: ${segment.name}`;
  if (segment.status === 'inactive') return `Inactive segment: ${segment.name}`;
  return null;
}

interface SegmentPickerProps {
  /** Where the condition is, for the accessible name ("Group 1, condition 2 segment"). */
  label: string;
  value: string;
  onChange: (segmentId: string) => void;
  readOnly?: boolean;
}

/**
 * The value control of an `in_segment` / `not_in_segment` condition: a select
 * of the active segments by name. A stored segment that is no longer active
 * stays selected, named, with the warning beside it.
 */
export function SegmentPicker({ label, value, onChange, readOnly = false }: SegmentPickerProps) {
  const ctx = useContext(SegmentPickerContext);
  const list = ctx?.list ?? IDLE;
  const used = new Set(ctx?.used ?? []);
  const ready = list.state === 'ready';
  const byId = new Map(list.segments.map((s) => [s.id, s] as const));
  const active = list.segments
    .filter((s) => s.status === 'active')
    .sort((a, b) => a.name.localeCompare(b.name));
  const unavailable = ready ? unavailableLabel(value, byId.get(value)) : null;
  const atLimit = used.size >= MAX_SEGMENTS_PER_RULESET;
  const selectId = `segment-picker-${label.replace(/[^A-Za-z0-9]+/g, '-')}`;

  return (
    <div className="flex-1 min-w-0 space-y-1" data-testid="segment-picker">
      <select
        id={selectId}
        data-testid="condition-segment"
        aria-label={`${label} segment`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={readOnly || !ready}
        className="w-full rounded border border-slate-300 px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-blue-600 disabled:bg-slate-100 disabled:text-slate-600"
      >
        {!ready && <option value={value}>{list.state === 'error' ? 'Segments not loaded' : 'Loading segments...'}</option>}
        {ready && (
          <>
            <option value="">Choose a segment</option>
            {unavailable && <option value={value}>{unavailable}</option>}
            {active.map((segment) => {
              const invalid = segment.kind === 'rules' && !segmentRulesAreValid(segment.rules);
              const overLimit = atLimit && !used.has(segment.id) && segment.id !== value;
              const reason = invalid ? ' (rules not valid)' : overLimit ? ' (limit of 10 segments reached)' : '';
              return (
                <option key={segment.id} value={segment.id} disabled={invalid || overLimit}>
                  {`${segment.name} · ${kindLabel(segment)}${reason}`}
                </option>
              );
            })}
          </>
        )}
      </select>

      {list.state === 'error' && (
        <div className="flex items-center gap-2 text-xs text-red-700" data-testid="segment-picker-error">
          <span role="alert">Couldn&apos;t load segments.</span>
          <button
            type="button"
            onClick={list.retry}
            className="underline font-medium"
            data-testid="segment-picker-retry"
          >
            Retry
          </button>
        </div>
      )}

      {ready && active.length === 0 && !unavailable && !readOnly && (
        <p className="text-xs text-slate-600" data-testid="segment-picker-empty">
          No active segments.{' '}
          <Link href="/segments/new" target="_blank" rel="noopener noreferrer" className="text-blue-700 underline">
            Create one on the Segments page (opens in a new tab)
          </Link>
        </p>
      )}

      {ready && atLimit && !readOnly && (
        <p className="text-xs text-slate-600" data-testid="segment-picker-limit">
          {SEGMENT_LIMIT_HINT}
        </p>
      )}

      {unavailable && (
        <p className="text-xs text-amber-900 bg-amber-50 border border-amber-200 rounded p-2" data-testid="segment-picker-unavailable">
          <span aria-hidden="true">⚠ </span>
          {UNAVAILABLE_SEGMENT_WARNING}
        </p>
      )}
    </div>
  );
}
