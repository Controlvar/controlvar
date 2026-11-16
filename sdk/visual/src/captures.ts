/**
 * The browser functions the script keeps from its first synchronous run.
 *
 * Each one is bound here, once, so that calling it later looks nothing up
 * on an object the page can change afterwards (a prototype, a global, a
 * method of `history` or `crypto`). `capture` must be the first thing the
 * classic script does.
 */
export interface Captures {
  replaceState(state: unknown, unused: string, url: string): void;
  getRandomValues(bytes: Uint8Array): Uint8Array;
  /** Called in exactly one place, send.ts (a unit test counts the sites). */
  fetch: (url: string, init: RequestInit) => Promise<Response>;
  setTimeout(callback: () => void, ms: number): unknown;
  addEventListener(type: string, listener: (event: MessageEvent) => void): void;
  contentWindow(frame: HTMLIFrameElement): Window | null;
  messageData(event: MessageEvent): unknown;
  messageOrigin(event: MessageEvent): string;
  messageSource(event: MessageEvent): unknown;
  /** CSSStyleDeclaration's methods, so a page that replaces them cannot keep itself hidden. */
  setProperty(style: CSSStyleDeclaration, name: string, value: string, priority: string): void;
  removeProperty(style: CSSStyleDeclaration, name: string): string;
  getPropertyValue(style: CSSStyleDeclaration, name: string): string;
  getPropertyPriority(style: CSSStyleDeclaration, name: string): string;
}

type Getter<T, R> = (target: T) => R;

export function capture(win: Window & typeof globalThis): Captures {
  const call = win.Function.prototype.call;
  function getter<T, R>(proto: object, name: string): Getter<T, R> {
    const descriptor = win.Object.getOwnPropertyDescriptor(proto, name);
    if (!descriptor || typeof descriptor.get !== "function") {
      throw new Error(`no ${name} getter`);
    }
    return call.bind(descriptor.get) as Getter<T, R>;
  }
  const style = win.CSSStyleDeclaration.prototype;
  return {
    setProperty: call.bind(style.setProperty) as Captures["setProperty"],
    removeProperty: call.bind(style.removeProperty) as Captures["removeProperty"],
    getPropertyValue: call.bind(style.getPropertyValue) as Captures["getPropertyValue"],
    getPropertyPriority: call.bind(style.getPropertyPriority) as Captures["getPropertyPriority"],
    replaceState: win.History.prototype.replaceState.bind(win.history),
    getRandomValues: win.Crypto.prototype.getRandomValues.bind(win.crypto) as Captures["getRandomValues"],
    fetch: win.fetch.bind(win),
    setTimeout: win.setTimeout.bind(win) as Captures["setTimeout"],
    addEventListener: win.EventTarget.prototype.addEventListener.bind(win) as Captures["addEventListener"],
    contentWindow: getter<HTMLIFrameElement, Window | null>(win.HTMLIFrameElement.prototype, "contentWindow"),
    messageData: getter<MessageEvent, unknown>(win.MessageEvent.prototype, "data"),
    messageOrigin: getter<MessageEvent, string>(win.MessageEvent.prototype, "origin"),
    messageSource: getter<MessageEvent, unknown>(win.MessageEvent.prototype, "source"),
  };
}
