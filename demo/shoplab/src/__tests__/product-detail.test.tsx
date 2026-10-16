import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import ProductDetailPage from '@/pages/products/[id]';
import { findProduct } from '@/data/products';
import { readCart } from '@/lib/cart';
import { __setExperiment } from '@/__mocks__/controlvar-sdk';
import { mockRouter } from '@/__mocks__/next-router';
import { callsFor, renderPage, resetTestState } from '@/test-utils';

describe('Product detail (shoplab_pdp_buy_button)', () => {
  beforeEach(() => {
    resetTestState();
    mockRouter.query = { id: 'p008' };
  });

  it('styles the buy button from the variant configuration (colour + text)', () => {
    __setExperiment('shoplab_pdp_buy_button', {
      variantName: 'orange_buy_now',
      isControl: false,
      configuration: { color: '#ea580c', text: 'Buy now' },
    });
    renderPage(<ProductDetailPage />);

    const button = screen.getByTestId('buy-button');
    expect(button).toHaveTextContent('Buy now');
    expect(button).toHaveStyle({ backgroundColor: '#ea580c' });
    expect(button).toHaveAttribute('data-variant', 'orange_buy_now');
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Stormshell Rain Jacket');
  });

  it('falls back to the control look when configuration is null', () => {
    __setExperiment('shoplab_pdp_buy_button', { configuration: null });
    renderPage(<ProductDetailPage />);
    const button = screen.getByTestId('buy-button');
    expect(button).toHaveTextContent('Add to cart');
    expect(button).toHaveStyle({ backgroundColor: '#0f172a' });
  });

  it('click fires add_to_cart with value = unit price and adds the item to the cart', () => {
    __setExperiment('shoplab_pdp_buy_button', {
      variantName: 'green_add',
      configuration: { color: '#16a34a', text: 'Add to cart' },
    });
    renderPage(<ProductDetailPage />);
    const product = findProduct('p008')!;

    fireEvent.change(screen.getByLabelText('Quantity'), { target: { value: '2' } });
    fireEvent.click(screen.getByTestId('buy-button'));

    const [call] = callsFor('add_to_cart');
    expect(call[1]).toEqual({ product_id: 'p008', quantity: 2, button: 'Add to cart' });
    expect(call[2]).toEqual({ value: product.price });
    expect(readCart()).toEqual([{ productId: 'p008', price: product.price, quantity: 2 }]);
    expect(screen.getByRole('status')).toHaveTextContent('Added to your cart');
    expect(callsFor('page_view')[0][1]).toEqual({ page: 'product' });
  });

  it('shows a not-found state for unknown ids', () => {
    mockRouter.query = { id: 'nope' };
    renderPage(<ProductDetailPage />);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Product not found');
  });
});
