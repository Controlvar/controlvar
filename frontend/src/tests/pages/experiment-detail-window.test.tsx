/**
 * The experiment page and its draft design edit with a conversion window
 * (beta, #1934).
 *
 * `PUT /api/v1/experiments/{id}` replaces the metrics, and a metric sent
 * without `conversion_window_hours` loses its window, with a 200. So the gates
 * here are on the exact body: a save that changes something else carries every
 * stored window back. What is pinned:
 *  - the metrics card says "Conversion window: 7 days" only where one is set;
 *  - the design edit opens each box with the stored hours and sends them back
 *    as numbers, null where there is none;
 *  - a window that cannot be saved is refused with its metric named, nothing
 *    is sent, and focus goes to that row's box, never to another control.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import ExperimentDetailPage from '@/pages/experiments/[id]';
import { apiFetch } from '@/services/api';
import { Experiment } from '@/types/experiments';
import { windowPageLine, windowRefusal } from '@/utils/conversionWindow';
import DRAFT_ANSWER from '@/tests/fixtures/conversion-window/experiment-draft.json';
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

const STAMPS = { experiment_id: 'exp-1', created_at: '2026-10-01T10:00:00', updated_at: '2026-10-01T10:00:00' };

/** Two metrics with windows (168 and 36 hours) and one with none. */
const DRAFT: Experiment = {
  id: 'exp-1',
  name: 'Checkout',
  key: 'checkout',
  description: null,
  hypothesis: null,
  experiment_type: 'a_b',
  status: 'draft',
  targeting_rules: null,
  tags: null,
  owner_id: 'someone-else',
  start_date: null,
  end_date: null,
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-01T10:00:00Z',
  variants: [
    { id: 'v-1', name: 'Control', description: null, is_control: true, traffic_allocation: 50, configuration: null, ...STAMPS },
    { id: 'v-2', name: 'Treatment', description: null, is_control: false, traffic_allocation: 50, configuration: null, ...STAMPS },
  ],
  metrics: [
    {
      id: 'm-1',
      name: 'Purchase',
      description: null,
      event_name: 'purchase',
      metric_type: 'conversion',
      is_primary: true,
      aggregation_method: 'average',
      minimum_sample_size: 100,
      expected_effect: null,
      event_value_path: null,
      lower_is_better: false,
      conversion_window_hours: 168,
      ...STAMPS,
    },
    {
      id: 'm-2',
      name: 'Refund',
      description: null,
      event_name: 'refund',
      metric_type: 'conversion',
      is_primary: false,
      aggregation_method: 'average',
      minimum_sample_size: 100,
      expected_effect: null,
      event_value_path: null,
      lower_is_better: true,
      conversion_window_hours: 36,
      ...STAMPS,
    },
    {
      id: 'm-3',
      name: 'Visit',
      description: null,
      event_name: 'visit',
      metric_type: 'count',
      is_primary: false,
      aggregation_method: 'average',
      minimum_sample_size: 100,
      expected_effect: null,
      event_value_path: null,
      lower_is_better: false,
      conversion_window_hours: null,
      ...STAMPS,
    },
  ],
};

function install(current: Experiment) {
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/experiments/exp-1', handler: () => current },
      {
        method: 'PUT',
        path: '/api/v1/experiments/exp-1',
        handler: (_p, o) => {
          const body = o.json as { metrics: object[] };
          return {
            ...current,
            metrics: body.metrics.map((m, i) => ({ ...m, id: `m-new-${i}`, ...STAMPS })),
          };
        },
      },
      { path: '/api/v1/holdout', handler: () => null },
    ]) as unknown as typeof apiFetch,
  );
}

async function renderPage(current: Experiment = DRAFT) {
  install(current);
  render(<ExperimentDetailPage />);
  await screen.findByTestId('experiment-detail');
}

async function openDesign(current: Experiment = DRAFT) {
  await renderPage(current);
  fireEvent.click(screen.getByTestId('experiment-edit-variants'));
  return screen.getByTestId('design-form');
}

const puts = () => mockedApiFetch.mock.calls.filter(([, o]) => (o?.method ?? 'GET').toUpperCase() === 'PUT');
const putMetrics = () => (puts()[0][1]?.json as { metrics: Record<string, unknown>[] }).metrics;
const type = (id: string, value: string) => fireEvent.change(screen.getByTestId(id), { target: { value } });
const save = () => fireEvent.click(screen.getByTestId('design-save'));

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockUseAuth.mockReturnValue({
    user: { id: 'user-1', email: 'admin@demo.com', username: 'admin', role: 'ADMIN', is_superuser: false },
    status: 'authenticated',
  });
});

describe('the metrics card', () => {
  it('says "Conversion window: 7 days" where one is set, and nothing where none is', async () => {
    await renderPage();
    const rows = within(screen.getByTestId('metrics-list')).getAllByTestId('metric-row');
    expect(within(rows[0]).getByTestId('metric-window-line')).toHaveTextContent(windowPageLine(168));
    expect(windowPageLine(168)).toBe('Conversion window: 7 days');
    expect(within(rows[1]).getByTestId('metric-window-line')).toHaveTextContent('Conversion window: 36 hours');
    expect(within(rows[2]).queryByTestId('metric-window-line')).toBeNull();
    expect(rows[2].textContent).not.toMatch(/window/i);
  });

  it('an answer without the field shows no window line', async () => {
    const older = {
      ...DRAFT,
      metrics: DRAFT.metrics.map(({ conversion_window_hours, ...m }) => m),
    } as Experiment;
    await renderPage(older);
    expect(screen.queryByTestId('metric-window-line')).toBeNull();
  });
});

describe('a draft as the API answered it', () => {
  // `GET /api/v1/experiments/{id}` for a draft created with windows of 168 and
  // 36 hours and none, saved as returned (fixtures/conversion-window).
  const answered = { ...(DRAFT_ANSWER as unknown as Experiment), id: 'exp-1' };

  it('the card names both windows, and the design edit sends them back unchanged with a variant renamed', async () => {
    await renderPage(answered);
    const rows = within(screen.getByTestId('metrics-list')).getAllByTestId('metric-row');
    expect(rows.map((r) => within(r).queryByTestId('metric-window-line')?.textContent ?? null)).toEqual([
      'Conversion window: 7 days',
      'Conversion window: 36 hours',
      null,
    ]);
    fireEvent.click(screen.getByTestId('experiment-edit-variants'));
    expect([0, 1, 2].map((i) => (screen.getByTestId(`metric-window-${i}`) as HTMLInputElement).value)).toEqual([
      '168',
      '36',
      '',
    ]);
    type('variant-name-1', 'Blue button');
    save();
    await screen.findByTestId('design-saved');
    expect(putMetrics().map((m) => [m.name, m.conversion_window_hours])).toStrictEqual([
      ['Purchase', 168],
      ['Refund', 36],
      ['Visit', null],
    ]);
  });
});

describe('the design edit', () => {
  it('opens each box with the stored hours', async () => {
    await openDesign();
    expect(screen.getByTestId('metric-window-0')).toHaveValue('168');
    expect(screen.getByTestId('metric-window-1')).toHaveValue('36');
    expect(screen.getByTestId('metric-window-2')).toHaveValue('');
  });

  it('a save that only renames a variant carries both stored windows back, as numbers', async () => {
    await openDesign();
    type('variant-name-1', 'Green button');
    save();
    await screen.findByTestId('design-saved');
    expect(puts()).toHaveLength(1);
    expect(putMetrics().map((m) => [m.name, m.conversion_window_hours])).toStrictEqual([
      ['Purchase', 168],
      ['Refund', 36],
      ['Visit', null],
    ]);
  });

  it('a window changed in the box is sent; one cleared is sent as null', async () => {
    await openDesign();
    type('metric-window-0', '24');
    type('metric-window-1', '');
    save();
    await screen.findByTestId('design-saved');
    expect(putMetrics().map((m) => m.conversion_window_hours)).toStrictEqual([24, null, null]);
  });

  it('nothing changed sends nothing, windows included', async () => {
    await openDesign();
    save();
    await waitFor(() => expect(screen.queryByTestId('design-form')).toBeNull());
    expect(puts()).toEqual([]);
  });

  const TWO = { ...DRAFT, metrics: DRAFT.metrics.slice(0, 2) };

  it.each(['0', '8761', '1.5', '7d', '1e2'])(
    'refuses "%s" on row 2 of 2: that box takes focus, marked invalid and described by the refusal',
    async (typed) => {
      await openDesign(TWO);
      type('metric-window-1', typed);
      save();
      const alert = await screen.findByTestId('design-error');
      expect(alert).toHaveTextContent(windowRefusal('Refund'));
      const box = screen.getByTestId('metric-window-1');
      await waitFor(() => expect(box).toHaveFocus());
      expect(box).toHaveAttribute('aria-invalid', 'true');
      expect((box.getAttribute('aria-describedby') ?? '').split(' ')[0]).toBe(alert.id);
      // Not row 1's primary radio, where a refusal with no field of its own would land.
      expect(screen.getByTestId('metric-primary-0')).not.toHaveFocus();
      expect(screen.getByTestId('metric-primary-0')).not.toHaveAttribute('aria-describedby');
      expect(puts()).toEqual([]);
    },
  );

  it('fixing the box clears its mark, and the save then goes through', async () => {
    await openDesign(TWO);
    type('metric-window-1', '7d');
    save();
    await screen.findByTestId('design-error');
    type('metric-window-1', '7');
    expect(screen.queryByTestId('design-error')).toBeNull();
    expect(screen.getByTestId('metric-window-1')).not.toHaveAttribute('aria-invalid');
    save();
    await screen.findByTestId('design-saved');
    expect(putMetrics().map((m) => m.conversion_window_hours)).toStrictEqual([168, 7]);
  });
});
