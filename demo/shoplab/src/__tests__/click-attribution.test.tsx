/**
 * Which experiments a product click counts for. The sort bandit's reward is
 * product_click, so only a click on the sorted product list may reach it: a click
 * on the home page's featured products or on a search result would reward the arm
 * the visitor was given on /products for a list it did not sort.
 *
 * The pages run against the SDK mock as everywhere else; what they pass to
 * `track` is then sent through the React SDK's real client (sdk/react/src, the
 * code the storefront runs), and the test reads the request bodies it posts.
 */
import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import { ExperimentationClient } from '../../../../sdk/react/src/client/ExperimentationClient';
import HomePage from '@/pages/index';
import SearchPage from '@/pages/search';
import ProductListPage from '@/pages/products/index';
import { EXPERIMENT_KEYS, FLAG_KEYS } from '@/lib/env';
import { __setFlag, DEFAULT_TEST_USER } from '@/__mocks__/controlvar-sdk';
import { callsFor, renderPage, resetTestState } from '@/test-utils';

type Sent = { path: string; body: Record<string, unknown> };

const realFetch = global.fetch;

/** A real SDK client whose requests are answered here and kept in `sent`. */
function realClient(): { client: ExperimentationClient; sent: Sent[] } {
  const sent: Sent[] = [];
  global.fetch = jest.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const path = new URL(String(url)).pathname;
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};
    sent.push({ path, body });
    const answer =
      path === '/api/v1/tracking/assign'
        ? { experiment_key: body.experiment_key, variant_name: 'control', variant_id: 'v-1', is_control: true, assigned: true, reason: 'assigned' }
        : path.startsWith('/api/v1/feature-flags/evaluate/')
          ? { enabled: true, reason: 'rollout' }
          : {};
    return { ok: true, status: 200, json: async () => answer } as Response;
  }) as typeof fetch;
  return { client: new ExperimentationClient({ apiKey: 'test-key', baseUrl: 'http://api.test' }), sent };
}

/** The visitor has been on the home page and the product list: hero and sort assigned, the banner flag evaluated. */
async function visitorWhoSawTheProductList() {
  const { client, sent } = realClient();
  await client.assignExperiment(DEFAULT_TEST_USER, EXPERIMENT_KEYS.hero);
  await client.assignExperiment(DEFAULT_TEST_USER, EXPERIMENT_KEYS.plpSort);
  await client.evaluateFeatureFlag(DEFAULT_TEST_USER, FLAG_KEYS.freeShippingBanner);
  sent.length = 0;
  return { client, sent };
}

/** Sends the page's last product_click through the real client; returns every key its requests carry. */
async function keysOfTheClick(client: ExperimentationClient, sent: Sent[]): Promise<string[]> {
  const calls = callsFor('product_click');
  expect(calls).toHaveLength(1);
  const [name, properties, options] = calls[0];
  await client.trackEvent(DEFAULT_TEST_USER.userId, name, properties, options);
  const bodies = sent.flatMap((s) =>
    s.path === '/api/v1/tracking/batch'
      ? (s.body.events as Array<Record<string, unknown>>)
      : s.path === '/api/v1/tracking/track'
        ? [s.body]
        : [],
  );
  expect(bodies.length).toBeGreaterThan(0);
  return bodies.flatMap((b) => [b.experiment_key, b.feature_flag_key].filter((k): k is string => typeof k === 'string')).sort();
}

function clickFirstProduct() {
  fireEvent.click(screen.getAllByTestId('product-card')[0].querySelector('a') as HTMLAnchorElement);
}

describe('product_click attribution', () => {
  beforeEach(resetTestState);
  afterEach(() => {
    global.fetch = realFetch;
  });

  it('a click on the sorted product list reaches the sort bandit (the reward it learns from)', async () => {
    const { client, sent } = await visitorWhoSawTheProductList();
    renderPage(<ProductListPage />);
    clickFirstProduct();

    expect(await keysOfTheClick(client, sent)).toContain(EXPERIMENT_KEYS.plpSort);
  });

  it('a click on a featured product on the home page counts for the hero test, not the sort bandit', async () => {
    const { client, sent } = await visitorWhoSawTheProductList();
    renderPage(<HomePage />);
    clickFirstProduct();

    expect(await keysOfTheClick(client, sent)).toEqual([EXPERIMENT_KEYS.hero]);
  });

  it('a click on a search result is attached to the search flag, not the sort bandit', async () => {
    __setFlag(FLAG_KEYS.newSearch, { isEnabled: true, variant: 'on' });
    const { client, sent } = await visitorWhoSawTheProductList();
    await client.evaluateFeatureFlag(DEFAULT_TEST_USER, FLAG_KEYS.newSearch);
    sent.length = 0;
    renderPage(<SearchPage />);
    fireEvent.change(screen.getByLabelText('Search products'), { target: { value: 'boots' } });
    fireEvent.submit(screen.getByRole('search'));
    clickFirstProduct();

    expect(await keysOfTheClick(client, sent)).toEqual([FLAG_KEYS.newSearch]);
  });
});
