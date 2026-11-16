/**
 * Applying checked changes to the page, through the only writes the script
 * makes: a text node's text, setAttribute for five names, an image's src,
 * and style properties through the CSSOM. Nothing is ever parsed as HTML.
 *
 * A change applies only when its selector matches exactly one element. While
 * the page is still loading, a change is applied as soon as its element
 * exists (a text change once its element is closed: a node follows it that
 * it does not contain); at DOMContentLoaded every change is checked once
 * more, and one whose selector no longer matches exactly one element is
 * undone and reported. The page is hidden until then.
 */
import type { Captures } from "./captures";
import type { NotApplied, Reporter } from "./reports";
import type { Change } from "./validate";

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
}

export interface ApplyEnv {
  window: Window & typeof globalThis;
  captures: Captures;
  reporter: Reporter;
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

export interface Applier {
  add(experimentKey: string, changes: Change[]): void;
  readonly empty: boolean;
  /** Applies what it can now and, while the page loads, as its elements arrive; calls *done* once every change is settled. */
  start(done: () => void): void;
}

export function createApplier(env: ApplyEnv): Applier {
  const doc = env.window.document;
  const items: Item[] = [];

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
    item.undo = undo;
    item.target = element;
    item.state = "applied";
  }

  function visit(item: Item, final: boolean): void {
    if (item.state === "failed") return;
    let matches: NodeListOf<Element>;
    try {
      matches = doc.querySelectorAll(item.change.selector);
    } catch {
      return fail(item, "invalid");
    }
    if (!final) {
      if (matches.length !== 1 || item.state === "applied") return;
      const element = matches[0];
      if (item.change.op === "set_text" && !closed(element) && !notAllowed(element, item.change)) return;
      return applyTo(item, element);
    }
    if (matches.length === 0) return fail(item, "not_found");
    if (matches.length > 1) return fail(item, "ambiguous");
    const element = matches[0];
    if (item.state === "applied" && item.target === element) return;
    if (item.undo) item.undo();
    item.undo = null;
    applyTo(item, element);
  }

  function pass(final: boolean): void {
    for (const item of items) {
      try {
        visit(item, final);
      } catch {
        if (item.state !== "failed") fail(item, "invalid");
      }
    }
  }

  return {
    add(experimentKey, changes) {
      changes.forEach((change, index) => {
        items.push({ experimentKey, index, change, state: "pending", target: null, undo: null });
      });
    },
    get empty() {
      return items.length === 0;
    },
    start(done) {
      let finished = false;
      let observer: MutationObserver | null = null;
      const finish = (): void => {
        if (finished) return;
        finished = true;
        if (observer) observer.disconnect();
        try {
          pass(true);
        } finally {
          done();
        }
      };
      if (doc.readyState !== "loading") return finish();
      try {
        observer = new env.window.MutationObserver(() => pass(false));
        observer.observe(doc, { childList: true, subtree: true });
        doc.addEventListener("DOMContentLoaded", finish);
        pass(false);
      } catch {
        finish();
      }
    },
  };
}
