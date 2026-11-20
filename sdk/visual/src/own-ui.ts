/**
 * The script's own UI on the page: the preview banner, in one host element
 * appended to <html> beside <body>, with a closed shadow root. The host sets
 * `visibility: visible` on itself, so it shows while the page is hidden, and
 * every style is written through the CSSOM, so a strict Content-Security-
 * Policy allows it. While it exists one MutationObserver puts it back if the
 * page removes it.
 *
 * No change is ever applied to <html>, the host or anything inside it: the
 * runtime asks `owns()` (an identity check, not a selector) before each write,
 * and reports such a change as `own_ui`.
 */
import type { Captures } from "./captures";

export const HOST_TAG = "controlvar-ui";

export interface OwnUi {
  /** Whether *element* is <html>, the host or inside the host. */
  owns(element: Element): boolean;
  /** Shows *text* as the banner's message. */
  say(text: string): void;
  /** Takes the host off the page and stops watching for its removal. */
  remove(): void;
}

type Styles = Record<string, string>;

const HOST_STYLE: Styles = {
  display: "block",
  position: "fixed",
  left: "0",
  right: "0",
  bottom: "0",
  "z-index": "2147483647",
  visibility: "visible",
  opacity: "1",
  margin: "0",
  padding: "0",
  border: "0",
  transform: "none",
  "font-size": "16px",
};

const BAR_STYLE: Styles = {
  display: "flex",
  "align-items": "center",
  gap: "12px",
  padding: "10px 16px",
  background: "#111",
  color: "#fff",
  font: "14px/1.4 system-ui, sans-serif",
  visibility: "visible",
  opacity: "1",
};

const BUTTON_STYLE: Styles = {
  "margin-left": "auto",
  padding: "6px 12px",
  background: "#fff",
  color: "#111",
  border: "0",
  "border-radius": "4px",
  font: "inherit",
  cursor: "pointer",
  visibility: "visible",
  opacity: "1",
};

function styled<E extends HTMLElement>(captures: Captures, element: E, styles: Styles): E {
  for (const name of Object.keys(styles)) captures.setProperty(element.style, name, styles[name], "important");
  return element;
}

export function createOwnUi(
  win: Window & typeof globalThis,
  captures: Captures,
  label: string,
  action: { text: string; run(): void },
): OwnUi {
  const doc = win.document;
  const host = styled(captures, doc.createElement(HOST_TAG), HOST_STYLE);
  const shadow = host.attachShadow({ mode: "closed" });
  const bar = styled(captures, doc.createElement("div"), BAR_STYLE);
  bar.setAttribute("role", "region");
  bar.setAttribute("aria-label", label);
  const title = doc.createElement("strong");
  title.textContent = label;
  const message = doc.createElement("span");
  message.setAttribute("aria-live", "polite");
  const button = styled(captures, doc.createElement("button"), BUTTON_STYLE);
  button.setAttribute("type", "button");
  button.textContent = action.text;
  button.addEventListener("click", () => {
    try {
      action.run();
    } catch {
      // The page stays as it is.
    }
  });
  bar.appendChild(title);
  bar.appendChild(message);
  bar.appendChild(button);
  shadow.appendChild(bar);

  const root = doc.documentElement;
  root.appendChild(host);
  let removed = false;
  const observer = new win.MutationObserver(() => {
    if (!removed && host.parentNode !== root) root.appendChild(host);
  });
  observer.observe(root, { childList: true });

  return {
    owns(element) {
      return element === doc.documentElement || element === host || host.contains(element);
    },
    say(text) {
      message.textContent = text;
    },
    remove() {
      removed = true;
      observer.disconnect();
      if (host.parentNode) host.parentNode.removeChild(host);
    },
  };
}
