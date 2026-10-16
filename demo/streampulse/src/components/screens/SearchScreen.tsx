import React, { useState } from 'react';
import { useFeatureFlag } from '@controlvar/react-sdk';
import { formatDuration } from '@/data/tracks';
import { FLAG_KEYS } from '@/lib/env';
import { useScreenView, useTrack } from '@/lib/eventLog';
import { aiSearch, keywordSearch, type SearchHit } from '@/lib/search';
import { Art, Binding, ScreenHeader } from '@/components/ui';

export const SUGGESTED_QUERIES = ['focus', 'late night drive', 'lumen', 'summer'];

/**
 * Search. `streampulse_ai_search` on → "AI search ✨" with per-result explanations;
 * off → keyword search. The flag is the targeting-rules story (iOS 17+, US,
 * premium): the same query gives different products on different devices.
 * Every search sends `search {query, engine, results}` attributed to the flag.
 */
export default function SearchScreen() {
  useScreenView('search');
  const track = useTrack();
  const ai = useFeatureFlag(FLAG_KEYS.aiSearch);
  const engine = ai.isEnabled ? 'ai' : 'keyword';
  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<SearchHit[] | null>(null);

  const run = (q: string) => {
    const trimmed = q.trim();
    if (!trimmed) return;
    const results = engine === 'ai' ? aiSearch(trimmed) : keywordSearch(trimmed);
    setQuery(q);
    setHits(results);
    track('search', { query: trimmed, engine, results: results.length }, { featureFlagKey: FLAG_KEYS.aiSearch });
  };

  return (
    <section aria-labelledby="search-title" data-testid="search" data-engine={engine}>
      <ScreenHeader
        id="search-title"
        title={engine === 'ai' ? 'AI search ✨' : 'Search'}
        subtitle={engine === 'ai' ? 'Understands moods, not just words' : 'Keyword search'}
        badge={<Binding>{FLAG_KEYS.aiSearch}</Binding>}
      />
      <form
        role="search"
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          run(query);
        }}
      >
        <label htmlFor="search-input" className="sr-only">
          Search tracks
        </label>
        <input
          id="search-input"
          className="input border-transparent bg-white/10 text-white placeholder:text-neutral-400"
          placeholder={engine === 'ai' ? 'Try "music for a late night drive"' : 'Title, artist or genre'}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          autoComplete="off"
        />
        <button type="submit" className="btn-phone-primary">
          Go
        </button>
      </form>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {SUGGESTED_QUERIES.map((q) => (
          <button key={q} type="button" className="rounded-full bg-white/10 px-2 py-0.5 text-xs text-neutral-200 hover:bg-white/20" onClick={() => run(q)}>
            {q}
          </button>
        ))}
      </div>

      {hits && (
        <div className="mt-4" aria-live="polite">
          <p className="text-xs text-neutral-400" data-testid="search-summary">
            {hits.length} {hits.length === 1 ? 'result' : 'results'} · {engine === 'ai' ? 'AI engine' : 'keyword engine'}
          </p>
          <ol className="mt-2 space-y-2" data-testid="search-results">
            {hits.map(({ track: t, explanation }) => (
              <li key={t.id} className="flex items-start gap-3 rounded-xl bg-white/5 p-2">
                <Art track={t} size={40} />
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold">{t.title}</div>
                  <div className="truncate text-xs text-neutral-400">
                    {t.artist} · {formatDuration(t.duration)}
                  </div>
                  {explanation && (
                    <p className="mt-0.5 text-[11px] text-pulse-300" data-testid="search-explanation">
                      {explanation}
                    </p>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}
