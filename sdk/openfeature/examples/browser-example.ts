/**
 * Browser example: using ExperimentationProvider in a browser bundle.
 *
 * The provider only needs the global `fetch`; the API key is visible to end
 * users, so use a read-only SDK key. Evaluation is done by the server per user.
 */

import { OpenFeature, EvaluationContext } from '@openfeature/server-sdk';
import { ExperimentationProvider } from '../src/ExperimentationProvider';

// Initialize once at app startup (e.g., in index.tsx / main.ts).
export async function initFeatureFlags(userId: string): Promise<void> {
  const provider = new ExperimentationProvider({
    apiKey: 'your-public-client-api-key',
    baseUrl: 'https://api.yourplatform.com',
    cacheTtlMs: 5 * 60_000, // reuse each evaluation for 5 minutes
  });

  await OpenFeature.setProviderAndWait(provider);

  // Set a global context that is merged with per-call context.
  await OpenFeature.setContext({
    targetingKey: userId,
  });
}

// Use in a component.
async function renderHomePage(userId: string): Promise<void> {
  const client = OpenFeature.getClient();

  const ctx: EvaluationContext = { targetingKey: userId };

  // Only boolean flags carry a value: the evaluate endpoint returns `config: null`.
  const showNewHero = await client.getBooleanValue('new-hero-section', false, ctx);

  console.log(`User ${userId}:`);
  console.log(`  new-hero-section: ${showNewHero}`);
}

// Example usage.
initFeatureFlags('browser-user-789')
  .then(() => renderHomePage('browser-user-789'))
  .catch(console.error);
