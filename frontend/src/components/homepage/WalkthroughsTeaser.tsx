import React from 'react';
import Link from 'next/link';
import { WALKTHROUGHS, anchorFor, lengthLabel } from '@/components/Walkthroughs';
import { FOCUS, H2, MORE_LINK, WIDTH } from '@/components/homepage/styles';

/** How many walkthroughs the homepage shows: the manifest's first three. */
export const TEASER_COUNT = 3;

export const TEASER = WALKTHROUGHS.slice(0, TEASER_COUNT);

/**
 * The homepage's walkthroughs (site design 2026-11-11, item 7): the first
 * three of `content/videos.json` as cards, each its poster, title and length,
 * linking to its row of the /walkthroughs page, then a link to that page.
 *
 * Posters, not players, so the homepage has no `<video>`: the players, their
 * captions and the transcripts are the page's. Each poster is a plain `<img>`
 * of the manifest's pinned file, with its width and height, loading lazily
 * (it is below the fold at every width), and `alt=""`: the card's title, in
 * the same link, names it. `scripts/check-site-media.mjs` holds the exported
 * homepage to that: no `<video>`, and no `/media/` path but a manifest poster.
 *
 * The MARKETING build only (`pages/index.tsx`): the videos and the page are
 * its only.
 */
export function WalkthroughsTeaser() {
  return (
    <section aria-labelledby="walkthroughs-heading" className={`${WIDTH} py-12 lg:py-16`}>
      <h2 id="walkthroughs-heading" className={H2}>
        Walkthroughs
      </h2>
      <ul role="list" className="mt-8 grid gap-6 sm:grid-cols-3">
        {TEASER.map((video) => {
          const length = lengthLabel(video.duration_seconds);
          return (
            <li key={video.slug} className="min-w-0">
              <Link href={`/walkthroughs#${anchorFor(video.slug)}`} className={`group block rounded-lg ${FOCUS}`}>
                <img
                  src={`/media/${video.poster.name}`}
                  alt=""
                  width={1280}
                  height={720}
                  loading="lazy"
                  decoding="async"
                  className="block h-auto w-full rounded-lg border border-slate-200 bg-slate-900"
                />
                <h3 className="mt-3 font-semibold text-slate-900 group-hover:text-blue-700">{video.title}</h3>
                <p className="mt-1 text-sm text-slate-600">
                  <span aria-hidden="true">{length.shown}</span>
                  <span className="sr-only">{length.spoken}</span>
                </p>
              </Link>
            </li>
          );
        })}
      </ul>
      <p className="mt-6">
        <Link href="/walkthroughs" className={MORE_LINK}>
          All the walkthroughs <span aria-hidden="true">→</span>
        </Link>
      </p>
    </section>
  );
}
