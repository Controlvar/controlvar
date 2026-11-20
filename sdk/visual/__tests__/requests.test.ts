/**
 * Every request the script makes goes through one fetch call that sends no
 * cookies and no Referer, and nothing else can send: no sendBeacon, no
 * XMLHttpRequest, no image request. Read from the sources, so a second way
 * to send fails here before any browser test runs.
 */
import { readdirSync, readFileSync } from 'fs';
import { join } from 'path';
import { createSend } from '../src/send';
import type { Captures } from '../src/captures';
import type { Config } from '../src/config';

const SRC = join(__dirname, '..', 'src');

function sources(dir: string): { name: string; text: string }[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory()
      ? sources(join(dir, entry.name))
      : entry.name.endsWith('.ts')
        ? [{ name: entry.name, text: readFileSync(join(dir, entry.name), 'utf8') }]
        : [],
  );
}

/** Each place a pattern occurs, as "file: the line". */
function sites(pattern: RegExp): string[] {
  return sources(SRC).flatMap(({ name, text }) =>
    text
      .split('\n')
      .filter((line) => new RegExp(pattern.source, pattern.flags).test(line))
      .map((line) => `${name}: ${line.trim()}`),
  );
}

describe('the sources', () => {
  test('call fetch in exactly one place, which sets credentials omit and referrerPolicy no-referrer', () => {
    const calls = sites(/fetch\s*\(/i);
    expect(calls).toEqual(['send.ts: return captures.fetch(`${config.apiUrl}${path}`, {']);
    const send = readFileSync(join(SRC, 'send.ts'), 'utf8');
    const init = send.slice(send.indexOf('captures.fetch('), send.indexOf('});', send.indexOf('captures.fetch(')));
    expect(init).toContain('credentials: "omit",');
    expect(init).toContain('referrerPolicy: "no-referrer",');
  });

  test.each([['sendBeacon', /sendBeacon/], ['XMLHttpRequest', /XMLHttpRequest/], ['new Image', /new\s+Image\b/]])(
    'never use %s',
    (_name, pattern) => {
      expect(sites(pattern)).toEqual([]);
    },
  );

  test('the patterns find a planted offender', () => {
    expect(['navigator.sendBeacon(u)', 'new XMLHttpRequest()', 'new  Image()', 'window.fetch (x)'].map((line) =>
      [/sendBeacon/, /XMLHttpRequest/, /new\s+Image\b/, /fetch\s*\(/i].some((pattern) => pattern.test(line)),
    )).toEqual([true, true, true, true]);
  });
});

describe('the send function', () => {
  const calls: string[] = [];
  const captures = { fetch: (url: string) => (calls.push(url), Promise.resolve({} as Response)) } as unknown as Captures;
  const config: Config = {
    apiUrl: 'https://cv.example',
    apiKey: 'k',
    editorOrigin: null,
    requireConsent: true,
    timeoutMs: 1000,
    userId: null,
    cookieName: 'controlvar_uid',
    attributes: null,
    warnings: [],
  };
  const storage = (backoff: string | null) => ({ getItem: () => backoff }) as unknown as Storage;

  beforeEach(() => {
    calls.length = 0;
  });

  test('sends nothing before consent, except the editor link', () => {
    const send = createSend(captures, config, { consented: () => false }, () => storage(null), () => 0);
    expect(send('/x', {})).toBeNull();
    expect(send('/claim', {}, { editorLink: true })).not.toBeNull();
    expect(calls).toEqual(['https://cv.example/claim']);
  });

  test('sends nothing while the backoff key is in the future, except the editor link', () => {
    const send = createSend(captures, config, { consented: () => true }, () => storage('2000'), () => 1000);
    expect(send('/x', {})).toBeNull();
    expect(send('/claim', {}, { editorLink: true })).not.toBeNull();
    const after = createSend(captures, config, { consented: () => true }, () => storage('2000'), () => 3000);
    expect(after('/y', {})).not.toBeNull();
    expect(calls).toEqual(['https://cv.example/claim', 'https://cv.example/y']);
  });

  test('sends nothing at all without the API URL or key, the editor link included', () => {
    for (const missing of [{ apiUrl: null }, { apiKey: null }]) {
      const send = createSend(captures, { ...config, ...missing }, { consented: () => true }, () => null, () => 0);
      expect(send('/claim', {}, { editorLink: true })).toBeNull();
    }
    expect(calls).toEqual([]);
  });
});
