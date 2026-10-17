import React, { FormEvent, useCallback, useEffect, useId, useRef, useState } from 'react';
import { useRouter } from 'next/router';
import { useAuth } from '@/contexts/AuthContext';
import { ApiError, safeNextPath, unreachableMessage } from '@/services/api';
import { PageTitle } from '@/components/PageTitle';
import { Wordmark } from '@/components/Wordmark';
// The SSO button and `/login?sso_error=` copy: the SSO module's; a core build
// resolves this to a stub that renders nothing.
import SsoSignIn from '@modules/components/sso/SsoSignIn';

export const DEFAULT_AFTER_LOGIN = '/experiments';

type FormState = 'idle' | 'submitting';

/** C1b's copy for a rate-limited sign-in; the SSO exchange's 429 shows it too. */
export const RATE_LIMITED_MESSAGE = 'Too many attempts. Please wait a moment and try again.';

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
    if (err.status === 0) return unreachableMessage();
    return err.message;
  }
  if (err instanceof Error && err.message) return err.message;
  return 'Something went wrong. Please try again.';
}

function firstQueryValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

export default function LoginPage() {
  const router = useRouter();
  const { login, status, sessionError } = useAuth();
  const emailId = useId();
  const passwordId = useId();
  const errorId = useId();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [formState, setFormState] = useState<FormState>('idle');
  const [error, setError] = useState<string | null>(null);
  // Set when the error in the alert came from an SSO sign-in: re-opens its form.
  const [ssoRetry, setSsoRetry] = useState<(() => void) | null>(null);
  const emailRef = useRef<HTMLInputElement>(null);

  const nextPath = safeNextPath(firstQueryValue(router.query.next), DEFAULT_AFTER_LOGIN);

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
      await login(trimmedEmail, password);
      await router.replace(nextPath);
    } catch (err) {
      setError(loginErrorMessage(err));
      setFormState('idle');
    }
  };

  const submitting = formState === 'submitting';

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
