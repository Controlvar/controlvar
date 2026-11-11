import React from 'react';

/**
 * One line of a code block that wraps (How it works; site polish 2026-11-10,
 * #7), for a `<pre>` with `whitespace-pre-wrap` to hold.
 *
 * The browser may break the line in two places only: at a space, and right
 * after a `(`. Every other run of characters is one `whitespace-nowrap` span,
 * so the soft wrap opportunities CSS finds inside a token are not taken. With
 * `whitespace-pre-wrap` alone, Chromium broke `'user-123'` after its `-` at
 * 360 px and `@controlvar/js-sdk` after its `-` at 375 px (13 px code). A `<wbr>` after each `(` lets a long call break
 * after its opening parenthesis. Spans and `<wbr>` add no text: the line's
 * `textContent`, and what a reader copies, is `text`.
 *
 * The line is a block with a hanging indent, so a wrapped line's rest sits
 * under its start and reads as one statement. Every line but the last ends in
 * `\n` (inside the block, so no blank line appears), which keeps a block's
 * `textContent` its lines joined by newlines.
 *
 * A token wider than the line is not broken: the block's `overflow-x-auto`
 * scrolls it instead. In How it works none is, from 320 px up (measured in
 * Chromium on macOS, 2026-11-10): the widest token is 24 characters, and a
 * 320 px phone's continuation line holds 24 at 12 px.
 */
export function CodeLine({ text, last = false, className = '' }: { text: string; last?: boolean; className?: string }) {
  return (
    <span className={`block pl-[2ch] -indent-[2ch]${className ? ` ${className}` : ''}`}>
      {tokens(text)}
      {last ? null : '\n'}
    </span>
  );
}

/** `text` as spaces, `whitespace-nowrap` tokens, and a `<wbr>` after each `(`. */
export function tokens(text: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  text.split(/( +)/).forEach((part, i) => {
    if (part === '') return;
    if (part[0] === ' ') {
      out.push(part);
      return;
    }
    const pieces = part.split('(');
    pieces.forEach((piece, j) => {
      const end = j === pieces.length - 1;
      const word = end ? piece : `${piece}(`;
      if (word !== '') {
        out.push(
          <span key={`${i}.${j}`} className="whitespace-nowrap">
            {word}
          </span>,
        );
      }
      if (!end) out.push(<wbr key={`${i}.${j}.wbr`} />);
    });
  });
  return out;
}
