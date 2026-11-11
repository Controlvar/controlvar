import React from 'react';
import { MetricResult, SrmResult } from '@/types/results';
import { docsUrl } from '@/services/docs';
import { isFiniteNumber } from '@/components/results/shared/resultFormat';

interface SrmNoticeProps {
  srm: SrmResult | null | undefined;
  /** Used only to name the variants the check keys by id. */
  metrics: MetricResult[];
}

export const SRM_HEADING = 'The traffic split does not match the allocation.';
export const SRM_BODY =
  'When this happens, assignment or tracking is usually broken, so these results should not ' +
  'be trusted, including the recommendation. Find the cause before you decide.';
/**
 * The qualifier the "Leading" chip, the Bayesian panel and the Sequential
 * tab's stopping banner carry under a mismatch.
 */
export const SRM_QUALIFIER =
  'The traffic split does not match the allocation, so treat this with caution.';

/**
 * A p-value as the notice shows it: "p < 0.001" below the server's threshold,
 * otherwise two significant figures ("p = 0.39", "p = 1").
 */
export function formatSrmP(p: number): string {
  if (!isFiniteNumber(p)) return 'p unavailable';
  if (p < 0.001) return 'p < 0.001';
  return `p = ${Number(p.toPrecision(2))}`;
}

function formatCount(n: number): string {
  return isFiniteNumber(n) ? Math.round(n).toLocaleString('en-US') : '—';
}

/** Variant ids in the results' order (control first), then any the check adds. */
function orderedIds(srm: SrmResult, metrics: MetricResult[]): string[] {
  const known = (metrics[0]?.variants ?? []).map((v) => v.variant_id);
  const fromCheck = Object.keys({ ...srm.expected, ...srm.observed });
  return [
    ...known.filter((id) => fromCheck.includes(id)),
    ...fromCheck.filter((id) => !known.includes(id)),
  ];
}

function variantName(id: string, metrics: MetricResult[]): string {
  for (const metric of metrics) {
    const v = metric.variants.find((vr) => vr.variant_id === id);
    if (v) return v.variant_name;
  }
  return id;
}

/**
 * The sample-ratio check, above the tabs because a mismatch qualifies every
 * tab. It follows the server's `warning` and applies no threshold of its own.
 *
 * - `srm` null (a bandit, no assignments yet, or a check that failed on the
 *   server): nothing. Absence is not a pass, so nothing says "passed".
 * - `warning` false: one quiet line, so a reader can see the split is checked.
 * - `warning` true: a region with the observed and expected counts.
 *
 * Visible text in a region, not an alert: it qualifies the numbers.
 */
export function SrmNotice({ srm, metrics }: SrmNoticeProps) {
  if (!srm) return null;

  if (!srm.warning) {
    return (
      <p className="text-sm text-slate-600" data-testid="srm-check-passed">
        Sample ratio check passed: the traffic split matches the allocation (
        <span data-testid="srm-p">{formatSrmP(srm.p_value)}</span>).
      </p>
    );
  }

  const ids = orderedIds(srm, metrics);
  return (
    <section
      aria-labelledby="srm-notice-heading"
      data-testid="srm-notice"
      className="rounded-lg border border-amber-400 bg-amber-50 p-4 space-y-2"
    >
      <h3 id="srm-notice-heading" className="text-sm font-semibold text-amber-900">
        <span aria-hidden="true">⚠ </span>
        {SRM_HEADING}
      </h3>
      <ul className="text-sm text-amber-900" data-testid="srm-counts">
        {ids.map((id) => (
          <li key={id} data-testid="srm-count">
            {variantName(id, metrics)}: {formatCount(srm.observed[id])} users (expected{' '}
            {formatCount(srm.expected[id])})
          </li>
        ))}
      </ul>
      <p className="text-sm text-amber-900">
        Chi-square {isFiniteNumber(srm.chi2) ? srm.chi2.toFixed(1) : '—'},{' '}
        <span data-testid="srm-p">{formatSrmP(srm.p_value)}</span>.
      </p>
      <p className="text-sm text-amber-900">{SRM_BODY}</p>
      <a
        href={docsUrl('guides/user-guide', 'sample-ratio-check')}
        target="_blank"
        rel="noopener noreferrer"
        className="text-sm text-amber-900 underline"
        data-testid="srm-docs-link"
      >
        What a sample ratio mismatch is
      </a>
    </section>
  );
}
