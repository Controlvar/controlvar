import { BOOKS_URL } from '@/content/site-links';

/**
 * The Controlvar books (#1859), as their own README lists them
 * (amarkanday/controlvar-books, "The library"): each book's title, one line
 * from that README's entry for it, and the link to the book's own README.
 * The /books page lists all of them in the README's groups; the homepage's
 * books band shows the core path and links /books.
 *
 * The words are the README's, cut to one line, and say no number of books:
 * the README and the repository's description disagree on it ("Nine books" in
 * the README over a list of ten, "Eight books" in the description), and that
 * is the books repository's to fix. The group labels are the README's own,
 * split at their comma into the group's name and when to read it.
 * Each link is a file of the books repository at `main`, under BOOKS_URL, so
 * `tests/pages/books-page.test.tsx` can hold every one to BOOKS_URL.
 */

export interface Book {
  /** The book's folder in the books repository. */
  dir: string;
  title: string;
  line: string;
}

export interface BookGroup {
  id: string;
  /** The README's group label, before its comma. */
  label: string;
  /** The rest of that label: when to read the group. */
  when: string;
  books: readonly Book[];
}

/** The book's README in the books repository. */
export const bookUrl = (book: Book): string => `${BOOKS_URL}/blob/main/${book.dir}/README.md`;

export const BOOK_GROUPS: readonly BookGroup[] = [
  {
    id: 'core-path',
    label: 'Core path',
    when: 'In order.',
    books: [
      {
        dir: 'learn-python-in-24-hours',
        title: 'Learn Python in 24 Hours',
        line: 'Python from absolute zero to the modern, typed Python the backend is written in.',
      },
      {
        dir: 'learn-fastapi-in-24-hours',
        title: 'Learn FastAPI in 24 Hours',
        line: 'The backend framework layer: FastAPI, Pydantic v2, SQLAlchemy 2, Alembic and the background schedulers.',
      },
      {
        dir: 'learn-react-nextjs-in-24-hours',
        title: 'Learn TypeScript, React & Next.js in 24 Hours',
        line: 'TypeScript, React and Next.js through the real dashboard.',
      },
      {
        dir: 'build-a-platform-in-28-days',
        title: 'Build an Experimentation Platform in 28 Days',
        line: 'The whole architecture end to end; after this capstone you can read and extend the codebase.',
      },
    ],
  },
  {
    id: 'specializations',
    label: 'Specializations',
    when: 'After the core path, in any order.',
    books: [
      {
        dir: 'experimentation-statistics',
        title: 'Experimentation Statistics',
        line: 'The mathematics the product is built on, and how the code implements it.',
      },
      {
        dir: 'sdk-engineering',
        title: 'SDK Engineering',
        line: 'One HTTP contract, many SDKs, and proving that every implementation agrees.',
      },
      {
        dir: 'platform-operations',
        title: 'Platform Operations',
        line: 'Running it: Compose, the CDK stacks, logging, metrics, deploys, migrations and releases.',
      },
      {
        dir: 'production-engineering',
        title: 'Production Engineering',
        line: 'Decisions, reliability and safe change.',
      },
    ],
  },
  {
    id: 'first-change',
    label: 'Before your first change',
    when: 'At any point.',
    books: [
      {
        dir: 'before-you-change-controlvar',
        title: 'Before You Change Controlvar',
        line: 'A short guide for someone about to make a first change.',
      },
    ],
  },
  {
    id: 'ai-tools',
    label: 'Working with AI coding tools',
    when: 'Alongside any of the others.',
    books: [
      {
        dir: 'using-ai-tools-effectively',
        title: 'Using AI Tools Effectively',
        line: 'Getting work you can trust from AI coding agents, taught through how Controlvar is built.',
      },
    ],
  },
];

/** Every book, in the README's order. */
export const BOOKS: readonly Book[] = BOOK_GROUPS.flatMap((group) => group.books);

/** The core path: the homepage's books band. */
export const CORE_PATH: readonly Book[] = BOOK_GROUPS[0].books;

/** "Who these books are for", from the README, one line each. */
export const BOOK_READERS: readonly string[] = [
  'Newcomers to a language or framework (Python, FastAPI, TypeScript, React, Next.js) who want to learn it the way it is used in production.',
  'Engineers who already code and want the next layer: data modelling and migrations, access control, testing strategy, release engineering, and the statistics behind experimentation.',
  'Contributors to Controlvar who want to understand why the code is shaped the way it is before changing it.',
];
