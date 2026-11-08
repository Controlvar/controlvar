/**
 * `/auth/callback` -- where the Cognito Hosted UI returns a sign-in (#708).
 *
 * A bare route (`BARE_ROUTES` in `@/utils/routes`), like `/sso/complete`: no
 * shell, and no `RequireAuth` bounce that would carry the query to `/login`.
 *
 * Once per page load (a module-scope latch, so React StrictMode's second
 * mount starts nothing), in this order:
 *
 * 1. the started sign-in is taken out of `sessionStorage`, so this code can
 *    be exchanged once at most;
 * 2. the query is taken out of the address bar (`history.replaceState`);
 * 3. the answer is checked: an `error`, no code, no started sign-in, an old
 *    one or a `state` that is not its own sends the browser to `/login`;
 * 4. the API exchanges the code and the verifier
 *    (`POST /api/v1/auth/cognito/exchange`); only the access token is kept;
 * 5. `GET /api/v1/users/me` with that token: only when it answers is the
 *    token stored and the user signed in.
 *
 * Nothing in the query but `code`, `state` and the `error` code is read, and
 * nothing of it is shown: `/login` says what happened in its own words. A
 * user the API refuses goes through Cognito's `/logout` on the way there.
 * The page asks for no referrer (`<meta name="referrer">`).
 */
import React, { useEffect } from 'react';
import Head from 'next/head';
import { useRouter } from 'next/router';
import { PageTitle } from '@/components/PageTitle';
import { useAuth } from '@/contexts/AuthContext';
import { useAuthConfig } from '@/contexts/AuthConfigContext';
import { ApiError, LOGIN_PATH, apiFetch, setToken } from '@/services/api';
import {
  AFTER_SIGN_IN_DEFAULT,
  CALLBACK_PATH,
  SignInNotice,
  SignInNoticeCode,
  beginSilentSignIn,
  endRefusedSignIn,
  hostedUiRedirectUri,
  signInFailureNotice,
  silentSignInAllowed,
  takePendingSignIn,
  writeSignInNotice,
} from '@/services/hostedUi';

export const CODE_EXCHANGE_PATH = '/api/v1/auth/cognito/exchange';
export const USERS_ME_PATH = '/api/v1/users/me';

export type CallbackOutcome =
  | { kind: 'signed-in'; next: string }
  | { kind: 'notice'; notice: SignInNotice }
  | { kind: 'refused'; notice: SignInNotice };

let latch: Promise<CallbackOutcome> | null = null;

/** Test hook: forget the page load's sign-in. */
export function __resetCallbackLatch(): void {
  latch = null;
}

function notice(code: SignInNoticeCode, next: string): CallbackOutcome {
  return { kind: 'notice', notice: { code, next } };
}

/** The outcome of a request that failed for a reason other than a refusal. */
function failureNotice(err: unknown, next: string): CallbackOutcome {
  return { kind: 'notice', notice: signInFailureNotice(err, next) };
}

async function begin(): Promise<CallbackOutcome> {
  const search = window.location.search;
  // First: the started sign-in is gone before anything can use it.
  const pending = takePendingSignIn();
  window.history.replaceState(window.history.state, '', CALLBACK_PATH);

  const params = new URLSearchParams(search);
  const next = pending?.next ?? AFTER_SIGN_IN_DEFAULT;
  const error = params.get('error');
  if (error !== null) return notice(error === 'access_denied' ? 'cancelled' : 'expired', next);

  const code = params.get('code');
  const state = params.get('state');
  if (!pending || !code || !state || state !== pending.state) return notice('expired', next);

  let accessToken: string;
  try {
    const answer = await apiFetch<unknown>(CODE_EXCHANGE_PATH, {
      method: 'POST',
      json: { code, code_verifier: pending.verifier, redirect_uri: hostedUiRedirectUri() },
      auth: false,
      redirectOn401: false,
    });
    const token =
      answer && typeof answer === 'object' ? (answer as Record<string, unknown>).access_token : undefined;
    if (typeof token !== 'string' || !token) return notice('server_error', next);
    accessToken = token;
  } catch (err) {
    if (err instanceof ApiError && (err.status === 400 || err.status === 422)) {
      return notice('expired', next);
    }
    return failureNotice(err, next);
  }

  try {
    await apiFetch<unknown>(USERS_ME_PATH, {
      auth: false,
      redirectOn401: false,
      headers: { Authorization: `Bearer ${accessToken}` },
    });
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
      return { kind: 'refused', notice: { code: 'refused', next } };
    }
    if (err instanceof ApiError && err.status === 400) {
      return { kind: 'refused', notice: { code: 'inactive', next } };
    }
    return failureNotice(err, next);
  }

  // The API accepted the token: only now is it stored.
  setToken(accessToken);
  return { kind: 'signed-in', next };
}

function CallbackPage() {
  const router = useRouter();
  const { refresh } = useAuth();
  const { loading, config } = useAuthConfig();

  useEffect(() => {
    if (latch === null) latch = begin();
  }, []);

  useEffect(() => {
    if (loading) return undefined;
    let mounted = true;
    void latch?.then(async (outcome) => {
      if (!mounted) return;
      if (outcome.kind === 'signed-in') {
        await refresh();
        if (mounted) void router.replace(outcome.next);
        return;
      }
      if (outcome.kind === 'refused' && config.kind === 'cognito') {
        if (outcome.notice.code === 'refused' && silentSignInAllowed()) {
          try {
            await beginSilentSignIn(config, outcome.notice.next);
            return;
          } catch {
            /* cannot start one: end the sign-in below */
          }
        }
        endRefusedSignIn(config, outcome.notice);
        return;
      }
      writeSignInNotice(outcome.notice);
      void router.replace(LOGIN_PATH);
    });
    return () => {
      mounted = false;
    };
    // Once the configuration is known; the latch makes the sign-in once per page load.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading]);

  return (
    <>
      <Head>
        <meta name="referrer" content="no-referrer" />
      </Head>
      <PageTitle title="Signing in" />
      <main className="min-h-screen bg-slate-50 flex items-center justify-center px-4">
        <p role="status" data-testid="hosted-ui-callback" className="text-sm text-slate-600">
          Signing you in…
        </p>
      </main>
    </>
  );
}

export default CallbackPage;
