#!/usr/bin/env node
/**
 * The dashboard a self-hoster builds carries none of the walkthroughs (#1290).
 *
 *   npm run build && node scripts/check-platform-export.mjs [outDir]
 *
 * The `Frontend Tests` CI job runs it on both platform exports, the full
 * profile and the core one. The walkthroughs section renders only in the
 * marketing build, and its files are fetched only there; this is the check
 * that neither leaks into the export the dashboard image is built from.
 *
 * It fails when
 *   - `out/index.html` is missing, or does not offer "Sign in to the
 *     dashboard": the platform homepage, so the check cannot pass on an empty
 *     or wrong page;
 *   - `out/index.html` has a `<video>` or references `/media/`;
 *   - `out/index.html` references `/screens/`: the homepage's dashboard
 *     screenshots are the marketing build's only (#1459 C2), and a
 *     self-hoster's homepage renders those rows as text. The files themselves
 *     are allowed: public/ is copied into every export, and they are capped
 *     and show demo data (#1459 PE 2);
 *   - `out/media` exists, or an `.mp4`, `.webm`, `.vtt` or `.srt` file is
 *     anywhere in the export.
 */
import { existsSync, readdirSync, readFileSync, realpathSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const MEDIA_FILE = /\.(mp4|webm|vtt|srt)$/i;
// A root-relative `/media/` path; Next's own `/_next/static/media/<asset>` is not one.
const MEDIA_REFERENCE = /(?<![\w.~%/-])\/media\//;
// The same for the screenshots in public/screens/ (components/homepage/Screenshot.tsx).
const SCREENS_REFERENCE = /(?<![\w.~%/-])\/screens\//;

function* walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(path);
    else yield path;
  }
}

/** Every reason this is not a clean platform export. */
export function platformProblems(outDir) {
  const index = join(outDir, 'index.html');
  if (!existsSync(index)) return [`${index} is missing; run npm run build first`];
  const problems = [];
  const html = readFileSync(index, 'utf8');
  if (!html.includes('Sign in to the dashboard')) problems.push('out/index.html does not offer "Sign in to the dashboard": not the platform homepage');
  const videos = (html.match(/<video\b/gi) ?? []).length;
  if (videos) problems.push(`out/index.html has ${videos} <video>: the walkthroughs section rendered in the platform build`);
  if (MEDIA_REFERENCE.test(html)) problems.push('out/index.html references /media/');
  if (SCREENS_REFERENCE.test(html)) problems.push('out/index.html references /screens/: a screenshot rendered in the platform build');
  if (existsSync(join(outDir, 'media'))) problems.push('out/media exists in the platform export');
  for (const path of walk(outDir)) {
    if (MEDIA_FILE.test(path)) problems.push(`the platform export carries ${relative(outDir, path)}`);
  }
  return problems;
}

function invokedAsScript() {
  if (!process.argv[1]) return false;
  try {
    return realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
}

if (invokedAsScript()) {
  const outDir = resolve(process.argv[2] ?? 'out');
  const problems = platformProblems(outDir);
  if (problems.length) {
    for (const p of problems) console.error(`check-platform-export: FAIL ${p}`);
    process.exitCode = 1;
  } else {
    console.log(`check-platform-export: ${outDir}: no walkthrough section, no media and no screenshot; the platform homepage is there`);
  }
}
