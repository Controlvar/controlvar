/**
 * A split URL experiment from `/experiments/new` (#1961), in both views.
 *
 * Split URL is offered, last, only where the API's `/modules` lists the
 * `split_url` module (the full profile); on core the API answers a split_url
 * create with 501, so nothing about it is shown. Choosing it adds a Pages block
 * (`split-url-config`, the dashboard-and-api witness) with one address per
 * variant. The create request is the usual body with `experiment_type`
 * "split_url" and a `split_url_config` that repeats each variant's name and
 * allocation and carries its page exactly as typed.
 *
 * The bodies and the addresses the client must not refuse are in
 * `fixtures/split-url-create.json`, which
 * `backend/tests/unit/schemas/test_split_url_form_fixture.py` holds to the API's
 * own rules. Texts are written out here, not imported, so a reworded sentence
 * fails a test instead of following the code.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import NewExperimentPage from '@/pages/experiments/new';
import { ApiError, apiFetch } from '@/services/api';
import { ModulesProvider } from '@/contexts/ModulesContext';
import { CORE_PROFILE, ModulesInfo } from '@/services/modules';
import { docsUrl } from '@/services/docs';
import { STEP_HEADINGS } from '@/components/experiments/new/Wizard';
import {
  buildCreatePayload,
  buildDesignPayload,
  createInitialFormState,
  experimentFormReducer,
  ExperimentFormState,
  formExperimentTypes,
  FORM_EXPERIMENT_TYPES,
  balanceSplitProblem,
  splitUrlProblem,
  validateStep,
} from '@/components/experiments/new/formState';
import { describeCreateError } from '@/components/experiments/new/createErrors';
import { makeRouter, routedApi, Route } from './helpers/apiMock';
import { AB_LITERAL, AB_NAME } from './helpers/abCreateBody';
import { shapeErrors } from '../helpers/openapiShape';
import FIXTURE from '../fixtures/split-url-create.json';

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

// A resolved, non-superuser DEVELOPER: with no user the page offers creation
// to anyone, so the form would render for the wrong reason.
jest.mock('@/contexts/AuthContext', () => ({
  useOptionalAuth: () => ({ user: { role: 'DEVELOPER', is_superuser: false } }),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const FULL: ModulesInfo = { profile: 'full', modules: ['split_url'], version: 't' };
const FULL_WITHOUT_SPLIT_URL: ModulesInfo = { profile: 'full', modules: [], version: 't' };

const CARD_DESCRIPTION = 'Give each variant its own page. Your server or edge sends each visitor to theirs.';
const PAGES_LINE =
  "Visitors who ask for the control's page are split. Nothing sends them anywhere until your server or edge asks Controlvar for each visitor (Split URL testing).";
const BALANCE_SPLIT_URL_LINE =
  'Split URL experiments cannot balance within groups: the edge router sends no user attributes.';
const NEEDS_TWO = 'A split URL experiment needs at least two variants, one page each.';
const duplicateName = (name: string) =>
  `Each variant of a split URL experiment needs its own name, because each page is matched to its variant by name: “${name}” is used more than once.`;
const blankPage = (name: string) => `Enter the page for “${name}”.`;
const notAddress = (name: string) =>
  `The page for “${name}” must be a full address that starts with http:// or https://.`;

const DESTINATION_IS_ENTRY =
  "The URL for variant 'Treatment' has the same host and path as the control URL, so a visitor sent there would be sent again. Give it a different path or host; a different query string is not enough.";
const NOT_INSTALLED =
  "Split URL testing is not installed in this deployment. experiment_type='split_url' requires the split_url module.";

type View = 'guided' | 'advanced';
const VIEWS: View[] = ['guided', 'advanced'];

function routerAt(view: View) {
  const query: Record<string, string> = view === 'advanced' ? { advanced: '' } : {};
  const search = new URLSearchParams(query).toString();
  mockRouter = makeRouter({
    pathname: '/experiments/new',
    asPath: `/experiments/new${search ? `?${search}` : ''}`,
    query,
    isReady: true,
  });
}

function api(extra: Route[] = []) {
  mockedApiFetch.mockImplementation(
    routedApi([
      ...extra,
      { method: 'POST', path: '/api/v1/experiments', handler: () => ({ id: 'exp-9', status: 'draft' }) },
    ]) as unknown as typeof apiFetch,
  );
}

/** Open a view with the API mocked and the page inside a provider seeded with `profile`. */
function open(view: View, profile: ModulesInfo | null = FULL, extra: Route[] = []) {
  routerAt(view);
  api(extra);
  return render(
    profile === null ? (
      <NewExperimentPage />
    ) : (
      <ModulesProvider initial={profile}>
        <NewExperimentPage />
      </ModulesProvider>
    ),
  );
}

const setValue = (testId: string, value: string) =>
  fireEvent.change(screen.getByTestId(testId), { target: { value } });
const click = (testId: string) => fireEvent.click(screen.getByTestId(testId));
const next = () => click('wizard-next');
const heading = () => screen.getByTestId('wizard-step-heading');
const posts = () => mockedApiFetch.mock.calls.filter(([, o]) => (o?.method ?? 'GET').toUpperCase() === 'POST');

function sentBody(): string {
  expect(posts()).toHaveLength(1);
  expect(posts()[0][0]).toBe('/api/v1/experiments');
  return JSON.stringify(posts()[0][1]?.json);
}

function chooseType(view: View, type: string) {
  if (view === 'advanced') setValue('experiment-type', type);
  else click(`wizard-type-${type}`);
}

/** Choose Split URL and (guided) walk to the Variants step, naming the experiment on the way. */
function toPages(view: View, name: string) {
  chooseType(view, 'split_url');
  if (view === 'advanced') {
    setValue('experiment-name', name);
  } else {
    next();
    setValue('experiment-name', name);
    next();
    expect(heading()).toHaveTextContent(STEP_HEADINGS.variants);
  }
}

function typePages(urls: string[]) {
  urls.forEach((u, i) => setValue(`split-url-variant-url-${i}`, u));
}

/** From the Variants step to Review (guided), or straight to the one button (advanced), then Create. */
function press(view: View) {
  if (view === 'advanced') {
    click('submit-experiment');
  } else {
    next();
    next();
    expect(heading()).toHaveTextContent(STEP_HEADINGS.review);
    click('wizard-create');
  }
}

async function createSplitUrl(view: View, name: string, urls: string[]): Promise<string> {
  toPages(view, name);
  typePages(urls);
  press(view);
  await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-9'));
  return sentBody();
}

/** The default A/B request for AB_NAME: name it (guided: walk to Review) and press Create. */
async function createAb(view: View): Promise<string> {
  if (view === 'advanced') {
    setValue('experiment-name', AB_NAME);
    click('submit-experiment');
  } else {
    next();
    setValue('experiment-name', AB_NAME);
    next();
    next();
    next();
    click('wizard-create');
  }
  await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-9'));
  return sentBody();
}

/** A pattern that matches `text` and nothing around it. */
function exactly(text: string): RegExp {
  return new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`);
}

const optionValues = () =>
  Array.from((screen.getByTestId('experiment-type') as HTMLSelectElement).options).map((o) => o.value);
const radioValues = () => (screen.getAllByRole('radio') as HTMLInputElement[]).map((r) => r.value);

beforeEach(() => {
  mockedApiFetch.mockReset();
});

// --- P1, P2: offered on the full profile only ---------------------------------

describe('Split URL is offered last, on the full profile only', () => {
  it('guided setup (full): four radios, Split URL last, with its description', () => {
    open('guided');
    expect(radioValues()).toEqual(['a_b', 'mv', 'bandit', 'split_url']);
    const card = screen.getByTestId('wizard-type-split_url').closest('label') as HTMLElement;
    expect(card).toHaveTextContent(`Split URL${CARD_DESCRIPTION}`);
    expect(screen.getByTestId('wizard-type-a_b')).toBeChecked();
  });

  it('the single-page form (full): Split URL is the last option', () => {
    open('advanced');
    expect(optionValues()).toEqual(['a_b', 'mv', 'bandit', 'split_url']);
    const select = screen.getByTestId('experiment-type') as HTMLSelectElement;
    expect(select.options[3].textContent).toBe('Split URL');
  });

  it.each([
    ['the core profile', CORE_PROFILE],
    ['no provider', null],
    ['a full profile without the split_url module', FULL_WITHOUT_SPLIT_URL],
  ] as Array<[string, ModulesInfo | null]>)('%s: nothing about Split URL, in either view', (_label, profile) => {
    open('guided', profile);
    expect(radioValues()).toEqual(['a_b', 'mv', 'bandit']);
    expect(screen.queryByTestId('wizard-type-note')).toBeNull();
    expect(screen.queryByTestId('split-url-config')).toBeNull();
    expect(screen.getByTestId('wizard-panel-type').textContent ?? '').not.toMatch(/split url/i);
  });

  it.each([
    ['the core profile', CORE_PROFILE],
    ['no provider', null],
    ['a full profile without the split_url module', FULL_WITHOUT_SPLIT_URL],
  ] as Array<[string, ModulesInfo | null]>)('%s: the single-page form has no Split URL option or note', (_label, profile) => {
    open('advanced', profile);
    expect(optionValues()).toEqual(['a_b', 'mv', 'bandit']);
    expect(screen.queryByTestId('experiment-type-note')).toBeNull();
    expect(screen.queryByTestId('split-url-config')).toBeNull();
    expect(screen.getByTestId('experiment-type').parentElement?.textContent ?? '').not.toMatch(/split url/i);
  });
});

// --- P3: the witness ------------------------------------------------------------

describe('choosing Split URL shows the Pages block', () => {
  it('the single-page form shows split-url-config, one address per variant, and says what sends visitors', () => {
    open('advanced');
    expect(screen.queryByTestId('split-url-config')).toBeNull();
    chooseType('advanced', 'split_url');
    const block = screen.getByTestId('split-url-config');
    expect(block).toBeInTheDocument();
    expect(screen.getByTestId('split-url-pages-line')).toHaveTextContent(PAGES_LINE);
    expect(screen.getByRole('link', { name: 'Split URL testing' })).toHaveAttribute('href', docsUrl('api/split-url'));
    expect(screen.getByLabelText('Page for Control (the page visitors ask for)')).toBe(
      screen.getByTestId('split-url-variant-url-0'),
    );
    expect(screen.getByLabelText('Page for Treatment')).toBe(screen.getByTestId('split-url-variant-url-1'));
    expect(screen.getByTestId('balance-unavailable')).toHaveTextContent(BALANCE_SPLIT_URL_LINE);
    expect(screen.queryByTestId('balance-switch')).toBeNull();
  });

  it('guided setup shows split-url-config on the Variants step, not before', () => {
    open('guided');
    chooseType('guided', 'split_url');
    expect(screen.queryByTestId('split-url-config')).toBeNull();
    next();
    setValue('experiment-name', 'Pages');
    next();
    expect(heading()).toHaveTextContent(STEP_HEADINGS.variants);
    expect(screen.getByTestId('split-url-config')).toBeInTheDocument();
    expect(screen.getByTestId('balance-unavailable')).toHaveTextContent(BALANCE_SPLIT_URL_LINE);
  });

  it('a variant with no name is labelled by its number, and the labels follow the control', () => {
    open('advanced');
    chooseType('advanced', 'split_url');
    setValue('variant-name-1', '  ');
    expect(screen.getByLabelText('Page for variant 2')).toBe(screen.getByTestId('split-url-variant-url-1'));
    setValue('variant-name-1', 'Treatment');
    click('variant-control-1');
    expect(screen.getByLabelText('Page for Treatment (the page visitors ask for)')).toBe(
      screen.getByTestId('split-url-variant-url-1'),
    );
    expect(screen.getByLabelText('Page for Control')).toBe(screen.getByTestId('split-url-variant-url-0'));
  });

  it('an address moves with its variant: renamed, then another removed', () => {
    open('advanced');
    chooseType('advanced', 'split_url');
    click('add-variant');
    typePages(['https://a.example/0', 'https://a.example/1', 'https://a.example/2']);
    setValue('variant-name-2', 'Third');
    expect((screen.getByLabelText('Page for Third') as HTMLInputElement).value).toBe('https://a.example/2');
    click('remove-variant-1');
    expect((screen.getByLabelText('Page for Third') as HTMLInputElement).value).toBe('https://a.example/2');
    expect((screen.getByTestId('split-url-variant-url-0') as HTMLInputElement).value).toBe('https://a.example/0');
  });

  it('keeps the typed addresses when the type changes away and back', () => {
    open('advanced');
    chooseType('advanced', 'split_url');
    typePages(['https://a.example/0', 'https://a.example/1']);
    chooseType('advanced', 'a_b');
    expect(screen.queryByTestId('split-url-config')).toBeNull();
    chooseType('advanced', 'split_url');
    expect((screen.getByTestId('split-url-variant-url-1') as HTMLInputElement).value).toBe('https://a.example/1');
  });
});

// --- P4, P5: the create request -----------------------------------------------

describe('the create request', () => {
  const cases = FIXTURE.cases as Array<{ name: string; urls: string[]; body: Record<string, unknown> }>;

  it('the fixture has a case whose addresses `new URL().href` would change, so an href plant is not vacuous', () => {
    const changed = cases.filter((c) => c.urls.every((u) => new URL(u).href !== u));
    expect(changed.map((c) => c.name)).toContain('Href cases');
    const documented = cases.find((c) => c.name === 'Checkout pages');
    expect(documented?.urls).toEqual(['https://shop.example.com/checkout', 'https://shop.example.com/checkout-v2']);
  });

  it.each(VIEWS.flatMap((view) => cases.map((c) => [view, c.name] as [View, string])))(
    '%s: "%s" sends exactly the fixture body, a valid ExperimentCreate',
    async (view, name) => {
      const c = cases.find((x) => x.name === name)!;
      open(view);
      const body = await createSplitUrl(view, c.name, c.urls);
      expect(body).toBe(JSON.stringify(c.body));
      expect(shapeErrors('ExperimentCreate', JSON.parse(body))).toEqual([]);
    },
  );

  it('the shape check refuses a split_url_config without a URL, so its pass above means something', () => {
    const body = JSON.parse(JSON.stringify(cases[0].body)) as {
      split_url_config: { variants: Array<Record<string, unknown>> };
    };
    expect(shapeErrors('ExperimentCreate', body)).toEqual([]);
    delete body.split_url_config.variants[1].url;
    expect(shapeErrors('ExperimentCreate', body)).not.toEqual([]);
  });

  it.each(VIEWS)('%s: balancing switched on under A/B is not sent for Split URL', async (view) => {
    open(view);
    if (view === 'guided') {
      // The switch is on the Variants step: switch it on there, then go back to Type.
      next();
      setValue('experiment-name', 'Balanced');
      next();
      click('balance-switch');
      click('wizard-back');
      click('wizard-back');
    } else {
      click('balance-switch');
    }
    toPages(view, 'Balanced');
    if (view === 'advanced') expect(screen.queryByTestId('balance-switch')).toBeNull();
    typePages(['https://a.example/0', 'https://a.example/1']);
    press(view);
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-9'));
    const body = JSON.parse(sentBody());
    expect(body.experiment_type).toBe('split_url');
    expect('balance_within' in body).toBe(false);
    expect('optimization_type' in body).toBe(false);
  });

  it('a 33/33/34 split is not refused for a multiple of 5 once Split URL is the type', async () => {
    open('advanced');
    click('balance-switch');
    click('add-variant');
    setValue('variant-allocation-0', '33');
    setValue('variant-allocation-1', '33');
    setValue('variant-allocation-2', '34');
    chooseType('advanced', 'split_url');
    expect(screen.queryByTestId('balance-split-error')).toBeNull();
    setValue('experiment-name', 'Thirds');
    typePages(['https://a.example/0', 'https://a.example/1', 'https://a.example/2']);
    click('submit-experiment');
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-9'));
    const body = JSON.parse(sentBody());
    expect(body.split_url_config.variants.map((v: { traffic_allocation: number }) => v.traffic_allocation)).toEqual([
      33, 33, 34,
    ]);
  });

  it('guided setup: the Review step lists each variant with its page', () => {
    open('guided');
    toPages('guided', 'Reviewed');
    typePages(['https://a.example/0', 'https://a.example/1']);
    next();
    next();
    expect(heading()).toHaveTextContent(STEP_HEADINGS.review);
    expect(screen.getByTestId('review-type')).toHaveTextContent(/^Split URL$/);
    expect(screen.getByTestId('review-variant-page-0')).toHaveTextContent('https://a.example/0');
    expect(screen.getByTestId('review-variant-page-1')).toHaveTextContent('https://a.example/1');
    expect(screen.getByTestId('review-variants')).toHaveTextContent(
      'Control: 50% (control), page https://a.example/0Treatment: 50%, page https://a.example/1',
    );
    expect(screen.getByTestId('review-balance')).toHaveTextContent(/^Balance: off$/);
  });

  it('an A/B review has no page', () => {
    open('guided');
    next();
    setValue('experiment-name', AB_NAME);
    next();
    next();
    next();
    expect(screen.queryByTestId('review-variant-page-0')).toBeNull();
  });
});

describe('buildCreatePayload', () => {
  function splitUrlState(urls: Array<string | undefined>): ExperimentFormState {
    let state: ExperimentFormState = { ...createInitialFormState(), name: 'Pages', type: 'split_url' };
    urls.forEach((url, index) => {
      if (url !== undefined) {
        state = experimentFormReducer(state, { type: 'changeVariant', index, field: 'url', value: url });
      }
    });
    return state;
  }

  it('always sends split_url_config for Split URL, one entry per variant in order, even with no address typed', () => {
    // The API accepts a split_url create without it, and that experiment cannot start.
    const payload = buildCreatePayload(splitUrlState([undefined, undefined]));
    expect(payload.experiment_type).toBe('split_url');
    expect(payload.split_url_config).toEqual({
      variants: [
        { name: 'Control', url: '', traffic_allocation: 50 },
        { name: 'Treatment', url: '', traffic_allocation: 50 },
      ],
    });
  });

  it('trims each address and sends it otherwise exactly as typed', () => {
    const payload = buildCreatePayload(splitUrlState(['  HTTPS://Shop.Example.com/x\t', ' https://shop.example.com ']));
    expect(payload.split_url_config?.variants.map((v) => v.url)).toEqual([
      'HTTPS://Shop.Example.com/x',
      'https://shop.example.com',
    ]);
  });

  it('repeats the trimmed name and the allocation sent in variants, in the same order', () => {
    let state = splitUrlState(['https://a.example/0', 'https://a.example/1']);
    state = experimentFormReducer(state, { type: 'changeVariant', index: 1, field: 'name', value: '  Treatment  ' });
    state = experimentFormReducer(state, { type: 'changeVariant', index: 0, field: 'traffic_allocation', value: '70' });
    state = experimentFormReducer(state, { type: 'changeVariant', index: 1, field: 'traffic_allocation', value: 30 });
    const payload = buildCreatePayload(state);
    expect(payload.split_url_config?.variants.map(({ name, traffic_allocation }) => ({ name, traffic_allocation }))).toEqual(
      payload.variants.map(({ name, traffic_allocation }) => ({ name, traffic_allocation })),
    );
    expect(payload.split_url_config?.variants[1].name).toBe('Treatment');
  });

  it('sends no balance_within or optimization_type, and the config last', () => {
    let state = splitUrlState(['https://a.example/0', 'https://a.example/1']);
    state = { ...state, balance: { ...state.balance, on: true }, optimizationType: 'ucb1' };
    const payload = buildCreatePayload(state);
    expect('balance_within' in payload).toBe(false);
    expect('optimization_type' in payload).toBe(false);
    expect(Object.keys(payload).pop()).toBe('split_url_config');
  });

  it.each(['a_b', 'mv', 'bandit'] as const)('%s never carries split_url_config, whatever was typed', (type) => {
    const state = { ...splitUrlState(['https://a.example/0', 'https://a.example/1']), type };
    expect('split_url_config' in buildCreatePayload(state)).toBe(false);
    expect(JSON.stringify(buildCreatePayload(state))).not.toContain('https://a.example');
  });

  it('a variant row carries its address only in split_url_config, never in variants or the design edit', () => {
    const state = splitUrlState(['https://a.example/0', 'https://a.example/1']);
    for (const v of buildCreatePayload(state).variants) {
      expect(Object.keys(v)).not.toContain('url');
      expect(JSON.stringify(v)).not.toContain('a.example');
    }
    for (const v of buildDesignPayload(state).variants) {
      expect(Object.keys(v).sort()).toEqual(['configuration', 'description', 'is_control', 'name', 'traffic_allocation']);
    }
  });
});

describe('formExperimentTypes', () => {
  it('is the core list, plus split_url last on the full profile', () => {
    expect(formExperimentTypes(false)).toEqual(['a_b', 'mv', 'bandit']);
    expect(formExperimentTypes(true)).toEqual(['a_b', 'mv', 'bandit', 'split_url']);
    expect(FORM_EXPERIMENT_TYPES).toEqual(['a_b', 'mv', 'bandit']);
  });
});

// --- P6: other bodies are unchanged ---------------------------------------------

describe('A/B bodies are byte for byte the same on the full profile', () => {
  it.each(VIEWS)('%s: the default A/B request is AB_LITERAL', async (view) => {
    open(view);
    expect(await createAb(view)).toBe(AB_LITERAL);
  });

  it.each(VIEWS)('%s: a bandit request is the A/B literal with its own type and algorithm', async (view) => {
    open(view);
    chooseType(view, 'bandit');
    setValue('optimization-type', 'ucb1');
    expect(await createAb(view)).toBe(
      JSON.stringify({ ...JSON.parse(AB_LITERAL), experiment_type: 'bandit', optimization_type: 'ucb1' }),
    );
  });

  it.each(VIEWS)('%s: Split URL with both pages typed, then back to A/B, sends AB_LITERAL', async (view) => {
    open(view);
    // Guided setup asks for the pages on the Variants step: walk there, type them, and go back to Type.
    toPages(view, AB_NAME);
    typePages(['https://a.example/0', 'https://a.example/1']);
    if (view === 'guided') {
      click('wizard-back');
      click('wizard-back');
      expect(heading()).toHaveTextContent(STEP_HEADINGS.type);
    }
    chooseType(view, 'a_b');
    expect(screen.queryByTestId('split-url-config')).toBeNull();
    const body = await createAb(view);
    expect(body).toBe(AB_LITERAL);
    expect(body).not.toContain('split_url');
    expect(body).not.toContain('a.example');
  });
});

// --- P7: refusals before sending ------------------------------------------------

describe('what the form refuses before it sends anything', () => {
  const GOOD = ['https://a.example/0', 'https://a.example/1'];

  type Setup = { urls: string[]; edit?: () => void; text: string };
  const refusals: Array<[string, Setup]> = [
    ['a blank page', { urls: [GOOD[0], ''], text: blankPage('Treatment') }],
    ['an address with no scheme', { urls: [GOOD[0], 'shop.example.com/x'], text: notAddress('Treatment') }],
    ['an ftp address', { urls: ['ftp://a.example/0', GOOD[1]], text: notAddress('Control') }],
    [
      'one variant',
      {
        urls: [GOOD[0]],
        edit: () => {
          click('remove-variant-1');
          setValue('variant-allocation-0', '100');
        },
        text: NEEDS_TWO,
      },
    ],
    [
      'two variants with the same name',
      { urls: GOOD, edit: () => setValue('variant-name-1', 'Control'), text: duplicateName('Control') },
    ],
  ];

  it.each(refusals)('single-page form: %s shows the exact sentence and sends nothing', (_label, setup) => {
    open('advanced');
    chooseType('advanced', 'split_url');
    setup.edit?.();
    setValue('experiment-name', 'Refused');
    typePages(setup.urls);
    click('submit-experiment');
    expect(screen.getByTestId('form-error')).toHaveTextContent(exactly(setup.text));
    expect(posts()).toHaveLength(0);
  });

  it.each(refusals)('guided setup: %s blocks Next on Variants with the exact sentence', (_label, setup) => {
    open('guided');
    toPages('guided', 'Refused');
    setup.edit?.();
    typePages(setup.urls);
    next();
    expect(screen.getByTestId('step-error')).toHaveTextContent(exactly(setup.text));
    expect(heading()).toHaveTextContent(STEP_HEADINGS.variants);
    expect(posts()).toHaveLength(0);
  });

  it('the first problem wins: a blank page is said before an address with no scheme', () => {
    open('advanced');
    chooseType('advanced', 'split_url');
    setValue('experiment-name', 'Refused');
    typePages(['shop.example.com', '']);
    click('submit-experiment');
    expect(screen.getByTestId('form-error')).toHaveTextContent(blankPage('Treatment'));
  });

  it('a page that is only spaces is blank, and the name in the sentence is the trimmed one', () => {
    open('advanced');
    chooseType('advanced', 'split_url');
    setValue('variant-name-1', '  Treatment  ');
    setValue('experiment-name', 'Refused');
    typePages([GOOD[0], '   ']);
    click('submit-experiment');
    expect(screen.getByTestId('form-error')).toHaveTextContent(exactly(blankPage('Treatment')));
  });
});

describe('splitUrlProblem', () => {
  const state = (urls: string[], type: ExperimentFormState['type'] = 'split_url') => {
    let s: ExperimentFormState = { ...createInitialFormState(), type };
    urls.forEach((url, index) => {
      s = experimentFormReducer(s, { type: 'changeVariant', index, field: 'url', value: url });
    });
    return s;
  };

  it('is null for every type but split_url', () => {
    for (const type of ['a_b', 'mv', 'bandit'] as const) expect(splitUrlProblem(state(['', ''], type))).toBeNull();
  });

  it('trims an address before it is checked and keeps http and https in any case', () => {
    expect(splitUrlProblem(state([' https://a.example/0 ', '\thttp://a.example/1']))).toBeNull();
    expect(splitUrlProblem(state(['HTTPS://A.example/0', 'HTTP://a.example/1']))).toBeNull();
  });

  it('refuses a blank address once trimmed, and an address that is not http or https', () => {
    expect(splitUrlProblem(state(['https://a.example/0', ' \t ']))).toBe(blankPage('Treatment'));
    expect(splitUrlProblem(state(['https://a.example/0', '//a.example/1']))).toBe(notAddress('Treatment'));
    expect(splitUrlProblem(state(['https://a.example/0', 'https:/a.example']))).toBe(notAddress('Treatment'));
  });

  it.each(FIXTURE.accepted_urls)('refuses nothing the API accepts: %s', (url) => {
    expect(splitUrlProblem(state([url, 'https://other.example/1']))).toBeNull();
    expect(splitUrlProblem(state(['https://other.example/1', url]))).toBeNull();
  });

  it('is in the Variants and Review checks, and the split is not asked about for Split URL', () => {
    const bad = state(['https://a.example/0', '']);
    expect(validateStep('variants', bad)).toBe(blankPage('Treatment'));
    expect(validateStep('review', { ...bad, name: 'Named' })).toBe(blankPage('Treatment'));
    const odd = { ...state(['https://a.example/0', 'https://a.example/1']), balance: { ...bad.balance, on: true } };
    const thirds = {
      ...odd,
      variants: [
        { ...odd.variants[0], traffic_allocation: 33 },
        { ...odd.variants[1], traffic_allocation: 67 },
      ],
    };
    expect(balanceSplitProblem(thirds)).toBeNull();
    expect(balanceSplitProblem({ ...thirds, type: 'a_b' })).not.toBeNull();
  });
});

// --- P9, P10: what the API says --------------------------------------------------

describe('what the API refuses is shown in its own words, at the foot, with the answers kept', () => {
  const GOOD = ['https://a.example/0', 'https://a.example/1'];

  function refuseWith(error: ApiError): Route {
    return {
      method: 'POST',
      path: '/api/v1/experiments',
      handler: () => {
        throw error;
      },
    };
  }

  const unprocessable = (items: Array<Record<string, unknown>>) => new ApiError({ status: 422, detail: items });

  it.each(VIEWS)('%s: a 422 on split_url_config shows the msg, without the loc path', async (view) => {
    const error = unprocessable([
      {
        loc: ['body', 'split_url_config', 'variants', 1, 'url'],
        msg: DESTINATION_IS_ENTRY,
        type: 'split_url_config',
      },
    ]);
    // The generic reading puts the path in front; the form must not.
    expect(error.message).toContain('split_url_config.variants');
    open(view, FULL, [refuseWith(error)]);
    toPages(view, 'Refused by the API');
    typePages(GOOD);
    press(view);
    await waitFor(() => expect(screen.getByTestId('form-error')).toBeInTheDocument());
    expect(screen.getByTestId('form-error').textContent).toBe(DESTINATION_IS_ENTRY);
    expect(posts()).toHaveLength(1);
    expect(mockRouter.push).not.toHaveBeenCalledWith('/experiments/exp-9');
    // The answers are kept.
    if (view === 'advanced') {
      expect((screen.getByTestId('split-url-variant-url-1') as HTMLInputElement).value).toBe(GOOD[1]);
      expect((screen.getByTestId('experiment-name') as HTMLInputElement).value).toBe('Refused by the API');
    }
  });

  it('several split_url_config items are joined; a mixed 422 keeps the generic reading', () => {
    const one = { loc: ['body', 'split_url_config', 'variants', 0, 'url'], msg: 'First.', type: 'split_url_config' };
    const two = { loc: ['body', 'split_url_config', 'variants', 1, 'url'], msg: 'Second.', type: 'split_url_config' };
    expect(describeCreateError(unprocessable([one, two]), 'k', 'advanced').message).toBe('First. Second.');
    const other = { loc: ['body', 'name'], msg: 'Field required', type: 'missing' };
    const mixed = describeCreateError(unprocessable([one, other]), 'k', 'advanced').message;
    expect(mixed).toContain('split_url_config.variants');
    expect(mixed).toContain('Field required');
  });

  it.each(VIEWS)('%s: a 501 shows the API\'s sentence and the request id once, and keeps the answers', async (view) => {
    const error = new ApiError({
      status: 501,
      detail: NOT_INSTALLED,
      message: `${NOT_INSTALLED} (Request ID: req-1961-a)`,
      requestId: 'req-1961-a',
    });
    open(view, FULL, [refuseWith(error)]);
    toPages(view, 'Refused with 501');
    typePages(GOOD);
    press(view);
    await waitFor(() => expect(screen.getByTestId('form-error')).toBeInTheDocument());
    const foot = screen.getByTestId('form-error').textContent ?? '';
    expect(foot.startsWith(NOT_INSTALLED)).toBe(true);
    expect(foot.split('req-1961-a')).toHaveLength(2);
    expect(foot).not.toMatch(/something went wrong while creating/i);
    expect(mockRouter.push).not.toHaveBeenCalledWith('/experiments/exp-9');
    if (view === 'advanced') {
      expect((screen.getByTestId('split-url-variant-url-0') as HTMLInputElement).value).toBe(GOOD[0]);
    }
  });
});
