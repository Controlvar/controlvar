import React, { useState } from 'react';
import { useRouter } from 'next/router';
import { useFeatureFlag } from '@controlvar/react-sdk';
import { FLAG_KEYS } from '@/lib/env';
import { usePageView, useTrack } from '@/lib/eventLog';
import { PRODUCTS } from '@/data/products';
import { highlightSegments, search, type SearchEngine, type SearchHit } from '@/lib/search';
import ProductCard from '@/components/ProductCard';

function Highlighted({ text, words }: { text: string; words: string[] }) {
  return (
    <>
      {highlightSegments(text, words).map((seg, i) =>
        seg.hit ? (
          <mark key={i} className="rounded bg-yellow-200 px-0.5">
            {seg.text}
          </mark>
        ) : (
          <React.Fragment key={i}>{seg.text}</React.Fragment>
        ),
      )}
    </>
  );
}

export default function SearchPage() {
  const router = useRouter();
  const track = useTrack();
  const newSearch = useFeatureFlag(FLAG_KEYS.newSearch);
  const engine: SearchEngine = newSearch.isEnabled ? 'fuzzy' : 'exact';
  const initialQuery = typeof router.query.q === 'string' ? router.query.q : '';
  const [query, setQuery] = useState(initialQuery);
  const [results, setResults] = useState<{ query: string; hits: SearchHit[]; engine: SearchEngine } | null>(null);
  usePageView('search');

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    const hits = search(PRODUCTS, q, engine);
    setResults({ query: q, hits, engine });
    track('search', { query: q, results: hits.length, engine }, { featureFlagKey: FLAG_KEYS.newSearch });
  };

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-center gap-3">
        <h1 className="text-3xl font-bold">Search</h1>
        {newSearch.isEnabled && (
          <span
            data-testid="new-search-badge"
            className="rounded-full bg-accent-100 px-3 py-1 text-xs font-semibold text-accent-700"
          >
            New search ✨
          </span>
        )}
      </div>

      <form onSubmit={onSubmit} role="search" className="flex gap-2">
        <label htmlFor="search-query" className="sr-only">
          Search products
        </label>
        <input
          id="search-query"
          type="search"
          className="input flex-1"
          placeholder={engine === 'fuzzy' ? 'Try "rain jaket" or "warm boots"…' : 'Search by product name…'}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          autoComplete="off"
        />
        <button type="submit" className="btn-primary">
          Search
        </button>
      </form>
      <p className="mt-2 text-xs text-neutral-500" data-testid="search-engine" data-engine={engine}>
        Engine: {engine === 'fuzzy' ? 'fuzzy match on name, description and category' : 'exact match on product name'}
      </p>

      {results && (
        <section className="mt-8" aria-live="polite">
          <h2 className="mb-4 text-lg font-semibold" data-testid="search-summary">
            {results.hits.length} {results.hits.length === 1 ? 'result' : 'results'} for “{results.query}”
          </h2>
          {results.hits.length === 0 ? (
            <p className="text-neutral-600">
              Nothing matched.{' '}
              {results.engine === 'exact' ? 'The legacy engine only matches product names exactly.' : 'Try fewer words.'}
            </p>
          ) : (
            <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {results.hits.map((hit, index) => (
                <ProductCard
                  key={hit.product.id}
                  product={hit.product}
                  position={index + 1}
                  title={
                    results.engine === 'fuzzy' ? <Highlighted text={hit.product.name} words={hit.matchedWords} /> : undefined
                  }
                  onClick={(p, position) => track('product_click', { product_id: p.id, position, sort: 'search' })}
                >
                  {results.engine === 'fuzzy' && (
                    <span className="mt-2 text-xs text-neutral-500">
                      <Highlighted text={hit.product.description} words={hit.matchedWords} />
                    </span>
                  )}
                </ProductCard>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}
