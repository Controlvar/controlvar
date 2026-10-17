import React, { useEffect, useRef, useState } from 'react';
import { useExperiment } from '@getexperimently/react-sdk';
import { EXPERIMENT_KEYS } from '@/lib/env';
import { useScreenView, useTrack } from '@/lib/eventLog';
import { Binding, ScreenHeader } from '@/components/ui';

export const BADGES = [
  { id: 'night-owl', label: 'Night owl', glyph: '🌙' },
  { id: 'early-adopter', label: 'Early adopter', glyph: '🚀' },
  { id: 'marathon', label: '10h streak', glyph: '🔥' },
  { id: 'explorer', label: 'Genre explorer', glyph: '🧭' },
];

interface Props {
  /** Device model shown on the profile card. */
  deviceModel: string;
  tier: string;
}

/**
 * Profile. Two experiments share the screen and sit in the same mutual
 * exclusion group (`streampulse-profile`), so a device is enrolled in at most
 * one of them:
 * - `streampulse_wrapped` → "Your 2026 Wrapped" card; `wrapped_view` on mount
 *   when in `wrapped_2026`, `share {surface}` on Share.
 * - `streampulse_profile_badges` → badge row; `badge_tap` per badge.
 */
export default function ProfileScreen({ deviceModel, tier }: Props) {
  useScreenView('profile');
  const track = useTrack();
  const wrapped = useExperiment(EXPERIMENT_KEYS.wrapped);
  const badges = useExperiment(EXPERIMENT_KEYS.profileBadges);
  const showWrapped = wrapped.configuration?.wrapped === true;
  const showBadges = badges.configuration?.badges === true;
  const [shared, setShared] = useState(false);
  const [tapped, setTapped] = useState<string[]>([]);

  const viewed = useRef(false);
  useEffect(() => {
    if (wrapped.loading || !showWrapped || viewed.current) return;
    viewed.current = true;
    track('wrapped_view', { year: 2026 }, { experimentKey: EXPERIMENT_KEYS.wrapped });
  }, [wrapped.loading, showWrapped, track]);

  const share = () => {
    setShared(true);
    track('share', { surface: 'wrapped' }, { experimentKey: EXPERIMENT_KEYS.wrapped });
  };
  const tapBadge = (id: string) => {
    setTapped((t) => (t.includes(id) ? t : [...t, id]));
    track('badge_tap', { badge: id }, { experimentKey: EXPERIMENT_KEYS.profileBadges });
  };

  return (
    <section aria-labelledby="profile-title" data-testid="profile" data-wrapped={showWrapped} data-badges={showBadges}>
      <ScreenHeader id="profile-title" title="Profile" subtitle="You, in numbers" />
      <div className="flex items-center gap-3 rounded-xl bg-white/5 p-3">
        <div className="flex h-12 w-12 flex-none items-center justify-center rounded-full bg-pulse-500 text-lg font-bold" aria-hidden="true">
          SP
        </div>
        <div className="min-w-0">
          <div className="font-semibold">Listener</div>
          <div className="truncate text-xs text-neutral-400">
            {deviceModel} · {tier} plan
          </div>
        </div>
      </div>

      {showWrapped && (
        <article
          className="mt-3 rounded-2xl p-4 text-white"
          style={{ background: 'linear-gradient(135deg, #7c3aed, #f43f5e 60%, #f59e0b)' }}
          data-testid="wrapped-card"
          aria-labelledby="wrapped-title"
        >
          <div className="flex items-center justify-between">
            <h3 id="wrapped-title" className="text-lg font-extrabold">
              Your 2026 Wrapped
            </h3>
            <Binding>{EXPERIMENT_KEYS.wrapped}</Binding>
          </div>
          <dl className="mt-2 grid grid-cols-3 gap-2 text-center">
            <div>
              <dt className="text-[10px] uppercase opacity-80">Minutes</dt>
              <dd className="text-xl font-bold">38,412</dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase opacity-80">Top artist</dt>
              <dd className="text-sm font-bold">Lumen Fields</dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase opacity-80">Top genre</dt>
              <dd className="text-sm font-bold">Synthwave</dd>
            </div>
          </dl>
          <button type="button" className="btn mt-3 w-full bg-white/20 text-white hover:bg-white/30" onClick={share} disabled={shared}>
            {shared ? 'Shared ✓' : 'Share your Wrapped'}
          </button>
        </article>
      )}

      {showBadges && (
        <div className="mt-3" data-testid="badge-row">
          <div className="mb-1 flex items-center justify-between">
            <h3 className="text-xs uppercase tracking-wide text-neutral-400">Badges</h3>
            <Binding>{EXPERIMENT_KEYS.profileBadges}</Binding>
          </div>
          <ul className="grid grid-cols-4 gap-2">
            {BADGES.map((b) => (
              <li key={b.id}>
                <button
                  type="button"
                  onClick={() => tapBadge(b.id)}
                  aria-label={`Badge: ${b.label}`}
                  aria-pressed={tapped.includes(b.id)}
                  className={`flex w-full flex-col items-center gap-1 rounded-xl p-2 text-[10px] ${
                    tapped.includes(b.id) ? 'bg-pulse-500/30' : 'bg-white/5 hover:bg-white/10'
                  }`}
                >
                  <span className="text-xl" aria-hidden="true">
                    {b.glyph}
                  </span>
                  {b.label}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {!showWrapped && !showBadges && (
        <p className="mt-3 text-xs text-neutral-400" data-testid="profile-plain">
          Standard profile. This device is in neither Profile experiment
          {wrapped.assigned === false || badges.assigned === false ? ' (see the Controlvar panel for why)' : ''}.
        </p>
      )}

      <dl className="mt-4 grid grid-cols-2 gap-2 text-sm">
        <div className="rounded-xl bg-white/5 p-2">
          <dt className="text-[10px] uppercase text-neutral-400">Playlists</dt>
          <dd className="font-semibold">14</dd>
        </div>
        <div className="rounded-xl bg-white/5 p-2">
          <dt className="text-[10px] uppercase text-neutral-400">Following</dt>
          <dd className="font-semibold">27 artists</dd>
        </div>
      </dl>
    </section>
  );
}
