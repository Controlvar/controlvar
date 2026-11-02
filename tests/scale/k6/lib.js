// SPDX-FileCopyrightText: 2026 The Controlvar authors
// SPDX-License-Identifier: Apache-2.0
//
// The scale scenarios' pure parts (#1608): what each request is, what its
// answer showed, and the one record per request the generator writes. No k6
// module and no I/O, so backend/tests/unit/scripts/test_scale_run.py runs these
// functions under node; scenarios.js is the k6 glue around them.
//
// The record carries no URL, no query, no header and no error text: the
// scenario name, the iteration number, the status, an error code, two timings
// and the few body facts tests/scale/scale_evidence.py judges. Every value is
// a code or a whole number, so the record survives k6's CSV output as written.

// The record's fields, in order. scripts/scale_run.py reads them back in this
// order; a test holds the two lists equal, and equal to scale_evidence.py's
// ROW_FIELDS without "endpoint" (which comes from that file's fixed list, never
// from a URL) plus the scenario's start time.
export const RECORD_FIELDS = [
  'scenario',
  'iteration',
  'status',
  'error',
  'latency_us',
  'start_lag_us',
  'rate_limit',
  'flag_class',
  'reason',
  'assigned',
  'variant',
  'user_index',
  'success_count',
  'failure_count',
  'flag_count',
  'id_present',
  'variant_id_present',
  'scenario_start_ms',
];

// The metadata key that carries the record on each request's sample. Not a k6
// system tag's name: k6's CSV output drops a metadata key that is one.
export const RECORD_KEY = 'scale_row';

// The limiter's header, looked up without regard to case: k6 hands headers on
// under Go's canonical spelling (X-Ratelimit-Limit), nginx sends them in lower
// case, and the API names them X-RateLimit-Limit.
export const LIMIT_HEADER = 'X-RateLimit-Limit';

// k6's error code for a request that hit its timeout (lib/netext/httpext).
export const TIMEOUT_ERROR_CODE = 1050;

// Every request path, as a template: the scenarios build nothing else.
export const PATHS = {
  evaluate_get: '/api/v1/feature-flags/evaluate/{key}',
  assign: '/api/v1/tracking/assign',
  assign_returning: '/api/v1/tracking/assign',
  track: '/api/v1/tracking/track',
  batch: '/api/v1/tracking/batch',
  ruleset: '/api/v1/sdk/ruleset',
  calibration: '/stall',
};

// The requests a measured run may name, and the smoke's mix of them.
export const SCENARIOS = Object.keys(PATHS).filter((name) => name !== 'calibration');

// One batch request carries this many events.
export const BATCH_EVENTS = 100;

// A step that spreads users over the preloaded pool visits each of them once
// in every pool-sized run of iterations: 7919 is prime, so it shares no factor
// with a pool of 10^n users.
const SPREAD = 7919;

const CODE = /^[a-z_]{1,40}$/;
const VARIANT = /^[A-Za-z0-9_.-]{1,64}$/;
const DIGITS = /^[0-9]{1,20}$/;
const SAFE = /^[A-Za-z0-9_.-]*$/;

function whole(value, name, low) {
  const text = String(value === undefined ? '' : value);
  if (!/^[0-9]{1,15}$/.test(text) || Number(text) < low) {
    throw new Error(`${name} must be a whole number of at least ${low}, got ${JSON.stringify(text)}`);
  }
  return Number(text);
}

// The harness's settings for one k6 process, from its environment, each
// checked: a k6 process with a wrong one fails before it sends anything.
export function readEnv(env) {
  const scenario = String(env.SCALE_SCENARIO || '');
  if (!(scenario in PATHS) && scenario !== 'mix') {
    throw new Error(`SCALE_SCENARIO ${JSON.stringify(scenario)} is not a scenario`);
  }
  const base = String(env.SCALE_BASE_URL || '');
  if (!/^http:\/\/[a-z0-9.-]{1,63}:[0-9]{1,5}$/.test(base)) {
    throw new Error(`SCALE_BASE_URL ${JSON.stringify(base)} is not http://<service>:<port>`);
  }
  const host = String(env.SCALE_HOST || '');
  if (!/^[a-z0-9.-]{1,63}(:[0-9]{1,5})?$/.test(host)) {
    throw new Error(`SCALE_HOST ${JSON.stringify(host)} is not a host`);
  }
  let mix = {};
  if (scenario === 'mix') {
    mix = JSON.parse(String(env.SCALE_MIX || '{}'));
    const names = Object.keys(mix);
    const total = names.reduce((sum, name) => sum + whole(mix[name], `SCALE_MIX.${name}`, 1), 0);
    if (!names.length || names.some((name) => !SCENARIOS.includes(name)) || total !== 100) {
      throw new Error('SCALE_MIX must weigh the measured requests, summing to 100');
    }
  }
  const out = {
    scenario,
    base,
    host,
    mix,
    rate: whole(env.SCALE_RATE, 'SCALE_RATE', 1),
    timeUnitMs: whole(env.SCALE_TIME_UNIT_MS, 'SCALE_TIME_UNIT_MS', 1),
    durationS: whole(env.SCALE_DURATION_S, 'SCALE_DURATION_S', 1),
    firstMeasured: whole(env.SCALE_FIRST_MEASURED, 'SCALE_FIRST_MEASURED', 0),
    vus: whole(env.SCALE_VUS, 'SCALE_VUS', 1),
    timeoutMs: whole(env.SCALE_TIMEOUT_MS, 'SCALE_TIMEOUT_MS', 1),
    step: whole(env.SCALE_STEP, 'SCALE_STEP', 0),
    etag: String(env.SCALE_ETAG || ''),
    eventName: String(env.SCALE_EVENT || ''),
    fixtures: String(env.SCALE_FIXTURES || ''),
    summary: String(env.SCALE_SUMMARY || ''),
  };
  if (!CODE.test(out.eventName)) {
    throw new Error(`SCALE_EVENT ${JSON.stringify(out.eventName)} is not an event name`);
  }
  if (!/^\/[A-Za-z0-9_./-]{1,200}$/.test(out.fixtures) || !/^\/[A-Za-z0-9_./-]{1,200}$/.test(out.summary)) {
    throw new Error('SCALE_FIXTURES and SCALE_SUMMARY are absolute paths');
  }
  if (!/^(W\/)?"[A-Za-z0-9_.:-]{0,200}"$/.test(out.etag) && out.etag !== '') {
    throw new Error('SCALE_ETAG is not an entity tag');
  }
  return out;
}

// A header's value whatever the case of its name; '' when it is absent.
export function headerValue(headers, name) {
  const wanted = String(name).toLowerCase();
  for (const key of Object.keys(headers || {})) {
    if (key.toLowerCase() === wanted) {
      const value = headers[key];
      return Array.isArray(value) ? String(value[0]) : String(value);
    }
  }
  return '';
}

// How late the request started against the schedule, in microseconds, in the
// script's own clock: iteration i of a step running `rate` per `timeUnitMs` is
// due i x timeUnitMs / rate ms after the scenario's start (ADDENDUM A item 1).
// Both clocks have 1 ms resolution, so a request can appear up to 1 ms early;
// that reads as 0, never as a negative lag.
export function startLagUs(nowMs, startTimeMs, iteration, rate, timeUnitMs) {
  const dueMs = startTimeMs + (iteration * timeUnitMs) / rate;
  const lag = Math.round((nowMs - dueMs) * 1000);
  return lag > 0 ? lag : 0;
}

// The request's time from its start to its last byte, any wait for a
// connection included: http_req_blocked + http_req_duration (PE-S1). Not
// blocked + connecting + duration: blocked already holds the connect.
export function latencyUs(timings) {
  const ms = Number(timings && timings.blocked) + Number(timings && timings.duration);
  if (!Number.isFinite(ms) || ms < 0) {
    return 0;
  }
  return Math.round(ms * 1000);
}

// '' for an answer, 'timeout' for a request k6 gave up on, 'connection' for
// one that got no answer at all.
export function errorKind(status, errorCode) {
  if (Number(status) !== 0) {
    return '';
  }
  return Number(errorCode) === TIMEOUT_ERROR_CODE ? 'timeout' : 'connection';
}

// The mix's request for an iteration: its place in each run of 100, against
// the weights in their order, so a smoke run is the same every time.
export function mixScenario(iteration, mix) {
  let slot = iteration % 100;
  for (const name of Object.keys(mix)) {
    if (slot < mix[name]) {
      return name;
    }
    slot -= mix[name];
  }
  throw new Error('the mix weights do not sum to 100');
}

function pad(index, width) {
  return String(index).padStart(width, '0');
}

// A preloaded (returning) user: its index in the pool and its id, the
// preload's own format (scripts/scale_stack.py's prefix and width).
export function returningUser(seed, pool) {
  const count = Number(pool.count);
  const index = (seed * SPREAD) % count;
  return { index, id: `${pool.prefix}${pad(index, Number(pool.width))}` };
}

// The flag an evaluation asks for: every fixture flag in turn, the rule flags
// first; its class decides the reason the method expects.
export function flagFor(iteration, flags) {
  const keys = flags.rule.concat(flags.plain);
  const position = iteration % keys.length;
  return { key: keys[position], flagClass: position < flags.rule.length ? 'rule' : 'plain' };
}

function json(value) {
  return JSON.stringify(value);
}

// One request: its method, URL, body and headers, and the facts the record
// keeps about what was asked (the flag's class, the returning user's index).
// Flag keys come only from the fixtures; user ids travel only in the query
// string or the body (V42).
export function buildRequest(scenario, iteration, ctx) {
  const fixtures = ctx.fixtures;
  const sdkKey = fixtures.api_keys['scale-sdk'].key;
  const headers = { Host: ctx.host };
  const out = { method: 'GET', body: null, headers, flagClass: '', userIndex: '' };
  if (scenario === 'evaluate_get') {
    const flag = flagFor(iteration, fixtures.flags);
    const query = [`user_id=${encodeURIComponent(`scale-eval-${iteration}`)}`];
    if (flag.flagClass === 'rule') {
      query.push(`context=${encodeURIComponent(json(fixtures.rule_context))}`);
    }
    headers['X-API-Key'] = sdkKey;
    out.url = `${ctx.base}${PATHS.evaluate_get.replace('{key}', encodeURIComponent(flag.key))}?${query.join('&')}`;
    out.flagClass = flag.flagClass;
    return out;
  }
  headers['Content-Type'] = 'application/json';
  if (scenario === 'assign' || scenario === 'assign_returning') {
    let user = `scale-new-${ctx.step}-${iteration}`;
    if (scenario === 'assign_returning') {
      const returning = returningUser(iteration, fixtures.returning_users);
      user = returning.id;
      out.userIndex = String(returning.index);
    }
    headers['X-API-Key'] = sdkKey;
    out.method = 'POST';
    out.url = `${ctx.base}${PATHS[scenario]}`;
    out.body = json({ experiment_key: fixtures.experiment.key, user_id: user });
    return out;
  }
  if (scenario === 'track') {
    const user = returningUser(iteration, fixtures.returning_users);
    headers['X-API-Key'] = sdkKey;
    out.method = 'POST';
    out.url = `${ctx.base}${PATHS.track}`;
    out.body = json({
      event_type: ctx.eventName,
      user_id: user.id,
      experiment_key: fixtures.experiment.key,
    });
    return out;
  }
  if (scenario === 'batch') {
    const events = [];
    for (let j = 0; j < BATCH_EVENTS; j += 1) {
      const user = returningUser(iteration * BATCH_EVENTS + j, fixtures.returning_users);
      events.push({
        event_type: ctx.eventName,
        user_id: user.id,
        experiment_key: fixtures.experiment.key,
      });
    }
    headers['X-API-Key'] = sdkKey;
    out.method = 'POST';
    out.url = `${ctx.base}${PATHS.batch}`;
    out.body = json({ events });
    return out;
  }
  if (scenario === 'ruleset') {
    delete headers['Content-Type'];
    headers['X-API-Key'] = fixtures.api_keys['scale-server'].key;
    if (ctx.etag) {
      headers['If-None-Match'] = ctx.etag;
    }
    out.url = `${ctx.base}${PATHS.ruleset}`;
    return out;
  }
  if (scenario === 'calibration') {
    delete headers['Content-Type'];
    out.url = `${ctx.base}${PATHS.calibration}`;
    return out;
  }
  throw new Error(`no request is called ${JSON.stringify(scenario)}`);
}

function code(value, pattern, otherwise) {
  return typeof value === 'string' && pattern.test(value) ? value : otherwise;
}

function count(value) {
  return Number.isInteger(value) && value >= 0 ? String(value) : '';
}

// What the answer's body showed, as codes and counts; '' where a fact does not
// apply or the body did not hold it. Whether that passes is decided later, by
// scale_evidence.py's judge, never here.
export function bodyFacts(scenario, status, body) {
  const facts = {
    reason: '',
    assigned: '',
    variant: '',
    success_count: '',
    failure_count: '',
    flag_count: '',
    id_present: '',
    variant_id_present: '',
  };
  if (Number(status) !== 200 || typeof body !== 'string' || !body) {
    return facts;
  }
  let data;
  try {
    data = JSON.parse(body);
  } catch (err) {
    return facts;
  }
  if (data === null || typeof data !== 'object') {
    return facts;
  }
  if (scenario === 'evaluate_get') {
    facts.reason = code(data.reason, CODE, 'unexpected');
  } else if (scenario === 'assign' || scenario === 'assign_returning') {
    facts.assigned = data.assigned === true ? '1' : '0';
    facts.reason = code(data.reason, CODE, 'unexpected');
    facts.variant = code(data.variant_name, VARIANT, '');
  } else if (scenario === 'track') {
    facts.id_present = typeof data.id === 'string' && data.id.length > 0 ? '1' : '0';
    facts.variant_id_present = typeof data.variant_id === 'string' && data.variant_id.length > 0 ? '1' : '0';
  } else if (scenario === 'batch') {
    facts.success_count = count(data.success_count);
    facts.failure_count = count(data.failure_count);
  } else if (scenario === 'ruleset') {
    facts.flag_count = Array.isArray(data.flags) ? String(data.flags.length) : '';
  }
  return facts;
}

// The record for one request, from what was asked and a plain view of k6's
// response: {status, error_code, headers, body, timings: {blocked, duration}}.
export function rowFor(scenario, iteration, request, response, startLag, scenarioStartMs) {
  const status = Number(response.status) || 0;
  const limit = headerValue(response.headers, LIMIT_HEADER);
  return {
    scenario,
    iteration: String(iteration),
    status: String(status),
    error: errorKind(status, response.error_code),
    latency_us: String(latencyUs(response.timings)),
    start_lag_us: String(startLag),
    // A value that is not a number cannot be the configured limit: it is kept
    // as a mismatch, never as text.
    rate_limit: limit === '' ? '' : code(limit, DIGITS, 'x'),
    flag_class: request.flagClass,
    user_index: request.userIndex,
    ...bodyFacts(scenario, status, response.body),
    scenario_start_ms: String(scenarioStartMs),
  };
}

// The record as one metadata value: the fields in RECORD_FIELDS order, joined
// by '|'. A value outside [A-Za-z0-9_.-] is a defect here, and throws: the
// iteration then writes nothing, which the summariser's sent identity catches.
export function record(fields) {
  const values = RECORD_FIELDS.map((name) => {
    const value = fields[name] === undefined ? '' : String(fields[name]);
    if (!SAFE.test(value)) {
      throw new Error(`record field ${name} holds ${JSON.stringify(value)}`);
    }
    return value;
  });
  return values.join('|');
}

// What k6 itself counted, for scripts/scale_run.py to check its reading of
// the CSV against: requests recorded, requests measured, and dropped ticks.
export function summaryFacts(data) {
  const metric = (name, stat) => {
    const found = data && data.metrics && data.metrics[name];
    const value = found && found.values ? found.values[stat] : 0;
    return Number.isFinite(value) ? value : 0;
  };
  return {
    requests: Math.round(metric('scale_request', 'count')),
    measured: Math.round(metric('scale_measured', 'count')),
    dropped: Math.round(metric('dropped_iterations', 'count')),
    measured_p99_us: Math.round(metric('scale_measured', 'p(99)') * 1000),
  };
}
