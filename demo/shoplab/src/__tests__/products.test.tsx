import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import ProductListPage from '@/pages/products/index';
import { PRODUCTS } from '@/data/products';
import { sortByMl, sortByPriceAsc, sortByRelevance } from '@/lib/sorting';
import { __setExperiment, DEFAULT_TEST_USER } from '@/__mocks__/controlvar-sdk';
import { callsFor, renderPage, resetTestState } from '@/test-utils';

function renderedIds(): string[] {
  return screen.getAllByTestId('product-card').map((el) => el.getAttribute('data-product-id') as string);
}

describe('Product list (shoplab_plp_sort)', () => {
  beforeEach(resetTestState);

  it('sorts by relevance for the control variant (and when configuration is missing)', () => {
    __setExperiment('shoplab_plp_sort', { variantName: 'relevance', configuration: { algorithm: 'relevance' } });
    renderPage(<ProductListPage />);
    expect(renderedIds()).toEqual(sortByRelevance(PRODUCTS).map((p) => p.id));
    expect(screen.getByTestId('sort-label')).toHaveAttribute('data-sort', 'relevance');
  });

  it('sorts by price for price_low_high', () => {
    __setExperiment('shoplab_plp_sort', { variantName: 'price_low_high', configuration: { algorithm: 'price_asc' } });
    renderPage(<ProductListPage />);
    expect(renderedIds()).toEqual(sortByPriceAsc(PRODUCTS).map((p) => p.id));
    expect(screen.getByTestId('sort-label')).toHaveTextContent('Price: low to high');
  });

  it('uses the deterministic per-visitor ML order for ml_personalized', () => {
    __setExperiment('shoplab_plp_sort', { variantName: 'ml_personalized', configuration: { algorithm: 'ml' } });
    renderPage(<ProductListPage />);
    expect(renderedIds()).toEqual(sortByMl(PRODUCTS, DEFAULT_TEST_USER.userId).map((p) => p.id));
    expect(screen.getByTestId('sort-label')).toHaveTextContent('Recommended for you');
  });

  it('fires page_view on mount and product_click with position + sort on click', () => {
    __setExperiment('shoplab_plp_sort', { variantName: 'price_low_high', configuration: { algorithm: 'price_asc' } });
    renderPage(<ProductListPage />);
    expect(callsFor('page_view')[0][1]).toEqual({ page: 'products' });

    const expected = sortByPriceAsc(PRODUCTS);
    const third = screen.getAllByTestId('product-card')[2];
    fireEvent.click(third.querySelector('a') as HTMLAnchorElement);

    const [call] = callsFor('product_click');
    expect(call[1]).toEqual({ product_id: expected[2].id, position: 3, sort: 'price_asc' });
    expect(call[2]).toBeUndefined();
  });
});
