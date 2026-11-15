/**
 * Bootstrap smoke for the React SDK (#1518), in two steps for one user. The live
 * runner (tests/sdk-contract/live/run_live_contract.py) runs them in turn after
 * the react row has built `dist`, and counts the server's exposure rows for the
 * user and experiment after each.
 *
 *   CONTRACT_BOOTSTRAP_STEP=server  what a server-rendered page does: the
 *     documented call, `new ServerClient(config).bootstrap({ flags, experiments },
 *     user)`, then the page's server render with the Provider given that
 *     bootstrap (react-dom/server, no DOM). Fails unless the render shows the
 *     bootstrapped answers and made no request.
 *   CONTRACT_BOOTSTRAP_STEP=client  what the page's `useExperiment` sends once
 *     after hydration: one assignment through the browser client (the jsdom
 *     tests in src/__tests__/bootstrap.test.tsx pin that hydration sends exactly
 *     one).
 *
 * Env: CONTROLVAR_API_URL (default http://localhost:8000), CONTROLVAR_API_KEY
 * (required), CONTRACT_EXPERIMENT_KEY (default sdk_contract_ab),
 * CONTRACT_FLAG_KEY (default sdk_contract_flag), CONTRACT_USER_ID (required: the
 * runner names the user so it can count that user's rows).
 *
 * Prints exactly one JSON line on stdout and exits 0; on failure prints one line
 * to stderr and exits 1.
 */
import React from 'react';
import ReactDOMServer from 'react-dom/server';

import root from '../dist/index.js';
import ssr from '../dist/ssr/index.js';

const { ExperimentationClient, ExperimentationProvider, useExperiment, useFeatureFlag } = root;
const { ServerClient } = ssr;

const apiUrl = process.env.CONTROLVAR_API_URL || 'http://localhost:8000';
const apiKey = process.env.CONTROLVAR_API_KEY;
const experimentKey = process.env.CONTRACT_EXPERIMENT_KEY || 'sdk_contract_ab';
const flagKey = process.env.CONTRACT_FLAG_KEY || 'sdk_contract_flag';
const userId = process.env.CONTRACT_USER_ID;
const step = process.env.CONTRACT_BOOTSTRAP_STEP;

function fail(message) {
  process.stderr.write(`react bootstrap smoke failed: ${message}\n`);
  process.exit(1);
}

if (!apiKey) fail('CONTROLVAR_API_KEY is required');
if (!userId) fail('CONTRACT_USER_ID is required');
if (step !== 'server' && step !== 'client') fail('CONTRACT_BOOTSTRAP_STEP must be server or client');

// Count every request the SDK makes, so a request during the render shows.
let requests = 0;
const realFetch = globalThis.fetch;
globalThis.fetch = (input, init) => {
  requests += 1;
  return realFetch(input, init);
};

const config = { apiKey, baseUrl: apiUrl, timeoutMs: 10_000 };
const user = { userId, attributes: { source: 'contract-smoke' } };

function Page() {
  const flag = useFeatureFlag(flagKey);
  const exp = useExperiment(experimentKey);
  const ready = flag.loading || exp.loading ? 'loading' : 'ready';
  return React.createElement('span', null, `${flag.isEnabled ? 'on' : 'off'}|${exp.variantName}|${ready}`);
}

try {
  if (step === 'server') {
    const bootstrap = await new ServerClient(config).bootstrap(
      { flags: [flagKey], experiments: [experimentKey] },
      user
    );
    if (bootstrap.userId !== userId) fail(`bootstrap.userId is ${JSON.stringify(bootstrap.userId)}`);
    const assigned = bootstrap.experiments?.[experimentKey];
    if (!assigned) fail(`the bootstrap has no answer for ${experimentKey}`);
    if (bootstrap.flags?.[flagKey]?.enabled !== true) fail(`the bootstrap does not have ${flagKey} on`);

    // The page's server render, given the bootstrap as a framework passes a prop: through JSON.
    const before = requests;
    const html = ReactDOMServer.renderToString(
      React.createElement(
        ExperimentationProvider,
        { config, user, bootstrap: JSON.parse(JSON.stringify(bootstrap)) },
        React.createElement(Page)
      )
    );
    await new Promise(resolve => setTimeout(resolve, 50));
    const duringRender = requests - before;
    const expected = `<span>on|${assigned.variant_name}|ready</span>`;
    if (html !== expected) fail(`the server render gave ${html}, expected ${expected}`);
    if (duringRender !== 0) fail(`the server render made ${duringRender} request(s)`);

    process.stdout.write(
      JSON.stringify({ sdk: 'react', step, bootstrap, html, requests_during_render: duringRender }) + '\n'
    );
  } else {
    const assignment = await new ExperimentationClient(config).assignExperiment(user, experimentKey);
    process.stdout.write(
      JSON.stringify({ sdk: 'react', step, variant_name: assignment.variantName, assigned: assignment.assigned }) + '\n'
    );
  }
} catch (err) {
  fail(err && err.message ? err.message : String(err));
}
