import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { PageTitle } from '@/components/PageTitle';
import { DemoNote } from '@/components/DemoNote';
import { AuthenticatorKey } from '@/components/account/AuthenticatorKey';
import { RecoveryCodesPanel } from '@/components/account/RecoveryCodesPanel';
import { TwoStepCodeInput } from '@/components/account/TwoStepCodeInput';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { useDemoMode } from '@/contexts/AuthConfigContext';
import {
  NOT_LOCAL_TWO_FACTOR,
  REQUIRED_FOR_ROLE_MESSAGE,
  TwoFactorEnrolment,
  TwoFactorFailure,
  TwoFactorStatus,
  classifyTwoFactorError,
  confirmTwoFactor,
  disableTwoFactor,
  readTwoFactorStatus,
  regenerateRecoveryCodes,
  startTwoFactorEnrolment,
  twoFactorFailureSentence,
} from '@/services/twoFactor';

export { CODES_SHOWN_ONCE } from '@/components/account/RecoveryCodesPanel';

const TITLE = 'Two-step sign-in';

/** What the page says once each change has gone through. */
export const ENABLED_SENTENCE =
  'Two-step sign-in is on. Signing in now asks for a code from your authenticator app after your password.';
export const DISABLED_SENTENCE = 'Two-step sign-in is off. Signing in asks for your password alone.';
export const REGENERATED_SENTENCE = 'Your new recovery codes are ready. The ones you had before no longer work.';
export const STATUS_LOAD_FAILED =
  'Your two-step sign-in could not be loaded. Reload the page to try again.';
/** Off, while your role requires it (LOCAL_AUTH_2FA_REQUIRED_ROLES). */
export const REQUIRED_WHILE_OFF =
  'Your role requires two-step sign-in. Set it up now, or your next sign-in asks you to set it up first.';

type View = 'status' | 'enrol' | 'codes';
type Action = 'regenerate' | 'disable' | null;

interface Message {
  tone: 'ok' | 'error';
  text: string;
}

const buttonPrimary =
  'px-4 py-2 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-60 disabled:cursor-not-allowed';
const buttonSecondary =
  'px-4 py-2 rounded-md text-sm font-medium text-slate-700 bg-white border border-slate-300 hover:bg-slate-50 disabled:opacity-60';
const buttonDanger =
  'px-4 py-2 rounded-md text-sm font-medium bg-red-600 text-white hover:bg-red-700 disabled:opacity-60 disabled:cursor-not-allowed';

function recoveryCodesLeft(count: number): string {
  if (count === 0) return 'No recovery codes left. Make a new set below.';
  return count === 1 ? '1 recovery code left.' : `${count} recovery codes left.`;
}

/**
 * Your own two-step sign-in (#1812; local sign-in only). Off: set it up in
 * three steps (add the key to an authenticator app by QR code or by typing it,
 * confirm with the app's first code, save the recovery codes). On: how many
 * recovery codes are left, a new set, or turn it off, each with a code. When
 * your role requires it (`required`), Turn off is not offered and the page
 * says why.
 *
 * Every outcome is one of the fixed sentences in `@/services/twoFactor`; no
 * response text reaches the screen. The secret and the recovery codes live in
 * this page's state only, and a reload starts again (a new secret replaces one
 * still waiting).
 */
export default function TwoFactorPage() {
  const auth = useOptionalAuth();
  const demo = useDemoMode();
  const isLocal = auth?.user?.auth_provider === 'local';
  const ready = !!auth && auth.status === 'authenticated' && isLocal && !demo;

  const codeId = useId();
  const messageId = useId();

  const [current, setCurrent] = useState<TwoFactorStatus | null>(null);
  const [loadFailure, setLoadFailure] = useState<string | null>(null);
  const [view, setView] = useState<View>('status');
  const [enrolment, setEnrolment] = useState<TwoFactorEnrolment | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [codesFrom, setCodesFrom] = useState<'confirm' | 'regenerate'>('confirm');
  const [action, setAction] = useState<Action>(null);
  const [recovery, setRecovery] = useState(false);
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message | null>(null);

  const headingRef = useRef<HTMLHeadingElement>(null);
  const messageRef = useRef<HTMLDivElement>(null);
  const codeRef = useRef<HTMLInputElement>(null);
  const firstRender = useRef(true);

  const loadStatus = useCallback(async () => {
    try {
      const found = await readTwoFactorStatus();
      setCurrent(found);
      setLoadFailure(null);
    } catch (err) {
      const failure = classifyTwoFactorError(err);
      setLoadFailure(
        failure.outcome === 'not-local' || failure.outcome === 'session-ended' || failure.outcome === 'read-only'
          ? twoFactorFailureSentence(failure)
          : STATUS_LOAD_FAILED,
      );
    }
  }, []);

  useEffect(() => {
    if (ready) void loadStatus();
  }, [ready, loadStatus]);

  // A new view moves focus to its heading; an outcome moves it to the outcome.
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    headingRef.current?.focus();
  }, [view]);

  useEffect(() => {
    if (message) messageRef.current?.focus();
  }, [message]);

  useEffect(() => {
    if (action) codeRef.current?.focus();
  }, [action, recovery]);

  if (!auth || auth.status === 'loading') {
    return (
      <div className="flex-1 bg-slate-50 p-8" data-testid="two-factor-loading">
        <PageTitle title={TITLE} />
        <p className="text-sm text-slate-600">Loading...</p>
      </div>
    );
  }

  const fail = (failure: TwoFactorFailure) => {
    setMessage({ tone: 'error', text: twoFactorFailureSentence(failure) });
    if (failure.outcome === 'changed' || failure.outcome === 'not-waiting' || failure.outcome === 'required') {
      // The page was out of date: show how it is now.
      setView('status');
      setAction(null);
      setCode('');
      void loadStatus();
    }
  };

  const startEnrolment = async () => {
    if (busy) return;
    setBusy(true);
    setMessage(null);
    try {
      const started = await startTwoFactorEnrolment();
      setEnrolment(started);
      setCode('');
      setView('enrol');
    } catch (err) {
      fail(classifyTwoFactorError(err));
    } finally {
      setBusy(false);
    }
  };

  const confirm = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    if (!code.trim()) {
      setMessage({ tone: 'error', text: 'Enter the 6-digit code your authenticator app shows.' });
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const issued = await confirmTwoFactor(code);
      setEnrolment(null);
      setCode('');
      setCodes(issued);
      setCodesFrom('confirm');
      setView('codes');
    } catch (err) {
      setCode('');
      fail(classifyTwoFactorError(err, { confirming: true }));
    } finally {
      setBusy(false);
    }
  };

  const cancelEnrolment = () => {
    setEnrolment(null);
    setCode('');
    setMessage(null);
    setView('status');
  };

  // The panel calls this only once "I have saved these codes" is ticked.
  const finishCodes = () => {
    const sentence = codesFrom === 'confirm' ? ENABLED_SENTENCE : REGENERATED_SENTENCE;
    setCodes(null);
    setView('status');
    setMessage({ tone: 'ok', text: sentence });
    void loadStatus();
  };

  const openAction = (next: Action) => {
    setAction(next);
    setRecovery(false);
    setCode('');
    setMessage(null);
  };

  const submitAction = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy || !action) return;
    if (!code.trim()) {
      setMessage({
        tone: 'error',
        text: recovery ? 'Enter one of your recovery codes.' : 'Enter the 6-digit code your authenticator app shows.',
      });
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      if (action === 'disable') {
        await disableTwoFactor(recovery ? { recovery_code: code } : { code });
        setAction(null);
        setCode('');
        setMessage({ tone: 'ok', text: DISABLED_SENTENCE });
        void loadStatus();
      } else {
        const issued = await regenerateRecoveryCodes(code);
        setAction(null);
        setCode('');
        setCodes(issued);
        setCodesFrom('regenerate');
        setView('codes');
      }
    } catch (err) {
      setCode('');
      fail(classifyTwoFactorError(err, { recovery }));
    } finally {
      setBusy(false);
    }
  };

  const outcome = message && (
    <div
      id={messageId}
      ref={messageRef}
      tabIndex={-1}
      role={message.tone === 'ok' ? 'status' : 'alert'}
      data-testid="two-factor-outcome"
      className={[
        'rounded-md border px-3 py-2 text-sm focus:outline-none',
        message.tone === 'ok' ? 'border-green-200 bg-green-50 text-green-800' : 'border-red-200 bg-red-50 text-red-800',
      ].join(' ')}
    >
      {message.text}
    </div>
  );
  const errorId = message?.tone === 'error' ? messageId : null;

  let body: React.ReactNode;
  if (demo) {
    body = <DemoNote action="Setting up two-step sign-in" walkthroughs={false} className="mt-4 text-sm text-slate-700" />;
  } else if (!isLocal) {
    body = (
      <p className="mt-4 text-sm text-slate-700" data-testid="two-factor-not-local">
        {NOT_LOCAL_TWO_FACTOR}
      </p>
    );
  } else if (loadFailure) {
    body = (
      <p role="alert" className="mt-4 text-sm text-red-800" data-testid="two-factor-load-failed">
        {loadFailure}
      </p>
    );
  } else if (current === null) {
    body = (
      <p className="mt-4 text-sm text-slate-600" data-testid="two-factor-status-loading">
        Loading...
      </p>
    );
  } else if (view === 'enrol' && enrolment) {
    body = (
      <section className="mt-6 flex flex-col gap-6" data-testid="two-factor-enrol">
        <h2 ref={headingRef} tabIndex={-1} className="text-lg font-semibold text-slate-900 focus:outline-none">
          Set up two-step sign-in
        </h2>

        <div className="flex flex-col gap-3">
          <h3 className="text-base font-medium text-slate-900">1. Add Controlvar to your authenticator app</h3>
          <AuthenticatorKey enrolment={enrolment} />
        </div>

        <form className="flex flex-col gap-3" onSubmit={confirm} noValidate data-testid="two-factor-confirm-form">
          <h3 className="text-base font-medium text-slate-900">2. Enter the code it shows</h3>
          <p className="text-sm text-slate-600">Nothing changes at sign-in until this code is accepted.</p>
          <TwoStepCodeInput
            id={codeId}
            label="Code from your authenticator app"
            value={code}
            onChange={setCode}
            errorId={errorId}
            readOnly={busy}
            testId="two-factor-code-input"
          />
          {outcome}
          <div className="flex flex-wrap gap-3">
            <button type="submit" data-testid="two-factor-confirm" disabled={busy} className={buttonPrimary}>
              {busy ? 'Turning on…' : 'Turn on two-step sign-in'}
            </button>
            <button type="button" data-testid="two-factor-cancel" onClick={cancelEnrolment} disabled={busy} className={buttonSecondary}>
              Cancel
            </button>
          </div>
        </form>
      </section>
    );
  } else if (view === 'codes' && codes) {
    body = (
      <section className="mt-6 flex flex-col gap-4" data-testid="two-factor-codes">
        <h2 ref={headingRef} tabIndex={-1} className="text-lg font-semibold text-slate-900 focus:outline-none">
          Save your recovery codes
        </h2>
        <RecoveryCodesPanel codes={codes} onDone={finishCodes} />
      </section>
    );
  } else if (!current.enabled) {
    body = (
      <section className="mt-6 flex flex-col gap-4" data-testid="two-factor-off">
        <h2 ref={headingRef} tabIndex={-1} className="text-lg font-semibold text-slate-900 focus:outline-none">
          Two-step sign-in is off
        </h2>
        {outcome}
        {current.required && (
          <p className="text-sm font-medium text-slate-800" data-testid="two-factor-required">
            {REQUIRED_WHILE_OFF}
          </p>
        )}
        <p className="text-sm text-slate-600">
          When it is on, signing in asks for your password and then a 6-digit code from an authenticator app on
          your phone. If the phone is lost, a recovery code signs you in instead.
        </p>
        <div>
          <button type="button" data-testid="two-factor-start" onClick={() => void startEnrolment()} disabled={busy} className={buttonPrimary}>
            {busy ? 'Starting…' : 'Set up two-step sign-in'}
          </button>
        </div>
      </section>
    );
  } else {
    body = (
      <section className="mt-6 flex flex-col gap-4" data-testid="two-factor-on">
        <h2 ref={headingRef} tabIndex={-1} className="text-lg font-semibold text-slate-900 focus:outline-none">
          Two-step sign-in is on
        </h2>
        {outcome}
        <p className="text-sm text-slate-700" data-testid="recovery-codes-left">
          {recoveryCodesLeft(current.recovery_codes_remaining)}
        </p>
        {current.required && (
          <p className="text-sm text-slate-700" data-testid="two-factor-required">
            {REQUIRED_FOR_ROLE_MESSAGE}
          </p>
        )}
        {action === null ? (
          <div className="flex flex-wrap gap-3">
            <button type="button" data-testid="two-factor-open-regenerate" onClick={() => openAction('regenerate')} className={buttonSecondary}>
              Make new recovery codes
            </button>
            {!current.required && (
              <button type="button" data-testid="two-factor-open-disable" onClick={() => openAction('disable')} className={buttonSecondary}>
                Turn off two-step sign-in
              </button>
            )}
          </div>
        ) : (
          <form className="flex flex-col gap-3" onSubmit={submitAction} noValidate data-testid="two-factor-action-form">
            <h3 className="text-base font-medium text-slate-900">
              {action === 'disable' ? 'Turn off two-step sign-in' : 'Make new recovery codes'}
            </h3>
            <p className="text-sm text-slate-600">
              {action === 'disable'
                ? 'Signing in will ask for your password alone.'
                : 'You get a new set of 10. The codes you have now stop working.'}
            </p>
            <TwoStepCodeInput
              ref={codeRef}
              id={codeId}
              label={recovery ? 'Recovery code' : 'Code from your authenticator app'}
              recovery={recovery}
              value={code}
              onChange={setCode}
              errorId={errorId}
              readOnly={busy}
              testId="two-factor-code-input"
            />
            {action === 'disable' && (
              <div>
                <button
                  type="button"
                  data-testid="two-factor-switch"
                  onClick={() => {
                    setRecovery((value) => !value);
                    setCode('');
                  }}
                  disabled={busy}
                  className="text-sm font-medium text-blue-700 underline hover:text-blue-900 disabled:opacity-60"
                >
                  {recovery ? 'Use a code from your authenticator app instead' : 'Use a recovery code instead'}
                </button>
              </div>
            )}
            <div className="flex flex-wrap gap-3">
              <button
                type="submit"
                data-testid="two-factor-submit"
                disabled={busy}
                className={action === 'disable' ? buttonDanger : buttonPrimary}
              >
                {action === 'disable' ? 'Turn off two-step sign-in' : 'Make new recovery codes'}
              </button>
              <button
                type="button"
                data-testid="two-factor-cancel"
                onClick={() => openAction(null)}
                disabled={busy}
                className={buttonSecondary}
              >
                Cancel
              </button>
            </div>
          </form>
        )}
      </section>
    );
  }

  return (
    <div className="flex-1 bg-slate-50" data-testid="two-factor-page">
      <PageTitle title={TITLE} />
      <div className="max-w-xl mx-auto px-4 py-8">
        <h1 className="text-2xl font-bold text-slate-900">{TITLE}</h1>
        {body}
      </div>
    </div>
  );
}
