/**
 * Contract smoke for the React SDK, driven through its SSR entry point
 * (`ServerClient`) so it runs under plain Node with no DOM and no React render.
 *
 * Run from the repo root:
 *   cd sdk/react && npm run build --silent && CONTROLVAR_API_KEY=... node examples/contract_smoke.mjs
 *
 * Env: CONTROLVAR_API_URL (default http://localhost:8000), CONTROLVAR_API_KEY (required),
 * (the former names EXPERIMENTLY_API_URL / EXPERIMENTLY_API_KEY are read when these are unset),
 *      CONTRACT_EXPERIMENT_KEY (default sdk_contract_ab), CONTRACT_FLAG_KEY (default sdk_contract_flag),
 *      CONTRACT_USER_ID (default smoke-<uuid>).
 *
 * Prints exactly one JSON line on stdout and exits 0; on failure prints one line to stderr and exits 1.
 *
 * Note on tracking: `ExperimentationClient.trackEvent()` is fire-and-forget and never
 * throws, so calling it proves nothing on its own. `fetch` is wrapped below to record
 * the status of every request the SDK makes, and the smoke fails when a tracking call
 * did not come back 2xx — which is what catches a path the backend does not serve.
 */
import { randomUUID } from 'node:crypto';

// The SSR entry point: no React, no browser globals.
import ssr from '../dist/ssr/index.js';
import clientModule from '../dist/client/ExperimentationClient.js';

const { ServerClient } = ssr;
const { ExperimentationClient } = clientModule;

const apiUrl =
  process.env.CONTROLVAR_API_URL || process.env.EXPERIMENTLY_API_URL || 'http://localhost:8000';
const apiKey = process.env.CONTROLVAR_API_KEY || process.env.EXPERIMENTLY_API_KEY;
const experimentKey = process.env.CONTRACT_EXPERIMENT_KEY || 'sdk_contract_ab';
const flagKey = process.env.CONTRACT_FLAG_KEY || 'sdk_contract_flag';
const userId = process.env.CONTRACT_USER_ID || `smoke-${randomUUID()}`;

function fail(message) {
  process.stderr.write(`react contract smoke failed: ${message}\n`);
  process.exit(1);
}

if (!apiKey) fail('CONTROLVAR_API_KEY is required');

// ── record every request the SDK makes, so fire-and-forget tracking is checkable ──
const calls = [];
const realFetch = globalThis.fetch;
globalThis.fetch = async (input, init) => {
  const url = typeof input === 'string' ? input : input.url;
  try {
    const response = await realFetch(input, init);
    calls.push({ url, status: response.status });
    return response;
  } catch (err) {
    calls.push({ url, status: 0, error: err?.message ?? String(err) });
    throw err;
  }
};

/** Calls to `path` since `from`, and the first one that was not 2xx. */
function since(from, path) {
  const made = calls.slice(from).filter(c => c.url.includes(path));
  return { made, bad: made.find(c => c.status < 200 || c.status >= 300) };
}

try {
  const config = { apiKey, baseUrl: apiUrl, timeoutMs: 10_000 };
  const user = { userId, attributes: { source: 'contract-smoke' } };

  // 1. Assign through ServerClient twice. ServerClient caches successful results
  //    per instance, so clearCache() forces the second call to hit the server and
  //    "sticky" reflects the server's answer rather than the local cache.
  const server = new ServerClient(config);
  const first = await server.assignExperiment(experimentKey, user);
  if (first.error) fail(`assignExperiment: ${first.error.message}`);
  server.clearCache();
  const second = await server.assignExperiment(experimentKey, user);
  if (second.error) fail(`assignExperiment (second): ${second.error.message}`);

  if (!['control', 'treatment'].includes(first.variantName)) {
    fail(`unexpected variant_name ${JSON.stringify(first.variantName)}`);
  }
  const sticky =
    first.variantName === second.variantName && first.variantId === second.variantId;
  if (!sticky) fail(`assignment not sticky: ${first.variantName} then ${second.variantName}`);

  // 2. Evaluate the flag through ServerClient (100% on in the seeded data), and
  //    through getAll() — the API a Next.js loader actually calls.
  const flag = await server.evaluateFeatureFlag(flagKey, user);
  if (flag.error) fail(`evaluateFeatureFlag: ${flag.error.message}`);
  if (typeof flag.isEnabled !== 'boolean') fail('flag.isEnabled is not a boolean');
  const all = await server.getAll([flagKey], user);
  if (all[flagKey]?.isEnabled !== flag.isEnabled) {
    fail('getAll disagrees with evaluateFeatureFlag');
  }

  // 3. Tracking lives on the browser client (ServerClient has no track). Prime its
  //    caches through the same public API the provider uses, then track.
  const client = new ExperimentationClient(config);
  await client.assignExperiment(user, experimentKey);
  await client.evaluateFeatureFlagDetailed(user, flagKey);

  let mark = calls.length;
  await client.trackEvent(userId, 'purchase', { sdk: 'react' }, { experimentKey, value: 12.5 });
  const track = since(mark, '/api/v1/tracking/track');
  if (track.made.length !== 1) fail(`expected one /tracking/track call, saw ${track.made.length}`);
  if (track.bad) fail(`track returned HTTP ${track.bad.status}`);

  // 4. Track without a key: fans out to the cached assignment + evaluated flag
  //    through POST /api/v1/tracking/batch.
  if (client.getAssignments(userId).length !== 1 || client.getEvaluatedFlags(userId).length !== 1) {
    fail('cache does not hold one assignment and one flag; fan-out would send nothing');
  }
  mark = calls.length;
  await client.trackEvent(userId, 'page_view', { page: '/contract-smoke' });
  const fanout = since(mark, '/api/v1/tracking/batch');
  if (fanout.made.length !== 1) fail(`expected one /tracking/batch call, saw ${fanout.made.length}`);
  if (fanout.bad) fail(`fan-out returned HTTP ${fanout.bad.status}`);

  process.stdout.write(
    JSON.stringify({
      sdk: 'react',
      assign: { variant_name: second.variantName, is_control: second.isControl, sticky },
      flag: { enabled: flag.isEnabled },
      track: { ok: true },
      fanout: { ok: true },
    }) + '\n'
  );
} catch (err) {
  fail(err && err.message ? err.message : String(err));
}
