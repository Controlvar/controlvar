/**
 * The two readings of an element whose text is marked up for a screen
 * reader, as the results page's intervals are (#1496): "95% CI" is
 * `aria-hidden`, and a visually hidden (`sr-only`) twin says "95% confidence
 * interval". `textContent` holds both, so a test reads one or the other.
 */

/** What a sighted reader sees: the text without `.sr-only` elements. */
export function visibleText(el: Element): string {
  const clone = el.cloneNode(true) as Element;
  clone.querySelectorAll('.sr-only').forEach((n) => n.remove());
  return clone.textContent ?? '';
}

/** What a screen reader reads: the text without `aria-hidden="true"` elements. */
export function spokenText(el: Element): string {
  const clone = el.cloneNode(true) as Element;
  clone.querySelectorAll('[aria-hidden="true"]').forEach((n) => n.remove());
  return clone.textContent ?? '';
}
