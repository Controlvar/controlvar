import React, { FormEvent, useCallback, useEffect, useId, useRef, useState } from 'react';
import { useRouter } from 'next/router';
import { INACTIVE_ACCOUNT_MESSAGE, SignInEnrolment, useAuth } from '@/contexts/AuthContext';
import { useAuthConfig } from '@/contexts/AuthConfigContext';
import { ApiError, safeNextPath, signInTimeoutMessage, unreachableMessage } from '@/services/api';
import type { AuthConfig, DemoAccount } from '@/services/authConfig';
import {
  DEMO_ENTER_FAILED,
  DEMO_ENTER_LABEL,
  DEMO_LOGIN_HEADING,
  DEMO_LOGIN_INTRO,
  DEMO_LOGIN_SMALL_PRINT,
  DEMO_UNREACHABLE,
  demoAppLinks,
  demoRateLimitedMessage,
} from '@/utils/demoMode';
import {
  SignInNotice,
  beginHostedUiSignIn,
  canStartHostedUiSignIn,
  safeAfterSignInPath,
  takeSignInNotice,
} from '@/services/hostedUi';
import { PageTitle } from '@/components/PageTitle';
import { Wordmark } from '@/components/Wordmark';
import { AuthenticatorKey } from '@/components/account/AuthenticatorKey';
import { RecoveryCodesPanel } from '@/components/account/RecoveryCodesPanel';
import { TwoStepCodeInput } from '@/components/account/TwoStepCodeInput';
import {
  CODE_STEP_LOCKED_TEXT,
  CONFIRM_CLOCK_HINT,
  ENROL_STEP_INTRO,
  LOST_DEVICE_HELP,
  SIGN_IN_TIMED_OUT_MESSAGE,
  TwoFactorEnrolment,
  UNAVAILABLE_TEXT,
  WRONG_CODE_DETAIL,
  WRONG_CODE_MESSAGE,
  WRONG_RECOVERY_CODE_MESSAGE,
  startSignInEnrolment,
} from '@/services/twoFactor';
// The SSO button and `/login?sso_error=` copy: the SSO module's; a core build
// resolves this to a stub that renders nothing.
import SsoSignIn from '@modules/components/sso/SsoSignIn';

export const DEFAULT_AFTER_LOGIN = '/experiments';

type FormState = 'idle' | 'submitting';

/** C1b's copy for a rate-limited sign-in; the SSO exchange's 429 shows it too. */
export const RATE_LIMITED_MESSAGE = 'Too many attempts. Please wait a moment and try again.';

/** The copy for a sign-in whose 2xx answer could not be read (#1039). */
export const SIGN_IN_UNREADABLE_MESSAGE =
  "Sign-in did not finish: the API's answer could not be read. Try again in a moment.";

/** The API's terse 400 when the request's Host is not one it answers on. */
export const INVALID_HOST_DETAIL = 'Invalid host header';

/**
 * The copy for that 400: the API rejected the address this dashboard was
 * opened on, so name that address and the settings that fix it. ALLOWED_HOSTS,
 * when set, replaces the hosts derived from PUBLIC_BASE_URL, so both are named.
 * Plain text, not links -- a link to this host is exactly what does not work.
 */
export function wrongHostMessage(): string {
  const loc = typeof window !== 'undefined' ? window.location : undefined;
  const host = loc?.host || 'this address';
  const origin = loc?.origin || 'the URL people open';
  return (
    `This Controlvar server isn't configured for the address you used (${host}). ` +
    `An administrator needs to set PUBLIC_BASE_URL to ${origin} ` +
    `(or add ${host} to ALLOWED_HOSTS, if that is set).`
  );
}

/** Map an API failure to the copy shown in the alert box. */
export function loginErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 400 && err.detail === INVALID_HOST_DETAIL) return wrongHostMessage();
    if (err.status === 401) return 'Email or password is incorrect.';
    if (err.status === 423) {
      return typeof err.detail === 'string' && err.detail
        ? err.detail
        : 'Too many failed attempts. Try again in a few minutes.';
    }
    if (err.status === 429) return RATE_LIMITED_MESSAGE;
    if (err.unreadableAnswer) return SIGN_IN_UNREADABLE_MESSAGE;
    if (err.timedOut) return signInTimeoutMessage();
    if (err.status === 0) return unreachableMessage();
    return err.message;
  }
  if (err instanceof Error && err.message) return err.message;
  return 'Something went wrong. Please try again.';
}

/**
 * The code step's 401 that means "start again": the mfa_token expired, or the
 * account can no longer finish signing in. Any 401 but a wrong code is one.
 */
export function codeStepExpired(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401 && err.detail !== WRONG_CODE_DETAIL;
}

/**
 * Map a failed code step (`POST /auth/mfa/verify`) to the copy in its alert.
 * Never `loginErrorMessage`'s: a 401 here is a wrong code, not a wrong
 * password. The cap's 423 shows the API's own sentence, which says waiting
 * will not help; the address lock's 423 shows its detail, as the password step
 * does.
 */
export function codeStepErrorMessage(err: unknown, recovery: boolean): string {
  if (err instanceof ApiError) {
    if (err.status === 401) {
      if (codeStepExpired(err)) return SIGN_IN_TIMED_OUT_MESSAGE;
      return recovery ? WRONG_RECOVERY_CODE_MESSAGE : WRONG_CODE_MESSAGE;
    }
    if (err.status === 423) {
      if (err.detail === CODE_STEP_LOCKED_TEXT) return CODE_STEP_LOCKED_TEXT;
      return typeof err.detail === 'string' && err.detail
        ? err.detail
        : 'Too many failed attempts. Try again in a few minutes.';
    }
    if (err.status === 503 && err.detail === UNAVAILABLE_TEXT) return UNAVAILABLE_TEXT;
    if (err.status === 422) {
      return recovery
        ? 'Enter one of your recovery codes as you saved it.'
        : 'Enter the 6-digit code your authenticator app shows.';
    }
    if (err.status === 429) return RATE_LIMITED_MESSAGE;
    if (err.status === 400 && err.detail === INVALID_HOST_DETAIL) return wrongHostMessage();
    if (err.unreadableAnswer) return SIGN_IN_UNREADABLE_MESSAGE;
    if (err.timedOut) return signInTimeoutMessage();
    if (err.status === 0) return unreachableMessage();
    return err.message;
  }
  if (err instanceof Error && err.message) return err.message;
  return 'Something went wrong. Please try again.';
}

/** The set-up a sign-in asks for could not go on, and starts again at the password. */
export const ENROL_RESTART_MESSAGE =
  'Setting up two-step sign-in could not be finished. Enter your email and password again.';

/**
 * The set-up a sign-in asks for (`POST /auth/mfa/enroll[/confirm]`) has
 * ended and sign-in starts again: the token expired, or the account changed
 * (any 401 but a wrong code), or no secret is waiting any more (409).
 */
export function enrolStepEnded(err: unknown): boolean {
  return codeStepExpired(err) || (err instanceof ApiError && err.status === 409);
}

/**
 * Map a refused first code during that set-up to the copy in its alert. A
 * wrong first code shows the phone's clock hint, where a wrong clock is first
 * met; everything else reads as at the code step.
 */
export function enrolStepErrorMessage(err: unknown): string {
  if (err instanceof ApiError && err.status === 401 && err.detail === WRONG_CODE_DETAIL) {
    return CONFIRM_CLOCK_HINT;
  }
  return codeStepErrorMessage(err, false);
}

function firstQueryValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

/**
 * `/login`. Which sign-in it offers comes from `GET /api/v1/auth/config`
 * (#708): the Cognito Hosted UI's one button when the API answers with a
 * well-formed Cognito configuration, and otherwise -- the local provider, or
 * any answer that is not one (an older API's 404, no answer, a page that is
 * not JSON) -- the email and password form. A read-only demo that publishes
 * its account gets one "Enter the demo" button and no form. Until the answer
 * arrives the page says it is loading and shows none of them.
 */
export default function LoginPage() {
  const { loading, config, demo } = useAuthConfig();
  if (loading) return <SignInLoading />;
  if (config.kind === 'local' && demo?.account) return <DemoSignIn account={demo.account} />;
  if (config.kind === 'local') return <LocalSignIn />;
  return <HostedUiSignIn config={config} />;
}

/** The demo sign-in's copy for a failure: never the API's own words. */
export function demoEnterErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 429) return demoRateLimitedMessage(err.retryAfter);
    if (err.status === 400 && err.detail === INVALID_HOST_DETAIL) return wrongHostMessage();
    if (err.unreadableAnswer) return DEMO_ENTER_FAILED;
    if (err.timedOut) return signInTimeoutMessage();
    if (err.status === 0 || err.status >= 500) return DEMO_UNREACHABLE;
  }
  return DEMO_ENTER_FAILED;
}

/**
 * `/login` on a read-only demo: what it is, one button that signs in with the
 * account `/auth/config` publishes (`POST /auth/login`, as the form would),
 * the two demo apps, and no password field. `?next=` is honoured.
 */
function DemoSignIn({ account }: { account: DemoAccount }) {
  const router = useRouter();
  const { login, status, sessionError } = useAuth();
  const [entering, setEntering] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const messageRef = useRef<HTMLDivElement>(null);
  // Only ever rendered in the browser (after `/auth/config` has answered), so
  // the page's own host is there to read.
  const links = demoAppLinks();

  const nextPath = safeNextPath(firstQueryValue(router.query.next), DEFAULT_AFTER_LOGIN);

  // Already signed in: go straight through.
  useEffect(() => {
    if (status === 'authenticated' && !entering && router.isReady) {
      void router.replace(nextPath);
    }
  }, [status, entering, router, nextPath]);

  useEffect(() => {
    if (error) messageRef.current?.focus();
  }, [error]);

  const enter = async () => {
    if (entering) return;
    setEntering(true);
    setError(null);
    try {
      const result = await login(account.email, account.password);
      if (result.status !== 'signed-in') {
        // The demo account cannot take a second step: demo mode refuses it.
        setError(DEMO_ENTER_FAILED);
        setEntering(false);
        return;
      }
      await router.replace(nextPath);
    } catch (err) {
      setError(demoEnterErrorMessage(err));
      setEntering(false);
    }
  };

  return (
    <>
      <PageTitle title="Live demo" />
      <main className="min-h-screen bg-slate-50 flex flex-col items-center justify-center px-4 py-12">
        <div className="w-full max-w-md">
          <div className="flex justify-center mb-8">
            <Wordmark href={null} />
          </div>

          <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-6 sm:p-8">
            <h1 className="text-xl font-semibold text-slate-900 mb-2">{DEMO_LOGIN_HEADING}</h1>
            <p className="text-sm text-slate-600 mb-6" data-testid="demo-login-intro">
              {DEMO_LOGIN_INTRO}
            </p>

            {!error && sessionError && (
              <div
                role="alert"
                data-testid="login-session-error"
                className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
              >
                {sessionError}
              </div>
            )}
            {error && (
              <div
                ref={messageRef}
                tabIndex={-1}
                role="alert"
                data-testid="login-error"
                className="login-error mb-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
              >
                {error}
              </div>
            )}

            <button
              type="button"
              data-testid="demo-enter"
              onClick={() => void enter()}
              disabled={entering}
              aria-busy={entering}
              className="w-full min-h-[44px] inline-flex items-center justify-center gap-2 px-4 py-2 bg-blue-600 text-white text-base font-medium rounded-md hover:bg-blue-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
            >
              {entering ? 'Entering…' : DEMO_ENTER_LABEL}
            </button>

            {links.length > 0 && (
              <ul className="mt-6 space-y-2 text-sm" data-testid="demo-app-links">
                {links.map((link) => (
                  <li key={link.href}>
                    <a href={link.href} data-testid={link.testId} className="text-blue-700 underline hover:text-blue-900">
                      {link.label}
                    </a>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <p className="mt-6 text-center text-xs text-slate-500" data-testid="demo-login-small-print">
            {DEMO_LOGIN_SMALL_PRINT}
          </p>
        </div>
      </main>
    </>
  );
}

function SignInLoading() {
  return (
    <>
      <PageTitle title="Sign in" />
      <main className="min-h-screen bg-slate-50 flex flex-col items-center justify-center px-4 py-12">
        <p role="status" data-testid="login-loading" className="text-sm text-slate-600">
          Loading…
        </p>
      </main>
    </>
  );
}

/** The sign-in page's sentence for each notice; never text from a URL. */
export function signInNoticeMessage(notice: SignInNotice): string {
  switch (notice.code) {
    case 'signed_out':
      return SIGNED_OUT_MESSAGE;
    case 'cancelled':
      return SIGN_IN_CANCELLED_MESSAGE;
    case 'expired':
      return SIGN_IN_EXPIRED_MESSAGE;
    case 'refused':
      return SIGN_IN_REFUSED_MESSAGE;
    case 'inactive':
      return INACTIVE_ACCOUNT_MESSAGE;
    case 'rate_limited':
      return RATE_LIMITED_MESSAGE;
    case 'unreachable':
      return unreachableMessage();
    case 'timed_out':
      return signInTimeoutMessage();
    case 'server_error':
      return notice.status
        ? `Sign-in did not finish: the server answered with an error (HTTP ${notice.status}). Try again in a moment.`
        : 'Sign-in did not finish: the server answered with an error. Try again in a moment.';
    default:
      return SIGN_IN_EXPIRED_MESSAGE;
  }
}

export const SIGNED_OUT_MESSAGE = 'You have signed out.';
export const SIGN_IN_CANCELLED_MESSAGE = 'Sign-in was cancelled. You can try again.';
export const SIGN_IN_EXPIRED_MESSAGE = 'That sign-in link has expired or was already used. Start again.';
export const SIGN_IN_REFUSED_MESSAGE = 'Your account could not be signed in. Ask an administrator.';
export const SIGN_IN_NOT_SET_UP_MESSAGE =
  'Sign-in is not set up correctly on this server. Ask an administrator.';
export const SIGN_IN_NEEDS_HTTPS_MESSAGE =
  'Sign-in needs a secure connection. Open this page over https and try again.';
export const SIGN_IN_CANNOT_START_MESSAGE =
  'Sign-in could not start in this browser. Allow this site to store data for the session and try again.';

type CognitoSignInConfig = Exclude<AuthConfig, { kind: 'local' }>;

/** `/login` under Cognito: one button to the Hosted UI, and no form. */
function HostedUiSignIn({ config }: { config: CognitoSignInConfig }) {
  const router = useRouter();
  const { status, sessionError } = useAuth();
  const [notice, setNotice] = useState<SignInNotice | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [redirecting, setRedirecting] = useState(false);
  const messageRef = useRef<HTMLDivElement>(null);

  // What the last redirect left for this page: read once, then gone.
  useEffect(() => {
    const taken = takeSignInNotice();
    if (taken) setNotice(taken);
  }, []);

  const nextPath = safeAfterSignInPath(firstQueryValue(router.query.next) ?? notice?.next);

  useEffect(() => {
    if (status === 'authenticated' && !redirecting && router.isReady) {
      void router.replace(nextPath);
    }
  }, [status, redirecting, router, nextPath]);

  const signedOut = notice?.code === 'signed_out';
  const alertText =
    error ?? (notice && !signedOut ? signInNoticeMessage(notice) : null) ?? sessionError ?? null;

  useEffect(() => {
    if (alertText) messageRef.current?.focus();
  }, [alertText]);

  const start = () => {
    if (redirecting || config.kind !== 'cognito') return;
    if (!canStartHostedUiSignIn()) {
      setError(SIGN_IN_NEEDS_HTTPS_MESSAGE);
      return;
    }
    setError(null);
    setRedirecting(true);
    beginHostedUiSignIn(config, nextPath).catch(() => {
      setRedirecting(false);
      setError(SIGN_IN_CANNOT_START_MESSAGE);
    });
  };

  return (
    <>
      <PageTitle title="Sign in" />
      <main className="min-h-screen bg-slate-50 flex flex-col items-center justify-center px-4 py-12">
        <div className="w-full max-w-sm">
          <div className="flex justify-center mb-8">
            <Wordmark href={null} />
          </div>

          <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-6 sm:p-8">
            <h1 className="text-xl font-semibold text-slate-900 mb-1">Sign in</h1>

            {config.kind === 'cognito-unconfigured' ? (
              <div
                ref={messageRef}
                tabIndex={-1}
                role="alert"
                data-testid="login-error"
                className="mt-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
              >
                {SIGN_IN_NOT_SET_UP_MESSAGE}
                {config.missing.length > 0 && (
                  <span className="mt-1 block" data-testid="login-missing-settings">
                    Not set: {config.missing.join(', ')}.
                  </span>
                )}
              </div>
            ) : (
              <>
                <p className="text-sm text-slate-500 mb-6">
                  You will sign in on your organisation&apos;s Cognito page.
                </p>
                {signedOut && !alertText && (
                  <div
                    role="status"
                    data-testid="login-signed-out"
                    className="mb-4 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700"
                  >
                    {SIGNED_OUT_MESSAGE}
                  </div>
                )}
                {alertText && (
                  <div
                    ref={messageRef}
                    tabIndex={-1}
                    role="alert"
                    data-testid="login-error"
                    className="login-error mb-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
                  >
                    {alertText}
                  </div>
                )}
                <button
                  type="button"
                  data-testid="hosted-ui-sign-in"
                  onClick={start}
                  aria-disabled={redirecting ? true : undefined}
                  aria-busy={redirecting ? true : undefined}
                  className="w-full inline-flex items-center justify-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-md hover:bg-blue-700 aria-disabled:opacity-60 aria-disabled:cursor-not-allowed transition-colors"
                >
                  {redirecting ? 'Redirecting…' : alertText ? 'Try again' : 'Sign in'}
                </button>
              </>
            )}
          </div>
        </div>
      </main>
    </>
  );
}

/**
 * `/login` under the local provider: the email and password form, then, for an
 * account with two-step sign-in on (#1812), the code step in the same card;
 * for an account whose role requires it and that has not set it up, the
 * set-up (add the key to an app, its first code, the recovery codes) before
 * the session starts. The `mfa_token` is held in this component's state only:
 * a reload, or "Back to sign-in", starts again at the password.
 */
function LocalSignIn() {
  const router = useRouter();
  const { login, verifySecondStep, confirmSignInEnrolment, status, sessionError } = useAuth();
  const emailId = useId();
  const passwordId = useId();
  const errorId = useId();
  const codeId = useId();
  const codeErrorId = useId();
  const codeIntroId = useId();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [formState, setFormState] = useState<FormState>('idle');
  const [error, setError] = useState<string | null>(null);
  // Set when the error in the alert came from an SSO sign-in: re-opens its form.
  const [ssoRetry, setSsoRetry] = useState<(() => void) | null>(null);
  const emailRef = useRef<HTMLInputElement>(null);

  // The code step. `mfaToken` set means the password was right and a code is
  // needed; it lives here and nowhere else.
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [recovery, setRecovery] = useState(false);
  const [code, setCode] = useState('');
  const [codeError, setCodeError] = useState<string | null>(null);
  const codeRef = useRef<HTMLInputElement>(null);
  // "Back to sign-in" gives focus back to the email field.
  const [backToPassword, setBackToPassword] = useState(0);

  // The set-up a required role is asked for. `enrolToken` set means the
  // password was right and two-step sign-in must be set up first; `enrolled`
  // holds the recovery codes and the sign-in to finish once they are saved.
  const enrolCodeId = useId();
  const enrolErrorId = useId();
  const [enrolToken, setEnrolToken] = useState<string | null>(null);
  const [enrolment, setEnrolment] = useState<TwoFactorEnrolment | null>(null);
  const [enrolCode, setEnrolCode] = useState('');
  const [enrolError, setEnrolError] = useState<string | null>(null);
  const [enrolled, setEnrolled] = useState<SignInEnrolment | null>(null);
  const enrolHeadingRef = useRef<HTMLHeadingElement>(null);
  const enrolCodeRef = useRef<HTMLInputElement>(null);

  const nextPath = safeNextPath(firstQueryValue(router.query.next), DEFAULT_AFTER_LOGIN);
  const codeStep = mfaToken !== null;
  const enrolStep = enrolToken !== null || enrolled !== null;

  // Already signed in (e.g. the user typed /login by hand): go straight through.
  useEffect(() => {
    if (status === 'authenticated' && formState === 'idle' && router.isReady) {
      void router.replace(nextPath);
    }
  }, [status, formState, router, nextPath]);

  useEffect(() => {
    // An SSO error focuses the SSO form's email instead (SsoSignIn does).
    if (error && !ssoRetry) emailRef.current?.focus();
  }, [error, ssoRetry]);

  useEffect(() => {
    if (backToPassword) emailRef.current?.focus();
  }, [backToPassword]);

  // The code step's field gets focus when the step opens, when it switches
  // between a code and a recovery code, and after a refused code.
  useEffect(() => {
    if (codeStep) codeRef.current?.focus();
  }, [codeStep, recovery, codeError]);

  // The set-up's heading gets focus when the key is shown and when the
  // recovery codes are.
  const enrolView = enrolled ? 'codes' : enrolment ? 'key' : null;
  useEffect(() => {
    if (enrolView) enrolHeadingRef.current?.focus();
  }, [enrolView]);

  // A refused first code gives focus back to the field, as at the code step.
  useEffect(() => {
    if (enrolError) enrolCodeRef.current?.focus();
  }, [enrolError]);

  const leaveCodeStep = () => {
    setMfaToken(null);
    setRecovery(false);
    setCode('');
    setCodeError(null);
  };

  const leaveEnrolStep = () => {
    setEnrolToken(null);
    setEnrolment(null);
    setEnrolCode('');
    setEnrolError(null);
    setEnrolled(null);
  };

  /** The set-up ended (expired, or the account changed): sign in again. */
  const restartFromEnrolStep = (message: string) => {
    leaveEnrolStep();
    setFormState('idle');
    setError(message);
  };

  const handleSsoError = useCallback((message: string | null, retry: (() => void) | null) => {
    setError(message);
    // A function in state must be wrapped, or React calls it as an updater.
    setSsoRetry(() => retry);
  }, []);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (formState === 'submitting') return;

    const trimmedEmail = email.trim();
    if (!trimmedEmail || !password) {
      setError('Enter your email and password.');
      return;
    }

    setFormState('submitting');
    setError(null);
    setSsoRetry(null);
    try {
      const result = await login(trimmedEmail, password);
      if (result.status === 'second-step') {
        // The password is not needed again: the code step starts.
        setPassword('');
        setShowPassword(false);
        setRecovery(false);
        setCode('');
        setCodeError(null);
        setMfaToken(result.mfaToken);
        setFormState('idle');
        return;
      }
      if (result.status === 'enrol-step') {
        // The password is not needed again: the set-up starts with a new
        // secret for the authenticator app.
        setPassword('');
        setShowPassword(false);
        setEnrolCode('');
        setEnrolError(null);
        setEnrolToken(result.mfaToken);
        try {
          setEnrolment(await startSignInEnrolment(result.mfaToken));
        } catch (err) {
          if (enrolStepEnded(err)) {
            restartFromEnrolStep(SIGN_IN_TIMED_OUT_MESSAGE);
            return;
          }
          leaveEnrolStep();
          setError(codeStepErrorMessage(err, false));
        }
        setFormState('idle');
        return;
      }
      await router.replace(nextPath);
    } catch (err) {
      setError(loginErrorMessage(err));
      setFormState('idle');
    }
  };

  const handleVerify = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (formState === 'submitting' || mfaToken === null) return;
    if (!code.trim()) {
      setCodeError(recovery ? 'Enter one of your recovery codes.' : 'Enter the 6-digit code from your authenticator app.');
      return;
    }
    setFormState('submitting');
    setCodeError(null);
    try {
      // Sent as typed: the API drops spaces and hyphens and ignores a
      // recovery code's letter case.
      await verifySecondStep(mfaToken, recovery ? { recovery_code: code } : { code });
      await router.replace(nextPath);
    } catch (err) {
      setFormState('idle');
      if (codeStepExpired(err)) {
        // The step ran out (or the account changed): sign in again.
        leaveCodeStep();
        setError(SIGN_IN_TIMED_OUT_MESSAGE);
        return;
      }
      setCode('');
      setCodeError(codeStepErrorMessage(err, recovery));
    }
  };

  const handleEnrolConfirm = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (formState === 'submitting' || enrolToken === null) return;
    if (!enrolCode.trim()) {
      setEnrolError('Enter the 6-digit code from your authenticator app.');
      return;
    }
    setFormState('submitting');
    setEnrolError(null);
    try {
      const done = await confirmSignInEnrolment(enrolToken, enrolCode);
      // The token is spent: only the codes and the sign-in to finish remain.
      setEnrolToken(null);
      setEnrolment(null);
      setEnrolCode('');
      setEnrolled(done);
      setFormState('idle');
    } catch (err) {
      if (enrolStepEnded(err)) {
        restartFromEnrolStep(
          err instanceof ApiError && err.status === 409 ? ENROL_RESTART_MESSAGE : SIGN_IN_TIMED_OUT_MESSAGE,
        );
        return;
      }
      setFormState('idle');
      setEnrolCode('');
      setEnrolError(enrolStepErrorMessage(err));
    }
  };

  const finishEnrolment = async () => {
    if (enrolled === null || formState === 'submitting') return;
    setFormState('submitting');
    try {
      enrolled.finish();
    } catch (err) {
      restartFromEnrolStep(loginErrorMessage(err));
      return;
    }
    await router.replace(nextPath);
  };

  const switchCodeKind = () => {
    setRecovery((value) => !value);
    setCode('');
    setCodeError(null);
  };

  const backToSignIn = () => {
    leaveCodeStep();
    leaveEnrolStep();
    setError(null);
    setBackToPassword((n) => n + 1);
  };

  const submitting = formState === 'submitting';

  if (enrolStep) {
    return (
      <>
        <PageTitle title="Sign in" />
        <main className="min-h-screen bg-slate-50 flex flex-col items-center justify-center px-4 py-12">
          <div className="w-full max-w-sm">
            <div className="flex justify-center mb-8">
              <Wordmark href={null} />
            </div>

            <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-6 sm:p-8">
              {enrolled ? (
                <section className="flex flex-col gap-4" data-testid="enrol-codes">
                  <h1
                    ref={enrolHeadingRef}
                    tabIndex={-1}
                    className="text-xl font-semibold text-slate-900 focus:outline-none"
                    data-testid="enrol-heading"
                  >
                    Save your recovery codes
                  </h1>
                  <p className="text-sm text-slate-600">
                    Two-step sign-in is on. From now on, signing in asks for a code from your authenticator app
                    after your password.
                  </p>
                  <RecoveryCodesPanel
                    codes={enrolled.recoveryCodes}
                    onDone={() => void finishEnrolment()}
                    busy={submitting}
                  />
                </section>
              ) : (
                <section className="flex flex-col gap-5" data-testid="enrol-step">
                  <div>
                    <h1
                      ref={enrolHeadingRef}
                      tabIndex={-1}
                      className="text-xl font-semibold text-slate-900 mb-1 focus:outline-none"
                      data-testid="enrol-heading"
                    >
                      Set up two-step sign-in
                    </h1>
                    <p className="text-sm text-slate-500" data-testid="enrol-intro">
                      {ENROL_STEP_INTRO}
                    </p>
                  </div>

                  {enrolment ? (
                    <>
                      <div className="flex flex-col gap-3">
                        <h2 className="text-base font-medium text-slate-900">
                          1. Add Controlvar to your authenticator app
                        </h2>
                        <AuthenticatorKey enrolment={enrolment} />
                      </div>

                      <form onSubmit={handleEnrolConfirm} noValidate data-testid="enrol-form" className="flex flex-col gap-3">
                        <h2 className="text-base font-medium text-slate-900">2. Enter the code it shows</h2>
                        {enrolError && (
                          <div
                            id={enrolErrorId}
                            role="alert"
                            data-testid="enrol-error"
                            className="login-error rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
                          >
                            {enrolError}
                          </div>
                        )}
                        <TwoStepCodeInput
                          ref={enrolCodeRef}
                          id={enrolCodeId}
                          label="Code from your authenticator app"
                          value={enrolCode}
                          onChange={setEnrolCode}
                          errorId={enrolError ? enrolErrorId : null}
                          readOnly={submitting}
                          testId="enrol-code-input"
                        />
                        <button
                          type="submit"
                          data-testid="enrol-submit"
                          disabled={submitting}
                          aria-busy={submitting}
                          className="w-full inline-flex items-center justify-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-md hover:bg-blue-700 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
                        >
                          {submitting ? 'Turning on…' : 'Turn on two-step sign-in'}
                        </button>
                      </form>
                    </>
                  ) : (
                    <p className="text-sm text-slate-600" data-testid="enrol-loading">
                      Loading...
                    </p>
                  )}

                  <div className="text-sm">
                    <button
                      type="button"
                      data-testid="enrol-back"
                      onClick={backToSignIn}
                      disabled={submitting}
                      className="text-slate-600 underline hover:text-slate-900 disabled:opacity-60"
                    >
                      Back to sign-in
                    </button>
                  </div>
                </section>
              )}
            </div>

            <p className="mt-6 text-center text-xs text-slate-500">
              No public sign-up. Accounts are created by an administrator.
            </p>
          </div>
        </main>
      </>
    );
  }

  if (codeStep) {
    return (
      <>
        <PageTitle title="Sign in" />
        <main className="min-h-screen bg-slate-50 flex flex-col items-center justify-center px-4 py-12">
          <div className="w-full max-w-sm">
            <div className="flex justify-center mb-8">
              <Wordmark href={null} />
            </div>

            <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-6 sm:p-8">
              <h1 className="text-xl font-semibold text-slate-900 mb-1" data-testid="mfa-heading">
                {recovery ? 'Enter a recovery code' : 'Enter your code'}
              </h1>
              <p id={codeIntroId} className="text-sm text-slate-500 mb-6" data-testid="mfa-intro">
                {recovery
                  ? 'Enter one of the recovery codes you saved when you turned on two-step sign-in. Each works once.'
                  : 'Open your authenticator app and enter the 6-digit code for Controlvar.'}
              </p>

              <form onSubmit={handleVerify} noValidate data-testid="mfa-form" aria-describedby={codeIntroId}>
                {codeError && (
                  <div
                    id={codeErrorId}
                    role="alert"
                    data-testid="mfa-error"
                    className="login-error mb-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
                  >
                    {codeError}
                  </div>
                )}

                <div className="mb-6">
                  <TwoStepCodeInput
                    ref={codeRef}
                    id={codeId}
                    label={recovery ? 'Recovery code' : 'Code'}
                    recovery={recovery}
                    value={code}
                    onChange={setCode}
                    errorId={codeError ? codeErrorId : null}
                    readOnly={submitting}
                    testId="mfa-code-input"
                  />
                </div>

                <button
                  type="submit"
                  data-testid="mfa-submit"
                  disabled={submitting}
                  aria-busy={submitting}
                  className="w-full inline-flex items-center justify-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-md hover:bg-blue-700 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
                >
                  {submitting && (
                    <span
                      aria-hidden="true"
                      className="inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin"
                    />
                  )}
                  {submitting ? 'Verifying…' : 'Verify'}
                </button>
              </form>

              <div className="mt-4 flex flex-col items-start gap-2 text-sm">
                <button
                  type="button"
                  data-testid="mfa-switch"
                  onClick={switchCodeKind}
                  disabled={submitting}
                  className="font-medium text-blue-700 underline hover:text-blue-900 disabled:opacity-60"
                >
                  {recovery ? 'Use a code from your authenticator app instead' : 'Use a recovery code instead'}
                </button>
                <button
                  type="button"
                  data-testid="mfa-back"
                  onClick={backToSignIn}
                  disabled={submitting}
                  className="text-slate-600 underline hover:text-slate-900 disabled:opacity-60"
                >
                  Back to sign-in
                </button>
              </div>

              <details className="mt-5 text-sm" data-testid="lost-device">
                <summary className="cursor-pointer text-slate-500 hover:text-slate-800 select-none">
                  Lost your phone and your recovery codes?
                </summary>
                <p className="mt-2 text-slate-600" data-testid="lost-device-help">
                  {LOST_DEVICE_HELP}
                </p>
              </details>
            </div>

            <p className="mt-6 text-center text-xs text-slate-500">
              No public sign-up. Accounts are created by an administrator.
            </p>
          </div>
        </main>
      </>
    );
  }

  return (
    <>
      <PageTitle title="Sign in" />
      <main className="min-h-screen bg-slate-50 flex flex-col items-center justify-center px-4 py-12">
        <div className="w-full max-w-sm">
          <div className="flex justify-center mb-8">
            <Wordmark href={null} />
          </div>

          <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-6 sm:p-8">
            <h1 className="text-xl font-semibold text-slate-900 mb-1">Sign in</h1>
            <p className="text-sm text-slate-500 mb-6">Use the account an administrator created for you.</p>

            <form onSubmit={handleSubmit} noValidate data-testid="login-form">
              {!error && sessionError && (
                <div
                  role="alert"
                  data-testid="login-session-error"
                  className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
                >
                  {sessionError}
                </div>
              )}
              {error && (
                <div
                  id={errorId}
                  role="alert"
                  data-testid="login-error"
                  className="login-error mb-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
                >
                  {error}
                  {ssoRetry && (
                    <button
                      type="button"
                      data-testid="sso-retry"
                      onClick={ssoRetry}
                      className="mt-2 block font-medium text-red-800 underline hover:text-red-900"
                    >
                      Start sign-in again
                    </button>
                  )}
                </div>
              )}

              <div className="mb-4">
                <label htmlFor={emailId} className="block text-sm font-medium text-slate-700 mb-1">
                  Email
                </label>
                <input
                  ref={emailRef}
                  id={emailId}
                  name="email"
                  type="email"
                  autoComplete="email"
                  autoFocus
                  required
                  readOnly={submitting}
                  aria-invalid={error ? true : undefined}
                  aria-describedby={error ? errorId : undefined}
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 read-only:bg-slate-50"
                />
              </div>

              <div className="mb-6">
                <label htmlFor={passwordId} className="block text-sm font-medium text-slate-700 mb-1">
                  Password
                </label>
                <div className="relative">
                  <input
                    id={passwordId}
                    name="password"
                    type={showPassword ? 'text' : 'password'}
                    autoComplete="current-password"
                    required
                    readOnly={submitting}
                    aria-invalid={error ? true : undefined}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className="w-full pl-3 pr-16 py-2 border border-slate-300 rounded-md text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 read-only:bg-slate-50"
                  />
                  <button
                    type="button"
                    data-testid="toggle-password"
                    aria-label={showPassword ? 'Hide password' : 'Show password'}
                    aria-pressed={showPassword}
                    onClick={() => setShowPassword((v) => !v)}
                    className="absolute inset-y-0 right-0 px-3 text-xs font-medium text-slate-500 hover:text-slate-800"
                  >
                    {showPassword ? 'Hide' : 'Show'}
                  </button>
                </div>
              </div>

              <button
                type="submit"
                data-testid="login-submit"
                disabled={submitting}
                aria-busy={submitting}
                className="w-full inline-flex items-center justify-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-md hover:bg-blue-700 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
              >
                {submitting && (
                  <span
                    aria-hidden="true"
                    className="inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin"
                  />
                )}
                {submitting ? 'Signing in…' : 'Sign in'}
              </button>
            </form>

            <SsoSignIn
              nextPath={nextPath}
              onError={handleSsoError}
              rateLimitedMessage={RATE_LIMITED_MESSAGE}
              unreachableMessage={unreachableMessage}
            />

            <details className="mt-5 text-sm">
              <summary className="cursor-pointer text-slate-500 hover:text-slate-800 select-none">
                Forgot password?
              </summary>
              <p className="mt-2 text-slate-600" data-testid="forgot-password-help">
                Ask an administrator to reset it.
              </p>
            </details>
          </div>

          <p className="mt-6 text-center text-xs text-slate-500">
            No public sign-up. Accounts are created by an administrator.
          </p>
        </div>
      </main>
    </>
  );
}
