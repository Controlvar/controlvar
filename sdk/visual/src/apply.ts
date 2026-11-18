/**
 * Applying checked changes to the page, through the only writes the script
 * makes: a text node's text, setAttribute for five names, an image's src,
 * and style properties through the CSSOM. Nothing is ever parsed as HTML.
 *
 * A change applies only when its selector matches exactly one element. While
 * the page is still loading, a change is applied as soon as its element
 * exists (a text change once its element is closed: a node follows it that
 * it does not contain). At DOMContentLoaded every change is checked once
 * more: one whose selector matches several elements is undone and reported.
 * The page is hidden until then.
 *
 * Until the bound (bound.ts) one MutationObserver keeps the changes: when the
 * page replaces a changed element, or writes over a change, the change is
 * written again, at most 20 times in any second for each change. A change
 * whose element has not appeared by the bound is reported then. After the
 * bound no observer is left; `lost()` (at pagehide, or when the tab is
 * hidden) reports, once each, the changes whose element the page replaced
 * since.
 */
import type { Bound } from "./bound";
import type { Captures } from "./captures";
import type { NotApplied, Reporter } from "./reports";
import type { Change } from "./validate";

/** More re-applies than this within one second stop that change. */
export const MAX_REAPPLIES = 20;
export const REAPPLY_WINDOW_MS = 1000;

const HTML_NAMESPACE = "http://www.w3.org/1999/xhtml";

/** Elements the script never changes, whatever the change. */
export const NEVER_CHANGED = [
  "script",
  "style",
  "link",
  "meta",
  "base",
  "title",
  "iframe",
  "frame",
  "object",
  "embed",
  "template",
  "noscript",
] as const;

/** Which elements each URL-carrying change may touch. */
const LINK_ELEMENTS = ["a", "area"];
const IMAGE_ELEMENTS = ["img"];

interface Item {
  experimentKey: string;
  index: number;
  change: Change;
  state: "pending" | "applied" | "failed";
  target: Element | null;
  undo: (() => void) | null;
  /** What the change's values read as right after the script wrote them. */
  written: string;
  /** When each re-apply of the last second happened. */
  reapplies: number[];
  /** Reported as replaced after the bound. */
  lost: boolean;
}

export interface ApplyEnv {
  window: Window & typeof globalThis;
  captures: Captures;
  reporter: Reporter;
  /** Every element the script has written to on this page. */
  written: WeakSet<Element>;
}

/** Whether the script refuses to apply *change* to *element*. */
export function notAllowed(element: Element, change: Change): boolean {
  if (element.namespaceURI !== HTML_NAMESPACE) return true;
  const tag = element.localName;
  if ((NEVER_CHANGED as readonly string[]).indexOf(tag) >= 0) return true;
  if (change.op === "set_attribute" && change.name === "href") return LINK_ELEMENTS.indexOf(tag) < 0;
  if (change.op === "set_image") return IMAGE_ELEMENTS.indexOf(tag) < 0;
  return false;
}

/** Whether the parser has finished *element*: some node follows it that it does not contain. */
export function closed(element: Element): boolean {
  for (let node: Node | null = element; node; node = node.parentNode) {
    if (node.nextSibling) return true;
  }
  return false;
}

function webUrl(win: Window & typeof globalThis, value: string): boolean {
  try {
    const protocol = new win.URL(value, win.location.href).protocol;
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}

function setAttribute(element: Element, name: string, value: string): () => void {
  const before = element.getAttribute(name);
  if (before !== value) element.setAttribute(name, value);
  return () => {
    if (element.getAttribute(name) !== value) return;
    if (before === null) element.removeAttribute(name);
    else element.setAttribute(name, before);
  };
}

function removeAttribute(element: Element, name: string): () => void {
  const before = element.getAttribute(name);
  if (before !== null) element.removeAttribute(name);
  return () => {
    if (before !== null && element.getAttribute(name) === null) element.setAttribute(name, before);
  };
}

function setStyle(captures: Captures, element: Element, name: string, value: string): () => void {
  const style = (element as HTMLElement).style;
  const before = captures.getPropertyValue(style, name);
  const priority = captures.getPropertyPriority(style, name);
  if (before !== value || priority !== "important") captures.setProperty(style, name, value, "important");
  return () => {
    if (captures.getPropertyValue(style, name) !== value) return;
    if (before) captures.setProperty(style, name, before, priority);
    else captures.removeProperty(style, name);
  };
}

function setText(doc: Document, element: Element, text: string): () => void {
  const children = element.childNodes;
  if (children.length === 1 && children[0].nodeType === 3) {
    const node = children[0];
    const before = node.nodeValue;
    if (before !== text) node.nodeValue = text;
    return () => {
      if (node.nodeValue === text) node.nodeValue = before;
    };
  }
  const removed: Node[] = [];
  for (let index = 0; index < children.length; index += 1) removed.push(children[index]);
  for (const child of removed) element.removeChild(child);
  const node = doc.createTextNode(text);
  element.appendChild(node);
  return () => {
    if (node.parentNode !== element) return;
    element.removeChild(node);
    for (const child of removed) element.appendChild(child);
  };
}

function setImage(element: Element, src: string, alt: string | null): () => void {
  const undo: Array<() => void> = [];
  const picture = element.parentElement;
  if (picture && picture.localName === "picture" && picture.namespaceURI === HTML_NAMESPACE) {
    const sources: Array<{ node: Element; next: Node | null }> = [];
    for (let index = 0; index < picture.children.length; index += 1) {
      const child = picture.children[index];
      if (child.localName === "source") sources.push({ node: child, next: child.nextSibling });
    }
    for (const { node } of sources) picture.removeChild(node);
    undo.push(() => {
      for (const { node, next } of sources) picture.insertBefore(node, next && next.parentNode === picture ? next : element);
    });
  }
  undo.push(removeAttribute(element, "srcset"), removeAttribute(element, "sizes"), setAttribute(element, "src", src));
  if (alt !== null) undo.push(setAttribute(element, "alt", alt));
  return () => {
    for (let index = undo.length - 1; index >= 0; index -= 1) undo[index]();
  };
}

/** Writes *change* to *element*; the answer undoes it. Null when the value is not a web address. */
function write(env: ApplyEnv, element: Element, change: Change): (() => void) | null {
  const { window: win, captures } = env;
  switch (change.op) {
    case "set_text":
      return setText(win.document, element, change.text);
    case "set_attribute":
      if (change.name === "href" && !webUrl(win, change.value)) return null;
      return setAttribute(element, change.name, change.value);
    case "set_image":
      if (!webUrl(win, change.src)) return null;
      return setImage(element, change.src, change.alt);
    case "hide":
      return setStyle(captures, element, "display", "none");
    case "set_style": {
      const undo = Object.keys(change.styles).map((name) => setStyle(captures, element, name, change.styles[name]));
      return () => {
        for (let index = undo.length - 1; index >= 0; index -= 1) undo[index]();
      };
    }
    default:
      return null;
  }
}

/**
 * What *change*'s values read as on *element* now, as the browser serialises
 * them (a colour written as `#FF0000` reads back as `rgb(255, 0, 0)`), so that
 * a change is written again only when something else has changed them.
 */
function readBack(captures: Captures, element: Element, change: Change): string {
  const style = (element as HTMLElement).style;
  const property = (name: string) =>
    style ? `${captures.getPropertyValue(style, name)}!${captures.getPropertyPriority(style, name)}` : "";
  switch (change.op) {
    case "set_text":
      return JSON.stringify(element.textContent);
    case "set_attribute":
      return JSON.stringify(element.getAttribute(change.name));
    case "set_image": {
      const picture = element.parentElement;
      const sources = picture && picture.localName === "picture" ? picture.getElementsByTagName("source").length : 0;
      return JSON.stringify([
        element.getAttribute("src"),
        element.getAttribute("alt"),
        element.getAttribute("srcset"),
        element.getAttribute("sizes"),
        sources,
      ]);
    }
    case "hide":
      return property("display");
    case "set_style":
      return JSON.stringify(Object.keys(change.styles).map(property));
    default:
      return "";
  }
}

export interface Applier {
  add(experimentKey: string, changes: Change[]): void;
  readonly empty: boolean;
  /**
   * Applies what it can now and, while the page loads, as its elements
   * arrive; calls *done* at DOMContentLoaded (or now, if it has passed),
   * when every change that can be applied by then is. Keeps the changes
   * until *bound*.
   */
  start(done: () => void, bound: Bound): void;
  /** Undoes every change and stops watching, reporting nothing (the page moved to another path). */
  stop(): void;
  /** Reports, once each, the applied changes whose element the page has replaced since the bound. */
  lost(): void;
}

type Pass = "parsing" | "loaded" | "watching" | "bound";

export function createApplier(env: ApplyEnv): Applier {
  const doc = env.window.document;
  const items: Item[] = [];

  let observer: MutationObserver | null = null;
  let stopped = false;

  function fail(item: Item, reason: NotApplied): void {
    if (item.undo) item.undo();
    item.undo = null;
    item.target = null;
    item.state = "failed";
    env.reporter.notApplied(item.experimentKey, item.index, reason);
  }

  function applyTo(item: Item, element: Element): void {
    if (notAllowed(element, item.change)) return fail(item, "not_allowed");
    let undo: (() => void) | null;
    try {
      undo = write(env, element, item.change);
    } catch {
      return fail(item, "invalid");
    }
    if (undo === null) return fail(item, "not_allowed");
    env.written.add(element);
    item.undo = undo;
    item.target = element;
    item.written = readBack(env.captures, element, item.change);
    item.state = "applied";
  }

  /** Writes an applied change again (its element was replaced, or written over), within the cap. */
  function reapply(item: Item, element: Element): void {
    const now = env.captures.now();
    item.reapplies = item.reapplies.filter((at) => now - at < REAPPLY_WINDOW_MS);
    if (item.reapplies.length >= MAX_REAPPLIES) return fail(item, "reapply_limit");
    item.reapplies.push(now);
    if (element !== item.target) item.undo = null;
    applyTo(item, element);
  }

  function visit(item: Item, pass: Pass): void {
    if (item.state === "failed") return;
    let matches: NodeListOf<Element>;
    try {
      matches = doc.querySelectorAll(item.change.selector);
    } catch {
      return fail(item, "invalid");
    }
    const count = matches.length;
    if (pass === "loaded" && count > 1) return fail(item, "ambiguous");
    if (pass === "bound" && item.state === "pending" && count !== 1) {
      return fail(item, count === 0 ? "not_found" : "ambiguous");
    }
    if (count !== 1) return;
    const element = matches[0];
    const waiting = pass === "parsing" && item.change.op === "set_text" && !closed(element);
    if (item.state === "pending") {
      if (waiting && !notAllowed(element, item.change)) return;
      return applyTo(item, element);
    }
    if (element === item.target && readBack(env.captures, element, item.change) === item.written) return;
    if (waiting && element !== item.target) return;
    reapply(item, element);
  }

  function run(pass: Pass): void {
    for (const item of items) {
      try {
        visit(item, pass);
      } catch {
        if (item.state !== "failed") fail(item, "invalid");
      }
    }
  }

  function disconnect(): void {
    if (observer) observer.disconnect();
    observer = null;
  }

  return {
    add(experimentKey, changes) {
      changes.forEach((change, index) => {
        items.push({
          experimentKey,
          index,
          change,
          state: "pending",
          target: null,
          undo: null,
          written: "",
          reapplies: [],
          lost: false,
        });
      });
    },
    get empty() {
      return items.length === 0;
    },
    start(done, bound) {
      let loaded = doc.readyState !== "loading";
      let revealed = false;
      const reveal = (): void => {
        if (revealed) return;
        revealed = true;
        loaded = true;
        try {
          if (!stopped) run("loaded");
        } finally {
          done();
        }
      };
      try {
        if (!bound.passed) {
          observer = new env.window.MutationObserver(() => {
            if (!stopped) run(loaded ? "watching" : "parsing");
          });
          observer.observe(doc, { childList: true, subtree: true, attributes: true, characterData: true });
        }
        if (loaded) {
          reveal();
        } else {
          doc.addEventListener("DOMContentLoaded", reveal);
          run("parsing");
        }
      } catch {
        reveal();
      }
      bound.whenPassed(() => {
        disconnect();
        if (!stopped) run("bound");
      });
    },
    stop() {
      stopped = true;
      disconnect();
      for (let index = items.length - 1; index >= 0; index -= 1) {
        const item = items[index];
        try {
          if (item.undo) item.undo();
        } catch {
          // An undo that fails leaves that element as it is.
        }
        item.undo = null;
        item.target = null;
        item.state = "failed";
      }
    },
    lost() {
      if (observer || stopped) return;
      for (const item of items) {
        if (item.state !== "applied" || item.lost) continue;
        let matches: NodeListOf<Element>;
        try {
          matches = doc.querySelectorAll(item.change.selector);
        } catch {
          continue;
        }
        if (matches.length !== 1 || env.written.has(matches[0])) continue;
        item.lost = true;
        env.reporter.notApplied(item.experimentKey, item.index, "replaced_after_bound", { keepalive: true });
      }
    },
  };
}
