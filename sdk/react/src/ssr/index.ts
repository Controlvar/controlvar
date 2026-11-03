/**
 * SSR utilities for the Controlvar React SDK.
 *
 * Use these utilities in server-side rendering contexts (Next.js getServerSideProps,
 * Remix loaders, etc.) where browser APIs are not available. ServerClient never throws.
 *
 * @example
 * import { ServerClient } from '@controlvar/react-sdk/ssr';
 *
 * export async function getServerSideProps({ req }) {
 *   const client = new ServerClient({ apiKey: process.env.API_KEY, baseUrl: process.env.API_URL });
 *   const user = { userId: req.session.userId };
 *   const flags = await client.getAll(['new-checkout', 'redesign'], user);
 *   const hero = await client.assignExperiment('shoplab_hero_banner', user);
 *   return { props: { flags, hero } };
 * }
 *
 * `client.bootstrap({ flags, experiments }, user)` returns the answers in the
 * form `ExperimentationProvider`'s `bootstrap` prop takes, so the page's first
 * render shows them.
 */
export { ServerClient } from '../client/ServerClient';
export type {
  SdkConfig,
  UserContext,
  FeatureFlagEvaluation,
  ExperimentAssignment,
  ExperimentationBootstrap,
  Exposure,
} from '../client/types';
