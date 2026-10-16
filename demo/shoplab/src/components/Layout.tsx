import React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { useFeatureFlag } from '@controlvar/react-sdk';
import { FLAG_KEYS } from '@/lib/env';
import { useCart } from '@/lib/useCart';

const NAV = [
  { href: '/', label: 'Home' },
  { href: '/products', label: 'Products' },
  { href: '/search', label: 'Search' },
];

export default function Layout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { count } = useCart();
  const freeShipping = useFeatureFlag(FLAG_KEYS.freeShippingBanner);

  return (
    <div className="flex min-h-screen flex-col">
      {freeShipping.isEnabled && (
        <div
          role="status"
          data-testid="free-shipping-banner"
          className="bg-accent-600 px-4 py-2 text-center text-sm font-medium text-white"
        >
          Free shipping on every order this week — no minimum.
        </div>
      )}
      <header className="border-b border-neutral-200 bg-white">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-6 px-4 py-4">
          <Link href="/" className="text-xl font-extrabold tracking-tight text-neutral-900" aria-label="ShopLab home">
            Shop<span className="text-accent-600">Lab</span>
          </Link>
          <nav aria-label="Primary" className="flex items-center gap-1 sm:gap-4">
            {NAV.map((item) => {
              const active = item.href === '/' ? router.pathname === '/' : router.pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? 'page' : undefined}
                  className={`rounded-md px-3 py-2 text-sm font-medium ${
                    active ? 'bg-neutral-100 text-neutral-900' : 'text-neutral-600 hover:text-neutral-900'
                  }`}
                >
                  {item.label}
                </Link>
              );
            })}
            <Link
              href="/checkout"
              className="btn-secondary ml-2"
              aria-label={`Cart, ${count} ${count === 1 ? 'item' : 'items'}`}
            >
              Cart
              <span
                data-testid="cart-count"
                className="ml-2 rounded-full bg-accent-600 px-2 py-0.5 text-xs font-semibold text-white"
              >
                {count}
              </span>
            </Link>
          </nav>
        </div>
      </header>
      <main id="main" className="mx-auto w-full max-w-6xl flex-1 px-4 py-8">
        {children}
      </main>
      <footer className="border-t border-neutral-200 bg-white">
        <div className="mx-auto max-w-6xl px-4 py-6 text-sm text-neutral-500">
          ShopLab is a demo storefront. Nothing is for sale; every click is an experiment event.
        </div>
      </footer>
    </div>
  );
}
