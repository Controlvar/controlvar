import React, { useState } from 'react';
import { useFeatureFlag } from '@controlvar/react-sdk';
import { formatDuration, TRACKS, TRACKS_BY_ID } from '@/data/tracks';
import { FLAG_KEYS } from '@/lib/env';
import { useScreenView, useTrack } from '@/lib/eventLog';
import { Art, Binding, ScreenHeader } from '@/components/ui';

interface Props {
  trackId: string;
  onChangeTrack: (trackId: string) => void;
}

const WAVE_BARS = [4, 9, 14, 8, 18, 22, 12, 26, 16, 30, 20, 10, 24, 14, 28, 18, 8, 22, 12, 6, 16, 26, 14, 20, 10, 24, 8, 18, 12, 6];

/**
 * Player. `streampulse_player_v2` on → redesigned player (large art, waveform,
 * queue); off → classic player. Every press of Play sends `play {track_id, player}`
 * attributed to the flag, so the flag's safety monitor can compare error rate
 * against evaluations. `streampulse_offline_mode` gates the Download button.
 */
export default function PlayerScreen({ trackId, onChangeTrack }: Props) {
  useScreenView('player');
  const track = useTrack();
  const playerV2 = useFeatureFlag(FLAG_KEYS.playerV2);
  const offline = useFeatureFlag(FLAG_KEYS.offlineMode);
  const [playing, setPlaying] = useState(false);
  const [downloaded, setDownloaded] = useState<Record<string, boolean>>({});

  const current = TRACKS_BY_ID[trackId] ?? TRACKS[0];
  const index = TRACKS.findIndex((t) => t.id === current.id);
  const player = playerV2.isEnabled ? 'v2' : 'classic';

  const play = () => {
    setPlaying(true);
    track('play', { track_id: current.id, player }, { featureFlagKey: FLAG_KEYS.playerV2 });
  };
  const step = (delta: number) => {
    const next = TRACKS[(index + delta + TRACKS.length) % TRACKS.length];
    setPlaying(false);
    onChangeTrack(next.id);
  };

  const controls = (
    <div className="flex items-center justify-center gap-4">
      <button type="button" className="btn-phone" onClick={() => step(-1)} aria-label="Previous track">
        ⏮
      </button>
      <button
        type="button"
        onClick={playing ? () => setPlaying(false) : play}
        aria-label={playing ? 'Pause' : 'Play'}
        className={`relative flex h-14 w-14 items-center justify-center rounded-full bg-pulse-500 text-xl text-white hover:bg-pulse-400 ${playing ? 'playing-ring' : ''}`}
      >
        {playing ? '❚❚' : '▶'}
      </button>
      <button type="button" className="btn-phone" onClick={() => step(1)} aria-label="Next track">
        ⏭
      </button>
    </div>
  );

  const download = offline.isEnabled && (
    <button
      type="button"
      className="btn-phone mt-3 w-full"
      data-testid="download-button"
      onClick={() => setDownloaded((d) => ({ ...d, [current.id]: true }))}
      disabled={Boolean(downloaded[current.id])}
    >
      {downloaded[current.id] ? 'Available offline ✓' : 'Download for offline'}
    </button>
  );

  if (playerV2.isEnabled) {
    return (
      <section aria-labelledby="player-title" data-testid="player" data-player="v2">
        <ScreenHeader id="player-title" title="Now playing" subtitle="Player v2" badge={<Binding>{FLAG_KEYS.playerV2}</Binding>} />
        <Art track={current} size={220} className="mx-auto shadow-2xl" />
        <div className="mt-4 text-center">
          <div className="text-lg font-bold">{current.title}</div>
          <div className="text-sm text-neutral-400">{current.artist}</div>
        </div>
        <div className="wave mt-4" aria-hidden="true">
          {WAVE_BARS.map((h, i) => (
            <span key={i} className={i < (playing ? 12 : 0) ? 'played' : ''} style={{ height: h }} />
          ))}
        </div>
        <div className="mt-1 flex justify-between text-[10px] text-neutral-400" aria-hidden="true">
          <span>{playing ? '1:24' : '0:00'}</span>
          <span>{formatDuration(current.duration)}</span>
        </div>
        <div className="mt-4">{controls}</div>
        <div className="mt-4 flex justify-center gap-2 text-xs text-neutral-300">
          <span className="rounded-full bg-white/10 px-2 py-0.5">Lyrics</span>
          <span className="rounded-full bg-white/10 px-2 py-0.5">Queue · {TRACKS.length - 1}</span>
          <span className="rounded-full bg-white/10 px-2 py-0.5">Sleep timer</span>
        </div>
        {download}
      </section>
    );
  }

  return (
    <section aria-labelledby="player-title" data-testid="player" data-player="classic">
      <ScreenHeader id="player-title" title="Player" subtitle="Classic player" badge={<Binding>{FLAG_KEYS.playerV2}</Binding>} />
      <div className="flex items-center gap-3 rounded-xl bg-white/5 p-3">
        <Art track={current} size={64} />
        <div className="min-w-0">
          <div className="truncate font-semibold">{current.title}</div>
          <div className="truncate text-xs text-neutral-400">{current.artist}</div>
        </div>
      </div>
      <div className="mt-4 h-1.5 w-full rounded-full bg-white/10" aria-hidden="true">
        <div className="h-1.5 rounded-full bg-neutral-300" style={{ width: playing ? '38%' : '0%' }} />
      </div>
      <div className="mt-1 flex justify-between text-[10px] text-neutral-400" aria-hidden="true">
        <span>{playing ? '1:24' : '0:00'}</span>
        <span>{formatDuration(current.duration)}</span>
      </div>
      <div className="mt-4">{controls}</div>
      {download}
    </section>
  );
}
