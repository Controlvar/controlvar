/**
 * @controlvar/openfeature-provider
 *
 * OpenFeature provider for Controlvar. Flag evaluation is
 * delegated to `@controlvar/js-sdk` and decided by the server.
 *
 * @example
 * ```typescript
 * import { OpenFeature } from '@openfeature/server-sdk';
 * import { ExperimentationProvider } from '@controlvar/openfeature-provider';
 *
 * const provider = new ExperimentationProvider({ apiKey: 'my-api-key', baseUrl: 'http://localhost:8000' });
 * await OpenFeature.setProviderAndWait(provider);
 *
 * const client = OpenFeature.getClient();
 * const enabled = await client.getBooleanValue('my-feature', false, { targetingKey: 'user-123' });
 *
 * // Experiments and tracking go through the underlying JS SDK client:
 * const variant = await provider.client.getVariant('checkout_flow', { userId: 'user-123' });
 * await provider.client.track('user-123', 'purchase', { value: 49.99, experimentKey: 'checkout_flow' });
 * ```
 */

export { ExperimentationProvider } from './ExperimentationProvider';
export type { ExperimentationProviderOptions, ExperimentationFlagMetadata, EvalReason } from './types';
export { ExperimentationClient, ExperimentationError } from '@controlvar/js-sdk';
export type { FlagEvaluation, Assignment, UserContext, TrackOptions } from '@controlvar/js-sdk';
