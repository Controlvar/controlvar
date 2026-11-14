/**
 * BalancedGroupsView: Results › Breakdowns for an experiment that balances its
 * split within groups (beta, #448), from `GET /results/{id}/balanced-groups`.
 *
 * - Notes (`role="note"`): how a user is counted, always; most users in
 *   `unknown`, from 100 users; how many were split without balancing, when any.
 * - One row per group as stored, then `other` and `unknown`, as a breakdown's
 *   rows: per variant they add up to the experiment's users.
 * - The row over all the groups, each group weighted by its size, with its
 *   interval and no winner or verdict: the experiment's results keep their
 *   decision.
 */
import React from 'react';
import { BalanceWithin } from '@/types/experiments';
import { BalancedGroupsEstimate, BalancedGroupsResponse, SegmentVariantResult } from '@/types/results';
import { docsUrl } from '@/services/docs';
import { useSegmentList } from '@/hooks/useSegmentList';
import {
  NOT_ENOUGH_DATA,
  correctionLabel,
  finitePair,
  formatSignedAmount,
  isFiniteNumber,
} from '@/components/results/shared/resultFormat';
import { correctionName, formatConfidence } from '@/components/results/shared/analysisSettings';
import { LeadWithInterval } from '@/components/results/shared/IntervalText';
import {
  ExploratoryNote,
  HteWarning,
  NO_ASSIGNED_USERS,
  SegmentRows,
  SegmentTableHead,
} from '@/components/results/Breakdowns/SegmentComparisonTable';
import {
  DECISION_UNCHANGED,
  OUTSIDE_NOTE_LINK,
  OVERALL_CAPTION,
  UNKNOWN_NOTE_LINK,
  countingNote,
  groupLabel,
  groupsCaption,
  mergedText,
  noEstimateText,
  outsideNote,
  unknownNote,
  usersIn,
} from '@/components/results/Breakdowns/balancedGroups';

export interface BalancedGroupsViewProps {
  /** The experiment's `balance_within`. */
  setting: BalanceWithin;
  data: BalancedGroupsResponse;
}

const NOTE = 'text-sm text-slate-700';
const LINK = 'text-blue-700 underline hover:text-blue-900';
const HEAD = 'px-4 py-3 text-xs font-medium uppercase tracking-wide text-slate-500';
const CAPTION = 'px-4 py-2 text-left text-sm font-medium text-slate-800';

export function BalancedGroupsView({ setting, data }: BalancedGroupsViewProps) {
  const segmentIds = Array.isArray(setting.segments) ? setting.segments : null;
  const segmentList = useSegmentList(Boolean(segmentIds && segmentIds.length > 0));
  const names =
    segmentIds && segmentList.state === 'ready'
      ? new Map(segmentList.segments.map((s) => [s.id, s.name] as const))
      : null;
  const label = (value: string) => groupLabel(value, setting, names);

  const { groups } = data;
  const users = usersIn(groups);
  const unknown = unknownNote(setting, data.unknown_share, groups, data.users_outside_blocks);
  const outside = outsideNote(data.users_outside_blocks, users);
  const caption = groupsCaption(setting);

  return (
    <div className="space-y-4" data-testid="balanced-groups">
      <p className={NOTE} role="note" data-testid="balanced-counting-note">
        {countingNote(setting)}
      </p>
      {unknown && (
        <p className={NOTE} role="note" data-testid="balanced-unknown-note">
          {unknown}{' '}
          <a href={docsUrl('guides/balance-within-groups', 'assign-users')} className={LINK}>
            {UNKNOWN_NOTE_LINK}
          </a>
        </p>
      )}
      {outside && (
        <p className={NOTE} role="note" data-testid="balanced-outside-note">
          {outside}{' '}
          <a href={docsUrl('guides/balance-within-groups', 'users-assigned-without-balancing')} className={LINK}>
            {OUTSIDE_NOTE_LINK}
          </a>
        </p>
      )}

      {groups.is_exploratory && <ExploratoryNote adjustedAlpha={groups.adjusted_alpha} />}
      {groups.has_heterogeneous_effects && groups.hte_warning && <HteWarning warning={groups.hte_warning} />}

      {groups.segments.length === 0 ? (
        <p className="text-sm text-slate-500">{NO_ASSIGNED_USERS}</p>
      ) : (
        // A phone scrolls the table inside this region, never the page.
        <div
          className="overflow-x-auto rounded-lg border border-slate-200"
          role="region"
          aria-label={caption}
          tabIndex={0}
        >
          <table className="min-w-full divide-y divide-slate-200">
            <caption className={CAPTION}>{caption}</caption>
            <SegmentTableHead first="Group" />
            <tbody className="divide-y divide-slate-100 bg-white">
              <SegmentRows segments={groups.segments} rowHeaders label={label} />
            </tbody>
          </table>
        </div>
      )}

      <Overall data={data} label={label} />
    </div>
  );
}

/** The variants as the rows list them, the control first, with each one's users over every row. */
function variantsWithUsers(data: BalancedGroupsResponse): { variant: SegmentVariantResult; users: number }[] {
  const first = data.groups.segments[0]?.variants ?? [];
  return first.map((variant) => ({
    variant,
    users: data.groups.segments.reduce(
      (sum, segment) =>
        sum + (segment.variants.find((v) => v.variant_id === variant.variant_id)?.sample_size ?? 0),
      0,
    ),
  }));
}

/**
 * The p-value as the API judged it: adjusted across the treatments when a
 * correction applies to two or more of them (with one, the correction changes
 * nothing, so it is shown once). Never a badge.
 */
function shownP(
  estimate: BalancedGroupsEstimate,
  adjusted: boolean,
  correction: BalancedGroupsResponse['correction_method'],
): React.ReactNode {
  const raw = estimate.p_value;
  if (!isFiniteNumber(raw)) return NOT_ENOUGH_DATA;
  if (adjusted && isFiniteNumber(estimate.adjusted_p_value)) {
    return (
      <span className="flex flex-col">
        <span>{`${estimate.adjusted_p_value.toFixed(4)}, ${correctionName(correction)} adjusted`}</span>
        <span className="text-xs text-slate-600">{`unadjusted ${raw.toFixed(4)}`}</span>
      </span>
    );
  }
  return raw.toFixed(4);
}

function Overall({ data, label }: { data: BalancedGroupsResponse; label: (value: string) => string }) {
  const merged = mergedText(data.merged_groups ?? [], label);
  if (!data.overall) {
    return (
      <div className="space-y-2">
        <p className={NOTE} data-testid="balanced-overall-none">
          {noEstimateText(data.reason)}
        </p>
        <p className="text-xs text-slate-500">{DECISION_UNCHANGED}</p>
      </div>
    );
  }

  const variants = variantsWithUsers(data);
  const control = variants.find(({ variant }) => variant.is_control);
  const usersOf = (id: string) => variants.find(({ variant }) => variant.variant_id === id)?.users ?? 0;
  const compared = data.overall.filter((e) => isFiniteNumber(e.p_value)).length;
  const adjusted = correctionLabel(data.correction_method) !== null && compared >= 2;
  const level = formatConfidence(data.confidence_level);

  return (
    <div className="space-y-2" data-testid="balanced-overall">
      <div
        className="overflow-x-auto rounded-lg border border-slate-200"
        role="region"
        aria-label={OVERALL_CAPTION}
        tabIndex={0}
      >
        <table className="min-w-full divide-y divide-slate-200">
          <caption className={CAPTION}>{OVERALL_CAPTION}</caption>
          <thead className="bg-slate-50">
            <tr>
              <th scope="col" className={`${HEAD} text-left`}>
                Variant
              </th>
              <th scope="col" className={`${HEAD} text-right`}>
                Users
              </th>
              <th scope="col" className={`${HEAD} text-left`}>
                {`Difference from ${control ? control.variant.variant_name : 'the control'}`}
              </th>
              <th scope="col" className={`${HEAD} text-right`}>
                p-value
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 bg-white">
            {control && (
              <tr>
                <th scope="row" className="px-4 py-3 text-left text-sm font-medium text-slate-900">
                  {control.variant.variant_name}
                  <span className="ml-1 text-xs text-slate-400">(ctrl)</span>
                </th>
                <td className="px-4 py-3 text-right text-sm tabular-nums text-slate-600">
                  {control.users.toLocaleString()}
                </td>
                <td className="px-4 py-3 text-sm text-slate-600">—</td>
                <td className="px-4 py-3 text-right text-sm text-slate-600">—</td>
              </tr>
            )}
            {data.overall.map((estimate) => {
              const diff = estimate.absolute_difference;
              const ci = finitePair(estimate.absolute_difference_ci);
              return (
                <tr key={estimate.variant_id}>
                  <th scope="row" className="px-4 py-3 text-left text-sm font-medium text-slate-900">
                    {estimate.variant_name}
                  </th>
                  <td className="px-4 py-3 text-right text-sm tabular-nums text-slate-600">
                    {usersOf(estimate.variant_id).toLocaleString()}
                  </td>
                  <td className="px-4 py-3 text-sm tabular-nums text-slate-900">
                    {isFiniteNumber(diff) ? (
                      <LeadWithInterval
                        lead={`${formatSignedAmount(diff * 100)} percentage points`}
                        interval={
                          ci
                            ? {
                                level,
                                unadjusted: adjusted,
                                lower: formatSignedAmount(ci[0] * 100),
                                upper: formatSignedAmount(ci[1] * 100),
                              }
                            : null
                        }
                      />
                    ) : (
                      NOT_ENOUGH_DATA
                    )}
                  </td>
                  <td className="px-4 py-3 text-right text-sm tabular-nums text-slate-600">
                    {shownP(estimate, adjusted, data.correction_method)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {merged && (
        <p className="text-xs text-slate-600" data-testid="balanced-merged">
          {merged}
        </p>
      )}
      <p className="text-xs text-slate-500">{DECISION_UNCHANGED}</p>
    </div>
  );
}
