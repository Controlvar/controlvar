import React, { useEffect, useRef } from 'react';
import { useFeatureFlag } from '@controlvar/react-sdk';
import { chronologicalFeed, formatDuration, recommendedFeed } from '@/data/tracks';
import { FLAG_KEYS } from '@/lib/env';
import { useScreenView, useTrack } from '@/lib/eventLog';
import { Art, Binding, ScreenHeader } from '@/components/ui';

export const RECS_ALGORITHM = 'recs_v2';
export const CLASSIC_ALGORITHM = 'chronological';

interface Props {
  onOpenTrack: (trackId: string) => void;
}

/**
 * Home feed. `streampulse_recs_v2` on → "For you" (new recommendation algorithm,
 * fires `recs_impression`); off → "Classic" chronological feed. The flag is the
 * kill switch of the demo: turn it off in the dashboard and the feed flips back
 * within the SDK cache TTL.
 */
export default function HomeScreen({ onOpenTrack }: Props) {
  useScreenView('home');
  const track = useTrack();
  const recs = useFeatureFlag(FLAG_KEYS.recsV2);
  const useRecs = recs.isEnabled;
  const feed = useRecs ? recommendedFeed() : chronologicalFeed();

  // One impression per time the new algorithm is shown (not while loading, not for classic).
  const impressed = useRef(false);
  useEffect(() => {
    if (recs.loading || !useRecs || impressed.current) return;
    impressed.current = true;
    track('recs_impression', { algorithm: RECS_ALGORITHM, items: feed.length }, { featureFlagKey: FLAG_KEYS.recsV2 });
  }, [recs.loading, useRecs, feed.length, track]);

  return (
    <section aria-labelledby="home-title" data-testid="home-feed" data-feed={useRecs ? RECS_ALGORITHM : CLASSIC_ALGORITHM}>
      <ScreenHeader
        id="home-title"
        title={useRecs ? 'For you' : 'Classic'}
        subtitle={useRecs ? 'Picked by the new recommendation model' : 'Newest releases first'}
        badge={<Binding>{FLAG_KEYS.recsV2}</Binding>}
      />
      <ol className="space-y-2">
        {feed.map((t, i) => (
          <li key={t.id} className="flex items-center gap-3 rounded-xl bg-white/5 p-2">
            <Art track={t} />
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-semibold">{t.title}</div>
              <div className="truncate text-xs text-neutral-400">
                {t.artist} · {t.genre} · {formatDuration(t.duration)}
              </div>
              {useRecs && i < 3 && <div className="text-[10px] text-pulse-300">Because you like {t.tags[0]} music</div>}
            </div>
            <button type="button" className="btn-phone" onClick={() => onOpenTrack(t.id)} aria-label={`Open ${t.title} in the player`}>
              Open
            </button>
          </li>
        ))}
      </ol>
    </section>
  );
}
