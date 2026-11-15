/**
 * What a page says, as items (#1459, QA 1a): the unit `homeInventory.ts` lists.
 *
 * For each text node, climb through its inline ancestors (a link, a span, a
 * code element) to the first element that is not inline. If that is a TEXT
 * BLOCK (a heading, paragraph, term, definition, caption, summary or code
 * block), the item is that block's whole text, so a sentence with a link in
 * it is one item. Otherwise (a list item, a div, a nav) the item is the
 * outermost inline element's text, or the bare text node's: so each link in a
 * nav is its own item, and so is each bullet. `tag` is the element the item
 * sits in. Whitespace is collapsed, except in a code block, which keeps its
 * lines.
 *
 * Then every `alt`, `aria-label` and `title` in the container, and each
 * `<meta name="description">` in its document (React hoists it there;
 * `claimText.ts` says why).
 */
import { CLAIM_ATTRIBUTES, metaDescriptions } from './claimText';

const INLINE = new Set(['A', 'SPAN', 'CODE', 'STRONG', 'EM', 'B', 'I', 'SMALL', 'ABBR', 'KBD', 'TIME', 'SUP', 'SUB', 'MARK', 'S', 'U', 'Q', 'CITE', 'LABEL', 'BUTTON', 'BR']);
const TEXT_BLOCK = new Set(['H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'P', 'DT', 'DD', 'FIGCAPTION', 'SUMMARY', 'PRE', 'CAPTION', 'LEGEND', 'TH', 'TD']);

export interface Item {
  tag: string;
  text: string;
}

const collapse = (s: string): string => s.replace(/\s+/g, ' ').trim();

/** Every item the container renders, in document order. */
export function renderedItems(container: HTMLElement): Item[] {
  const items: Item[] = [];
  const seen = new Set<Node>();
  const walker = container.ownerDocument.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if ((node.textContent ?? '').trim() === '') continue;
    let outermostInline: Node = node;
    let parent = node.parentElement;
    while (parent !== null && parent !== container && INLINE.has(parent.tagName)) {
      outermostInline = parent;
      parent = parent.parentElement;
    }
    const block = parent !== null && TEXT_BLOCK.has(parent.tagName) ? parent : null;
    const root: Node = block ?? outermostInline;
    if (seen.has(root)) continue;
    seen.add(root);
    const raw = root.textContent ?? '';
    const tag = (parent ?? container).tagName.toLowerCase();
    items.push({ tag, text: tag === 'pre' ? raw.replace(/^\n+|\s+$/g, '') : collapse(raw) });
  }
  for (const el of Array.from(container.querySelectorAll('[alt], [aria-label], [title]'))) {
    for (const name of CLAIM_ATTRIBUTES) {
      const value = el.getAttribute(name);
      if (value !== null) items.push({ tag: name, text: collapse(value) });
    }
  }
  for (const description of metaDescriptions(container)) items.push({ tag: 'meta', text: collapse(description) });
  return items;
}

/** `tag|text`, the key an item and an inventory row are compared by. */
export const keyOf = (item: { tag: string; text: string }): string => `${item.tag}|${item.text}`;

/** What `have` holds more often than `want`, each as many times as it is extra. */
export function surplus(have: string[], want: string[]): string[] {
  const left = new Map<string, number>();
  for (const k of want) left.set(k, (left.get(k) ?? 0) + 1);
  const extra: string[] = [];
  for (const k of have) {
    const n = left.get(k) ?? 0;
    if (n > 0) left.set(k, n - 1);
    else extra.push(k);
  }
  return extra;
}
