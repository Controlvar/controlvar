/**
 * The editor link in the built classic script, in jsdom: the fragment, the
 * claim request and the panel frame. The browser tests (frontend/tests/e2e/
 * runtime) cover the hand-over and what later scripts can read.
 */
import { randomBytes } from 'crypto';
import { base64url } from '../src/base64url';
import { runClassic } from './classic';

const TAG = {
  'data-api-url': 'https://cv.example/',
  'data-api-key': 'test-website-key',
  'data-editor-origin': 'https://dash.example',
};
const PAGE = 'https://shop.example/a/b?x=1';

const ticket = () => randomBytes(32).toString('base64url');

describe('base64url', () => {
  test('matches Node for every length up to 40 bytes, without padding', () => {
    for (let length = 0; length <= 40; length += 1) {
      const bytes = randomBytes(length);
      expect(base64url(new Uint8Array(bytes))).toBe(bytes.toString('base64url'));
    }
  });

  test('32 bytes are 43 characters', () => {
    expect(base64url(new Uint8Array(32))).toMatch(/^[A-Za-z0-9_-]{43}$/);
  });
});

describe('an exact editor link', () => {
  test('is removed from the address and claimed with one request that carries no cookie, no Referer and no key', () => {
    const value = ticket();
    const page = runClassic(`${PAGE}#controlvar-editor=${value}`, TAG);
    expect(page.window.location.href).toBe(PAGE);
    expect(page.fetches.length).toBe(1);
    const [{ url, init }] = page.fetches;
    expect(url).toBe('https://cv.example/api/v1/visual-editor/tickets/claim');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('omit');
    expect(init.referrerPolicy).toBe('no-referrer');
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' });
    const body = JSON.parse(String(init.body));
    expect(Object.keys(body).sort()).toEqual(['claim', 'ticket']);
    expect(body.ticket).toBe(value);
    expect(body.claim).toMatch(/^[A-Za-z0-9_-]{43}$/);
    page.close();
  });

  test('opens the panel in a frame with no Referer and no ticket in its address', () => {
    const value = ticket();
    const page = runClassic(`${PAGE}#controlvar-editor=${value}`, TAG);
    const frames = page.window.document.querySelectorAll('iframe');
    expect(frames.length).toBe(1);
    expect(String(frames[0].getAttribute('referrerpolicy'))).toBe('no-referrer');
    expect(frames[0].getAttribute('src')).toBe('https://dash.example/visual-editor/panel');
    expect(page.window.document.documentElement.outerHTML).not.toContain(value);
    expect(page.window.sessionStorage.getItem('controlvar_editor_open')).toBe('1');
    page.close();
  });

  test('without data-editor-origin: removed and claimed, no frame, one console line', () => {
    const page = runClassic(`${PAGE}#controlvar-editor=${ticket()}`, {
      'data-api-url': TAG['data-api-url'],
      'data-api-key': TAG['data-api-key'],
    });
    expect(page.window.location.href).toBe(PAGE);
    expect(page.fetches.length).toBe(1);
    expect(page.window.document.querySelectorAll('iframe').length).toBe(0);
    expect(page.console).toEqual([
      'warn: Controlvar: this page was opened with a visual editor link, but the script tag has no data-editor-origin, so the editor stays closed.',
    ]);
    page.close();
  });

  test('a data-editor-origin that is not an origin opens no frame', () => {
    for (const origin of ['https://dash.example/', 'https://dash.example/panel', 'javascript:alert(1)', 'dash.example']) {
      const page = runClassic(`${PAGE}#controlvar-editor=${ticket()}`, { ...TAG, 'data-editor-origin': origin });
      expect(page.window.document.querySelectorAll('iframe').length).toBe(0);
      page.close();
    }
  });
});

describe('any other fragment', () => {
  test.each([
    ['42 characters', (value: string) => value.slice(0, 42)],
    ['44 characters', (value: string) => `${value}A`],
    ['a +', (value: string) => `${value.slice(0, 42)}+`],
    ['padding', (value: string) => `${value.slice(0, 42)}=`],
    ['a suffix', (value: string) => `${value}&x=1`],
    ['another key', (value: string) => value.replace(/^/, 'x')],
  ])('is left alone: %s', (_name, make) => {
    const url = `${PAGE}#controlvar-editor=${make(ticket())}`;
    const page = runClassic(url, TAG);
    expect(page.window.location.href).toBe(url);
    // An ordinary page view: the only request is the one for the page's changes.
    expect(page.fetches.map((call) => new URL(call.url).pathname)).toEqual(['/api/v1/tracking/visual']);
    expect(page.window.document.querySelectorAll('iframe').length).toBe(0);
    page.close();
  });
});
