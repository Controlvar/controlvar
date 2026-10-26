/**
 * Contract smoke for the OpenFeature (JS) provider.
 *
 * Run from the repo root:
 *   cd sdk/openfeature && npm run build --silent && CONTROLVAR_API_KEY=... node examples/contract_smoke.mjs
 *
 * Flags go through the OpenFeature API; experiment assignment and tracking go
 * through `provider.client` (the underlying JS SDK client).
 * Prints exactly one JSON line on stdout and exits 0; on failure one line to stderr and exit 1.
 */
import { randomUUID } from 'node:crypto';
import { OpenFeature } from '@openfeature/server-sdk';
import pkg from '../dist/index.js';

const { ExperimentationProvider, ExperimentationClient } = pkg;

const apiUrl =
  process.env.CONTROLVAR_API_URL || process.env.EXPERIMENTLY_API_URL || 'http://localhost:8000';
const apiKey = process.env.CONTROLVAR_API_KEY || process.env.EXPERIMENTLY_API_KEY;
const experimentKey = process.env.CONTRACT_EXPERIMENT_KEY || 'sdk_contract_ab';
const flagKey = process.env.CONTRACT_FLAG_KEY || 'sdk_contract_flag';
const userId = process.env.CONTRACT_USER_ID || `smoke-${randomUUID()}`;

function fail(message) {
  process.stderr.write(`openfeature contract smoke failed: ${message}\n`);
  process.exit(1);
}

if (!apiKey) fail('CONTROLVAR_API_KEY is required');

const trackErrors = [];
const sdk = new ExperimentationClient({
  apiUrl,
  apiKey,
  onError: (error, operation) => trackErrors.push(`${operation}: ${error.message}`),
});
const provider = new ExperimentationProvider({ apiKey, baseUrl: apiUrl, client: sdk });

try {
  await OpenFeature.setProviderAndWait(provider);
  const flags = OpenFeature.getClient('contract-smoke');

  // 1. Assign twice; clearCache() in between makes the second call hit the server.
  const first = await sdk.getAssignment(experimentKey, { userId, attributes: { source: 'contract-smoke' } });
  sdk.clearCache();
  const second = await sdk.getAssignment(experimentKey, { userId });
  if (!['control', 'treatment'].includes(first.variantName)) {
    fail(`unexpected variant_name ${JSON.stringify(first.variantName)}`);
  }
  const sticky = first.variantName === second.variantName && first.variantId === second.variantId;
  if (!sticky) fail(`assignment not sticky: ${first.variantName} then ${second.variantName}`);

  // 2. Evaluate the flag through OpenFeature.
  const details = await flags.getBooleanDetails(flagKey, false, { targetingKey: userId });
  if (details.errorCode) fail(`flag: ${details.errorCode} ${details.errorMessage ?? ''}`);
  if (typeof details.value !== 'boolean') fail('flag value is not a boolean');

  // 3. Track with an explicit experiment key.
  await sdk.track(userId, 'purchase', { value: 12.5, experimentKey });
  if (trackErrors.length) fail(`track: ${trackErrors[0]}`);

  // 4. Track without a key: the JS SDK fans it out to the assignment above and the flag
  //    evaluated via OpenFeature.
  if (sdk.getAssignments(userId).length !== 1 || sdk.getEvaluatedFlags(userId).length !== 1) {
    fail('cache does not hold one assignment and one flag; the key-less track would not reach both');
  }
  await sdk.track(userId, 'page_view', { properties: { page: '/contract-smoke' } });
  if (trackErrors.length) fail(`fan-out: ${trackErrors[0]}`);

  // 4b. Explicit two-event batch.
  const batch = await sdk.trackBatch([
    { userId, eventName: 'add_to_cart', experimentKey, value: 1 },
    { userId, eventName: 'flag_seen', featureFlagKey: flagKey },
  ]);
  if (batch.failureCount > 0 || batch.successCount !== 2) fail(`batch: ${JSON.stringify(batch)}`);

  process.stdout.write(
    JSON.stringify({
      sdk: 'openfeature',
      assign: { variant_name: second.variantName, is_control: second.isControl, sticky },
      flag: { enabled: details.value },
      track: { ok: true },
      fanout: { ok: true },
    }) + '\n'
  );
  await OpenFeature.close();
} catch (err) {
  fail(err && err.message ? err.message : String(err));
}
