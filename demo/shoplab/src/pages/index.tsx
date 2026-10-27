import React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { useExperiment } from '@controlvar/react-sdk';
import { EXPERIMENT_KEYS } from '@/lib/env';
import { usePageView, useTrack } from '@/lib/eventLog';
import { PRODUCTS } from '@/data/products';
import ProductCard from '@/components/ProductCard';
import { sortByRelevance } from '@/lib/sorting';

export const HERO_DEFAULTS = {
  media: 'image',
  headline: 'Gear up for the season',
  cta: 'Shop the collection',
} as const;

function readHeroConfig(configuration: Record<string, unknown> | null) {
  const cfg = configuration ?? {};
  return {
    media: cfg.media === 'video' ? 'video' : 'image',
    headline: typeof cfg.headline === 'string' && cfg.headline ? cfg.headline : HERO_DEFAULTS.headline,
    cta: typeof cfg.cta === 'string' && cfg.cta ? cfg.cta : HERO_DEFAULTS.cta,
  } as const;
}

export default function HomePage() {
  const router = useRouter();
  const track = useTrack();
  const hero = useExperiment(EXPERIMENT_KEYS.hero);
  const { media, headline, cta } = readHeroConfig(hero.configuration);
  usePageView('home');

  const featured = sortByRelevance(PRODUCTS).slice(0, 4);

  const onCta = () => {
    track('hero_cta_click', { variant: hero.variantName, media });
    void router.push('/products');
  };

  return (
    <div>
      <section
        data-testid="hero"
        data-media={media}
        className={`relative overflow-hidden rounded-2xl px-8 py-16 text-white sm:px-12 sm:py-24 ${
          media === 'video'
            ? 'hero-video bg-gradient-to-r from-indigo-900 via-fuchsia-700 to-amber-500'
            : 'bg-gradient-to-br from-emerald-800 via-teal-700 to-sky-600'
        }`}
      >
        <div className="relative z-10 max-w-xl">
          <p className="mb-3 text-sm font-semibold uppercase tracking-widest text-white/80">
            {media === 'video' ? 'Now playing' : 'New season'}
          </p>
          <h1 className="text-4xl font-extrabold tracking-tight sm:text-5xl">{headline}</h1>
          <p className="mt-4 text-lg text-white/85">
            Packs, boots and layers built for long days outside — tested by real people and real experiments.
          </p>
          <button type="button" onClick={onCta} className="btn mt-8 bg-white px-6 py-3 text-base text-neutral-900 hover:bg-neutral-100">
            {cta}
          </button>
        </div>
        {media === 'video' && (
          <div
            aria-hidden="true"
            className="absolute bottom-4 right-6 z-10 flex items-center gap-2 rounded-full bg-black/40 px-3 py-1 text-xs"
          >
            <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-red-500" />
            Video
          </div>
        )}
      </section>

      <section className="mt-12" aria-labelledby="featured-heading">
        <div className="mb-4 flex items-end justify-between">
          <h2 id="featured-heading" className="text-2xl font-bold">
            Top rated this week
          </h2>
          <Link href="/products" className="text-sm font-semibold text-accent-600 hover:underline">
            View all products →
          </Link>
        </div>
        <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {featured.map((p, i) => (
            <ProductCard
              key={p.id}
              product={p}
              position={i + 1}
              // A click here is not on the sorted list, so it names the hero test rather
              // than fanning out to the sort bandit, whose reward is product_click.
              onClick={(product, position) =>
                track('product_click', { product_id: product.id, position, sort: 'featured' }, { experimentKey: EXPERIMENT_KEYS.hero })
              }
            />
          ))}
        </ul>
      </section>
    </div>
  );
}
