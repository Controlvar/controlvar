/**
 * The not-found page (`pages/404.tsx`, the export's `404.html`; site polish
 * 2026-11-10, #4). Before it, the export's 404.html was Next's default page,
 * and controlvar.com answered an unknown path with the homepage.
 *
 * It says the page is not there and links home and to the docs hub, in both
 * builds: `/` and `/docs` are in every export. That it is in the marketing
 * export, with the site's footer, is `scripts/site-pages.test.mjs`; that the
 * shell gives it the footer is `marketing-site-audit.test.tsx`.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { isMarketingSite } from '@/utils/site-mode';
import NotFoundPage from '@/pages/404';

jest.mock('@/utils/site-mode', () => ({
  ...jest.requireActual('@/utils/site-mode'),
  isMarketingSite: jest.fn(),
}));
jest.mock('next/head', () => { const H=({children}:{children:React.ReactNode})=><>{children}</>; H.displayName='H'; return H; });

const mode = isMarketingSite as jest.Mock;

describe.each([
  ['marketing build', true],
  ['platform build', false],
])('the not-found page, %s', (_name, marketing) => {
  beforeEach(() => mode.mockReset().mockReturnValue(marketing));

  it('says the page is not there', () => {
    render(<NotFoundPage />);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Page not found');
    expect(document.querySelector('title')?.textContent).toBe('Page not found · Controlvar');
  });

  it('links home and to the docs hub, and nowhere else', () => {
    render(<NotFoundPage />);
    const links = screen.getAllByRole('link').map((a) => [a.textContent, a.getAttribute('href')]);
    expect(links).toEqual([
      ['Go to the homepage', '/'],
      ['Read the documentation', '/docs'],
    ]);
  });
});
