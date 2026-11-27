import React from 'react';
import { ExperimentResultsResponse, MetricResult } from '@/types/results';
import { analysedAsMean } from '@/components/results/shared/resultFormat';
import { parseApiTimestamp } from '@/utils/apiTimestamp';
import { windowText } from '@/utils/conversionWindow';

/**
 * Who had less than a metric's conversion window (beta, #1934), one line per
 * metric with a window, below the sample ratio check.
 *
 * Counting stops with the experiment: an event received while it is paused,
 * completed or archived gets no variant. So users assigned near the end never
 * get their full window, and the server counts them (`open_window_users`) at
 * the time that applies: when the results were computed while the experiment
 * runs (or is a draft), at its end once it is completed or archived with an
 * end, and not at all while it is paused or archived with no end (null).
 *
 * Plain text, not an alert: like the sample ratio line it qualifies the
 * numbers. Nothing for a metric with no window, or from a server that sends
 * neither key (an older or cached answer).
 */

/** Paused, or archived with no end: what is said instead of a count. */
export const WINDOW_STOPPED_SENTENCE =
  'Events sent while the experiment is paused or archived are not counted, so users assigned shortly before it stopped have less than the full window.';

/** The completed and archived line's last sentence: a pause is not stored, so it cannot be counted. */
export const WINDOW_PAUSE_SENTENCE =
  'If it was paused, events sent during the pause were not counted either, so more users may have had less than the full window.';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const pad = (n: number) => String(n).padStart(2, '0');

/** "24 Nov 2026, 23:40 UTC"; null for a time that cannot be read. */
export function computedAtText(value: string | null | undefined): string | null {
  if (!value) return null;
  const at = parseApiTimestamp(value);
  if (Number.isNaN(at.getTime())) return null;
  return `${at.getUTCDate()} ${MONTHS[at.getUTCMonth()]} ${at.getUTCFullYear()}, ${pad(at.getUTCHours())}:${pad(at.getUTCMinutes())} UTC`;
}

const count = (n: number) => n.toLocaleString('en-US');
/** A share above 0 that rounds to 0% reads "<1%", one below the whole that rounds to 100% reads ">99%". */
const share = (part: number, whole: number) => {
  const percent = Math.round((part / whole) * 100);
  if (percent === 0 && part > 0) return '<1%';
  if (percent === 100 && part < whole) return '>99%';
  return `${percent}%`;
};

type Timing = Pick<ExperimentResultsResponse, 'status' | 'end_date' | 'computed_at'>;

/**
 * True when the counts were read at the experiment's end: completed, or
 * archived with an end at or before the time the results were computed.
 */
function readAtEnd(results: Timing): boolean {
  const status = (results.status ?? '').toLowerCase();
  if (status === 'completed') return true;
  if (status !== 'archived' || !results.end_date) return false;
  const end = parseApiTimestamp(results.end_date).getTime();
  const computed = parseApiTimestamp(results.computed_at).getTime();
  return !Number.isNaN(end) && !Number.isNaN(computed) && end <= computed;
}

/** The line for one metric, or null when there is nothing to say. */
export function windowNotice(results: Timing, metric: MetricResult): string | null {
  const hours = metric.conversion_window_hours;
  if (typeof hours !== 'number') return null;
  if (metric.variants.length === 0 || metric.variants.some((v) => !('open_window_users' in v))) return null;
  const name = metric.metric_name;
  const status = (results.status ?? '').toLowerCase();
  const atEnd = readAtEnd(results);
  if (!atEnd && (status === 'paused' || status === 'archived')) return `${name}: ${WINDOW_STOPPED_SENTENCE}`;
  if (!atEnd && status !== 'active' && status !== 'draft') return null;

  const counts = metric.variants.map((v) => v.open_window_users);
  if (counts.some((c) => typeof c !== 'number')) return null;
  const open = (counts as number[]).reduce((sum, c) => sum + c, 0);
  const total = metric.variants.reduce((sum, v) => sum + v.sample_size, 0);
  if (total === 0) return null;
  const users = `${count(open)} of ${count(total)} ${total === 1 ? 'user' : 'users'} (${share(open, total)})`;
  const span = windowText(hours);

  if (atEnd) {
    // Shown at 0 too: a pause longer than the window gives 0, and the last sentence still applies.
    return (
      `${name}: ${users} had less than ${span} before the experiment ended. ` +
      'Events received after it ended are not counted, so this does not change. ' +
      WINDOW_PAUSE_SENTENCE
    );
  }

  if (open === 0) return null;
  const computed = computedAtText(results.computed_at);
  if (computed === null) return null;
  const counted = analysedAsMean(metric) ? 'count as 0 until they send the event' : 'count as not converted until they convert';
  const inside = metric.variants
    .filter((v) => v.sample_size > 0)
    .map((v) => `${v.variant_name} ${share(v.open_window_users as number, v.sample_size)}`)
    .join(', ');
  return (
    `${name}: ${users} ${open === 1 ? 'was' : 'were'} still inside their ${span} ` +
    `when these results were computed (${computed}). ` +
    `They are in the sample size, and ${counted}. ` +
    'The numbers and the comparison can change, in either direction. ' +
    `Still inside: ${inside}.`
  );
}

/** Every metric's line, in the results' order. */
export function windowNotices(results: Timing & Pick<ExperimentResultsResponse, 'metrics'>): string[] {
  return results.metrics.map((m) => windowNotice(results, m)).filter((line): line is string => line !== null);
}

export function ConversionWindowNotices({
  results,
}: {
  results: Timing & Pick<ExperimentResultsResponse, 'metrics'>;
}) {
  const lines = windowNotices(results);
  if (lines.length === 0) return null;
  return (
    <div className="space-y-1" data-testid="window-notices">
      {lines.map((line) => (
        <p key={line} className="text-sm text-slate-700" data-testid="window-notice">
          {line}
        </p>
      ))}
    </div>
  );
}
