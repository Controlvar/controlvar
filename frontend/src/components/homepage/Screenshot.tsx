import React from 'react';
import registry from '@/content/site-screens.json';
import { isMarketingSite } from '@/utils/site-mode';

/**
 * A dashboard screenshot on the homepage (#1459), from the registry
 * `content/site-screens.json` (SPEC B).
 *
 * The file is `public/screens/<id>-<sha256[:12]>.webp`, so a retake is a new
 * name and its registry row, alt text included, has to be edited with it.
 * `tests/content/site-screens.test.ts` holds the registry to the folder (the
 * bytes, the hash, the size, no EXIF or XMP) and `tests/pages/home-page.test.tsx`
 * holds the rendered `<img>` to the registry.
 *
 * The MARKETING build only. A self-hoster's dashboard renders the rows as text
 * (#1459 C2): a screenshot there would show a demo, not their data, and
 * `scripts/check-platform-export.mjs` refuses a `/screens/` reference in the
 * platform homepage. The files are in that export anyway (public/ is copied
 * whole); only the reference is refused.
 *
 * One plain `<img>` with its width and height, never `srcset`, `<picture>` or a
 * CSS background: `check-site-media.mjs` reads only `src` and `poster`, so
 * anything else would load an image no check has seen. Every screenshot but
 * the hero lazy-loads. Never wrapped in a link: the export serves no page for
 * the file.
 */

export type ScreenId = 'results' | 'flag-rollout' | 'power-calculator' | 'audit-log';

export interface Screen {
  id: string;
  file: string;
  bytes: number;
  sha256: string;
  width: number;
  height: number;
  alt: string;
  caption: string | null;
}

export const SCREENS: readonly Screen[] = registry.screens;

/** The path the page loads a screenshot from. */
export const screenSrc = (screen: Screen): string => `/screens/${screen.file}`;

export function screenFor(id: ScreenId): Screen {
  const screen = SCREENS.find((s) => s.id === id);
  if (!screen) throw new Error(`content/site-screens.json has no screen with the id ${id}`);
  return screen;
}

export function Screenshot({ id, hero = false }: { id: ScreenId; hero?: boolean }) {
  if (!isMarketingSite()) return null;
  const screen = screenFor(id);
  return (
    <figure className="min-w-0">
      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl">
        <div aria-hidden="true" className="flex items-center gap-2 border-b border-slate-200 bg-slate-100 px-3.5 py-2.5">
          <span className="h-2.5 w-2.5 rounded-full bg-slate-300" />
          <span className="h-2.5 w-2.5 rounded-full bg-slate-300" />
          <span className="h-2.5 w-2.5 rounded-full bg-slate-300" />
        </div>
        <img
          src={screenSrc(screen)}
          alt={screen.alt}
          width={screen.width}
          height={screen.height}
          loading={hero ? undefined : 'lazy'}
          decoding="async"
          className="block h-auto w-full"
        />
      </div>
      {screen.caption !== null && (
        <figcaption className="mt-3 text-center text-sm text-slate-600">{screen.caption}</figcaption>
      )}
    </figure>
  );
}
