import React, { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useExperiment } from '@controlvar/react-sdk';
import { EXPERIMENT_KEYS } from '@/lib/env';
import { usePageView, useTrack } from '@/lib/eventLog';
import { findProduct, formatPrice } from '@/data/products';
import { cartCount, cartTotal } from '@/lib/cart';
import { useCart } from '@/lib/useCart';

export type CheckoutFlow = 'standard' | 'one_page';

const STEPS = ['Address', 'Shipping', 'Payment'] as const;

const SHIPPING_OPTIONS = [
  { id: 'standard', label: 'Standard (3–5 days)', price: 0 },
  { id: 'express', label: 'Express (1–2 days)', price: 9 },
] as const;

/** `{"steps":1}` → one page; anything else (including the loading default) → standard. */
export function flowFromConfiguration(configuration: Record<string, unknown> | null): CheckoutFlow {
  return configuration?.steps === 1 ? 'one_page' : 'standard';
}

export function makeOrderId(): string {
  return `SL-${Date.now().toString(36).toUpperCase()}-${Math.floor(Math.random() * 900 + 100)}`;
}

interface FormState {
  name: string;
  email: string;
  address: string;
  city: string;
  postal: string;
  shipping: (typeof SHIPPING_OPTIONS)[number]['id'];
  card: string;
}

const EMPTY_FORM: FormState = { name: '', email: '', address: '', city: '', postal: '', shipping: 'standard', card: '' };

export default function CheckoutPage() {
  const track = useTrack();
  const cart = useCart();
  const experiment = useExperiment(EXPERIMENT_KEYS.checkoutFlow);
  const flow = flowFromConfiguration(experiment.configuration);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [step, setStep] = useState(0);
  const [order, setOrder] = useState<{ id: string; total: number; items: number } | null>(null);
  usePageView('checkout');

  // Fire begin_checkout once the assignment has settled (so `flow` is right) and the
  // cart is known to be non-empty.
  const began = useRef(false);
  useEffect(() => {
    if (began.current || experiment.loading || cart.items.length === 0) return;
    began.current = true;
    track('begin_checkout', { items: cartCount(cart.items), flow }, { value: cartTotal(cart.items) });
  }, [experiment.loading, cart.items, flow, track]);

  const shippingPrice = SHIPPING_OPTIONS.find((o) => o.id === form.shipping)?.price ?? 0;
  const orderTotal = Math.round((cart.total + shippingPrice) * 100) / 100;

  const update = (field: keyof FormState) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [field]: e.target.value }));

  const placeOrder = (e: React.FormEvent) => {
    e.preventDefault();
    const items = cart.count;
    const orderId = makeOrderId();
    track('purchase', { items, flow, order_id: orderId }, { value: orderTotal });
    setOrder({ id: orderId, total: orderTotal, items });
    cart.clear();
  };

  if (order) {
    return (
      <div className="mx-auto max-w-lg text-center" role="status" data-testid="order-confirmation">
        <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-emerald-100 text-2xl text-emerald-700">
          ✓
        </div>
        <h1 className="text-3xl font-bold">Thanks for your order!</h1>
        <p className="mt-2 text-neutral-600">
          Order <code className="font-mono">{order.id}</code> · {order.items} {order.items === 1 ? 'item' : 'items'} ·{' '}
          <span className="font-semibold">{formatPrice(order.total)}</span>
        </p>
        <p className="mt-1 text-sm text-neutral-500">This is a demo — nothing was charged.</p>
        <Link href="/products" className="btn-primary mt-6">
          Keep shopping
        </Link>
      </div>
    );
  }

  if (cart.items.length === 0) {
    return (
      <div className="mx-auto max-w-lg text-center">
        <h1 className="text-3xl font-bold">Your cart is empty</h1>
        <p className="mt-2 text-neutral-600">Add something from the catalogue to start checkout.</p>
        <Link href="/products" className="btn-primary mt-6">
          Browse products
        </Link>
      </div>
    );
  }

  const addressFields = (
    <fieldset className="space-y-3">
      <legend className="mb-1 text-lg font-semibold">Delivery address</legend>
      <div>
        <label htmlFor="name" className="label">
          Full name
        </label>
        <input id="name" className="input" required autoComplete="name" value={form.name} onChange={update('name')} />
      </div>
      <div>
        <label htmlFor="email" className="label">
          Email
        </label>
        <input id="email" type="email" className="input" required autoComplete="email" value={form.email} onChange={update('email')} />
      </div>
      <div>
        <label htmlFor="address" className="label">
          Street address
        </label>
        <input id="address" className="input" required autoComplete="street-address" value={form.address} onChange={update('address')} />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label htmlFor="city" className="label">
            City
          </label>
          <input id="city" className="input" required autoComplete="address-level2" value={form.city} onChange={update('city')} />
        </div>
        <div>
          <label htmlFor="postal" className="label">
            Postal code
          </label>
          <input id="postal" className="input" required autoComplete="postal-code" value={form.postal} onChange={update('postal')} />
        </div>
      </div>
    </fieldset>
  );

  const shippingFields = (
    <fieldset className="space-y-2">
      <legend className="mb-1 text-lg font-semibold">Shipping method</legend>
      {SHIPPING_OPTIONS.map((opt) => (
        <label key={opt.id} className="flex cursor-pointer items-center gap-3 rounded-md border border-neutral-200 p-3 hover:bg-neutral-50">
          <input
            type="radio"
            name="shipping"
            value={opt.id}
            checked={form.shipping === opt.id}
            onChange={update('shipping')}
          />
          <span className="flex-1">{opt.label}</span>
          <span className="font-semibold">{opt.price === 0 ? 'Free' : formatPrice(opt.price)}</span>
        </label>
      ))}
    </fieldset>
  );

  const paymentFields = (
    <fieldset className="space-y-3">
      <legend className="mb-1 text-lg font-semibold">Payment</legend>
      <div>
        <label htmlFor="card" className="label">
          Card number (any digits — demo only)
        </label>
        <input id="card" className="input" inputMode="numeric" required placeholder="4242 4242 4242 4242" value={form.card} onChange={update('card')} />
      </div>
    </fieldset>
  );

  const summary = (
    <aside className="card h-fit p-4" aria-labelledby="summary-heading">
      <h2 id="summary-heading" className="mb-3 font-semibold">
        Order summary
      </h2>
      <ul className="divide-y divide-neutral-100 text-sm">
        {cart.items.map((item) => {
          const product = findProduct(item.productId);
          return (
            <li key={item.productId} className="flex justify-between py-2">
              <span>
                {product?.name ?? item.productId} × {item.quantity}
              </span>
              <span>{formatPrice(item.price * item.quantity)}</span>
            </li>
          );
        })}
      </ul>
      <dl className="mt-3 space-y-1 border-t border-neutral-200 pt-3 text-sm">
        <div className="flex justify-between">
          <dt>Subtotal</dt>
          <dd>{formatPrice(cart.total)}</dd>
        </div>
        <div className="flex justify-between">
          <dt>Shipping</dt>
          <dd>{shippingPrice === 0 ? 'Free' : formatPrice(shippingPrice)}</dd>
        </div>
        <div className="flex justify-between text-base font-bold">
          <dt>Total</dt>
          <dd data-testid="order-total">{formatPrice(orderTotal)}</dd>
        </div>
      </dl>
    </aside>
  );

  const placeOrderButton = (
    <button type="submit" className="btn-primary w-full py-3 text-base">
      Place order · {formatPrice(orderTotal)}
    </button>
  );

  return (
    <div>
      <h1 className="mb-6 text-3xl font-bold">Checkout</h1>
      <div className="grid grid-cols-1 gap-8 md:grid-cols-[1fr_20rem]">
        {flow === 'one_page' ? (
          <form onSubmit={placeOrder} className="card space-y-8 p-6" data-testid="checkout-one-page" aria-label="One-page checkout">
            {addressFields}
            {shippingFields}
            {paymentFields}
            {placeOrderButton}
          </form>
        ) : (
          <div className="card p-6" data-testid="checkout-standard">
            <ol className="mb-6 flex items-center gap-2" aria-label="Checkout steps">
              {STEPS.map((label, i) => (
                <li
                  key={label}
                  aria-current={i === step ? 'step' : undefined}
                  className={`flex items-center gap-2 text-sm ${i === step ? 'font-semibold text-neutral-900' : 'text-neutral-500'}`}
                >
                  <span
                    className={`flex h-6 w-6 items-center justify-center rounded-full text-xs ${
                      i < step ? 'bg-emerald-600 text-white' : i === step ? 'bg-accent-600 text-white' : 'bg-neutral-200'
                    }`}
                  >
                    {i < step ? '✓' : i + 1}
                  </span>
                  {label}
                  {i < STEPS.length - 1 && <span className="mx-1 text-neutral-300">—</span>}
                </li>
              ))}
            </ol>
            {step < STEPS.length - 1 ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  setStep((s) => s + 1);
                }}
                className="space-y-6"
                aria-label={`Step ${step + 1}: ${STEPS[step]}`}
              >
                {step === 0 ? addressFields : shippingFields}
                <div className="flex justify-between">
                  {step > 0 ? (
                    <button type="button" className="btn-secondary" onClick={() => setStep((s) => s - 1)}>
                      Back
                    </button>
                  ) : (
                    <span />
                  )}
                  <button type="submit" className="btn-primary">
                    Continue
                  </button>
                </div>
              </form>
            ) : (
              <form onSubmit={placeOrder} className="space-y-6" aria-label="Step 3: Payment">
                {paymentFields}
                <div className="flex items-center gap-3">
                  <button type="button" className="btn-secondary" onClick={() => setStep((s) => s - 1)}>
                    Back
                  </button>
                  <div className="flex-1">{placeOrderButton}</div>
                </div>
              </form>
            )}
          </div>
        )}
        {summary}
      </div>
    </div>
  );
}
