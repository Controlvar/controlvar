/**
 * Hiding the page while its changes are applied: `visibility: hidden
 * !important` on <html>, written through the CSSOM (so a strict
 * Content-Security-Policy allows it), never `opacity`, under which an
 * invisible page still takes clicks. The page shows again when the changes
 * are applied, when there is nothing to apply, or at data-timeout-ms,
 * whichever is first; showing it twice does nothing.
 */
import type { Captures } from "./captures";

export interface Hider {
  /** Shows the page now. Safe to call any number of times. */
  show(): void;
  readonly hidden: boolean;
}

const NOT_HIDDEN: Hider = { show: () => undefined, hidden: false };

export function hidePage(doc: Document, captures: Captures, timeoutMs: number): Hider {
  const root = doc.documentElement;
  if (timeoutMs <= 0 || !root) return NOT_HIDDEN;
  const style = root.style;
  const before = captures.getPropertyValue(style, "visibility");
  const priority = captures.getPropertyPriority(style, "visibility");
  captures.setProperty(style, "visibility", "hidden", "important");
  let hidden = true;
  const hider: Hider = {
    show() {
      if (!hidden) return;
      hidden = false;
      if (before) captures.setProperty(style, "visibility", before, priority);
      else captures.removeProperty(style, "visibility");
    },
    get hidden() {
      return hidden;
    },
  };
  captures.setTimeout(() => hider.show(), timeoutMs);
  return hider;
}
