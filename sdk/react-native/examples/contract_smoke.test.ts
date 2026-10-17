/**
 * Contract smoke for the React Native SDK, run as a Jest test under Node.
 *
 * The SDK's client is plain TypeScript over `fetch` plus AsyncStorage, so it
 * needs no device, emulator or Metro bundler: Jest with `testEnvironment: node`
 * and the in-memory AsyncStorage mock is enough to drive it against a live
 * backend. That is why this is a Jest test rather than a standalone script —
 * the package ships no build output, and this reuses the ts-jest transform the
 * unit tests already use.
 *
 * Run from the repo root (or via tests/sdk-contract/live/run_live_contract.py):
 *   cd sdk/react-native && CONTROLVAR_API_KEY=... \
 *     npx jest --config jest.contract.config.js
 *
 * Env: CONTROLVAR_API_URL (default http://localhost:8000), CONTROLVAR_API_KEY
 *      (required), CONTRACT_EXPERIMENT_KEY (default sdk_contract_ab),
 *      CONTRACT_FLAG_KEY (default sdk_contract_flag), CONTRACT_USER_ID.
 *
 * On success the contract report is written to `.contract_smoke.json` (the live
 * runner reads the last stdout line, and Jest owns stdout) and echoed there.
 */
import { writeFileSync } from 'fs';
import { join } from 'path';
import { randomUUID } from 'crypto';

import { ExperimentationClient } from '../src/client';
import type { SwallowedOperation } from '../src/types';

const apiUrl =
  process.env.CONTROLVAR_API_URL || process.env.EXPERIMENTLY_API_URL || 'http://localhost:8000';
const apiKey = process.env.CONTROLVAR_API_KEY || process.env.EXPERIMENTLY_API_KEY || '';
const experimentKey = process.env.CONTRACT_EXPERIMENT_KEY || 'sdk_contract_ab';
const flagKey = process.env.CONTRACT_FLAG_KEY || 'sdk_contract_flag';
const userId = process.env.CONTRACT_USER_ID || `smoke-${randomUUID()}`;

const REPORT = join(__dirname, '..', '.contract_smoke.json');

describe('react-native SDK against a live backend', () => {
  // Every swallowed failure lands here; the assertions below require it to stay empty.
  const swallowed: string[] = [];
  const onError = (error: Error, operation: SwallowedOperation) =>
    swallowed.push(`${operation}: ${error.message}`);

  const config = { apiKey, baseUrl: apiUrl, timeoutMs: 10_000, onError };

  it('assigns stickily, evaluates the flag and tracks', async () => {
    expect(apiKey).not.toBe('');

    const client = new ExperimentationClient(config);

    // 1. Assign twice. The second client has offlineFallback off and its own
    //    in-memory cache, so its call reaches the server — "sticky" is the
    //    server's answer, not a local cache hit.
    const first = await client.getAssignment(experimentKey, userId, { source: 'contract-smoke' });
    expect(swallowed).toEqual([]);
    expect(first).not.toBeNull();
    expect(['control', 'treatment']).toContain(first!.variantName);

    const fresh = new ExperimentationClient({ ...config, offlineFallback: false });
    const second = await fresh.getAssignment(experimentKey, userId);
    expect(swallowed).toEqual([]);
    expect(second).not.toBeNull();
    const sticky =
      first!.variantName === second!.variantName && first!.variantId === second!.variantId;
    expect(sticky).toBe(true);

    // 2. Evaluate the flag (100% on in the seeded data).
    const flag = await client.evaluateFlag(flagKey, userId);
    expect(swallowed).toEqual([]);
    expect(typeof flag.enabled).toBe('boolean');

    // 3. Track a conversion attributed to the experiment (POST /api/v1/tracking/track).
    await client.track('purchase', userId, { sdk: 'react-native' }, { experimentKey, value: 12.5 });
    expect(swallowed).toEqual([]);

    // 4. Track without a key: fans out over the cached assignment + evaluated flag
    //    through POST /api/v1/tracking/batch.
    expect(client.getAssignments(userId)).toHaveLength(1);
    expect(client.getEvaluatedFlags(userId)).toHaveLength(1);
    await client.track('page_view', userId, { page: '/contract-smoke' });
    expect(swallowed).toEqual([]);

    // 4b. Explicit two-event batch — the server reports per-event results.
    const batch = await client.trackBatch([
      { userId, eventName: 'add_to_cart', experimentKey, value: 1 },
      { userId, eventName: 'flag_seen', featureFlagKey: flagKey },
    ]);
    expect(swallowed).toEqual([]);
    expect(batch.failureCount).toBe(0);
    expect(batch.successCount).toBe(2);

    writeFileSync(
      REPORT,
      JSON.stringify({
        sdk: 'react-native',
        assign: { variant_name: second!.variantName, is_control: second!.isControl, sticky },
        flag: { enabled: flag.enabled },
        track: { ok: true },
        fanout: { ok: true },
      }) + '\n'
    );
  });
});
