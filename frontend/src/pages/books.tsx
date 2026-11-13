import React from 'react';
import { PageTitle } from '@/components/PageTitle';
import { BOOKS_LICENCE_URL, BOOKS_URL } from '@/content/site-links';
import { BOOK_GROUPS, BOOK_READERS, bookUrl } from '@/content/books';
import { marketingOnly } from '@/utils/marketing-only';
import { FOCUS, H1, H2, MORE_LINK, WIDTH } from '@/components/homepage/styles';

/** The marketing build only: a platform build writes no /books page (`utils/marketing-only.ts`). */
export const getStaticProps = marketingOnly;

/**
 * The Controlvar books (#1859; site design 2026-11-11): what they teach, who
 * they are for and their licence, then every book in its README's groups,
 * each linking its own README. The words are the books' README's
 * (`content/books.ts`), and the page says no number of books.
 *
 * `/books` is an `open` route (`utils/routes.ts`), so it sits in the shell:
 * the public site's header and footer.
 */
export default function BooksPage() {
  return (
    <>
      <PageTitle
        title="The Controlvar books"
        path="/books"
        description="Engineering and programming books that use Controlvar's own code for their examples. The books are licensed CC BY 4.0."
      />
      <div className="flex-1 bg-white">
        <div className={`${WIDTH} py-14 lg:py-16`}>
          <h1 className={H1}>The Controlvar books</h1>
          <p className="mt-5 max-w-3xl text-lg leading-relaxed text-slate-600">
            Engineering and programming books that use Controlvar&apos;s own code for their examples. Every concept is taught twice: once in general terms, and once in Controlvar&apos;s own
            source, which you can clone, run and break on your own machine.
          </p>

          {/* Labels, not headings: the page's h2s are its groups of books, on the site's one scale. */}
          <div className="mt-10 grid gap-10 lg:grid-cols-2">
            <div className="min-w-0">
              <p id="readers-label" className="font-semibold text-slate-900">
                Who they are for
              </p>
              <ul role="list" aria-labelledby="readers-label" className="mt-3 flex flex-col gap-2.5 leading-relaxed text-slate-700">
                {BOOK_READERS.map((reader) => (
                  <li key={reader} className="flex gap-2.5">
                    <span aria-hidden="true" className="mt-2.5 h-1.5 w-1.5 flex-none rounded-full bg-blue-600" />
                    <span>{reader}</span>
                  </li>
                ))}
              </ul>
            </div>
            <div className="min-w-0">
              <p className="font-semibold text-slate-900">Licence</p>
              {/* The books' README, "Licence": its conditions in its words (books-page.test.tsx). */}
              <p className="mt-3 leading-relaxed text-slate-700">
                The books are licensed{' '}
                <a href={BOOKS_LICENCE_URL} className={`text-blue-700 underline hover:text-blue-900 ${FOCUS}`}>
                  CC BY 4.0
                </a>
                . You may share and adapt them for any purpose, including commercially, as long as you give credit
                (&ldquo;The Controlvar Books, The Controlvar authors&rdquo;), link to the licence and say what you
                changed. Code quoted from Controlvar keeps its own licences, Apache-2.0 and MIT.
              </p>
              <p className="mt-4">
                <a href={BOOKS_URL} className={MORE_LINK}>
                  The books on GitHub <span aria-hidden="true">→</span>
                </a>
              </p>
            </div>
          </div>

          {BOOK_GROUPS.map((group) => (
            <section key={group.id} aria-labelledby={`${group.id}-heading`} className="mt-14">
              <h2 id={`${group.id}-heading`} className={H2}>
                {group.label}
              </h2>
              <p className="mt-2 text-slate-600">{group.when}</p>
              <ul role="list" className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                {group.books.map((book) => (
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
            </section>
          ))}
        </div>
      </div>
    </>
  );
}
