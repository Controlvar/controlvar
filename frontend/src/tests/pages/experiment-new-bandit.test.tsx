/**
 * A bandit experiment from `/experiments/new` (#1956), in both views.
 *
 * Bandit is the third type, after Multivariate. Choosing it shows an
 * Optimization algorithm select (Thompson sampling, UCB1, Epsilon-greedy; no
 * "fixed") whose container `bandit-create` is the dashboard-and-api witness.
 * The request is the A/B request with `experiment_type: "bandit"` and the
 * algorithm appended as `optimization_type`, and no `balance_within`, whatever
 * the balance switch said. Going back to A/B sends the A/B request byte for
 * byte (AB_LITERAL, captured from main: helpers/abCreateBody.ts).
 *
 * Texts are written out here, not imported, so a reworded sentence fails a
 * test instead of following the code.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import NewExperimentPage from '@/pages/experiments/new';
import { apiFetch } from '@/services/api';
import { STEP_HEADINGS } from '@/components/experiments/new/Wizard';
import { apiError, makeRouter, routedApi, Route } from './helpers/apiMock';
import { AB_LITERAL, AB_NAME } from './helpers/abCreateBody';
import { shapeErrors } from '../helpers/openapiShape';

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

const ALGORITHM_LABEL = 'Optimization algorithm';
const HELP = {
  thompson_sampling:
    "Each variant's share of new users is the chance that it is the best, given its conversions so far.",
  ucb1:
    "Each variant's share grows with its conversion rate and with how little it has been tried, so every variant " +
    'keeps some traffic once each has had users; until then, the variants nobody has been assigned to share all ' +
    'new users.',
  epsilon_greedy:
    'The variant with the highest conversion rate gets 90% of new users plus its equal part of the other 10%, ' +
    'which is shared evenly by all variants.',
};
const SUCCESS_LINE =
  "The bandit counts a user as a success when they send the primary metric's event (with Lower is better on, " +
  'when they do not), and it moves new users while the experiment is active, every 5 minutes by default.';
const CARD_DESCRIPTION = 'Send more new users to the variant that is doing better, as results come in.';
const ALLOCATION_LINE =
  'For a bandit, these allocations are the split until its first weight update; after that the bandit sets the split.';
const ESTIMATE_LINE =
  'A fixed sample size is a guide only for a bandit: it moves traffic between variants as results come in.';
const BALANCE_BANDIT_LINE =
  'A bandit experiment cannot balance its split within groups, because it moves traffic between variants as results come in.';

/**
 * The bandit request for AB_NAME with UCB1: the A/B request with
 * `experiment_type` "bandit" and `optimization_type` appended, nothing else.
 */
const BANDIT_LITERAL =
  '{"name":"Default A/B body","key":"default_a_b_body","experiment_type":"bandit","targeting_rules":null,"variants":[{"name":"Control","is_control":true,"traffic_allocation":50},{"name":"Treatment","is_control":false,"traffic_allocation":50}],"metrics":[{"name":"Conversion","event_name":"conversion","metric_type":"conversion","is_primary":true}],"confidence_level":0.95,"correction_method":"benjamini_hochberg","optimization_type":"ucb1"}';

const ADVANCED = { advanced: '' };

function routerAt(query: Record<string, string> = {}) {
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

const setValue = (testId: string, value: string) =>
  fireEvent.change(screen.getByTestId(testId), { target: { value } });
const click = (testId: string) => fireEvent.click(screen.getByTestId(testId));
const next = () => click('wizard-next');
const heading = () => screen.getByTestId('wizard-step-heading');
const posts = () => mockedApiFetch.mock.calls.filter(([, o]) => (o?.method ?? 'GET').toUpperCase() === 'POST');

/** The one create request, as the string that goes over the wire. */
function sentBody(): string {
  expect(posts()).toHaveLength(1);
  expect(posts()[0][0]).toBe('/api/v1/experiments');
  return JSON.stringify(posts()[0][1]?.json);
}

type View = 'guided' | 'advanced';

/** Open a view with the API mocked; guided setup stays on its Type step. */
function open(view: View) {
  routerAt(view === 'advanced' ? ADVANCED : {});
  api();
  return render(<NewExperimentPage />);
}

/** Choose a type the way the view offers it. */
function chooseType(view: View, type: 'a_b' | 'mv' | 'bandit') {
  if (view === 'advanced') setValue('experiment-type', type);
  else click(`wizard-type-${type}`);
}

/** Guided setup from the Type step to Review, typing the name on the way. */
function guidedToReview(name = AB_NAME) {
  next();
  setValue('experiment-name', name);
  next();
  next();
  next();
  expect(heading()).toHaveTextContent(STEP_HEADINGS.review);
}

/** Name it (guided: walk to Review) and press Create; the body sent. */
async function create(view: View): Promise<string> {
  if (view === 'advanced') {
    setValue('experiment-name', AB_NAME);
    click('submit-experiment');
  } else {
    guidedToReview();
    click('wizard-create');
  }
  await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-9'));
  return sentBody();
}

beforeEach(() => {
  mockedApiFetch.mockReset();
});

const VIEWS: View[] = ['guided', 'advanced'];

describe('Bandit is offered last, in both views', () => {
  it('guided setup has three radios in the order A/B Test, Multivariate, Bandit', () => {
    open('guided');
    const radios = screen.getAllByRole('radio') as HTMLInputElement[];
    expect(radios.map((r) => r.value)).toEqual(['a_b', 'mv', 'bandit']);
    const card = screen.getByTestId('wizard-type-bandit').closest('label') as HTMLElement;
    expect(card).toHaveTextContent(`Bandit${CARD_DESCRIPTION}`);
    expect(screen.getByTestId('wizard-type-a_b')).toBeChecked();
  });

  it('the single-page form offers A/B Test, Multivariate, Bandit, in that order', () => {
    open('advanced');
    const select = screen.getByTestId('experiment-type') as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => [o.value, o.textContent])).toEqual([
      ['a_b', 'A/B Test'],
      ['mv', 'Multivariate'],
      ['bandit', 'Bandit'],
    ]);
  });
});

describe('the Optimization algorithm', () => {
  it.each(VIEWS)('%s: appears only while Bandit is chosen, and goes again with it', (view) => {
    open(view);
    expect(screen.queryByTestId('bandit-create')).toBeNull();
    expect(screen.queryByLabelText(ALGORITHM_LABEL)).toBeNull();
    chooseType(view, 'mv');
    expect(screen.queryByTestId('bandit-create')).toBeNull();
    chooseType(view, 'bandit');
    const witness = screen.getByTestId('bandit-create');
    expect(within(witness).getByLabelText(ALGORITHM_LABEL)).toBe(screen.getByTestId('optimization-type'));
    chooseType(view, 'a_b');
    expect(screen.queryByTestId('bandit-create')).toBeNull();
  });

  it.each(VIEWS)('%s: a native select of three algorithms, Thompson sampling first and chosen, no fixed split', (view) => {
    open(view);
    chooseType(view, 'bandit');
    const select = screen.getByLabelText(ALGORITHM_LABEL) as HTMLSelectElement;
    expect(select.tagName).toBe('SELECT');
    expect(select.id).toBe('optimization-type');
    expect(Array.from(select.options).map((o) => [o.value, o.textContent])).toEqual([
      ['thompson_sampling', 'Thompson sampling'],
      ['ucb1', 'UCB1'],
      ['epsilon_greedy', 'Epsilon-greedy'],
    ]);
    expect(select.value).toBe('thompson_sampling');
  });

  it.each(VIEWS)('%s: each algorithm says what it does, and how a success is counted, as the description', (view) => {
    open(view);
    chooseType(view, 'bandit');
    const select = screen.getByLabelText(ALGORITHM_LABEL);
    for (const algorithm of ['thompson_sampling', 'ucb1', 'epsilon_greedy'] as const) {
      setValue('optimization-type', algorithm);
      expect(screen.getByTestId('optimization-type-help')).toHaveTextContent(HELP[algorithm]);
      expect(select).toHaveAccessibleDescription(`${HELP[algorithm]} ${SUCCESS_LINE}`);
    }
  });

  it('choosing Bandit in guided setup moves no focus; the select follows the radios in the page', async () => {
    const user = userEvent.setup();
    open('guided');
    const radio = screen.getByTestId('wizard-type-bandit');
    await user.click(radio);
    expect(radio).toBeChecked();
    expect(radio).toHaveFocus();
    const select = screen.getByTestId('optimization-type');
    expect(radio.compareDocumentPosition(select) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // Under the radios, before the note on split URL experiments.
    expect(select.compareDocumentPosition(screen.getByTestId('wizard-type-note')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('the review names the type and the algorithm', () => {
    open('guided');
    chooseType('guided', 'bandit');
    guidedToReview();
    expect(screen.getByTestId('review-type')).toHaveTextContent(/^Bandit \(Thompson sampling\)$/);
    click('review-edit-type');
    setValue('optimization-type', 'epsilon_greedy');
    guidedToReview();
    expect(screen.getByTestId('review-type')).toHaveTextContent(/^Bandit \(Epsilon-greedy\)$/);
  });

  it('an A/B review still says only A/B Test', () => {
    open('guided');
    guidedToReview();
    expect(screen.getByTestId('review-type')).toHaveTextContent(/^A\/B Test$/);
  });
});

describe('the create request', () => {
  it('the literal is the A/B literal with experiment_type bandit and optimization_type appended, nothing else', () => {
    const fromAb = JSON.stringify({ ...JSON.parse(AB_LITERAL), experiment_type: 'bandit', optimization_type: 'ucb1' });
    expect(BANDIT_LITERAL).toBe(fromAb);
  });

  it.each(VIEWS)('%s: Bandit with UCB1 sends exactly the bandit literal, a valid ExperimentCreate', async (view) => {
    open(view);
    chooseType(view, 'bandit');
    setValue('optimization-type', 'ucb1');
    const body = await create(view);
    expect(body).toBe(BANDIT_LITERAL);
    expect(shapeErrors('ExperimentCreate', JSON.parse(body))).toEqual([]);
  });

  it.each(VIEWS)('%s: Bandit alone sends the default algorithm, never a bandit without one', async (view) => {
    open(view);
    chooseType(view, 'bandit');
    const body = JSON.parse(await create(view)) as Record<string, unknown>;
    expect(body.experiment_type).toBe('bandit');
    expect(body.optimization_type).toBe('thompson_sampling');
    expect(shapeErrors('ExperimentCreate', body)).toEqual([]);
  });

  it('the shape check refuses an algorithm the API does not know, so its pass above means something', () => {
    const body = JSON.parse(BANDIT_LITERAL) as Record<string, unknown>;
    expect(shapeErrors('ExperimentCreate', body)).toEqual([]);
    expect(shapeErrors('ExperimentCreate', { ...body, optimization_type: 'thompson' })).not.toEqual([]);
  });

  it.each(VIEWS)('%s: Bandit with UCB1, then back to A/B, sends the A/B request byte for byte', async (view) => {
    open(view);
    chooseType(view, 'bandit');
    setValue('optimization-type', 'ucb1');
    chooseType(view, 'a_b');
    const body = await create(view);
    expect(body).toBe(AB_LITERAL);
    expect(shapeErrors('ExperimentCreate', JSON.parse(body))).toEqual([]);
  });

  it.each(VIEWS)('%s: Bandit then Multivariate sends no optimization_type either', async (view) => {
    open(view);
    chooseType(view, 'bandit');
    setValue('optimization-type', 'epsilon_greedy');
    chooseType(view, 'mv');
    const body = JSON.parse(await create(view)) as Record<string, unknown>;
    expect(body.experiment_type).toBe('mv');
    expect('optimization_type' in body).toBe(false);
  });

  it('a refused bandit create keeps the answers: the type and the algorithm are still chosen', async () => {
    open('advanced');
    api([{ method: 'POST', path: '/api/v1/experiments', handler: () => { throw apiError(409, 'taken'); } }]);
    chooseType('advanced', 'bandit');
    setValue('optimization-type', 'ucb1');
    setValue('experiment-name', AB_NAME);
    click('submit-experiment');
    expect(await screen.findByTestId('form-error')).toHaveTextContent(
      'An experiment with the key “default_a_b_body” already exists. Choose a different key.',
    );
    expect(mockRouter.push).not.toHaveBeenCalled();
    expect(screen.getByTestId('experiment-type')).toHaveValue('bandit');
    expect(screen.getByTestId('optimization-type')).toHaveValue('ucb1');
  });
});

describe('a bandit cannot balance its split', () => {
  /** Turn the balance switch on (with a split the switch would refuse), then choose Bandit. */
  function balanceThenBandit(view: View) {
    open(view);
    if (view === 'guided') next();
    setValue('experiment-name', AB_NAME);
    if (view === 'guided') next();
    click('balance-switch');
    setValue('variant-allocation-0', '33');
    setValue('variant-allocation-1', '67');
    if (view === 'guided') {
      click('wizard-back');
      click('wizard-back');
    }
    chooseType(view, 'bandit');
  }

  it.each(VIEWS)('%s: the switch gives way to the line, and no balance_within is sent', async (view) => {
    balanceThenBandit(view);
    if (view === 'guided') {
      next();
      next();
    }
    expect(screen.queryByTestId('balance-switch')).toBeNull();
    expect(screen.getByTestId('balance-unavailable')).toHaveTextContent(BALANCE_BANDIT_LINE);
    expect(screen.getByTestId('bandit-allocation-line')).toHaveTextContent(ALLOCATION_LINE);
    // The 33/67 split the switch would refuse does not stop a bandit.
    expect(screen.queryByText(/every share must be a multiple of 5/)).toBeNull();
    if (view === 'guided') {
      next();
      next();
      expect(heading()).toHaveTextContent(STEP_HEADINGS.review);
      expect(screen.getByTestId('review-balance')).toHaveTextContent('Balance: off');
      click('wizard-create');
    } else {
      click('submit-experiment');
    }
    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-9'));
    const body = JSON.parse(sentBody()) as Record<string, unknown>;
    expect(body.experiment_type).toBe('bandit');
    expect('balance_within' in body).toBe(false);
    expect(body.variants).toEqual([
      { name: 'Control', is_control: true, traffic_allocation: 33 },
      { name: 'Treatment', is_control: false, traffic_allocation: 67 },
    ]);
  });

  it.each(VIEWS)('%s: back to A/B, the switch comes back as it was left', (view) => {
    balanceThenBandit(view);
    chooseType(view, 'a_b');
    if (view === 'guided') {
      next();
      next();
    }
    expect(screen.getByTestId('balance-switch')).toBeChecked();
    expect(screen.queryByTestId('balance-unavailable')).toBeNull();
    expect(screen.queryByTestId('bandit-allocation-line')).toBeNull();
  });

  it.each(VIEWS)('%s: an A/B experiment shows neither the allocation line nor the bandit line', (view) => {
    open(view);
    if (view === 'guided') {
      next();
      setValue('experiment-name', AB_NAME);
      next();
    }
    expect(screen.getByTestId('balance-switch')).toBeInTheDocument();
    expect(screen.queryByTestId('bandit-allocation-line')).toBeNull();
    expect(screen.queryByTestId('balance-unavailable')).toBeNull();
  });
});

describe("guided setup's estimate for a bandit", () => {
  it('says a fixed sample size is only a guide, and only for a bandit', () => {
    open('guided');
    next();
    setValue('experiment-name', AB_NAME);
    next();
    next();
    expect(heading()).toHaveTextContent(STEP_HEADINGS.estimate);
    expect(screen.queryByTestId('bandit-estimate-line')).toBeNull();
    click('wizard-back');
    click('wizard-back');
    click('wizard-back');
    chooseType('guided', 'bandit');
    next();
    next();
    next();
    expect(heading()).toHaveTextContent(STEP_HEADINGS.estimate);
    expect(screen.getByTestId('bandit-estimate-line')).toHaveTextContent(ESTIMATE_LINE);
  });
});

describe('accessibility (axe-core in jsdom; colour contrast is not computable here)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  async function violations() {
    const container = document.querySelector('body > div');
    if (!container) throw new Error('nothing rendered');
    const result = await axe.run(container, axeOptions);
    return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`);
  }

  it.each(VIEWS)('%s with Bandit chosen has no axe violations', async (view) => {
    open(view);
    chooseType(view, 'bandit');
    expect(await violations()).toEqual([]);
  });
});
