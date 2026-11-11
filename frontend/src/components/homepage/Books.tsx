import React from 'react';
import Link from 'next/link';
import { BOOKS_URL } from '@/content/site-links';
import { CORE_PATH, bookUrl } from '@/content/books';
import { isMarketingSite } from '@/utils/site-mode';
import { FOCUS, H2, MORE_LINK, WIDTH } from '@/components/homepage/styles';

/**
 * The Controlvar books (#1859), near the top of the page (site design
 * 2026-11-11, item 4): it sat at 87% of the page's height, under the deploy
 * cards. The words are the books' own README's (what they teach, who they are
 * for, the licence), then the core path's four books as cards, each linking
 * its own README (`content/books.ts`), then the way to all of them: the
 * /books page on the marketing build, the books repository on a self-hosted
 * platform, which has no /books page. No number of books: the README and the
 * repository's description disagree on it.
 */
export function Books() {
  const marketing = isMarketingSite();
  const all = (
    <>
      All the books <span aria-hidden="true">→</span>
    </>
  );
  return (
    <section aria-labelledby="books-heading" className={`${WIDTH} py-12 lg:py-16`}>
      <h2 id="books-heading" className={H2}>
        The Controlvar books
      </h2>
      <p className="mt-3 max-w-3xl leading-relaxed text-slate-600">
        Learn engineering, programming and the craft of shipping software through one real, open-source codebase:
        Controlvar itself. The books are for newcomers to a language or framework, engineers who already code, and
        contributors. They are licensed CC BY 4.0.
      </p>
      <ul role="list" className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {CORE_PATH.map((book) => (
          <li key={book.dir} className="min-w-0">
            <a
              href={bookUrl(book)}
              className={`flex h-full flex-col rounded-xl border border-slate-200 bg-white p-5 hover:border-blue-300 ${FOCUS}`}
            >
              <h3 className="font-semibold text-slate-900">{book.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-slate-600">{book.line}</p>
            </a>
          </li>
        ))}
      </ul>
      <p className="mt-6">
        {marketing ? (
          <Link href="/books" className={MORE_LINK}>
            {all}
          </Link>
        ) : (
          <a href={BOOKS_URL} className={MORE_LINK}>
            {all}
          </a>
        )}
      </p>
    </section>
  );
}
