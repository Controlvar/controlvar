/**
 * The marketing export's pages, as files (site polish 2026-11-10, #4 and #5):
 *
 *   npm run build:marketing && node --test scripts/site-pages.test.mjs
 *
 * `Frontend Tests` runs it beside site-head.test.mjs, over the export the
 * marketing step just built. A missing file is a failure, never a skip, so it
 * cannot pass on a build that did not happen.
 *
 *   - out/404.html is the site's not-found page (pages/404.tsx), not Next's
 *     default "This page could not be found", with links to / and /docs;
 *   - each page the site ships, the not-found page among them, has exactly one
 *     <footer>, and it is the site's (its <nav aria-label="Footer">);
 *   - each kept page is also at <page>/index.html, byte for byte, so /docs/
 *     is the docs hub (prune-marketing-site.mjs writes the copies);
 *   - out/robots.txt lets every crawler in.
 *
 * What controlvar.com answers for a missing path is Amplify's console rule,
 * which this repository cannot see or test.
 */
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

const OUT = fileURLToPath(new URL('../out/', import.meta.url));

/** The kept pages prune-marketing-site.mjs copies to `<page>/index.html`. */
const KEPT = ['docs', 'power-calculator', 'privacy'];
const PAGES = ['index.html', ...KEPT.map((p) => `${p}.html`), '404.html'];

function readOut(name) {
  const path = `${OUT}${name}`;
  assert.ok(existsSync(path), `out/${name} is missing; run npm run build:marketing first`);
  return readFileSync(path);
}

/** The page's HTML with every <script> removed: what it renders, not its hydration data. */
const markup = (name) => readOut(name).toString('utf8').replace(/<script\b[\s\S]*?<\/script>/gi, '');

describe('the not-found page', () => {
  it('is the site\'s own page, not Next\'s default', () => {
    const html = markup('404.html');
    assert.match(html, /<h1\b[^>]*>Page not found<\/h1>/);
    assert.doesNotMatch(html, /This page could not be found/);
  });

  it('links home and to the docs hub', () => {
    const html = markup('404.html');
    const main = /<main\b[\s\S]*?<\/main>/.exec(html)?.[0] ?? '';
    const hrefs = [...main.matchAll(/<a\b[^>]*\bhref="([^"]*)"/g)].map((m) => m[1]);
    assert.deepEqual(hrefs, ['/', '/docs']);
  });
});

describe("every page ends with the site's footer", () => {
  for (const name of PAGES) {
    it(`out/${name}`, () => {
      const footers = [...markup(name).matchAll(/<footer\b[\s\S]*?<\/footer>/gi)].map((m) => m[0]);
      assert.equal(footers.length, 1, `out/${name} has ${footers.length} <footer>`);
      assert.match(footers[0], /<nav\b[^>]*aria-label="Footer"/, `out/${name}: the footer is not the site's`);
    });
  }
});

describe('each kept page is at its trailing-slash path too', () => {
  for (const page of KEPT) {
    it(`out/${page}/index.html is out/${page}.html`, () => {
      assert.ok(readOut(`${page}/index.html`).equals(readOut(`${page}.html`)), `out/${page}/index.html differs`);
    });
  }
});

describe('robots.txt', () => {
  it('lets every crawler in', () => {
    const lines = readOut('robots.txt').toString('utf8').split(/\r?\n/).map((l) => l.trim());
    assert.ok(lines.includes('User-agent: *'), 'no "User-agent: *" group');
    assert.ok(lines.includes('Allow: /'), 'no "Allow: /"');
    assert.deepEqual(lines.filter((l) => /^disallow:\s*\S/i.test(l)), [], 'a Disallow rule');
  });
});
