// Each setting is read under its name, then under the name it had before the
// project was renamed (an existing .env.local), then defaults. Each read is a
// literal `process.env.NAME`: Next.js inlines only that form into the bundle.
function stripTrailingSlash(url: string): string {
  return url.replace(/\/+$/, '');
}

/** Controlvar API origin. The SDK appends `/api/v1` to every request. */
export const API_URL = stripTrailingSlash(process.env.NEXT_PUBLIC_CONTROLVAR_API_URL || process.env.NEXT_PUBLIC_EXPERIMENTLY_API_URL || 'http://localhost:8000');

/** Plaintext API key (`shoplab-storefront`, owned by admin@demo.com). Written by the seed script. */
export const API_KEY = process.env.NEXT_PUBLIC_CONTROLVAR_API_KEY || process.env.NEXT_PUBLIC_EXPERIMENTLY_API_KEY || '';

/** Controlvar dashboard, used by the overlay panel's links. */
export const DASHBOARD_URL = stripTrailingSlash(
  process.env.NEXT_PUBLIC_CONTROLVAR_DASHBOARD_URL || process.env.NEXT_PUBLIC_EXPERIMENTLY_DASHBOARD_URL || 'http://localhost:3100',
);

/** Experiment keys the storefront uses — must match the seed script exactly. */
export const EXPERIMENT_KEYS = {
  hero: 'shoplab_hero_banner',
  plpSort: 'shoplab_plp_sort',
  pdpBuyButton: 'shoplab_pdp_buy_button',
  checkoutFlow: 'shoplab_checkout_flow',
} as const;

/** Feature flag keys the storefront uses. */
export const FLAG_KEYS = {
  newSearch: 'shoplab_new_search',
  freeShippingBanner: 'shoplab_free_shipping_banner',
} as const;

export const ALL_EXPERIMENT_KEYS: string[] = Object.values(EXPERIMENT_KEYS);
export const ALL_FLAG_KEYS: string[] = Object.values(FLAG_KEYS);
