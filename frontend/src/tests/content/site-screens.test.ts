/**
 * `content/site-screens.json` against `public/screens/` (#1459 SPEC B; PE 1
 * and 12; C8).
 *
 * The homepage's dashboard screenshots are committed files, named
 * `<id>-<sha256[:12]>.webp`, and the registry is the one place that lists
 * them with their bytes, sha256, size, alt text and caption. This test holds
 * the two together:
 *
 *   - the registry names exactly the four planned screenshots (S1-S4 of the
 *     UX plan), was captured at a release tag, and gives each a file name
 *     made from its own sha256, and an alt text with no figure in it (a
 *     number would be wrong after a retake);
 *   - the folder exists and holds exactly the registered files: each is the
 *     registered bytes, a RIFF/WEBP still of the registered width and height,
 *     with no EXIF or XMP (leak_guard skips images, so nothing else reads
 *     what a screenshot carries), at most 256 KiB, and at most 1 MiB in all.
 *
 * A missing folder or file is a FAILURE, never a skip. `public/site/` was
 * gitignored (the MkDocs output rule), so files there passed every check on
 * the machine that had them and were never committed (PE 1); a fresh checkout
 * of a commit that registers a file it does not carry fails here.
 *
 * It reads the working tree, as `sdks.test.tsx` reads `sdk/`, so it runs in
 * a copy with no `.git` (`scripts/core_build.sh`).
 */
import { createHash } from 'crypto';
import fs from 'fs';
import path from 'path';
import registry from '@/content/site-screens.json';

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');
const FRONTEND = path.join(REPO_ROOT, 'frontend');
const SCREENS_DIR = path.join(FRONTEND, 'public', 'screens');

/** The four screenshots the plan names, in the order the page shows them (UX S1-S4). */
const PLANNED = ['results', 'flag-rollout', 'power-calculator', 'audit-log'];

/** Caps (C8, the architect's): one file, and the folder. */
const MAX_FILE_BYTES = 256 * 1024;
const MAX_DIR_BYTES = 1024 * 1024;

const ROW_KEYS = ['alt', 'bytes', 'caption', 'file', 'height', 'id', 'sha256', 'width'];

const rel = (p: string): string => path.relative(REPO_ROOT, p);

interface WebpFacts {
  width: number;
  height: number;
  chunks: string[];
  problems: string[];
}

/**
 * A WebP's canvas size and its chunks, read from the RIFF container, with what
 * is wrong with it: not RIFF/WEBP, a RIFF size that is not the file's, a
 * chunk that runs past the end, no image chunk, an EXIF or XMP chunk, or
 * the VP8X flags that announce one.
 */
function webpFacts(buf: Buffer): WebpFacts {
  const problems: string[] = [];
  const chunks: string[] = [];
  let width = 0;
  let height = 0;
  if (buf.length < 20 || buf.toString('latin1', 0, 4) !== 'RIFF' || buf.toString('latin1', 8, 12) !== 'WEBP') {
    return { width, height, chunks, problems: ['not a RIFF/WEBP file'] };
  }
  if (buf.readUInt32LE(4) !== buf.length - 8) problems.push(`the RIFF size is ${buf.readUInt32LE(4)}, the file holds ${buf.length - 8}`);
  let pos = 12;
  while (pos + 8 <= buf.length) {
    const fourcc = buf.toString('latin1', pos, pos + 4);
    const size = buf.readUInt32LE(pos + 4);
    const data = pos + 8;
    if (data + size > buf.length) {
      problems.push(`the ${JSON.stringify(fourcc)} chunk runs past the end of the file`);
      break;
    }
    chunks.push(fourcc);
    if (fourcc === 'VP8X') {
      const flags = buf[data];
      if (flags & 0x08) problems.push('the VP8X header announces EXIF');
      if (flags & 0x04) problems.push('the VP8X header announces XMP');
      width = 1 + buf.readUIntLE(data + 4, 3);
      height = 1 + buf.readUIntLE(data + 7, 3);
    } else if (fourcc === 'VP8 ' && width === 0) {
      if (buf[data + 3] !== 0x9d || buf[data + 4] !== 0x01 || buf[data + 5] !== 0x2a) problems.push('the VP8 frame has no start code');
      width = buf.readUInt16LE(data + 6) & 0x3fff;
      height = buf.readUInt16LE(data + 8) & 0x3fff;
    } else if (fourcc === 'VP8L' && width === 0) {
      if (buf[data] !== 0x2f) problems.push('the VP8L frame has no signature');
      const bits = buf.readUInt32LE(data + 1);
      width = (bits & 0x3fff) + 1;
      height = ((bits >> 14) & 0x3fff) + 1;
    } else if (fourcc === 'EXIF' || fourcc === 'XMP ') {
      problems.push(`it carries ${fourcc.trim()} metadata`);
    }
    pos = data + size + (size % 2);
  }
  if (!chunks.some((c) => c === 'VP8 ' || c === 'VP8L')) problems.push('it has no VP8 or VP8L image chunk');
  return { width, height, chunks, problems };
}

/** The registry's problems with one row, apart from its file. */
function rowProblems(row: Record<string, unknown>): string[] {
  const problems: string[] = [];
  const keys = Object.keys(row).sort();
  if (JSON.stringify(keys) !== JSON.stringify(ROW_KEYS)) problems.push(`keys ${keys.join(', ')}, want ${ROW_KEYS.join(', ')}`);
  const { id, file, sha256, alt, caption } = row as Record<string, string | null>;
  if (typeof sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(sha256)) problems.push('sha256 is not 64 hex digits');
  else if (file !== `${id}-${sha256.slice(0, 12)}.webp`) problems.push(`file ${file}, want ${id}-${sha256.slice(0, 12)}.webp`);
  for (const k of ['bytes', 'width', 'height']) {
    if (!Number.isInteger(row[k]) || (row[k] as number) <= 0) problems.push(`${k} is not a positive integer`);
  }
  if (typeof alt !== 'string' || alt.trim() === '') problems.push('the alt text is empty');
  else if (/\d/.test(alt)) problems.push(`the alt text has a figure in it: ${JSON.stringify(alt)}`);
  if (caption !== null && (typeof caption !== 'string' || caption.trim() === '')) problems.push('the caption is neither null nor a sentence');
  if (typeof caption === 'string') {
    for (const version of caption.match(/\bv\d+\.\d+\.\d+\b/g) ?? []) {
      if (version !== registry.captured_at) problems.push(`the caption names ${version}; the screenshots were captured at ${registry.captured_at}`);
    }
  }
  return problems;
}

/** Everything wrong with one registered file on disk. */
function fileProblems(row: { file: string; bytes: number; sha256: string; width: number; height: number }, dir = SCREENS_DIR): string[] {
  const file = path.join(dir, row.file);
  if (!fs.existsSync(file)) return [`${rel(file)} is missing: the registry names it, and it is not in the tree`];
  const buf = fs.readFileSync(file);
  const problems: string[] = [];
  if (buf.length !== row.bytes) problems.push(`${row.file} is ${buf.length} bytes, registered ${row.bytes}`);
  const digest = createHash('sha256').update(buf).digest('hex');
  if (digest !== row.sha256) problems.push(`${row.file} sha256 ${digest.slice(0, 12)}, registered ${row.sha256.slice(0, 12)}`);
  if (buf.length > MAX_FILE_BYTES) problems.push(`${row.file} is ${buf.length} bytes, over the ${MAX_FILE_BYTES}-byte cap`);
  const facts = webpFacts(buf);
  problems.push(...facts.problems.map((p) => `${row.file}: ${p}`));
  if (facts.problems.length === 0 && (facts.width !== row.width || facts.height !== row.height)) {
    problems.push(`${row.file} is ${facts.width}x${facts.height}, registered ${row.width}x${row.height}`);
  }
  return problems;
}

describe('content/site-screens.json', () => {
  it('is {captured_at, screens} and nothing else, captured at a release tag', () => {
    expect(Object.keys(registry).sort()).toEqual(['captured_at', 'screens']);
    expect(registry.captured_at).toMatch(/^v\d+\.\d+\.\d+$/);
  });

  it('names exactly the four planned screenshots, in order', () => {
    expect(registry.screens.map((s) => s.id)).toEqual(PLANNED);
  });

  it.each(registry.screens.map((s) => [s.id, s] as const))('%s: its row is whole, named by its sha256, with an alt text and no figure in it', (_id, row) => {
    expect(rowProblems(row as unknown as Record<string, unknown>)).toEqual([]);
  });
});

describe('frontend/public/screens/', () => {
  it('exists, and holds exactly the registered files', () => {
    if (!fs.existsSync(SCREENS_DIR)) {
      throw new Error(`${rel(SCREENS_DIR)} does not exist: content/site-screens.json registers ${registry.screens.length} files there`);
    }
    expect(fs.readdirSync(SCREENS_DIR).sort()).toEqual(registry.screens.map((s) => s.file).sort());
  });

  it.each(registry.screens.map((s) => [s.file, s] as const))(
    '%s: the registered bytes, a WebP still of the registered size, no EXIF or XMP, at most 256 KiB',
    (_file, row) => {
      expect(fileProblems(row)).toEqual([]);
    },
  );

  it('holds at most 1 MiB in all', () => {
    if (!fs.existsSync(SCREENS_DIR)) throw new Error(`${rel(SCREENS_DIR)} does not exist`);
    const total = fs.readdirSync(SCREENS_DIR).reduce((n, f) => n + fs.statSync(path.join(SCREENS_DIR, f)).size, 0);
    expect(total).toBeGreaterThan(0);
    expect(total).toBeLessThanOrEqual(MAX_DIR_BYTES);
  });

  /**
   * The folder must survive both builds and serve as files: not `media`
   * (`fetch-site-media.mjs` refuses an export that already has `out/media`),
   * not a route the marketing build prunes (read from its script as text),
   * and not a page.
   */
  it('is a folder both exports keep', () => {
    const name = path.basename(SCREENS_DIR);
    expect(name).not.toBe('media');
    const prune = fs.readFileSync(path.join(FRONTEND, 'scripts', 'prune-marketing-site.mjs'), 'utf8');
    const list = /const PLATFORM_ONLY = \[([^\]]*)\]/.exec(prune);
    expect(list).not.toBeNull();
    const pruned = Array.from((list as RegExpExecArray)[1].matchAll(/'([^']+)'/g)).map((m) => m[1]);
    expect(pruned.length).toBeGreaterThan(5);
    expect(pruned).not.toContain(name);
    for (const ext of ['tsx', 'ts', 'jsx', 'js']) {
      expect(fs.existsSync(path.join(FRONTEND, 'src', 'pages', `${name}.${ext}`))).toBe(false);
    }
    expect(fs.existsSync(path.join(FRONTEND, 'src', 'pages', name))).toBe(false);
  });
});

/** The parser refuses what the rule refuses, so a pass above means something. */
describe('webpFacts', () => {
  const chunk = (fourcc: string, data: Buffer): Buffer => {
    const head = Buffer.alloc(8);
    head.write(fourcc, 0, 'latin1');
    head.writeUInt32LE(data.length, 4);
    return Buffer.concat([head, data, data.length % 2 ? Buffer.alloc(1) : Buffer.alloc(0)]);
  };
  const riff = (...chunks: Buffer[]): Buffer => {
    const body = Buffer.concat([Buffer.from('WEBP', 'latin1'), ...chunks]);
    const head = Buffer.alloc(8);
    head.write('RIFF', 0, 'latin1');
    head.writeUInt32LE(body.length, 4);
    return Buffer.concat([head, body]);
  };
  // A VP8L frame header for 3x2: signature, then (width-1) and (height-1) in 14 bits each.
  const vp8l = (() => {
    const b = Buffer.alloc(6);
    b[0] = 0x2f;
    b.writeUInt32LE((3 - 1) | ((2 - 1) << 14), 1);
    return chunk('VP8L', b);
  })();
  const vp8x = (flags: number): Buffer => {
    const b = Buffer.alloc(10);
    b[0] = flags;
    b.writeUIntLE(3 - 1, 4, 3);
    b.writeUIntLE(2 - 1, 7, 3);
    return chunk('VP8X', b);
  };

  it('reads a plain still', () => {
    expect(webpFacts(riff(vp8l))).toEqual({ width: 3, height: 2, chunks: ['VP8L'], problems: [] });
  });

  it('refuses EXIF and XMP, as a chunk or as a VP8X flag', () => {
    expect(webpFacts(riff(vp8x(0x08), vp8l, chunk('EXIF', Buffer.from('Exif\0\0'))) ).problems).toEqual([
      'the VP8X header announces EXIF',
      'it carries EXIF metadata',
    ]);
    expect(webpFacts(riff(vp8x(0x04), vp8l, chunk('XMP ', Buffer.from('<x:xmpmeta/>')))).problems).toEqual([
      'the VP8X header announces XMP',
      'it carries XMP metadata',
    ]);
  });

  it('refuses a file that is not a whole WebP', () => {
    expect(webpFacts(Buffer.from('\x89PNG\r\n\x1a\n0000000000000', 'latin1')).problems).toEqual(['not a RIFF/WEBP file']);
    const whole = riff(vp8l);
    expect(webpFacts(whole.subarray(0, whole.length - 2)).problems[0]).toMatch(/RIFF size/);
    expect(webpFacts(riff(chunk('ICCP', Buffer.alloc(4)))).problems).toEqual(['it has no VP8 or VP8L image chunk']);
  });
});
