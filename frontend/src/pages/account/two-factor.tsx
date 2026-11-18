import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { PageTitle } from '@/components/PageTitle';
import { DemoNote } from '@/components/DemoNote';
import { TwoStepCodeInput } from '@/components/account/TwoStepCodeInput';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { useDemoMode } from '@/contexts/AuthConfigContext';
import {
  ENROL_PASSWORD_HINT,
  ENROL_PASSWORD_MESSAGE,
  NOT_LOCAL_TWO_FACTOR,
  NO_PASSWORD_TWO_FACTOR,
  QR_ALT,
  TwoFactorEnrolment,
  TwoFactorFailure,
  TwoFactorStatus,
  classifyTwoFactorError,
  confirmTwoFactor,
  disableTwoFactor,
  groupKey,
  groupRecoveryCode,
  qrCodeDataUrl,
  readTwoFactorStatus,
  recoveryCodesFileText,
  regenerateRecoveryCodes,
  startTwoFactorEnrolment,
  twoFactorFailureSentence,
} from '@/services/twoFactor';

const TITLE = 'Two-step sign-in';

/** What the page says once each change has gone through. */
export const ENABLED_SENTENCE =
  'Two-step sign-in is on. Signing in now asks for a code from your authenticator app after your password.';
export const DISABLED_SENTENCE = 'Two-step sign-in is off. Signing in asks for your password alone.';
export const REGENERATED_SENTENCE = 'Your new recovery codes are ready. The ones you had before no longer work.';
export const STATUS_LOAD_FAILED =
  'Your two-step sign-in could not be loaded. Reload the page to try again.';
export const CODES_SHOWN_ONCE =
  'These codes are shown once. Each signs in one time when your authenticator app is not at hand. ' +
  'Keep them somewhere safe, such as a password manager.';

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
const inputClass =
  'mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500';

function recoveryCodesLeft(count: number): string {
  if (count === 0) return 'No recovery codes left. Make a new set below.';
  return count === 1 ? '1 recovery code left.' : `${count} recovery codes left.`;
}

/**
 * Your own two-step sign-in (#1812; local sign-in only). Off: set it up in
 * three steps (add the key to an authenticator app by QR code or by typing it,
 * confirm with the app's first code, save the recovery codes). On: how many
 * recovery codes are left, a new set, or turn it off, each with a code.
 *
 * Every outcome is one of the fixed sentences in `@/services/twoFactor`; no
 * response text reaches the screen. The secret and the recovery codes live in
 * this page's state only, and a reload starts again (a new secret replaces one
 * still waiting).
 *
 * Setting it up asks for the current password, as changing the password does.
 * It is held in this page's state only until the API answers, and then
 * cleared, whatever the answer. An account with no password of its own is
 * told that two-step sign-in does not apply to it, in place of the form.
 */
export default function TwoFactorPage() {
  const auth = useOptionalAuth();
  const demo = useDemoMode();
  const isLocal = auth?.user?.auth_provider === 'local';
  const ready = !!auth && auth.status === 'authenticated' && isLocal && !demo;

  const codeId = useId();
  const messageId = useId();
  const savedId = useId();
  const passwordId = useId();
  const passwordHintId = useId();

  const [current, setCurrent] = useState<TwoFactorStatus | null>(null);
  const [loadFailure, setLoadFailure] = useState<string | null>(null);
  const [view, setView] = useState<View>('status');
  const [enrolment, setEnrolment] = useState<TwoFactorEnrolment | null>(null);
  // The QR image and the link it encodes: shown only beside that same secret,
  // so a slow first load for an earlier secret can never stand in for this one.
  const [qr, setQr] = useState<{ uri: string; src: string } | null>(null);
  const shownUri = useRef<string | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [codesFrom, setCodesFrom] = useState<'confirm' | 'regenerate'>('confirm');
  const [codesSaved, setCodesSaved] = useState(false);
  const [action, setAction] = useState<Action>(null);
  const [recovery, setRecovery] = useState(false);
  const [code, setCode] = useState('');
  // The current password for setting it up: never stored anywhere else.
  const [password, setPassword] = useState('');
  const [passwordRefused, setPasswordRefused] = useState(false);
  const [noPassword, setNoPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message | null>(null);
  const [copied, setCopied] = useState('');

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

  // The form is gone for an account with no password: focus goes to the heading.
  useEffect(() => {
    if (noPassword) headingRef.current?.focus();
  }, [noPassword]);

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
    if (failure.outcome === 'changed' || failure.outcome === 'not-waiting') {
      // The page was out of date: show how it is now.
      setView('status');
      setAction(null);
      setCode('');
      void loadStatus();
    }
  };

  const startEnrolment = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    if (!password) {
      setPasswordRefused(true);
      setMessage({ tone: 'error', text: ENROL_PASSWORD_MESSAGE });
      return;
    }
    setBusy(true);
    setMessage(null);
    setPasswordRefused(false);
    try {
      const started = await startTwoFactorEnrolment(password);
      setEnrolment(started);
      setQr(null);
      setCode('');
      setCopied('');
      setView('enrol');
      // The QR library is loaded only now. Without it the key and the link
      // still add the account.
      const uri = started.otpauth_uri;
      shownUri.current = uri;
      qrCodeDataUrl(uri).then(
        (src) => {
          // A QR drawn for a secret that has since been replaced is dropped.
          if (shownUri.current === uri) setQr({ uri, src });
        },
        () => undefined,
      );
    } catch (err) {
      const failure = classifyTwoFactorError(err);
      if (failure.outcome === 'no-password') {
        // Nothing to set up: the form gives way to the sentence, with no retry.
        setNoPassword(true);
      } else {
        setPasswordRefused(failure.outcome === 'password-incorrect' || failure.outcome === 'password-missing');
        fail(failure);
      }
    } finally {
      // The API has answered: the password is not kept, whatever the answer.
      setPassword('');
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
      shownUri.current = null;
      setQr(null);
      setCode('');
      setCodes(issued);
      setCodesFrom('confirm');
      setCodesSaved(false);
      setCopied('');
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
    shownUri.current = null;
    setQr(null);
    setCode('');
    setMessage(null);
    setView('status');
  };

  const finishCodes = () => {
    if (!codesSaved) return;
    const sentence = codesFrom === 'confirm' ? ENABLED_SENTENCE : REGENERATED_SENTENCE;
    setCodes(null);
    setCodesSaved(false);
    setCopied('');
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
        setCodesSaved(false);
        setCopied('');
        setView('codes');
      }
    } catch (err) {
      setCode('');
      fail(classifyTwoFactorError(err, { recovery }));
    } finally {
      setBusy(false);
    }
  };

  const copy = (text: string) => {
    const clipboard = typeof navigator !== 'undefined' ? navigator.clipboard : undefined;
    if (!clipboard) {
      setCopied('Copying is not available here. Select the text and copy it.');
      return;
    }
    clipboard.writeText(text).then(
      () => setCopied('Copied.'),
      () => setCopied('Copying is not available here. Select the text and copy it.'),
    );
  };

  const download = (list: string[]) => {
    if (typeof URL === 'undefined' || typeof URL.createObjectURL !== 'function') {
      setCopied('Downloading is not available here. Copy the codes instead.');
      return;
    }
    const url = URL.createObjectURL(new Blob([recoveryCodesFileText(list)], { type: 'text/plain' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'controlvar-recovery-codes.txt';
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
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

  const copyStatus = (
    <p role="status" data-testid="two-factor-copy-status" className="text-sm text-slate-600 min-h-[1.25rem]">
      {copied}
    </p>
  );

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
          <p className="text-sm text-slate-600">Scan this QR code with the app, or type the key below into it.</p>
          {qr && qr.uri === enrolment.otpauth_uri && (
            <img
              src={qr.src}
              alt={QR_ALT}
              width={192}
              height={192}
              data-testid="two-factor-qr"
              className="h-48 w-48 border border-slate-200 bg-white [image-rendering:pixelated]"
            />
          )}
          <div>
            <p className="text-sm font-medium text-slate-700">Key</p>
            <code
              data-testid="two-factor-key"
              className="mt-1 block break-all rounded-md bg-slate-100 px-3 py-2 font-mono text-sm tracking-wider text-slate-900 select-all"
            >
              {groupKey(enrolment.secret)}
            </code>
            <div className="mt-2 flex flex-wrap items-center gap-3">
              <button
                type="button"
                data-testid="two-factor-copy-key"
                onClick={() => copy(enrolment.secret)}
                className={buttonSecondary}
              >
                Copy key
              </button>
              <a
                href={enrolment.otpauth_uri}
                data-testid="two-factor-otpauth-link"
                className="text-sm font-medium text-blue-700 underline hover:text-blue-900"
              >
                Open in an authenticator app on this device
              </a>
            </div>
            {copyStatus}
          </div>
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
        <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900" data-testid="recovery-codes-warning">
          <strong>Important: </strong>
          {CODES_SHOWN_ONCE}
        </p>
        <ol data-testid="recovery-codes" className="grid grid-cols-1 gap-x-8 gap-y-1 sm:grid-cols-2 list-decimal pl-6">
          {codes.map((item) => (
            <li key={item} className="font-mono text-sm tracking-wider text-slate-900">
              <code>{groupRecoveryCode(item)}</code>
            </li>
          ))}
        </ol>
        <div className="flex flex-wrap gap-3">
          <button type="button" data-testid="recovery-codes-download" onClick={() => download(codes)} className={buttonSecondary}>
            Download as a text file
          </button>
          <button
            type="button"
            data-testid="recovery-codes-copy"
            onClick={() => copy(codes.map(groupRecoveryCode).join('\n'))}
            className={buttonSecondary}
          >
            Copy codes
          </button>
        </div>
        {copyStatus}
        <div className="flex items-center gap-2">
          <input
            id={savedId}
            type="checkbox"
            data-testid="recovery-codes-saved"
            checked={codesSaved}
            onChange={(event) => setCodesSaved(event.target.checked)}
            className="h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
          />
          <label htmlFor={savedId} className="text-sm font-medium text-slate-700">
            I have saved these codes
          </label>
        </div>
        <div>
          <button
            type="button"
            data-testid="recovery-codes-done"
            onClick={finishCodes}
            disabled={!codesSaved}
            className={buttonPrimary}
          >
            Done
          </button>
        </div>
      </section>
    );
  } else if (!current.enabled && noPassword) {
    body = (
      <section className="mt-6 flex flex-col gap-4" data-testid="two-factor-no-password">
        <h2 ref={headingRef} tabIndex={-1} className="text-lg font-semibold text-slate-900 focus:outline-none">
          Two-step sign-in is off
        </h2>
        <p role="status" className="text-sm text-slate-700" data-testid="two-factor-no-password-text">
          {NO_PASSWORD_TWO_FACTOR}
        </p>
      </section>
    );
  } else if (!current.enabled) {
    const describedBy = [passwordHintId, passwordRefused ? errorId : null].filter(Boolean).join(' ');
    body = (
      <section className="mt-6 flex flex-col gap-4" data-testid="two-factor-off">
        <h2 ref={headingRef} tabIndex={-1} className="text-lg font-semibold text-slate-900 focus:outline-none">
          Two-step sign-in is off
        </h2>
        {outcome}
        <p className="text-sm text-slate-600">
          When it is on, signing in asks for your password and then a 6-digit code from an authenticator app on
          your phone. If the phone is lost, a recovery code signs you in instead.
        </p>
        <form className="flex flex-col gap-3" onSubmit={startEnrolment} noValidate data-testid="two-factor-start-form">
          <div>
            <label htmlFor={passwordId} className="block text-sm font-medium text-slate-700">
              Current password
            </label>
            <p id={passwordHintId} className="text-sm text-slate-600">
              {ENROL_PASSWORD_HINT}
            </p>
            <input
              id={passwordId}
              type="password"
              autoComplete="current-password"
              aria-describedby={describedBy}
              aria-invalid={passwordRefused ? true : undefined}
              data-testid="two-factor-current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              readOnly={busy}
              className={inputClass}
            />
          </div>
          <div>
            <button type="submit" data-testid="two-factor-start" disabled={busy} className={buttonPrimary}>
              {busy ? 'Starting…' : 'Set up two-step sign-in'}
            </button>
          </div>
        </form>
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
        {action === null ? (
          <div className="flex flex-wrap gap-3">
            <button type="button" data-testid="two-factor-open-regenerate" onClick={() => openAction('regenerate')} className={buttonSecondary}>
              Make new recovery codes
            </button>
            <button type="button" data-testid="two-factor-open-disable" onClick={() => openAction('disable')} className={buttonSecondary}>
              Turn off two-step sign-in
            </button>
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
