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

  // 3. Build user context. `targetingKey` reaches the server as user_id; every other
  //    key is sent as the user's attributes, for the flag's targeting rules.
  const userContext: EvaluationContext = { targetingKey: 'user-12345', plan: 'pro', country: 'US' };

  // 4. Evaluate flags — application code is SDK-agnostic.
  const darkModeEnabled = await client.getBooleanValue('dark-mode', false, userContext);
  console.log('dark-mode enabled:', darkModeEnabled);

  // A string, number or JSON flag serves its on value or its off value; a call of
  // another type than the flag's gets the default you pass, with TYPE_MISMATCH.
  const label = await client.getStringDetails('checkout-button-label', 'Buy now', userContext);
  console.log('checkout-button-label:', label.value, label.reason, label.variant);

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
