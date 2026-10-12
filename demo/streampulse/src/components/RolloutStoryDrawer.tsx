import React, { useEffect, useRef } from 'react';
import { useFeatureFlag } from '@getexperimently/react-sdk';
import type { Device } from '@/lib/devices';
import { DASHBOARD_URL, FLAG_KEYS } from '@/lib/env';
import { flagReasonLabel } from '@/lib/labels';

export const STORY_COMMAND = 'python demo/streampulse/simulator/rollout_story.py --auto';

export interface StoryStep {
  title: string;
  /** What happens on the platform. */
  action: string;
  /** What to look at in the dashboard / this app. */
  lookAt: string;
}

/** The 7-step narrative for the StreamPulse rollout story as seeded and scripted. */
export const STORY_STEPS: StoryStep[] = [
  {
    title: 'We are launching a redesigned player',
    action: 'The flag streampulse_player_v2 exists at 5 % with a targeting rule "employee equals true" and a 4-stage rollout schedule (5 → 25 → 50 → 100 %). Safety monitoring watches its error rate (warn 2 %, critical 5 %).',
    lookAt: 'Dashboard → Feature flags → Player v2: rollout, rules, schedule, safety config. In the app pick "Internal tester": Player v2 is on by targeting rule.',
  },
  {
    title: 'Start with the internal team',
    action: 'Employees always get the new player (rule → 100 %), everyone else is bucketed by the flag\'s 5 % rollout — the same hash the simulator devices go through.',
    lookAt: 'Switch between "Internal tester" and any other preset: the panel shows on: matched a targeting rule vs. in/outside the rollout %.',
  },
  {
    title: 'Looks good, roll to 25 %',
    action: 'The script advances rollout stage 2 (POST /rollout-schedules/{id}/stages/{stage}/advance). The flag\'s rollout percentage becomes 25 %.',
    lookAt: 'Flag detail shows 25 %. In the simulator stats the player_v2 on-rate climbs towards a quarter of devices.',
  },
  {
    title: 'Crash rate spikes on Android 12',
    action: 'The simulator runs with --incident android12: 40 % of Android 12 devices with the flag on crash, and each crash is reported through POST /tracking/errors. The simulator reports the crashes the way the StreamPulse server would forward them, with its server-side key. The flag\'s error rate crosses the 5 % critical threshold.',
    lookAt: 'Dashboard → Admin → Safety: the check for Player v2 turns unhealthy. Pick "Galaxy S10 · Android 12" in the app — that is the affected cohort.',
  },
  {
    title: 'Safety monitor rolls back to 5 %',
    action: 'Automatic rollback (rollback_percentage 5) — or the script calls POST /safety/feature-flags/{id}/rollback?percentage=5 — takes the flag back to 5 % and pauses its rollout schedule, so no stage can raise it again. Employees keep Player v2 through their targeting rule. A rollback record is written.',
    lookAt: 'Flag rollout is 5 % again and its rollout schedule shows Paused. Within 5 s the app\'s Player v2 readout below flips for most devices.',
  },
  {
    title: 'Fix shipped in v3.2.1, resume rollout',
    action: 'A new rule app_version semver_gte 3.2.1 → 100 % is added (the employee rule stays). The script resumes the paused schedule (POST /rollout-schedules/{id}/activate), then stage 3 advances the base rollout to 50 %.',
    lookAt: 'Compare "iPhone 15 (app 3.2.1)" with "Galaxy S10 (app 3.1.0)": the fixed build gets Player v2 by rule, the old build only by the 50 % rollout.',
  },
  {
    title: 'Full rollout',
    action: 'Stage 4 advances to 100 % and the targeting rules are removed — the flag is now plain "on" and ready to be retired from the code.',
    lookAt: 'Every preset shows Player v2 on: inside the rollout %. The schedule is COMPLETED; the audit log has every step.',
  },
];

interface Props {
  open: boolean;
  onClose: () => void;
  device: Device;
}

/**
 * Side drawer with the guided rollout narrative plus a live readout of
 * `streampulse_player_v2` for the current device. Must be rendered inside the
 * SDK provider.
 */
export default function RolloutStoryDrawer({ open, onClose, device }: Props) {
  const player = useFeatureFlag(FLAG_KEYS.playerV2);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-40 flex justify-end" data-testid="rollout-story">
      <button type="button" className="flex-1 bg-black/40" aria-label="Close rollout story" onClick={onClose} tabIndex={-1} />
      <aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="story-title"
        className="h-full w-[min(32rem,100vw)] overflow-y-auto bg-white p-5 shadow-2xl"
      >
        <div className="mb-4 flex items-start justify-between gap-3">
          <div>
            <h2 id="story-title" className="text-lg font-bold">
              The rollout story
            </h2>
            <p className="text-xs text-neutral-500">Player v2, from internal testers to 100 % — with an incident on the way.</p>
          </div>
          <button ref={closeRef} type="button" className="btn-secondary text-xs" onClick={onClose}>
            Close
          </button>
        </div>

        <section className="mb-4 rounded-lg border border-neutral-200 bg-neutral-50 p-3" aria-labelledby="story-live-title">
          <h3 id="story-live-title" className="text-xs font-semibold uppercase tracking-wide text-neutral-500">
            Player v2 rollout · this device
          </h3>
          <div className="mt-1 flex items-center justify-between gap-2">
            <div className="min-w-0 text-sm">
              <div className="truncate font-medium" data-testid="story-device">
                {device.device_model} · {device.os} {device.os_version} · app {device.app_version}
                {device.employee ? ' · employee' : ''}
              </div>
              <div className="text-xs text-neutral-600" data-testid="story-player-reason">
                {flagReasonLabel(player)}
              </div>
            </div>
            <span className={player.loading ? 'pill-off' : player.error ? 'pill-error' : player.isEnabled ? 'pill-on' : 'pill-off'} data-testid="story-player-state">
              {player.loading ? 'loading…' : player.error ? 'error' : player.isEnabled ? 'on' : 'off'}
            </span>
          </div>
        </section>

        <section className="mb-4" aria-labelledby="story-run-title">
          <h3 id="story-run-title" className="text-xs font-semibold uppercase tracking-wide text-neutral-500">
            Run it
          </h3>
          <pre className="mt-1 overflow-x-auto rounded-md bg-neutral-900 p-2 text-xs text-neutral-100">
            <code>{STORY_COMMAND}</code>
          </pre>
          <p className="mt-1 text-xs text-neutral-500">
            From the repo root with the venv active. <code className="font-mono">--step N</code> runs one step, <code className="font-mono">--pace</code> sets the
            seconds between steps in <code className="font-mono">--auto</code>. Keep <code className="font-mono">traffic.py</code> running so the numbers move.
          </p>
        </section>

        <ol className="space-y-3">
          {STORY_STEPS.map((step, i) => (
            <li key={step.title} className="rounded-lg border border-neutral-200 p-3">
              <h3 className="flex items-baseline gap-2 text-sm font-semibold">
                <span className="flex h-5 w-5 flex-none items-center justify-center rounded-full bg-pulse-600 text-[11px] text-white" aria-hidden="true">
                  {i + 1}
                </span>
                <span>
                  <span className="sr-only">Step {i + 1}: </span>
                  {step.title}
                </span>
              </h3>
              <p className="mt-1 text-xs text-neutral-700">{step.action}</p>
              <p className="mt-1 text-xs text-neutral-500">
                <span className="font-semibold text-neutral-600">Look at:</span> {step.lookAt}
              </p>
            </li>
          ))}
        </ol>

        <nav className="mt-4 flex flex-wrap gap-x-4 gap-y-1 text-xs font-semibold text-pulse-700" aria-label="Story links">
          <a href={`${DASHBOARD_URL}/feature-flags`} target="_blank" rel="noreferrer" className="hover:underline">
            Feature flags ↗
          </a>
          <a href={`${DASHBOARD_URL}/admin/safety`} target="_blank" rel="noreferrer" className="hover:underline">
            Safety ↗
          </a>
          <a href={`${DASHBOARD_URL}/admin/audit`} target="_blank" rel="noreferrer" className="hover:underline">
            Audit log ↗
          </a>
        </nav>
      </aside>
    </div>
  );
}
