import React from 'react';
import { fireEvent, screen, within } from '@testing-library/react';
import SearchPage from '@/pages/search';
import { PRODUCTS } from '@/data/products';
import { exactSearch, fuzzySearch, highlightSegments } from '@/lib/search';
import { __setFlag } from '@/__mocks__/controlvar-sdk';
import { callsFor, renderPage, resetTestState } from '@/test-utils';

function submitSearch(query: string) {
  fireEvent.change(screen.getByLabelText('Search products'), { target: { value: query } });
  fireEvent.submit(screen.getByRole('search'));
}

describe('search engines (pure)', () => {
  it('exact matches product names only; fuzzy tolerates typos and searches descriptions', () => {
    expect(exactSearch(PRODUCTS, 'jacket').map((h) => h.product.id)).toEqual(['p008']);
    expect(exactSearch(PRODUCTS, 'jaket')).toEqual([]);
    expect(exactSearch(PRODUCTS, 'waterproof')).toEqual([]); // only in a description

    expect(fuzzySearch(PRODUCTS, 'jaket').map((h) => h.product.id)).toEqual(['p008']);
    expect(fuzzySearch(PRODUCTS, 'waterproof').map((h) => h.product.id)).toEqual(['p006']);
    expect(fuzzySearch(PRODUCTS, 'footwear').map((h) => h.product.id).sort()).toEqual(['p004', 'p005', 'p006']);
    expect(fuzzySearch(PRODUCTS, 'purple unicorn')).toEqual([]);
    expect(highlightSegments('Stormshell Rain Jacket', ['jacket'])).toEqual([
      { text: 'Stormshell Rain ', hit: false },
      { text: 'Jacket', hit: true },
    ]);
  });
});

describe('Search page (shoplab_new_search flag)', () => {
  beforeEach(resetTestState);

  it('flag off: legacy exact engine, no badge, typo finds nothing', () => {
    __setFlag('shoplab_new_search', { isEnabled: false });
    renderPage(<SearchPage />);

    expect(screen.queryByTestId('new-search-badge')).not.toBeInTheDocument();
    expect(screen.getByTestId('search-engine')).toHaveAttribute('data-engine', 'exact');

    submitSearch('jaket');
    expect(screen.getByTestId('search-summary')).toHaveTextContent('0 results');
    submitSearch('jacket');
    expect(screen.getByTestId('search-summary')).toHaveTextContent('1 result');
    expect(document.querySelectorAll('mark')).toHaveLength(0);
  });

  it('flag on: fuzzy engine with badge and highlighted terms', () => {
    __setFlag('shoplab_new_search', { isEnabled: true, variant: 'on' });
    renderPage(<SearchPage />);

    expect(screen.getByTestId('new-search-badge')).toHaveTextContent('New search');
    expect(screen.getByTestId('search-engine')).toHaveAttribute('data-engine', 'fuzzy');

    submitSearch('jaket');
    expect(screen.getByTestId('search-summary')).toHaveTextContent('1 result');
    const card = screen.getByTestId('product-card');
    expect(within(card).getAllByText('Jacket', { selector: 'mark' }).length).toBeGreaterThan(0);
  });

  it('fires search with {query, results, engine} attached to the flag, and product_click on result click', () => {
    __setFlag('shoplab_new_search', { isEnabled: true, variant: 'on' });
    renderPage(<SearchPage />);
    expect(callsFor('page_view')[0][1]).toEqual({ page: 'search' });

    submitSearch('boots');
    const [searchCall] = callsFor('search');
    expect(searchCall[1]).toEqual({ query: 'boots', results: 1, engine: 'fuzzy' });
    expect(searchCall[2]).toEqual({ featureFlagKey: 'shoplab_new_search' });

    fireEvent.click(screen.getByTestId('product-card').querySelector('a') as HTMLAnchorElement);
    const [click] = callsFor('product_click');
    expect(click[1]).toEqual({ product_id: 'p006', position: 1, sort: 'search' });
  });

  it('flag off: the search event reports the exact engine', () => {
    __setFlag('shoplab_new_search', { isEnabled: false });
    renderPage(<SearchPage />);
    submitSearch('Tent');
    expect(callsFor('search')[0][1]).toEqual({ query: 'Tent', results: 1, engine: 'exact' });
    expect(callsFor('search')[0][2]).toEqual({ featureFlagKey: 'shoplab_new_search' });
  });
});
