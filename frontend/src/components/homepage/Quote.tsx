import React from 'react';
import table from '@/content/home-quotes.json';
import { docsUrl } from '@/services/docs';

/**
 * A homepage sentence that quotes a docs page, rendered from
 * `content/home-quotes.json` (#1459 SPEC A).
 *
 * The words live in that table, not here, so the page and the two tests that
 * hold the words to docs/ (`tests/content/home-quotes.test.tsx` and its Python
 * reader, which runs on a docs-only pull request) read the same thing. To
 * change a quoted sentence, change its row, and the page it quotes if the page
 * is what moved.
 */

/**
 * The docs pages a quote may link, each a `docsUrl` call with a literal page.
 * `docs-links.test.ts` checks only such calls, and refuses any other, so a
 * link built from a row's `page` would be a link nothing checks. A quote that
 * links a page missing here does not render: it throws. (This comment names
 * no call of its own: the scan reads comments too.)
 */
const LINKS: Readonly<Record<string, string>> = {
  'self-hosting/aws-install': docsUrl('self-hosting/aws-install'),
};

type Part = string | { link: string; page: string };
const ROWS: ReadonlyArray<{ id: string; parts: Part[] }> = table.quotes;

export function Quote({ id }: { id: string }) {
  const row = ROWS.find((q) => q.id === id);
  if (!row) {
    throw new Error(`content/home-quotes.json has no quote with the id ${id}`);
  }
  return (
    <span data-quote={id}>
      {row.parts.map((part, i) => {
        if (typeof part === 'string') {
          return <React.Fragment key={i}>{part}</React.Fragment>;
        }
        if (!Object.prototype.hasOwnProperty.call(LINKS, part.page)) {
          throw new Error(`the quote ${id} links docs/${part.page}.md, which Quote's LINKS does not list`);
        }
        return (
          <a key={i} href={LINKS[part.page]} className="text-blue-700 underline">
            {part.link}
          </a>
        );
      })}
    </span>
  );
}
