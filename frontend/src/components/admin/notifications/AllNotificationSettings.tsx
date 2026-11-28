import React, { useEffect, useState } from 'react';
import { isApiError } from '@/services/api';
import { AdminService } from '@/services/admin';
import { NotificationPreference } from '@/types/admin';

/**
 * "Everyone's settings" (#2277): every user's saved notification settings, from
 * `GET /api/v1/notifications/admin/preferences`, which answers role ADMIN only.
 *
 * Whoever may not read them is told so and the route is never called. Every
 * failure is fixed copy chosen by the answer's status, never the server's
 * `detail`; a status 0 shows the dashboard's own unreachable or time-out
 * sentence.
 */

export const EVERYONE_HEADING = "Everyone's settings";
export const EVERYONE_REFUSED = "Everyone's notification settings need the ADMIN role.";
export const EVERYONE_EMPTY = 'No one has saved notification settings yet.';
export const EVERYONE_LOADING = "Loading everyone's settings...";
export const EVERYONE_FAILED = "Could not load everyone's settings. Reload the page to try again.";

export function everyoneErrorMessage(err: unknown): string {
  if (isApiError(err)) {
    if (err.status === 0) return err.message;
    if (err.status === 403) return EVERYONE_REFUSED;
  }
  return EVERYONE_FAILED;
}

const onOff = (value: boolean) => (value ? 'On' : 'Off');

type State =
  | { kind: 'loading' }
  | { kind: 'failed'; message: string }
  | { kind: 'loaded'; rows: NotificationPreference[] };

interface AllNotificationSettingsProps {
  /** Whether the user may read them (`readsAllNotificationSettings`). */
  allowed: boolean;
}

export function AllNotificationSettings({ allowed }: AllNotificationSettingsProps) {
  const [state, setState] = useState<State>({ kind: 'loading' });

  useEffect(() => {
    if (!allowed) return;
    let current = true;
    AdminService.getAllNotificationPrefs()
      .then((rows) => current && setState({ kind: 'loaded', rows }))
      .catch((err: unknown) => current && setState({ kind: 'failed', message: everyoneErrorMessage(err) }));
    return () => {
      current = false;
    };
  }, [allowed]);

  return (
    <section aria-labelledby="everyone-heading">
      <h2 id="everyone-heading" className="text-lg font-semibold text-slate-800 mb-4">
        {EVERYONE_HEADING}
      </h2>
      {!allowed && (
        <p className="text-sm text-slate-700" data-testid="notification-settings-refused">
          {EVERYONE_REFUSED}
        </p>
      )}
      {allowed && state.kind === 'loading' && (
        <p className="text-slate-500" data-testid="notification-settings-loading">
          {EVERYONE_LOADING}
        </p>
      )}
      {allowed && state.kind === 'failed' && (
        <p role="alert" className="text-red-600" data-testid="notification-settings-error">
          {state.message}
        </p>
      )}
      {allowed && state.kind === 'loaded' && state.rows.length === 0 && (
        <p className="text-sm text-slate-700" data-testid="notification-settings-empty">
          {EVERYONE_EMPTY}
        </p>
      )}
      {allowed && state.kind === 'loaded' && state.rows.length > 0 && (
        <div className="overflow-x-auto" data-testid="notification-settings-all">
          <table className="w-full text-sm">
            <caption className="sr-only">Every user&apos;s notification settings</caption>
            <thead>
              <tr className="border-b border-slate-200">
                <th scope="col" className="text-left py-2 font-medium text-slate-700">User id</th>
                <th scope="col" className="text-left py-2 font-medium text-slate-700">Experiment started</th>
                <th scope="col" className="text-left py-2 font-medium text-slate-700">Experiment completed</th>
                <th scope="col" className="text-left py-2 font-medium text-slate-700">Safety rollback</th>
                <th scope="col" className="text-left py-2 font-medium text-slate-700">Rollout advanced</th>
                <th scope="col" className="text-left py-2 font-medium text-slate-700">Slack channel</th>
                <th scope="col" className="text-left py-2 font-medium text-slate-700">Email override</th>
              </tr>
            </thead>
            <tbody>
              {state.rows.map((row) => (
                <tr key={row.id} data-testid={`notification-settings-row-${row.user_id}`} className="border-b border-slate-100">
                  <th scope="row" className="py-2 text-left font-mono text-xs font-normal text-slate-700">{row.user_id}</th>
                  <td className="py-2">{onOff(row.notify_experiment_started)}</td>
                  <td className="py-2">{onOff(row.notify_experiment_completed)}</td>
                  <td className="py-2">{onOff(row.notify_safety_rollback)}</td>
                  <td className="py-2">{onOff(row.notify_rollout_advanced)}</td>
                  <td className="py-2">{row.slack_channel ?? 'None'}</td>
                  <td className="py-2">{row.email_override ?? 'None'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
