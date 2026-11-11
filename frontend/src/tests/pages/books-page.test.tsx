/**
 * The /books page (site design 2026-11-11): the Controlvar books' pitch, who
 * they are for and their licence, then every book in its README's groups,
 * each linking its own README in the books repository.
 *
 *   - a page of the marketing build only (`getStaticProps`; a platform build
 *     writes no file, which `scripts/check-platform-export.mjs` holds);
 *   - every book of `content/books.ts`, in its group, in order, with its title,
 *     its line and a link to `<BOOKS_URL>/blob/main/<dir>/README.md`, and the
 *     repository itself linked;
 *   - no number of books anywhere on the page: the books' README says "Nine"
 *     over a list of ten and the repository's description "Eight", and that
 *     is the books repository's to fix, not this page's to repeat;
 *   - one h1, the groups as h2s and the books as h3s, so no level is skipped;
 *   - no claim the homepage's sweeps forbid (`claimText`), and no
 *     accessibility claim.
 *
 * `scripts/site-pages.test.mjs` holds the exported page to the same links.
 */
import React from 'react';
import { render, within } from '@testing-library/react';
import BooksPage, { getStaticProps } from '@/pages/books';
import { BOOK_GROUPS, BOOKS, CORE_PATH, bookUrl } from '@/content/books';
import { BOOKS_URL } from '@/content/site-links';
import { isMarketingSite } from '@/utils/site-mode';
import { claimText, metaDescriptions } from './helpers/claimText';

jest.mock('@/utils/site-mode', () => ({
  ...jest.requireActual('@/utils/site-mode'),
  isMarketingSite: jest.fn(),
}));
jest.mock('next/head', () => { const H = ({ children }: { children: React.ReactNode }) => <>{children}</>; H.displayName = 'H'; return H; });
jest.mock('next/router', () => ({ useRouter: () => ({ pathname: '/books', asPath: '/books', query: {}, push: jest.fn(), replace: jest.fn(), isReady: true }) }));

const mode = isMarketingSite as jest.Mock;

beforeEach(() => mode.mockReset().mockReturnValue(true));

describe('/books', () => {
  it('is a page of the marketing build only', () => {
    expect(getStaticProps()).toEqual({ props: {} });
    mode.mockReturnValue(false);
    expect(getStaticProps()).toEqual({ notFound: true });
  });

  it('lists every book in its group, in the README order, each linking its README', () => {
    const { container } = render(<BooksPage />);
    const groups = BOOK_GROUPS.map((group) => {
      const section = container.querySelector(`section[aria-labelledby="${group.id}-heading"]`) as HTMLElement;
      return {
        label: within(section).getByRole('heading', { level: 2 }).textContent,
        when: section.querySelector('h2 + p')?.textContent,
        books: Array.from(section.querySelectorAll('li > a')).map((a) => [
          a.querySelector('h3')?.textContent,
          a.querySelector('p')?.textContent,
          a.getAttribute('href'),
        ]),
      };
    });
    expect(groups).toEqual(
      BOOK_GROUPS.map((group) => ({
        label: group.label,
        when: group.when,
        books: group.books.map((book) => [book.title, book.line, bookUrl(book)]),
      })),
    );
    expect(groups.map((g) => g.label)).toEqual([
      'Core path',
      'Specializations',
      'Before your first change',
      'Working with AI coding tools',
    ]);
    expect(CORE_PATH).toBe(BOOK_GROUPS[0].books);
  });

  it('links each book to a README in the books repository, each once', () => {
    const { container } = render(<BooksPage />);
    const readmes = Array.from(container.querySelectorAll('a'))
      .map((a) => a.getAttribute('href') ?? '')
      .filter((href) => href.startsWith(`${BOOKS_URL}/`));
    expect(readmes).toEqual(BOOKS.map(bookUrl));
    expect(new Set(readmes).size).toBe(readmes.length);
    for (const href of readmes) expect(href).toMatch(/^https:\/\/github\.com\/amarkanday\/controlvar-books\/blob\/main\/[a-z0-9-]+\/README\.md$/);
    expect(Array.from(container.querySelectorAll('a')).filter((a) => a.getAttribute('href') === BOOKS_URL)).toHaveLength(1);
  });

  it('says what the books teach, who they are for and their licence', () => {
    const { container } = render(<BooksPage />);
    expect(container.querySelector('h1')?.textContent).toBe('The Controlvar books');
    const text = container.textContent ?? '';
    expect(text).toContain('through one real, open-source codebase: Controlvar itself');
    expect(text).toContain('Who they are for');
    expect(text).toContain('licensed CC BY 4.0');
  });

  it('says no number of books, in digits or in words', () => {
    const { container } = render(<BooksPage />);
    const text = claimText(container);
    expect(text).not.toMatch(/\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+books\b/i);
  });

  it('skips no heading level: one h1, the groups h2, the books h3', () => {
    const { container } = render(<BooksPage />);
    const levels = Array.from(container.querySelectorAll('h1, h2, h3, h4, h5, h6')).map((h) => Number(h.tagName[1]));
    expect(levels[0]).toBe(1);
    expect(levels.filter((l) => l === 1)).toHaveLength(1);
    expect(levels.flatMap((l, i) => (i > 0 && l > levels[i - 1] + 1 ? [i] : []))).toEqual([]);
    expect(levels.filter((l) => l === 3)).toHaveLength(BOOKS.length);
  });

  it('has a description for its link preview, and makes no accessibility claim', () => {
    const { container } = render(<BooksPage />);
    const descriptions = metaDescriptions(container);
    expect(descriptions).toHaveLength(1);
    expect(descriptions[0]).toMatch(/CC BY 4\.0/);
    expect(claimText(container)).not.toMatch(new RegExp(['accessi', 'b'].join(''), 'i'));
  });
});
