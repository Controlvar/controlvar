/**
 * The new-experiment form sets a metric's `lower_is_better` (#1377).
 *
 * Without it, no experiment created in the dashboard could use the direction
 * the results read: the API takes `lower_is_better` on each metric and stores
 * false when it is absent.
 *
 * What is pinned:
 *  - each metric row has a "Lower is better" checkbox, unticked by default;
 *  - ticking it sends `lower_is_better: true` on that metric only, through
 *    guided setup and through the single-page form alike;
 *  - unticked, the request carries no `lower_is_better` key at all, so the
 *    body of a form that never touches it is the one sent before;
 *  - the metrics editor has no axe violations (colour contrast is not
 *    computable in jsdom).
 */
import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import NewExperimentPage from '@/pages/experiments/new';
import { apiFetch } from '@/services/api';
import {
  buildCreatePayload,
  createInitialFormState,
  experimentFormReducer,
  ExperimentFormAction,
} from '@/components/experiments/new/formState';
import { makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

let mockRouter = makeRouter();
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

jest.mock('@/contexts/AuthContext', () => ({
  useOptionalAuth: () => null,
}));

jest.mock('@/components/experiments/new/leaveGuard', () => ({
  ...jest.requireActual('@/components/experiments/new/leaveGuard'),
  navigateHard: jest.fn(),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

function routerAt(query: Record<string, string> = {}) {
  const search = new URLSearchParams(query).toString();
  mockRouter = makeRouter({
    pathname: '/experiments/new',
    asPath: `/experiments/new${search ? `?${search}` : ''}`,
    query,
  });
}

function api() {
  mockedApiFetch.mockImplementation(
    routedApi([
      { method: 'POST', path: '/api/v1/experiments', handler: () => ({ id: 'exp-9', status: 'draft' }) },
    ]) as unknown as typeof apiFetch,
  );
}

const setValue = (testId: string, value: string) =>
  fireEvent.change(screen.getByTestId(testId), { target: { value } });
const next = () => fireEvent.click(screen.getByTestId('wizard-next'));

type SentMetric = Record<string, unknown>;

async function postedMetrics(): Promise<SentMetric[]> {
  await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-9'));
  const posts = mockedApiFetch.mock.calls.filter(([, o]) => (o?.method ?? 'GET').toUpperCase() === 'POST');
  expect(posts).toHaveLength(1);
  expect(posts[0][0]).toBe('/api/v1/experiments');
  return (posts[0][1]?.json as { metrics: SentMetric[] }).metrics;
}

/** Two metrics: a conversion primary, and an uninstall rate where lower is better. */
function fillMetrics(tickUninstall: boolean) {
  setValue('metric-name-0', 'Activation');
  setValue('metric-event-0', 'activated');
  fireEvent.click(screen.getByTestId('add-metric'));
  setValue('metric-name-1', 'Uninstall');
  setValue('metric-event-1', 'uninstall');
  if (tickUninstall) fireEvent.click(screen.getByTestId('metric-lower-is-better-1'));
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  routerAt();
});

describe('the metric row', () => {
  it('has a labelled "Lower is better" checkbox, unticked by default', () => {
    routerAt({ advanced: '' });
    render(<NewExperimentPage />);
    const row = screen.getByTestId('metric-row-0');
    const box = within(row).getByRole('checkbox', { name: /^Lower is better/ });
    expect(box).not.toBeChecked();
    fireEvent.click(box);
    expect(box).toBeChecked();
    fireEvent.click(box);
    expect(box).not.toBeChecked();
  });

  it('has no axe violations with two metrics, one of them lower is better', async () => {
    render(<NewExperimentPage />);
    fireEvent.click(screen.getByTestId('wizard-type-a_b'));
    next();
    fillMetrics(true);
    const section = screen.getByTestId('metric-row-0').parentElement as HTMLElement;
    // The "×" remove buttons are left out: axe measures their icon glyph on a
    // canvas, which jsdom does not have, and they are not part of this change.
    // In act: the page's links update themselves on a timer while axe runs.
    let result: axe.AxeResults | undefined;
    await act(async () => {
      result = await axe.run(
        { include: [section], exclude: [['[data-testid^="remove-metric"]']] },
        { rules: { 'color-contrast': { enabled: false } } },
      );
    });
    if (!result) throw new Error('axe did not run');
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toStrictEqual(
      [],
    );
  });
});

describe('the create request', () => {
  it('guided setup sends lower_is_better: true on the ticked metric only', async () => {
    api();
    render(<NewExperimentPage />);
    fireEvent.click(screen.getByTestId('wizard-type-a_b'));
    next();
    setValue('experiment-name', 'Onboarding');
    fillMetrics(true);
    next(); // variants: the default 50/50 split
    next(); // analysis settings
    next(); // the estimate is optional
    fireEvent.click(screen.getByTestId('wizard-create'));
    const metrics = await postedMetrics();
    expect(metrics[1]).toStrictEqual({
      name: 'Uninstall',
      event_name: 'uninstall',
      metric_type: 'conversion',
      is_primary: false,
      lower_is_better: true,
    });
    expect(metrics[0]).not.toHaveProperty('lower_is_better');
  });

  it('the single-page form sends the same', async () => {
    routerAt({ advanced: '' });
    api();
    render(<NewExperimentPage />);
    setValue('experiment-name', 'Onboarding');
    fillMetrics(true);
    fireEvent.click(screen.getByTestId('submit-experiment'));
    const metrics = await postedMetrics();
    expect(metrics[1]).toMatchObject({ name: 'Uninstall', lower_is_better: true });
    expect(metrics[0]).not.toHaveProperty('lower_is_better');
  });

  it('unticked, no metric carries the key', async () => {
    routerAt({ advanced: '' });
    api();
    render(<NewExperimentPage />);
    setValue('experiment-name', 'Onboarding');
    fillMetrics(false);
    fireEvent.click(screen.getByTestId('submit-experiment'));
    const metrics = await postedMetrics();
    for (const metric of metrics) expect(metric).not.toHaveProperty('lower_is_better');
  });
});

describe('the form state', () => {
  const apply = (actions: ExperimentFormAction[]) =>
    actions.reduce(experimentFormReducer, createInitialFormState());

  it('ticking then unticking sends nothing; ticking sends true', () => {
    const ticked = apply([{ type: 'changeMetric', index: 0, field: 'lower_is_better', value: true }]);
    expect(ticked.metrics[0].lower_is_better).toBe(true);
    expect(buildCreatePayload(ticked).metrics[0].lower_is_better).toBe(true);

    const unticked = apply([
      { type: 'changeMetric', index: 0, field: 'lower_is_better', value: true },
      { type: 'changeMetric', index: 0, field: 'lower_is_better', value: false },
    ]);
    expect(buildCreatePayload(unticked).metrics[0]).not.toHaveProperty('lower_is_better');
  });

  it('a new metric starts higher is better', () => {
    const s = apply([{ type: 'addMetric' }]);
    expect(s.metrics[1].lower_is_better).toBeUndefined();
    expect(buildCreatePayload(s).metrics[1]).not.toHaveProperty('lower_is_better');
  });
});
