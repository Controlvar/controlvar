/**
 * A flag's split and its rule's own value on the flag's page (#2012), in
 * Production, Staging and Development.
 *
 * What these tests hold:
 *   - a string, number or json flag has a Split section in every environment,
 *     editable by a user who may change flags and read-only otherwise; a
 *     boolean flag has none, and no Serve control, rather than disabled ones;
 *   - the weights must add up to 100 to save, and the reason is text at the
 *     Total; each refusal of the API is shown at the field it names, with
 *     `aria-invalid` and `aria-describedby`, and the summary links the first
 *     invalid field, which takes focus;
 *   - a save sends `split` only when the split was changed: never for a save
 *     of the rollout, the rules or a value, never from a page whose API
 *     answered no `split`, and `null` only after the user removed it;
 *   - a split's values are sent as the flag's type;
 *   - the rule's Serve control sends its value as the flag's type, and keeps a
 *     stored value, sending no rules when the rule was not touched, also on a
 *     page whose API answered no `split`;
 *   - a long value shows in a bounded, labelled preview;
 *   - axe finds nothing on the editor empty, refused and valid, and on the
 *     read-only view.
 *
 * The mocked API keeps the flag in a `world` that a PUT changes, and every
 * test whose save reaches the API reads it back (`expectOnlyChanged`,
 * `expectRefusedAt`): each PUT went to the route of the environment on
 * screen, that environment changed (or, when refused, nothing did), and the
 * other two did not. The one exception is 'a late answer from the last
 * environment lands nowhere': its answer never comes back, so it asserts the
 * PUT's route and body instead. A save the page refuses itself sends nothing
 * (`puts` is empty).
 */
import React from 'react';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import FeatureFlagDetailPage from '@/pages/feature-flags/[id]';
import { EnvironmentSwitcher } from '@/components/feature-flags/EnvironmentSwitcher';
import { FlagEnvironmentProvider } from '@/contexts/FlagEnvironmentContext';
import { apiFetch, ApiError, ApiFetchOptions } from '@/services/api';
import {
  RULE_SERVE_HINT,
  SPLIT_ON_VALUE_HINT,
  SPLIT_PUBLIC_NOTE,
  SPLIT_ROLLOUT_HINT,
  SPLIT_STATUS_ON,
  SPLIT_TWO_VALUES_NOTE,
  VALUES_EQUAL_MESSAGE,
  WEIGHT_MESSAGE,
} from '@/utils/flagSplit';
import { shapeErrors } from '@/tests/helpers/openapiShape';
import { renderInAppRoot } from '@/tests/helpers/renderInAppRoot';
import { makeRouter, routedApi, Route } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

let mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1' } });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

function signIn(role: string) {
  mockAuth.mockReturnValue({
    status: 'authenticated',
    user: { id: 'u1', email: 'u1@example.com', username: 'u1', role, is_superuser: false },
  });
}

type Env = 'production' | 'staging' | 'development';
type Json = Record<string, unknown>;

const SPLIT = [
  { value: 'red', weight: 34 },
  { value: 'green', weight: 33 },
  { value: 'blue', weight: 33 },
];
const STAGING_SPLIT = [
  { value: 'red', weight: 50 },
  { value: 'green', weight: 25 },
  { value: 'blue', weight: 25 },
];
const RULES = {
  logical_operator: 'AND',
  groups: [{ logical_operator: 'AND', conditions: [{ attribute: 'plan', operator: 'equals', value: 'pro' }] }],
};

/** `FeatureFlagRead`: Production, the flag row. */
function flagRead(overrides: Json = {}): Json {
  return {
    id: 'flag-1',
    key: 'checkout_copy',
    name: 'Checkout copy',
    description: 'The checkout button',
    status: 'active',
    is_active: true,
    rollout_percentage: 50,
    targeting_rules: RULES,
    default_value: false,
    value_type: 'string',
    on_value: 'A',
    off_value: 'Z',
    split: SPLIT,
    tags: [],
    owner_id: 'user-1',
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-05T10:00:00Z',
    ...overrides,
  };
}

/** `FlagEnvironmentSettings` in Staging or Development. */
function settings(environment: Env, overrides: Json = {}): Json {
  return {
    environment,
    status: 'active',
    is_active: true,
    rollout_percentage: 40,
    targeting_rules: RULES,
    split: STAGING_SPLIT,
    configured: true,
    updated_at: '2026-11-20T09:00:00Z',
    ...overrides,
  };
}

/** Production as the environment routes read it. */
function productionSettings(flag: Json): Json {
  return {
    environment: 'production',
    status: flag.status,
    is_active: flag.status === 'active',
    rollout_percentage: flag.rollout_percentage,
    targeting_rules: flag.targeting_rules,
    split: flag.split,
    configured: true,
    updated_at: flag.updated_at,
  };
}

interface World {
  flag: Json;
  staging: Json;
  development: Json;
}
let world: World;
/** Every PUT, as `{route, body}`. */
let puts: Array<{ route: string; body: Json }>;

function routes(first: Route[]): Route[] {
  return [
    ...first,
    { path: '/api/v1/feature-flags/flag-1', handler: () => world.flag },
    {
      method: 'PUT',
      path: '/api/v1/feature-flags/flag-1',
      handler: (p, opts: ApiFetchOptions) => {
        const body = opts.json as Json;
        puts.push({ route: `PUT ${p}`, body });
        world.flag = { ...world.flag, ...body, updated_at: '2026-11-26T00:00:00Z' };
        return world.flag;
      },
    },
    {
      path: /^\/api\/v1\/feature-flags\/flag-1\/environments\/(staging|development)$/,
      handler: (p) => world[p.split('/')[6] as 'staging' | 'development'],
    },
    {
      method: 'PUT',
      path: /^\/api\/v1\/feature-flags\/flag-1\/environments\/[a-z]+$/,
      handler: (p, opts: ApiFetchOptions) => {
        const env = p.split('/')[6] as Env;
        const body = opts.json as Json;
        puts.push({ route: `PUT ${p}`, body });
        if (env === 'production') {
          world.flag = { ...world.flag, ...body };
          return productionSettings(world.flag);
        }
        world[env] = { ...world[env], ...body, configured: true, updated_at: '2026-11-26T00:00:00Z' };
        return world[env];
      },
    },
    {
      path: '/api/v1/feature-flags/flag-1/environments',
      handler: () => ({
        flag_id: 'flag-1',
        key: 'checkout_copy',
        environments: [productionSettings(world.flag), world.staging, world.development],
      }),
    },
    { path: '/api/v1/rollout-schedules', handler: () => ({ items: [], total: 0, skip: 0, limit: 50 }) },
    {
      path: '/api/v1/safety/feature-flags/flag-1/check',
      handler: () => ({ feature_flag_id: 'flag-1', is_healthy: true, metrics: [], last_checked: '2026-11-21T00:00:00Z' }),
    },
  ];
}

function install(overrides: Partial<World> = {}, first: Route[] = []) {
  world = {
    flag: flagRead(),
    staging: settings('staging'),
    development: settings('development', { split: null, targeting_rules: null, rollout_percentage: 0 }),
    ...overrides,
  };
  puts = [];
  mockedApiFetch.mockImplementation(routedApi(routes(first)) as unknown as typeof apiFetch);
}

async function openIn(env: Env) {
  mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1', env } });
  renderInAppRoot(
    <FlagEnvironmentProvider>
      <EnvironmentSwitcher />
      <FeatureFlagDetailPage />
    </FlagEnvironmentProvider>,
  );
  await screen.findByTestId('flag-detail');
}

const section = () => screen.getByTestId('flag-split-section');
const valueInput = (n: number) => screen.getByLabelText(`Value ${n}`) as HTMLTextAreaElement | HTMLInputElement;
const weightInput = (n: number) => screen.getByLabelText(`Weight of value ${n} (%)`) as HTMLInputElement;
const type = (el: HTMLElement, value: string) => fireEvent.change(el, { target: { value } });
async function setSlider(value: number) {
  fireEvent.change(screen.getByTestId('rollout-percentage'), { target: { value: String(value) } });
  await waitFor(() => expect(screen.getByTestId('rollout-value')).toHaveTextContent(`${value}%`));
}
async function save() {
  fireEvent.click(screen.getByTestId('save-flag'));
}
async function saved() {
  await screen.findByTestId('save-success');
}
const lastPut = () => puts[puts.length - 1];
const refusal = (detail: unknown[]) => new ApiError({ status: 422, detail, requestId: 'r-9' });

const ENVS: Env[] = ['production', 'staging', 'development'];
/** The one route a save in each environment takes. */
const SAVE_ROUTE: Record<Env, string> = {
  production: 'PUT /api/v1/feature-flags/flag-1',
  staging: 'PUT /api/v1/feature-flags/flag-1/environments/staging',
  development: 'PUT /api/v1/feature-flags/flag-1/environments/development',
};
/** The three environments as the mocked API holds them (a PUT replaces an entry, never changes it). */
const held = (): Record<Env, Json> => ({ production: world.flag, staging: world.staging, development: world.development });

/**
 * The read-back after a test's saves: every PUT went to the route of `env`,
 * `env` now holds `changed`, and the other two environments are exactly as
 * they were `before`.
 */
function expectOnlyChanged(env: Env, before: Record<Env, Json>, changed: Json) {
  expect(puts.length).toBeGreaterThan(0);
  expect(puts.map((p) => p.route)).toEqual(puts.map(() => SAVE_ROUTE[env]));
  const now = held();
  expect(now[env]).toMatchObject(changed);
  for (const other of ENVS.filter((e) => e !== env)) expect(now[other]).toEqual(before[other]);
}

/** axe over the page's root, as the other flag page tests run it (contrast is not computed in jsdom). */
async function axeViolations(): Promise<string[]> {
  const root = document.getElementById('__next') as HTMLElement;
  const results = await axe.run(root, { rules: { 'color-contrast': { enabled: false } } });
  return results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`);
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockAuth.mockReset();
  localStorage.clear();
  signIn('DEVELOPER');
});

it("the mocks are the API's shapes, and so are the bodies the page sends", async () => {
  install();
  const before = held();
  expect(shapeErrors('FeatureFlagRead', flagRead())).toEqual([]);
  expect(shapeErrors('FlagEnvironmentSettings', settings('staging'))).toEqual([]);
  expect(shapeErrors('FlagEnvironmentSettings', productionSettings(flagRead()))).toEqual([]);
  await openIn('staging');
  type(weightInput(1), '60');
  type(weightInput(2), '20');
  type(weightInput(3), '20');
  await save();
  await saved();
  expect(shapeErrors('FlagEnvironmentUpdate', lastPut().body)).toEqual([]);
  expectOnlyChanged('staging', before, {
    split: [
      { value: 'red', weight: 60 },
      { value: 'green', weight: 20 },
      { value: 'blue', weight: 20 },
    ],
  });
});

// ---------------------------------------------------------------------------
// Where the section is, and where it is not
// ---------------------------------------------------------------------------

describe('a boolean flag has no split and no Serve control', () => {
  it.each(['production', 'staging'] as const)('in %s: absent, not disabled', async (env) => {
    install({
      flag: flagRead({ value_type: 'boolean', on_value: true, off_value: false, split: null }),
      staging: settings('staging', { split: null }),
    });
    await openIn(env);
    expect(screen.getByTestId('flag-targeting-section')).toBeInTheDocument();
    expect(screen.queryByTestId('flag-split-section')).toBeNull();
    expect(screen.queryByText(/^Split/)).toBeNull();
    expect(screen.queryByTestId('rule-serve')).toBeNull();
    expect(screen.queryByLabelText('Users these rules match get')).toBeNull();
  });
});

describe('the Split section of a string flag', () => {
  it('in Production: the table, its caption, a labelled input per value and weight, the notes', async () => {
    install();
    await openIn('production');
    const split = section();
    expect(within(split).getByRole('heading', { name: 'Split' })).toBeInTheDocument();
    const table = within(split).getByRole('table', { name: 'Values and weights' });
    expect(within(table).getAllByRole('columnheader').map((th) => th.textContent)).toEqual(['Value', 'Weight (%)', 'Remove']);
    expect([valueInput(1), valueInput(2), valueInput(3)].map((el) => el.value)).toEqual(['red', 'green', 'blue']);
    expect([weightInput(1), weightInput(2), weightInput(3)].map((el) => el.value)).toEqual(['34', '33', '33']);
    expect(weightInput(1)).toHaveAttribute('inputmode', 'numeric');
    expect(within(split).getByTestId('split-total')).toHaveTextContent('Total 100%');
    expect(within(split).getByText('Order is fixed. New values are added at the end.')).toBeInTheDocument();
    expect(within(split).getByText(SPLIT_TWO_VALUES_NOTE)).toBeInTheDocument();
    expect(within(split).getByTestId('split-public-note')).toHaveTextContent(SPLIT_PUBLIC_NOTE);
    expect(within(split).getByRole('link', { name: 'create an experiment' })).toHaveAttribute('href', '/experiments');
  });

  it('in Staging the heading names it, and the rows are Staging’s own split', async () => {
    install();
    await openIn('staging');
    expect(within(section()).getByRole('heading', { name: 'Split in Staging' })).toBeInTheDocument();
    expect([weightInput(1), weightInput(2), weightInput(3)].map((el) => el.value)).toEqual(['50', '25', '25']);
  });

  it('with a split, the status, the rollout hint and the On value hint say who gets its values', async () => {
    install();
    await openIn('production');
    expect(screen.getByTestId('flag-header')).toHaveTextContent(SPLIT_STATUS_ON);
    expect(screen.getByTestId('rollout-value-hint')).toHaveTextContent(SPLIT_ROLLOUT_HINT);
    expect(screen.getByRole('textbox', { name: 'On value' })).toHaveAccessibleDescription(
      `${SPLIT_ON_VALUE_HINT} Leave empty to serve an empty string.`,
    );
  });

  it('without a split: "Add a split" starts three empty values at 34/33/33, focus on the first', async () => {
    install({ flag: flagRead({ split: null }) });
    await openIn('production');
    expect(within(section()).getByTestId('split-none')).toHaveTextContent(
      'No split: the users the flag is on for get the on value.',
    );
    fireEvent.click(within(section()).getByRole('button', { name: 'Add a split' }));
    expect([valueInput(1), valueInput(2), valueInput(3)].map((el) => el.value)).toEqual(['', '', '']);
    expect([weightInput(1), weightInput(2), weightInput(3)].map((el) => el.value)).toEqual(['34', '33', '33']);
    expect(valueInput(1)).toHaveFocus();
  });
});

// ---------------------------------------------------------------------------
// Weights must add up to 100
// ---------------------------------------------------------------------------

describe('weights must add up to 100 to save', () => {
  it.each(['production', 'staging', 'development'] as const)(
    'in %s, 90 is refused at the Total in words, nothing is sent, the first weight takes focus',
    async (env) => {
      install({ development: settings('development') });
      await openIn(env);
      type(weightInput(3), String(Number(weightInput(3).value) - 10));
      fireEvent.blur(weightInput(3));
      expect(screen.getByTestId('split-total')).toHaveTextContent('Total 90%: 10% more needed');
      expect(screen.getByTestId('split-total-status')).toHaveTextContent('Total 90%: 10% more needed');
      await save();
      expect(screen.getByTestId('split-total-error')).toHaveTextContent('Weights must add up to 100; they add up to 90.');
      expect(puts).toEqual([]);
      expect(weightInput(1)).toHaveFocus();
      expect(weightInput(1)).toHaveAccessibleDescription('Weights must add up to 100; they add up to 90.');
      const summary = screen.getByTestId('split-error-summary');
      expect(within(summary).getByRole('link')).toHaveAttribute('href', '#split-weight-0');
    },
  );

  it('99 is refused too', async () => {
    install();
    await openIn('production');
    type(weightInput(3), '32');
    await save();
    expect(screen.getByTestId('split-total-error')).toHaveTextContent('Weights must add up to 100; they add up to 99.');
    expect(puts).toEqual([]);
  });

  it('Spread evenly gives 34/33/33 and then the save goes', async () => {
    install();
    await openIn('production');
    type(weightInput(1), '10');
    fireEvent.click(screen.getByRole('button', { name: 'Spread evenly' }));
    expect([weightInput(1), weightInput(2), weightInput(3)].map((el) => el.value)).toEqual(['34', '33', '33']);
    expect(screen.getByTestId('split-total-status')).toHaveTextContent('Total 100%');
  });
});

// ---------------------------------------------------------------------------
// The split is sent only when it was changed
// ---------------------------------------------------------------------------

describe('a save sends the split only when it was changed', () => {
  it.each(['production', 'staging', 'development'] as const)('in %s, a save of the rollout alone carries no split', async (env) => {
    install({ development: settings('development') });
    const before = held();
    await openIn(env);
    await setSlider(61);
    await save();
    await saved();
    expect(lastPut().body).toEqual({ rollout_percentage: 61 });
    expect('split' in lastPut().body).toBe(false);
    expectOnlyChanged(env, before, { rollout_percentage: 61 });
  });

  it.each(['production', 'staging', 'development'] as const)('in %s, a save of the rules alone carries no split', async (env) => {
    install({ development: settings('development') });
    const before = held();
    await openIn(env);
    fireEvent.change(screen.getByDisplayValue('pro'), { target: { value: 'team' } });
    await save();
    await saved();
    expect(Object.keys(lastPut().body).sort()).toEqual(['rollout_percentage', 'targeting_rules']);
    expectOnlyChanged(env, before, { targeting_rules: { groups: [{ conditions: [{ value: 'team' }] }] } });
  });

  it('in Production, a save of the on value carries no split', async () => {
    install();
    const before = held();
    await openIn('production');
    type(screen.getByRole('textbox', { name: 'On value' }), 'Buy');
    await save();
    await saved();
    expect(Object.keys(lastPut().body).sort()).toEqual(['on_value', 'rollout_percentage']);
    expectOnlyChanged('production', before, { on_value: 'Buy', split: SPLIT });
  });

  it.each(['production', 'staging'] as const)(
    'in %s, a page whose API answered no split offers none and sends none',
    async (env) => {
      const { split: _p, ...oldFlag } = flagRead();
      const { split: _s, ...oldStaging } = settings('staging');
      install({ flag: oldFlag, staging: oldStaging });
      const before = held();
      await openIn(env);
      expect(screen.queryByTestId('flag-split-section')).toBeNull();
      expect(screen.queryByTestId('rule-serve')).toBeNull();
      await setSlider(62);
      await save();
      await saved();
      expect(lastPut().body).toEqual({ rollout_percentage: 62 });
      expectOnlyChanged(env, before, { rollout_percentage: 62 });
    },
  );

  // An API older than the split answers none, and shows no Serve control, but
  // it may hold rules with a value of their own (written by a newer one, then
  // rolled back). A save of anything else must not erase it.
  it.each(['production', 'staging'] as const)(
    'in %s, a page whose API answered no split keeps a stored rule value',
    async (env) => {
      const stored = { ...RULES, value: 'pro-copy' };
      const { split: _p, ...oldFlag } = flagRead({ targeting_rules: stored });
      const { split: _s, ...oldStaging } = settings('staging', { targeting_rules: stored });
      install({ flag: oldFlag, staging: oldStaging });
      const before = held();
      await openIn(env);
      expect(screen.queryByTestId('rule-serve')).toBeNull();
      await setSlider(63);
      await save();
      await saved();
      expect(lastPut().body).toEqual({ rollout_percentage: 63 });
      // A change of a condition sends the rules with the stored value.
      fireEvent.change(screen.getByDisplayValue('pro'), { target: { value: 'team' } });
      await save();
      await waitFor(() => expect(puts).toHaveLength(2));
      expect((lastPut().body.targeting_rules as Json).value).toBe('pro-copy');
      expect('split' in lastPut().body).toBe(false);
      expectOnlyChanged(env, before, {
        rollout_percentage: 63,
        targeting_rules: { value: 'pro-copy', groups: [{ conditions: [{ value: 'team' }] }] },
      });
    },
  );

  it.each(['production', 'staging', 'development'] as const)(
    'in %s, removing the split sends null, and only then',
    async (env) => {
      install({ development: settings('development') });
      const before = held();
      await openIn(env);
      fireEvent.click(screen.getByRole('button', { name: 'Remove the split' }));
      expect(screen.getByRole('button', { name: 'Add a split' })).toHaveFocus();
      expect(screen.getByTestId('split-none')).toHaveTextContent('The split is removed when you save.');
      await save();
      await saved();
      expect(lastPut().body).toEqual({ rollout_percentage: env === 'production' ? 50 : 40, split: null });
      expectOnlyChanged(env, before, { split: null });
    },
  );

  it('a weight changed and changed back sends no split', async () => {
    install();
    const before = held();
    await openIn('production');
    type(weightInput(1), '50');
    type(weightInput(1), '34');
    await save();
    await saved();
    expect(lastPut().body).toEqual({ rollout_percentage: 50 });
    expectOnlyChanged('production', before, { rollout_percentage: 50, split: SPLIT });
  });

  it('"Keep the stored split" after removing it sends nothing of the split', async () => {
    install();
    const before = held();
    await openIn('production');
    fireEvent.click(screen.getByRole('button', { name: 'Remove the split' }));
    fireEvent.click(screen.getByRole('button', { name: 'Keep the stored split' }));
    expect(valueInput(1)).toHaveValue('red');
    await save();
    await saved();
    expect(lastPut().body).toEqual({ rollout_percentage: 50 });
    expectOnlyChanged('production', before, { rollout_percentage: 50, split: SPLIT });
  });
});

// ---------------------------------------------------------------------------
// A split's values are sent as the flag's type
// ---------------------------------------------------------------------------

describe("a split's values are sent as the flag's type", () => {
  it.each(['production', 'staging'] as const)(
    'in %s, a number flag sends numbers, and 1 and 1.0 are one value, refused at the later',
    async (env) => {
      install({
        flag: flagRead({ value_type: 'number', on_value: 1, off_value: 0, split: null }),
        staging: settings('staging', { split: null }),
      });
      const before = held();
      await openIn(env);
      fireEvent.click(screen.getByRole('button', { name: 'Add a split' }));
      type(valueInput(1), '1');
      type(valueInput(2), '1.0');
      type(valueInput(3), '3');
      await save();
      expect(screen.getByTestId('split-value-1-error')).toHaveTextContent(VALUES_EQUAL_MESSAGE);
      expect(puts).toEqual([]);
      type(valueInput(2), '2');
      await save();
      await saved();
      const sent = [
        { value: 1, weight: 34 },
        { value: 2, weight: 33 },
        { value: 3, weight: 33 },
      ];
      expect(lastPut().body.split).toEqual(sent);
      expectOnlyChanged(env, before, { split: sent });
    },
  );

  it.each(['production', 'staging'] as const)(
    'in %s, a json flag sends JSON, never its text, and {"n": 1} and {"n": 1.0} are one value',
    async (env) => {
      install({
        flag: flagRead({ value_type: 'json', on_value: { a: 1 }, off_value: { a: 0 }, split: null }),
        staging: settings('staging', { split: null }),
      });
      const before = held();
      await openIn(env);
      fireEvent.click(screen.getByRole('button', { name: 'Add a split' }));
      type(valueInput(1), '{"n": 1}');
      type(valueInput(2), '{"n": 1.0}');
      type(valueInput(3), '{"n": 3}');
      await save();
      expect(screen.getByTestId('split-value-1-error')).toHaveTextContent(VALUES_EQUAL_MESSAGE);
      expect(puts).toEqual([]);
      type(valueInput(2), '["gold", true]');
      await save();
      await saved();
      const sent = [
        { value: { n: 1 }, weight: 34 },
        { value: ['gold', true], weight: 33 },
        { value: { n: 3 }, weight: 33 },
      ];
      expect(lastPut().body.split).toEqual(sent);
      expectOnlyChanged(env, before, { split: sent });
    },
  );
});

// ---------------------------------------------------------------------------
// Editing the table
// ---------------------------------------------------------------------------

describe('editing the table', () => {
  it('Add value appends an empty row at weight 0 and moves focus to it; at ten it is explained', async () => {
    install();
    await openIn('production');
    const add = screen.getByRole('button', { name: 'Add value' });
    fireEvent.click(add);
    expect(valueInput(4)).toHaveValue('');
    expect(weightInput(4)).toHaveValue('0');
    expect(valueInput(4)).toHaveFocus();
    for (let n = 5; n <= 10; n += 1) fireEvent.click(add);
    expect(screen.getAllByTestId('split-row')).toHaveLength(10);
    expect(add).toHaveAttribute('aria-disabled', 'true');
    expect(add).toHaveAccessibleDescription('A split can have at most 10 values.');
    fireEvent.click(add);
    expect(screen.getAllByTestId('split-row')).toHaveLength(10);
  });

  it('Remove is labelled per row, moves focus to the row before, and at three is explained', async () => {
    install();
    await openIn('production');
    fireEvent.click(screen.getByRole('button', { name: 'Add value' }));
    type(valueInput(4), 'amber');
    fireEvent.click(screen.getByRole('button', { name: 'Remove value 4' }));
    expect(screen.getAllByTestId('split-row')).toHaveLength(3);
    expect(valueInput(3)).toHaveFocus();
    const remove = screen.getByRole('button', { name: 'Remove value 2' });
    expect(remove).toHaveAttribute('aria-disabled', 'true');
    expect(remove).toHaveAccessibleDescription('A split needs at least 3 values.');
    fireEvent.click(remove);
    expect(screen.getAllByTestId('split-row')).toHaveLength(3);
  });

  it('removing the first row moves focus to Add value', async () => {
    install();
    await openIn('production');
    fireEvent.click(screen.getByRole('button', { name: 'Add value' }));
    fireEvent.click(screen.getByRole('button', { name: 'Remove value 1' }));
    expect(screen.getByRole('button', { name: 'Add value' })).toHaveFocus();
    expect(valueInput(1)).toHaveValue('green');
  });

  it('the impact line names every move, read off the running totals', async () => {
    install();
    await openIn('production');
    type(weightInput(1), '44');
    type(weightInput(3), '23');
    expect(screen.getByTestId('split-impact')).toHaveTextContent(
      'About 10 in every 100 users the flag is on for move from "green" to "red", and about 10 in every 100 move from "blue" to "green". Everyone else keeps their value.',
    );
  });

  it('a save of new weights sends the whole split, in order, and shows what the API answered', async () => {
    install();
    const before = held();
    await openIn('production');
    type(weightInput(1), '50');
    type(weightInput(2), '25');
    type(weightInput(3), '25');
    await save();
    await saved();
    expect(lastPut().body).toEqual({
      rollout_percentage: 50,
      split: [
        { value: 'red', weight: 50 },
        { value: 'green', weight: 25 },
        { value: 'blue', weight: 25 },
      ],
    });
    expect(weightInput(1)).toHaveValue('50');
    expect(screen.queryByTestId('split-impact')).toBeNull();
    expectOnlyChanged('production', before, {
      split: [
        { value: 'red', weight: 50 },
        { value: 'green', weight: 25 },
        { value: 'blue', weight: 25 },
      ],
    });
  });
});

// ---------------------------------------------------------------------------
// The API's refusals, each at its field
// ---------------------------------------------------------------------------

describe("the API's refusals, each at its field", () => {
  function refuseWith(detail: unknown[]): Route[] {
    const handler = (p: string, opts: ApiFetchOptions) => {
      puts.push({ route: `PUT ${p}`, body: opts.json as Json });
      throw refusal(detail);
    };
    return [
      { method: 'PUT', path: '/api/v1/feature-flags/flag-1', handler },
      { method: 'PUT', path: /^\/api\/v1\/feature-flags\/flag-1\/environments\/[a-z]+$/, handler },
    ];
  }
  async function changeAndSave() {
    type(weightInput(1), '60');
    type(weightInput(2), '20');
    type(weightInput(3), '20');
    await save();
  }
  /** The refused save went to the route of `env` alone, and no environment changed. */
  function expectRefusedAt(env: Env, before: Record<Env, Json>) {
    expect(puts.map((p) => p.route)).toEqual([SAVE_ROUTE[env]]);
    expect(held()).toEqual(before);
  }

  it.each(['production', 'staging'] as const)('in %s, ["body","split"] is at the Total and the first weight takes focus', async (env) => {
    const msg = "A split's values and the rules' own values can be at most 81920 bytes together in one environment.";
    install({}, refuseWith([{ loc: ['body', 'split'], msg, type: 'served_values_too_large' }]));
    const before = held();
    await openIn(env);
    await changeAndSave();
    expect(await screen.findByTestId('split-total-error')).toHaveTextContent(msg);
    expect(screen.queryByTestId('save-error')).toBeNull();
    const summary = screen.getByTestId('split-error-summary');
    expect(within(summary).getByRole('link')).toHaveTextContent(msg);
    expect(valueInput(1)).toHaveFocus();
    expectRefusedAt(env, before);
  });

  it.each(['production', 'staging'] as const)('in %s, a weight and a value are at their rows, aria-invalid and described', async (env) => {
    install(
      {},
      refuseWith([
        { loc: ['body', 'split', 1, 'weight'], msg: WEIGHT_MESSAGE, type: 'split_weight' },
        { loc: ['body', 'split', 2, 'value'], msg: VALUES_EQUAL_MESSAGE, type: 'split_values_equal' },
      ]),
    );
    const before = held();
    await openIn(env);
    await changeAndSave();
    await waitFor(() => expect(weightInput(2)).toHaveAttribute('aria-invalid', 'true'));
    expect(weightInput(2)).toHaveAccessibleDescription(WEIGHT_MESSAGE);
    expect(valueInput(3)).toHaveAttribute('aria-invalid', 'true');
    expect(valueInput(3)).toHaveAccessibleDescription(VALUES_EQUAL_MESSAGE);
    expect(weightInput(1)).not.toHaveAttribute('aria-invalid');
    const links = within(screen.getByTestId('split-error-summary')).getAllByRole('link');
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['#split-weight-1', '#split-value-2']);
    expect(weightInput(2)).toHaveFocus();
    fireEvent.click(links[1]);
    expect(valueInput(3)).toHaveFocus();
    expectRefusedAt(env, before);
  });

  it("a refusal of the rule's own value is at the Serve control", async () => {
    const msg = 'Value error, value larger than 8192 bytes';
    install({}, refuseWith([{ loc: ['body', 'targeting_rules'], msg, type: 'value_error' }]));
    const before = held();
    await openIn('production');
    fireEvent.change(screen.getByLabelText('Users these rules match get'), { target: { value: 'own' } });
    type(screen.getByLabelText('Their value'), 'pro copy');
    await save();
    const input = screen.getByLabelText('Their value');
    await waitFor(() => expect(input).toHaveAttribute('aria-invalid', 'true'));
    expect(input).toHaveAccessibleDescription(`value larger than 8192 bytes ${RULE_SERVE_HINT}`);
    expect(input).toHaveFocus();
    expect(screen.queryByTestId('targeting-save-error')).toBeNull();
    expect(within(screen.getByTestId('split-error-summary')).getByRole('link')).toHaveAttribute('href', '#rule-serve-value');
    expectRefusedAt('production', before);
  });

  it.each(['production', 'staging'] as const)(
    "in %s, an API that answered no split and refuses the rule's value as an unknown key: shown at the rules, nothing erased",
    async (env) => {
      const stored = { ...RULES, value: 'pro-copy' };
      const { split: _p, ...oldFlag } = flagRead({ targeting_rules: stored });
      const { split: _s, ...oldStaging } = settings('staging', { targeting_rules: stored });
      const msg = 'Value error, targeting rules: unknown key';
      install(
        { flag: oldFlag, staging: oldStaging },
        refuseWith([{ loc: ['body', 'targeting_rules'], msg, type: 'value_error' }]),
      );
      const before = held();
      await openIn(env);
      fireEvent.change(screen.getByDisplayValue('pro'), { target: { value: 'team' } });
      await save();
      const alert = await screen.findByTestId('targeting-save-error');
      expect(alert).toHaveTextContent('The rules were not accepted:');
      expect(alert).toHaveTextContent('unknown key');
      expect((lastPut().body.targeting_rules as Json).value).toBe('pro-copy');
      expectRefusedAt(env, before);
    },
  );
});

// ---------------------------------------------------------------------------
// The rule's Serve control
// ---------------------------------------------------------------------------

describe("the rule's Serve control", () => {
  it.each<[string, Json, string, unknown]>([
    ['number', { on_value: 1, off_value: 0, split: [{ value: 1, weight: 34 }, { value: 2, weight: 33 }, { value: 3, weight: 33 }] }, '3', 3],
    ['string', {}, 'pro copy', 'pro copy'],
    ['json', { on_value: { a: 1 }, off_value: { a: 0 }, split: null }, '{"tier": "gold"}', { tier: 'gold' }],
  ])('a %s flag sends the value typed as its type', async (valueType, overrides, text, sent) => {
    install({ flag: flagRead({ value_type: valueType, ...overrides }) });
    const before = held();
    await openIn('production');
    const select = screen.getByLabelText('Users these rules match get') as HTMLSelectElement;
    expect(select.tagName).toBe('SELECT');
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual([
      overrides.split === null ? 'The on value (the default)' : 'A value of the split (the default)',
      'A value of their own',
    ]);
    fireEvent.change(select, { target: { value: 'own' } });
    type(screen.getByLabelText('Their value'), text);
    await save();
    await saved();
    const rules = lastPut().body.targeting_rules as Json;
    expect(rules.value).toEqual(sent);
    expect(typeof rules.value).toBe(typeof sent);
    expect(rules.groups).toEqual(RULES.groups);
    expect('split' in lastPut().body).toBe(false);
    expectOnlyChanged('production', before, { targeting_rules: { value: sent } });
  });

  it('a number that is not one is refused at the field, and nothing is sent', async () => {
    install({ flag: flagRead({ value_type: 'number', on_value: 1, off_value: 0, split: null }) });
    await openIn('production');
    fireEvent.change(screen.getByLabelText('Users these rules match get'), { target: { value: 'own' } });
    type(screen.getByLabelText('Their value'), 'three');
    await save();
    expect(screen.getByTestId('rule-serve-value-error')).toHaveTextContent('Enter a number, such as 10, -3 or 2.5.');
    expect(screen.getByLabelText('Their value')).toHaveFocus();
    expect(puts).toEqual([]);
  });

  it('says users outside a rule percentage get the off value', async () => {
    install();
    await openIn('production');
    expect(screen.getByLabelText('Users these rules match get')).toHaveAccessibleDescription(RULE_SERVE_HINT);
    expect(RULE_SERVE_HINT).toContain('outside that percentage the off value');
  });

  it.each(['production', 'staging'] as const)(
    'in %s, a stored value is shown and kept: an untouched rule sends no rules',
    async (env) => {
      const stored = { ...RULES, value: 'pro-copy' };
      install({ flag: flagRead({ targeting_rules: stored }), staging: settings('staging', { targeting_rules: stored }) });
      const before = held();
      await openIn(env);
      expect(screen.getByLabelText('Users these rules match get')).toHaveValue('own');
      expect(screen.getByLabelText('Their value')).toHaveValue('pro-copy');
      await setSlider(70);
      await save();
      await saved();
      expect(lastPut().body).toEqual({ rollout_percentage: 70 });
      // A change of a condition keeps the stored value.
      fireEvent.change(screen.getByDisplayValue('pro'), { target: { value: 'team' } });
      await save();
      await waitFor(() => expect(puts).toHaveLength(2));
      expect((lastPut().body.targeting_rules as Json).value).toBe('pro-copy');
      expectOnlyChanged(env, before, {
        rollout_percentage: 70,
        targeting_rules: { value: 'pro-copy', groups: [{ conditions: [{ value: 'team' }] }] },
      });
    },
  );

  it('choosing the default sends the rules without the value', async () => {
    install({ flag: flagRead({ targeting_rules: { ...RULES, value: 'pro-copy' } }) });
    const before = held();
    await openIn('production');
    fireEvent.change(screen.getByLabelText('Users these rules match get'), { target: { value: 'default' } });
    await save();
    await saved();
    expect(lastPut().body.targeting_rules).toEqual(RULES);
    expectOnlyChanged('production', before, { targeting_rules: RULES });
  });

  it('a stored value of another type leaves the rules read-only', async () => {
    install({ flag: flagRead({ value_type: 'number', on_value: 1, off_value: 0, split: null, targeting_rules: { ...RULES, value: 'three' } }) });
    await openIn('production');
    expect(screen.getByTestId('targeting-raw')).toBeInTheDocument();
    expect(screen.queryByTestId('rule-serve')).toBeNull();
  });

  it('rules with no condition offer no value', async () => {
    install({ flag: flagRead({ targeting_rules: null }) });
    await openIn('production');
    expect(screen.queryByLabelText('Users these rules match get')).toBeNull();
    expect(screen.getByTestId('rule-serve-no-condition')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Who may change it
// ---------------------------------------------------------------------------

describe('a VIEWER or an ANALYST reads the split and changes none of it', () => {
  it.each([
    ['VIEWER', 'production'],
    ['ANALYST', 'production'],
    ['VIEWER', 'staging'],
    ['ANALYST', 'staging'],
  ] as const)('%s in %s: the bar, the table, "Serving 3 values", the note, no input', async (role, env) => {
    signIn(role);
    install({ flag: flagRead({ targeting_rules: { ...RULES, value: 'pro-copy' } }) });
    await openIn(env);
    const split = section();
    expect(within(split).queryAllByRole('textbox')).toEqual([]);
    expect(within(split).queryAllByRole('button')).toEqual([]);
    expect(within(split).getByTestId('split-serving')).toHaveTextContent('Serving 3 values');
    expect(within(split).getByTestId('split-bar')).toHaveAttribute('aria-hidden', 'true');
    const rows = within(within(split).getByRole('table', { name: 'Values and weights' })).getAllByRole('row');
    expect(rows.map((row) => row.textContent)).toEqual(
      env === 'production'
        ? ['ValueWeight (%)', '"red"34', '"green"33', '"blue"33']
        : ['ValueWeight (%)', '"red"50', '"green"25', '"blue"25'],
    );
    expect(within(split).getByRole('link', { name: 'create an experiment' })).toHaveAttribute('href', '/experiments');
    expect(screen.queryByLabelText('Users these rules match get')).toBeNull();
    expect(screen.getByTestId('rule-serve-readonly')).toHaveTextContent(
      env === 'production'
        ? 'Users these rules match get: a value of their own, "pro-copy".'
        : 'Users these rules match get: a value of the split.',
    );
  });

  it('a weight-0 value is listed and not counted as served', async () => {
    signIn('VIEWER');
    install({ flag: flagRead({ split: [{ value: 'A', weight: 50 }, { value: 'B', weight: 50 }, { value: 'Z', weight: 0 }] }) });
    await openIn('production');
    expect(screen.getByTestId('split-serving')).toHaveTextContent('Serving 2 values');
    expect(screen.getAllByTestId('split-readonly-row')).toHaveLength(3);
    expect(screen.getAllByTestId('split-bar-segment')).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// Long values
// ---------------------------------------------------------------------------

describe('a long value', () => {
  const LONG = 'x'.repeat(8190); // 8192 bytes as JSON

  it('in the read-only view: the first 80 characters, and it says so', async () => {
    signIn('VIEWER');
    install({ flag: flagRead({ split: [{ value: LONG, weight: 34 }, ...SPLIT.slice(1)] }) });
    await openIn('production');
    const cell = screen.getByTestId('split-readonly-value-0');
    expect(cell.textContent).toBe(`"${'x'.repeat(79)}… (first 80 of 8192 characters)`);
  });

  it('in the editor: a labelled input bounded in height, holding the value to edit', async () => {
    install({ flag: flagRead({ split: [{ value: LONG, weight: 34 }, ...SPLIT.slice(1)] }) });
    await openIn('production');
    const input = valueInput(1);
    expect(input.value).toBe(LONG);
    expect(input).toHaveAttribute('rows', '2');
    expect(input.className).toMatch(/\bmax-h-40\b/);
    expect(input.className).toMatch(/\boverflow-auto\b/);
  });
});

// ---------------------------------------------------------------------------
// Accessibility (axe in jsdom; contrast is the browser audit's)
// ---------------------------------------------------------------------------

describe('axe on the split', () => {
  it('the editor with no split', async () => {
    install({ flag: flagRead({ split: null }) });
    await openIn('production');
    expect(await axeViolations()).toEqual([]);
  });

  it('the editor, valid', async () => {
    install();
    await openIn('production');
    expect(await axeViolations()).toEqual([]);
  });

  it('the editor, refused', async () => {
    install();
    await openIn('staging');
    type(weightInput(3), '15');
    type(valueInput(2), 'red');
    await save();
    await screen.findByTestId('split-error-summary');
    expect(await axeViolations()).toEqual([]);
  });

  it('the read-only view', async () => {
    signIn('ANALYST');
    install();
    await openIn('staging');
    expect(await axeViolations()).toEqual([]);
  });
});

it('a late answer from the last environment lands nowhere', async () => {
  let give!: (v: unknown) => void;
  install({}, [
    {
      method: 'PUT',
      path: '/api/v1/feature-flags/flag-1/environments/staging',
      handler: (p, opts: ApiFetchOptions) => {
        puts.push({ route: `PUT ${p}`, body: opts.json as Json });
        return new Promise((resolve) => (give = resolve));
      },
    },
  ]);
  await openIn('staging');
  type(weightInput(1), '60');
  type(weightInput(2), '20');
  type(weightInput(3), '20');
  await save();
  fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'production' } });
  await screen.findByTestId('flag-detail');
  await waitFor(() => expect(screen.getByRole('heading', { name: 'Split' })).toBeInTheDocument());
  await act(async () => give({ ...world.staging, split: [{ value: 'x', weight: 100 }, { value: 'y', weight: 0 }, { value: 'z', weight: 0 }] }));
  expect([weightInput(1), weightInput(2), weightInput(3)].map((el) => el.value)).toEqual(['34', '33', '33']);
  // Its answer is held back, so there is nothing to read back: the one PUT
  // went to Staging's route, with the split typed there.
  expect(puts).toEqual([
    {
      route: SAVE_ROUTE.staging,
      body: {
        rollout_percentage: 40,
        split: [
          { value: 'red', weight: 60 },
          { value: 'green', weight: 20 },
          { value: 'blue', weight: 20 },
        ],
      },
    },
  ]);
});
