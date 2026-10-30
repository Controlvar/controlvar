/**
 * What a claim sweep reads on a rendered page (#1459, QA 1f and PE 11).
 *
 * `textContent` alone misses the words a page says outside its text nodes: an
 * image's `alt`, an `aria-label` a screen reader speaks, a `title` tooltip, and
 * the `<meta name="description">` a search result and a link preview show (the
 * marketing build copies it into og:description). An uptime figure placed in
 * an `alt` passed every sweep (measured: 49 of 49 green). So a sweep matches
 * all of them.
 *
 * The meta description is NOT in the container. The tests mock `next/head` as
 * a fragment, and React 19 then hoists a `<meta>` (and the `<title>`) into the
 * document's `<head>`, wherever it was rendered; measured, the container held
 * none. So it is read from the container's document, and `metaDescriptions()`
 * lets a sweep assert it really found one, so a change to that mock or to
 * React's hoisting cannot make the sweep vacuous. Render one page per test:
 * the document holds every rendered tree's head tags.
 */

/** The attributes whose text a reader is shown or told. */
export const CLAIM_ATTRIBUTES = ['alt', 'aria-label', 'title'] as const;

/** Every `<meta name="description">` content in the document `container` is in. */
export function metaDescriptions(container: HTMLElement): string[] {
  return Array.from(container.ownerDocument.querySelectorAll('meta[name="description"]')).map(
    (meta) => meta.getAttribute('content') ?? '',
  );
}

/**
 * The text a claim sweep matches, one part per line: the text twice, then the
 * claim attributes and the meta description.
 *
 * The text twice, because each reading hides something the other shows.
 * `textContent` runs adjacent elements together: a heading followed by "14
 * SDKs" reads "have14 SDKs", which no longer starts a word (measured: the
 * digit-count rule passed it). The text nodes joined with a space keep that
 * boundary, but split a word one element breaks in two. Both are swept.
 */
export function claimText(container: HTMLElement): string {
  const nodes: string[] = [];
  const walker = container.ownerDocument.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    nodes.push(node.textContent ?? '');
  }
  const parts: string[] = [container.textContent ?? '', nodes.join(' ')];
  for (const el of Array.from(container.querySelectorAll('[alt], [aria-label], [title]'))) {
    for (const name of CLAIM_ATTRIBUTES) {
      const value = el.getAttribute(name);
      if (value) parts.push(value);
    }
  }
  parts.push(...metaDescriptions(container));
  return parts.join('\n');
}
