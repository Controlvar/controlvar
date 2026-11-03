// SPDX-FileCopyrightText: 2026 The Controlvar authors
// SPDX-License-Identifier: Apache-2.0
//
// One step of the scale measurement (#1608): one request type at one rate, as
// an open model. scripts/scale_run.py starts one k6 process per step, from the
// official image pinned by digest, and gives it everything below through
// SCALE_* variables; lib.js holds the logic, and this file is the k6 glue.
//
// Each step is one constant-arrival-rate scenario: `rate` iterations every
// `timeUnit`, the exact pair for the step's rate, with every VU allocated
// before the step starts. A tick no VU can take is a dropped iteration, and
// the threshold below makes k6 exit 99 when there is one: scale_run.py reads
// the count from the summary and checks the exit status agrees with it.
//
// One sample of scale_request per request carries the request's record as
// metadata (lib.js RECORD_KEY); scale_run.py reads those samples back from
// k6's CSV output. The system tags are set explicitly, without url, name or
// error, so no URL (whose query holds the user ids and the context) reaches
// that output (PE-S2): a test parses the setting below.

import http from 'k6/http';
import exec from 'k6/execution';
import { Trend } from 'k6/metrics';

import {
  buildRequest,
  latencyUs,
  mixScenario,
  readEnv,
  record,
  RECORD_KEY,
  rowFor,
  sentOffsetUs,
  startLagUs,
  summaryFacts,
} from './lib.js';

const env = readEnv(__ENV);
const fixtures = JSON.parse(open(env.fixtures));
const context = {
  base: env.base,
  host: env.host,
  step: env.step,
  fixtures,
  eventName: env.eventName,
  etag: env.etag,
};

// Every request, and the measured ones (after the settle) for k6's own count.
const requests = new Trend('scale_request', true);
const measured = new Trend('scale_measured', true);

export const options = {
  systemTags: ['status', 'method', 'scenario', 'expected_response'],
  summaryTrendStats: ['count', 'p(99)'],
  discardResponseBodies: false,
  userAgent: 'controlvar-scale',
  scenarios: {
    [env.scenario]: {
      executor: 'constant-arrival-rate',
      rate: env.rate,
      timeUnit: `${env.timeUnitMs}ms`,
      duration: `${env.durationS}s`,
      preAllocatedVUs: env.vus,
      maxVUs: env.vus,
      // Long enough for the last request to answer or time out.
      gracefulStop: `${env.timeoutMs + 2000}ms`,
    },
  },
  thresholds: {
    dropped_iterations: ['count<1'],
  },
};

export default function () {
  const iteration = exec.scenario.iterationInTest;
  const startTime = exec.scenario.startTime;
  const now = Date.now();
  const lag = startLagUs(now, startTime, iteration, env.rate, env.timeUnitMs);
  const sentOffset = sentOffsetUs(now, startTime);
  const scenario = env.scenario === 'mix' ? mixScenario(iteration, env.mix) : env.scenario;
  const request = buildRequest(scenario, iteration, context);
  const res = http.request(request.method, request.url, request.body, {
    headers: request.headers,
    timeout: `${env.timeoutMs}ms`,
    redirects: 0,
    responseType: 'text',
  });
  const fields = rowFor(
    scenario,
    iteration,
    request,
    {
      status: res.status,
      error_code: res.error_code,
      headers: res.headers,
      body: res.body,
      timings: { blocked: res.timings.blocked, duration: res.timings.duration },
    },
    lag,
    startTime,
    sentOffset,
  );
  const value = latencyUs(res.timings) / 1000;
  exec.vu.metrics.metadata[RECORD_KEY] = record(fields);
  requests.add(value);
  delete exec.vu.metrics.metadata[RECORD_KEY];
  // Measured: sent after the settle, by send time (the iteration number counts
  // started iterations, not scheduled slots, once one is dropped: #1667).
  if (sentOffset >= env.settleMs * 1000) {
    measured.add(value);
  }
}

export function handleSummary(data) {
  return { [env.summary]: JSON.stringify(summaryFacts(data)) };
}
