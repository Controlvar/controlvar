/**
 * The link-preview tags `PageTitle` writes (#1458).
 *
 * The marketing build's own HTML is checked by `scripts/site-head.test.mjs`
 * in the Frontend Tests marketing step; this is the same rule at the
 * component, in both builds: the tags on a marketing page that gives its path
 * and description, and none on a self-hosted dashboard.
 */
import React from 'react';
import { cleanup, render } from '@testing-library/react';
import { isMarketingSite } from '@/utils/site-mode';
import { PageTitle } from '@/components/PageTitle';

jest.mock('@/utils/site-mode', () => ({
  ...jest.requireActual('@/utils/site-mode'),
  isMarketingSite: jest.fn(),
}));
// Rendered in place; React 19 then hoists each <meta> into document.head.
jest.mock('next/head', () => {
  const H = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  H.displayName = 'Head';
  return H;
});

const mode = isMarketingSite as jest.Mock;

/** Every og:/twitter: tag in the document, as {key: content}. */
function shareTags(): Record<string, string> {
  const tags: Record<string, string> = {};
  document.querySelectorAll('meta').forEach((m) => {
    const key = m.getAttribute('property') ?? m.getAttribute('name') ?? '';
    if (key.startsWith('og:') || key.startsWith('twitter:')) tags[key] = m.getAttribute('content') ?? '';
  });
  return tags;
}

const DESCRIPTION = 'Pre-experiment statistical power calculator.';

describe('PageTitle link-preview tags', () => {
  beforeEach(() => mode.mockReset());

  it('writes the Open Graph and Twitter tags on a marketing page', () => {
    mode.mockReturnValue(true);
    render(<PageTitle title="Power Calculator" path="/power-calculator" description={DESCRIPTION} />);
    expect(shareTags()).toEqual({
      'og:type': 'website',
      'og:title': 'Power Calculator · Controlvar',
      'og:description': DESCRIPTION,
      'og:url': 'https://controlvar.com/power-calculator',
      'og:image': 'https://controlvar.com/og.png',
      'og:image:width': '1200',
      'og:image:height': '630',
      'twitter:card': 'summary_large_image',
      'twitter:title': 'Power Calculator · Controlvar',
      'twitter:description': DESCRIPTION,
      'twitter:image': 'https://controlvar.com/og.png',
    });
  });

  it('titles the homepage with the bare name and its URL with a slash', () => {
    mode.mockReturnValue(true);
    render(<PageTitle path="/" description={DESCRIPTION} />);
    const tags = shareTags();
    expect(tags['og:title']).toBe('Controlvar');
    expect(tags['og:url']).toBe('https://controlvar.com/');
  });

  it('writes none on the self-hosted dashboard', () => {
    mode.mockReturnValue(false);
    render(<PageTitle title="Power Calculator" path="/power-calculator" description={DESCRIPTION} />);
    expect(shareTags()).toEqual({});
    expect(document.querySelector('meta[name="description"]')).toHaveAttribute('content', DESCRIPTION);
  });

  it.each([
    ['no path or description', { title: 'Experiments' }],
    ['no path', { title: 'Docs', description: DESCRIPTION }],
    ['no description', { title: 'Docs', path: '/docs' }],
  ])('writes none on a page with %s', (_name, props) => {
    mode.mockReturnValue(true);
    render(<PageTitle {...props} />);
    expect(shareTags()).toEqual({});
  });

  it('leaves nothing behind when the page goes', () => {
    mode.mockReturnValue(true);
    render(<PageTitle path="/" description={DESCRIPTION} />);
    expect(Object.keys(shareTags())).not.toHaveLength(0);
    cleanup();
    expect(shareTags()).toEqual({});
  });
});
