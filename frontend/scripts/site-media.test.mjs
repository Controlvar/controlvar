/**
 * The walkthrough build scripts, offline (#1290):
 *
 *   node --test scripts/site-media.test.mjs
 *
 * Node's own runner, because jest here compiles TypeScript to CommonJS and
 * cannot import an ES module script. `Frontend Tests` runs this file before
 * the marketing build. Every download goes through an injected `fetch`, so
 * nothing here reaches GitHub; the real downloads are the build itself.
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, it } from 'node:test';

import {
  MANIFEST,
  MAX_ATTEMPTS,
  MAX_WAIT_MS,
  RELEASE_URL,
  Refusal,
  classifyResponse,
  download,
  main,
  mediaReferences,
  parseManifest,
  referenceProblems,
  retryAfterMs,
  toSiteVtt,
} from './fetch-site-media.mjs';
import { attributes, entryProblems, jpegSize, mp4Facts, pageProblems, parseVtt } from './check-site-media.mjs';
import { platformProblems } from './check-platform-export.mjs';

const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');
const headers = (pairs = {}) => new Headers(pairs);
const scratch = () => mkdtempSync(join(tmpdir(), 'site-media-test-'));

// --- the retry rule (PE 5) ------------------------------------------------------------

describe('classifyResponse', () => {
  it('retries a 429, as rate limiting', () => {
    assert.deepEqual(classifyResponse(429, headers()), { action: 'retry', rateLimited: true, waitMs: null });
  });

  it('refuses a 404 at once', () => {
    assert.deepEqual(classifyResponse(404, headers()), { action: 'refuse', rateLimited: false, waitMs: null });
  });

  it('honours Retry-After in seconds and as a date', () => {
    assert.equal(classifyResponse(429, headers({ 'retry-after': '7' })).waitMs, 7000);
    const now = Date.parse('2026-10-18T12:00:00Z');
    assert.equal(classifyResponse(503, headers({ 'retry-after': 'Sun, 18 Oct 2026 12:00:30 GMT' }), now).waitMs, 30_000);
  });

  it('retries a 403 only when it says it is a rate limit', () => {
    assert.equal(classifyResponse(403, headers({ 'retry-after': '1' })).action, 'retry');
    assert.equal(classifyResponse(403, headers({ 'x-ratelimit-remaining': '0' })).action, 'retry');
    assert.equal(classifyResponse(403, headers({ 'x-ratelimit-remaining': '0' })).rateLimited, true);
    assert.equal(classifyResponse(403, headers()).action, 'refuse');
  });

  it('retries a server error, and accepts a 2xx', () => {
    assert.equal(classifyResponse(502, headers()).action, 'retry');
    assert.equal(classifyResponse(502, headers()).rateLimited, false);
    assert.equal(classifyResponse(200, headers()).action, 'ok');
  });

  it('reads no wait from a Retry-After it cannot parse', () => {
    assert.equal(retryAfterMs('soon'), null);
    assert.equal(retryAfterMs(null), null);
  });
});

/** A fetch that answers from a script of responses (or errors) and records every call. */
function scriptedFetch(answers) {
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(url);
    const next = answers.shift();
    if (next instanceof Error) throw next;
    return next;
  };
  return { fetchImpl, calls };
}

function clock() {
  let t = 0;
  const waits = [];
  return { now: () => t, sleep: async (ms) => { waits.push(ms); t += ms; }, waits };
}

const FILE = { name: 'a.mp4', bytes: 5, sha256: sha(Buffer.from('hello')) };

describe('download', () => {
  it('retries a 429 after the Retry-After it was given, then takes the file', async () => {
    const { fetchImpl, calls } = scriptedFetch([
      new Response('slow down', { status: 429, headers: { 'retry-after': '3' } }),
      new Response('hello', { status: 200 }),
    ]);
    const c = clock();
    const bytes = await download(FILE, { deadline: 600_000, fetchImpl, ...c });
    assert.equal(bytes.toString(), 'hello');
    assert.deepEqual(c.waits, [3000]);
    assert.deepEqual(calls, [RELEASE_URL + 'a.mp4', RELEASE_URL + 'a.mp4']);
  });

  it('refuses a 404 without asking again', async () => {
    const { fetchImpl, calls } = scriptedFetch([new Response('Not Found', { status: 404 })]);
    await assert.rejects(download(FILE, { deadline: 600_000, fetchImpl, ...clock() }), (e) => e instanceof Refusal && /HTTP 404/.test(e.message));
    assert.equal(calls.length, 1);
  });

  it('names rate limiting when the retries run out', async () => {
    const answers = Array.from({ length: MAX_ATTEMPTS }, () => new Response('', { status: 429 }));
    const { fetchImpl, calls } = scriptedFetch(answers);
    await assert.rejects(download(FILE, { deadline: 600_000, fetchImpl, ...clock() }), /rate-limited/);
    assert.equal(calls.length, MAX_ATTEMPTS);
  });

  it('gives up at once on a Retry-After longer than it may wait', async () => {
    const { fetchImpl, calls } = scriptedFetch([new Response('', { status: 429, headers: { 'retry-after': String(MAX_WAIT_MS / 1000 + 1) } })]);
    await assert.rejects(download(FILE, { deadline: 600_000, fetchImpl, ...clock() }), /rate-limited/);
    assert.equal(calls.length, 1);
  });

  it('stays inside the overall time limit', async () => {
    const { fetchImpl, calls } = scriptedFetch([new Response('', { status: 503, headers: { 'retry-after': '60' } })]);
    await assert.rejects(download(FILE, { deadline: 30_000, fetchImpl, ...clock() }), /no good answer/);
    assert.equal(calls.length, 1);
  });

  it('retries a network error', async () => {
    const { fetchImpl } = scriptedFetch([new TypeError('fetch failed'), new Response('hello', { status: 200 })]);
    assert.equal((await download(FILE, { deadline: 600_000, fetchImpl, ...clock() })).toString(), 'hello');
  });

  it('refuses bytes that are not the pinned ones, and does not retry them', async () => {
    for (const [body, pattern] of [
      ['hellO', /sha256 .* pinned/],
      ['hello world', /more than the 5 bytes pinned/],
      ['hell', /4 bytes, pinned 5/],
    ]) {
      const { fetchImpl, calls } = scriptedFetch([new Response(body, { status: 200 })]);
      await assert.rejects(download(FILE, { deadline: 600_000, fetchImpl, ...clock() }), pattern);
      assert.equal(calls.length, 1);
    }
  });
});

// --- the caption transform ------------------------------------------------------------

const VTT = 'WEBVTT\n\n1\n00:00:03.000 --> 00:00:08.033\nOne line\nand another.\n\n2\n00:01:08.267 --> 00:01:13.633\nTwo.\n';

describe('toSiteVtt', () => {
  it('appends line:-5 to every timing line and changes nothing else', () => {
    assert.equal(
      toSiteVtt(VTT),
      'WEBVTT\n\n1\n00:00:03.000 --> 00:00:08.033 line:-5\nOne line\nand another.\n\n2\n00:01:08.267 --> 00:01:13.633 line:-5\nTwo.\n',
    );
  });

  it('refuses a timing line that already carries a setting', () => {
    assert.throws(() => toSiteVtt(VTT.replace('00:00:08.033', '00:00:08.033 align:start')), /already carries a cue setting/);
  });

  it('refuses a CR', () => {
    assert.throws(() => toSiteVtt(VTT.replace(/\n/g, '\r\n')), /CR character/);
  });

  it('refuses an arrow that is not a timing line, and a file with no cue', () => {
    assert.throws(() => toSiteVtt('WEBVTT\n\n1\nnot --> a time\nx\n'), /not a cue timing line/);
    assert.throws(() => toSiteVtt('WEBVTT\n'), /no cue timing line/);
  });
});

// --- the manifest ---------------------------------------------------------------------

const committed = () => JSON.parse(readFileSync(MANIFEST, 'utf8'));

describe('parseManifest', () => {
  it('accepts the committed manifest', () => {
    assert.ok(parseManifest(readFileSync(MANIFEST, 'utf8')).length >= 1);
  });

  const refusals = [
    ['an unknown key', (m) => { m.videos[0].transcript = {}; }, /exactly the keys/],
    ['a missing key', (m) => { delete m.videos[0].poster; }, /exactly the keys/],
    ['a bad slug', (m) => { m.videos[0].slug = 'Getting Started'; }, /slug/],
    ['a duplicate slug', (m) => { m.videos.push(structuredClone(m.videos[0])); }, /listed twice/],
    ['a sha256 that is not lowercase hex', (m) => { m.videos[0].mp4.sha256 = m.videos[0].mp4.sha256.toUpperCase(); }, /64 lowercase hex/],
    ['an mp4 over its cap', (m) => { m.videos[0].mp4.bytes = 64 * 1024 * 1024 + 1; }, /bytes must be/],
    ['an old archive name', (m) => { m.videos[0].mp4.name = '01-getting-started.mp4'; }, /must be 01-getting-started-/],
    ['a path in a name', (m) => { m.videos[0].poster.name = '../index.html'; }, /must be/],
    ['a poster named by the mp4 hash alone', (m) => { m.videos[0].poster.name = m.videos[0].mp4.name.replace('.mp4', '.jpg'); }, /-poster-/],
    ['an empty list', (m) => { m.videos = []; }, /1 to 12/],
    ['a list of 13', (m) => { m.videos = Array.from({ length: 13 }, () => m.videos[0]); }, /1 to 12/],
    ['a fractional duration', (m) => { m.videos[0].duration_seconds = 83.967; }, /whole number of seconds/],
  ];
  for (const [what, plant, pattern] of refusals) {
    it(`refuses ${what}`, () => {
      const m = committed();
      plant(m);
      assert.throws(() => parseManifest(JSON.stringify(m)), (e) => e instanceof Refusal && pattern.test(e.message));
    });
  }
});

describe('the two-way reference check', () => {
  it('finds every /media/ reference, and a mismatch either way', () => {
    const html = '<video src="/media/a.mp4" poster="/media/a.jpg"><track src="/media/a.site.vtt"/></video>';
    assert.deepEqual([...mediaReferences(html)].sort(), ['a.jpg', 'a.mp4', 'a.site.vtt']);
    assert.deepEqual(referenceProblems(mediaReferences(html), new Set(['a.mp4', 'a.jpg', 'a.site.vtt'])), []);
    assert.equal(referenceProblems(mediaReferences(html), new Set(['a.mp4', 'a.jpg'])).length, 1);
    assert.equal(referenceProblems(mediaReferences(html), new Set(['a.mp4', 'a.jpg', 'a.site.vtt', 'b.mp4'])).length, 1);
  });
});

// --- main, end to end against a fake release -------------------------------------------

/** A manifest of one made-up video whose files a fake release serves. */
function fixture() {
  const mp4 = Buffer.from('not really an mp4');
  const vtt = Buffer.from(VTT);
  const jpg = Buffer.from('not really a jpeg');
  const slug = '01-getting-started';
  const stem = `${slug}-${sha(mp4).slice(0, 12)}`;
  const entry = {
    slug,
    title: 'Get started',
    description: 'A video.',
    duration_seconds: 1,
    mp4: { name: `${stem}.mp4`, bytes: mp4.length, sha256: sha(mp4) },
    vtt: { name: `${stem}.vtt`, bytes: vtt.length, sha256: sha(vtt) },
    poster: { name: `${stem}-poster-${sha(jpg).slice(0, 12)}.jpg`, bytes: jpg.length, sha256: sha(jpg) },
  };
  const served = new Map([[entry.mp4.name, mp4], [entry.vtt.name, vtt], [entry.poster.name, jpg]]);
  const dir = scratch();
  const outDir = join(dir, 'out');
  mkdirSync(outDir);
  const page = `<html><video src="/media/${entry.mp4.name}" poster="/media/${entry.poster.name}"><track src="/media/${stem}.site.vtt"/></video></html>`;
  writeFileSync(join(outDir, 'index.html'), page);
  const manifestPath = join(dir, 'videos.json');
  writeFileSync(manifestPath, JSON.stringify({ videos: [entry] }));
  const fetchImpl = async (url) => {
    const bytes = served.get(decodeURIComponent(url.slice(RELEASE_URL.length)));
    return bytes ? new Response(bytes, { status: 200 }) : new Response('Not Found', { status: 404 });
  };
  const lines = [];
  const run = (overrides = {}) =>
    main({ outDir, manifestPath, mode: 'marketing', fetchImpl, sleep: async () => {}, log: (l) => lines.push(l), error: (l) => lines.push(l), ...overrides });
  return { dir, outDir, entry, stem, served, manifestPath, run, lines };
}

describe('main', () => {
  it('places the mp4, the poster and the positioned captions, and nothing else', async () => {
    const f = fixture();
    assert.equal(await f.run(), 0);
    assert.deepEqual(readdirSync(join(f.outDir, 'media')).sort(), [f.entry.poster.name, f.entry.mp4.name, `${f.stem}.site.vtt`].sort());
    assert.equal(readFileSync(join(f.outDir, 'media', `${f.stem}.site.vtt`), 'utf8'), toSiteVtt(VTT));
    assert.deepEqual(readdirSync(f.outDir).sort(), ['index.html', 'media']);
    rmSync(f.dir, { recursive: true });
  });

  it('refuses outside the marketing build', async () => {
    const f = fixture();
    assert.equal(await f.run({ mode: undefined }), 1);
    assert.match(f.lines.join('\n'), /not "marketing"/);
    assert.equal(existsSync(join(f.outDir, 'media')), false);
    rmSync(f.dir, { recursive: true });
  });

  it('refuses an existing out/media, and leaves it alone', async () => {
    const f = fixture();
    mkdirSync(join(f.outDir, 'media'));
    writeFileSync(join(f.outDir, 'media', 'keep'), 'x');
    assert.equal(await f.run(), 1);
    assert.equal(readFileSync(join(f.outDir, 'media', 'keep'), 'utf8'), 'x');
    rmSync(f.dir, { recursive: true });
  });

  it('refuses a missing asset and leaves no out/media and no staging directory', async () => {
    const f = fixture();
    f.served.delete(f.entry.poster.name);
    assert.equal(await f.run(), 1);
    assert.match(f.lines.join('\n'), /HTTP 404/);
    assert.deepEqual(readdirSync(f.outDir), ['index.html']);
    rmSync(f.dir, { recursive: true });
  });

  it('refuses a wrong pin and leaves no out/media', async () => {
    const f = fixture();
    const m = JSON.parse(readFileSync(f.manifestPath, 'utf8'));
    m.videos[0].vtt.sha256 = '0'.repeat(64);
    writeFileSync(f.manifestPath, JSON.stringify(m));
    assert.equal(await f.run(), 1);
    assert.match(f.lines.join('\n'), /sha256 .* pinned 0{64}/);
    assert.deepEqual(readdirSync(f.outDir), ['index.html']);
    rmSync(f.dir, { recursive: true });
  });

  it('refuses a page that references the approved captions instead of the positioned ones', async () => {
    const f = fixture();
    const page = readFileSync(join(f.outDir, 'index.html'), 'utf8').replace('.site.vtt', '.vtt');
    writeFileSync(join(f.outDir, 'index.html'), page);
    assert.equal(await f.run(), 1);
    assert.match(f.lines.join('\n'), /references \/media\/.*\.vtt, which is not placed/);
    assert.deepEqual(readdirSync(f.outDir), ['index.html']);
    rmSync(f.dir, { recursive: true });
  });
});

// --- the CI content checks (check-site-media.mjs) ---------------------------------------

const box = (type, ...parts) => {
  const payload = Buffer.concat(parts);
  const head = Buffer.alloc(8);
  head.writeUInt32BE(payload.length + 8);
  head.write(type, 4, 'latin1');
  return Buffer.concat([head, payload]);
};
const u32 = (...values) => Buffer.concat(values.map((v) => { const b = Buffer.alloc(4); b.writeUInt32BE(v); return b; }));

/** A minimal ISO BMFF file: a video track, optionally an AAC track, `seconds` long. */
function mp4({ seconds = 84, audio = 'mp4a', faststart = true, audioSeconds = seconds } = {}) {
  const track = (handler, entry, length) =>
    box('trak', box('mdia',
      box('mdhd', u32(0, 0, 0, 1000, Math.round(length * 1000), 0)),
      box('hdlr', u32(0, 0), Buffer.from(handler, 'latin1'), Buffer.alloc(12), Buffer.from('x\0')),
      box('minf', box('stbl', box('stsd', u32(0, 1), box(entry, Buffer.alloc(8)))))));
  const moov = box('moov', box('mvhd', u32(0, 0, 0, 1000, Math.round(seconds * 1000)), Buffer.alloc(80)),
    track('vide', 'avc1', seconds), ...(audio ? [track('soun', audio, audioSeconds)] : []));
  const ftyp = box('ftyp', Buffer.from('isom'), u32(512));
  const mdat = box('mdat', Buffer.alloc(16));
  return faststart ? Buffer.concat([ftyp, moov, mdat]) : Buffer.concat([ftyp, mdat, moov]);
}

/** A minimal JPEG: SOI, an APP0, an SOF0 of the given size, EOI. */
function jpeg(width = 1280, height = 720) {
  const sof = Buffer.from([0xff, 0xc0, 0x00, 0x11, 0x08, height >> 8, height & 255, width >> 8, width & 255, 3, 1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1]);
  return Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x04, 0x00, 0x00]), sof, Buffer.from([0xff, 0xd9])]);
}

/** An entry with real-shaped files in `out/media` and a transcript, ready to plant defects in. */
function contentFixture({ mp4Bytes = mp4(), jpgBytes = jpeg(), vtt = VTT, said = ['One line and another.', 'Two.'] } = {}) {
  const dir = scratch();
  const media = join(dir, 'media');
  const transcripts = join(dir, 'walkthroughs');
  mkdirSync(media);
  mkdirSync(transcripts);
  const slug = '01-getting-started';
  const stem = `${slug}-${sha(mp4Bytes).slice(0, 12)}`;
  const entry = {
    slug,
    title: 'Get started',
    description: 'A video.',
    duration_seconds: 84,
    mp4: { name: `${stem}.mp4`, bytes: mp4Bytes.length, sha256: sha(mp4Bytes) },
    vtt: { name: `${stem}.vtt`, bytes: Buffer.byteLength(vtt), sha256: sha(Buffer.from(vtt)) },
    poster: { name: `${stem}-poster-${sha(jpgBytes).slice(0, 12)}.jpg`, bytes: jpgBytes.length, sha256: sha(jpgBytes) },
  };
  writeFileSync(join(media, entry.mp4.name), mp4Bytes);
  writeFileSync(join(media, entry.poster.name), jpgBytes);
  writeFileSync(join(media, `${stem}.site.vtt`), toSiteVtt(vtt));
  writeFileSync(join(transcripts, `${slug}.json`), JSON.stringify({ slug, mp4: entry.mp4.name, items: [{ on_screen: ['x'] }, { said }] }));
  return { dir, media, transcripts, entry, stem, problems: () => entryProblems(entry, media, transcripts) };
}

describe('entryProblems', () => {
  it('passes a faststart, narrated video with matching captions, transcript and poster', () => {
    const f = contentFixture();
    assert.deepEqual(f.problems(), []);
    rmSync(f.dir, { recursive: true });
  });

  const plants = [
    ['a video that is not faststart', { mp4Bytes: mp4({ faststart: false }) }, /not faststart/],
    ['a silent video', { mp4Bytes: mp4({ audio: null }) }, /0 audio track/],
    ['audio that is not AAC', { mp4Bytes: mp4({ audio: 'Opus' }) }, /in AAC/],
    ['narration that stops early', { mp4Bytes: mp4({ audioSeconds: 60 }) }, /narration lasts 60/],
    ['a length the manifest misstates', { mp4Bytes: mp4({ seconds: 85.6 }) }, /duration_seconds 84, but the video is 85.600 s \(86/],
    ['a poster of the wrong size', { jpgBytes: jpeg(1920, 1080) }, /1920x1080, want 1280x720/],
    ['a poster that is not a JPEG', { jpgBytes: Buffer.from('GIF89a') }, /not a whole JPEG/],
    ['a transcript missing a spoken line', { said: ['One line and another.'] }, /spoken lines are not the captions/],
    ['a transcript with a word changed', { said: ['One line and other.', 'Two.'] }, /first difference at 1/],
  ];
  for (const [what, options, pattern] of plants) {
    it(`fails ${what}`, () => {
      const f = contentFixture(options);
      const problems = f.problems();
      assert.ok(problems.some((p) => pattern.test(p)), problems.join('\n') || 'no problem reported');
      rmSync(f.dir, { recursive: true });
    });
  }

  it('fails captions changed beyond the setting, and a timing line without it', () => {
    const f = contentFixture();
    const site = join(f.media, `${f.stem}.site.vtt`);
    writeFileSync(site, readFileSync(site, 'utf8').replace('Two.', 'Two!'));
    assert.ok(f.problems().some((p) => /is not the pinned/.test(p)));
    writeFileSync(site, VTT);
    assert.ok(f.problems().some((p) => /without line:-5/.test(p)));
    rmSync(f.dir, { recursive: true });
  });

  it('fails a transcript of another render, and a file holding the former name', () => {
    const f = contentFixture();
    const path = join(f.transcripts, `${f.entry.slug}.json`);
    writeFileSync(path, readFileSync(path, 'utf8').replace(f.entry.mp4.name, '01-getting-started-b0575bc55609.mp4'));
    assert.ok(f.problems().some((p) => /the transcript is of/.test(p)));
    const g = contentFixture({ jpgBytes: Buffer.concat([jpeg().subarray(0, -2), Buffer.from(['Experi', 'mently'].join('')), Buffer.from([0xff, 0xd9])]) });
    assert.ok(g.problems().some((p) => /former name/.test(p)));
    rmSync(f.dir, { recursive: true });
    rmSync(g.dir, { recursive: true });
  });

  it('reads the facts it checks', () => {
    assert.deepEqual(jpegSize(jpeg()), { width: 1280, height: 720 });
    const facts = mp4Facts(mp4());
    assert.equal(facts.faststart, true);
    assert.equal(facts.duration, 84);
    assert.deepEqual(facts.tracks.map((t) => [t.handler, t.sampleEntry]), [['vide', 'avc1'], ['soun', 'mp4a']]);
    assert.deepEqual(parseVtt(VTT).cues.map((c) => c.text), ['One line and another.', 'Two.']);
    assert.deepEqual(parseVtt('WEBVTT\n\n00:00:02.000 --> 00:00:01.000\nx\n').problems, ['cue 1 ends before it starts']);
  });
});

describe('pageProblems', () => {
  const entry = {
    slug: '01-getting-started',
    mp4: { name: '01-getting-started-aaaaaaaaaaaa.mp4', sha256: 'a'.repeat(64) },
    poster: { name: '01-getting-started-aaaaaaaaaaaa-poster-bbbbbbbbbbbb.jpg' },
  };
  // As React exports it: `controls=""`, camelCase `srcLang`, a self-closing track.
  const good =
    '<video controls="" preload="none" poster="/media/01-getting-started-aaaaaaaaaaaa-poster-bbbbbbbbbbbb.jpg" width="1920" height="1080" ' +
    'aria-labelledby="t" aria-describedby="d" src="/media/01-getting-started-aaaaaaaaaaaa.mp4">' +
    '<track kind="captions" srcLang="en" label="English" src="/media/01-getting-started-aaaaaaaaaaaa.site.vtt"/></video>';

  it('passes the section as React exports it', () => {
    assert.deepEqual(pageProblems(good, [entry]), []);
    assert.equal(attributes(' srcLang="en" default=""').get('srclang'), 'en');
  });

  const plants = [
    ['a default track', good.replace('label="English"', 'label="English" default=""'), /default is set/],
    ['autoplay', good.replace('controls=""', 'controls="" autoplay=""'), /autoplay is set/],
    ['no controls', good.replace('controls="" ', ''), /no controls/],
    ['preload metadata', good.replace('preload="none"', 'preload="metadata"'), /preload=/],
    ['the approved captions', good.replace('.site.vtt', '.vtt'), /<track>: src=/],
    ['subtitles', good.replace('kind="captions"', 'kind="subtitles"'), /kind=/],
    ['a second video', good + good, /2 <video>, the manifest lists 1/],
    ['an iframe', `${good}<iframe src="/x"></iframe>`, /<iframe>/],
    ['a third-party source', good.replace('src="/media/01-getting-started-aaaaaaaaaaaa.mp4"', 'src="https://github.com/x.mp4"'), /not a path on this site/],
    ['a protocol-relative poster', good.replace('poster="/media/', 'poster="//cdn.example/media/'), /not a path on this site/],
  ];
  for (const [what, html, pattern] of plants) {
    it(`fails ${what}`, () => {
      const problems = pageProblems(html, [entry]);
      assert.ok(problems.some((p) => pattern.test(p)), problems.join('\n') || 'no problem reported');
    });
  }
});

// --- G2b (check-platform-export.mjs) ------------------------------------------------------

describe('platformProblems', () => {
  const platform = () => {
    const dir = scratch();
    writeFileSync(join(dir, 'index.html'), '<a href="/login">Sign in to the dashboard</a>');
    mkdirSync(join(dir, '_next'));
    return dir;
  };

  it('passes a platform export', () => {
    const dir = platform();
    assert.deepEqual(platformProblems(dir), []);
    rmSync(dir, { recursive: true });
  });

  it('fails a video, a media directory, a nested media file, and a page that is not the platform homepage', () => {
    const plants = [
      [(d) => writeFileSync(join(d, 'index.html'), '<a>Sign in to the dashboard</a><video src="/media/a.mp4"></video>'), /1 <video>/],
      [(d) => mkdirSync(join(d, 'media')), /out\/media exists/],
      [(d) => writeFileSync(join(d, '_next', 'a.vtt'), 'WEBVTT'), /carries _next\/a\.vtt/],
      [(d) => writeFileSync(join(d, 'index.html'), '<p>Walkthroughs</p>'), /not the platform homepage/],
    ];
    for (const [plant, pattern] of plants) {
      const dir = platform();
      plant(dir);
      assert.ok(platformProblems(dir).some((p) => pattern.test(p)), platformProblems(dir).join('\n'));
      rmSync(dir, { recursive: true });
    }
    assert.match(platformProblems(join(tmpdir(), 'no-such-export-dir'))[0], /is missing/);
  });
});
