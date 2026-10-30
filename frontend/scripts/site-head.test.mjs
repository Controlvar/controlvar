/**
 * The link-preview tags in the marketing build's own HTML, and the image they
 * point at (#1458):
 *
 *   npm run build:marketing && node --test scripts/site-head.test.mjs
 *
 * `Frontend Tests` runs it in the marketing step, after check-site-media.mjs,
 * over the export that step just built. It reads only that export: out/index.html,
 * out/docs.html, out/power-calculator.html, out/privacy.html and out/og.png.
 * A missing directory or file is a failure, never a skip, so it cannot pass
 * on a build that did not happen.
 *
 * Per page, in <head>: og:type, og:title, og:description, og:url, og:image
 * (with its width and height), twitter:card, twitter:title,
 * twitter:description and twitter:image, each exactly once and no other og: or
 * twitter: tag. The titles are the page's <title>, the descriptions its
 * <meta name="description">, og:url the page's address on controlvar.com, and
 * both images the absolute https URL of a file in out/: a PNG of 1200 x 630.
 */
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

const OUT = fileURLToPath(new URL('../out/', import.meta.url));
const ORIGIN = 'https://controlvar.com';
const IMAGE = { url: `${ORIGIN}/og.png`, width: 1200, height: 630 };

const PAGES = [
  { file: 'index.html', url: `${ORIGIN}/` },
  { file: 'docs.html', url: `${ORIGIN}/docs` },
  { file: 'power-calculator.html', url: `${ORIGIN}/power-calculator` },
  { file: 'privacy.html', url: `${ORIGIN}/privacy` },
];

const KEYS = [
  'og:type',
  'og:title',
  'og:description',
  'og:url',
  'og:image',
  'og:image:width',
  'og:image:height',
  'twitter:card',
  'twitter:title',
  'twitter:description',
  'twitter:image',
];

const unescapeHtml = (s) =>
  s.replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

/** The attributes of one start tag's attribute text, names lower-cased. */
function attributes(source) {
  const attrs = new Map();
  for (const m of source.matchAll(/([^\s"'=<>/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'))?/g)) {
    attrs.set(m[1].toLowerCase(), unescapeHtml(m[2] ?? m[3] ?? ''));
  }
  return attrs;
}

function readOut(name) {
  const path = `${OUT}${name}`;
  assert.ok(existsSync(path), `out/${name} is missing; run npm run build:marketing first`);
  return readFileSync(path);
}

/** <head>'s <title>, description and every og:/twitter: tag, as [key, content] pairs. */
function head(html, name) {
  const match = /<head\b[^>]*>([\s\S]*?)<\/head>/i.exec(html);
  assert.ok(match, `out/${name} has no <head>`);
  const metas = [...match[1].matchAll(/<meta\b([^>]*)>/gi)].map((m) => attributes(m[1]));
  const titles = [...match[1].matchAll(/<title\b[^>]*>([^<]*)<\/title>/gi)].map((m) => unescapeHtml(m[1]));
  const descriptions = metas.filter((a) => a.get('name') === 'description').map((a) => a.get('content'));
  const share = metas
    .map((a) => [a.get('property') ?? a.get('name') ?? '', a.get('content') ?? ''])
    .filter(([key]) => key.startsWith('og:') || key.startsWith('twitter:'));
  return { titles, descriptions, share };
}

/** A PNG's width and height from its IHDR chunk, or null when it is not a PNG. */
function pngSize(buf) {
  const signature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  if (buf.length < 24 || !buf.subarray(0, 8).equals(signature) || buf.toString('latin1', 12, 16) !== 'IHDR') return null;
  return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
}

describe('the marketing export carries link-preview tags', () => {
  for (const page of PAGES) {
    it(`out/${page.file}`, () => {
      const { titles, descriptions, share } = head(readOut(page.file).toString('utf8'), page.file);
      assert.equal(titles.length, 1, `out/${page.file} has ${titles.length} <title>`);
      assert.equal(descriptions.length, 1, `out/${page.file} has ${descriptions.length} <meta name="description">`);
      const [title] = titles;
      const [description] = descriptions;
      assert.ok(description, `out/${page.file} has an empty description`);

      const keys = share.map(([key]) => key).sort();
      assert.deepEqual(keys, [...KEYS].sort(), `out/${page.file}: the og:/twitter: tags, each once`);
      assert.deepEqual(Object.fromEntries(share), {
        'og:type': 'website',
        'og:title': title,
        'og:description': description,
        'og:url': page.url,
        'og:image': IMAGE.url,
        'og:image:width': String(IMAGE.width),
        'og:image:height': String(IMAGE.height),
        'twitter:card': 'summary_large_image',
        'twitter:title': title,
        'twitter:description': description,
        'twitter:image': IMAGE.url,
      });
    });
  }
});

describe('the image the tags point at', () => {
  it('is an absolute https URL on controlvar.com, to a file in out/', () => {
    const url = new URL(IMAGE.url);
    assert.equal(url.protocol, 'https:');
    assert.equal(url.host, 'controlvar.com');
    readOut(url.pathname.slice(1));
  });

  it(`is a PNG of ${IMAGE.width} x ${IMAGE.height}`, () => {
    const file = new URL(IMAGE.url).pathname.slice(1);
    assert.deepEqual(pngSize(readOut(file)), { width: IMAGE.width, height: IMAGE.height });
  });
});
