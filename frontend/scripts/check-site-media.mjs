#!/usr/bin/env node
/**
 * What the walkthrough files contain, checked in CI over the bytes the
 * marketing build just downloaded (#1290).
 *
 *   npm run build:marketing && node scripts/check-site-media.mjs [outDir]
 *
 * `scripts/fetch-site-media.mjs` pins each file by size and sha256 and does
 * nothing else in the deploy path. These checks belong to the `Frontend
 * Tests` job instead: they read pinned bytes, so a result here holds for
 * every later build of the same manifest, and a check that could fail only in
 * Amplify would block a deploy with nobody watching. Per manifest entry:
 *
 *   mp4      the pinned bytes; `ftyp` first and `moov` before `mdat`
 *            (faststart, so playback starts before the whole file arrives);
 *            a video track and an AAC audio track lasting the video (every
 *            listed recording is narrated); `duration_seconds` is the movie's
 *            length rounded to the nearest second
 *   captions `<stem>.site.vtt` has ` line:-5` on every cue timing line, and
 *            without it is the pinned `.vtt`, byte for byte; WebVTT, cues in
 *            order, the last ending inside the video
 *   words    the transcript's spoken lines (`src/content/walkthroughs/
 *            <slug>.json`) are the cues' text, in order; it names this mp4
 *   poster   the pinned bytes; a JPEG of 1280x720, at most 64 KiB
 *   frame    the poster is a frame of the video itself, not its title card
 *            (#1459): see POSTER FRAMES below
 *   all      the project's former name in none of the downloaded bytes
 *
 * That is a recording: a transcript with no `kind`. An ANIMATION (the
 * transcript says `"kind": "animation"`, T178 condition 2) gets every check
 * above but two: its mp4 has no audio track at all, and its transcript is only
 * on-screen items of one caption each, equal to the cues' text in order. Any
 * other `kind` is refused. Whether the captions are the animation's own text
 * is checked outside CI, by re-running the extractor that wrote them.
 *
 * and in `out/index.html`: one `<video>` per entry, wired as the section
 * renders it (`src/components/Walkthroughs.tsx`), its one caption track not
 * `default`, no `<iframe>`, and every `src` and `poster` on this site.
 *
 * POSTER FRAMES (#1459). `src/content/poster-frames.json` is
 * `{"posters": [row, ...]}`, one row per manifest entry and none for a video
 * the manifest does not list, each row exactly
 *
 *   {"slug", "mp4_sha256", "poster_sha256", "t", "first_cue", "crop", "pad", "psnr_db"}
 *
 * It says which frame of the video the poster is: the frame at `t` seconds of
 * the pinned mp4, transformed one of two ways (#1459, the founder's D200):
 *
 *   - a recording: `crop` [1920, 940, 0, 0], the whole page above the 140 px
 *     caption band and nothing narrower, scaled to 1280x627 and padded top
 *     and bottom to 1280x720 with the page's own background colour, `pad`
 *     (`#rrggbb`);
 *   - an animation (its transcript says `"kind": "animation"`): `crop` and
 *     `pad` null, the whole frame scaled to 1280x720.
 *
 * `psnr_db` is the poster against that frame, transformed the same way: at
 * least 35, measured with ffmpeg when the founder approves the poster's upload.
 *
 * CI CANNOT CHECK THE PIXELS. The runner has no ffmpeg, so nothing here
 * verifies that the poster is the frame its row names: `psnr_db` is a typed
 * number, and a swapped poster with a hand-written row (its new sha256, a
 * plausible t, psnr_db 41) passes. That is verified by re-running the ffmpeg
 * commands: by whoever asks for the upload approval, and by the reviewer of
 * every manifest PR that adds or changes a row. What CI checks, with no ffmpeg:
 *
 *   - the row's mp4_sha256 and poster_sha256 are the manifest's, so neither
 *     file can change without its row being written again;
 *   - `first_cue` is the start of the first cue of the placed
 *     `out/media/<stem>.site.vtt`, read from that file;
 *   - `t` is at or after that start, and inside the video. A recording opens
 *     on its title card and its first cue starts as the card ends, so a frame
 *     before the first cue is the card. A title card is a frame of its own
 *     video too, which is why only a pinned frame time can tell it apart;
 *   - `psnr_db` is a number of at least 35; `crop` is exactly
 *     [1920, 940, 0, 0] or null, and null only for an animation; `pad` is a
 *     `#rrggbb` colour with that crop and null with a null crop.
 *
 * Exit 0 when all hold; 1 with every problem listed.
 */
import { createHash } from 'node:crypto';
import { existsSync, readdirSync, readFileSync, realpathSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { MANIFEST, SITE_SETTING, parseManifest, placedNames, stemOf } from './fetch-site-media.mjs';

const TRANSCRIPTS = fileURLToPath(new URL('../src/content/walkthroughs/', import.meta.url));
export const POSTER_FRAMES = fileURLToPath(new URL('../src/content/poster-frames.json', import.meta.url));
/** The least PSNR, in dB, of a poster against the frame its row names. */
export const MIN_PSNR_DB = 35;
const FRAME_KEYS = ['slug', 'mp4_sha256', 'poster_sha256', 't', 'first_cue', 'crop', 'pad', 'psnr_db'];
const COLOUR = /^#[0-9a-f]{6}$/;
/** A recording poster's crop (D200): the whole 1920x940 page above the caption band. */
const PAGE_CROP = [1920, 940, 0, 0];
// Assembled, so this file never contains the word it looks for
// (scripts/check_legacy_name.py does the same).
const FORMER_NAME = ['experi', 'mently'].join('');
export const POSTER = { width: 1280, height: 720, maxBytes: 64 * 1024 };

const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');

/** Top-level (or child) boxes of an ISO BMFF buffer: [{type, start, end}] with start at the payload. */
export function boxes(buf, from = 0, to = buf.length) {
  const out = [];
  let pos = from;
  while (pos + 8 <= to) {
    let size = buf.readUInt32BE(pos);
    const type = buf.toString('latin1', pos + 4, pos + 8);
    let header = 8;
    if (size === 1) {
      size = Number(buf.readBigUInt64BE(pos + 8));
      header = 16;
    } else if (size === 0) {
      size = to - pos;
    }
    if (size < header || pos + size > to) throw new Error(`a box ${JSON.stringify(type)} at ${pos} runs past its parent`);
    out.push({ type, start: pos + header, end: pos + size });
    pos += size;
  }
  return out;
}

const child = (buf, box, type) => boxes(buf, box.start, box.end).find((b) => b.type === type);

/** Faststart, the movie's length, and each track's handler, sample entry and length. */
export function mp4Facts(buf) {
  const top = boxes(buf);
  const order = top.map((b) => b.type);
  const moov = top.find((b) => b.type === 'moov');
  if (!moov) return { order, faststart: false, duration: null, tracks: [] };
  const timed = (box, v1Offset, v0Offset) => {
    const version = buf.readUInt8(box.start);
    return version === 1
      ? { scale: buf.readUInt32BE(box.start + v1Offset), length: Number(buf.readBigUInt64BE(box.start + v1Offset + 4)) }
      : { scale: buf.readUInt32BE(box.start + v0Offset), length: buf.readUInt32BE(box.start + v0Offset + 4) };
  };
  const mvhd = child(buf, moov, 'mvhd');
  const movie = mvhd ? timed(mvhd, 20, 12) : null;
  const tracks = boxes(buf, moov.start, moov.end)
    .filter((b) => b.type === 'trak')
    .map((trak) => {
      const mdia = child(buf, trak, 'mdia');
      const hdlr = mdia && child(buf, mdia, 'hdlr');
      const mdhd = mdia && child(buf, mdia, 'mdhd');
      const stbl = mdia && child(buf, mdia, 'minf') && child(buf, child(buf, mdia, 'minf'), 'stbl');
      const stsd = stbl && child(buf, stbl, 'stsd');
      const time = mdhd ? timed(mdhd, 20, 12) : null;
      return {
        handler: hdlr ? buf.toString('latin1', hdlr.start + 8, hdlr.start + 12) : null,
        sampleEntry: stsd ? buf.toString('latin1', stsd.start + 12, stsd.start + 16) : null,
        duration: time ? time.length / time.scale : null,
      };
    });
  return {
    order,
    faststart: order[0] === 'ftyp' && order.includes('mdat') && order.indexOf('moov') < order.indexOf('mdat'),
    duration: movie ? movie.length / movie.scale : null,
    tracks,
  };
}

/** A JPEG's frame size from its SOF marker, or null when it is not a whole JPEG. */
export function jpegSize(buf) {
  if (buf.length < 4 || buf[0] !== 0xff || buf[1] !== 0xd8 || buf[buf.length - 2] !== 0xff || buf[buf.length - 1] !== 0xd9) return null;
  let pos = 2;
  while (pos + 4 <= buf.length && buf[pos] === 0xff) {
    const marker = buf[pos + 1];
    if (marker === 0xd8 || (marker >= 0xd0 && marker <= 0xd7) || marker === 0x01) {
      pos += 2;
      continue;
    }
    const length = buf.readUInt16BE(pos + 2);
    if ([0xc0, 0xc1, 0xc2].includes(marker)) return { height: buf.readUInt16BE(pos + 5), width: buf.readUInt16BE(pos + 7) };
    if (marker === 0xda) return null;
    pos += 2 + length;
  }
  return null;
}

const seconds = (stamp) => stamp.split(':').reduce((total, part) => total * 60 + Number(part), 0);

/** Cues of a WebVTT file ({start, end, text}), or a list of what is wrong with it. */
export function parseVtt(text) {
  const problems = [];
  const lines = text.split('\n');
  if (!/^WEBVTT([ \t].*)?$/.test(lines[0])) problems.push('does not start with WEBVTT');
  const cues = [];
  const blocks = text.split('\n').slice(1).join('\n').split(/\n{2,}/).map((b) => b.split('\n').filter(Boolean)).filter((b) => b.length);
  for (const block of blocks) {
    const at = block.findIndex((l) => l.includes('-->'));
    if (at < 0 || at > 1) {
      problems.push(`a block that is not a cue: ${JSON.stringify(block[0])}`);
      continue;
    }
    const match = /^(\S+) --> (\S+)/.exec(block[at]);
    const text = block.slice(at + 1).join(' ');
    if (!match || !text) {
      problems.push(`a cue with no time or no text: ${JSON.stringify(block[at])}`);
      continue;
    }
    cues.push({ start: seconds(match[1]), end: seconds(match[2]), timing: block[at], text });
  }
  cues.forEach((cue, i) => {
    if (!(cue.start < cue.end)) problems.push(`cue ${i + 1} ends before it starts`);
    if (i && cue.start < cues[i - 1].start) problems.push(`cue ${i + 1} starts before cue ${i}`);
  });
  if (!cues.length) problems.push('has no cue');
  return { cues, problems };
}

/** Attributes of one start tag's attribute text, names lower-cased (React writes `srcLang`). */
export function attributes(source) {
  const attrs = new Map();
  for (const m of source.matchAll(/([^\s"'=<>/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g)) {
    attrs.set(m[1].toLowerCase(), m[2] ?? m[3] ?? m[4] ?? '');
  }
  return attrs;
}

const unescapeHtml = (s) => s.replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

/** Problems with the exported page against the manifest. */
export function pageProblems(html, videos) {
  const problems = [];
  const found = [...html.matchAll(/<video\b([^>]*)>([\s\S]*?)<\/video>/gi)];
  if (found.length !== videos.length) problems.push(`out/index.html has ${found.length} <video>, the manifest lists ${videos.length}`);
  found.slice(0, videos.length).forEach((match, i) => {
    const entry = videos[i];
    const where = `out/index.html <video> ${i + 1} (${entry.slug})`;
    const video = attributes(match[1]);
    const want = {
      src: `/media/${entry.mp4.name}`,
      poster: `/media/${entry.poster.name}`,
      preload: 'none',
      width: '1920',
      height: '1080',
    };
    for (const [name, value] of Object.entries(want)) {
      if (video.get(name) !== value) problems.push(`${where}: ${name}=${JSON.stringify(video.get(name) ?? null)}, want ${JSON.stringify(value)}`);
    }
    if (!video.has('controls')) problems.push(`${where}: no controls`);
    for (const name of ['autoplay', 'muted', 'loop', 'playsinline']) if (video.has(name)) problems.push(`${where}: ${name} is set`);
    for (const name of ['aria-labelledby', 'aria-describedby']) if (!video.get(name)) problems.push(`${where}: no ${name}`);
    const tracks = [...match[2].matchAll(/<track\b([^>]*?)\/?>/gi)].map((t) => attributes(t[1]));
    if (tracks.length !== 1) {
      problems.push(`${where}: ${tracks.length} <track>, want exactly 1`);
      return;
    }
    const track = tracks[0];
    const wantTrack = { kind: 'captions', srclang: 'en', label: 'English', src: `/media/${stemOf(entry)}.site.vtt` };
    for (const [name, value] of Object.entries(wantTrack)) {
      if (track.get(name) !== value) problems.push(`${where} <track>: ${name}=${JSON.stringify(track.get(name) ?? null)}, want ${JSON.stringify(value)}`);
    }
    if (track.has('default')) problems.push(`${where} <track>: default is set; an enabled track covers the burned-in headline`);
  });
  if (/<iframe\b/i.test(html)) problems.push('out/index.html has an <iframe>');
  for (const m of html.matchAll(/\s(src|poster)\s*=\s*"([^"]*)"/gi)) {
    const value = unescapeHtml(m[2]);
    if (!/^\/[^/]/.test(value)) problems.push(`out/index.html ${m[1]}=${JSON.stringify(value)} is not a path on this site`);
  }
  return problems;
}

/** Problems with one entry's files in out/media and its transcript. */
export function entryProblems(entry, mediaDir, transcriptDir = TRANSCRIPTS) {
  const problems = [];
  const where = entry.slug;
  const read = (name) => {
    const path = join(mediaDir, name);
    if (!existsSync(path)) {
      problems.push(`${where}: out/media/${name} is missing`);
      return null;
    }
    return readFileSync(path);
  };
  const pinned = (file, bytes) => {
    if (bytes.length !== file.bytes) problems.push(`${where}: ${file.name} is ${bytes.length} bytes, pinned ${file.bytes}`);
    else if (sha256(bytes) !== file.sha256) problems.push(`${where}: ${file.name} sha256 ${sha256(bytes).slice(0, 12)}, pinned ${file.sha256.slice(0, 12)}`);
  };

  // The transcript first: its `kind` decides the sound rule. No `kind` is a
  // recording, checked as before; "animation" is the only other kind.
  const transcriptPath = join(transcriptDir, `${entry.slug}.json`);
  const transcript = existsSync(transcriptPath) ? JSON.parse(readFileSync(transcriptPath, 'utf8')) : null;
  const animation = transcript !== null && transcript.kind === 'animation';
  if (transcript !== null && 'kind' in transcript && !animation) {
    problems.push(`${where}: the transcript's kind is ${JSON.stringify(transcript.kind)}; the only kind is "animation", and a recording declares none`);
  }

  const mp4 = read(entry.mp4.name);
  let duration = null;
  if (mp4) {
    pinned(entry.mp4, mp4);
    const facts = mp4Facts(mp4);
    duration = facts.duration;
    if (!facts.faststart) problems.push(`${where}: ${entry.mp4.name} is not faststart (top-level boxes ${facts.order.join(', ')}; want ftyp first and moov before mdat)`);
    if (duration === null) problems.push(`${where}: ${entry.mp4.name} has no movie header`);
    else if (Math.round(duration) !== entry.duration_seconds) {
      problems.push(`${where}: duration_seconds ${entry.duration_seconds}, but the video is ${duration.toFixed(3)} s (${Math.round(duration)} to the nearest second)`);
    }
    if (!facts.tracks.some((t) => t.handler === 'vide')) problems.push(`${where}: ${entry.mp4.name} has no video track`);
    const audio = facts.tracks.filter((t) => t.handler === 'soun');
    if (animation) {
      if (audio.length) problems.push(`${where}: ${entry.mp4.name} has ${audio.length} audio track(s); an animation has no sound`);
    } else if (audio.length !== 1 || audio[0].sampleEntry !== 'mp4a') {
      problems.push(`${where}: ${entry.mp4.name} has ${audio.length} audio track(s) (${audio.map((t) => t.sampleEntry).join(', ') || 'none'}); every listed video is narrated, in AAC`);
    } else if (duration !== null && Math.abs(audio[0].duration - duration) > 0.5) {
      problems.push(`${where}: the narration lasts ${audio[0].duration.toFixed(3)} s of a ${duration.toFixed(3)} s video`);
    }
  }

  const poster = read(entry.poster.name);
  if (poster) {
    pinned(entry.poster, poster);
    const size = jpegSize(poster);
    if (!size) problems.push(`${where}: ${entry.poster.name} is not a whole JPEG`);
    else if (size.width !== POSTER.width || size.height !== POSTER.height) problems.push(`${where}: the poster is ${size.width}x${size.height}, want ${POSTER.width}x${POSTER.height}`);
    if (poster.length > POSTER.maxBytes) problems.push(`${where}: the poster is ${poster.length} bytes, over ${POSTER.maxBytes}`);
  }

  const siteName = `${stemOf(entry)}.site.vtt`;
  const site = read(siteName);
  let cues = [];
  if (site) {
    const text = site.toString('utf8');
    if (text.includes('\r')) problems.push(`${where}: ${siteName} has a CR character`);
    const timing = text.split('\n').filter((l) => l.includes('-->'));
    const bare = timing.filter((l) => !l.endsWith(SITE_SETTING));
    if (bare.length) problems.push(`${where}: ${siteName} has ${bare.length} cue timing line(s) without${SITE_SETTING}`);
    const approved = Buffer.from(text.split('\n').map((l) => (l.includes('-->') && l.endsWith(SITE_SETTING) ? l.slice(0, -SITE_SETTING.length) : l)).join('\n'), 'utf8');
    if (approved.length !== entry.vtt.bytes || sha256(approved) !== entry.vtt.sha256) {
      problems.push(`${where}: ${siteName} without${SITE_SETTING} is not the pinned ${entry.vtt.name} (the transform changed something else)`);
    }
    const parsed = parseVtt(approved.toString('utf8'));
    problems.push(...parsed.problems.map((p) => `${where}: ${entry.vtt.name} ${p}`));
    cues = parsed.cues;
    if (cues.length && duration !== null && cues[cues.length - 1].end > duration + 0.001) {
      problems.push(`${where}: the last cue ends at ${cues[cues.length - 1].end} s, after the video's ${duration.toFixed(3)} s`);
    }
  }

  if (transcript === null) {
    problems.push(`${where}: no transcript at src/content/walkthroughs/${entry.slug}.json`);
  } else if (animation) {
    if (transcript.slug !== entry.slug) problems.push(`${where}: the transcript says slug ${JSON.stringify(transcript.slug)}`);
    if (transcript.mp4 !== entry.mp4.name) {
      problems.push(`${where}: the transcript is of ${JSON.stringify(transcript.mp4)}, the manifest lists ${entry.mp4.name}. Write it from the listed video's captions`);
    }
    const items = Array.isArray(transcript.items) ? transcript.items : [];
    const other = items.filter((item) => Object.keys(item).length !== 1 || !Array.isArray(item.on_screen) || item.on_screen.length !== 1);
    if (other.length) {
      problems.push(`${where}: an animation's transcript is on-screen items of one caption each; it has ${other.length} other item(s), the first ${JSON.stringify(other[0])}`);
    }
    const shown = items.flatMap((item) => item.on_screen ?? []);
    const captions = cues.map((c) => c.text);
    if (site && JSON.stringify(shown) !== JSON.stringify(captions)) {
      const at = shown.findIndex((s, i) => s !== captions[i]);
      const i = at < 0 ? Math.min(shown.length, captions.length) : at;
      problems.push(
        `${where}: the animation's transcript is not its captions (${shown.length} lines, ${captions.length} cues; ` +
          `first difference at ${i + 1}: ${JSON.stringify(shown[i] ?? null)} vs ${JSON.stringify(captions[i] ?? null)})`,
      );
    }
  } else {
    if (transcript.slug !== entry.slug) problems.push(`${where}: the transcript says slug ${JSON.stringify(transcript.slug)}`);
    if (transcript.mp4 !== entry.mp4.name) {
      problems.push(`${where}: the transcript is of ${JSON.stringify(transcript.mp4)}, the manifest lists ${entry.mp4.name}. Assemble it from the approved review page of the listed video`);
    }
    const said = (transcript.items ?? []).flatMap((item) => item.said ?? []);
    const spoken = cues.map((c) => c.text);
    if (site && JSON.stringify(said) !== JSON.stringify(spoken)) {
      const at = said.findIndex((s, i) => s !== spoken[i]);
      const i = at < 0 ? Math.min(said.length, spoken.length) : at;
      problems.push(
        `${where}: the transcript's spoken lines are not the captions (${said.length} lines, ${spoken.length} cues; ` +
          `first difference at ${i + 1}: ${JSON.stringify(said[i] ?? null)} vs ${JSON.stringify(spoken[i] ?? null)})`,
      );
    }
  }

  for (const name of placedNames(entry)) {
    const path = join(mediaDir, name);
    if (existsSync(path) && readFileSync(path).toString('latin1').toLowerCase().includes(FORMER_NAME)) {
      problems.push(`${where}: ${name} contains the project's former name`);
    }
  }
  return problems;
}

const hasExactly = (value, keys) =>
  value !== null && typeof value === 'object' && !Array.isArray(value) &&
  Object.keys(value).length === keys.length && keys.every((k) => Object.prototype.hasOwnProperty.call(value, k));
const finite = (value) => typeof value === 'number' && Number.isFinite(value);
const ms = (value) => Math.round(value * 1000);

/**
 * Problems with the poster-frames file (its `text`) against the manifest's
 * entries, the files placed in `mediaDir` and the transcripts' kinds
 * (POSTER FRAMES above).
 */
export function frameProblems(text, videos, mediaDir, transcriptDir) {
  let data;
  try {
    data = JSON.parse(text);
  } catch (error) {
    return [`poster-frames.json is not JSON: ${error.message}`];
  }
  if (!hasExactly(data, ['posters']) || !Array.isArray(data.posters)) return ['poster-frames.json must be {"posters": [...]} and nothing else'];
  const problems = [];
  const rows = new Map();
  data.posters.forEach((row, i) => {
    if (!hasExactly(row, FRAME_KEYS)) problems.push(`poster-frames.json posters[${i}] must have exactly the keys ${FRAME_KEYS.join(', ')}`);
    else if (rows.has(row.slug)) problems.push(`poster-frames.json has two rows for ${JSON.stringify(row.slug)}`);
    else rows.set(row.slug, row);
  });
  const listed = new Set(videos.map((entry) => entry.slug));
  for (const slug of rows.keys()) {
    if (!listed.has(slug)) problems.push(`poster-frames.json has a row for ${JSON.stringify(slug)}, which videos.json does not list`);
  }
  for (const entry of videos) {
    const where = `${entry.slug}: poster-frames.json`;
    const row = rows.get(entry.slug);
    if (!row) {
      problems.push(`${where} has no row; when ${entry.poster.name}'s upload is approved, measure which frame of the video it is and add one`);
      continue;
    }
    if (row.mp4_sha256 !== entry.mp4.sha256) problems.push(`${where} mp4_sha256 ${String(row.mp4_sha256).slice(0, 12)} is not the manifest's ${entry.mp4.sha256.slice(0, 12)}`);
    if (row.poster_sha256 !== entry.poster.sha256) {
      problems.push(`${where} poster_sha256 ${String(row.poster_sha256).slice(0, 12)} is not the manifest's ${entry.poster.sha256.slice(0, 12)}: the poster changed and its frame was not measured again`);
    }
    const isPage = Array.isArray(row.crop) && row.crop.length === PAGE_CROP.length && row.crop.every((n, i) => n === PAGE_CROP[i]);
    if (row.crop !== null && !isPage) {
      problems.push(`${where} crop ${JSON.stringify(row.crop)} is not the page, [1920, 940, 0, 0]: a recording's poster is the whole page above the caption band, padded, never cut (D200)`);
    } else if (row.crop === null) {
      // Unreadable or missing, the transcript cannot show an animation, so the null crop is refused (the stricter case).
      let kind;
      try {
        kind = JSON.parse(readFileSync(join(transcriptDir, `${entry.slug}.json`), 'utf8')).kind;
      } catch (error) {
        problems.push(`${where}: cannot read src/content/walkthroughs/${entry.slug}.json (${error.message}), so a null crop cannot be shown to be an animation's`);
      }
      if (kind !== 'animation') problems.push(`${where} crop is null (the whole frame), but ${entry.slug} is not an animation: a recording's caption band comes off, and only an animation keeps its whole frame`);
    }
    if (row.pad !== null && !(typeof row.pad === 'string' && COLOUR.test(row.pad))) {
      problems.push(`${where} pad must be null or a colour written #rrggbb`);
    } else if (row.crop === null && row.pad !== null) {
      problems.push(`${where} pad is ${row.pad}, but the crop is null: the whole frame is 16:9 already, so pad must be null`);
    } else if (isPage && row.pad === null) {
      problems.push(`${where} crop is the page (1920x940, not 16:9), so pad must name the colour it is padded with to 1280x720`);
    }
    const notNumbers = ['t', 'first_cue', 'psnr_db'].filter((key) => !finite(row[key]) || row[key] < 0);
    if (notNumbers.length) {
      problems.push(`${where} ${notNumbers.join(', ')} must be a number of at least 0`);
      continue;
    }
    if (row.psnr_db < MIN_PSNR_DB) problems.push(`${where} psnr_db ${row.psnr_db} is under ${MIN_PSNR_DB}: the poster is not the frame its row names`);
    // The first cue as placed, read from the file: never a typed value.
    const siteName = `${stemOf(entry)}.site.vtt`;
    const sitePath = join(mediaDir, siteName);
    const cues = existsSync(sitePath) ? parseVtt(readFileSync(sitePath, 'utf8')).cues : [];
    if (!cues.length) {
      problems.push(`${where}: out/media/${siteName} is missing or has no cue, so t cannot be held to the first cue`);
      continue;
    }
    const first = cues[0].start;
    if (ms(row.first_cue) !== ms(first)) problems.push(`${where} first_cue ${row.first_cue} s, but the first cue of out/media/${siteName} starts at ${first} s`);
    // In whole milliseconds, a WebVTT timestamp's precision: a t less than half a millisecond before the cue (2.9996 s against 3.000 s) counts as at it.
    if (ms(row.t) < ms(first)) problems.push(`${where} t ${row.t} s is before the first cue (${first} s): that frame is the title card`);
    const mp4Path = join(mediaDir, entry.mp4.name);
    const duration = existsSync(mp4Path) ? mp4Facts(readFileSync(mp4Path)).duration : null;
    if (duration === null) problems.push(`${where}: out/media/${entry.mp4.name} is missing or has no length, so t cannot be held inside the video`);
    else if (row.t >= duration) problems.push(`${where} t ${row.t} s is not inside the ${duration.toFixed(3)} s video`);
  }
  return problems;
}

export function check(outDir, manifestPath = MANIFEST, transcriptDir = TRANSCRIPTS, framesPath = POSTER_FRAMES) {
  const videos = parseManifest(readFileSync(manifestPath, 'utf8'));
  const mediaDir = join(outDir, 'media');
  const problems = [];
  const indexPath = join(outDir, 'index.html');
  if (!existsSync(indexPath)) return [`${indexPath} is missing; run npm run build:marketing first`];
  if (!existsSync(mediaDir)) return [`${mediaDir} is missing; npm run build:marketing places it`];
  const want = videos.flatMap(placedNames).sort();
  const have = readdirSync(mediaDir).sort();
  if (JSON.stringify(want) !== JSON.stringify(have)) problems.push(`out/media holds ${JSON.stringify(have)}, want ${JSON.stringify(want)}`);
  for (const entry of videos) problems.push(...entryProblems(entry, mediaDir, transcriptDir));
  if (!existsSync(framesPath)) problems.push(`${framesPath} is missing: every listed poster needs its frame row`);
  else problems.push(...frameProblems(readFileSync(framesPath, 'utf8'), videos, mediaDir, transcriptDir));
  problems.push(...pageProblems(readFileSync(indexPath, 'utf8'), videos));
  return problems;
}

function invokedAsScript() {
  // Real paths on both sides, so a symlinked path cannot turn the check into
  // a silent pass.
  if (!process.argv[1]) return false;
  try {
    return realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
}

if (invokedAsScript()) {
  const outDir = resolve(process.argv[2] ?? 'out');
  const problems = check(outDir);
  if (problems.length) {
    for (const p of problems) console.error(`check-site-media: FAIL ${p}`);
    process.exitCode = 1;
  } else {
    const count = parseManifest(readFileSync(MANIFEST, 'utf8')).length;
    console.log(`check-site-media: ${count} video(s): pinned bytes, faststart, sound as declared (recordings: AAC narration; animations: none), captions = pinned .vtt + line:-5, transcript = cues, poster 1280x720, poster frame at or after the first cue, page wiring: all hold`);
  }
}
