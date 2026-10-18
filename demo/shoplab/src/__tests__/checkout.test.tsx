import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import CheckoutPage, { flowFromConfiguration } from '@/pages/checkout';
import { readCart, writeCart } from '@/lib/cart';
import { __setExperiment, trackEventMock } from '@/__mocks__/controlvar-sdk';
import { callsFor, renderPage, resetTestState } from '@/test-utils';

const CART = [
  { productId: 'p001', price: 129, quantity: 1 },
  { productId: 'p012', price: 29, quantity: 2 },
];
const CART_TOTAL = 129 + 29 * 2; // 187

describe('Checkout (shoplab_checkout_flow)', () => {
  beforeEach(() => {
    resetTestState();
    writeCart(CART);
  });

  it('maps configuration to a flow', () => {
    expect(flowFromConfiguration({ steps: 3 })).toBe('standard');
    expect(flowFromConfiguration({ steps: 1 })).toBe('one_page');
    expect(flowFromConfiguration(null)).toBe('standard');
  });

  it('standard variant: three-step stepper, purchase fired only on the last step', () => {
    __setExperiment('shoplab_checkout_flow', { variantName: 'standard', configuration: { steps: 3 } });
    renderPage(<CheckoutPage />);

    expect(screen.getByTestId('checkout-standard')).toBeInTheDocument();
    expect(screen.queryByTestId('checkout-one-page')).not.toBeInTheDocument();
    const steps = screen.getByRole('list', { name: 'Checkout steps' });
    expect(steps).toHaveTextContent('Address');
    expect(steps).toHaveTextContent('Shipping');
    expect(steps).toHaveTextContent('Payment');

    // begin_checkout on load, with the cart total.
    const [begin] = callsFor('begin_checkout');
    expect(begin[1]).toEqual({ items: 3, flow: 'standard' });
    expect(begin[2]).toEqual({ value: CART_TOTAL });

    // Step 1 → 2 → 3.
    fireEvent.submit(screen.getByRole('form', { name: 'Step 1: Address' }));
    expect(screen.getByRole('form', { name: 'Step 2: Shipping' })).toBeInTheDocument();
    expect(callsFor('purchase')).toHaveLength(0);
    fireEvent.click(screen.getByLabelText(/Express/));
    fireEvent.submit(screen.getByRole('form', { name: 'Step 2: Shipping' }));
    expect(screen.getByRole('form', { name: 'Step 3: Payment' })).toBeInTheDocument();
    expect(screen.getByTestId('order-total')).toHaveTextContent('$196.00'); // 187 + 9 express

    fireEvent.submit(screen.getByRole('form', { name: 'Step 3: Payment' }));
    const [purchase] = callsFor('purchase');
    expect(purchase[1]).toMatchObject({ items: 3, flow: 'standard' });
    expect((purchase[1] as { order_id: string }).order_id).toMatch(/^SL-/);
    expect(purchase[2]).toEqual({ value: 196 });
    expect(screen.getByTestId('order-confirmation')).toHaveTextContent('Thanks for your order');
    expect(readCart()).toEqual([]);
  });

  it('one_page variant: single form, no stepper, purchase with flow one_page', () => {
    __setExperiment('shoplab_checkout_flow', { variantName: 'one_page', isControl: false, configuration: { steps: 1 } });
    renderPage(<CheckoutPage />);

    expect(screen.getByTestId('checkout-one-page')).toBeInTheDocument();
    expect(screen.queryByRole('list', { name: 'Checkout steps' })).not.toBeInTheDocument();
    expect(callsFor('begin_checkout')[0][1]).toEqual({ items: 3, flow: 'one_page' });

    fireEvent.submit(screen.getByRole('form', { name: 'One-page checkout' }));
    const [purchase] = callsFor('purchase');
    expect(purchase[1]).toMatchObject({ items: 3, flow: 'one_page' });
    expect(purchase[2]).toEqual({ value: CART_TOTAL });
    expect(readCart()).toEqual([]);
  });

  it('waits for the assignment before firing begin_checkout, and skips it for an empty cart', () => {
    __setExperiment('shoplab_checkout_flow', { loading: true });
    const { unmount } = renderPage(<CheckoutPage />);
    expect(callsFor('begin_checkout')).toHaveLength(0);
    unmount();

    trackEventMock.mockClear();
    writeCart([]);
    __setExperiment('shoplab_checkout_flow', { configuration: { steps: 1 } });
    renderPage(<CheckoutPage />);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Your cart is empty');
    expect(callsFor('begin_checkout')).toHaveLength(0);
    expect(callsFor('page_view')[0][1]).toEqual({ page: 'checkout' });
  });
});
