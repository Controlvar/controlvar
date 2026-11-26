/**
 * The Controlvar logo on the public site's header and footer (#2040).
 *
 * `Wordmark` draws the logo on the marketing build and the old "C" and the word
 * everywhere else: the dashboard, the login page and the platform build's `/`
 * keep today's output, byte for byte.
 *
 * THE PINS. The brand's rules are that the lockup below 48 px uses the small
 * mark's bars, that the wordmark is outlined glyphs, and that the bars are not
 * recoloured or swapped. A copy that drifted (the large mark's bars, the old
 * "C", another colour) would still render and still pass an accessibility
 * check, so the drawing is pinned by hash: the glyph path's data, and the three
 * elements of the small mark (the tile and its two bars) written as the brand's
 * own file writes them. The two expected hashes are literals because the brand
 * files are not in this repository:
 *   - GLYPH_PATH_SHA256: the data of the one path of the wordmark, which the
 *     lockup and the standalone wordmark share;
 *   - SMALL_MARK_SHA256: the small mark's three elements, from its first
 *     `<rect` to its last `/>`, with no title and no wrapper.
 * They are not the hashes of the master lockup or the master mark, which draw
 * the large mark's bars.
 *
 * jsdom measures no size and no colour; the first-screen height, contrast and
 * focus ring are the site spec's, over the marketing export.
 */
import { createHash } from 'crypto';
import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import axe from 'axe-core';
import { LogoLockup } from '@/components/LogoLockup';
import { SiteHeader } from '@/components/SiteHeader';
import { HomeFooter } from '@/components/homepage/HomeFooter';
import { Wordmark } from '@/components/Wordmark';
import { isMarketingSite } from '@/utils/site-mode';

const GLYPH_PATH_SHA256 = 'bc25222ef9a956ba19743b522a81f723efbf926b408ea27b8edccca98b332277';
const SMALL_MARK_SHA256 = '90d66743b66107dab7ad5516fd983426e84cfb586a9f5c7ee5ed923bf3e37682';

jest.mock('@/utils/site-mode', () => ({
  ...jest.requireActual('@/utils/site-mode'),
  isMarketingSite: jest.fn(),
}));
jest.mock('next/head', () => {
  const H = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  H.displayName = 'Head';
  return H;
});
let mockPathname = '/';
jest.mock('next/router', () => ({
  useRouter: () => ({
    pathname: mockPathname,
    asPath: mockPathname,
    query: {},
    push: jest.fn(),
    replace: jest.fn(),
    isReady: true,
  }),
}));

const mode = isMarketingSite as jest.Mock;
const sha256 = (text: string) => createHash('sha256').update(text).digest('hex');

beforeEach(() => {
  mockPathname = '/';
  mode.mockReset().mockReturnValue(true);
});

/** An element written the way the brand's SVG files write it: attributes in order, self-closed. */
const asWritten = (el: Element) =>
  `<${el.tagName.toLowerCase()}${Array.from(el.attributes)
    .map((a) => ` ${a.name}="${a.value}"`)
    .join('')}/>`;

describe('the drawing', () => {
  it('is the small mark and the outlined wordmark of the brand, by hash', () => {
    const { container } = render(<LogoLockup decorative={false} />);
    const svg = container.querySelector('svg') as SVGElement;
    const rects = Array.from(svg.querySelectorAll('rect'));
    expect(rects).toHaveLength(3);
    expect(sha256(rects.map(asWritten).join(''))).toBe(SMALL_MARK_SHA256);
    const paths = svg.querySelectorAll('path');
    expect(paths).toHaveLength(1);
    expect(sha256(paths[0].getAttribute('d') ?? '')).toBe(GLYPH_PATH_SHA256);
    // The ink of the wordmark on a light background; the tile and the bars are in the hash.
    expect(paths[0].getAttribute('fill')).toBe('#0f172a');
  });

  // The hashes cover the values; this covers what else could change the picture: an opacity, a
  // transform (it would move the gap), a preserveAspectRatio, a class that sets a fill. Only the
  // forced-colours rule may sit on the wordmark.
  it('has no other element and no other attribute than these', () => {
    const attrs = (el: Element) => Array.from(el.attributes).map((a) => a.name).sort();
    const named = render(<LogoLockup decorative={false} />).container.querySelector('svg') as SVGElement;
    expect(Array.from(named.children).map((c) => c.tagName)).toEqual(['rect', 'rect', 'rect', 'path']);
    expect(attrs(named)).toEqual(['aria-label', 'class', 'height', 'role', 'viewBox', 'width', 'xmlns']);
    cleanup();
    const hidden = render(<LogoLockup decorative />).container.querySelector('svg') as SVGElement;
    expect(attrs(hidden)).toEqual(['aria-hidden', 'class', 'focusable', 'height', 'viewBox', 'width', 'xmlns']);
    expect(attrs(named.querySelector('path') as Element)).toEqual(['class', 'd', 'fill']);
    expect(named.querySelector('path')?.getAttribute('class')).toBe('forced-colors:fill-[CanvasText]');
    expect(Array.from(named.querySelectorAll('rect')).map(attrs)).toEqual([
      ['fill', 'height', 'rx', 'width'],
      ['fill', 'height', 'rx', 'stroke', 'stroke-width', 'width', 'x', 'y'],
      ['fill', 'height', 'rx', 'width', 'x', 'y'],
    ]);
    expect(named.getAttribute('class')).toBe('block shrink-0');
  });

  it('is the lockup, 28 px tall: 273 by 64 units shown at a 28 px tile, about 119 px wide', () => {
    const { container } = render(<LogoLockup decorative={false} />);
    const svg = container.querySelector('svg') as SVGElement;
    expect(svg.getAttribute('viewBox')).toBe('0 0 273 64');
    expect(svg.getAttribute('height')).toBe('28');
    expect(Number(svg.getAttribute('width'))).toBeCloseTo((273 * 28) / 64, 1);
    // Text would fall back to the system font: the glyphs are outlines.
    expect(svg.querySelector('text')).toBeNull();
  });
});

describe('its name', () => {
  it('is an image called "Controlvar" where it is not a link, and has no title of its own', () => {
    const { container } = render(<LogoLockup decorative={false} />);
    const svg = container.querySelector('svg') as SVGElement;
    expect(svg).toHaveAttribute('role', 'img');
    expect(svg).not.toHaveAttribute('aria-hidden');
    expect(container.querySelector('title')).toBeNull();
    expect(screen.getByRole('img', { name: 'Controlvar' })).toBe(svg);
  });

  it('is hidden where a link names it, and has no title there either', () => {
    const { container } = render(<LogoLockup decorative />);
    const svg = container.querySelector('svg') as SVGElement;
    expect(svg).toHaveAttribute('aria-hidden', 'true');
    expect(svg).toHaveAttribute('focusable', 'false');
    expect(svg).not.toHaveAttribute('role');
    expect(svg).not.toHaveAttribute('aria-label');
    expect(container.querySelector('title')).toBeNull();
    expect(screen.queryByRole('img')).toBeNull();
  });
});

describe('Wordmark', () => {
  it('on the marketing build, linked: the link is "Controlvar home" and nothing else names it', () => {
    const { container } = render(<Wordmark href="/" />);
    const link = screen.getByRole('link', { name: 'Controlvar home' });
    expect(link.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
    expect(link.textContent).toBe('');
    expect(container.querySelector('title')).toBeNull();
    expect(screen.queryByRole('img')).toBeNull();
  });

  it('on the marketing build, unlinked: one image called "Controlvar" and no text beside it', () => {
    const { container } = render(<Wordmark href={null} />);
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.getAllByRole('img')).toHaveLength(1);
    expect(screen.getByRole('img', { name: 'Controlvar' })).toBeInTheDocument();
    expect(container.textContent).toBe('');
  });

  it('on any other build, is the "C" and the word, as the dashboard has always had it', () => {
    mode.mockReturnValue(false);
    const { container } = render(<Wordmark href={null} className="x" />);
    expect(container.innerHTML).toBe(
      '<span class="inline-flex items-center gap-2 x">' +
        '<span aria-hidden="true" class="inline-flex h-7 w-7 items-center justify-center rounded-md bg-blue-600 text-sm font-bold text-white">C</span>' +
        '<span class="text-lg font-semibold tracking-tight text-slate-900">Controlvar</span>' +
        '</span>',
    );
    expect(container.querySelector('svg')).toBeNull();
    cleanup();
    render(<Wordmark />);
    expect(screen.getByRole('link', { name: 'Controlvar home' })).toHaveTextContent(/^CControlvar home$/);
    expect(document.querySelector('svg')).toBeNull();
  });
});

describe('where the site shows it', () => {
  const axeResult = (node: Element, rules: string[]) =>
    axe.run(node as HTMLElement, { runOnly: { type: 'rule', values: rules }, rules: { 'color-contrast': { enabled: false } } });

  it('has no axe violation on the home page: header and footer, each an image named once', async () => {
    mockPathname = '/';
    const { container } = render(
      <>
        <SiteHeader />
        <HomeFooter />
      </>,
    );
    expect(screen.getAllByRole('img', { name: 'Controlvar' })).toHaveLength(2);
    const result = await axeResult(container, ['svg-img-alt', 'role-img-alt', 'aria-hidden-focus', 'link-name', 'aria-allowed-attr']);
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(' | ')}`)).toEqual([]);
    // svg-img-alt ran against the logos, so a violation was possible.
    expect(result.passes.map((p) => p.id)).toEqual(expect.arrayContaining(['svg-img-alt']));
  });

  it('has no axe violation on another page, where the header links home', async () => {
    mockPathname = '/docs';
    const { container } = render(<SiteHeader />);
    expect(screen.getByRole('link', { name: 'Controlvar home' })).toBeInTheDocument();
    const result = await axeResult(container, ['svg-img-alt', 'role-img-alt', 'aria-hidden-focus', 'link-name', 'aria-allowed-attr']);
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(' | ')}`)).toEqual([]);
    expect(result.passes.map((p) => p.id)).toEqual(expect.arrayContaining(['link-name']));
  });
});
