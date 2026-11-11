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

/**
 * A focusable code block (axe scrollable-region-focusable) that wraps its lines
 * (How it works; each line a `CodeLine`, which breaks only at a space or after
 * a `(`). 12 px below `sm`, 13 px from it. It still scrolls sideways if a
 * single token is wider than the line, which no How it works token is from
 * 320 px up.
 */
export const CODE_BLOCK = `overflow-x-auto whitespace-pre-wrap rounded-lg bg-slate-900 p-4 font-mono text-[12px] leading-relaxed text-slate-100 sm:text-[13px] ${FOCUS}`;
