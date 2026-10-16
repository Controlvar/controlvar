/**
 * examples/contract_smoke.mjs: what it reads from the environment and what it sends.
 *
 * The SDK Live Contract job runs the script against a real API, with every variable
 * set. Here it runs as `npm run smoke` runs it, on a build of this tree, with the
 * global fetch replaced before it starts (`node --import`): no network, so the
 * defaults it falls back to and the steps it takes are held.
 */
import { execFileSync, spawnSync } from 'child_process';
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

const SDK = join(__dirname, '..');

/** A fetch that answers like the API and writes each request to $FAKE_API_LOG on exit. */
const FAKE_API = `
import { writeFileSync } from 'node:fs';
const seen = [];
process.on('exit', () => writeFileSync(process.env.FAKE_API_LOG, JSON.stringify(seen)));
const reply = (body) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
globalThis.fetch = async (url, init = {}) => {
  const body = typeof init.body === 'string' ? JSON.parse(init.body) : null;
  seen.push({ url: String(url), method: init.method || 'GET', key: (init.headers || {})['X-API-Key'], body });
  const path = new URL(String(url)).pathname;
  if (path === '/api/v1/tracking/assign') {
    return reply({ experiment_key: body.experiment_key, user_id: body.user_id, variant_id: 'v-1',
      variant_name: 'control', is_control: true, configuration: null });
  }
  if (path.startsWith('/api/v1/feature-flags/evaluate/')) {
    return reply({ key: decodeURIComponent(path.split('/').pop()), enabled: true, config: null });
  }
  if (path === '/api/v1/tracking/batch') {
    return reply({ success_count: body.events.length, failure_count: 0, errors: [] });
  }
  return reply({});
};
`;

let work: string;

beforeAll(() => {
  work = mkdtempSync(join(tmpdir(), 'js-sdk-contract-smoke-'));
  cpSync(join(SDK, 'package.json'), join(work, 'package.json'));
  cpSync(join(SDK, 'examples'), join(work, 'examples'), { recursive: true });
  execFileSync(
    process.execPath,
    [require.resolve('typescript/bin/tsc'), '-p', join(SDK, 'tsconfig.json'), '--outDir', join(work, 'dist')],
    { stdio: 'pipe' }
  );
  writeFileSync(join(work, 'fake-api.mjs'), FAKE_API);
}, 120_000);

afterAll(() => {
  rmSync(work, { recursive: true, force: true });
});

interface Seen {
  url: string;
  method: string;
  key: string;
  body: Record<string, any> | null;
}

function runSmoke(env: Record<string, string>) {
  const log = join(work, `requests-${Date.now()}-${Math.random()}.json`);
  const run = spawnSync(
    process.execPath,
    ['--import', join(work, 'fake-api.mjs'), join(work, 'examples', 'contract_smoke.mjs')],
    { env: { PATH: process.env.PATH ?? '', FAKE_API_LOG: log, ...env }, encoding: 'utf8', timeout: 60_000 }
  );
  const seen: Seen[] = JSON.parse(readFileSync(log, 'utf8'));
  return { run, seen };
}

test('it reads its settings from the environment, with these defaults', () => {
  const { run, seen } = runSmoke({ CONTROLVAR_API_KEY: 'smoke-key' });
  expect(run.stderr).toBe('');
  expect(run.status).toBe(0);
  expect(seen.length).toBeGreaterThan(0);
  expect(seen.every(r => r.url.startsWith('http://localhost:8000/api/v1/'))).toBe(true);
  expect(seen.every(r => r.key === 'smoke-key')).toBe(true);
  expect(seen[0].body).toMatchObject({ experiment_key: 'sdk_contract_ab' });
  expect(seen[0].body!.user_id).toMatch(/^smoke-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
  expect(seen.some(r => r.url.includes('/feature-flags/evaluate/sdk_contract_flag?'))).toBe(true);
});

test('it assigns twice, evaluates the flag, tracks with and without a key and sends a 2-event batch', () => {
  const { run, seen } = runSmoke({
    CONTROLVAR_API_URL: 'http://api.test',
    CONTROLVAR_API_KEY: 'k',
    CONTRACT_EXPERIMENT_KEY: 'exp_a',
    CONTRACT_FLAG_KEY: 'flag_a',
    CONTRACT_USER_ID: 'user-a',
  });
  expect(run.status).toBe(0);
  expect(seen.map(r => `${r.method} ${new URL(r.url).pathname}`)).toEqual([
    'POST /api/v1/tracking/assign',
    'POST /api/v1/tracking/assign',
    'GET /api/v1/feature-flags/evaluate/flag_a',
    'POST /api/v1/tracking/track',
    'POST /api/v1/tracking/batch',
    'POST /api/v1/tracking/batch',
  ]);
  expect(seen[3].body).toMatchObject({ event_name: 'purchase', experiment_key: 'exp_a', user_id: 'user-a' });
  expect(seen[4].body!.events.map((e: Record<string, string>) => [e.event_name, e.experiment_key, e.feature_flag_key])).toEqual([
    ['page_view', 'exp_a', undefined],
    ['page_view', undefined, 'flag_a'],
  ]);
  expect(seen[5].body!.events).toHaveLength(2);
  const lines = run.stdout.trim().split('\n');
  expect(lines).toHaveLength(1);
  expect(JSON.parse(lines[0])).toEqual({
    sdk: 'js',
    assign: { variant_name: 'control', is_control: true, sticky: true },
    flag: { enabled: true },
    track: { ok: true },
    fanout: { ok: true },
  });
});

test('it stops without a key and sends nothing', () => {
  const { run, seen } = runSmoke({});
  expect(run.status).toBe(1);
  expect(run.stderr).toContain('CONTROLVAR_API_KEY is required');
  expect(seen).toEqual([]);
});

// Three distinct values per setting, none of them the default or what CI sets.
const NEW_URL = 'http://127.0.0.1:9';
const OLD_URL = 'http://127.0.0.1:7';

test.each([
  ['the former names only', { EXPERIMENTLY_API_URL: OLD_URL, EXPERIMENTLY_API_KEY: 'old-key' }, OLD_URL, 'old-key'],
  ['the new names only', { CONTROLVAR_API_URL: NEW_URL, CONTROLVAR_API_KEY: 'new-key' }, NEW_URL, 'new-key'],
  [
    'both: the new names win',
    { CONTROLVAR_API_URL: NEW_URL, CONTROLVAR_API_KEY: 'new-key', EXPERIMENTLY_API_URL: OLD_URL, EXPERIMENTLY_API_KEY: 'old-key' },
    NEW_URL,
    'new-key',
  ],
  ['no URL: the default', { EXPERIMENTLY_API_KEY: 'old-key' }, 'http://localhost:8000', 'old-key'],
])('%s', (_name, env, url, key) => {
  const { run, seen } = runSmoke(env as Record<string, string>);
  expect(run.status).toBe(0);
  expect(seen.length).toBeGreaterThan(0);
  expect(new Set(seen.map(r => new URL(r.url).origin))).toEqual(new Set([new URL(url).origin]));
  expect(new Set(seen.map(r => r.key))).toEqual(new Set([key]));
});
