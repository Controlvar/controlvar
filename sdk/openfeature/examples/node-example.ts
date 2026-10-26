/**
 * Node.js example: using ExperimentationProvider with the OpenFeature Server SDK.
 *
 * Run with ts-node (after `npm run build`, which also builds ../js):
 *   ts-node examples/node-example.ts
 */

import { OpenFeature, EvaluationContext } from '@openfeature/server-sdk';
import { ExperimentationProvider } from '../src/ExperimentationProvider';

async function main(): Promise<void> {
  // 1. Create and register the provider.
  const provider = new ExperimentationProvider({
    apiKey: process.env.EP_API_KEY ?? 'your-api-key',
    baseUrl: process.env.EP_BASE_URL ?? 'http://localhost:8000',
    cacheTtlMs: 60_000, // reuse each user's evaluation for 1 minute
    timeout: 5_000,
  });

  await OpenFeature.setProviderAndWait(provider);

  // 2. Get a client.
  const client = OpenFeature.getClient('my-app');

  // 3. Build user context. Only `targetingKey` reaches the server (as user_id);
  //    other attributes are not used for flag evaluation.
  const userContext: EvaluationContext = { targetingKey: 'user-12345' };

  // 4. Evaluate flags — application code is SDK-agnostic.
  const darkModeEnabled = await client.getBooleanValue('dark-mode', false, userContext);
  console.log('dark-mode enabled:', darkModeEnabled);

  // Only boolean flags carry a value: the evaluate endpoint returns `config: null`
  // for every flag, so string, number and object calls return the default you pass.

  // 5. Experiments and tracking are not OpenFeature concepts: use the JS SDK client.
  const assignment = await provider.client.getAssignment('checkout_flow', {
    userId: 'user-12345',
    attributes: { country: 'US', plan: 'pro' },
  });
  console.log('checkout_flow variant:', assignment.variantName, assignment.configuration);

  await provider.client.track('user-12345', 'purchase', { value: 49.99, experimentKey: 'checkout_flow' });
  await provider.client.track('user-12345', 'page_view'); // no key: fanned out by the JS SDK's rule

  // 6. Clean up.
  await OpenFeature.close();
}

main().catch(console.error);
