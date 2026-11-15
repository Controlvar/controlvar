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
 * The site's one type scale (site design 2026-11-11, finding 9): every page's
 * h1, and every section's h2, in every page of the public site. The h1 was
 * 48/600 on home, 36/700 on /docs, 30/700 on the power calculator and 30/600
 * on /privacy and the 404 page; on a phone it was 36 over a 30 px h2. Now it
 * is 36 px up to `lg` and 48 from it, over a 24 px h2 on a phone and 30 from
 * `sm`: 1.5 times on a phone, 1.6 on a desktop.
 */
export const H1 = 'text-4xl font-semibold tracking-tight text-slate-900 lg:text-5xl';
export const H2 = 'text-2xl font-semibold tracking-tight text-slate-900 sm:text-3xl';

/** A card's or a list entry's title, whatever its level. */
export const CARD_TITLE = 'text-lg font-semibold text-slate-900';

/** A link that leads on from a section, with an arrow after it. */
export const MORE_LINK = `font-semibold text-blue-700 hover:text-blue-900 ${FOCUS}`;

/**
 * A focusable code block (axe scrollable-region-focusable) that wraps its lines
 * (How it works; each line a `CodeLine`, which breaks only at a space or after
 * a `(`). 12 px below `sm`, 13 px from it. It still scrolls sideways if a
 * single token is wider than the line, which no How it works token is from
 * 320 px up.
 */
export const CODE_BLOCK = `overflow-x-auto whitespace-pre-wrap rounded-lg bg-slate-900 p-4 font-mono text-[12px] leading-relaxed text-slate-100 sm:text-[13px] ${FOCUS}`;
