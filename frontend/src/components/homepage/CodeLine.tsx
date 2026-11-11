import React from 'react';

/**
 * One line of a code block that wraps (How it works; site polish 2026-11-10,
 * #7), for a `<pre>` with `whitespace-pre-wrap` to hold.
 *
 * The browser may break the line in three places only: at a space, right
 * after a `(`, and right after a `/` of a URL (a token starting `https://`,
 * past its `//`). Every other run of characters is one `whitespace-nowrap`
 * span, so the soft wrap opportunities CSS finds inside a token are not taken.
 * With `whitespace-pre-wrap` alone, Chromium broke `'user-123'` after its `-`
 * at 360 px and `@controlvar/js-sdk` after its `-` at 375 px (13 px code). A
 * `<wbr>` after each `(` lets a long call break after its opening parenthesis.
 * The URL's breaks are for step 1's clone command (site design 2026-11-11):
 * `https://github.com/Controlvar/controlvar.git` is 44 characters, wider than
 * a phone's line at any legible size, so it breaks after `github.com/` or
 * `Controlvar/` and nowhere else. Spans and `<wbr>` add no text: the line's
 * `textContent`, and what a reader copies, is `text`.
 *
 * The line is a block with a hanging indent, so a wrapped line's rest sits
 * under its start and reads as one statement. Every line but the last ends in
 * `\n` (inside the block, so no blank line appears), which keeps a block's
 * `textContent` its lines joined by newlines.
 *
 * A token wider than the line is not broken: the block's `overflow-x-auto`
 * scrolls it instead. In How it works none is meant to be, from 320 px up
 * (measured in Chromium on macOS, 2026-11-10): the widest token was 24
 * characters, and a 320 px phone's continuation line holds 24 at 12 px. The
 * clone URL's widest piece is `https://github.com/`, 19.
 */
export function CodeLine({ text, last = false, className = '' }: { text: string; last?: boolean; className?: string }) {
  return (
    <span className={`block pl-[2ch] -indent-[2ch]${className ? ` ${className}` : ''}`}>
      {tokens(text)}
      {last ? null : '\n'}
    </span>
  );
}

/** Where a token may break: after each `(`, and after each `/` of a URL past its `//`. */
export function breakPieces(token: string): string[] {
  if (token.startsWith('https://')) {
    const rest = token.slice('https://'.length).split('/');
    return rest.map((piece, i) => {
      const head = i === 0 ? `https://${piece}` : piece;
      return i === rest.length - 1 ? head : `${head}/`;
    });
  }
  const pieces = token.split('(');
  return pieces.map((piece, i) => (i === pieces.length - 1 ? piece : `${piece}(`));
}

/** `text` as spaces, `whitespace-nowrap` tokens, and a `<wbr>` at each break of `breakPieces`. */
export function tokens(text: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  text.split(/( +)/).forEach((part, i) => {
    if (part === '') return;
    if (part[0] === ' ') {
      out.push(part);
      return;
    }
    const pieces = breakPieces(part);
    pieces.forEach((word, j) => {
      const end = j === pieces.length - 1;
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
