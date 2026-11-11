/**
 * "How it works" (site polish 2026-11-10, #7).
 *
 * Step 2's code is two lines of the JavaScript guide, 77 and 101 characters
 * long. At 13 px its card's box holds about 33 characters on a 390 px phone
 * and 35 at 1440 px. The block scrolled sideways and showed about 37 of them,
 * cut mid-word, with no scrollbar on macOS or iOS; at 320 px step 1's one line
 * was cut too. Below 640 px the code is now 12 px: 26 characters on a 320 px
 * phone's first line, 24 on a continuation line.
 *
 * Now both blocks wrap, and they may break a line only at a space or right
 * after a `(`. CSS finds other places in a token: Chromium broke `'user-123'`
 * after its `-` at 360 px and `@controlvar/js-sdk` at 375 px (measured
 * 2026-11-10). So this test treats a break as possible after ANY character
 * that is not inside a `whitespace-nowrap` span. It passes only when every
 * character but the spaces is in one, each `<wbr>` follows a `(`, and no run
 * between two allowed breaks is longer than a 320 px phone's continuation
 * line.
 *
 * jsdom has no layout, so this checks what decides the layout. The browser
 * check is `tests/e2e/marketing-site.site.spec.ts`: no code block in the
 * section is wider than its box, and none breaks a line anywhere else, at 320,
 * 360, 375, 390 and 1440 px. The words are held to the guide by
 * `tests/content/home-quotes.test.tsx`.
 */
import React from 'react';
import { render, within } from '@testing-library/react';
import { HowItWorks } from '@/components/homepage/HowItWorks';
import { docsUrl } from '@/services/docs';
import quotes from '@/content/home-quotes.json';

/**
 * A 320 px phone's continuation line at 12 px: a 190 px box, less the 2ch
 * hanging indent, at 7.23 px a character (Chromium on macOS) is 24 characters.
 */
const MAX_UNBREAKABLE = 24;

const SNIPPET = (quotes.quotes.find((q) => q.id === 'sdk-snippet')?.code ?? []) as string[];

interface Walk {
  /** Runs of characters between two allowed breaks: a space, or a `<wbr>`. */
  runs: string[];
  /** Each whitespace-separated word with a character outside a `whitespace-nowrap` span. */
  unprotected: string[];
  /** The text before each `<wbr>` that does not end in `(`. */
  strayWbr: string[];
}

function walk(pre: Element): Walk {
  const runs: string[] = [];
  const unprotected = new Set<string>();
  const strayWbr: string[] = [];
  let run = '';
  let word = '';
  let wordUnprotected = false;
  const endWord = () => {
    if (word && wordUnprotected) unprotected.add(word);
    word = '';
    wordUnprotected = false;
  };
  const endRun = () => {
    if (run) runs.push(run);
    run = '';
  };
  const visit = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      const inSpan = (node.parentElement?.closest('.whitespace-nowrap') ?? null) !== null;
      for (const ch of node.textContent ?? '') {
        if (/\s/.test(ch)) {
          endRun();
          endWord();
          continue;
        }
        run += ch;
        word += ch;
        if (!inSpan) wordUnprotected = true;
      }
      return;
    }
    if ((node as Element).tagName === 'WBR') {
      if (!run.endsWith('(')) strayWbr.push(run);
      endRun();
      return;
    }
    node.childNodes.forEach(visit);
  };
  visit(pre);
  endRun();
  endWord();
  return { runs, unprotected: Array.from(unprotected), strayWbr };
}

describe('How it works', () => {
  const blocks = () => {
    const { container } = render(<HowItWorks />);
    return {
      steps: Array.from(container.querySelectorAll('ol > li')) as HTMLElement[],
      run: within(container).getByRole('region', { name: 'Run it' }),
      sdk: within(container).getByRole('region', { name: 'Add the SDK' }),
    };
  };

  it.each(['Run it', 'Add the SDK'])('wraps the %s block at 12 px below sm, 13 px from it', (name) => {
    const b = blocks();
    const pre = name === 'Run it' ? b.run : b.sdk;
    expect(pre.className.split(/\s+/)).toEqual(expect.arrayContaining(['whitespace-pre-wrap', 'text-[12px]', 'sm:text-[13px]']));
  });

  it.each(['Run it', 'Add the SDK'])('lets the %s block break only at a space or after a `(`', (name) => {
    const b = blocks();
    const w = walk(name === 'Run it' ? b.run : b.sdk);
    expect(w.runs.length).toBeGreaterThan(name === 'Run it' ? 4 : 10);
    expect({ unprotected: w.unprotected, strayWbr: w.strayWbr }).toEqual({ unprotected: [], strayWbr: [] });
    expect(w.runs.filter((r) => r.length > MAX_UNBREAKABLE)).toEqual([]);
  });

  it("keeps the hyphen in 'user-123' and in the package name inside one token", () => {
    const tokens = Array.from(blocks().sdk.querySelectorAll('.whitespace-nowrap')).map((s) => s.textContent);
    expect(tokens).toEqual(expect.arrayContaining(["'user-123',", '@controlvar/js-sdk']));
  });

  it('renders the same text, so a copy is the guide\'s lines', () => {
    const b = blocks();
    expect(b.run.textContent).toBe('docker compose up -d --wait');
    expect(b.sdk.textContent).toBe(['// npm install @controlvar/js-sdk', ...SNIPPET].join('\n'));
    expect(SNIPPET).toHaveLength(2);
  });

  it('indents the rest of a wrapped line under its start', () => {
    const b = blocks();
    const lines = Array.from(b.sdk.querySelectorAll('pre > span, [data-quote="sdk-snippet"] > span'));
    expect(lines).toHaveLength(3);
    for (const line of [...lines, b.run.querySelector('code > span') as Element]) {
      expect(line.className.split(/\s+/)).toEqual(expect.arrayContaining(['block', 'pl-[2ch]', '-indent-[2ch]']));
    }
  });

  it('ends step 3 with a link to how a results page is read', () => {
    const links = within(blocks().steps[2]).getAllByRole('link');
    expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
      ['How to read a results page, in the user guide →', docsUrl('guides/user-guide', 'reading-results')],
    ]);
  });
});
