/**
 * The dashboard's side of the API's demo mode: a read-only copy that refuses
 * every write except sign-in and the SDK's tracking.
 *
 * The API says it is a demo in `GET /api/v1/auth/config` (`"demo_mode": true`,
 * read by `AuthConfigContext`'s `useDemoMode`), and refuses a write with
 *
 *     403 {"detail": {"code": "demo_read_only", "message": "<sentence>"}}
 *
 * `ApiError` exposes `detail.code` as `code` and `detail.message` as its
 * message. The pages whose failures are fixed copy by status (segments,
 * experiment create, holdouts, exclusion groups, schedules, rollouts, flag
 * archiving, the password page, the user editor) ask
 * {@link demoRefusalMessage} first, so a demo refusal shows the API's
 * sentence and never "Your role cannot ...". It is the one server sentence
 * those pages show: the demo layer's own, chosen by its code.
 * `tests/services/demo-refusal-contract.test.ts` finds every file that maps
 * a 403 to its own copy and fails one that does not ask.
 *
 * The reset time is written here once and read by every sentence that names
 * it (`tests/utils/demoMode.test.ts` keeps it the only copy in the dashboard).
 */
import { isApiError } from '@/services/api';
import { docsUrl } from '@/services/docs';
import { REPO_URL } from '@/content/site-links';
import { SITE_ORIGIN } from '@/components/PageTitle';

/** The code of the API's demo refusal: `detail.code`. */
export const DEMO_READ_ONLY_CODE = 'demo_read_only';

/** The API's sentence, for a refusal that arrives without one. */
export const DEMO_READ_ONLY_MESSAGE = 'This is a read-only demo: creating and changing things is turned off.';

/** When the hosted demo is rebuilt from scratch, every night. */
export const DEMO_RESET_TIME = '08:00 UTC';

/** Where "Run your own copy" goes: the quick start. */
export const DEMO_RUN_YOUR_OWN_URL = docsUrl('getting-started/quick-start');

/** Where "Source" goes. */
export const DEMO_SOURCE_URL = REPO_URL;

/** The public site's walkthroughs: the setup a viewer cannot see, done on video. */
export const DEMO_WALKTHROUGHS_URL = `${SITE_ORIGIN}/walkthroughs`;

/** True for the API's demo refusal: a 403 whose `detail.code` is `demo_read_only`. */
export function isDemoRefusal(err: unknown): boolean {
  return isApiError(err) && err.status === 403 && err.code === DEMO_READ_ONLY_CODE;
}

/**
 * The sentence to show for the API's demo refusal (its own `detail.message`,
 * or {@link DEMO_READ_ONLY_MESSAGE} when it sent none), or null for any other
 * error.
 */
export function demoRefusalMessage(err: unknown): string | null {
  if (!isDemoRefusal(err) || !isApiError(err)) return null;
  const detail = err.detail as Record<string, unknown>;
  const message = typeof detail.message === 'string' ? detail.message.trim() : '';
  return message && message.length <= 500 ? message : DEMO_READ_ONLY_MESSAGE;
}

/** The demo note's subject on each kind of page (`components/DemoNote.tsx`). */
export const DEMO_EXPERIMENTS_ACTION = 'Creating and changing experiments';
export const DEMO_FLAGS_ACTION = 'Creating and changing flags';
export const DEMO_SEGMENTS_ACTION = 'Creating and changing segments';

/** The banner, for a visitor who is signed in. */
export const DEMO_BANNER_SIGNED_IN =
  'Read-only demo. You are signed in as a viewer, so nothing you do here is saved. ' +
  `The data is simulated and is rebuilt every night at ${DEMO_RESET_TIME}.`;

/** The banner on a page open without signing in (the docs, the power calculator). */
export const DEMO_BANNER_SIGNED_OUT =
  'Read-only demo. Nothing you do here is saved. ' +
  `The data is simulated and is rebuilt every night at ${DEMO_RESET_TIME}.`;

/** `/login` in demo mode. */
export const DEMO_LOGIN_HEADING = 'Controlvar live demo';
export const DEMO_LOGIN_INTRO =
  'A read-only copy of Controlvar. Two demo apps send it simulated traffic, so the numbers ' +
  'grow as they run; reload a page to see the latest. You enter as a viewer: open any ' +
  'experiment, result, flag or segment. Creating and changing things is turned off, and ' +
  `everything is rebuilt every night at ${DEMO_RESET_TIME}.`;
export const DEMO_ENTER_LABEL = 'Enter the demo';
export const DEMO_LOGIN_SMALL_PRINT =
  'The admin@demo.com account in the README is for the copy you run yourself and does not work here.';
export const DEMO_UNREACHABLE =
  `The demo is not answering. It is rebuilt every night at ${DEMO_RESET_TIME}, which takes a ` +
  'few minutes; otherwise try again shortly.';
export const DEMO_ENTER_FAILED = 'The demo could not sign you in. Try again shortly.';

/** A sign-in refused for too many attempts from one address. */
export function demoRateLimitedMessage(retryAfter?: number): string {
  return retryAfter !== undefined && retryAfter > 0
    ? `Too many people are entering from your network. Try again in ${retryAfter} ${retryAfter === 1 ? 'second' : 'seconds'}.`
    : 'Too many people are entering from your network. Try again in a minute.';
}

/** The two demo apps, on the dashboard host's `shoplab.` and `streampulse.` names. */
export interface DemoAppLink {
  label: string;
  href: string;
  testId: string;
}

/**
 * The demo apps' addresses. The box serves them beside the dashboard:
 * `demo.example.com` has `shoplab.demo.example.com` and
 * `streampulse.demo.example.com`. Built from the page's own host, so nothing
 * about a deployment is baked into the image.
 */
export function demoAppLinks(location?: Pick<Location, 'protocol' | 'host'> | null): DemoAppLink[] {
  const loc = location ?? (typeof window !== 'undefined' ? window.location : null);
  if (!loc || !loc.host) return [];
  const scheme = loc.protocol === 'http:' ? 'http:' : 'https:';
  return [
    {
      label: 'Open ShopLab, the demo storefront',
      href: `${scheme}//shoplab.${loc.host}/`,
      testId: 'demo-shoplab-link',
    },
    {
      label: 'Open StreamPulse, the demo phone app',
      href: `${scheme}//streampulse.${loc.host}/`,
      testId: 'demo-streampulse-link',
    },
  ];
}
