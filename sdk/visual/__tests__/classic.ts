/**
 * Runs the built classic script in a jsdom page, from a <script> element with
 * the given attributes, as a site's tag would. jsdom has no fetch, so each
 * page gets a recording stub.
 */
import { readFileSync } from 'fs';
import { join } from 'path';
import { JSDOM, VirtualConsole } from 'jsdom';

export const CLASSIC = join(__dirname, '..', 'dist', 'controlvar-visual.js');

export interface FetchCall {
  url: string;
  init: RequestInit;
}

export interface ClassicPage {
  window: Window & typeof globalThis;
  fetches: FetchCall[];
  console: string[];
  close(): void;
}

/** A stand-in for fetch's Response: what the script reads of one. */
export interface Answer {
  status: number;
  body?: unknown;
}

/** Answers each request; undefined leaves it pending for ever. */
export type Responder = (call: FetchCall) => Answer | undefined;

export function runClassic(
  url: string,
  attributes: Record<string, string> = {},
  setup: (window: Window & typeof globalThis) => void = () => undefined,
  respond: Responder = () => undefined,
): ClassicPage {
  const messages: string[] = [];
  const virtualConsole = new VirtualConsole();
  for (const level of ['log', 'warn', 'error'] as const) {
    virtualConsole.on(level, (...args: unknown[]) => messages.push(`${level}: ${args.map(String).join(' ')}`));
  }
  virtualConsole.on('jsdomError', (error: Error) => messages.push(`jsdomError: ${error.message}`));
  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>', {
    url,
    runScripts: 'dangerously',
    virtualConsole,
  });
  const window = dom.window as unknown as Window & typeof globalThis;
  const fetches: FetchCall[] = [];
  (window as unknown as { fetch: unknown }).fetch = (input: string, init: RequestInit) => {
    const call = { url: String(input), init };
    fetches.push(call);
    const answer = respond(call);
    if (answer === undefined) return new window.Promise<Response>(() => undefined);
    const text = JSON.stringify(answer.body ?? null);
    return window.Promise.resolve({
      ok: answer.status >= 200 && answer.status < 300,
      status: answer.status,
      json: () => window.Promise.resolve(window.JSON.parse(text)),
    });
  };
  setup(window);
  const script = window.document.createElement('script');
  for (const [name, value] of Object.entries(attributes)) script.setAttribute(name, value);
  script.textContent = readFileSync(CLASSIC, 'utf8');
  window.document.head.appendChild(script);
  return { window, fetches, console: messages, close: () => dom.window.close() };
}
