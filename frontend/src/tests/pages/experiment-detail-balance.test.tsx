/**
 * "Balance the split within groups" on the experiment page (#448): the switch
 * in a draft's "Edit variants and metrics", a text line for a draft that
 * cannot balance, the row on the page (read-only once the experiment has
 * started), and the API's refusals shown at their fields.
 *
 * Texts are written out here, not imported, so a reworded sentence fails a
 * test instead of following the code.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import ExperimentDetailPage from '@/pages/experiments/[id]';
import { ApiError, apiFetch } from '@/services/api';
import { Experiment, ExperimentStatus } from '@/types/experiments';
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

const SWITCH_LABEL = 'Balance the split within groups';
const STEPS_REFUSAL =
  'To balance within groups, every share must be a multiple of 5, such as 50/50, 20/80, 35/35/30 or 40/30/30; 34/33/33 is not.';
const KEY_REFUSAL =
  "Balance on a top-level key of the user's attributes, made of letters, digits, _ and - (up to 100 characters), other than user_id and segment.";
const SPLIT_URL_REFUSAL = 'Split URL experiments cannot balance within groups: the edge router sends no user attributes.';
const BANDIT_LINE =
  'A bandit experiment cannot balance its split within groups, because it moves traffic between variants as results come in.';

const SEG_A = '8d4c6f0e-3a1b-4c2d-9e8f-7a6b5c4d3e2f';
const SEG_B = '1f2e3d4c-5b6a-4978-8a9b-0c1d2e3f4a5b';
const SEGMENTS = [
  { id: SEG_A, name: 'High value', kind: 'id_list', rules: null, status: 'active', created_at: '', updated_at: '' },
  { id: SEG_B, name: 'Churn risk', kind: 'id_list', rules: null, status: 'active', created_at: '', updated_at: '' },
];

const STAMPS = { experiment_id: 'exp-1', created_at: '2026-10-01T10:00:00', updated_at: '2026-10-01T10:00:00' };

type WithBalance = Experiment & { balance_within?: unknown };

const BASE: WithBalance = {
  id: 'exp-1',
  name: 'Checkout button colour',
  key: 'checkout-button-colour',
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
  sequential_testing_enabled: false,
  optimization_type: 'fixed',
  balance_within: null,
  variants: [
    { id: 'v-1', name: 'Control', description: null, is_control: true, traffic_allocation: 50, configuration: null, ...STAMPS },
    { id: 'v-2', name: 'Blue', description: null, is_control: false, traffic_allocation: 50, configuration: null, ...STAMPS },
  ],
  metrics: [
    {
      id: 'm-1',
      name: 'Revenue',
      description: null,
      event_name: 'purchase',
      metric_type: 'revenue',
      is_primary: true,
      aggregation_method: 'sum',
      minimum_sample_size: 100,
      expected_effect: null,
      event_value_path: null,
      lower_is_better: false,
      ...STAMPS,
    },
  ],
};

const experiment = (overrides: Partial<WithBalance> = {}): WithBalance => ({ ...BASE, ...overrides });
const BALANCED = { attribute: 'platform', values: ['ios', 'android', 'web'] };

function signIn() {
  mockUseAuth.mockReturnValue({
    user: { id: 'user-1', email: 'a@demo.com', username: 'a', role: 'ADMIN', is_superuser: false },
    status: 'authenticated',
  });
}

type Put = (body: Record<string, unknown>) => unknown;

function install(current: WithBalance, put?: Put) {
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/experiments/exp-1', handler: () => current },
      {
        method: 'PUT',
        path: '/api/v1/experiments/exp-1',
        handler: (_p, o) => {
          const body = o.json as Record<string, unknown>;
          return put ? put(body) : { ...current, balance_within: body.balance_within ?? current.balance_within };
        },
      },
      { path: '/api/v1/holdout', handler: () => null },
      { path: '/api/v1/segments', handler: () => SEGMENTS },
    ]) as unknown as typeof apiFetch,
  );
}

async function renderPage(current: WithBalance = experiment(), put?: Put) {
  install(current, put);
  const view = render(<ExperimentDetailPage />);
  await screen.findByTestId('experiment-detail');
  return view;
}

async function openDesign(current: WithBalance = experiment(), put?: Put) {
  await renderPage(current, put);
  fireEvent.click(screen.getByTestId('experiment-edit-variants'));
  return screen.getByTestId('design-form');
}

const puts = () => mockedApiFetch.mock.calls.filter(([, o]) => (o?.method ?? 'GET').toUpperCase() === 'PUT');
const putBody = () => puts()[0][1]?.json as Record<string, unknown>;
const set = (testId: string, value: string) => fireEvent.change(screen.getByTestId(testId), { target: { value } });
const save = () => fireEvent.click(screen.getByTestId('design-save'));
const addValue = (value: string) => {
  fireEvent.change(screen.getByLabelText('Values to keep apart (up to 20)'), { target: { value } });
  fireEvent.click(screen.getByTestId('balance-value-add'));
};

const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };
async function violations(node: Element) {
  const result = await axe.run(node, axeOptions);
  return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join('; ')}`);
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  signIn();
});

describe('the switch in Edit variants and metrics', () => {
  it('a fixed-split draft has it, off, with its label', async () => {
    await openDesign();
    const toggle = screen.getByLabelText(SWITCH_LABEL);
    expect(toggle).not.toBeChecked();
    expect(screen.queryByTestId('balance-unavailable')).toBeNull();
  });

  it('a draft that already balances shows it on, with its key and values', async () => {
    await openDesign(experiment({ balance_within: BALANCED }));
    expect(screen.getByLabelText(SWITCH_LABEL)).toBeChecked();
    expect(screen.getByLabelText('Group users by')).toHaveValue('platform');
    expect(screen.getByRole('button', { name: 'Remove value android' })).toBeInTheDocument();
  });

  it('turning it on and saving sends the setting with the lists', async () => {
    await openDesign();
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    addValue('ios');
    addValue('android');
    save();
    await waitFor(() => expect(puts()).toHaveLength(1));
    const body = putBody();
    expect(Object.keys(body).sort()).toEqual(['balance_within', 'metrics', 'variants']);
    expect(body.balance_within).toEqual({ attribute: 'platform', values: ['ios', 'android'] });
  });

  it('saving with the setting unchanged sends no balance_within, and with nothing changed sends nothing', async () => {
    await openDesign(experiment({ balance_within: BALANCED }));
    save();
    await waitFor(() => expect(screen.queryByTestId('design-form')).toBeNull());
    expect(puts()).toEqual([]);
    fireEvent.click(screen.getByTestId('experiment-edit-variants'));
    set('variant-name-1', 'Green');
    save();
    await waitFor(() => expect(puts()).toHaveLength(1));
    expect('balance_within' in putBody()).toBe(false);
  });

  it('turning it off sends null', async () => {
    await openDesign(experiment({ balance_within: BALANCED }));
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    save();
    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(putBody().balance_within).toBeNull();
  });

  it('a stored custom key and a stored closed key come back as they were', async () => {
    await openDesign(experiment({ balance_within: { attribute: 'tier', values: ['gold'] } }));
    expect(screen.getByLabelText('Group users by')).toHaveValue('custom');
    expect(screen.getByTestId('balance-custom-key')).toHaveValue('tier');
    save();
    await waitFor(() => expect(screen.queryByTestId('design-form')).toBeNull());
    expect(puts()).toEqual([]);
  });

  it('stored segments show by name', async () => {
    await openDesign(experiment({ balance_within: { segments: [SEG_B, SEG_A] } }));
    expect(screen.getByLabelText('Group users by')).toHaveValue('segments');
    await waitFor(() => expect(screen.getByTestId('balance-segment-0')).toHaveTextContent('Churn risk'));
    expect(screen.getByTestId('balance-segment-1')).toHaveTextContent('High value');
    expect(screen.getByTestId('balance-within').textContent).not.toContain(SEG_A);
  });

  it('a bandit draft gets a text line, not a control, and sends no setting', async () => {
    await openDesign(experiment({ optimization_type: 'thompson_sampling' }));
    expect(screen.queryByLabelText(SWITCH_LABEL)).toBeNull();
    expect(screen.queryByRole('checkbox', { name: /Balance/ })).toBeNull();
    expect(screen.getByTestId('balance-unavailable').textContent).toBe(BANDIT_LINE);
    set('variant-name-1', 'Green');
    save();
    await waitFor(() => expect(puts()).toHaveLength(1));
    expect('balance_within' in putBody()).toBe(false);
  });

  it('a bandit-type draft gets the same line', async () => {
    await openDesign(experiment({ experiment_type: 'bandit' }));
    expect(screen.getByTestId('balance-unavailable').textContent).toBe(BANDIT_LINE);
    expect(screen.queryByLabelText(SWITCH_LABEL)).toBeNull();
  });

  it('a split URL draft gets the sentence the API gives for it', async () => {
    await openDesign(experiment({ experiment_type: 'split_url' }));
    expect(screen.getByTestId('balance-unavailable').textContent).toBe(SPLIT_URL_REFUSAL);
    expect(screen.queryByLabelText(SWITCH_LABEL)).toBeNull();
  });

  it.each<ExperimentStatus>(['active', 'paused', 'completed', 'archived'])(
    'a %s experiment has no switch anywhere: the setting is read-only',
    async (status) => {
      await renderPage(experiment({ status, balance_within: BALANCED }));
      expect(screen.queryByLabelText(SWITCH_LABEL)).toBeNull();
      expect(screen.queryByTestId('experiment-edit-variants')).toBeNull();
      expect(screen.getByTestId('experiment-balance')).toHaveTextContent('within Platform (ios, android, web)');
    },
  );
});

describe('the row on the page', () => {
  it('says off for an experiment that does not balance', async () => {
    await renderPage(experiment({ balance_within: null }));
    expect(screen.getByTestId('experiment-balance').parentElement).toHaveTextContent('Balance');
    expect(screen.getByTestId('experiment-balance').textContent).toBe('off');
  });

  it('names the key and its values', async () => {
    await renderPage(experiment({ balance_within: BALANCED }));
    const row = screen.getByTestId('experiment-balance');
    expect(row.parentElement).toHaveTextContent('Balance');
    expect(row).toHaveTextContent('within Platform (ios, android, web)');
  });

  it('names a custom key as typed, and a closed key without values', async () => {
    await renderPage(experiment({ balance_within: { attribute: 'tier', values: ['gold', 'silver'] } }));
    expect(screen.getByTestId('experiment-balance')).toHaveTextContent('within tier (gold, silver)');
  });

  it('a closed key lists no values', async () => {
    await renderPage(experiment({ balance_within: { attribute: 'web_device' } }));
    expect(screen.getByTestId('experiment-balance').textContent).toContain('within web_device');
    expect(screen.getByTestId('experiment-balance').textContent).not.toContain('(');
  });

  it('names segments, never their ids', async () => {
    await renderPage(experiment({ balance_within: { segments: [SEG_A, SEG_B] } }));
    await waitFor(() =>
      expect(screen.getByTestId('experiment-balance')).toHaveTextContent('within segments High value, Churn risk'),
    );
    expect(screen.getByTestId('experiment-balance').textContent).not.toContain(SEG_A);
  });

  it('while the segment names load, it counts them instead of showing ids', async () => {
    await renderPage(experiment({ balance_within: { segments: [SEG_A, SEG_B] }, status: 'active' }));
    expect(screen.getByTestId('experiment-balance').textContent).not.toContain(SEG_B);
  });

  it('shows no row when the server did not answer the field at all (an older server)', async () => {
    const { balance_within: _unused, ...older } = experiment();
    await renderPage(older as Experiment);
    expect(screen.queryByTestId('experiment-balance')).toBeNull();
  });

  it('makes no request for segments unless the experiment balances within them', async () => {
    await renderPage(experiment({ balance_within: BALANCED }));
    expect(mockedApiFetch.mock.calls.map(([p]) => p)).not.toContain('/api/v1/segments');
  });
});

describe('refusals at their fields', () => {
  it('a split that is not in steps of 5 shows at the split as it is typed, and Save sends nothing', async () => {
    await openDesign(experiment({ balance_within: BALANCED }));
    fireEvent.click(screen.getByTestId('add-variant'));
    set('variant-allocation-0', '34');
    set('variant-allocation-1', '33');
    set('variant-allocation-2', '33');
    const line = screen.getByTestId('balance-split-error');
    expect(line.textContent).toBe(STEPS_REFUSAL);
    expect(screen.getByTestId('variant-allocation-0')).toHaveAttribute('aria-describedby', line.id);
    save();
    expect(puts()).toEqual([]);
    expect(screen.getByTestId('design-error')).toHaveTextContent(STEPS_REFUSAL);
    expect(screen.getByTestId('variant-allocation-0')).toHaveFocus();
  });

  it('with the switch off, a split of 34/33/33 is saved as before', async () => {
    await openDesign();
    fireEvent.click(screen.getByTestId('add-variant'));
    set('variant-allocation-0', '34');
    set('variant-allocation-1', '33');
    set('variant-allocation-2', '33');
    expect(screen.queryByTestId('balance-split-error')).toBeNull();
    save();
    await waitFor(() => expect(puts()).toHaveLength(1));
  });

  it('a 422 on the attribute shows verbatim at the key, with focus there, and Save stays usable', async () => {
    await openDesign(
      experiment(),
      () => {
        throw new ApiError({
          status: 422,
          detail: [{ loc: ['body', 'balance_within', 'attribute'], msg: KEY_REFUSAL, type: 'value_error' }],
        });
      },
    );
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'custom');
    set('balance-custom-key', 'a b');
    save();
    const alert = await screen.findByTestId('balance-error');
    expect(alert.textContent).toBe(KEY_REFUSAL);
    expect(screen.getByTestId('balance-custom-key')).toHaveFocus();
    expect(screen.getByTestId('design-save')).not.toBeDisabled();
    expect(screen.queryByTestId('design-error')).toBeNull();
  });

  it('a 422 on another field keeps the dashboard\'s own sentence, and the setting stays as typed', async () => {
    await openDesign(experiment(), () => {
      throw new ApiError({ status: 422, detail: [{ loc: ['body', 'variants'], msg: 'bad', type: 'value_error' }] });
    });
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    addValue('ios');
    save();
    expect(await screen.findByTestId('design-error')).toHaveTextContent('The variants and metrics were not accepted.');
    expect(screen.queryByTestId('balance-error')).toBeNull();
    expect(screen.getByLabelText(SWITCH_LABEL)).toBeChecked();
  });

  it('the text goes when the setting changes', async () => {
    await openDesign(experiment(), () => {
      throw new ApiError({
        status: 422,
        detail: [{ loc: ['body', 'balance_within', 'attribute'], msg: KEY_REFUSAL, type: 'value_error' }],
      });
    });
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'custom');
    set('balance-custom-key', 'a b');
    save();
    await screen.findByTestId('balance-error');
    set('balance-custom-key', 'ab');
    expect(screen.queryByTestId('balance-error')).toBeNull();
  });
});

describe('the values belong to the key they were listed for', () => {
  it('changing "Group users by" removes the chips, and Save sends an empty list', async () => {
    await openDesign(experiment({ balance_within: BALANCED }));
    expect(screen.getByTestId('balance-values')).toBeInTheDocument();
    set('balance-group-by', 'country');
    expect(screen.queryByTestId('balance-values')).toBeNull();
    expect(screen.queryByRole('button', { name: /Remove value/ })).toBeNull();
    save();
    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(putBody().balance_within).toEqual({ attribute: 'country', values: [] });
  });

  it('settling on another custom key removes the chips, and Save sends an empty list', async () => {
    await openDesign(experiment({ balance_within: { attribute: 'tier', values: ['gold', 'silver'] } }));
    const key = screen.getByTestId('balance-custom-key');
    fireEvent.focus(key);
    set('balance-custom-key', 'plan');
    fireEvent.blur(key);
    expect(screen.queryByTestId('balance-values')).toBeNull();
    save();
    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(putBody().balance_within).toEqual({ attribute: 'plan', values: [] });
  });

  it('Enter settles the custom key the same way', async () => {
    await openDesign(experiment({ balance_within: { attribute: 'tier', values: ['gold'] } }));
    const key = screen.getByTestId('balance-custom-key');
    fireEvent.focus(key);
    set('balance-custom-key', 'plan');
    expect(screen.getByTestId('balance-values')).toBeInTheDocument();
    fireEvent.keyDown(key, { key: 'Enter' });
    expect(screen.queryByTestId('balance-values')).toBeNull();
  });

  it('a typo fixed mid-word keeps the chips, and the unchanged setting sends nothing', async () => {
    await openDesign(experiment({ balance_within: { attribute: 'tier', values: ['gold', 'silver'] } }));
    const key = screen.getByTestId('balance-custom-key');
    fireEvent.focus(key);
    set('balance-custom-key', 'tiir');
    expect(screen.getByTestId('balance-values')).toHaveTextContent('gold');
    set('balance-custom-key', 'tier');
    fireEvent.blur(key);
    expect(screen.getByRole('button', { name: 'Remove value gold' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Remove value silver' })).toBeInTheDocument();
    save();
    await waitFor(() => expect(screen.queryByTestId('design-form')).toBeNull());
    expect(puts()).toEqual([]);
  });
});

describe('a refusal of the whole setting is never silent', () => {
  const refusedWith = (detail: unknown) => () => {
    throw new ApiError({ status: 422, detail });
  };

  it('a closed key has no values control: the text shows and takes the focus', async () => {
    const reworded = 'The setting is not usable here, said in some new words.';
    await openDesign(
      experiment(),
      refusedWith([{ loc: ['body', 'balance_within'], msg: reworded, type: 'value_error' }]),
    );
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'custom');
    set('balance-custom-key', 'web_device');
    expect(screen.queryByTestId('balance-value-input')).toBeNull();
    save();
    const alert = await screen.findByTestId('balance-error');
    expect(alert.textContent).toBe(reworded);
    expect(alert).toHaveFocus();
    expect(screen.getByTestId('design-save')).not.toBeDisabled();
  });

  it('another field\'s refusal in the same 422 still shows at the foot', async () => {
    await openDesign(
      experiment(),
      refusedWith([
        { loc: ['body', 'balance_within', 'attribute'], msg: KEY_REFUSAL, type: 'value_error' },
        { loc: ['body', 'variants'], msg: 'bad', type: 'value_error' },
      ]),
    );
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'custom');
    set('balance-custom-key', 'a b');
    save();
    expect((await screen.findByTestId('balance-error')).textContent).toBe(KEY_REFUSAL);
    expect(screen.getByTestId('design-error')).toHaveTextContent('The variants and metrics were not accepted.');
    expect(screen.getByTestId('balance-custom-key')).toHaveFocus();
  });
});

describe('the same words, and axe', () => {
  it('the same sentences as the new-experiment forms, and no even or stratified', async () => {
    await openDesign();
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    const region = screen.getByTestId('balance-within');
    expect(region).toHaveTextContent('Each group gets the same split as the whole experiment.');
    expect(region).toHaveTextContent(
      'The global holdout, the exclusion group and the targeting rules come first: only users who take part are balanced. A user keeps their variant and their group, even if their attribute changes later.',
    );
    expect(region.textContent).not.toMatch(/stratif/i);
    expect(region.textContent).not.toMatch(/\beven(?! if)\b/i);
  });

  it.each([
    ['off', experiment(), () => undefined],
    ['on, with values', experiment({ balance_within: BALANCED }), () => undefined],
    ['a bandit draft', experiment({ optimization_type: 'ucb1' }), () => undefined],
  ])('the design form, %s', async (_name, current, arrange) => {
    const form = await openDesign(current);
    arrange();
    expect(await violations(form)).toEqual([]);
  });

  it('the page rows', async () => {
    const { container } = await renderPage(experiment({ balance_within: BALANCED }));
    expect(await violations(container)).toEqual([]);
  });

  it('the design form with a refusal at the split', async () => {
    const form = await openDesign(experiment({ balance_within: BALANCED }));
    fireEvent.click(screen.getByTestId('add-variant'));
    set('variant-allocation-0', '34');
    set('variant-allocation-1', '33');
    set('variant-allocation-2', '33');
    save();
    expect(await violations(form)).toEqual([]);
    expect(within(form).getByTestId('balance-split-error')).toBeInTheDocument();
  });
});
