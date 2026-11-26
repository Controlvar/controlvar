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
const links = new Set();
process.on('exit', () => writeFileSync(process.env.FAKE_API_LOG, JSON.stringify(seen)));
const reply = (body) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
globalThis.fetch = async (url, init = {}) => {
  const body = typeof init.body === 'string' ? JSON.parse(init.body) : null;
  seen.push({ url: String(url), method: init.method || 'GET', key: (init.headers || {})['X-API-Key'], body });
  const path = new URL(String(url)).pathname;
  if (path === '/api/v1/tracking/alias') {
    // #449: created for a new link, not for one already stored.
    const link = JSON.stringify([body.anonymous_id, body.user_id]);
    const created = !links.has(link);
    links.add(link);
    return reply({ anonymous_id: body.anonymous_id, user_id: body.user_id, created });
  }
  if (path === '/api/v1/tracking/assign' && process.env.FAKE_LINK_IGNORED && body.user_id.startsWith('smoke-known-')) {
    // A server that does not follow the link: the known id gets a variant of its own.
    return reply({ experiment_key: body.experiment_key, user_id: body.user_id, variant_id: 'v-2',
      variant_name: 'treatment', is_control: false, configuration: null, assigned: true, reason: 'assigned' });
  }
  if (path === '/api/v1/tracking/assign') {
    // The server answers an enrolled user with assigned: true; FAKE_NO_ASSIGNED drops the field.
    const enrolled = process.env.FAKE_NO_ASSIGNED ? {} : { assigned: true, reason: 'assigned' };
    return reply({ experiment_key: body.experiment_key, user_id: body.user_id, variant_id: 'v-1',
      variant_name: 'control', is_control: true, configuration: null, ...enrolled });
  }
  if (path.startsWith('/api/v1/feature-flags/evaluate/')) {
    const key = decodeURIComponent(path.split('/').pop());
    // The seed's value flags, on at 100% (#1810); FAKE_NO_VALUES answers as a server before flag values.
    const values = {
      sdk_contract_value_string: ['string', 'on-copy'],
      sdk_contract_value_number: ['number', 10],
      sdk_contract_value_json: ['json', { color: 'blue' }],
    };
    if (key in values) {
      const [value_type, value] = values[key];
      const served = process.env.FAKE_NO_VALUES ? {} : { value, value_type };
      return reply({ key, enabled: true, ...served, config: null, reason: 'rollout' });
    }
    // The seed's targeted flag: 0%, on only through its rule plan equals pro (#1520).
    if (key === (process.env.CONTRACT_TARGETED_FLAG_KEY || 'sdk_contract_targeted')) {
      const context = new URL(String(url)).searchParams.get('context');
      const pro = context !== null && JSON.parse(context).plan === 'pro';
      return reply({ key, enabled: pro, config: null, reason: pro ? 'targeting_rule' : 'rollout' });
    }
    // The seed's multi-way flags (#2012): a split, here each user's value by name; and a rule,
    // plan equals pro, that serves a value of its own. FAKE_NO_VALUES drops their values too.
    if (key === 'sdk_contract_multiway') {
      const user = new URL(String(url)).searchParams.get('user_id');
      const served = process.env.FAKE_NO_VALUES ? {} : { value: 'value-of-' + user, value_type: 'string' };
      return reply({ key, enabled: true, ...served, config: null, reason: 'rollout' });
    }
    if (key === 'sdk_contract_rule_value') {
      const context = new URL(String(url)).searchParams.get('context');
      const pro = context !== null && JSON.parse(context).plan === 'pro';
      const value = pro ? 'pro-copy' : 'off-copy';
      const served = process.env.FAKE_NO_VALUES ? {} : { value, value_type: 'string' };
      return reply({ key, enabled: pro, ...served, config: null, reason: pro ? 'targeting_rule' : 'rollout' });
    }
    return reply({ key, enabled: true, config: null });
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
  expect(seen.some(r => r.url.includes('/feature-flags/evaluate/sdk_contract_targeted?'))).toBe(true);
});

test('it assigns twice, evaluates the flag, tracks with and without a key and sends a 2-event batch', () => {
  const { run, seen } = runSmoke({
    CONTROLVAR_API_URL: 'http://api.test',
    CONTROLVAR_API_KEY: 'k',
    CONTRACT_EXPERIMENT_KEY: 'exp_a',
    CONTRACT_FLAG_KEY: 'flag_a',
    CONTRACT_TARGETED_FLAG_KEY: 'targeted_a',
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
    // #1543 (C4): one keyed event sent twice with one eventId.
    'POST /api/v1/tracking/track',
    'POST /api/v1/tracking/track',
    // #1520: the targeted flag on a new client, with plan=pro, then after clearCache() plan=free.
    'GET /api/v1/feature-flags/evaluate/targeted_a',
    'GET /api/v1/feature-flags/evaluate/targeted_a',
    // The exposure hook's new client: two assigns, the second from its cache.
    'POST /api/v1/tracking/assign',
    // The client whose hook rejects.
    'POST /api/v1/tracking/assign',
    // #1810: the seed's three value flags, read with the typed getters on a new client.
    'GET /api/v1/feature-flags/evaluate/sdk_contract_value_string',
    'GET /api/v1/feature-flags/evaluate/sdk_contract_value_number',
    'GET /api/v1/feature-flags/evaluate/sdk_contract_value_json',
    // #2012: no CONTRACT_MULTIWAY_USERS, so no user of the split; the rule value flag with plan=pro.
    'GET /api/v1/feature-flags/evaluate/sdk_contract_rule_value',
  ]);
  expect(seen.slice(12).map(r => new URL(r.url).searchParams.get('user_id'))).toEqual([
    'user-a',
    'user-a',
    'user-a',
    'user-a',
  ]);
  expect(seen.slice(8, 10).map(r => r.url)).toEqual([
    'http://api.test/api/v1/feature-flags/evaluate/targeted_a?user_id=user-a&context=%7B%22plan%22%3A%22pro%22%7D',
    'http://api.test/api/v1/feature-flags/evaluate/targeted_a?user_id=user-a&context=%7B%22plan%22%3A%22free%22%7D',
  ]);
  expect(seen[3].body).toMatchObject({ event_name: 'purchase', experiment_key: 'exp_a', user_id: 'user-a' });
  expect(seen[4].body!.events.map((e: Record<string, string>) => [e.event_name, e.experiment_key, e.feature_flag_key])).toEqual([
    ['page_view', 'exp_a', undefined],
    ['page_view', undefined, 'flag_a'],
  ]);
  expect(seen[5].body!.events).toHaveLength(2);
  // C4: both sends carry the same event_id, the report names it, and the event name is fresh.
  const dedup = JSON.parse(run.stdout.trim()).event_id;
  expect(dedup).toEqual({
    user_id: 'user-a',
    event_name: expect.stringMatching(/^contract_dedup_[0-9a-f-]{36}$/),
    id: expect.stringMatching(/^contract-[0-9a-f-]{36}$/),
    sends: 2,
  });
  expect(seen.slice(6, 8).map(r => r.body)).toEqual([
    { event_type: dedup.event_name, event_name: dedup.event_name, user_id: 'user-a', experiment_key: 'exp_a', event_id: dedup.id },
    { event_type: dedup.event_name, event_name: dedup.event_name, user_id: 'user-a', experiment_key: 'exp_a', event_id: dedup.id },
  ]);
  const lines = run.stdout.trim().split('\n');
  expect(lines).toHaveLength(1);
  expect(JSON.parse(lines[0])).toEqual({
    sdk: 'js',
    assign: { variant_name: 'control', is_control: true, sticky: true },
    flag: { enabled: true },
    track: { ok: true },
    fanout: { ok: true },
    targeted: { pro: true, free: false, reason: 'targeting_rule' },
    event_id: dedup,
    exposure: {
      calls: 1,
      payload: { experiment_key: 'exp_a', variant_name: 'control', variant_id: 'v-1', user_id: 'user-a' },
    },
    values: { string: 'on-copy', number: 10, json: { color: 'blue' } },
    multiway: { users: {}, rule: 'pro-copy' },
  });
  expect(seen.slice(10, 12).map(r => r.body)).toEqual([
    { experiment_key: 'exp_a', user_id: 'user-a' },
    { experiment_key: 'exp_a', user_id: 'user-a' },
  ]);
});

test('without assigned in the answer no hook is called, and it fails on the rejecting hook', () => {
  const { run, seen } = runSmoke({ CONTROLVAR_API_KEY: 'k', CONTRACT_USER_ID: 'user-a', FAKE_NO_ASSIGNED: '1' });
  expect(run.status).toBe(1);
  expect(run.stdout).toBe('');
  expect(run.stderr).toBe(
    'js contract smoke failed: the rejecting hook was reported as [], expected ["exposure/HOOK_ERROR"]\n'
  );
  expect(seen.filter(r => new URL(r.url).pathname === '/api/v1/tracking/assign')).toHaveLength(4);
});

test('it reads the multi-way flag for each user it is given, and the rule value flag with plan=pro', () => {
  const { run, seen } = runSmoke({
    CONTROLVAR_API_URL: 'http://api.test',
    CONTROLVAR_API_KEY: 'k',
    CONTRACT_USER_ID: 'user-a',
    CONTRACT_MULTIWAY_USERS: 'mw-a,mw-b,mw-c',
  });
  expect(run.stderr).toBe('');
  expect(run.status).toBe(0);
  const read = seen.filter(r => /\/evaluate\/sdk_contract_(multiway|rule_value)$/.test(new URL(r.url).pathname));
  expect(read.map(r => r.url)).toEqual([
    'http://api.test/api/v1/feature-flags/evaluate/sdk_contract_multiway?user_id=mw-a',
    'http://api.test/api/v1/feature-flags/evaluate/sdk_contract_multiway?user_id=mw-b',
    'http://api.test/api/v1/feature-flags/evaluate/sdk_contract_multiway?user_id=mw-c',
    'http://api.test/api/v1/feature-flags/evaluate/sdk_contract_rule_value?user_id=user-a&context=%7B%22plan%22%3A%22pro%22%7D',
  ]);
  expect(JSON.parse(run.stdout.trim()).multiway).toEqual({
    users: { 'mw-a': 'value-of-mw-a', 'mw-b': 'value-of-mw-b', 'mw-c': 'value-of-mw-c' },
    rule: 'pro-copy',
  });
});

test('it reports the default for a multi-way flag the server sends no value for', () => {
  const { run } = runSmoke({ CONTROLVAR_API_KEY: 'k', CONTRACT_MULTIWAY_USERS: 'mw-a', FAKE_NO_VALUES: '1' });
  expect(run.status).toBe(0);
  expect(JSON.parse(run.stdout.trim()).multiway).toEqual({ users: { 'mw-a': 'smoke-default' }, rule: 'smoke-default' });
});

test('it reports what the typed getters returned: the defaults when the server sends no value', () => {
  const { run } = runSmoke({ CONTROLVAR_API_KEY: 'k', FAKE_NO_VALUES: '1' });
  expect(run.status).toBe(0);
  expect(JSON.parse(run.stdout.trim()).values).toEqual({ string: 'smoke-default', number: -1, json: ['smoke-default'] });
});

test('it stops without a key and sends nothing', () => {
  const { run, seen } = runSmoke({});
  expect(run.status).toBe(1);
  expect(run.stderr).toContain('CONTROLVAR_API_KEY is required');
  expect(seen).toEqual([]);
});

// #449: with the seed's sdk:ruleset key the smoke also links two ids. The runner gives that key
// to every smoke as CONTROLVAR_LOCAL_API_KEY and reads the database for what the step stored.
const LINKING = {
  CONTROLVAR_API_URL: 'http://api.test',
  CONTROLVAR_API_KEY: 'k',
  CONTROLVAR_LOCAL_API_KEY: 'scoped',
  CONTRACT_EXPERIMENT_KEY: 'exp_a',
  CONTRACT_FLAG_KEY: 'flag_a',
  CONTRACT_TARGETED_FLAG_KEY: 'targeted_a',
  CONTRACT_USER_ID: 'user-a',
};

test('with the sdk:ruleset key it links a new anonymous id to a new known id on one new client, and reports it', () => {
  const { run, seen } = runSmoke(LINKING);
  expect(run.stderr).toBe('');
  expect(run.status).toBe(0);
  const report = JSON.parse(run.stdout.trim());
  const { alias } = report;
  expect(alias).toEqual({
    anonymous_id: expect.stringMatching(/^smoke-anon-[0-9a-f-]{36}$/),
    user_id: expect.stringMatching(/^smoke-known-[0-9a-f-]{36}$/),
    created: [true, false],
    variant_before: 'control',
    variant_after: 'control',
    event_name: expect.stringMatching(/^contract_alias_[0-9a-f-]{36}$/),
  });
  // Every step before it is unchanged and uses the plain key; the alias step alone uses the
  // sdk:ruleset key: assign the anonymous id, link it twice, a key-less event under the known id
  // (fanned out to the experiment the anonymous id was answered for), then assign the known id.
  expect(seen.slice(0, 16).every(r => r.key === 'k')).toBe(true);
  const step = seen.slice(16);
  expect(step.map(r => `${r.key} ${r.method} ${new URL(r.url).pathname}`)).toEqual([
    'scoped POST /api/v1/tracking/assign',
    'scoped POST /api/v1/tracking/alias',
    'scoped POST /api/v1/tracking/alias',
    'scoped POST /api/v1/tracking/batch',
    'scoped POST /api/v1/tracking/assign',
  ]);
  expect(step[0].body).toEqual({ experiment_key: 'exp_a', user_id: alias.anonymous_id });
  expect(step[1].body).toEqual({ anonymous_id: alias.anonymous_id, user_id: alias.user_id });
  expect(step[3].body).toEqual({
    events: [
      { event_type: alias.event_name, event_name: alias.event_name, user_id: alias.user_id, value: 1, experiment_key: 'exp_a' },
    ],
  });
  expect(step[4].body).toEqual({ experiment_key: 'exp_a', user_id: alias.user_id });
  expect(Object.keys(report).sort()).toEqual(
    ['alias', 'assign', 'event_id', 'exposure', 'fanout', 'flag', 'multiway', 'sdk', 'targeted', 'track', 'values']
  );
});

test('it fails when the server answers the known id another variant than the anonymous id', () => {
  const { run } = runSmoke({ ...LINKING, FAKE_LINK_IGNORED: '1' });
  expect(run.status).toBe(1);
  expect(run.stdout).toBe('');
  expect(run.stderr).toBe(
    'js contract smoke failed: alias: the known id was answered treatment, the anonymous id control\n'
  );
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
