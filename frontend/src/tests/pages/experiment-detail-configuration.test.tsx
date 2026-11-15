/**
 * The experiment page shows each variant's configuration, whether Bayesian
 * analysis is on, whether sequential testing is on (#1449), and an adaptive
 * experiment's traffic algorithm (#442).
 *
 * Everything here is read from `GET /api/v1/experiments/{id}` and shown to
 * every role that can open the page; nothing on it sends a request.
 */
import React from 'react';
import { render, screen, within } from '@testing-library/react';
import axe from 'axe-core';
import ExperimentDetailPage, { algorithmLabel, formatConfiguration } from '@/pages/experiments/[id]';
import { apiFetch } from '@/services/api';
import { Experiment } from '@/types/experiments';
import { makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/experiments/[id]', query: { id: 'exp-1' } });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockUseAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useAuth: () => mockUseAuth() }));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

/** What the API sends for a ShopLab-like hero test: a nested, non-ASCII configuration on one variant. */
const HERO_CONFIGURATION = {
  headline: 'Été en avance ✓',
  hero: { kind: 'video', autoplay: false, sizes: [640, 1280] },
  discount: 0.15,
  badge: null,
};

const BASE: Experiment = {
  id: 'exp-1',
  name: 'Hero banner',
  key: 'hero-banner',
  description: null,
  hypothesis: null,
  experiment_type: 'a_b',
  status: 'active',
  targeting_rules: null,
  owner_id: 'user-1',
  start_date: '2026-09-01T10:00:00Z',
  end_date: null,
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-01T10:00:00Z',
  variants: [
    { id: 'v-1', name: 'Image', is_control: true, traffic_allocation: 50, configuration: null },
    { id: 'v-2', name: 'Video', is_control: false, traffic_allocation: 50, configuration: HERO_CONFIGURATION },
  ],
  metrics: [{ id: 'm-1', name: 'Click', event_name: 'click', metric_type: 'conversion', is_primary: true }],
  optimization_type: 'fixed',
  bayesian_enabled: false,
};

function show(overrides: Partial<Experiment> = {}) {
  const current = { ...BASE, ...overrides };
  mockedApiFetch.mockImplementation(
    routedApi([{ path: '/api/v1/experiments/exp-1', handler: () => current }]) as unknown as typeof apiFetch,
  );
  render(<ExperimentDetailPage />);
  return screen.findByTestId('experiment-detail');
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockUseAuth.mockReturnValue({
    user: { id: 'user-9', email: 'viewer@demo.com', username: 'viewer', role: 'VIEWER' },
    status: 'authenticated',
  });
});

describe('the variants table', () => {
  it('has a Configuration column showing each configuration as indented JSON', async () => {
    await show();
    const table = screen.getByTestId('variants-table');
    expect(within(table).getByRole('columnheader', { name: 'Configuration' })).toBeInTheDocument();
    const view = screen.getByTestId('variant-configuration-view-1');
    expect(view.tagName).toBe('PRE');
    expect(view.textContent).toBe(JSON.stringify(HERO_CONFIGURATION, null, 2));
  });

  it('shows a dash, read as "None", for a variant with no configuration', async () => {
    await show();
    const none = screen.getByTestId('variant-configuration-none-0');
    expect(none).toHaveTextContent('None');
    expect(screen.queryByTestId('variant-configuration-view-0')).not.toBeInTheDocument();
  });

  it('shows an empty object as {}, not as none', async () => {
    await show({
      variants: [{ id: 'v-1', name: 'Image', is_control: true, traffic_allocation: 100, configuration: {} }],
    });
    expect(screen.getByTestId('variant-configuration-view-0').textContent).toBe('{}');
  });

  it('treats a missing configuration key like null', async () => {
    await show({ variants: [{ id: 'v-1', name: 'Image', is_control: true, traffic_allocation: 100 }] });
    expect(screen.getByTestId('variant-configuration-none-0')).toBeInTheDocument();
  });

  it('renders markup in a configuration as text, never as an element', async () => {
    const hostile = {
      html: '<img src=x onerror="window.__planted=1"><script>window.__planted=2</script>',
      '<b>key</b>': 'value',
    };
    await show({
      variants: [{ id: 'v-1', name: 'Image', is_control: true, traffic_allocation: 100, configuration: hostile }],
    });
    const table = screen.getByTestId('variants-table');
    expect(table.querySelector('img')).toBeNull();
    expect(table.querySelector('script')).toBeNull();
    expect(table.querySelector('b')).toBeNull();
    expect(screen.getByTestId('variant-configuration-view-0').textContent).toBe(JSON.stringify(hostile, null, 2));
    expect((window as unknown as { __planted?: number }).__planted).toBeUndefined();
  });

  it('the configuration can be reached by keyboard, so a long one can be scrolled', async () => {
    await show();
    expect(screen.getByTestId('variant-configuration-view-1')).toHaveAttribute('tabindex', '0');
  });
});

describe('the header rows', () => {
  it.each([
    [true, 'On'],
    [false, 'Off'],
  ])('bayesian_enabled %s reads %s', async (enabled, text) => {
    await show({ bayesian_enabled: enabled });
    expect(screen.getByTestId('experiment-bayesian')).toHaveTextContent(text);
  });

  it('leaves the Bayesian row out when the API does not say', async () => {
    await show({ bayesian_enabled: undefined });
    expect(screen.queryByTestId('experiment-bayesian')).not.toBeInTheDocument();
  });

  // #1449: sequential testing is turned on through the API only; the page
  // says whether it is on. Bayesian is set the other way each time, so a row
  // that read the wrong field fails.
  it.each([
    [true, false, 'On'],
    [false, true, 'Off'],
  ])('sequential_testing_enabled %s (Bayesian %s) reads %s', async (sequential, bayesian, text) => {
    await show({ sequential_testing_enabled: sequential, bayesian_enabled: bayesian });
    const row = screen.getByTestId('experiment-sequential');
    expect(row).toHaveTextContent(text);
    expect(row.previousElementSibling?.textContent).toBe('Sequential testing (beta)');
  });

  it('leaves the sequential row out when the API does not say', async () => {
    await show({ sequential_testing_enabled: undefined, bayesian_enabled: true });
    expect(screen.queryByTestId('experiment-sequential')).not.toBeInTheDocument();
    expect(screen.queryByText('Sequential testing (beta)')).not.toBeInTheDocument();
  });

  // plan-1449 #13: the rows keep their order, and each is one item holding a
  // dt and then its dd (axe does not flag a dt left without its dd).
  it('lists every term in order, each item a dt followed by its dd', async () => {
    const page = await show({
      status: 'completed',
      confidence_level: 0.95,
      correction_method: 'bonferroni',
      bayesian_enabled: true,
      sequential_testing_enabled: false,
      experiment_type: 'bandit',
      optimization_type: 'thompson_sampling',
      start_date: '2026-09-01T10:00:00Z',
      end_date: '2026-09-20T10:00:00Z',
    });
    const lists = page.querySelectorAll('dl');
    expect(lists).toHaveLength(1);
    const dl = lists[0];
    expect(Array.from(dl.querySelectorAll('dt')).map((dt) => dt.textContent)).toEqual([
      'Key',
      'Type',
      'Analysis',
      'Bayesian analysis',
      'Sequential testing (beta)',
      'Traffic algorithm',
      'Owner',
      'Created',
      'Started',
      'Ended',
      'Exclusion group',
      'Global holdout',
    ]);
    expect(Array.from(dl.children).map((item) => Array.from(item.children).map((c) => c.tagName))).toEqual(
      Array.from({ length: 12 }, () => ['DT', 'DD']),
    );
  });

  it.each([
    ['thompson_sampling', 'Thompson sampling'],
    ['ucb1', 'UCB1'],
    ['epsilon_greedy', 'Epsilon-greedy'],
  ])('an adaptive experiment (%s) shows its algorithm as %s', async (optimization, label) => {
    await show({ experiment_type: 'bandit', optimization_type: optimization });
    expect(screen.getByTestId('experiment-algorithm')).toHaveTextContent(label);
  });

  it('a fixed split shows no algorithm row', async () => {
    await show({ optimization_type: 'fixed' });
    expect(screen.queryByTestId('experiment-algorithm')).not.toBeInTheDocument();
  });

  it('an algorithm this page does not know is shown as the API names it', async () => {
    await show({ optimization_type: 'softmax_v2' });
    expect(screen.getByTestId('experiment-algorithm')).toHaveTextContent('softmax_v2');
  });
});

describe('helpers', () => {
  it('algorithmLabel is null for fixed or none', () => {
    expect(algorithmLabel('fixed')).toBeNull();
    expect(algorithmLabel(undefined)).toBeNull();
    expect(algorithmLabel(null)).toBeNull();
    expect(algorithmLabel('ucb1')).toBe('UCB1');
  });

  it('formatConfiguration is null only for null or undefined', () => {
    expect(formatConfiguration(null)).toBeNull();
    expect(formatConfiguration(undefined)).toBeNull();
    expect(formatConfiguration({})).toBe('{}');
    expect(formatConfiguration({ a: [1] })).toBe('{\n  "a": [\n    1\n  ]\n}');
  });
});

describe('accessibility (axe-core in jsdom; colour contrast is not computable here)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  it('a page with configurations, Bayesian and sequential on and an algorithm has no axe violations', async () => {
    const page = await show({
      bayesian_enabled: true,
      sequential_testing_enabled: true,
      experiment_type: 'bandit',
      optimization_type: 'thompson_sampling',
    });
    const result = await axe.run(page, axeOptions);
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toEqual([]);
  });
});
