/**
 * Reading a time the API sent (#1134).
 *
 * The API writes some date-times with a zone (`2026-10-07T05:42:19+00:00`) and
 * some without (`2026-10-11T05:02:00`, a stored UTC value). A browser reads the
 * second kind as LOCAL time, so `new Date(value).toLocaleString()` showed the
 * UTC clock reading as if it were the viewer's: 5:02 AM for someone in Los
 * Angeles who should see 10:02 PM the evening before.
 *
 * A date-time with no zone from the API is UTC. Use this for every API
 * timestamp that is shown or compared. Do not use it for a value the user
 * typed into a `datetime-local` input, or one the dashboard made itself
 * (`new Date()`): those are local by design.
 */

// `2026-10-11T05:02`, `...T05:02:00`, `...T05:02:00.391716`, with `T` or a
// space between the date and the time, and nothing after. A zone (`Z`, `+hh`,
// `+hh:mm`, `+hhmm`, `-hh:mm`) makes it not match, so a string that names its
// zone is left alone. A date alone (`2026-10-11`) does not match either: a
// browser already reads it as UTC, and `...Z` would make it invalid.
const NO_ZONE = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)$/;

/**
 * The instant an API time stands for, as a `Date`. A string that is not a time
 * gives an Invalid Date, as `new Date(value)` does; callers already check
 * `getTime()` for NaN.
 */
export function parseApiTimestamp(value: string | number | Date): Date {
  if (typeof value === 'string') {
    const match = NO_ZONE.exec(value);
    if (match) return new Date(`${match[1]}T${match[2]}Z`);
  }
  return new Date(value);
}
