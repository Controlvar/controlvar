/**
 * The website details' rules, held to the contract files: the vocabulary
 * (tests/sdk-contract/visitor-attributes.json) equals what the script sends,
 * both ways; the server's own vectors (visual-visitor-vectors.json) pass the
 * script's checks exactly as the server answers them; the script never asks
 * the browser for its high-entropy values; data-attributes is limited in
 * bytes and cannot set a web_ key; and the details go only in the call for
 * the page's changes.
 */
import { readdirSync, readFileSync } from 'fs';
import { join } from 'path';
import { BAD_ATTRIBUTES, MAX_ATTRIBUTES_BYTES, readConfig, utf8Bytes, WEB_ATTRIBUTES } from '../src/config';
import {
  BROWSERS,
  campaignFrom,
  COLLECT_TOKENS,
  DEFAULT_COLLECT,
  DEVICES,
  landingPathKept,
  languageFrom,
  referrerFrom,
  utmKept,
  UTM_PARAMS,
  VISITORS,
} from '../src/details';
import { VISIT_MINUTES } from '../src/visit';
import { runClassic, type Answer, type FetchCall } from './classic';
import { SERVER, sentForAll, VECTORS, VOCABULARY, type Sent } from './vectors';

const ROOT = join(__dirname, '..');

interface Detail {
  key: string;
  collect: string;
  default: boolean;
  label: string;
  values: { value: string; label: string }[] | null;
  example: string;
}
const DETAILS: Detail[] = VOCABULARY.details;

let results = new Map<string, Sent>();
beforeAll(async () => {
  results = await sentForAll();
});

const sorted = (values: Iterable<string>) => [...new Set(values)].sort();
const braces = (values: string[]) => `{${values.join(',')}}`;

describe('the vocabulary', () => {
  test('names every key the script sends, in the order it sends them, and the data-collect values', () => {
    const keys = ['web_device', 'web_browser', 'web_language', 'web_visitor', 'web_referrer', ...UTM_PARAMS.map(([, key]) => key), 'web_landing_path'];
    expect(DETAILS.map((detail) => detail.key)).toEqual(keys);
    expect(VOCABULARY.collect_tokens).toEqual([...COLLECT_TOKENS, 'none']);
    expect(VOCABULARY.collect_default).toEqual([...DEFAULT_COLLECT]);
    expect(DETAILS.filter((detail) => detail.default).map((detail) => detail.collect)).toEqual(
      DETAILS.filter((detail) => DEFAULT_COLLECT.includes(detail.collect as never)).map((detail) => detail.collect),
    );
    expect(VOCABULARY.visit_minutes).toBe(VISIT_MINUTES);
  });

  test("each closed detail's values are the script's, and every one of them is sent by some vector", () => {
    const script: Record<string, readonly string[]> = { web_device: DEVICES, web_browser: BROWSERS, web_visitor: VISITORS };
    const problems: string[] = [];
    for (const detail of DETAILS) {
      const vocabulary = detail.values === null ? null : detail.values.map((value) => value.value);
      if (!(detail.key in script)) {
        if (vocabulary !== null) problems.push(`${detail.key} has values in the vocabulary but is open in the script`);
        continue;
      }
      const sentValues = sorted(VECTORS.map((vector) => results.get(vector.id)!.context[detail.key]).filter((v): v is string => typeof v === 'string'));
      if (vocabulary === null) problems.push(`${detail.key} has no values in the vocabulary`);
      else {
        if (braces(sorted(script[detail.key])) !== braces(sorted(vocabulary))) {
          problems.push(`${detail.key} script values ${braces(sorted(script[detail.key]))} != vocabulary ${braces(sorted(vocabulary))}`);
        }
        if (braces(sentValues) !== braces(sorted(vocabulary))) {
          problems.push(`${detail.key} values ${braces(sentValues)} != vocabulary ${braces(sorted(vocabulary))}`);
        }
      }
    }
    expect(problems).toEqual([]);
  });

  test('every key in the vocabulary is sent by some vector, and the vectors send no other key', () => {
    const sentKeys = sorted(VECTORS.flatMap((vector) => Object.keys(results.get(vector.id)!.context)));
    expect(sentKeys).toEqual(sorted(DETAILS.map((detail) => detail.key)));
  });

  test("each example is a value the script's own checks keep", () => {
    const kept = DETAILS.filter((detail) => !scriptKeeps(detail.key, detail.example)).map((detail) => `${detail.key}: ${detail.example}`);
    expect(kept).toEqual([]);
  });
});

/** Whether the script would send `value` as `key`, by the script's own checks. */
function scriptKeeps(key: string, value: unknown): boolean {
  switch (key) {
    case 'web_device':
      return (DEVICES as readonly unknown[]).includes(value);
    case 'web_browser':
      return (BROWSERS as readonly unknown[]).includes(value);
    case 'web_visitor':
      return (VISITORS as readonly unknown[]).includes(value);
    case 'web_language':
      return typeof value === 'string' && languageFrom(value) === value;
    case 'web_referrer':
      return typeof value === 'string' && referrerFrom(`https://${value}/`, 'shop.example') === value;
    case 'web_utm_source':
    case 'web_utm_medium':
    case 'web_utm_campaign':
      return utmKept(value);
    case 'web_landing_path':
      return landingPathKept(value);
    default:
      return false;
  }
}

describe("the server's vectors, through the script's checks", () => {
  test("every detail vector: the script keeps exactly what the server keeps (60, exact)", () => {
    expect(SERVER.details).toHaveLength(60);
    const disagree = SERVER.details
      .filter((vector: { key: string; value: unknown; kept: boolean }) => scriptKeeps(vector.key, vector.value) !== vector.kept)
      .map((vector: { id: string; kept: boolean }) => `${vector.id} ${vector.kept ? 'dropped' : 'kept'}`);
    expect(disagree).toEqual([]);
  });

  test('every campaign query: the script reads the same value and keeps it exactly when the server does (10, exact)', () => {
    expect(SERVER.utm_queries).toHaveLength(10);
    const disagree: string[] = [];
    for (const query of SERVER.utm_queries as { id: string; query: string; param: string; value: string; kept: boolean }[]) {
      const read = new URLSearchParams(`?${query.query}`).get(query.param);
      if (read !== query.value) disagree.push(`${query.id}: read ${JSON.stringify(read)} != ${JSON.stringify(query.value)}`);
      const value = campaignFrom(`?${query.query}`)[query.param as 'utm_source'];
      const expected = query.kept ? query.value : undefined;
      if (value !== expected) {
        disagree.push(`${query.id}: ${value === undefined ? 'absent' : `'${value.slice(0, 12)}...'`} != ${query.kept ? 'kept' : 'absent'}`);
      }
    }
    expect(disagree).toEqual([]);
  });
});

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? sourceFiles(join(dir, entry.name)) : [join(dir, entry.name)],
  );
}

describe("the browser's high-entropy values", () => {
  const count = (files: string[]) => files.reduce((total, file) => total + (readFileSync(file, 'utf8').match(/getHighEntropyValues/g) || []).length, 0);

  test('the sources and the built package never ask for them', () => {
    const src = sourceFiles(join(ROOT, 'src'));
    const dist = sourceFiles(join(ROOT, 'dist'));
    expect(dist.map((file) => file.slice(file.lastIndexOf('/') + 1)).sort()).toEqual(['controlvar-visual.js', 'index.mjs']);
    expect(`getHighEntropyValues ${count(src)} != 0`).toBe('getHighEntropyValues 0 != 0');
    expect(`getHighEntropyValues ${count(dist)} != 0`).toBe('getHighEntropyValues 0 != 0');
  });

  test('the count finds a planted call', () => {
    expect('navigator.userAgentData.getHighEntropyValues(["model"])'.match(/getHighEntropyValues/g)).toHaveLength(1);
  });
});

function tag(attributes: Record<string, string>): Element {
  return {
    getAttribute: (name: string) => (name in attributes ? attributes[name] : null),
    hasAttribute: (name: string) => name in attributes,
  } as unknown as Element;
}

describe('data-attributes', () => {
  const json = (value: string) => `{"a":"${value}"}`;

  test('is limited to 4096 UTF-8 bytes, so text in another script counts every byte', () => {
    expect(MAX_ATTRIBUTES_BYTES).toBe(4096);
    const ascii = json('a'.repeat(4096 - 8));
    expect(utf8Bytes(ascii)).toBe(4096);
    expect(readConfig(tag({ 'data-attributes': ascii })).attributes).not.toBeNull();
    expect(readConfig(tag({ 'data-attributes': json('a'.repeat(4096 - 7)) })).attributes).toBeNull();
    // 1366 characters of three bytes each: 4098 bytes, though only 1374 characters.
    const threeByte = json(String.fromCharCode(0x20ac).repeat(1366));
    expect(threeByte.length).toBeLessThan(4096);
    const refused = readConfig(tag({ 'data-attributes': threeByte }));
    expect(refused.attributes).toBeNull();
    expect(refused.warnings).toEqual([BAD_ATTRIBUTES]);
  });

  test('is also limited as sent: a short text that is larger as JSON is refused', () => {
    const text = `{"a":[${Array(800).fill('1e20').join(',')}]}`;
    expect(utf8Bytes(text)).toBeLessThan(4096);
    expect(utf8Bytes(JSON.stringify(JSON.parse(text)))).toBeGreaterThan(4096);
    expect(readConfig(tag({ 'data-attributes': text }))).toMatchObject({ attributes: null, warnings: [BAD_ATTRIBUTES] });
  });

  test('cannot set a web_ key: those are dropped, with one warning for the tag', () => {
    const config = readConfig(tag({ 'data-attributes': '{"plan":"pro","web_device":"tablet","web_country":"FR","Web_x":1}' }));
    expect(config.attributes).toEqual({ plan: 'pro', Web_x: 1 });
    expect(config.warnings).toEqual([WEB_ATTRIBUTES]);
  });

  test("utf8Bytes counts as the server counts: 1 to 4 bytes a character, 3 for a lone surrogate", () => {
    expect([utf8Bytes('a'), utf8Bytes(String.fromCharCode(0xe9)), utf8Bytes(String.fromCharCode(0x20ac)), utf8Bytes(String.fromCodePoint(0x1f600)), utf8Bytes(String.fromCharCode(0xd800))]).toEqual([1, 2, 3, 4, 3]);
  });
});

const TAG = { 'data-api-url': 'https://cv.example', 'data-api-key': 'test-website-key' };
const settle = () => new Promise((resolve) => setTimeout(resolve, 10));
/** A change the script refuses at once, so a report is sent straight away. */
const CHANGE = { version: 1, changes: [{ op: 'no_such_op', selector: '#t' }] };

function answering(call: FetchCall): Answer | undefined {
  const path = new URL(call.url).pathname;
  if (path === '/api/v1/tracking/visual') {
    const experiments = [{ experiment_key: 'headline', variant_id: 'v1', variant_name: 'B', assigned: true, reason: 'assigned', changes: CHANGE, goals: [] }];
    return { status: 200, body: { experiments, pages: { host: 'shop.example', rules: [{ match: 'prefix', path: '/' }], max_age: 60 } } };
  }
  return { status: 201, body: {} };
}

describe('where the details go', () => {
  test("only in the call for the page's changes, after data-attributes; never in track()'s events or a report", async () => {
    const page = runClassic(
      'https://shop.example/a?utm_source=news',
      { ...TAG, 'data-attributes': '{"plan":"pro","web_device":"tablet"}' },
      (window) => {
        window.document.body.innerHTML = '<h1 id="t">Original</h1>';
        Object.defineProperty(window.document, 'referrer', { configurable: true, get: () => 'https://www.google.com/search?q=cv-secret-7731' });
      },
      answering,
    );
    await settle();
    (page.window as unknown as { controlvar: { visual: { track(e: string): void } } }).controlvar.visual.track('buy');
    await settle();
    const byPath = (path: string) => page.fetches.filter((call) => new URL(call.url).pathname === path).map((call) => JSON.parse(String(call.init.body)));
    const [ask] = byPath('/api/v1/tracking/visual');
    expect(Object.keys(ask.context)).toEqual(['plan', 'web_device', 'web_browser', 'web_language', 'web_visitor', 'web_referrer', 'web_utm_source']);
    expect(ask.context).toMatchObject({ plan: 'pro', web_device: 'desktop', web_referrer: 'www.google.com', web_utm_source: 'news' });
    const others = [...byPath('/api/v1/tracking/batch'), ...byPath('/api/v1/tracking/errors')];
    expect(others.length).toBeGreaterThanOrEqual(2);
    expect(JSON.stringify(others)).not.toMatch(/web_|news|google|cv-secret/);
    expect(JSON.stringify(ask)).not.toContain('cv-secret-7731');
    expect(page.console.filter((line) => line.startsWith('warn:'))).toEqual([`warn: ${WEB_ATTRIBUTES}`]);
    page.close();
  });
});
