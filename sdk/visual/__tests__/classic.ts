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

export function runClassic(
  url: string,
  attributes: Record<string, string> = {},
  setup: (window: Window & typeof globalThis) => void = () => undefined,
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
    fetches.push({ url: String(input), init });
    return new window.Promise<Response>(() => undefined);
  };
  setup(window);
  const script = window.document.createElement('script');
  for (const [name, value] of Object.entries(attributes)) script.setAttribute(name, value);
  script.textContent = readFileSync(CLASSIC, 'utf8');
  window.document.head.appendChild(script);
  return { window, fetches, console: messages, close: () => dom.window.close() };
}
