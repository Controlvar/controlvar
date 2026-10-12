/**
 * Local-evaluation smoke for the JavaScript SDK (#226), run by the live contract
 * (tests/sdk-contract/live/run_live_contract.py), which checks the server side of it: that these
 * evaluations made no evaluate call, and that the counts sent on close() are the safety
 * monitor's denominator for the errors reported afterwards.
 *
 * The errors are reported with the same sdk:ruleset key, the way a server forwards them: only
 * reports sent with a server-side key count toward a flag's error rate and automatic rollback.
 *
 * Run from the repo root:
 *   cd sdk/js && npm run build --silent && node examples/local_eval_smoke.mjs
 *
 * Env: EXPERIMENTLY_API_URL (default http://localhost:8000), EXPERIMENTLY_LOCAL_API_KEY (a key
 *      with the sdk:ruleset scope, required; also used for POST /api/v1/tracking/errors),
 *      CONTRACT_FLAG_KEY (default sdk_contract_flag), CONTRACT_USER_ID (default
 *      local-smoke-<uuid>), LOCAL_EVALUATIONS (default 40), LOCAL_ERRORS (default 2).
 *
 * Prints exactly one JSON line on stdout and exits 0; on failure prints one line to stderr and exits 1.
 */
import { randomUUID } from 'node:crypto';
import sdk from '../dist/index.js';

const { ExperimentationClient } = sdk;

const apiUrl = (process.env.EXPERIMENTLY_API_URL || 'http://localhost:8000').replace(/\/+$/, '');
const localKey = process.env.EXPERIMENTLY_LOCAL_API_KEY;
const flagKey = process.env.CONTRACT_FLAG_KEY || 'sdk_contract_flag';
const userId = process.env.CONTRACT_USER_ID || `local-smoke-${randomUUID()}`;
const evaluations = Number(process.env.LOCAL_EVALUATIONS || 40);
const errorsToPost = Number(process.env.LOCAL_ERRORS || 2);

function fail(message) {
  process.stderr.write(`js local-evaluation smoke failed: ${message}\n`);
  process.exit(1);
}

if (!localKey) fail('EXPERIMENTLY_LOCAL_API_KEY (a key with the sdk:ruleset scope) is required');

const swallowed = [];
const client = new ExperimentationClient({
  apiUrl,
  apiKey: localKey,
  evaluation: 'local',
  onError: (error, operation) => swallowed.push(`${operation}: ${error.message}`),
});

try {
  const ready = await client.ready({ timeoutMs: 15_000 });
  if (!ready.ok) fail(`ready(): ${ready.error.message}`);

  let enabled = 0;
  for (let i = 0; i < evaluations; i++) {
    const result = await client.evaluateFlag(flagKey, { userId, attributes: { source: 'local-smoke' } });
    if (result.source !== 'local') fail(`evaluation ${i} was made on the server (source ${result.source})`);
    if (result.enabled) enabled += 1;
  }
  const status = client.status();

  // close() sends the evaluation counts.
  await client.close();
  if (swallowed.length) fail(swallowed[0]);

  // Errors against the same flag, reported with the server-side key: the safety monitor
  // divides these by the counts above.
  for (let i = 0; i < errorsToPost; i++) {
    const response = await fetch(`${apiUrl}/api/v1/tracking/errors`, {
      method: 'POST',
      headers: { 'X-API-Key': localKey, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        feature_flag_key: flagKey,
        user_id: userId,
        error_type: 'local_smoke',
        message: `local-evaluation smoke error ${i + 1}`,
      }),
    });
    if (response.status !== 201) fail(`POST /api/v1/tracking/errors answered ${response.status}`);
  }

  process.stdout.write(
    JSON.stringify({
      sdk: 'js',
      mode: 'local',
      flag_key: flagKey,
      user_id: userId,
      evaluations,
      enabled,
      errors_posted: errorsToPost,
      ruleset_version: status.rulesetVersion,
    }) + '\n'
  );
} catch (err) {
  fail(err && err.message ? err.message : String(err));
}
