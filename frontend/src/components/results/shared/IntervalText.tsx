import React from 'react';
import { IntervalParts } from '@/components/results/shared/resultFormat';

/**
 * An interval as the results page writes it (#1496): "95% CI +2.1% to
 * +22.5%", or "95% CI, unadjusted, …" on a row whose p-value is adjusted.
 *
 * - "95% CI" is hidden from a screen reader, which hears "95% confidence
 *   interval" from a visually hidden twin instead. "unadjusted" is real text.
 * - Each bound stays on one line with what precedes it, so on a phone the
 *   only place the interval can break is " to ": "99% CI +226.1%" and "above
 *   +1,000,000%" are never split.
 * - `open` and `close` ("(" and ")") are kept with the first and last bound,
 *   so a parenthesis never sits alone on a line.
 * - The first piece is `relative`, so the visually hidden twin (absolutely
 *   positioned) is placed inside it. Without that, a twin in the table's
 *   cells is placed against the page, escapes the table's own horizontal
 *   scroll, and widens the page on a phone.
 *
 * Seen, it reads `{open}95% CI[, unadjusted,] {lower} to {upper}{close}`;
 * a screen reader hears "confidence interval" in place of "CI".
 */
export function IntervalText({
  interval,
  open = '',
  close = '',
}: {
  interval: IntervalParts;
  open?: string;
  close?: string;
}) {
  const after = `${interval.unadjusted ? ', unadjusted,' : ''} ${interval.lower}`;
  return (
    <>
      <span className="relative whitespace-nowrap">
        {open}
        <span aria-hidden="true">{`${interval.level} CI`}</span>
        <span className="sr-only">{`${interval.level} confidence interval`}</span>
        {after}
      </span>
      {' to '}
      <span className="whitespace-nowrap">{`${interval.upper}${close}`}</span>
    </>
  );
}

/**
 * A number with its interval in parentheses, "+29 converting users (99% CI
 * +16 to +46)", or the lead alone when there is no interval.
 */
export function LeadWithInterval({
  lead,
  interval,
}: {
  lead: string;
  interval: IntervalParts | null;
}) {
  if (!interval) return <>{lead}</>;
  return (
    <>
      {`${lead} `}
      <IntervalText interval={interval} open="(" close=")" />
    </>
  );
}
