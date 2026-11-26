/**
 * The call for the page's changes always fits the server's context limit:
 * 8192 bytes of compact UTF-8 JSON, as backend/app/core/context_limits.py
 * counts it (`compact_size`), or the call is refused with 422. The worst case
 * is built from the script's own rules: every website detail at the longest
 * value the script keeps, and data-attributes at the most the script accepts,
 * in each kind of text that counts differently (one to four bytes a
 * character, escapes, numbers the server writes longer than the browser).
 */
import { readConfig } from '../src/config';
import { BROWSERS, DEVICES, landingPathKept, referrerFrom, utmKept, UTM_PARAMS, VISITORS } from '../src/details';

const MAX_CONTEXT_BYTES = 8192;

/** A number as Python's json writes it after reading the browser's text: the same digits, its exponent form. */
function pythonNumber(n: number): string {
  const js = JSON.stringify(n);
  if (!/[.e]/.test(js)) return js;
  const [mantissa, exponent] = n.toExponential().split('e');
  const e = Number(exponent);
  if (e < -4 || e >= 16) return `${mantissa}e${e < 0 ? '-' : '+'}${String(Math.abs(e)).padStart(2, '0')}`;
  return js;
}

/** `value` as compact JSON the way the server writes it. Strings as JSON.stringify writes them, never shorter. */
function pythonJson(value: unknown): string {
  if (typeof value === 'number') return pythonNumber(value);
  if (Array.isArray(value)) return `[${value.map(pythonJson).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    return `{${Object.entries(value).map(([key, item]) => `${JSON.stringify(key)}:${pythonJson(item)}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

/** The server's count: bytes of the compact JSON in UTF-8. */
const serverBytes = (value: unknown) => Buffer.byteLength(pythonJson(value), 'utf8');

/** The largest n for which keeps(n) holds, n from 1 to `limit` (keeps is true up to some n, then false). */
function largest(keeps: (n: number) => boolean, limit = 8192): number {
  let low = 0;
  let high = limit;
  while (low < high) {
    const mid = Math.ceil((low + high) / 2);
    if (keeps(mid)) low = mid;
    else high = mid - 1;
  }
  return low;
}

const longestOf = (values: readonly string[]) => values.reduce((a, b) => (b.length > a.length ? b : a));

function host(length: number): string {
  const labels: string[] = [];
  let left = length;
  while (left > 0) {
    const size = Math.min(63, left);
    labels.push('a'.repeat(size));
    left -= size + 1;
  }
  return labels.join('.').slice(0, length);
}

/** Every website detail at the longest value the script keeps. */
function longestDetails(): Record<string, string> {
  const utm = largest((n) => utmKept('a'.repeat(n)));
  const referrer = largest((n) => referrerFrom(`https://${host(n)}/`, 'shop.example') === host(n), 2048);
  const landing = largest((n) => landingPathKept('/' + 'a'.repeat(n - 1)));
  const details: Record<string, string> = {
    web_device: longestOf(DEVICES),
    web_browser: longestOf(BROWSERS),
    web_language: 'fil',
    web_visitor: longestOf(VISITORS),
    web_referrer: host(referrer),
  };
  for (const [, key] of UTM_PARAMS) details[key] = 'a'.repeat(utm);
  details.web_landing_path = '/' + 'a'.repeat(landing - 1);
  return details;
}

const accepted = (text: string) =>
  readConfig({ getAttribute: (name: string) => (name === 'data-attributes' ? text : null), hasAttribute: () => false } as unknown as Element).attributes;

const BACKSLASH = String.fromCharCode(92);
/** Kinds of data-attributes text, each n units long. */
const KINDS: [string, (n: number) => string][] = [
  ['one-byte characters', (n) => `{"a":"${'a'.repeat(n)}"}`],
  ['two-byte characters', (n) => `{"a":"${String.fromCharCode(0xe9).repeat(n)}"}`],
  ['three-byte characters', (n) => `{"a":"${String.fromCharCode(0x20ac).repeat(n)}"}`],
  ['four-byte characters', (n) => `{"a":"${String.fromCodePoint(0x1f600).repeat(n)}"}`],
  ['escaped control characters', (n) => `{"a":"${(BACKSLASH + 'u0001').repeat(n)}"}`],
  ['numbers the server writes longer (1e-7)', (n) => `{"a":[${Array(n).fill('1e-7').join(',')}]}`],
  ['short keys', (n) => `{${Array.from({ length: n }, (_, i) => `"k${i}":0`).join(',')}}`],
];

describe('the context of the call for the page', () => {
  test("is counted here as the server counts it", () => {
    // Both measured with backend.app.core.context_limits.compact_size on the browser's JSON.
    expect(serverBytes(JSON.parse(`{"a":[${Array(10).fill('1e-7').join(',')}]}`))).toBe(67);
    const mixed = `{"n":[1e-7,0.00001,1e+21,1.5e-8,123.25,100000000000000000000,-0],"s":"${String.fromCharCode(0x20ac)}${BACKSLASH}u0001","t":true,"z":null}`;
    expect(serverBytes(JSON.parse(mixed))).toBe(98);
  });

  test('fits in 8192 bytes in the worst case: every detail at its longest, data-attributes at its most', () => {
    const details = longestDetails();
    const sizes = KINDS.map(([name, text]) => {
      const n = largest((count) => accepted(text(count)) !== null);
      const attributes = accepted(text(n))!;
      return { name, n, bytes: serverBytes({ ...attributes, ...details }) };
    });
    const worst = sizes.reduce((a, b) => (b.bytes > a.bytes ? b : a));
    // The number this row reports, for the record.
    console.log(`worst case ${worst.bytes} bytes (${worst.name}); details alone ${serverBytes(details)}`);
    expect(`worst case ${worst.bytes.toLocaleString('en-US')} ${worst.bytes > MAX_CONTEXT_BYTES ? '>' : '<='} 8192`).toBe(
      `worst case ${worst.bytes.toLocaleString('en-US')} <= 8192`,
    );
    expect(Object.keys(details)).toHaveLength(9);
  });
});
