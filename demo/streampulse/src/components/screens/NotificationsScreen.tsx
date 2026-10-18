import React, { useState } from 'react';
import { useExperiment } from '@controlvar/react-sdk';
import { EXPERIMENT_KEYS } from '@/lib/env';
import { useScreenView, useTrack } from '@/lib/eventLog';
import { Binding, ScreenHeader } from '@/components/ui';

export type PushFrequency = 'daily' | '3x_week';

export const FREQUENCY_LABEL: Record<PushFrequency, string> = {
  daily: 'Daily digest',
  '3x_week': '3× a week',
};

const SAMPLE: Record<PushFrequency, string[]> = {
  daily: ['New from Lumen Fields: "Neon Tide"', 'Your Monday mix is ready', 'Harbor & Vale just dropped a live session'],
  '3x_week': ['Your weekend mix is ready', 'New from Lumen Fields: "Neon Tide"'],
};

/**
 * Notifications. `streampulse_push_frequency` decides how often the app pushes
 * (`daily` control vs `three_weekly`). "Open notification" is the primary metric
 * (`notification_open`), "Uninstall app" the guardrail (`app_uninstall`) — the
 * experiment runs with sequential testing so it can stop early either way.
 */
export default function NotificationsScreen() {
  useScreenView('notifications');
  const track = useTrack();
  const assignment = useExperiment(EXPERIMENT_KEYS.pushFrequency);
  const frequency: PushFrequency = assignment.configuration?.frequency === '3x_week' ? '3x_week' : 'daily';
  const [opened, setOpened] = useState<number[]>([]);
  const [uninstalled, setUninstalled] = useState(false);

  const openNotification = (i: number) => {
    setOpened((o) => (o.includes(i) ? o : [...o, i]));
    track('notification_open', { frequency, index: i }, { experimentKey: EXPERIMENT_KEYS.pushFrequency });
  };
  const uninstall = () => {
    setUninstalled(true);
    track('app_uninstall', { frequency }, { experimentKey: EXPERIMENT_KEYS.pushFrequency });
  };

  return (
    <section aria-labelledby="notifications-title" data-testid="notifications" data-frequency={frequency}>
      <ScreenHeader id="notifications-title" title="Notifications" subtitle="Push settings" badge={<Binding>{EXPERIMENT_KEYS.pushFrequency}</Binding>} />
      <div className="rounded-xl bg-white/5 p-3">
        <div className="text-xs uppercase tracking-wide text-neutral-400">Push frequency</div>
        <div className="mt-0.5 text-lg font-semibold" data-testid="push-frequency">
          {FREQUENCY_LABEL[frequency]}
        </div>
        <p className="mt-1 text-xs text-neutral-400">
          Variant <span className="font-mono">{assignment.variantName}</span>
          {assignment.assigned === false && ' (not enrolled — default shown)'}
        </p>
      </div>

      {uninstalled ? (
        <div className="mt-4 rounded-xl border border-red-400/40 bg-red-500/10 p-3 text-sm" role="status">
          App uninstalled. <span className="text-neutral-400">(guardrail event sent)</span>
          <button type="button" className="btn-phone mt-2 w-full" onClick={() => setUninstalled(false)}>
            Reinstall
          </button>
        </div>
      ) : (
        <>
          <h3 className="mt-4 text-xs uppercase tracking-wide text-neutral-400">Recent pushes</h3>
          <ul className="mt-1 space-y-2">
            {SAMPLE[frequency].map((text, i) => (
              <li key={text} className="flex items-center justify-between gap-2 rounded-xl bg-white/5 p-2 text-sm">
                <span className={opened.includes(i) ? 'text-neutral-400' : ''}>{text}</span>
                <button type="button" className="btn-phone flex-none" onClick={() => openNotification(i)} aria-label={`Open notification: ${text}`}>
                  Open
                </button>
              </li>
            ))}
          </ul>
          <button type="button" className="btn-phone mt-4 w-full" onClick={() => openNotification(SAMPLE[frequency].length)}>
            Open notification
          </button>
          <button type="button" className="btn mt-2 w-full border border-red-400/40 text-red-300 hover:bg-red-500/10" onClick={uninstall}>
            Uninstall app
          </button>
        </>
      )}
    </section>
  );
}
