/**
 * `content/home-quotes.json`: every homepage sentence that quotes a docs page
 * says only what that page says (#1459 SPEC A, folding in #1415).
 *
 * Three things are checked here:
 *
 *   1. the table against docs/ and mkdocs.yml, by the rules in
 *      `home-quotes-rules.ts`: each phrase is in its quote and on the page it
 *      cites, a link's text is its page's title, the page is in the nav, and a
 *      quote says "beta" exactly when the page it links is titled "(beta)";
 *   2. the rules themselves, by the cases in `fixtures/home-quotes-cases.json`,
 *      which the Python reader runs too;
 *   3. the page against the table: each quote renders once in each build,
 *      word for word, as `[data-quote=<id>]`, and its link goes where
 *      `docsUrl()` sends that page (in the marketing build, docs.controlvar.com).
 *
 * The pages are read from the working tree, as `content/sdks.test.tsx` reads
 * `sdk/`, so this passes in a copy with no `.git`. A missing page is a
 * refusal, never a skip. A pull request that changes only docs/ (or
 * mkdocs.yml) runs no Jest; there the Python reader,
 * `backend/tests/unit/docs/test_homepage_quotes_docs.py`, checks 1 and 2, so
 * a page reworded on its own turns that pull request itself red.
 */
import fs from 'fs';
import path from 'path';
import React from 'react';
import { render } from '@testing-library/react';
import table from '@/content/home-quotes.json';
import HomePage from '@/pages/index';
import { AuthProvider } from '@/contexts/AuthContext';
import { docsUrl } from '@/services/docs';
import { isMarketingSite } from '@/utils/site-mode';
import { problems, QuoteRow, Read, RULES } from './home-quotes-rules';

jest.mock('@/utils/site-mode', () => ({
  ...jest.requireActual('@/utils/site-mode'),
  isMarketingSite: jest.fn(),
}));
jest.mock('next/head', () => { const H=({children}:{children:React.ReactNode})=><>{children}</>; H.displayName='H'; return H; });
jest.mock('next/router', () => ({ useRouter: () => ({ pathname:'/', asPath:'/', query:{}, push:jest.fn(), replace:jest.fn(), isReady:true }) }));

const mode = isMarketingSite as jest.Mock;

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');
const FIXTURE = 'frontend/src/tests/fixtures/home-quotes-cases.json';

/** A file of the working tree, or null when it does not exist. */
const readRepo: Read = (rel) => {
  const file = path.join(REPO_ROOT, rel);
  return fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : null;
};

const QUOTES: readonly QuoteRow[] = table.quotes;

/** What a quote renders: its parts joined (a link by its text), or its code lines. */
const words = (row: QuoteRow): string =>
  row.parts ? row.parts.map((p) => (typeof p === 'string' ? p : p.link)).join('') : (row.code ?? []).join('\n');

const LINKS = QUOTES.flatMap((row) =>
  (row.parts ?? []).flatMap((p) => (typeof p === 'string' ? [] : [[row.id, p.page, p.link] as const])),
);

beforeEach(() => {
  localStorage.clear();
  mode.mockReset().mockReturnValue(false);
  global.fetch = jest.fn(() => Promise.reject(new Error('no API'))) as unknown as typeof fetch;
});

describe('content/home-quotes.json against docs/', () => {
  it('has quotes to check, so an empty table cannot pass', () => {
    expect(QUOTES.length).toBeGreaterThanOrEqual(2);
    expect(LINKS.length).toBeGreaterThanOrEqual(1);
  });

  it.each(QUOTES.map((row) => [row.id, row] as const))('%s says only what the pages it cites say', (_id, row) => {
    expect(problems({ quotes: [row] }, readRepo)).toEqual([]);
  });

  it('passes whole, every id distinct', () => {
    expect(problems(table, readRepo)).toEqual([]);
  });
});

describe('the rules, by the cases both readers run', () => {
  interface Case {
    name: string;
    refusals: string[];
    files?: Record<string, string | null>;
    table: unknown;
  }
  const text = readRepo(FIXTURE);
  if (text === null) throw new Error(`${FIXTURE} does not exist`);
  const fixture = JSON.parse(text) as { files: Record<string, string>; cases: Case[] };

  it.each(fixture.cases.map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const files: Record<string, string | null> = { ...fixture.files, ...(c.files ?? {}) };
    const read: Read = (rel) => {
      const value = files[rel];
      return typeof value === 'string' ? value : null;
    };
    expect(problems(c.table, read).map((p) => p.code)).toEqual(c.refusals);
  });

  it('has a case for every rule this reader knows, and names no other', () => {
    // Array.from, not a spread: this jest compiles a Set spread to [].
    const named = Array.from(new Set(fixture.cases.flatMap((c) => c.refusals)));
    expect(named.sort()).toEqual([...RULES].sort());
    expect(fixture.cases.some((c) => c.refusals.length === 0)).toBe(true);
  });
});

describe.each([
  ['platform build', false],
  ['marketing build', true],
])('the homepage, %s', (_build, marketing) => {
  beforeEach(() => mode.mockReturnValue(marketing));

  const page = (): HTMLElement =>
    render(
      <AuthProvider>
        <HomePage />
      </AuthProvider>,
    ).container;

  it.each(QUOTES.map((row) => [row.id, row] as const))('renders %s once, word for word', (id, row) => {
    const found = page().querySelectorAll(`[data-quote="${id}"]`);
    expect(found).toHaveLength(1);
    expect(found[0].textContent).toBe(words(row));
  });

  it.each(LINKS)('links %s to docsUrl(%s), as "%s"', (id, docsPage, linkText) => {
    const anchors = page().querySelectorAll(`[data-quote="${id}"] a`);
    expect(anchors).toHaveLength(1);
    expect(anchors[0].textContent).toBe(linkText);
    expect(anchors[0].getAttribute('href')).toBe(docsUrl(docsPage));
  });

  it('renders no quote the table does not hold', () => {
    const rendered = Array.from(page().querySelectorAll('[data-quote]')).map((e) => e.getAttribute('data-quote'));
    expect(rendered.sort()).toEqual(QUOTES.map((row) => row.id).sort());
  });
});

/**
 * The marketing build as `npm run build:marketing` makes it: its
 * NEXT_PUBLIC_DOCS_URL, read from package.json, and the page rendered to HTML
 * as the static export renders it.
 *
 * The tests above run with no NEXT_PUBLIC_DOCS_URL, where `docsUrl()` gives
 * the repository's GitHub URL, so a link hard-coded to that URL passed them
 * (#1459 Q round 1, plant 7b: 48 of 48 green). Here the same link must be
 * docs.controlvar.com, so a hard-code fails in one setting or the other.
 * DOCS_SITE is read at module load, so the page, React and the renderer are
 * all loaded in one fresh module registry (`isolateModules`), and rendered
 * with `renderToStaticMarkup`, which needs no second copy of the DOM renderer.
 */
describe('the marketing build, rendered with its docs URL', () => {
  const scripts = JSON.parse(readRepo('frontend/package.json') ?? '{}').scripts as Record<string, string>;
  const site = /NEXT_PUBLIC_DOCS_URL=(\S+)/.exec(scripts['build:marketing'] ?? '')?.[1];

  it('is docs.controlvar.com', () => {
    expect(site).toBe('https://docs.controlvar.com/');
  });

  it.each(LINKS)('links %s to docs.controlvar.com, at docs/%s, in the rendered page', (id, docsPage, linkText) => {
    const before = process.env.NEXT_PUBLIC_DOCS_URL;
    let html = '';
    let expected = '';
    try {
      jest.isolateModules(() => {
        process.env.NEXT_PUBLIC_DOCS_URL = site;
        const R: typeof import('react') = require('react');
        const server: typeof import('react-dom/server') = require('react-dom/server');
        (require('@/utils/site-mode').isMarketingSite as jest.Mock).mockReturnValue(true);
        const Home: typeof HomePage = require('@/pages/index').default;
        const Auth: typeof AuthProvider = require('@/contexts/AuthContext').AuthProvider;
        expected = (require('@/services/docs') as typeof import('@/services/docs')).docsUrl(docsPage);
        html = server.renderToStaticMarkup(R.createElement(Auth, null, R.createElement(Home)));
      });
    } finally {
      if (before === undefined) delete process.env.NEXT_PUBLIC_DOCS_URL;
      else process.env.NEXT_PUBLIC_DOCS_URL = before;
    }
    const dir = docsPage.replace(/(^|\/)README$/, '');
    expect(expected).toBe(dir ? `https://docs.controlvar.com/${dir}/` : 'https://docs.controlvar.com/');
    // The marketing build really rendered: its CTA, not the sign-in.
    expect(html).toContain('Try the power calculator');
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const anchors = doc.querySelectorAll(`[data-quote="${id}"] a`);
    expect(anchors).toHaveLength(1);
    expect(anchors[0].textContent).toBe(linkText);
    expect(anchors[0].getAttribute('href')).toBe(expected);
  });
});

/**
 * A homepage component links the docs only through `docsUrl`. An `href`
 * written as an absolute URL is a link `docs-links.test.ts` never sees, and
 * one equal to the repository's docs URL passes every render test that sets no
 * NEXT_PUBLIC_DOCS_URL. A cheap guard beside the rendered one above.
 */
describe('components/homepage', () => {
  const DIR = path.join(REPO_ROOT, 'frontend', 'src', 'components', 'homepage');
  const files = (dir: string): string[] =>
    fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
      e.isDirectory() ? files(path.join(dir, e.name)) : /\.tsx?$/.test(e.name) ? [path.join(dir, e.name)] : [],
    );

  it('writes no href as an absolute URL', () => {
    const found = files(DIR);
    expect(found.map((f) => path.basename(f))).toContain('Quote.tsx');
    const literal = /href=\s*\{?\s*["'`]https?:/g;
    const hits = found.flatMap((f) => {
      const text = fs.readFileSync(f, 'utf8');
      return (text.match(literal) ?? []).map((m) => `${path.relative(REPO_ROOT, f)}: ${m}`);
    });
    expect(hits).toEqual([]);
  });
});
