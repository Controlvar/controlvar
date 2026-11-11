import React from 'react';
import { BOOKS_URL } from '@/content/site-links';
import { FOCUS, WIDTH } from '@/components/homepage/styles';

/**
 * The Controlvar books (#1859), just above the closing band and its "Get
 * started": where a reader who has seen the product and wants to learn from
 * it looks next. The words are the books' own README's (what they teach, who
 * they are for, the licence) and say no number of books, which the README and
 * the repository's description disagree on.
 */
export function Books() {
  return (
    <section aria-labelledby="books-heading" className={`${WIDTH} py-12 lg:py-16`}>
      <h2 id="books-heading" className="text-3xl font-semibold tracking-tight">
        The Controlvar books
      </h2>
      <p className="mt-3 max-w-3xl leading-relaxed text-slate-600">
        Learn engineering, programming and the craft of shipping software through one real, open-source codebase:
        Controlvar itself. The books are for newcomers to a language or framework, engineers who already code, and
        contributors. They are licensed CC BY 4.0.
      </p>
      <p className="mt-4">
        <a href={BOOKS_URL} className={`font-semibold text-blue-700 hover:text-blue-900 ${FOCUS}`}>
          Read the Controlvar books on GitHub <span aria-hidden="true">→</span>
        </a>
      </p>
    </section>
  );
}
