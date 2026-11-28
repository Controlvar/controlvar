import React, { useRef, useState } from 'react';
import { HoldoutsService } from '@/services/holdouts';
import { HOLDOUT_COPY, checkAnswer, checkErrorCopy } from '@/utils/holdouts';

/**
 * "Check a user id" on the global-holdouts page (#2276): `GET
 * /api/v1/holdout/check/{user_id}` for one id.
 *
 * - The id is matched exactly: it is sent as typed, never trimmed or case
 *   folded. Check is disabled while the field is empty.
 * - An id with a slash, and the ids `.` and `..` (a URL collapses them), never
 *   match the route, so they are refused here, at the field, before anything
 *   is sent.
 * - The answer names the id it was asked about; editing the field clears it.
 * - Every failure is fixed copy chosen by the answer's status.
 *
 * The page offers it only to the people the route accepts (the experiment
 * create check, `canReadEligibility`).
 */
export function HoldoutCheckForm() {
  const [userId, setUserId] = useState('');
  const [answer, setAnswer] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [invalid, setInvalid] = useState(false);
  const [busy, setBusy] = useState(false);
  // Only the latest request may write an answer.
  const latest = useRef(0);

  const change = (value: string) => {
    latest.current += 1;
    setUserId(value);
    setAnswer(null);
    setError(null);
    setInvalid(false);
    setBusy(false);
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (userId === '' || busy) return;
    if (userId.includes('/') || userId === '.' || userId === '..') {
      setError(HOLDOUT_COPY.checkIdSlash);
      setInvalid(true);
      return;
    }
    const mine = ++latest.current;
    setBusy(true);
    setAnswer(null);
    setError(null);
    setInvalid(false);
    try {
      const result = await HoldoutsService.check(userId);
      if (latest.current !== mine) return;
      setAnswer(checkAnswer(result));
    } catch (err) {
      if (latest.current !== mine) return;
      setError(checkErrorCopy(err));
      setInvalid(true);
    } finally {
      if (latest.current === mine) setBusy(false);
    }
  };

  return (
    <section
      aria-labelledby="holdout-check-heading"
      className="rounded-lg border border-slate-200 bg-white p-6 space-y-3"
    >
      <h2 id="holdout-check-heading" className="text-lg font-semibold text-slate-900">
        Check a user id
      </h2>
      <form
        onSubmit={(event) => void submit(event)}
        noValidate
        className="space-y-3"
        data-testid="holdout-check-form"
      >
        <div>
          <label htmlFor="holdout-check-user" className="block text-sm font-medium text-slate-700">
            User id
          </label>
          <input
            id="holdout-check-user"
            type="text"
            value={userId}
            onChange={(event) => change(event.target.value)}
            aria-invalid={invalid || undefined}
            aria-describedby={error ? 'holdout-check-help holdout-check-error' : 'holdout-check-help'}
            autoComplete="off"
            className="mt-1 w-full max-w-md rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-800"
            data-testid="holdout-check-user"
          />
          <p id="holdout-check-help" className="mt-1 text-xs text-slate-600">
            {HOLDOUT_COPY.checkHelp}
          </p>
        </div>
        <button
          type="submit"
          disabled={userId === '' || busy}
          className="px-3 py-1.5 rounded-md text-sm font-medium border transition-colors disabled:opacity-50 disabled:cursor-not-allowed bg-white text-slate-700 border-slate-300 hover:bg-slate-50"
          data-testid="holdout-check-submit"
        >
          {busy ? 'Checking…' : 'Check'}
        </button>
      </form>
      {answer && (
        <p role="status" className="text-sm text-slate-800" data-testid="holdout-check-answer">
          {answer}
        </p>
      )}
      {error && (
        <p
          id="holdout-check-error"
          role="alert"
          className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-800"
          data-testid="holdout-check-error"
        >
          {error}
        </p>
      )}
    </section>
  );
}
