/**
 * A metric's conversion window (beta, #1934): how the dashboard reads what was
 * typed for `conversion_window_hours`, and the words it shows for one.
 *
 * The window is elapsed hours from a user's assignment: a whole number from 1
 * to 8,760, or none. The API stores hours; the dashboard also shows a multiple
 * of 24 in days, so 168 reads "7 days", and 7 typed for seven days reads
 * "7 hours".
 */

/**
 * The API's bounds (`MetricBase.conversion_window_hours`): the box refuses
 * what the API refuses. results-window-types.test.ts holds them to the API
 * snapshot's minimum and maximum.
 */
export const WINDOW_MIN_HOURS = 1;
export const WINDOW_MAX_HOURS = 8760;

/**
 * A window in words: a multiple of 24 in days, otherwise in hours. 1 is
 * "1 hour", 24 "1 day", 36 "36 hours", 168 "7 days", 8760 "365 days".
 */
export function windowText(hours: number): string {
  if (hours % 24 === 0) {
    const days = hours / 24;
    return `${days.toLocaleString('en-US')} ${days === 1 ? 'day' : 'days'}`;
  }
  return `${hours.toLocaleString('en-US')} ${hours === 1 ? 'hour' : 'hours'}`;
}

/**
 * A window with its hours beside the days: "7 days (168 hours)". A window
 * shown in hours already is not repeated: 36 is "36 hours".
 */
export function windowSpan(hours: number): string {
  return hours % 24 === 0 ? `${windowText(hours)} (${hours.toLocaleString('en-US')} hours)` : windowText(hours);
}

export type WindowParse = { ok: true; hours: number | null } | { ok: false };

/**
 * What the window box holds, read the way the API will take it: blank is none
 * (`hours` null); otherwise ASCII digits only, 1 to 8,760. "0", "8761", "1.5",
 * "7d", "1e2" and "-1" are refused here, because the API refuses them (422).
 */
export function parseWindowHours(text: string | undefined): WindowParse {
  const trimmed = (text ?? '').trim();
  if (trimmed === '') return { ok: true, hours: null };
  if (!/^[0-9]+$/.test(trimmed)) return { ok: false };
  const hours = Number(trimmed);
  return hours >= WINDOW_MIN_HOURS && hours <= WINDOW_MAX_HOURS ? { ok: true, hours } : { ok: false };
}

// --- the form ----------------------------------------------------------------

export const WINDOW_LABEL = 'Conversion window in hours (beta, optional)';

export const WINDOW_HELP_BLANK = 'Blank: an event counts however long after the assignment it comes.';

/** The help under a window that can be saved: the echo that shows 7 typed for seven days as hours. */
export function windowHelp(hours: number): string {
  return `Counts events in the ${windowSpan(hours)} from a user's assignment. Fixed once the experiment leaves draft.`;
}

/** The refusal of a window that cannot be saved, naming its metric. */
export function windowRefusal(metricName: string): string {
  const from = WINDOW_MIN_HOURS.toLocaleString('en-US');
  const to = WINDOW_MAX_HOURS.toLocaleString('en-US');
  return `“${metricName}”: the conversion window must be a whole number of hours from ${from} to ${to}, or blank.`;
}

/** What guided setup's Review adds to a metric's line. */
export function windowReviewText(hours: number): string {
  return `, conversion window ${windowText(hours)}`;
}

/** The experiment page's line in a metric's row. */
export function windowPageLine(hours: number): string {
  return `Conversion window: ${windowText(hours)}`;
}

/** The Estimate step's line when the primary metric has a window. */
export function windowEstimateLine(hours: number): string {
  const text = windowText(hours);
  return `The estimate counts users, not their windows: users who join in the last ${text} have less than ${text} when the experiment ends.`;
}

// --- the results -------------------------------------------------------------

/** The summary card's line for the primary metric's window. */
export function windowSummaryLine(hours: number): string {
  return `Conversion window: events in the ${windowSpan(hours)} from each user's assignment count.`;
}

/** The results table's tag on a metric with a window. */
export function windowTag(hours: number): string {
  return `within ${windowText(hours)}`;
}
