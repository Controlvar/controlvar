/**
 * Guard: every `docsUrl('...')` literal in the dashboard must address a file
 * that exists under `docs/`.
 *
 * Why this exists. The docs links were dead — all of them. `docsUrl()`
 * defaulted to the MkDocs site, which `.github/workflows/docs.yml` publishes
 * only on a release tag from the public repository; there are no tags and the
 * repository is private, so the site had never been built and every link
 * answered 404. Nothing noticed, because nothing checked either half: not that
 * the destination exists, and not that the page does.
 *
 * This is the second half — the page. It is the same shape as
 * `url-literals.test.ts`, which checks `/api/v1/` literals against the OpenAPI
 * dump: scan the sources, resolve each literal, assert the target is real.
 *
 * It cannot check that the SITE is reachable (no network in a unit test, and
 * the answer depends on repository visibility). What it does check is that a
 * link never points at a page that does not exist — which is the failure this
 * codebase can actually introduce, by renaming or deleting a doc.
 *
 * A pull request that changes docs/ alone runs no Node, so this rule also
 * lives in Python: `backend/tests/unit/docs/test_dashboard_docs_links.py`,
 * which the docs-only leg runs. That file reads this one as text and pins the
 * regex, the skips and the README branch below; change the rule in both.
 */
import { existsSync, readFileSync, readdirSync, statSync } from 'fs';
import { join, resolve } from 'path';

const REPO_ROOT = resolve(__dirname, '../../../..');
const DOCS_ROOT = join(REPO_ROOT, 'docs');
const SRC_ROOT = resolve(__dirname, '../..');

/** Every source file the dashboard ships, excluding tests and mocks. */
function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === 'tests' || entry === '__mocks__' || entry === 'node_modules') continue;
      sourceFiles(full, out);
    } else if (/\.(ts|tsx)$/.test(entry) && !/\.test\.tsx?$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

/** The path `docsUrl(page)` addresses, mirroring the README handling in docs.ts. */
function targetFor(page: string): string {
  const path = page.replace(/^\/+|\/+$/g, '');
  if (path === '' || path === 'README') return join(DOCS_ROOT, 'README.md');
  if (path.endsWith('/README')) return join(DOCS_ROOT, `${path}.md`);
  return join(DOCS_ROOT, `${path}.md`);
}

describe('every docsUrl() link addresses a real page', () => {
  const files = sourceFiles(SRC_ROOT);
  const found: Array<{ page: string; file: string }> = [];

  // An `exec` loop, not `String.prototype.matchAll`. Under this jest
  // environment `matchAll` is present (`typeof` is "function") and yields
  // NOTHING, while `match` on the identical regex returns its two hits — so
  // the first version of this scan reported zero call sites and only the
  // vacuity guard below caught it. A method existing is not evidence it works.
  for (const file of files) {
    // The module that DEFINES docsUrl is not a call site: its docstring spells
    // `docsUrl('...')` to explain the argument, and the scan matched that.
    if (file.endsWith(join('services', 'docs.ts'))) continue;
    const text = readFileSync(file, 'utf8');
    const re = /docsUrl\(\s*'([^']*)'/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(text)) !== null) {
      found.push({ page: m[1], file: file.slice(REPO_ROOT.length + 1) });
    }
  }

  it('finds the call sites at all, so an empty scan cannot pass vacuously', () => {
    // The scan is the gate. If a refactor renames docsUrl or moves the sources,
    // this test would otherwise report success over zero links.
    expect(found.length).toBeGreaterThan(20);
    expect(existsSync(DOCS_ROOT)).toBe(true);
  });

  it('points every link at a file that exists', () => {
    const missing = found
      .filter(({ page }) => !existsSync(targetFor(page)))
      .map(({ page, file }) => `${file}: docsUrl('${page}') -> ${targetFor(page).slice(REPO_ROOT.length + 1)}`);
    expect(missing).toEqual([]);
  });

  // The scan above reads only a single-quoted literal page. A call built from
  // a variable, a template or double quotes is a link it never sees: three
  // such calls passed it (#1459, PE 6). So every call must be one literal
  // page, followed by `,` (an anchor) or `)`. A list of links is a map of
  // literal calls (content/sdks.ts's SDK_GUIDES, components/homepage/Quote.tsx).
  it('reads every docsUrl() call, so none is a link this test cannot check', () => {
    const unseen: string[] = [];
    for (const file of files) {
      if (file.endsWith(join('services', 'docs.ts'))) continue;
      const text = readFileSync(file, 'utf8');
      const call = /docsUrl\(/g;
      let m: RegExpExecArray | null;
      while ((m = call.exec(text)) !== null) {
        const literal = /^docsUrl\(\s*'[^']*'\s*[,)]/;
        if (!literal.test(text.slice(m.index))) {
          const line = text.slice(0, m.index).split('\n').length;
          unseen.push(`${file.slice(REPO_ROOT.length + 1)}:${line}: ${text.slice(m.index, m.index + 60).split('\n')[0]}`);
        }
      }
    }
    expect(unseen).toEqual([]);
  });
});
