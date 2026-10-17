import React, { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { useExperiment } from '@controlvar/react-sdk';
import { EXPERIMENT_KEYS } from '@/lib/env';
import { usePageView, useTrack } from '@/lib/eventLog';
import { findProduct, formatPrice } from '@/data/products';
import { useCart } from '@/lib/useCart';
import ProductArt from '@/components/ProductArt';

export const BUY_BUTTON_DEFAULTS = { color: '#0f172a', text: 'Add to cart' } as const;

function readButtonConfig(configuration: Record<string, unknown> | null) {
  const cfg = configuration ?? {};
  return {
    color: typeof cfg.color === 'string' && /^#[0-9a-f]{3,8}$/i.test(cfg.color) ? cfg.color : BUY_BUTTON_DEFAULTS.color,
    text: typeof cfg.text === 'string' && cfg.text ? cfg.text : BUY_BUTTON_DEFAULTS.text,
  };
}

export default function ProductDetailPage() {
  const router = useRouter();
  const track = useTrack();
  const cart = useCart();
  const buyButton = useExperiment(EXPERIMENT_KEYS.pdpBuyButton);
  const { color, text } = readButtonConfig(buyButton.configuration);
  const id = typeof router.query.id === 'string' ? router.query.id : undefined;
  const product = findProduct(id);
  const [quantity, setQuantity] = useState(1);
  const [added, setAdded] = useState(false);
  usePageView('product');

  if (!id) {
    return <p className="text-neutral-500">Loading…</p>;
  }
  if (!product) {
    return (
      <div>
        <h1 className="text-2xl font-bold">Product not found</h1>
        <Link href="/products" className="mt-4 inline-block text-accent-600 hover:underline">
          ← Back to products
        </Link>
      </div>
    );
  }

  const onBuy = () => {
    cart.add(product.id, product.price, quantity);
    track('add_to_cart', { product_id: product.id, quantity, button: text }, { value: product.price });
    setAdded(true);
  };

  return (
    <div className="grid grid-cols-1 gap-10 md:grid-cols-2">
      <ProductArt product={product} className="aspect-square w-full" />
      <div>
        <Link href="/products" className="text-sm text-neutral-500 hover:underline">
          ← All products
        </Link>
        <p className="mt-4 text-xs uppercase tracking-wide text-neutral-500">{product.category}</p>
        <h1 className="mt-1 text-3xl font-bold">{product.name}</h1>
        <p className="mt-2 text-sm text-neutral-600">★ {product.rating.toFixed(1)} · 128 reviews</p>
        <p className="mt-4 text-2xl font-bold" data-testid="price">
          {formatPrice(product.price)}
        </p>
        <p className="mt-4 leading-relaxed text-neutral-700">{product.description}</p>

        <div className="mt-6 flex items-end gap-3">
          <div>
            <label htmlFor="quantity" className="label">
              Quantity
            </label>
            <select
              id="quantity"
              className="input w-20"
              value={quantity}
              onChange={(e) => setQuantity(Number(e.target.value))}
            >
              {[1, 2, 3, 4, 5].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </div>
          <button
            type="button"
            onClick={onBuy}
            data-testid="buy-button"
            data-variant={buyButton.variantName}
            className="btn flex-1 py-3 text-base text-white shadow-sm hover:opacity-90"
            style={{ backgroundColor: color }}
          >
            {text}
          </button>
        </div>

        {added && (
          <div role="status" className="mt-4 rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-900">
            Added to your cart ({cart.count} {cart.count === 1 ? 'item' : 'items'}).{' '}
            <Link href="/checkout" className="font-semibold underline">
              Go to checkout
            </Link>
          </div>
        )}
      </div>
    </div>
  );
}
