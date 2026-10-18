/**
 * Contract smoke for the Edge SDK (see docs/sdk-guide.md, "Contract smoke").
 *
 * Runs under plain Node >= 18 — the SDK only uses Web-standard globals
 * (`fetch`, `AbortController`, `Headers`), which Node provides, so no edge
 * runtime adapter is needed.
 *
 * Run from the repo root:
 *   cd sdk/edge && npm run build --silent && CONTROLVAR_API_KEY=... node examples/contract_smoke.mjs
 *
 * Env: CONTROLVAR_API_URL (default http://localhost:8000), CONTROLVAR_API_KEY (required),
 * (the former names EXPERIMENTLY_API_URL / EXPERIMENTLY_API_KEY are read when these are unset),
 *      CONTRACT_EXPERIMENT_KEY (default sdk_contract_ab), CONTRACT_FLAG_KEY (default sdk_contract_flag),
 *      CONTRACT_USER_ID (default smoke-<uuid>).
 *
 * Prints exactly one JSON line on stdout and exits 0; on failure prints one line to stderr and exits 1.
 */
import { randomUUID } from 'node:crypto';
import { EdgeExperimentationClient } from '../dist/index.js';

const apiUrl =
  process.env.CONTROLVAR_API_URL || process.env.EXPERIMENTLY_API_URL || 'http://localhost:8000';
const apiKey = process.env.CONTROLVAR_API_KEY || process.env.EXPERIMENTLY_API_KEY;
const experimentKey = process.env.CONTRACT_EXPERIMENT_KEY || 'sdk_contract_ab';
const flagKey = process.env.CONTRACT_FLAG_KEY || 'sdk_contract_flag';
const userId = process.env.CONTRACT_USER_ID || `smoke-${randomUUID()}`;

function fail(message) {
  process.stderr.write(`edge contract smoke failed: ${message}\n`);
  process.exit(1);
}

if (!apiKey) fail('CONTROLVAR_API_KEY is required');

// Track requests with a wrapped fetch so we can tell "swallowed failure" from "ok":
// the SDK's track() never throws, so failures only show up on the wire.
const trackFailures = [];
const fetchImpl = async (url, init) => {
  const response = await fetch(url, init);
  if (typeof url === 'string' && url.includes('/api/v1/tracking/') && !url.endsWith('/assign') && !response.ok) {
    trackFailures.push(`${url} -> ${response.status}`);
  }
  return response;
};

// A generous timeout: the edge default (500 ms) is tuned for production latency.
const client = new EdgeExperimentationClient({ apiKey, baseUrl: apiUrl, timeout: 5000, fetch: fetchImpl });

try {
  // 1. Assign twice. clearCache() between the calls forces the second one to hit the
  //    server, so "sticky" reflects the server's answer rather than the local cache.
  const first = await client.getAssignment(experimentKey, userId, { source: 'contract-smoke' });
  if (!first) fail('assignment failed (null)');
  client.clearCache();
  const second = await client.getAssignment(experimentKey, userId);
  if (!second) fail('second assignment failed (null)');

  if (!['control', 'treatment'].includes(first.variantName)) {
    fail(`unexpected variant_name ${JSON.stringify(first.variantName)}`);
  }
  const sticky = first.variantName === second.variantName && first.variantId === second.variantId;
  if (!sticky) fail(`assignment not sticky: ${first.variantName} then ${second.variantName}`);

  // 2. Evaluate the flag (100% on in the seeded data).
  const flag = await client.evaluateFlag(flagKey, userId);
  if (typeof flag.enabled !== 'boolean') fail('flag.enabled is not a boolean');
  if (client.evaluateFlagSync(flagKey, userId) !== flag.enabled) fail('sync cache read disagrees with evaluateFlag');

  // 3. Track with an explicit experiment key.
  await client.track('purchase', userId, { source: 'contract-smoke' }, { value: 12.5, experimentKey });
  if (trackFailures.length) fail(`track: ${trackFailures[0]}`);

  // 4. Track without a key: fans out to the cached assignment + evaluated flag.
  if (client.getAssignments(userId).length !== 1 || client.getEvaluatedFlags(userId).length !== 1) {
    fail('cache does not hold one assignment and one flag; fan-out would send nothing');
  }
  await client.track('page_view', userId, { page: '/contract-smoke' });
  if (trackFailures.length) fail(`fan-out: ${trackFailures[0]}`);

  // 4b. Explicit two-event batch.
  const batch = await client.trackBatch([
    { eventName: 'add_to_cart', userId, experimentKey, value: 1 },
    { eventName: 'flag_seen', userId, featureFlagKey: flagKey },
  ]);
  if (batch.failureCount > 0 || batch.successCount !== 2) fail(`batch: ${JSON.stringify(batch)}`);

  process.stdout.write(
    JSON.stringify({
      sdk: 'edge',
      assign: { variant_name: second.variantName, is_control: second.isControl, sticky },
      flag: { enabled: flag.enabled },
      track: { ok: true },
      fanout: { ok: true },
    }) + '\n',
  );
} catch (err) {
  fail(err && err.message ? err.message : String(err));
}
