/**
 * SegmentComparisonTable — Issue #28: Dimensional Analysis & Segment Breakdown
 *
 * Shows per-segment variant means and significance badges.
 * Displays "Exploratory only" warning and HTE alert when relevant.
 */

import React from 'react';
import { DimensionalBreakdownResponse } from '@/types/results';
import {
  NOT_ENOUGH_DATA,
  formatRate,
  isFiniteNumber,
} from '@/components/results/shared/resultFormat';
import { docsUrl } from '@/services/docs';

/**
 * How many values a breakdown lists before it counts the rest under `other`
 * (the API's MAX_LISTED_SEGMENTS). The caption below says this number, so the
 * two move together.
 */
export const MAX_LISTED_SEGMENTS = 20;

/** The rows the API puts a user in when no listed value fits. */
const OTHER_SEGMENT = 'other';
const UNKNOWN_SEGMENT = 'unknown';

export const NO_ASSIGNED_USERS = 'No users are assigned to this experiment yet.';
export const ATTRIBUTION_CAPTION =
  'Each user is counted once, in the segment of the value the user was first assigned with, or else the first value tracked for them; unknown is users with none.';
export const OTHER_CAPTION = `Values beyond the ${MAX_LISTED_SEGMENTS} with the most users are counted together under other.`;

export interface SegmentComparisonTableProps {
  breakdown: DimensionalBreakdownResponse;
}

function SignificanceBadge({ isSignificant }: { isSignificant: boolean }) {
  return isSignificant ? (
    <span className="inline-flex items-center rounded-full bg-green-100 px-2 py-0.5 text-xs font-medium text-green-800">
      Significant
    </span>
  ) : (
    <span className="inline-flex items-center rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">
      Not significant
    </span>
  );
}

export function SegmentComparisonTable({
  breakdown,
}: SegmentComparisonTableProps) {
  const { segments, is_exploratory, has_heterogeneous_effects, hte_warning } =
    breakdown;
  // Every row is `unknown`: no user carries the dimension at all.
  const onlyUnknown =
    segments.length > 0 && segments.every((s) => s.segment_value === UNKNOWN_SEGMENT);
  const hasOther = segments.some((s) => s.segment_value === OTHER_SEGMENT);

  return (
    <div className="space-y-4" data-testid="segment-comparison-table">
      {/* Exploratory warning */}
      {is_exploratory && (
        <div
          className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3"
          role="note"
          aria-label="Exploratory analysis warning"
        >
          <p className="text-sm font-medium text-amber-800">
            Exploratory only
          </p>
          <p className="mt-0.5 text-xs text-amber-700">
            Segment breakdowns are exploratory analyses. Results use
            Bonferroni-corrected significance thresholds (α ={' '}
            {breakdown.adjusted_alpha.toFixed(4)}) and should not be the sole
            basis for shipping decisions.
          </p>
        </div>
      )}

      {/* HTE warning */}
      {has_heterogeneous_effects && hte_warning && (
        <div
          className="rounded-lg border border-red-200 bg-red-50 px-4 py-3"
          role="alert"
          data-testid="hte-warning"
        >
          <p className="text-sm font-medium text-red-800">
            Heterogeneous treatment effects detected
          </p>
          <p className="mt-0.5 text-xs text-red-700">{hte_warning}</p>
        </div>
      )}

      {segments.length === 0 ? (
        <p className="text-sm text-slate-500">{NO_ASSIGNED_USERS}</p>
      ) : (
        <>
        {onlyUnknown && (
          <p className="text-sm text-slate-700" data-testid="segment-unknown-only">
            No user of this experiment was assigned or tracked with{' '}
            <code className="rounded bg-slate-100 px-1 text-xs">{breakdown.dimension}</code>.
            Send it in the user&apos;s attributes when you assign them.{' '}
            <a
              href={docsUrl('api/dimensional-analysis', 'where-the-segments-come-from')}
              className="text-blue-700 underline hover:text-blue-900"
            >
              How a user gets a segment
            </a>
          </p>
        )}
        <div className="overflow-x-auto rounded-lg border border-slate-200">
          <table className="min-w-full divide-y divide-slate-200">
            <thead className="bg-slate-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                  Segment
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wide text-slate-500">
                  Sample
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wide text-slate-500">
                  Variant
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wide text-slate-500">
                  Mean
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wide text-slate-500">
                  p-value (unadjusted)
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium tracking-wide text-slate-500">
                  {/* The breakdown keeps the raw p and moves the threshold:
                      significant means p below the Bonferroni-adjusted alpha
                      (its value is in the note above). */}
                  <span className="uppercase">Significance</span>{' '}
                  <span className="normal-case">at adjusted (Bonferroni) α</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 bg-white">
              {segments.flatMap((seg) =>
                seg.variants.map((v, vIdx) => (
                  <tr key={`${seg.segment_value}-${v.variant_id}`}>
                    {/* Only show segment value on first variant row */}
                    {vIdx === 0 ? (
                      <td
                        className="px-4 py-3 align-top text-sm font-medium text-slate-900"
                        rowSpan={seg.variants.length}
                      >
                        {seg.segment_value}
                        <span className="ml-1.5 text-xs text-slate-400">
                          (n={seg.sample_size.toLocaleString()})
                        </span>
                      </td>
                    ) : null}
                    <td className="px-4 py-3 text-right text-sm text-slate-600">
                      {v.sample_size.toLocaleString()}
                    </td>
                    <td className="px-4 py-3 text-right text-sm text-slate-900">
                      {v.variant_name}
                      {v.is_control && (
                        <span className="ml-1 text-xs text-slate-400">
                          (ctrl)
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-right text-sm tabular-nums text-slate-900">
                      {formatRate(v.mean, v.sample_size)}
                    </td>
                    <td className="px-4 py-3 text-right text-sm tabular-nums text-slate-600">
                      {v.is_control
                        ? '—'
                        : isFiniteNumber(v.p_value)
                          ? v.p_value.toFixed(4)
                          : NOT_ENOUGH_DATA}
                    </td>
                    <td className="px-4 py-3 text-sm">
                      {!v.is_control && (
                        <SignificanceBadge isSignificant={v.is_significant} />
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
        <p className="text-xs text-slate-500" data-testid="segment-attribution-caption">
          {ATTRIBUTION_CAPTION}
        </p>
        {hasOther && (
          <p className="text-xs text-slate-500" data-testid="segment-other-caption">
            {OTHER_CAPTION}
          </p>
        )}
        </>
      )}
    </div>
  );
}
