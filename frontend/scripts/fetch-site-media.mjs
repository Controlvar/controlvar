#!/usr/bin/env node
/**
 * Put the walkthrough videos into a marketing build's export (#1290).
 *
 *   node scripts/fetch-site-media.mjs [outDir]     (default: out)
 *
 * The last command of `npm run build:marketing`, so it runs wherever the
 * public site is built: AWS Amplify on every push to main, the `Frontend
 * Tests` CI job, a developer's machine. It does exactly four things:
 *
 *   1. downloads every file `src/content/videos.json` names from the public
 *      `media` release, refusing any whose size or sha256 differs from its
 *      pin -- checked before a byte of it is parsed;
 *   2. places the mp4 and the poster in `out/media/` under their own names;
 *   3. derives `<stem>.site.vtt` from the approved `<stem>.vtt` by appending
 *      ` line:-5` to every cue timing line and changing nothing else, and
 *      places only that: the setting lifts the captions above the video's
 *      burned-in headline band, where an unpositioned cue covers it;
 *   4. checks both ways that `out/index.html` references, as `/media/<name>`,
 *      exactly the files it placed.
 *
 * Nothing else runs here. What the files contain (faststart, the audio
 * stream, the captions against the transcript, the poster's size) is checked
 * over the same pinned bytes by `scripts/check-site-media.mjs` in CI, because
 * a check that can only fail in Amplify blocks a deploy with nobody watching.
 *
 * It REFUSES (exit 1, and no `out/media`) unless NEXT_PUBLIC_SITE_MODE is
 * `marketing` -- the dashboard a self-hoster builds must never carry the
 * media -- and when `out/index.html` is missing or `out/media` already exists.
 *
 * A PINNED ASSET IS NEVER REPLACED OR DELETED. `gh release upload --clobber`,
 * or deleting a pinned asset, fails this download and so blocks every later
 * controlvar.com deploy until the manifest changes, and an Amplify build that
 * fails is reported to no pull request. A caption correction is a new render
 * (a new mp4, so a new file name); a new poster has a new name by
 * construction (its own hash).
 *
 * Node built-ins only, and nothing newer than Node 20.9.0 (Next 16.4's floor,
 * so the oldest Node Amplify can build with): no `import.meta.dirname`.
 */
import { createHash } from 'node:crypto';
import { chmodSync, existsSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * A plain release download, not an API call, so no token is needed. Whether
 * GitHub rate-limits these from a build host is not known; hence the retry
 * rule in `classifyResponse`.
 */
export const RELEASE_URL = 'https://github.com/Controlvar/controlvar/releases/download/media/';
export const MANIFEST = fileURLToPath(new URL('../src/content/videos.json', import.meta.url));

export const ATTEMPT_MS = 60_000;
export const TOTAL_MS = 10 * 60_000;
export const MAX_ATTEMPTS = 5;
/** The longest single wait before a retry, a server's Retry-After included. */
export const MAX_WAIT_MS = 120_000;
export const MAX_ENTRIES = 12;
/** Largest pinned size, by manifest key. */
export const MAX_BYTES = { mp4: 64 * 1024 * 1024, vtt: 256 * 1024, poster: 64 * 1024 };
/** What the site's copy of the captions adds to every cue timing line. */
export const SITE_SETTING = ' line:-5';

const ENTRY_KEYS = ['slug', 'title', 'description', 'duration_seconds', 'mp4', 'vtt', 'poster'];
const FILE_KEYS = ['name', 'bytes', 'sha256'];
const SLUG = /^[0-9]{2}-[a-z0-9]+(-[a-z0-9]+)*$/;
const SHA256 = /^[0-9a-f]{64}$/;
const TIMESTAMP = String.raw`(?:\d{2,}:)?[0-5]\d:[0-5]\d\.\d{3}`;
const TIMING_PREFIX = new RegExp(`^${TIMESTAMP} --> ${TIMESTAMP}`);
const TIMING = new RegExp(`^${TIMESTAMP} --> ${TIMESTAMP}$`);

/** A reason the build must stop: printed as is, and never retried. */
export class Refusal extends Error {}

const sameKeys = (value, keys) =>
  value !== null && typeof value === 'object' && !Array.isArray(value) &&
  Object.keys(value).length === keys.length && keys.every((k) => Object.prototype.hasOwnProperty.call(value, k));

const oneLine = (value, max) => typeof value === 'string' && value.length >= 1 && value.length <= max && !/[\r\n]/.test(value);

/** `<slug>-<first 12 hex of the mp4's sha256>`: the render tool's own naming. */
export const stemOf = (entry) => `${entry.slug}-${entry.mp4.sha256.slice(0, 12)}`;

/** The files one entry puts in `out/media/`. The approved `.vtt` is not one. */
export const placedNames = (entry) => [entry.mp4.name, `${stemOf(entry)}.site.vtt`, entry.poster.name];

/**
 * The manifest's entries, or a Refusal naming what is wrong. Every file name
 * is derived from the slug and a pinned hash, so none can carry `/` or `..`.
 */
export function parseManifest(text) {
  let data;
  try {
    data = JSON.parse(text);
  } catch (error) {
    throw new Refusal(`videos.json is not JSON: ${error.message}`);
  }
  if (!sameKeys(data, ['videos'])) throw new Refusal('videos.json must be {"videos": [...]} and nothing else');
  const { videos } = data;
  if (!Array.isArray(videos) || videos.length < 1 || videos.length > MAX_ENTRIES) {
    throw new Refusal(`videos.json must list 1 to ${MAX_ENTRIES} videos`);
  }
  const slugs = new Set();
  videos.forEach((entry, i) => {
    const where = `videos[${i}]`;
    if (!sameKeys(entry, ENTRY_KEYS)) throw new Refusal(`${where} must have exactly the keys ${ENTRY_KEYS.join(', ')}`);
    if (typeof entry.slug !== 'string' || !SLUG.test(entry.slug)) throw new Refusal(`${where}.slug ${JSON.stringify(entry.slug)} is not like 01-getting-started`);
    if (slugs.has(entry.slug)) throw new Refusal(`${where}.slug ${entry.slug} is listed twice`);
    slugs.add(entry.slug);
    if (!oneLine(entry.title, 80)) throw new Refusal(`${where}.title must be one line of 1 to 80 characters`);
    if (!oneLine(entry.description, 200)) throw new Refusal(`${where}.description must be one line of 1 to 200 characters`);
    if (!Number.isInteger(entry.duration_seconds) || entry.duration_seconds < 1) throw new Refusal(`${where}.duration_seconds must be a whole number of seconds`);
    for (const key of ['mp4', 'vtt', 'poster']) {
      const file = entry[key];
      if (!sameKeys(file, FILE_KEYS)) throw new Refusal(`${where}.${key} must have exactly the keys ${FILE_KEYS.join(', ')}`);
      if (typeof file.sha256 !== 'string' || !SHA256.test(file.sha256)) throw new Refusal(`${where}.${key}.sha256 must be 64 lowercase hex characters`);
      if (!Number.isInteger(file.bytes) || file.bytes < 1 || file.bytes > MAX_BYTES[key]) {
        throw new Refusal(`${where}.${key}.bytes must be a whole number from 1 to ${MAX_BYTES[key]}`);
      }
    }
    const stem = stemOf(entry);
    const want = { mp4: `${stem}.mp4`, vtt: `${stem}.vtt`, poster: `${stem}-poster-${entry.poster.sha256.slice(0, 12)}.jpg` };
    for (const key of ['mp4', 'vtt', 'poster']) {
      if (entry[key].name !== want[key]) {
        throw new Refusal(`${where}.${key}.name ${JSON.stringify(entry[key].name)} must be ${want[key]} (named from the slug and the pinned hashes)`);
      }
    }
  });
  return videos;
}

/** Milliseconds a Retry-After header asks for (seconds, or an HTTP date), or null. */
export function retryAfterMs(value, now = Date.now()) {
  if (value === null || value === undefined) return null;
  const text = String(value).trim();
  if (/^\d+$/.test(text)) return Number(text) * 1000;
  const when = Date.parse(text);
  return Number.isNaN(when) ? null : Math.max(0, when - now);
}

/**
 * What one answer from the release means, so the rule is a unit (PE 5).
 *
 *   2xx                                    ok
 *   429, or 403 with Retry-After or
 *   x-ratelimit-remaining: 0               retry: rate limited
 *   5xx                                    retry
 *   anything else (404 above all)          refuse at once: the asset is not
 *                                          there, and asking again will not
 *                                          put it there
 *
 * `headers` is anything with `get(name)`: a fetch Headers object.
 */
export function classifyResponse(status, headers, now = Date.now()) {
  if (status >= 200 && status < 300) return { action: 'ok', rateLimited: false, waitMs: null };
  const wait = retryAfterMs(headers.get('retry-after'), now);
  if (status === 429) return { action: 'retry', rateLimited: true, waitMs: wait };
  if (status === 403 && (wait !== null || headers.get('x-ratelimit-remaining') === '0')) {
    return { action: 'retry', rateLimited: true, waitMs: wait };
  }
  if (status >= 500 && status < 600) return { action: 'retry', rateLimited: false, waitMs: wait };
  return { action: 'refuse', rateLimited: false, waitMs: null };
}

/** The wait before attempt `attempt + 1` when the server named none: 1, 2, 4, 8 s. */
export const backoffMs = (attempt) => Math.min(MAX_WAIT_MS, 1000 * 2 ** (attempt - 1));

/**
 * The site's captions: `text` with ` line:-5` appended to every cue timing
 * line and nothing else changed. Refuses a CR anywhere, and a timing line
 * that already carries a setting (or anything after its end time), so a
 * stripped copy is always the approved file again.
 */
export function toSiteVtt(text) {
  if (text.includes('\r')) throw new Refusal('the captions contain a CR character; the approved files are LF-only');
  let timings = 0;
  const lines = text.split('\n').map((line, i) => {
    if (!line.includes('-->')) return line;
    if (!TIMING.test(line)) {
      throw new Refusal(
        TIMING_PREFIX.test(line)
          ? `caption line ${i + 1} already carries a cue setting: ${JSON.stringify(line)}`
          : `caption line ${i + 1} has "-->" but is not a cue timing line: ${JSON.stringify(line)}`,
      );
    }
    timings += 1;
    return `${line}${SITE_SETTING}`;
  });
  if (timings === 0) throw new Refusal('the captions have no cue timing line');
  return lines.join('\n');
}

/** Every name the page references as `/media/<name>`. */
export function mediaReferences(html) {
  const names = new Set();
  for (const match of html.matchAll(/\/media\/([^"'\s<>)?#]*)/g)) names.add(match[1]);
  return names;
}

/** Problems with "the page references exactly the files placed", both ways. */
export function referenceProblems(referenced, placed) {
  const problems = [];
  for (const name of placed) if (!referenced.has(name)) problems.push(`${name} is placed but out/index.html never references /media/${name}`);
  for (const name of referenced) if (!placed.has(name)) problems.push(`out/index.html references /media/${name}, which is not placed`);
  return problems;
}

const defaultSleep = (ms) => new Promise((done) => setTimeout(done, ms));

async function receive(response, file) {
  const hash = createHash('sha256');
  const chunks = [];
  let received = 0;
  for await (const chunk of response.body) {
    received += chunk.length;
    if (received > file.bytes) throw new Refusal(`${file.name}: more than the ${file.bytes} bytes pinned; download stopped`);
    hash.update(chunk);
    chunks.push(chunk);
  }
  if (received !== file.bytes) throw new Refusal(`${file.name}: ${received} bytes, pinned ${file.bytes}`);
  const sha256 = hash.digest('hex');
  if (sha256 !== file.sha256) {
    throw new Refusal(
      `${file.name}: sha256 ${sha256}, pinned ${file.sha256}. A pinned asset in the media release is never ` +
        'replaced; either the manifest is wrong or the release was changed',
    );
  }
  return Buffer.concat(chunks);
}

/** One file from the release, pinned, with the retry rule above. */
export async function download(file, { deadline, fetchImpl = fetch, sleep = defaultSleep, now = Date.now, log = () => {} }) {
  let rateLimited = false;
  let lastReason = 'the time limit ran out before an attempt';
  for (let attempt = 1; ; attempt += 1) {
    const left = deadline - now();
    if (left <= 0) break;
    let verdict;
    try {
      const response = await fetchImpl(RELEASE_URL + encodeURIComponent(file.name), {
        redirect: 'follow',
        signal: AbortSignal.timeout(Math.min(ATTEMPT_MS, left)),
      });
      verdict = classifyResponse(response.status, response.headers, now());
      if (verdict.action === 'ok') return await receive(response, file);
      if (response.body) await response.body.cancel().catch(() => {});
      if (verdict.action === 'refuse') {
        throw new Refusal(
          `${file.name}: HTTP ${response.status} from the media release. Upload the approved file under ` +
            'exactly this name before listing it, or remove the entry',
        );
      }
      lastReason = `HTTP ${response.status}`;
    } catch (error) {
      if (error instanceof Refusal) throw error;
      verdict = { action: 'retry', rateLimited: false, waitMs: null };
      lastReason = `${error.name}: ${error.message}`;
    }
    rateLimited = rateLimited || verdict.rateLimited;
    if (attempt >= MAX_ATTEMPTS) break;
    const wait = verdict.waitMs ?? backoffMs(attempt);
    if (wait > MAX_WAIT_MS || now() + wait >= deadline) break;
    log(`fetch-site-media: ${file.name}: ${lastReason}; retrying in ${Math.ceil(wait / 1000)} s`);
    await sleep(wait);
  }
  throw new Refusal(
    rateLimited
      ? `${file.name}: GitHub rate-limited the download (${lastReason}) and the retries ran out inside the ` +
          `${TOTAL_MS / 60_000}-minute limit. Run the build again later`
      : `${file.name}: no good answer from the media release (${lastReason}) inside the retry limit`,
  );
}

/** Does the whole job; returns the exit status. Only the CLI below calls it with no arguments. */
export async function main({
  outDir = resolve('out'),
  manifestPath = MANIFEST,
  mode = process.env.NEXT_PUBLIC_SITE_MODE,
  fetchImpl = fetch,
  sleep = defaultSleep,
  now = Date.now,
  log = (line) => console.log(line),
  error = (line) => console.error(line),
} = {}) {
  const mediaDir = join(outDir, 'media');
  // What this run made, and so all a failure removes: a refusal never touches
  // an out/media it did not create.
  let staging = null;
  let placedDir = false;
  try {
    if (mode !== 'marketing') {
      throw new Refusal(
        `NEXT_PUBLIC_SITE_MODE is ${JSON.stringify(mode ?? null)}, not "marketing". The dashboard a ` +
          'self-hoster builds must never carry the site media',
      );
    }
    const indexPath = join(outDir, 'index.html');
    if (!existsSync(indexPath)) throw new Refusal(`${indexPath} does not exist; run next build first`);
    if (existsSync(mediaDir)) throw new Refusal(`${mediaDir} already exists; run next build first (it empties out/)`);

    const videos = parseManifest(readFileSync(manifestPath, 'utf8'));
    const placed = new Set(videos.flatMap(placedNames));
    const before = referenceProblems(mediaReferences(readFileSync(indexPath, 'utf8')), placed);
    if (before.length) throw new Refusal(before.join('\n  '));

    // Each file is written as soon as it matches its pin, so at most one is
    // ever held in memory; the directory becomes out/media only when all have.
    staging = mkdtempSync(join(outDir, '.media-'));
    const deadline = now() + TOTAL_MS;
    let total = 0;
    const place = (name, bytes, note) => {
      writeFileSync(join(staging, name), bytes);
      total += bytes.length;
      log(`fetch-site-media: ${name} ${bytes.length} bytes ${note}`);
    };
    for (const entry of videos) {
      for (const key of ['mp4', 'poster']) {
        const file = entry[key];
        place(file.name, await download(file, { deadline, fetchImpl, sleep, now, log }), `sha256 ${file.sha256.slice(0, 12)} (pinned)`);
      }
      const vtt = await download(entry.vtt, { deadline, fetchImpl, sleep, now, log });
      log(`fetch-site-media: ${entry.vtt.name} ${vtt.length} bytes sha256 ${entry.vtt.sha256.slice(0, 12)} (pinned; not placed)`);
      place(`${stemOf(entry)}.site.vtt`, Buffer.from(toSiteVtt(vtt.toString('utf8')), 'utf8'), `(derived: ${entry.vtt.name} with${SITE_SETTING} on every cue)`);
    }
    renameSync(staging, mediaDir);
    staging = null;
    placedDir = true;
    chmodSync(mediaDir, 0o755); // mkdtemp makes it 0700; the site is served from it

    const after = referenceProblems(mediaReferences(readFileSync(indexPath, 'utf8')), new Set(readdirSync(mediaDir)));
    if (after.length) throw new Refusal(after.join('\n  '));
    log(`fetch-site-media: placed ${placed.size} files, ${total} bytes, in ${mediaDir}`);
    return 0;
  } catch (failure) {
    if (staging) rmSync(staging, { recursive: true, force: true });
    if (placedDir) rmSync(mediaDir, { recursive: true, force: true });
    if (!(failure instanceof Refusal)) throw failure;
    error(`fetch-site-media: refusing.\n  ${failure.message}`);
    return 1;
  }
}

function invokedAsScript() {
  // Real paths on both sides: a symlinked checkout path must not turn the
  // build step into a silent no-op.
  if (!process.argv[1]) return false;
  try {
    return realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
}

if (invokedAsScript()) {
  process.exitCode = await main({ outDir: resolve(process.argv[2] ?? 'out') });
}
