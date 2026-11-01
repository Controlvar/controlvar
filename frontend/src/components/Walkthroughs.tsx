import React from 'react';
import manifest from '@/content/videos.json';
import { FOCUS } from '@/components/homepage/styles';

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
 * Two kinds of video, declared by the transcript's `kind`:
 *   - a recording (no `kind`): the dashboard filmed on a local stack with demo
 *     data, narrated, with a burned-in headline band;
 *   - an animation (`"kind": "animation"`): no sound, with captions of its
 *     own text only.
 * Each row says which it is, in visible text.
 *
 * What a visitor gets, and why (the UX plan):
 *   - the browser's own player: no autoplay, nothing muted or looped, and
 *     `preload="none"` with a poster, so no video bytes load before Play;
 *   - no `playsinline`, meant to let an iPhone play fullscreen, where the
 *     burned-in text is readable (expected WebKit behaviour, not verified);
 *   - a caption track that is NOT `default`. An enabled track is drawn over
 *     the text a video burns in at the bottom of its frame, and both become
 *     unreadable. The site serves `<stem>.site.vtt`, the approved captions
 *     with ` line:-5` on every cue, which lifts them above a recording's
 *     headline band (measured in three engines). An animation's captions
 *     repeat the text it already shows;
 *   - a closed transcript under each video. For a recording it is
 *     descriptive: every card and headline ("On screen:") and the sentences
 *     said over each, because some of what a recording states is only on
 *     screen. For an animation it is the text of its captions.
 *
 * EVERY SENTENCE HERE IS CHECKED. The section's own copy is the plan's WORDS
 * table. A recording's transcript is a quotation of the video, held to the
 * storyboard and the render's templates by
 * `backend/tests/unit/showcase/test_site_walkthroughs.py`. Every transcript
 * is held to the pinned captions by `scripts/check-site-media.mjs`. Never
 * edit the transcript of an unchanged video: re-render it (a new file name)
 * or revert the change.
 *
 * Adding a video changes no code: an entry in `content/videos.json`, its
 * transcript in `content/walkthroughs/<slug>.json`, its poster's frame in
 * `content/poster-frames.json` (`scripts/check-site-media.mjs`), and the
 * per-video rows of the tests that pin them (the slug list in
 * `tests/pages/walkthroughs.test.tsx`, the description's facts in
 * `test_site_walkthroughs.py`).
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

export type Transcript = { slug: string; kind?: 'animation'; mp4: string; items: TranscriptItem[] };

export type WalkthroughKind = 'recording' | 'animation';

/** What each row says it is, in visible text. */
export const KIND_LABEL: Readonly<Record<WalkthroughKind, string>> = {
  recording: 'Narrated recording',
  animation: 'Animation without sound',
};

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

/** A recording unless the transcript says `"kind": "animation"`; any other `kind` fails the export. */
export function kindOf(transcript: Transcript): WalkthroughKind {
  if (!('kind' in transcript)) return 'recording';
  if (transcript.kind === 'animation') return 'animation';
  throw new Error(`${transcript.slug}: unknown transcript kind ${JSON.stringify(transcript.kind)}`);
}

function TranscriptItemView({ item }: { item: TranscriptItem }) {
  if ('on_screen' in item) {
    return <p className="text-slate-500">{onScreenLine(item.on_screen)}</p>;
  }
  if ('said' in item) {
    return <p className="text-slate-700">{item.said.join(' ')}</p>;
  }
  if ('code' in item) {
    // Wrapped, not scrolled: a card is a third of the page wide, and a block
    // that scrolls sideways must take focus (axe scrollable-region-focusable).
    return (
      <pre className="whitespace-pre-wrap break-words rounded-md bg-slate-900 p-4 text-xs leading-relaxed text-slate-100">
        <code>{item.code.join('\n')}</code>
      </pre>
    );
  }
  throw new Error(`a transcript item must be on_screen, said or code: ${JSON.stringify(item)}`);
}

/**
 * The section: a grid of one card per entry, three columns from `lg`, two
 * from `sm`, one below (#1459). It takes any number of entries in manifest
 * order. Each card is, in DOM order: its h3 title (so heading navigation lands
 * before the player), the player, the description, the kind, the length and
 * the transcript. No thumbnail, no play button over the video and no badge on
 * it: the browser's own controls, as before.
 */
export function Walkthroughs() {
  return (
    <section id="walkthroughs" aria-labelledby="walkthroughs-heading" className="scroll-mt-20 border-t border-slate-200">
      <div className="mx-auto max-w-[1180px] px-6 py-16">
        <h2 id="walkthroughs-heading" className="text-3xl font-semibold tracking-tight">
          Walkthroughs
        </h2>
        <p className="mt-3 max-w-2xl text-slate-600">
          The recordings were made on a local stack with demo data. Turn captions on in the player; the transcript is
          below each video.
        </p>
        <ol role="list" className="mt-10 grid gap-x-7 gap-y-10 sm:grid-cols-2 lg:grid-cols-3">
          {WALKTHROUGHS.map((video) => {
            const id = anchorFor(video.slug);
            const length = lengthLabel(video.duration_seconds);
            const transcript = transcriptFor(video.slug);
            const kind = kindOf(transcript);
            return (
              <li key={video.slug} id={id} className="min-w-0 scroll-mt-20">
                <h3 id={`${id}-title`} className="text-lg font-semibold">
                  {video.title}
                </h3>
                <video
                  className={`mt-3 block h-auto w-full rounded-lg border border-slate-200 bg-slate-900 ${FOCUS}`}
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
                <p id={`${id}-description`} className="mt-3 text-sm leading-relaxed text-slate-600">
                  {video.description}
                </p>
                <p className="mt-2 text-sm text-slate-600" data-kind={kind}>
                  {KIND_LABEL[kind]}
                </p>
                <p className="mt-1 text-sm text-slate-600">
                  <span aria-hidden="true">{length.shown}</span>
                  <span className="sr-only">{length.spoken}</span>
                </p>
                <details className="mt-2">
                  <summary className={`cursor-pointer py-2 text-sm font-medium text-blue-700 ${FOCUS}`}>
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
