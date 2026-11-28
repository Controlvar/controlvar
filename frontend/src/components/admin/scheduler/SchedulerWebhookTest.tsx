import React, { useState } from 'react';
import { isApiError } from '@/services/api';
import { AdminService } from '@/services/admin';
import { demoRefusalMessage } from '@/utils/demoMode';

/**
 * "Send a test notification through the scheduler's webhook" (#2277):
 * `POST /api/v1/scheduler/notify/test`, which sends one test message to the
 * webhook the server is configured with (`NOTIFICATION_WEBHOOK_URL`) and
 * answers `sent`, `failed` or `skipped` (no webhook is set). The page shows
 * it to superusers, which is who the route accepts.
 *
 * One request at a time: the button is disabled while one is out (the test
 * that sends two clicks fails without it). Every
 * answer is fixed copy; the server's `message` and `detail` never show.
 */

export const WEBHOOK_HEADING = 'Scheduler webhook';
export const WEBHOOK_BUTTON = "Send a test notification through the scheduler's webhook";
export const WEBHOOK_SENDING = 'Sending...';
export const WEBHOOK_HELP =
  'Sends one test message to the webhook this server is set up with. It needs superuser access.';
export const WEBHOOK_SENT = 'Test notification sent.';
export const WEBHOOK_FAILED =
  'The webhook did not accept the test notification. Check NOTIFICATION_WEBHOOK_URL and the server log.';
export const WEBHOOK_SKIPPED = 'No webhook is set: NOTIFICATION_WEBHOOK_URL is empty on the server.';
export const WEBHOOK_REFUSED = 'Sending a test notification needs superuser access.';
export const WEBHOOK_ERROR = 'Could not send the test notification. Try again.';

export function webhookErrorMessage(err: unknown): string {
  // The read-only demo refuses writes, with its own sentence.
  const demo = demoRefusalMessage(err);
  if (demo) return demo;
  if (isApiError(err)) {
    if (err.status === 0) return err.message;
    if (err.status === 403) return WEBHOOK_REFUSED;
  }
  return WEBHOOK_ERROR;
}

type Outcome = { tone: 'ok' | 'problem'; text: string };

/** The sentence for the route's `status`; an answer of another shape is an error. */
export function webhookOutcome(answer: { status?: unknown } | null | undefined): Outcome {
  switch (answer?.status) {
    case 'sent':
      return { tone: 'ok', text: WEBHOOK_SENT };
    case 'skipped':
      return { tone: 'ok', text: WEBHOOK_SKIPPED };
    case 'failed':
      return { tone: 'problem', text: WEBHOOK_FAILED };
    default:
      return { tone: 'problem', text: WEBHOOK_ERROR };
  }
}

export function SchedulerWebhookTest() {
  const [sending, setSending] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  const send = async () => {
    setSending(true);
    setOutcome(null);
    try {
      setOutcome(webhookOutcome(await AdminService.sendSchedulerTestNotification()));
    } catch (err) {
      setOutcome({ tone: 'problem', text: webhookErrorMessage(err) });
    } finally {
      setSending(false);
    }
  };

  return (
    <section aria-labelledby="webhook-heading" className="rounded-md border border-slate-200 bg-white p-4">
      <h3 id="webhook-heading" className="text-base font-semibold text-slate-900">
        {WEBHOOK_HEADING}
      </h3>
      <p id="webhook-help" className="mt-1 text-sm text-slate-600" data-testid="scheduler-webhook-help">
        {WEBHOOK_HELP}
      </p>
      <button
        type="button"
        onClick={() => void send()}
        disabled={sending}
        aria-describedby="webhook-help"
        className="mt-3 px-3 py-1.5 rounded-md text-sm font-medium bg-slate-700 text-white hover:bg-slate-800 disabled:opacity-50"
        data-testid="scheduler-test-webhook"
      >
        {sending ? WEBHOOK_SENDING : WEBHOOK_BUTTON}
      </button>
      {outcome && (
        <p
          role={outcome.tone === 'ok' ? 'status' : 'alert'}
          className={`mt-3 text-sm ${outcome.tone === 'ok' ? 'text-slate-700' : 'text-red-700'}`}
          data-testid="scheduler-webhook-result"
        >
          {outcome.text}
        </p>
      )}
    </section>
  );
}
