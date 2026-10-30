/**
 * The experiment page's "Edit variants and metrics" (#1453): a draft's
 * variants and metrics, saved with one `PUT /api/v1/experiments/{id}` of
 * exactly `{variants, metrics}`.
 *
 * The update REPLACES both lists in one commit: a variant or a metric left
 * out is deleted, and a field left out of an item is reset to its default,
 * with a 200 (`minimum_sample_size` to 100, `expected_effect` to null, ...).
 * So the gates here are on the exact body: every item, every time (QA #5),
 * and every field, held to the OpenAPI snapshot's own schema (QA #6).
 *
 * Every case renders the page module itself, with `apiFetch` mocked and a
 * RESOLVED user (a null user is offered everything, so a role case rendered
 * without one would pass for the wrong reason).
 */
import React from 'react';
import fs from 'fs';
import path from 'path';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import ExperimentDetailPage from '@/pages/experiments/[id]';
import { ApiError, ApiFetchOptions, apiFetch } from '@/services/api';
import { Experiment, ExperimentStatus } from '@/types/experiments';
import {
  DESIGN_SAVED,
  EDIT_NOTE,
  ERROR_COPY,
  UNREACHABLE,
} from '@/components/experiments/ExperimentManageSection';
import {
  DESIGN_HEADING,
  DESIGN_NOTE,
  DESIGN_SEQUENTIAL_NOTE,
} from '@/components/experiments/ExperimentDesignForm';
import {
  checkMetrics,
  checkVariants,
  formStateFromExperiment,
  ONE_CONTROL,
  ONE_PRIMARY,
  TWO_VARIANTS,
} from '@/components/experiments/new/formState';
import { apiError, makeRouter, routedApi } from './helpers/apiMock';

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

/**
 * Three variants and two metrics, with every `VariantBase` and `MetricBase`
 * field away from its default (`is_control` and `is_primary` can be only on
 * one item each).
 */
const FULL: Experiment = {
  id: 'exp-1',
  name: 'Checkout button colour',
  key: 'checkout-button-colour',
  description: 'Blue vs green CTA',
  hypothesis: 'Blue converts better',
  experiment_type: 'a_b',
  status: 'draft',
  targeting_rules: null,
  tags: null,
  owner_id: 'someone-else',
  start_date: null,
  end_date: null,
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-01T10:00:00Z',
  sequential_testing_enabled: false,
  variants: [
    {
      id: 'v-1',
      name: 'Control',
      description: 'The page as it is',
      is_control: true,
      traffic_allocation: 34,
      configuration: { color: 'grey', layout: { columns: 2 } },
      ...STAMPS,
    },
    {
      id: 'v-2',
      name: 'Blue CTA',
      description: 'A blue button',
      is_control: false,
      traffic_allocation: 33,
      configuration: { color: 'blue' },
      ...STAMPS,
    },
    {
      id: 'v-3',
      name: 'Green CTA',
      description: 'A green button',
      is_control: false,
      traffic_allocation: 33,
      configuration: { color: 'green', label: 'Été ✓' },
      ...STAMPS,
    },
  ],
  metrics: [
    {
      id: 'm-1',
      name: 'Revenue',
      description: 'Order value',
      event_name: 'purchase',
      metric_type: 'revenue',
      is_primary: true,
      aggregation_method: 'sum',
      minimum_sample_size: 500,
      expected_effect: 0.1,
      event_value_path: 'amount',
      lower_is_better: true,
      ...STAMPS,
    },
    {
      id: 'm-2',
      name: 'Errors',
      description: 'Client errors',
      event_name: 'error',
      metric_type: 'count',
      is_primary: false,
      aggregation_method: 'median',
      minimum_sample_size: 250,
      expected_effect: 0.03,
      event_value_path: 'count',
      lower_is_better: true,
      ...STAMPS,
    },
  ],
};

const experiment = (overrides: Partial<Experiment> = {}): Experiment => ({ ...FULL, ...overrides });

/** A stored item as the body sends it back: the server's own fields gone. */
function asSent<T extends { id: string; experiment_id?: string; created_at?: string; updated_at?: string }>(item: T) {
  const { id, experiment_id, created_at, updated_at, ...rest } = item;
  return rest;
}

const as = (role: string, extra: Record<string, unknown> = {}) => ({
  id: 'user-1',
  email: `${role.toLowerCase()}@demo.com`,
  username: role.toLowerCase(),
  role,
  is_superuser: false,
  ...extra,
});

function signIn(user: Record<string, unknown>) {
  mockUseAuth.mockReturnValue({ user, status: 'authenticated' });
}

/** Text the server might send, which must never reach the page. */
const PLANTED = 'Traceback (most recent call last): sqlalchemy.exc.PLANTED-1453';

type Put = (body: Record<string, unknown>) => unknown;

/** The answer a real PUT gives: the lists as sent, with new ids. */
const stored = (current: Experiment, body: Record<string, unknown>): Experiment => ({
  ...current,
  variants: ((body.variants as object[]) ?? []).map((v, i) => ({ ...(v as object), id: `v-new-${i}`, ...STAMPS })) as Experiment['variants'],
  metrics: ((body.metrics as object[]) ?? []).map((m, i) => ({ ...(m as object), id: `m-new-${i}`, ...STAMPS })) as Experiment['metrics'],
});

function install(current: Experiment, put?: Put, get?: () => unknown) {
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/experiments/exp-1', handler: () => (get ? get() : current) },
      {
        method: 'PUT',
        path: '/api/v1/experiments/exp-1',
        handler: (_p, o) => {
          const body = o.json as Record<string, unknown>;
          return put ? put(body) : stored(current, body);
        },
      },
      // #1452's rows on the same page: the eligibility reads, for ADMIN and DEVELOPER.
      { path: '/api/v1/holdout', handler: () => null },
    ]) as unknown as typeof apiFetch,
  );
}

async function renderPage(current: Experiment = experiment(), put?: Put) {
  install(current, put);
  const view = render(<ExperimentDetailPage />);
  await screen.findByTestId('experiment-detail');
  return view;
}

const callsTo = (method: string) =>
  mockedApiFetch.mock.calls.filter(([, o]) => (o?.method ?? 'GET').toUpperCase() === method);
const puts = () => callsTo('PUT');
const putBody = (n = 0) => puts()[n][1]?.json as { variants: Record<string, unknown>[]; metrics: Record<string, unknown>[] };

async function openDesign(current: Experiment = experiment(), put?: Put) {
  await renderPage(current, put);
  fireEvent.click(screen.getByTestId('experiment-edit-variants'));
  return screen.getByTestId('design-form');
}

const type = (id: string, value: string) => fireEvent.change(screen.getByTestId(id), { target: { value } });
const save = () => fireEvent.click(screen.getByTestId('design-save'));

let confirmSpy: jest.SpyInstance;

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockRouter.push.mockClear();
  signIn(as('ADMIN'));
  confirmSpy = jest.spyOn(window, 'confirm').mockImplementation(() => true);
});

afterEach(() => {
  expect(confirmSpy).not.toHaveBeenCalled();
  confirmSpy.mockRestore();
});

// ---------------------------------------------------------------------------
// Who is offered it, on which experiment (QA #1, #2, #9, #27)
// ---------------------------------------------------------------------------

describe('who is offered Edit variants and metrics', () => {
  it.each<[string, Record<string, unknown>]>([
    ['ADMIN', as('ADMIN')],
    ['DEVELOPER', as('DEVELOPER')],
    ['superuser VIEWER', as('VIEWER', { is_superuser: true })],
  ])('%s gets it on a draft, after Edit details', async (_n, user) => {
    signIn(user);
    await renderPage();
    const button = screen.getByTestId('experiment-edit-variants');
    expect(button).toHaveTextContent(DESIGN_HEADING);
    const ids = Array.from(
      within(screen.getByTestId('experiment-manage')).getAllByRole('button'),
    ).map((b) => b.dataset.testid);
    expect(ids).toEqual(['experiment-clone', 'experiment-edit-details', 'experiment-edit-variants', 'experiment-delete']);
  });

  it.each(['ANALYST', 'VIEWER'])('%s is offered nothing, and the page sends no change', async (role) => {
    signIn(as(role));
    await renderPage();
    expect(screen.queryByTestId('experiment-edit-variants')).toBeNull();
    expect(screen.queryByTestId('design-form')).toBeNull();
    // The read-only cards are what they read.
    expect(screen.getByTestId('variants-table')).toBeInTheDocument();
    expect(mockedApiFetch.mock.calls.filter(([, o]) => (o?.method ?? 'GET') !== 'GET')).toEqual([]);
    expect(callsTo('GET').map(([p]) => p)).toEqual(['/api/v1/experiments/exp-1']);
  });

  const LATER: ExperimentStatus[] = ['active', 'paused', 'completed', 'archived'];
  it.each(LATER)('a superuser is not offered it on a %s experiment', async (status) => {
    signIn(as('ADMIN', { is_superuser: true }));
    await renderPage(experiment({ status }));
    expect(screen.queryByTestId('experiment-edit-variants')).toBeNull();
    expect(screen.getByTestId('variants-table')).toBeInTheDocument();
  });

  it('is absent while the experiment loads, and when it cannot be loaded', async () => {
    let release: (value: Experiment) => void = () => undefined;
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          path: '/api/v1/experiments/exp-1',
          handler: () => new Promise<Experiment>((resolve) => (release = resolve)),
        },
        { path: '/api/v1/holdout', handler: () => null },
      ]) as unknown as typeof apiFetch,
    );
    const view = render(<ExperimentDetailPage />);
    expect(await screen.findByTestId('experiment-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('experiment-edit-variants')).toBeNull();
    release(experiment());
    expect(await screen.findByTestId('experiment-edit-variants')).toBeInTheDocument();
    view.unmount();

    install(experiment(), undefined, () => {
      throw apiError(500, PLANTED);
    });
    render(<ExperimentDetailPage />);
    expect(await screen.findByTestId('experiment-error')).toBeInTheDocument();
    expect(screen.queryByTestId('experiment-edit-variants')).toBeNull();
  });

  it('a draft that starts while the form is open loses the form, and the cards come back', async () => {
    await renderPage();
    mockedApiFetch.mockImplementation(
      routedApi([
        { path: '/api/v1/experiments/exp-1', handler: () => experiment() },
        { method: 'POST', path: '/api/v1/experiments/exp-1/start', handler: () => experiment({ status: 'active' }) },
        { path: '/api/v1/holdout', handler: () => null },
      ]) as unknown as typeof apiFetch,
    );
    fireEvent.click(screen.getByTestId('experiment-edit-variants'));
    expect(screen.queryByTestId('experiment-design-cards')).toBeNull();
    // The page takes the answer of a lifecycle action as the experiment.
    fireEvent.click(screen.getByTestId('action-start'));
    await waitFor(() => expect(screen.getByTestId('experiment-status')).toHaveTextContent('Active'));
    expect(screen.queryByTestId('design-form')).toBeNull();
    expect(screen.getByTestId('experiment-design-cards')).toBeInTheDocument();
    expect(puts()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// The form
// ---------------------------------------------------------------------------

describe('the form', () => {
  it('stands in for the read-only cards, seeded with every variant and metric', async () => {
    await openDesign();
    expect(screen.queryByTestId('experiment-design-cards')).toBeNull();
    expect(screen.queryByTestId('variants-table')).toBeNull();
    const form = screen.getByTestId('design-form');
    expect(within(form).getByRole('heading', { level: 2, name: DESIGN_HEADING })).toBeInTheDocument();
    expect(screen.getByTestId('design-note')).toHaveTextContent(DESIGN_NOTE);
    expect([0, 1, 2].map((i) => (screen.getByTestId(`variant-name-${i}`) as HTMLInputElement).value)).toEqual([
      'Control',
      'Blue CTA',
      'Green CTA',
    ]);
    expect([0, 1, 2].map((i) => (screen.getByTestId(`variant-allocation-${i}`) as HTMLInputElement).value)).toEqual([
      '34',
      '33',
      '33',
    ]);
    expect(screen.getByTestId('variant-control-0')).toBeChecked();
    // A stored configuration opens as indented JSON.
    expect(screen.getByTestId('variant-configuration-1')).toHaveValue('{\n  "color": "blue"\n}');
    expect((screen.getByTestId('metric-name-1') as HTMLInputElement).value).toBe('Errors');
    expect(screen.getByTestId('metric-type-0')).toHaveValue('revenue');
    expect(screen.getByTestId('metric-primary-0')).toBeChecked();
    expect(screen.getByTestId('metric-lower-is-better-1')).toBeChecked();
    expect(screen.queryByTestId('design-sequential-note')).toBeNull();
    expect(puts()).toEqual([]);
  });

  it('says which treatment sequential testing compares, when it is on', async () => {
    await openDesign(experiment({ sequential_testing_enabled: true }));
    expect(screen.getByTestId('design-sequential-note')).toHaveTextContent(DESIGN_SEQUENTIAL_NOTE);
  });

  it('Cancel sends nothing, brings the cards back and returns focus to the button', async () => {
    await openDesign();
    type('variant-name-1', 'Changed');
    fireEvent.click(screen.getByTestId('design-cancel'));
    expect(screen.queryByTestId('design-form')).toBeNull();
    expect(screen.getByTestId('variants-table')).toBeInTheDocument();
    expect(screen.getByTestId('experiment-edit-variants')).toHaveFocus();
    expect(puts()).toEqual([]);
  });

  it('nothing changed sends nothing and closes the form', async () => {
    await openDesign();
    save();
    await waitFor(() => expect(screen.queryByTestId('design-form')).toBeNull());
    expect(puts()).toEqual([]);
    expect(screen.getByTestId('experiment-edit-variants')).toHaveFocus();
  });

  it('saves, says so, and the cards show the answer', async () => {
    await openDesign(experiment(), (body) => ({
      ...stored(FULL, body),
      variants: stored(FULL, body).variants.map((v, i) => (i === 1 ? { ...v, name: 'As the server stored it' } : v)),
    }));
    type('variant-name-1', 'Blue button');
    save();
    expect(screen.getByTestId('design-save')).toHaveTextContent('Saving…');
    const status = await screen.findByTestId('design-saved');
    expect(status).toHaveTextContent(DESIGN_SAVED);
    expect(status).toHaveAttribute('role', 'status');
    expect(screen.queryByTestId('design-form')).toBeNull();
    expect(screen.getByTestId('variants-table')).toHaveTextContent('As the server stored it');
    expect(screen.getByTestId('experiment-edit-variants')).toHaveFocus();
    expect(puts()).toHaveLength(1);
  });

  it('the details form now points to this one', async () => {
    await renderPage();
    fireEvent.click(screen.getByTestId('experiment-edit-details'));
    const form = screen.getByTestId('manage-edit-form');
    expect(form).toHaveTextContent(EDIT_NOTE);
    expect(form.textContent).not.toMatch(/delete this draft|create it again|cannot/i);
  });
});

// ---------------------------------------------------------------------------
// The body: exactly {variants, metrics}, every item, every field (QA #5, #6)
// ---------------------------------------------------------------------------

describe('the body', () => {
  it('is exactly the two lists, both complete, every time (QA #5)', async () => {
    await openDesign();
    // One variant of three renamed; neither metric touched.
    type('variant-name-1', 'Blue button');
    save();
    await screen.findByTestId('design-saved');
    expect(puts()).toHaveLength(1);
    const [url, options] = puts()[0];
    expect(url).toBe('/api/v1/experiments/exp-1');
    expect(Object.keys(options as ApiFetchOptions)).toEqual(['method', 'json']);
    const body = putBody();
    expect(Object.keys(body)).toEqual(['variants', 'metrics']);
    expect(body.variants.map((v) => v.name)).toEqual(['Control', 'Blue button', 'Green CTA']);
    expect(body.variants).toEqual(
      FULL.variants.map((v, i) => (i === 1 ? { ...asSent(v), name: 'Blue button' } : asSent(v))),
    );
    expect(body.metrics).toEqual(FULL.metrics.map(asSent));
  });

  it('a second save sends both lists whole again', async () => {
    await openDesign();
    type('variant-name-2', 'Green button');
    save();
    await screen.findByTestId('design-saved');
    fireEvent.click(screen.getByTestId('experiment-edit-variants'));
    type('metric-name-1', 'Client errors');
    save();
    await waitFor(() => expect(puts()).toHaveLength(2));
    expect(putBody(1).variants).toHaveLength(3);
    expect(putBody(1).metrics.map((m) => m.name)).toEqual(['Revenue', 'Client errors']);
  });

  describe('every field of each item, against the OpenAPI snapshots (QA #6)', () => {
    const properties = (file: string, schema: string): string[] => {
      const doc = JSON.parse(
        fs.readFileSync(path.join(__dirname, '../../../../docs/api', file), 'utf-8'),
      ) as { components: { schemas: Record<string, { properties: Record<string, unknown> }> } };
      return Object.keys(doc.components.schemas[schema].properties).sort();
    };

    it.each(['openapi-v1.full.json', 'openapi-v1.stable.json'])(
      'each item carries exactly the %s VariantBase and MetricBase properties, as stored',
      async (file) => {
        await openDesign();
        type('variant-allocation-1', '32');
        type('variant-allocation-2', '34');
        save();
        await screen.findByTestId('design-saved');
        const body = putBody();
        for (const v of body.variants) expect(Object.keys(v).sort()).toEqual(properties(file, 'VariantBase'));
        for (const m of body.metrics) expect(Object.keys(m).sort()).toEqual(properties(file, 'MetricBase'));
        expect(body.variants).toEqual(
          FULL.variants.map((v, i) =>
            i === 0 ? asSent(v) : { ...asSent(v), traffic_allocation: i === 1 ? 32 : 34 },
          ),
        );
        expect(body.metrics).toEqual(FULL.metrics.map(asSent));
      },
    );
  });

  it('a new primary metric takes the planned effect, and the old one gives it up (C7)', async () => {
    await openDesign();
    fireEvent.click(screen.getByTestId('metric-primary-1'));
    save();
    await screen.findByTestId('design-saved');
    expect(putBody().metrics).toEqual([
      { ...asSent(FULL.metrics[0]), is_primary: false, expected_effect: null },
      { ...asSent(FULL.metrics[1]), is_primary: true, expected_effect: 0.1 },
    ]);
  });
});

// ---------------------------------------------------------------------------
// Refused before sending: zero PUTs, guided setup's words, focus on the field (QA #8)
// ---------------------------------------------------------------------------

describe('refused before sending', () => {
  async function expectRefused(message: string, focused: string) {
    save();
    const alert = await screen.findByTestId('design-error');
    expect(alert).toHaveTextContent(message);
    expect(alert).toHaveAttribute('role', 'alert');
    await waitFor(() => expect(screen.getByTestId(focused)).toHaveFocus());
    expect(puts()).toEqual([]);
    expect(screen.getByTestId('design-form')).toBeInTheDocument();
  }

  const seeded = (current: Experiment) => formStateFromExperiment(current);

  it('one variant: an experiment needs two to start', async () => {
    const two = experiment({ variants: [{ ...FULL.variants[0], traffic_allocation: 50 }, { ...FULL.variants[1], traffic_allocation: 50 }] });
    await openDesign(two);
    fireEvent.click(screen.getByTestId('remove-variant-1'));
    type('variant-allocation-0', '100');
    await expectRefused(TWO_VARIANTS, 'add-variant');
  });

  it('a draft stored with one variant is refused the same way', async () => {
    await openDesign(experiment({ variants: [{ ...FULL.variants[0], traffic_allocation: 100 }] }));
    await expectRefused(TWO_VARIANTS, 'add-variant');
  });

  it('a split that does not add up to 100', async () => {
    await openDesign();
    type('variant-allocation-2', '30');
    const state = seeded(FULL);
    state.variants[2].traffic_allocation = 30;
    await expectRefused(checkVariants(state.variants) as string, 'variant-allocation-0');
    expect(screen.getByTestId('design-error')).toHaveTextContent('Variant allocations must add up to 100% (currently 97%).');
  });

  it('a blank variant name', async () => {
    await openDesign();
    type('variant-name-2', '  ');
    await expectRefused('Every variant needs a name.', 'variant-name-2');
  });

  it('no control, as stored', async () => {
    await openDesign(experiment({ variants: FULL.variants.map((v) => ({ ...v, is_control: false })) }));
    await expectRefused('One variant must be marked as control.', 'variant-control-0');
  });

  it('two controls, as stored', async () => {
    await openDesign(experiment({ variants: FULL.variants.map((v, i) => ({ ...v, is_control: i < 2 })) }));
    await expectRefused(ONE_CONTROL, 'variant-control-0');
  });

  it('a configuration that is not a JSON object', async () => {
    await openDesign();
    type('variant-configuration-2', '["green"]');
    save();
    expect(await screen.findByTestId('design-error')).toHaveTextContent(
      'The configuration of “Green CTA” needs fixing: The configuration must be a JSON object in braces',
    );
    await waitFor(() => expect(screen.getByTestId('variant-configuration-2')).toHaveFocus());
    expect(puts()).toEqual([]);
  });

  it('no metric, as stored', async () => {
    await openDesign(experiment({ metrics: [] }));
    await expectRefused(checkMetrics([]) as string, 'add-metric');
    expect(screen.getByTestId('design-error')).toHaveTextContent('Add at least one metric.');
  });

  it('a blank event name', async () => {
    await openDesign();
    type('metric-event-1', '');
    await expectRefused('Every metric needs a name and an event name.', 'metric-event-1');
  });

  it('a repeated metric name', async () => {
    await openDesign();
    type('metric-name-1', 'Revenue ');
    await expectRefused('Metric names must be unique.', 'metric-name-1');
  });

  it('no primary metric, as stored', async () => {
    await openDesign(experiment({ metrics: FULL.metrics.map((m) => ({ ...m, is_primary: false })) }));
    await expectRefused('Choose a primary metric.', 'metric-primary-0');
  });

  it('two primary metrics, as stored', async () => {
    await openDesign(experiment({ metrics: FULL.metrics.map((m) => ({ ...m, is_primary: true })) }));
    await expectRefused(ONE_PRIMARY, 'metric-primary-0');
  });

  it('fixing the field clears its mark, and the save then goes through', async () => {
    await openDesign();
    type('variant-name-2', '');
    save();
    await screen.findByTestId('design-error');
    expect(screen.getByTestId('variant-name-2')).toHaveAttribute('aria-invalid', 'true');
    type('variant-name-2', 'Green button');
    expect(screen.queryByTestId('design-error')).toBeNull();
    expect(screen.getByTestId('variant-name-2')).not.toHaveAttribute('aria-invalid');
    save();
    await screen.findByTestId('design-saved');
    expect(puts()).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// Refused by the server: fixed copy per status, never its detail
// ---------------------------------------------------------------------------

describe('a refused save shows fixed copy, never the server text', () => {
  const failing = (err: unknown) => () => {
    throw err;
  };

  type Case = [string, unknown, string];
  const cases: Case[] = [
    ...[400, 403, 404, 422].map((s): Case => [`${s}`, apiError(s, PLANTED), ERROR_COPY.design[s]]),
    ['500 with a JSON body', new ApiError({ status: 500, detail: PLANTED }), ERROR_COPY.design.fallback],
    ['status 0', new ApiError({ status: 0, detail: PLANTED, message: PLANTED }), UNREACHABLE],
    ['a browser exception', new TypeError(`Cannot read properties of undefined (reading 'x') ${PLANTED}`), ERROR_COPY.design.fallback],
  ];

  it.each(cases)('%s keeps what was typed and focuses Save', async (_n, err, copy) => {
    await openDesign(experiment(), failing(err));
    type('variant-name-1', 'Typed name');
    save();
    const alert = await screen.findByTestId('design-error');
    expect(alert).toHaveTextContent(copy);
    expect(alert).toHaveAttribute('role', 'alert');
    expect(document.body.textContent).not.toContain('PLANTED');
    expect(document.body.textContent).not.toContain('Cannot read');
    expect(screen.getByTestId('variant-name-1')).toHaveValue('Typed name');
    expect(screen.queryByTestId('design-saved')).toBeNull();
    await waitFor(() => expect(screen.getByTestId('design-save')).toHaveFocus());
    expect(screen.getByTestId('design-save')).not.toBeDisabled();
  });

  it('the copy for each status is the one the plan fixed', () => {
    expect(ERROR_COPY.design).toEqual({
      400:
        'This experiment is no longer a draft, so its variants and metrics cannot be changed. ' +
        'Reload the page to see its status.',
      403: 'Changing an experiment requires the ADMIN or DEVELOPER role.',
      404: 'This experiment no longer exists.',
      422:
        'The variants and metrics were not accepted. Check that the split adds up to 100%, one ' +
        'variant is the control, and every metric has a name and an event name.',
      fallback: 'Could not save the variants and metrics. Try again.',
    });
  });
});

// ---------------------------------------------------------------------------
// Accessibility (QA #28). Scoped to the region that holds the form.
// ---------------------------------------------------------------------------

describe('accessibility', () => {
  async function noViolations() {
    const region = screen.getByTestId('experiment-manage');
    const result = await axe.run(region, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toEqual([]);
  }

  it('in every state: open, changed, refused, saving, refused by the server, saved', async () => {
    let fail = true;
    let finish: (value: unknown) => void = () => undefined;
    await openDesign(experiment(), (body) =>
      fail
        ? (() => {
            throw apiError(500, PLANTED);
          })()
        : new Promise((resolve) => (finish = () => resolve(stored(FULL, body)))),
    );
    expect(screen.getByRole('heading', { name: DESIGN_HEADING })).toHaveFocus();
    await noViolations();

    type('variant-name-1', 'Blue button');
    await noViolations();

    type('variant-name-2', '');
    save();
    await screen.findByTestId('design-error');
    await noViolations();

    type('variant-name-2', 'Green button');
    save();
    await screen.findByTestId('design-error');
    await noViolations();

    fail = false;
    save();
    await waitFor(() => expect(screen.getByTestId('design-save')).toHaveTextContent('Saving…'));
    await noViolations();
    finish(undefined);
    await screen.findByTestId('design-saved');
    await noViolations();
  });

  it('a refused text field is invalid and described by the alert', async () => {
    await openDesign();
    type('metric-event-0', ' ');
    save();
    const alert = await screen.findByTestId('design-error');
    const field = screen.getByTestId('metric-event-0');
    expect(field).toHaveAttribute('aria-invalid', 'true');
    expect(field.getAttribute('aria-describedby')?.split(' ')).toContain(alert.id);
    expect(alert.id).not.toBe('');
  });

  it('a refused radio is described by the alert, and is not marked invalid', async () => {
    await openDesign(experiment({ metrics: FULL.metrics.map((m) => ({ ...m, is_primary: false })) }));
    save();
    const alert = await screen.findByTestId('design-error');
    const radio = screen.getByTestId('metric-primary-0');
    expect(radio.getAttribute('aria-describedby')).toBe(alert.id);
    expect(radio).not.toHaveAttribute('aria-invalid');
  });

  it('the control and the primary radios sit in a fieldset with a legend', async () => {
    await openDesign();
    const legendOf = (id: string) => screen.getByTestId(id).closest('fieldset')?.querySelector('legend')?.textContent;
    expect(legendOf('variant-control-0')).toBe('Variants');
    expect(legendOf('metric-primary-0')).toBe('Metrics');
  });
});
