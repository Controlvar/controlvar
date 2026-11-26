/**
 * The website-detail vectors and a way to run one: the built classic script
 * in a jsdom page that gives it the vector's browser (user agent,
 * userAgentData, touch points, language, referrer, address, data-collect and
 * any seeded localStorage). Shared by the tests that read the details.
 */
import { readFileSync } from 'fs';
import { join } from 'path';
import { runClassic, type Answer, type FetchCall } from './classic';

const CONTRACT = join(__dirname, '..', '..', '..', 'tests', 'sdk-contract');
/** tests/sdk-contract/visual-script-vectors.json: the script's answers, written by hand. */
export const SCRIPT = JSON.parse(readFileSync(join(CONTRACT, 'visual-script-vectors.json'), 'utf8'));
/** tests/sdk-contract/visual-visitor-vectors.json: the server's answers, generated from the server. */
export const SERVER = JSON.parse(readFileSync(join(CONTRACT, 'visual-visitor-vectors.json'), 'utf8'));
/** tests/sdk-contract/visitor-attributes.json: the details' vocabulary. */
export const VOCABULARY = JSON.parse(readFileSync(join(CONTRACT, 'visitor-attributes.json'), 'utf8'));

export interface ScriptVector {
  id: string;
  name: string;
  user_agent: string;
  mobile: boolean | null;
  max_touch_points: number;
  language: string | null;
  referrer: string;
  url: string;
  collect: string | null;
  local_storage?: Record<string, string>;
  context: Record<string, string>;
  warnings?: number;
  writes?: string[];
}

export const CORPORA = ['user_agents', 'languages', 'referrers', 'pages', 'collects'] as const;
export const VECTORS: ScriptVector[] = CORPORA.flatMap((corpus) => SCRIPT[corpus] as ScriptVector[]);

const TAG = { 'data-api-url': 'https://cv.example', 'data-api-key': 'test-website-key' };

function answer(call: FetchCall): Answer | undefined {
  if (new URL(call.url).pathname === '/api/v1/tracking/visual') {
    return { status: 200, body: { experiments: [], pages: { host: SCRIPT.page_host, rules: [{ match: 'prefix', path: '/' }], max_age: 60 } } };
  }
  return { status: 200, body: {} };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 10));

/** Gives the page the vector's browser. */
export function browserLike(vector: ScriptVector) {
  return (window: Window & typeof globalThis) => {
    const nav = window.navigator;
    Object.defineProperty(nav, 'userAgent', { configurable: true, get: () => vector.user_agent });
    Object.defineProperty(nav, 'maxTouchPoints', { configurable: true, get: () => vector.max_touch_points });
    Object.defineProperty(nav, 'language', { configurable: true, get: () => (vector.language === null ? undefined : vector.language) });
    if (vector.mobile !== null) {
      const mobile = vector.mobile;
      Object.defineProperty(nav, 'userAgentData', { configurable: true, get: () => ({ mobile, brands: [], platform: '' }) });
    }
    Object.defineProperty(window.document, 'referrer', { configurable: true, get: () => vector.referrer });
    for (const [key, value] of Object.entries(vector.local_storage || {})) window.localStorage.setItem(key, value);
  };
}

export interface Sent {
  /** How many calls for the page's changes it made. */
  calls: number;
  /** The context of that call ({} when it has none). */
  context: Record<string, unknown>;
  /** data-collect console warnings. */
  warnings: number;
  /** The visit keys in localStorage afterwards, sorted. */
  writes: string[];
}

/** Runs the vector's page in the built classic script. */
export async function sent(vector: ScriptVector, extraTag: Record<string, string> = {}): Promise<Sent> {
  const tag: Record<string, string> = { ...TAG, ...extraTag };
  if (vector.collect !== null) tag['data-collect'] = vector.collect;
  const page = runClassic(vector.url, tag, browserLike(vector), answer);
  await settle();
  const calls = page.fetches.filter((call) => new URL(call.url).pathname === '/api/v1/tracking/visual');
  const body = calls.length === 1 ? JSON.parse(String(calls[0].init.body)) : null;
  const warnings = page.console.filter((line) => line.startsWith('warn: Controlvar: data-collect')).length;
  const writes = ['controlvar_seen', 'controlvar_visit'].filter((key) => page.window.localStorage.getItem(key) !== null);
  page.close();
  return { calls: calls.length, context: body && body.context ? body.context : {}, warnings, writes };
}

/** Every vector run once, by id. */
export async function sentForAll(): Promise<Map<string, Sent>> {
  const results = new Map<string, Sent>();
  for (const vector of VECTORS) results.set(vector.id, await sent(vector));
  return results;
}
