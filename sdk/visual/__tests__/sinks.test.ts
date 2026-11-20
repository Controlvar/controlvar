/**
 * No HTML or code sink: neither the sources nor the built classic script
 * (what npm pack ships, and what a site loads) write HTML, run a string as
 * code, or create a document fragment from markup. Each pattern is first run
 * against a planted offender, so a pattern that matches nothing fails here
 * instead of passing for ever.
 */
import { readdirSync, readFileSync } from 'fs';
import { join } from 'path';
import { CLASSIC } from './classic';

const SRC = join(__dirname, '..', 'src');

/** In the sources: the names of every HTML sink. */
const SOURCE_SINKS: [string, RegExp][] = [
  ['innerHTML', /innerHTML/],
  ['outerHTML', /outerHTML/],
  ['insertAdjacentHTML', /insertAdjacentHTML/],
  ['document.write', /document\s*\.\s*write/],
  ['srcdoc', /srcdoc/i],
  ['createContextualFragment', /createContextualFragment/],
];

/** In the built bundle: the HTML sinks, and every way to run a string as code. */
const BUNDLE_SINKS: [string, RegExp][] = [
  ['innerHTML', /innerHTML/],
  ['outerHTML', /outerHTML/],
  ['insertAdjacentHTML', /insertAdjacentHTML/],
  ['document.write', /\.write(ln)?\s*\(/],
  ['eval(', /\beval\s*\(/],
  ['Function(', /\bFunction\s*\(/],
  ['string setTimeout', /setTimeout\s*\(\s*["'`]/],
];

const OFFENDERS: Record<string, string> = {
  innerHTML: 'el.innerHTML = x',
  outerHTML: 'el.outerHTML = x',
  insertAdjacentHTML: 'el.insertAdjacentHTML("beforeend", x)',
  'document.write': 'document.write(x)',
  srcdoc: 'frame.srcdoc = x',
  createContextualFragment: 'range.createContextualFragment(x)',
  'eval(': 'eval(x)',
  'Function(': 'new Function("return this")',
  'string setTimeout': 'setTimeout("x()", 1)',
};

function sources(dir: string): { name: string; text: string }[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory()
      ? sources(join(dir, entry.name))
      : entry.name.endsWith('.ts')
        ? [{ name: entry.name, text: readFileSync(join(dir, entry.name), 'utf8') }]
        : [],
  );
}

describe('the patterns', () => {
  test.each([...SOURCE_SINKS, ...BUNDLE_SINKS])('%s finds its planted offender', (name, pattern) => {
    expect(pattern.test(OFFENDERS[name])).toBe(true);
  });

  test('a double-escaped pattern would be caught: it matches nothing', () => {
    const doubled = new RegExp(String.raw`document\\s*\\.\\s*write`);
    expect(doubled.test(OFFENDERS['document.write'])).toBe(false);
    expect(SOURCE_SINKS.every(([, pattern]) => !pattern.source.includes('\\\\'))).toBe(true);
  });
});

describe('the sources', () => {
  test.each(SOURCE_SINKS)('never use %s', (_name, pattern) => {
    const hits = sources(SRC).flatMap(({ name, text }) =>
      text.split('\n').filter((line) => pattern.test(line)).map((line) => `${name}: ${line.trim()}`),
    );
    expect(hits).toEqual([]);
  });
});

describe('the built classic script', () => {
  test.each(BUNDLE_SINKS)('never uses %s', (_name, pattern) => {
    const hits = readFileSync(CLASSIC, 'utf8')
      .split('\n')
      .filter((line) => pattern.test(line))
      .map((line) => line.trim());
    expect(hits).toEqual([]);
  });
});
