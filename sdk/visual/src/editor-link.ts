import { BASE64URL_ALPHABET, base64url } from "./base64url";
import type { Captures } from "./captures";
import type { Config } from "./config";
import { KEYS } from "./keys";
import type { Send } from "./send";

/**
 * The visual editor's link: the page is opened with
 * `#controlvar-editor=<43 base64url characters>` (a one-time sign-in). Run
 * in the script's first synchronous run, before anything else but the
 * captures. Any other value after `#controlvar-editor=` is ignored.
 */
export const EDITOR_PREFIX = "#controlvar-editor=";
export const TICKET_LENGTH = 43;
export const CLAIM_PATH = "/api/v1/visual-editor/tickets/claim";
export const PANEL_PATH = "/visual-editor/panel";
export const HANDSHAKE_MS = 5000;
export const HANDSHAKE_MESSAGE =
  "The visual editor did not open: its panel did not answer within 5 seconds. " +
  "Your site's Content-Security-Policy may not allow it in a frame (frame-src).";
export const NO_EDITOR_ORIGIN =
  "Controlvar: this page was opened with a visual editor link, but the script tag has no data-editor-origin, so the editor stays closed.";

export interface EditorLinkEnv {
  window: Window & typeof globalThis;
  captures: Captures;
  config: Config;
  send: Send;
}

export interface EditorLink {
  /** The page was opened with the exact editor fragment. */
  opened: boolean;
}

/**
 * The link's value when *hash* is exactly `#controlvar-editor=` and 43
 * base64url characters, else null. Checked with string operations only.
 */
export function editorTicket(hash: string): string | null {
  if (hash.length !== EDITOR_PREFIX.length + TICKET_LENGTH) return null;
  if (hash.slice(0, EDITOR_PREFIX.length) !== EDITOR_PREFIX) return null;
  const ticket = hash.slice(EDITOR_PREFIX.length);
  for (let i = 0; i < ticket.length; i += 1) {
    if (BASE64URL_ALPHABET.indexOf(ticket.charAt(i)) < 0) return null;
  }
  return ticket;
}

function isHello(data: unknown): boolean {
  if (typeof data !== "object" || data === null) return false;
  const message = data as { cv?: unknown; type?: unknown };
  return message.cv === 1 && message.type === "hello";
}

/** Whether *win* shows a document from another origin than the page's: reading its document throws. */
function onAnotherOrigin(win: Window): boolean {
  try {
    return win.document === undefined;
  } catch {
    return true;
  }
}

function showHandshakeMessage(doc: Document): void {
  const box = doc.createElement("div");
  box.setAttribute("role", "alert");
  box.textContent = HANDSHAKE_MESSAGE;
  const style = box.style;
  style.setProperty("position", "fixed", "important");
  style.setProperty("left", "16px", "important");
  style.setProperty("bottom", "16px", "important");
  style.setProperty("max-width", "420px", "important");
  style.setProperty("padding", "12px 16px", "important");
  style.setProperty("background", "#fff", "important");
  style.setProperty("color", "#111", "important");
  style.setProperty("border", "1px solid #b00", "important");
  style.setProperty("font", "14px/1.4 system-ui, sans-serif", "important");
  style.setProperty("z-index", "2147483647", "important");
  style.setProperty("visibility", "visible", "important");
  doc.documentElement.appendChild(box);
}

export function runEditorLink(env: EditorLinkEnv): EditorLink {
  const { window: win, captures, config, send } = env;
  const doc = win.document;
  let ticket = editorTicket(win.location.hash);
  if (ticket === null) return { opened: false };

  captures.replaceState(win.history.state, "", win.location.pathname + win.location.search);

  let claim: string | null = base64url(captures.getRandomValues(new Uint8Array(32)));
  const pending = send(CLAIM_PATH, { ticket, claim }, { editorLink: true });
  ticket = null;
  if (pending === null) {
    claim = null;
    return { opened: true };
  }

  const editorOrigin = config.editorOrigin;
  if (editorOrigin === null) {
    claim = null;
    pending.then(
      () => undefined,
      () => undefined,
    );
    win.console.warn(NO_EDITOR_ORIGIN);
    return { opened: true };
  }

  let settled = false;
  let helloSeen = false;

  // The panel's own request carries no Referer, whatever the page's policy.
  // Set before src, so the first navigation already has it.
  const frame = doc.createElement("iframe");
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.setAttribute("title", "Controlvar visual editor");
  frame.setAttribute("src", editorOrigin + PANEL_PATH);
  const style = frame.style;
  style.setProperty("position", "fixed", "important");
  style.setProperty("top", "0", "important");
  style.setProperty("right", "0", "important");
  style.setProperty("width", "380px", "important");
  style.setProperty("height", "100%", "important");
  style.setProperty("border", "0", "important");
  style.setProperty("z-index", "2147483647", "important");
  style.setProperty("visibility", "visible", "important");
  doc.documentElement.appendChild(frame);
  const panel = captures.contentWindow(frame);

  const handOver = (): undefined => {
    if (!settled || !helloSeen || claim === null || panel === null) return undefined;
    const message = { cv: 1, type: "claim", claim };
    claim = null;
    // Handed over only to a panel frame on another origin; otherwise dropped.
    if (!onAnotherOrigin(panel)) return undefined;
    panel.postMessage(message, editorOrigin);
    return undefined;
  };
  const onSettled = (): undefined => {
    settled = true;
    return handOver();
  };
  pending.then(onSettled, onSettled);

  captures.addEventListener("message", (event: MessageEvent): undefined => {
    // Only a message the browser delivered counts.
    if (event.isTrusted !== true) return undefined;
    if (helloSeen) return undefined;
    if (captures.messageSource(event) !== panel) return undefined;
    if (captures.messageOrigin(event) !== editorOrigin) return undefined;
    if (!isHello(captures.messageData(event))) return undefined;
    helloSeen = true;
    return handOver();
  });

  try {
    win.sessionStorage.setItem(KEYS.editorOpen, "1");
  } catch {
    // Storage can be off (a private window, blocked site data): the editor still opens.
  }

  captures.setTimeout(() => {
    if (!helloSeen) showHandshakeMessage(doc);
  }, HANDSHAKE_MS);

  return { opened: true };
}
