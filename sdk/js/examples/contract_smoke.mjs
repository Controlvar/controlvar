/**
 * Contract smoke for the JavaScript SDK (see docs/sdk-guide.md, "Contract smoke").
 *
 * Run from the repo root:
 *   cd sdk/js && npm run build --silent && CONTROLVAR_API_KEY=... node examples/contract_smoke.mjs
 *
 * Env: CONTROLVAR_API_URL (default http://localhost:8000), CONTROLVAR_API_KEY (required),
 * (the former names EXPERIMENTLY_API_URL / EXPERIMENTLY_API_KEY are read when these are unset),
 *      CONTRACT_EXPERIMENT_KEY (default sdk_contract_ab), CONTRACT_FLAG_KEY (default sdk_contract_flag),
 *      CONTRACT_TARGETED_FLAG_KEY (default sdk_contract_targeted), CONTRACT_USER_ID (default smoke-<uuid>),
 *      CONTROLVAR_LOCAL_API_KEY (optional: a key with the sdk:ruleset scope; with it the smoke also
 *      links two ids with alias, step 7), CONTRACT_MULTIWAY_USERS (comma-separated users of the
 *      multi-way flag, step 6b; the live runner sets it).
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
const targetedKey = process.env.CONTRACT_TARGETED_FLAG_KEY || 'sdk_contract_targeted';
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

  // 4b2. One keyed event sent twice with one fixed eventId (#1543, C4). The server is to store
  //      it once: the runner counts the rows for this user and event name.
  const dedupName = `contract_dedup_${randomUUID()}`;
  const dedupId = `contract-${randomUUID()}`;
  for (let i = 0; i < 2; i++) {
    await client.track(userId, dedupName, { experimentKey, eventId: dedupId });
  }
  if (trackErrors.length) fail(`event_id: ${trackErrors[0]}`);

  // 4c. The attributes reach flag evaluation (#1520). The seeded targeted flag is at 0% with one
  //     rule, plan equals pro, at 100%. A NEW client (so the fan-out above is untouched) asks for
  //     it with plan=pro, then, after clearCache() (the cache key is user + flag), with plan=free.
  //     The runner requires on with the reason targeting_rule, then off.
  const targeting = new ExperimentationClient({ apiUrl, apiKey });
  const pro = await targeting.evaluateFlag(targetedKey, { userId, attributes: { plan: 'pro' } });
  targeting.clearCache();
  const free = await targeting.evaluateFlag(targetedKey, { userId, attributes: { plan: 'free' } });

  // 5. The exposure hook (#1499): a NEW client with onExposure assigns the same user twice,
  //    with no clearCache() in between. The server records an exposure for the first request
  //    (assigned: true) and the second answer comes from the cache, so the hook is called once.
  //    The runner checks the count, the experiment key and the variant; the user id is checked
  //    here, the one place that knows it.
  const exposures = [];
  const hooked = new ExperimentationClient({ apiUrl, apiKey, onExposure: e => exposures.push({ ...e }) });
  await hooked.getAssignment(experimentKey, { userId });
  await hooked.getAssignment(experimentKey, { userId });
  const wrongUser = exposures.find(e => e.userId !== userId);
  if (wrongUser) fail(`onExposure received user ${JSON.stringify(wrongUser.userId)}, expected ${JSON.stringify(userId)}`);

  // 5b. A hook that returns a rejected Promise: the client must handle the rejection (in plain
  //     node an unhandled one ends this process with exit 1) and pass it to onError as
  //     'exposure' with code HOOK_ERROR, the answer unchanged.
  const hookErrors = [];
  const rejecting = new ExperimentationClient({
    apiUrl,
    apiKey,
    onExposure: () => Promise.reject(new Error('async boom')),
    onError: (error, operation) => hookErrors.push(`${operation}/${error.code}`),
  });
  const fromRejecting = await rejecting.getAssignment(experimentKey, { userId });
  await new Promise(resolve => setTimeout(resolve, 0));
  if (fromRejecting.variantName !== second.variantName) {
    fail(`the client with a rejecting hook answered ${fromRejecting.variantName}, not ${second.variantName}`);
  }
  if (hookErrors.join() !== 'exposure/HOOK_ERROR') {
    fail(`the rejecting hook was reported as ${JSON.stringify(hookErrors)}, expected ["exposure/HOOK_ERROR"]`);
  }

  // 6. Flag values (#1810): a NEW client (so the fan-out above is untouched) reads the seed's
  //    string, number and json flags, each on at 100%, with the typed getters. Each default is
  //    no flag value, so a getter that falls back shows. The report is what they returned; the
  //    runner requires the seed's on values.
  const valued = new ExperimentationClient({ apiUrl, apiKey });
  const values = {
    string: await valued.getStringValue('sdk_contract_value_string', { userId }, 'smoke-default'),
    number: await valued.getNumberValue('sdk_contract_value_number', { userId }, -1),
    json: await valued.getJsonValue('sdk_contract_value_json', { userId }, ['smoke-default']),
  };

  // 6b. Multi-way flags (#2012): the same client reads the seed's split flag with the string
  //     getter for each user the runner gives in CONTRACT_MULTIWAY_USERS (one per value of the
  //     split), and the flag whose rule serves a value of its own for this user with plan=pro.
  //     The report is what the getter returned; the runner requires each user's value of the
  //     split, and the rule's own value.
  const multiway = { users: {}, rule: null };
  for (const id of (process.env.CONTRACT_MULTIWAY_USERS || '').split(',').filter(Boolean)) {
    multiway.users[id] = await valued.getStringValue('sdk_contract_multiway', { userId: id }, 'smoke-default');
  }
  multiway.rule = await valued.getStringValue(
    'sdk_contract_rule_value',
    { userId, attributes: { plan: 'pro' } },
    'smoke-default'
  );

  // 7. Two ids of one person (#449), only with the seed's sdk:ruleset key, which the live runner
  //    gives every smoke as CONTROLVAR_LOCAL_API_KEY. A NEW client with that key assigns a fresh
  //    anonymous id, links it to a fresh known id twice (created true, then false), sends one
  //    key-less event under the known id before anything assigned it (the client copied the
  //    anonymous id's experiment), then assigns the known id: the anonymous id's variant. The
  //    runner then reads the database: one assignment row among the two ids, the anonymous id's,
  //    and the event stored under it with the experiment and that variant.
  const linkingKey = process.env.CONTROLVAR_LOCAL_API_KEY || process.env.EXPERIMENTLY_LOCAL_API_KEY;
  let alias;
  if (linkingKey) {
    const linkErrors = [];
    const linking = new ExperimentationClient({
      apiUrl,
      apiKey: linkingKey,
      onError: (error, operation) => linkErrors.push(`${operation}: ${error.message}`),
    });
    const anonymousId = `smoke-anon-${randomUUID()}`;
    const knownId = `smoke-known-${randomUUID()}`;
    const before = await linking.getAssignment(experimentKey, { userId: anonymousId });
    const created = [await linking.alias(anonymousId, knownId), await linking.alias(anonymousId, knownId)];
    const eventName = `contract_alias_${randomUUID()}`;
    await linking.track(knownId, eventName, { value: 1 });
    if (linkErrors.length) fail(`alias: ${linkErrors[0]}`);
    const after = await linking.getAssignment(experimentKey, { userId: knownId });
    if (after.variantName !== before.variantName) {
      fail(`alias: the known id was answered ${after.variantName}, the anonymous id ${before.variantName}`);
    }
    alias = {
      anonymous_id: anonymousId,
      user_id: knownId,
      created,
      variant_before: before.variantName,
      variant_after: after.variantName,
      event_name: eventName,
    };
  }

  const [exposure] = exposures;
  process.stdout.write(
    JSON.stringify({
      sdk: 'js',
      assign: { variant_name: second.variantName, is_control: second.isControl, sticky },
      flag: { enabled: flag.enabled },
      track: { ok: true },
      fanout: { ok: true },
      targeted: { pro: pro.enabled, free: free.enabled, reason: pro.reason ?? null },
      event_id: { user_id: userId, event_name: dedupName, id: dedupId, sends: 2 },
      exposure: {
        calls: exposures.length,
        payload: exposure
          ? {
              experiment_key: exposure.experimentKey,
              variant_name: exposure.variantName,
              variant_id: exposure.variantId,
              user_id: exposure.userId,
            }
          : null,
      },
      values,
      multiway,
      ...(alias ? { alias } : {}),
    }) + '\n'
  );
} catch (err) {
  fail(err && err.message ? err.message : String(err));
}
