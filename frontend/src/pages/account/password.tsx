import React, { useEffect, useId, useRef, useState } from 'react';
import { PageTitle } from '@/components/PageTitle';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { useDemoMode } from '@/contexts/AuthConfigContext';
import { DemoNote } from '@/components/DemoNote';
import {
  NOT_LOCAL_SENTENCE,
  PASSWORD_RULES,
  PasswordChangeResult,
  changeOwnPassword,
  classifyPasswordError,
  passwordOutcomeSentence,
} from '@/services/password';

const TITLE = 'Change password';

const inputClass =
  'mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500';

/**
 * Change your own password (local sign-in). Every outcome is shown as one of
 * the fixed sentences in `@/services/password`; no response text reaches the
 * screen.
 */
export default function ChangePasswordPage() {
  const auth = useOptionalAuth();
  // A read-only demo refuses the change: the page says so instead of the form.
  const demo = useDemoMode();
  const currentId = useId();
  const newId = useId();
  const confirmId = useId();
  const rulesId = useId();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<PasswordChangeResult | null>(null);
  const outcomeRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (result) outcomeRef.current?.focus();
  }, [result]);

  if (auth && auth.status === 'loading') {
    return (
      <div className="flex-1 bg-slate-50 p-8" data-testid="change-password-loading">
        <PageTitle title={TITLE} />
        <p className="text-sm text-slate-600">Loading...</p>
      </div>
    );
  }

  const isLocal = auth?.user?.auth_provider === 'local';

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving) return;
    if (!current) {
      setResult({ outcome: 'missing' });
      return;
    }
    if (next !== confirm) {
      setResult({ outcome: 'mismatch' });
      return;
    }
    setSaving(true);
    setResult(null);
    try {
      await changeOwnPassword(current, next);
      setCurrent('');
      setNext('');
      setConfirm('');
      setResult({ outcome: 'changed' });
    } catch (err) {
      setResult(classifyPasswordError(err));
    } finally {
      setSaving(false);
    }
  };

  const changed = result?.outcome === 'changed';

  return (
    <div className="flex-1 bg-slate-50" data-testid="change-password-page">
      <PageTitle title={TITLE} />
      <div className="max-w-md mx-auto px-4 py-8">
        <h1 className="text-2xl font-bold text-slate-900">{TITLE}</h1>

        {demo ? (
          <DemoNote action="Changing your password" walkthroughs={false} className="mt-4 text-sm text-slate-700" />
        ) : !isLocal ? (
          <p className="mt-4 text-sm text-slate-700" data-testid="change-password-not-local">
            {NOT_LOCAL_SENTENCE}
          </p>
        ) : (
          <form className="mt-6 flex flex-col gap-4" onSubmit={submit} noValidate>
            <p id={rulesId} className="text-sm text-slate-600" data-testid="change-password-rules">
              {PASSWORD_RULES}
            </p>

            <div>
              <label htmlFor={currentId} className="block text-sm font-medium text-slate-700">
                Current password
              </label>
              <input
                id={currentId}
                type="password"
                autoComplete="current-password"
                data-testid="current-password-input"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
                className={inputClass}
              />
            </div>

            <div>
              <label htmlFor={newId} className="block text-sm font-medium text-slate-700">
                New password
              </label>
              <input
                id={newId}
                type="password"
                autoComplete="new-password"
                aria-describedby={rulesId}
                data-testid="new-password-input"
                value={next}
                onChange={(e) => setNext(e.target.value)}
                className={inputClass}
              />
            </div>

            <div>
              <label htmlFor={confirmId} className="block text-sm font-medium text-slate-700">
                Confirm new password
              </label>
              <input
                id={confirmId}
                type="password"
                autoComplete="new-password"
                data-testid="confirm-password-input"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                className={inputClass}
              />
            </div>

            {result && (
              <div
                ref={outcomeRef}
                tabIndex={-1}
                role={changed ? 'status' : 'alert'}
                data-testid="change-password-outcome"
                data-outcome={result.outcome}
                className={[
                  'rounded-md border px-3 py-2 text-sm focus:outline-none',
                  changed
                    ? 'border-green-200 bg-green-50 text-green-800'
                    : 'border-red-200 bg-red-50 text-red-800',
                ].join(' ')}
              >
                {passwordOutcomeSentence(result)}
              </div>
            )}

            <div>
              <button
                type="submit"
                data-testid="change-password-submit"
                disabled={saving}
                className="px-4 py-2 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-60"
              >
                {saving ? 'Changing…' : TITLE}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
