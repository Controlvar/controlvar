import React from 'react';
import { Segment } from '@/services/segments';
import { ApiError } from '@/services/api';
import { docsUrl } from '@/services/docs';
import { MetricResult, SegmentResultsResponse, SrmResult } from '@/types/results';
import { SegmentListState } from '@/hooks/useSegmentList';
import { unavailableLabel } from '@/components/targeting/SegmentPicker';
import { SrmWording, WordedSrmNotice } from './SrmNotice';

// The results page's "Show results for" control (#1933): one experiment's
// results for the members of one saved ID-list segment, from
// GET /api/v1/results/{id}/segments/{segment_id}. Every string is fixed: the
// server's text is never shown.

export const PICKER_ID = 'results-segment-picker';
export const PICKER_LABEL = 'Show results for';
export const ALL_USERS = 'All users';
export const PICKER_HINT =
  'A saved segment narrows the summary and the Overview tab to its members, as an exploratory ' +
  'result. Only ID-list segments are listed. The other tabs and the CSV download always show all users.';
export const SEGMENTS_LOADING = 'Loading segments...';
export const SEGMENTS_NOT_LOADED = 'Segments not loaded';
export const SEGMENTS_LOAD_FAILED = "Couldn't load segments.";
export const NO_ID_LIST_SEGMENTS = 'No ID-list segments to filter by.';
export const CREATE_SEGMENT_LINK = 'Create one on the Segments page (opens in a new tab)';
export const CSV_NOTE = 'The file covers all users: the saved segment is not applied.';
export const BAYESIAN_SEGMENT_NOTE =
  'The Bayesian analysis is not computed for a saved segment. Choose All users above to see it.';
export const SEGMENT_RESULTS_FORBIDDEN = 'You do not have access to these results.';
export const DOCS_LINK = 'How results for a saved segment are computed (opens in a new tab)';

export function tabNote(name: string): string {
  return `This tab shows all users. The saved segment ${name} applies to the summary and the Overview tab only.`;
}

export function segmentResultsUnavailable(name: string): string {
  return (
    `Results for ${name} are not available from this API. It may be older than this dashboard, ` +
    "or the experiment or the segment may no longer exist. Choose All users to see the experiment's results."
  );
}

/**
 * Fixed copy for a failed segment request. Never the results page's own
 * mapping: a 404 here can also be an API older than the route, so it never
 * reads "No results were found for this experiment".
 */
export function segmentResultsErrorMessage(e: unknown, name: string): string {
  if (e instanceof ApiError) {
    if (e.status === 0) return e.message;
    if (e.status === 404) return segmentResultsUnavailable(name);
    if (e.status === 422) {
      return `${name} is not an ID-list segment, so it cannot filter these results. Choose another segment, or All users.`;
    }
    if (e.status === 403) return SEGMENT_RESULTS_FORBIDDEN;
    if (e.status >= 500) {
      return `The results for ${name} could not be computed. Try again; if it keeps failing, check the API logs.`;
    }
  }
  return `The results for ${name} could not be loaded.`;
}

/** The status line, always in the DOM (polite), visually hidden. */
export function segmentStatusText(
  name: string | null,
  state: 'loading' | 'ready' | 'other',
  users?: number,
): string {
  if (name === null) return 'Showing results for all users.';
  if (state === 'loading') return `Loading results for ${name}.`;
  if (state === 'ready' && typeof users === 'number') {
    return `Showing results for ${name}: ${users} ${users === 1 ? 'user' : 'users'}.`;
  }
  return '';
}

/** ID-list segments of every status: active by name first, then the others by name. */
export function pickerSegments(segments: Segment[]): Segment[] {
  const lists = segments.filter((s) => s.kind === 'id_list');
  const byName = (a: Segment, b: Segment) => a.name.localeCompare(b.name);
  return [
    ...lists.filter((s) => s.status === 'active').sort(byName),
    ...lists.filter((s) => s.status !== 'active').sort(byName),
  ];
}

interface PickerProps {
  value: string;
  onChange: (segmentId: string) => void;
  state: SegmentListState;
  segments: Segment[];
  onRetry: () => void;
  canCreate: boolean;
  /** Called when the picker is about to be used: the list is loaded then. */
  onEngage?: () => void;
}

export function ResultsSegmentPicker({
  value,
  onChange,
  state,
  segments,
  onRetry,
  canCreate,
  onEngage,
}: PickerProps) {
  const options = pickerSegments(segments);
  const ready = state === 'ready';
  return (
    <div
      className="space-y-1"
      data-testid="results-segment-picker"
      onPointerEnter={onEngage}
      onTouchStart={onEngage}
    >
      <label htmlFor={PICKER_ID} className="block text-sm font-medium text-slate-800">
        {PICKER_LABEL}
      </label>
      <select
        id={PICKER_ID}
        aria-describedby={`${PICKER_ID}-hint`}
        value={value}
        onFocus={onEngage}
        onMouseDown={onEngage}
        onKeyDown={onEngage}
        onChange={(e) => onChange(e.target.value)}
        className="w-full max-w-md rounded border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-800 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
      >
        <option value="">{ALL_USERS}</option>
        {state === 'loading' && (
          <option value="" disabled>
            {SEGMENTS_LOADING}
          </option>
        )}
        {state === 'error' && (
          <option value="" disabled>
            {SEGMENTS_NOT_LOADED}
          </option>
        )}
        {ready &&
          options.map((s) => (
            <option key={s.id} value={s.id}>
              {unavailableLabel(s.id, s) ?? s.name}
            </option>
          ))}
      </select>
      <p id={`${PICKER_ID}-hint`} className="text-xs text-slate-600">
        {PICKER_HINT}
      </p>
      {state === 'error' && (
        <div className="flex items-center gap-2 text-sm" data-testid="segment-list-error">
          <p role="alert" className="text-red-700">
            {SEGMENTS_LOAD_FAILED}
          </p>
          <button
            type="button"
            onClick={onRetry}
            className="rounded border border-slate-300 bg-white px-2 py-1 text-sm text-slate-800 hover:bg-slate-50"
          >
            Retry
          </button>
        </div>
      )}
      {ready && options.length === 0 && (
        <p className="text-sm text-slate-600" data-testid="segment-list-empty">
          {NO_ID_LIST_SEGMENTS}{' '}
          {canCreate && (
            <a
              href="/segments/new"
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-700 underline"
            >
              {CREATE_SEGMENT_LINK}
            </a>
          )}
        </p>
      )}
    </div>
  );
}

/** The exploratory banner, before the summary card (its h2 comes first). */
export function SegmentBanner({ name, users, archived }: { name: string; users: number; archived: boolean }) {
  return (
    <section
      aria-labelledby="segment-banner-heading"
      data-testid="segment-banner"
      className="rounded-lg border border-amber-300 bg-amber-50 p-4 space-y-2 text-amber-900"
    >
      <h2 id="segment-banner-heading" className="text-base font-semibold">
        Exploratory: {name} only
      </h2>
      <p className="text-sm">
        {`These results count only the ${users} users in this experiment whose user ID is in the saved segment ${name}. ` +
          'Use them to find questions, not to decide: the correction covers the comparisons inside the segment, ' +
          'not your choice of segment, so each segment you look at is another chance of a false positive. ' +
          'To act on a finding, confirm it with an experiment that targets this segment.'}
      </p>
      <p className="text-sm">
        {'Membership is the list as it is now, not when each user was assigned: users added to the list since ' +
          'are counted, and users removed are not. If the list was built from something this experiment could ' +
          'change, such as purchases made after it started, the comparison is biased.'}
      </p>
      {archived && (
        <p className="text-sm" data-testid="segment-banner-archived">
          {`${name} is archived, so its IDs cannot change unless it is made active again.`}
        </p>
      )}
      <a
        href={docsUrl('api/dimensional-analysis', 'results-for-one-saved-segment')}
        target="_blank"
        rel="noopener noreferrer"
        className="text-sm underline"
      >
        {DOCS_LINK}
      </a>
    </section>
  );
}

/** The all-users check's words under a saved segment. */
export const ALL_USERS_SRM: SrmWording = {
  passed: (p) => (
    <>Sample ratio check for all users passed: the traffic split matches the allocation ({p}).</>
  ),
  heading: 'For all users, the traffic split does not match the allocation.',
  body:
    'When this happens, assignment or tracking is usually broken, so these results should not ' +
    'be trusted, including the recommendation. Find the cause before you decide.',
  idPrefix: 'srm',
};

/** The segment's own check's words. */
export function insideSrm(name: string, users: number): SrmWording {
  return {
    passed: (p) => (
      <>
        Sample ratio check inside {name} passed for its {users} users: their split matches the
        allocation ({p}).
      </>
    ),
    heading: `Inside ${name}, the traffic split does not match the allocation.`,
    body:
      "These users were assigned by the experiment's allocation like everyone else, so they should split " +
      'as allocated. When they do not, either the list depends on something the experiment changed, or ' +
      "assignment or tracking is broken for these users. Do not use this segment's results until you know which.",
    missing: `No sample ratio check could be run inside ${name}, so nothing here says its users split as allocated.`,
    idPrefix: 'segment-srm',
  };
}

/** Both checks under a saved segment: all users first, then the segment's users. */
export function SegmentSrmNotices({
  allUsers,
  allUsersMetrics,
  segment,
}: {
  allUsers: SrmResult | null | undefined;
  allUsersMetrics: MetricResult[];
  segment: SegmentResultsResponse;
}) {
  return (
    <div className="space-y-2">
      <WordedSrmNotice srm={allUsers} metrics={allUsersMetrics} wording={ALL_USERS_SRM} />
      <WordedSrmNotice
        srm={segment.srm}
        metrics={segment.metrics}
        wording={insideSrm(segment.segment.name, segment.summary.total_users)}
      />
    </div>
  );
}

/**
 * The zero states, from `member_count`: never a table of zeros. Null when
 * the segment has assigned members.
 */
export function zeroStateText(segment: SegmentResultsResponse): string | null {
  if (segment.summary.total_users > 0) return null;
  const { name, member_count } = segment.segment;
  if (member_count === 0) return `${name} has no IDs yet, so there is nothing to compare.`;
  return (
    `None of the ${member_count} IDs in ${name} has been assigned to this experiment, so there is ` +
    'nothing to compare. An ID counts only when it is exactly the user_id your app sends when it ' +
    'assigns users: IDs are not trimmed or case-folded.'
  );
}

/**
 * Under a saved segment, a variant none of its members is in: the engine gives
 * it a relative change of -100%, which is not a finding, so it is not shown.
 */
export function withoutEmptyArmChanges(metrics: MetricResult[]): MetricResult[] {
  return metrics.map((m) => ({
    ...m,
    variants: m.variants.map((v) =>
      v.sample_size === 0
        ? {
            ...v,
            relative_improvement_pct: null,
            relative_improvement_pct_ci: null,
            incremental_impact: null,
            incremental_impact_ci: null,
          }
        : v,
    ),
  }));
}
