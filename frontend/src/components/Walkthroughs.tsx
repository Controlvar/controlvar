import React from 'react';
import manifest from '@/content/videos.json';

/**
 * The walkthrough videos on controlvar.com (#1290): the homepage section the
 * MARKETING build renders, one row per entry of `content/videos.json`.
 *
 * Rendered only when `isMarketingSite()` is true (`pages/index.tsx`). The
 * files live in no build: `scripts/fetch-site-media.mjs`, the last command of
 * `build:marketing`, downloads them from the `media` release, checks each
 * against the size and sha256 pinned in the manifest, and places them in
 * `out/media/`. A self-hoster's dashboard has neither the section nor the
 * files.
 *
 * What a visitor gets, and why (the UX plan):
 *   - the browser's own player: no autoplay, nothing muted or looped, and
 *     `preload="none"` with a poster, so no video bytes load before Play;
 *   - no `playsinline`, so an iPhone plays fullscreen, where the burned-in
 *     headline is readable;
 *   - a caption track that is NOT `default`: the burned-in band is a
 *     headline, the captions are the narration, and an enabled track drawn
 *     over the band makes both unreadable. The site serves `<stem>.site.vtt`,
 *     the approved captions with ` line:-5` on every cue, which lifts them
 *     above the band;
 *   - a closed descriptive transcript under each video: every card and
 *     headline ("On screen:") and the sentences said over each, because some
 *     of what a video states is only on screen.
 *
 * EVERY SENTENCE HERE IS CHECKED. The section's own copy is the plan's WORDS
 * table; a transcript is a quotation of its video, held to the storyboard and
 * the render's templates by
 * `backend/tests/unit/showcase/test_site_walkthroughs.py` and to the pinned
 * captions by `scripts/check-site-media.mjs`. Never edit the transcript of an
 * unchanged video: re-render it (a new file name) or revert the change.
 *
 * Adding a video changes no code: an entry in `content/videos.json`, its
 * transcript in `content/walkthroughs/<slug>.json`, and the per-video rows of
 * the tests that pin them (the slug list in `tests/pages/walkthroughs.test.tsx`,
 * the description's facts in `test_site_walkthroughs.py`).
 */

export type MediaFile = { name: string; bytes: number; sha256: string };

export type WalkthroughVideo = {
  slug: string;
  title: string;
  description: string;
  duration_seconds: number;
  mp4: MediaFile;
  vtt: MediaFile;
  poster: MediaFile;
};

/** One paragraph of a transcript, in the order the video shows it. */
export type TranscriptItem = { on_screen: string[] } | { said: string[] } | { code: string[] };

export type Transcript = { slug: string; mp4: string; items: TranscriptItem[] };

export const WALKTHROUGHS: readonly WalkthroughVideo[] = (manifest as { videos: WalkthroughVideo[] }).videos;

/** The page anchor: the slug without its number, so a link survives a re-record and a reorder. */
export const anchorFor = (slug: string): string => slug.replace(/^[0-9]{2}-/, '');

/** The caption file the site serves: the approved `<stem>.vtt`, positioned (`scripts/fetch-site-media.mjs`). */
export const siteCaptionsFor = (video: WalkthroughVideo): string => video.vtt.name.replace(/\.vtt$/, '.site.vtt');

const count = (n: number, unit: string): string => `${n} ${unit}${n === 1 ? '' : 's'}`;

/** `1:24`, and what a screen reader says instead: `Length: 1 minute 24 seconds`. */
export function lengthLabel(seconds: number): { shown: string; spoken: string } {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  const parts = [minutes ? count(minutes, 'minute') : '', rest || !minutes ? count(rest, 'second') : ''];
  return {
    shown: `${minutes}:${String(rest).padStart(2, '0')}`,
    spoken: `Length: ${parts.filter(Boolean).join(' ')}`,
  };
}

/**
 * An on-screen card or headline as one line: each fragment is a sentence, so a
 * fragment the video shows without a full stop (a title, a command) gets one.
 * The fragments themselves are the video's text, verbatim.
 */
export const onScreenLine = (fragments: string[]): string =>
  `On screen: ${fragments.map((f) => (/[.!?]$/.test(f) ? f : `${f}.`)).join(' ')}`;

/**
 * The transcript of one video. A template-literal `require`, so the bundler
 * takes every file in `content/walkthroughs/` and a new video needs no code:
 * the marketing export pre-renders the one each entry names, and an entry
 * with no transcript fails the build.
 */
export function transcriptFor(slug: string): Transcript {
  // eslint-disable-next-line @typescript-eslint/no-require-imports -- a static import cannot name a file per entry
  return require(`../content/walkthroughs/${slug}.json`) as Transcript;
}

function TranscriptItemView({ item }: { item: TranscriptItem }) {
  if ('on_screen' in item) {
    return <p className="text-slate-500">{onScreenLine(item.on_screen)}</p>;
  }
  if ('said' in item) {
    return <p className="text-slate-700">{item.said.join(' ')}</p>;
  }
  if ('code' in item) {
    return (
      <pre className="overflow-x-auto rounded-md bg-slate-900 p-4 text-xs leading-relaxed text-slate-100">
        <code>{item.code.join('\n')}</code>
      </pre>
    );
  }
  throw new Error(`a transcript item must be on_screen, said or code: ${JSON.stringify(item)}`);
}

export function Walkthroughs() {
  return (
    <section id="walkthroughs" aria-labelledby="walkthroughs-heading" className="border-t border-slate-200">
      <div className="mx-auto max-w-5xl px-6 py-14">
        <h2 id="walkthroughs-heading" className="text-2xl font-semibold tracking-tight">
          Walkthroughs
        </h2>
        <p className="mt-3 max-w-2xl text-slate-600">
          Narrated recordings, made on a local stack with demo data. Turn captions on in the player; the transcript is
          below each video.
        </p>
        {/* Only Tailwind classes the dashboard already uses: a new one would change the CSS that
            every page of the platform build loads (measured), though nothing there renders this. */}
        <ol role="list" className="mt-10 flex flex-col gap-8">
          {WALKTHROUGHS.map((video) => {
            const id = anchorFor(video.slug);
            const length = lengthLabel(video.duration_seconds);
            const transcript = transcriptFor(video.slug);
            return (
              <li key={video.slug} id={id}>
                <div className="grid gap-6 lg:grid-cols-3 lg:items-start">
                  <div>
                    <h3 id={`${id}-title`} className="text-lg font-semibold">
                      {video.title}
                    </h3>
                    <p id={`${id}-description`} className="mt-2 text-sm leading-relaxed text-slate-600">
                      {video.description}
                    </p>
                    <p className="mt-2 text-sm text-slate-500">
                      <span aria-hidden="true">{length.shown}</span>
                      <span className="sr-only">{length.spoken}</span>
                    </p>
                  </div>
                  <video
                    className="block w-full rounded-lg bg-slate-900 lg:col-span-2"
                    controls
                    preload="none"
                    poster={`/media/${video.poster.name}`}
                    width={1920}
                    height={1080}
                    aria-labelledby={`${id}-title`}
                    aria-describedby={`${id}-description`}
                    src={`/media/${video.mp4.name}`}
                  >
                    <track kind="captions" srcLang="en" label="English" src={`/media/${siteCaptionsFor(video)}`} />
                  </video>
                </div>
                <details className="mt-3">
                  <summary className="cursor-pointer py-2 text-sm font-medium text-blue-700">
                    Transcript<span className="sr-only">: {video.title}</span>
                  </summary>
                  <div className="mt-2 max-w-3xl space-y-2 text-sm leading-relaxed">
                    {transcript.items.map((item, i) => (
                      <TranscriptItemView key={i} item={item} />
                    ))}
                  </div>
                </details>
              </li>
            );
          })}
        </ol>
      </div>
    </section>
  );
}
