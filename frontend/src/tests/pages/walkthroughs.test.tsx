/**
 * The walkthrough videos on the homepage (#1290): rendered in the marketing
 * build only, wired as the plan says, and carrying no claim a test does not
 * hold.
 *
 * What each block pins (the plan's gate names):
 *   G3   the marketing build renders exactly the pinned videos, the platform
 *        build none; each player's attributes, its one caption track (not
 *        `default`), names, headings, and axe's `video-caption` in `passes`
 *   G4   every `src` and `poster` is a path on this site; no `<iframe>`
 *   C10  each player is named by its title and described by its description;
 *        each transcript summary has a name of its own
 *   C11  the platform homepage has no section, no player and no `/media/`
 *   C12  showing the section and opening a transcript writes no storage and
 *        no cookie, so `/privacy` stays true
 *   C13  the section promises nothing and claims no accessibility standard
 *   C16  the anchors exist and every id on the page is unique
 * and the copy is the plan's WORDS table, verbatim. The transcripts are held
 * to the videos by `backend/tests/unit/showcase/test_site_walkthroughs.py`
 * (the storyboards) and `scripts/check-site-media.mjs` (the captions).
 *
 * Two kinds of video (T178 condition 2). A recording has no `kind`, and its
 * transcript opens with the title card and ends with the "Recorded on" line.
 * An animation (`"kind": "animation"`) has neither: only on-screen items, one
 * caption each. Its title, description and label say nothing of narration,
 * recording or a local stack, and its row says in visible text that it is an
 * animation without sound. Any other `kind` is refused.
 *
 * The flag is mocked, as in `index.site-mode.test.tsx`: SITE_MODE is read at
 * module load, and `isolateModules` would load a second React.
 */
import fs from 'fs';
import path from 'path';
import React from 'react';
import axe from 'axe-core';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AuthProvider } from '@/contexts/AuthContext';
import HomePage from '@/pages/index';
import { isMarketingSite } from '@/utils/site-mode';
import {
  KIND_LABEL,
  Transcript,
  WALKTHROUGHS,
  WalkthroughVideo,
  Walkthroughs,
  anchorFor,
  kindOf,
  lengthLabel,
  onScreenLine,
  siteCaptionsFor,
  transcriptFor,
} from '@/components/Walkthroughs';

jest.mock('@/utils/site-mode', () => ({
  ...jest.requireActual('@/utils/site-mode'),
  isMarketingSite: jest.fn(),
}));
jest.mock('next/head', () => { const H = ({ children }: { children: React.ReactNode }) => <>{children}</>; H.displayName = 'H'; return H; });
jest.mock('next/router', () => ({ useRouter: () => ({ pathname: '/', asPath: '/', query: {}, push: jest.fn(), replace: jest.fn(), isReady: true }) }));

/**
 * The videos the site lists, in order. Adding one to `content/videos.json`
 * fails here until it is added to this list too, and dropping one fails
 * however consistent everything else stays.
 */
const PINNED = [
  '01-getting-started',
  '02-first-experiment',
  '03-flag-rollout',
  '05-plan-and-team',
  '06-shoplab-ab-test',
  '08-warehouse-explainer',
  '09-design-in-depth',
];

const CONTENT = path.join(__dirname, '..', '..', 'content');

/**
 * T178 condition 2(e): what an animation's title, description and label may
 * not say. A function, so the plants below prove it fires.
 */
const NOT_FOR_AN_ANIMATION = [/narrat/i, /\brecord(ed|ing)\b/i, /local stack/i];
const animationCopyProblems = (video: WalkthroughVideo, label: string): string[] =>
  [video.title, video.description, label].flatMap((text) =>
    NOT_FOR_AN_ANIMATION.filter((p) => p.test(text)).map((p) => `${JSON.stringify(text)} matches ${p}`),
  );

/** T178 condition 2(c)/(d): an animation's transcript is its captions, one on-screen fragment each, and nothing else. */
const animationTranscriptProblems = (transcript: Transcript): string[] => {
  const problems: string[] = [];
  for (const item of transcript.items) {
    if (!('on_screen' in item) || Object.keys(item).length !== 1) problems.push(`not an on-screen item: ${JSON.stringify(item)}`);
    else if (item.on_screen.length !== 1) problems.push(`${item.on_screen.length} fragments, want 1: ${JSON.stringify(item)}`);
  }
  const text = JSON.stringify(transcript.items);
  if (/recorded on/i.test(text)) problems.push('says "Recorded on"');
  if (text.includes('{version}')) problems.push('has a {version} placeholder');
  return problems;
};
const mode = isMarketingSite as jest.Mock;
const renderHome = () => render(<AuthProvider><HomePage /></AuthProvider>);

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  mode.mockReset();
  // Every API call fails, as on the published site.
  global.fetch = jest.fn(() => Promise.reject(new Error('no API'))) as unknown as typeof fetch;
});

describe('the manifest and the transcripts', () => {
  it('lists exactly the pinned videos, in order', () => {
    expect(WALKTHROUGHS.map((v) => v.slug)).toEqual(PINNED);
  });

  it('has one transcript per listed video, of that video, and no other', () => {
    const files = fs.readdirSync(path.join(CONTENT, 'walkthroughs')).sort();
    expect(files).toEqual(PINNED.map((slug) => `${slug}.json`).sort());
    for (const video of WALKTHROUGHS) {
      const transcript = transcriptFor(video.slug);
      expect({ slug: transcript.slug, mp4: transcript.mp4 }).toEqual({ slug: video.slug, mp4: video.mp4.name });
    }
  });

  it('builds every transcript from on-screen, said and code items only', () => {
    for (const video of WALKTHROUGHS) {
      const transcript = transcriptFor(video.slug);
      const { items } = transcript;
      expect(items.length).toBeGreaterThan(2);
      for (const item of items) {
        const keys = Object.keys(item);
        expect(keys).toHaveLength(1);
        expect(['on_screen', 'said', 'code']).toContain(keys[0]);
        const lines = Object.values(item)[0] as unknown[];
        expect(lines.length).toBeGreaterThan(0);
        for (const line of lines) expect(typeof line).toBe('string');
        if (!('code' in item)) for (const line of lines) expect((line as string).trim()).not.toBe('');
      }
      if (kindOf(transcript) === 'recording') {
        // The title card opens every recording and the end card closes it.
        expect(items[0]).toEqual({ on_screen: ['Controlvar', video.title, 'A/B testing and feature flags you run yourself.'] });
        const end = items[items.length - 1] as { on_screen: string[] };
        expect(end.on_screen[end.on_screen.length - 1]).toMatch(/^Recorded on Controlvar v\d+\.\d+\.\d+, a local stack with demo data$/);
      } else {
        expect(animationTranscriptProblems(transcript)).toEqual([]);
      }
    }
  });

  it('declares no kind but "animation", and a recording by declaring none', () => {
    for (const video of WALKTHROUGHS) {
      const transcript = transcriptFor(video.slug);
      const keys = Object.keys(transcript).sort();
      expect(keys).toEqual('kind' in transcript ? ['items', 'kind', 'mp4', 'slug'] : ['items', 'mp4', 'slug']);
      expect(['recording', 'animation']).toContain(kindOf(transcript));
    }
  });

  // T178 condition 2(f): each planted defect is refused.
  const planted: Transcript = { slug: '09-toy', kind: 'animation', mp4: '09-toy-000000000000.mp4', items: [{ on_screen: ['A caption.'] }] };

  it('refuses a kind that is neither absent nor "animation"', () => {
    expect(() => kindOf({ ...planted, kind: 'silent' } as unknown as Transcript)).toThrow(/unknown transcript kind "silent"/);
  });

  it('refuses an animation transcript with a said item, or with the recordings\' end-card line', () => {
    expect(animationTranscriptProblems(planted)).toEqual([]);
    expect(animationTranscriptProblems({ ...planted, items: [...planted.items, { said: ['A caption.'] }] })).toEqual([
      'not an on-screen item: {"said":["A caption."]}',
    ]);
    const endCard = { on_screen: ['Recorded on Controlvar v0.36.0, a local stack with demo data'] };
    expect(animationTranscriptProblems({ ...planted, items: [...planted.items, endCard] })).toEqual(['says "Recorded on"']);
  });

  it('refuses an animation whose title, description or label says "Narrated"', () => {
    const video = WALKTHROUGHS[0];
    expect(animationCopyProblems({ ...video, title: 'A title', description: 'A description.' }, KIND_LABEL.animation)).toEqual([]);
    expect(animationCopyProblems({ ...video, title: 'A title', description: 'Narrated, with captions.' }, KIND_LABEL.animation)).toEqual([
      '"Narrated, with captions." matches /narrat/i',
    ]);
  });
});

describe('length labels', () => {
  it.each([
    [84, '1:24', 'Length: 1 minute 24 seconds'],
    [87, '1:27', 'Length: 1 minute 27 seconds'],
    [60, '1:00', 'Length: 1 minute'],
    [61, '1:01', 'Length: 1 minute 1 second'],
    [121, '2:01', 'Length: 2 minutes 1 second'],
    [45, '0:45', 'Length: 45 seconds'],
  ])('%i s shows %s and says "%s"', (seconds, shown, spoken) => {
    expect(lengthLabel(seconds)).toEqual({ shown, spoken });
  });

  it('adds a full stop only where a fragment has none', () => {
    expect(onScreenLine(['Controlvar', 'Done.', 'Why?'])).toBe('On screen: Controlvar. Done. Why?');
  });
});

describe('marketing build (G3)', () => {
  beforeEach(() => mode.mockReturnValue(true));

  const section = (container: HTMLElement) => container.querySelector('section#walkthroughs') as HTMLElement;

  // #1459 moved the section from straight after the hero (#1290's place) to
  // between the SDKs and the deploy cards, the approved redesign's order.
  it('renders the section after the SDKs and before the deploy cards, with exactly the pinned videos in order', () => {
    const { container } = renderHome();
    const walk = section(container);
    expect(walk).toBeInTheDocument();
    expect(walk.previousElementSibling?.querySelector('h2')).toHaveTextContent(/^SDKs for the stack you have$/);
    expect(walk.nextElementSibling?.querySelector('h2')).toHaveTextContent(/^Deploy it yourself$/);
    expect(Array.from(walk.querySelectorAll('ol > li')).map((li) => li.id)).toEqual(PINNED.map(anchorFor));
    expect(walk.querySelectorAll('video')).toHaveLength(PINNED.length);
  });

  it('says exactly the words the plan wrote', () => {
    const { container } = renderHome();
    const walk = section(container);
    expect(within(walk).getByRole('heading', { level: 2 })).toHaveTextContent(/^Walkthroughs$/);
    // Sentence 1 covers the recordings only; each row says which kind it is.
    expect(walk.querySelector('h2 + p')?.textContent).toBe(
      'The recordings were made on a local stack with demo data. Turn captions on in the player; the transcript is below each video.',
    );
    for (const video of WALKTHROUGHS) {
      const id = anchorFor(video.slug);
      expect(walk.querySelector(`#${id}-title`)?.textContent).toBe(video.title);
      expect(walk.querySelector(`#${id}-description`)?.textContent).toBe(video.description);
    }
  });

  it('says on each row, in visible text, whether it is a narrated recording or an animation without sound', () => {
    const { container } = renderHome();
    for (const video of WALKTHROUGHS) {
      const kind = kindOf(transcriptFor(video.slug));
      const label = section(container).querySelector(`#${anchorFor(video.slug)} [data-kind]`) as HTMLElement;
      expect(label).toHaveAttribute('data-kind', kind);
      expect(label.textContent).toBe(KIND_LABEL[kind]);
      expect(label).not.toHaveAttribute('aria-hidden');
      expect(label).not.toHaveClass('sr-only');
    }
    expect(KIND_LABEL).toEqual({ recording: 'Narrated recording', animation: 'Animation without sound' });
  });

  it("keeps narration, recording and a local stack out of an animation's title, description and label", () => {
    const animations = WALKTHROUGHS.filter((v) => kindOf(transcriptFor(v.slug)) === 'animation');
    expect(animations.map((v) => v.slug)).toEqual(['08-warehouse-explainer']);
    for (const video of animations) expect(animationCopyProblems(video, KIND_LABEL.animation)).toEqual([]);
  });

  it('wires each player as the plan says', () => {
    const { container } = renderHome();
    for (const video of WALKTHROUGHS) {
      const el = section(container).querySelector(`#${anchorFor(video.slug)} video`) as HTMLVideoElement;
      expect(el).toHaveAttribute('src', `/media/${video.mp4.name}`);
      expect(el).toHaveAttribute('poster', `/media/${video.poster.name}`);
      expect(el).toHaveAttribute('preload', 'none');
      expect(el).toHaveAttribute('controls');
      expect(el).toHaveAttribute('width', '1920');
      expect(el).toHaveAttribute('height', '1080');
      for (const name of ['autoplay', 'muted', 'loop', 'playsinline']) expect(el).not.toHaveAttribute(name);
      const tracks = el.querySelectorAll('track');
      expect(tracks).toHaveLength(1);
      expect(tracks[0]).toHaveAttribute('kind', 'captions');
      expect(tracks[0]).toHaveAttribute('srclang', 'en');
      expect(tracks[0]).toHaveAttribute('label', 'English');
      expect(tracks[0]).toHaveAttribute('src', `/media/${siteCaptionsFor(video)}`);
      // On by default it would be drawn over the text the video burns in.
      expect(tracks[0]).not.toHaveAttribute('default');
    }
  });

  it('names each player by its title and describes it by its description (C10)', () => {
    renderHome();
    for (const video of WALKTHROUGHS) {
      const el = document.querySelector(`#${anchorFor(video.slug)} video`) as HTMLElement;
      expect(el).toHaveAccessibleName(video.title);
      expect(el).toHaveAccessibleDescription(video.description);
    }
    const summaries = Array.from(document.querySelectorAll('#walkthroughs summary'));
    const names = summaries.map((s) => s.textContent);
    expect(names).toEqual(WALKTHROUGHS.map((v) => `Transcript: ${v.title}`));
    expect(new Set(names).size).toBe(names.length);
  });

  it('shows each length as the player does, and says it in words', () => {
    const { container } = renderHome();
    for (const video of WALKTHROUGHS) {
      const row = section(container).querySelector(`#${anchorFor(video.slug)}`) as HTMLElement;
      const length = lengthLabel(video.duration_seconds);
      const shown = within(row).getByText(length.shown);
      expect(shown).toHaveAttribute('aria-hidden', 'true');
      expect(within(row).getByText(length.spoken)).toHaveClass('sr-only');
    }
  });

  it('heads the section h2 and each video h3', () => {
    const { container } = renderHome();
    const levels = Array.from(section(container).querySelectorAll('h1, h2, h3, h4, h5, h6')).map((h) => h.tagName);
    expect(levels).toEqual(['H2', ...WALKTHROUGHS.map(() => 'H3')]);
  });

  it("passes axe's video-caption rule for every video, with no violation in the section", async () => {
    const { container } = renderHome();
    const result = await axe.run(section(container), { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => v.id)).toEqual([]);
    // Without a captions track axe reports `incomplete`, not a violation, so
    // the pass has to be asserted.
    const passed = result.passes.find((r) => r.id === 'video-caption');
    expect(passed?.nodes).toHaveLength(WALKTHROUGHS.length);
  });

  it('closes each transcript under its video, and writes each item in order', () => {
    const { container } = renderHome();
    for (const video of WALKTHROUGHS) {
      const row = section(container).querySelector(`#${anchorFor(video.slug)}`) as HTMLElement;
      const details = row.querySelector('details') as HTMLDetailsElement;
      expect(details).not.toHaveAttribute('open');
      expect(row.querySelector('video')!.compareDocumentPosition(details) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      const body = details.querySelector('summary + div') as HTMLElement;
      const want = transcriptFor(video.slug).items.map((item) =>
        'on_screen' in item ? onScreenLine(item.on_screen) : 'said' in item ? item.said.join(' ') : item.code.join('\n'),
      );
      expect(Array.from(body.children).map((el) => el.textContent)).toEqual(want);
    }
  });

  it('loads nothing from anywhere but this site (G4)', () => {
    const { container } = renderHome();
    const walk = section(container);
    expect(walk.querySelector('iframe')).toBeNull();
    const urls = Array.from(walk.querySelectorAll('[src], [poster]')).flatMap((el) =>
      ['src', 'poster'].map((a) => el.getAttribute(a)).filter((v): v is string => v !== null),
    );
    expect(urls.length).toBe(WALKTHROUGHS.length * 3);
    for (const url of urls) expect(url).toMatch(/^\/media\/[^/]+$/);
  });

  it('writes no storage and no cookie, even with a transcript open (C12)', async () => {
    render(<Walkthroughs />);
    for (const summary of Array.from(document.querySelectorAll('summary'))) await userEvent.click(summary);
    expect(Array.from(document.querySelectorAll('details')).every((d) => d.open)).toBe(true);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    expect(document.cookie).toBe('');
  });

  /**
   * C13: nothing about videos that do not exist yet, no YouTube (it would
   * need a line on /privacy), and no accessibility claim: the site claims no
   * accessibility standard (T176 condition 13). Fragments, so the words this
   * forbids never appear whole in this file.
   */
  const promises: Array<[string, string]> = [
    ['coming\\s+' + 'soon', 'no video is promised'],
    ['\\b' + 'soon\\b', 'the same'],
    ['more\\s+' + 'videos', 'no count, no series'],
    ['stay\\s+' + 'tuned', 'the same'],
    ['you' + 'tube', 'no third-party copy is linked'],
    ['accessi' + 'b', 'the site claims no accessibility standard'],
    ['WC' + 'AG', 'the same'],
  ];

  it.each(promises)('the section says nothing matching /%s/ (%s)', (pattern) => {
    const { container } = renderHome();
    expect(section(container).textContent ?? '').not.toMatch(new RegExp(pattern, 'i'));
  });

  it('has the anchors, and every id on the page is unique (C16)', () => {
    const { container } = renderHome();
    expect(container.querySelector('#walkthroughs')).toBeInTheDocument();
    for (const slug of PINNED) expect(container.querySelector(`#${anchorFor(slug)}`)).toBeInTheDocument();
    const ids = Array.from(container.querySelectorAll('[id]')).map((el) => el.id);
    expect(ids.filter((id, i) => ids.indexOf(id) !== i)).toEqual([]);
  });
});

describe('platform build (C11)', () => {
  beforeEach(() => mode.mockReturnValue(false));

  it('renders no section, no player and no media path', async () => {
    const { container } = renderHome();
    await screen.findByRole('link', { name: /sign in to the dashboard/i });
    expect(container.querySelector('#walkthroughs')).toBeNull();
    expect(container.querySelectorAll('video')).toHaveLength(0);
    expect(container.textContent).not.toMatch(/Walkthroughs/);
    expect(container.innerHTML).not.toContain('/media/');
  });
});
