/**
 * Contract smoke for the JavaScript SDK (see docs/sdk-guide.md, "Contract smoke").
 *
 * Run from the repo root:
 *   cd sdk/js && npm run build --silent && CONTROLVAR_API_KEY=... node examples/contract_smoke.mjs
 *
 * Env: CONTROLVAR_API_URL (default http://localhost:8000), CONTROLVAR_API_KEY (required),
 * (the former names EXPERIMENTLY_API_URL / EXPERIMENTLY_API_KEY are read when these are unset),
 *      CONTRACT_EXPERIMENT_KEY (default sdk_contract_ab), CONTRACT_FLAG_KEY (default sdk_contract_flag),
 *      CONTRACT_USER_ID (default smoke-<uuid>).
 *
 * Prints exactly one JSON line on stdout and exits 0; on failure prints one line to stderr and exits 1.
 */
import { randomUUID } from 'node:crypto';
import sdk from '../dist/index.js';

const { ExperimentationClient } = sdk;

const apiUrl =
  process.env.CONTROLVAR_API_URL || process.env.EXPERIMENTLY_API_URL || 'http://localhost:8000';
const apiKey = process.env.CONTROLVAR_API_KEY || process.env.EXPERIMENTLY_API_KEY;
const experimentKey = process.env.CONTRACT_EXPERIMENT_KEY || 'sdk_contract_ab';
const flagKey = process.env.CONTRACT_FLAG_KEY || 'sdk_contract_flag';
const userId = process.env.CONTRACT_USER_ID || `smoke-${randomUUID()}`;

function fail(message) {
  process.stderr.write(`js contract smoke failed: ${message}\n`);
  process.exit(1);
}

if (!apiKey) fail('CONTROLVAR_API_KEY is required');

const trackErrors = [];
const client = new ExperimentationClient({
  apiUrl,
  apiKey,
  onError: (error, operation) => trackErrors.push(`${operation}: ${error.message}`),
});

try {
  // 1. Assign twice. clearCache() between the calls forces the second one to hit the
  //    server, so "sticky" reflects the server's answer rather than the local cache.
  const first = await client.getAssignment(experimentKey, {
    userId,
    attributes: { source: 'contract-smoke' },
  });
  client.clearCache();
  const second = await client.getAssignment(experimentKey, { userId });

  if (!['control', 'treatment'].includes(first.variantName)) {
    fail(`unexpected variant_name ${JSON.stringify(first.variantName)}`);
  }
  const sticky = first.variantName === second.variantName && first.variantId === second.variantId;
  if (!sticky) fail(`assignment not sticky: ${first.variantName} then ${second.variantName}`);

  // 2. Evaluate the flag (100% on in the seeded data).
  const flag = await client.evaluateFlag(flagKey, { userId });
  if (typeof flag.enabled !== 'boolean') fail('flag.enabled is not a boolean');

  // 3. Track with an explicit experiment key.
  await client.track(userId, 'purchase', { value: 12.5, experimentKey });
  if (trackErrors.length) fail(`track: ${trackErrors[0]}`);

  // 4. Track without a key: fans out to the assignment + evaluated flag answered above.
  if (client.getAssignments(userId).length !== 1 || client.getEvaluatedFlags(userId).length !== 1) {
    fail('cache does not hold one assignment and one flag');
  }
  await client.track(userId, 'page_view', { properties: { page: '/contract-smoke' } });
  if (trackErrors.length) fail(`fan-out: ${trackErrors[0]}`);

  // 4b. Explicit two-event batch.
  const batch = await client.trackBatch([
    { userId, eventName: 'add_to_cart', experimentKey, value: 1 },
    { userId, eventName: 'flag_seen', featureFlagKey: flagKey },
  ]);
  if (batch.failureCount > 0 || batch.successCount !== 2) {
    fail(`batch: ${JSON.stringify(batch)}`);
  }

  process.stdout.write(
    JSON.stringify({
      sdk: 'js',
      assign: { variant_name: second.variantName, is_control: second.isControl, sticky },
      flag: { enabled: flag.enabled },
      track: { ok: true },
      fanout: { ok: true },
    }) + '\n'
  );
} catch (err) {
  fail(err && err.message ? err.message : String(err));
}
