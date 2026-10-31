/**
 * Class strings the homepage's sections share (#1459).
 *
 * A focus outline every link, button, chip and focusable code block draws
 * when the keyboard reaches it, so it never depends on the browser's default
 * (`tests/e2e/marketing-site.site.spec.ts` Tabs through the page and fails a
 * stop that draws nothing). On the dark band it is white.
 */
export const FOCUS =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600';

export const FOCUS_ON_DARK =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white';

/** The width every section lines up on: the header's row (SiteHeader.tsx). */
export const WIDTH = 'mx-auto max-w-[1180px] px-6';

/** A focusable code block (axe scrollable-region-focusable): it scrolls sideways when a line is too long. */
export const CODE_BLOCK = `overflow-x-auto rounded-lg bg-slate-900 p-4 font-mono text-[13px] leading-relaxed text-slate-100 ${FOCUS}`;
