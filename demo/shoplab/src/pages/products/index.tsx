import React, { useMemo } from 'react';
import { useExperiment, useExperimentation } from '@controlvar/react-sdk';
import { EXPERIMENT_KEYS } from '@/lib/env';
import { usePageView, useTrack } from '@/lib/eventLog';
import { PRODUCTS } from '@/data/products';
import { normalizeAlgorithm, SORT_LABELS, sortProducts } from '@/lib/sorting';
import ProductCard from '@/components/ProductCard';

export default function ProductListPage() {
  const track = useTrack();
  const { user } = useExperimentation();
  const sortExperiment = useExperiment(EXPERIMENT_KEYS.plpSort);
  const algorithm = normalizeAlgorithm(sortExperiment.configuration?.algorithm);
  usePageView('products');

  const products = useMemo(() => sortProducts(PRODUCTS, algorithm, user.userId), [algorithm, user.userId]);

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-3xl font-bold">All products</h1>
          <p className="mt-1 text-neutral-600">{products.length} items</p>
        </div>
        <p className="text-sm text-neutral-600" data-testid="sort-label" data-sort={algorithm}>
          Sorted by <span className="font-semibold text-neutral-900">{SORT_LABELS[algorithm]}</span>
        </p>
      </div>
      <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3" data-testid="product-grid">
        {products.map((product, index) => (
          <ProductCard
            key={product.id}
            product={product}
            position={index + 1}
            onClick={(p, position) => track('product_click', { product_id: p.id, position, sort: algorithm })}
          />
        ))}
      </ul>
    </div>
  );
}
