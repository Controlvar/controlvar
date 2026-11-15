/**
 * "Balance the split within groups" on a new experiment (#448, the dashboard's
 * side): the switch in guided setup's Variants step and in the single-page
 * form, the review row, and every refusal the API gives for the setting shown
 * in its own words at its own field.
 *
 * Texts are written out here, not imported, so a reworded sentence fails a
 * test instead of following the code.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import NewExperimentPage from '@/pages/experiments/new';
import { ApiError, apiFetch } from '@/services/api';
import { apiError, makeRouter, routedApi, Route } from './helpers/apiMock';

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

jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => null }));

jest.mock('@/components/experiments/new/leaveGuard', () => ({
  ...jest.requireActual('@/components/experiments/new/leaveGuard'),
  navigateHard: jest.fn(),
}));

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const SWITCH_LABEL = 'Balance the split within groups';
const HELPER = 'Each group gets the same split as the whole experiment.';
const COMES_FIRST =
  'The global holdout, the exclusion group and the targeting rules come first: only users who take part are balanced. ' +
  "A user keeps their variant and their group, even if their attribute changes later.";
const STEPS_REFUSAL =
  'To balance within groups, every share must be a multiple of 5, such as 50/50, 20/80, 35/35/30 or 40/30/30; 34/33/33 is not.';
const KEY_REFUSAL =
  "Balance on a top-level key of the user's attributes, made of letters, digits, _ and - (up to 100 characters), other than user_id and segment.";
const VALUES_REFUSAL =
  'List the values of tier to balance within (1 to 20). Users with any other value are balanced together as other, and users without tier as unknown.';
const NONE_NAMED_REFUSAL = 'Balance within an attribute, such as country, or within segments.';
const TOO_MANY_SEGMENTS_REFUSAL = 'Balance within at most 10 segments, not 12.';
const LISTED_OTHER_REFUSAL = 'other and unknown name the groups for any other value and for no value, so they cannot be listed.';

const SEG_A = '8d4c6f0e-3a1b-4c2d-9e8f-7a6b5c4d3e2f';
const SEG_B = '1f2e3d4c-5b6a-4978-8a9b-0c1d2e3f4a5b';
const SEGMENTS = [
  { id: SEG_A, name: 'High value', kind: 'id_list', rules: null, status: 'active', created_at: '', updated_at: '' },
  { id: SEG_B, name: 'Churn risk', kind: 'id_list', rules: null, status: 'active', created_at: '', updated_at: '' },
];

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
      { method: 'GET', path: '/api/v1/segments', handler: () => SEGMENTS },
    ]) as unknown as typeof apiFetch,
  );
}

const posts = () => mockedApiFetch.mock.calls.filter(([, o]) => (o?.method ?? 'GET').toUpperCase() === 'POST');
const postedBody = () => {
  expect(posts()).toHaveLength(1);
  return posts()[0][1]?.json as Record<string, unknown>;
};
const set = (testId: string, value: string) => fireEvent.change(screen.getByTestId(testId), { target: { value } });
const click = (testId: string) => fireEvent.click(screen.getByTestId(testId));
const heading = () => screen.getByTestId('wizard-step-heading');

function refuse(detail: unknown) {
  api([
    {
      method: 'POST',
      path: '/api/v1/experiments',
      handler: () => {
        throw new ApiError({ status: 422, detail });
      },
    },
  ]);
}

/** Guided setup, on the Variants step. */
function toVariantsStep() {
  render(<NewExperimentPage />);
  click('wizard-next');
  set('experiment-name', 'Checkout redesign');
  click('wizard-next');
  expect(heading()).toHaveTextContent('Set up the versions users will see');
}

const split = (...shares: number[]) => {
  shares.forEach((_share, i) => {
    if (!screen.queryByTestId(`variant-allocation-${i}`)) click('add-variant');
  });
  shares.forEach((share, i) => set(`variant-allocation-${i}`, String(share)));
};

const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };
async function violations(node: Element) {
  const result = await axe.run(node, axeOptions);
  return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join('; ')}`);
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  routerAt();
  api();
});

describe('the switch on a draft', () => {
  it('guided setup offers it on the Variants step, off, before the targeting rules', () => {
    toVariantsStep();
    const toggle = screen.getByLabelText(SWITCH_LABEL);
    expect(toggle).toBeInstanceOf(HTMLInputElement);
    expect((toggle as HTMLInputElement).type).toBe('checkbox');
    expect(toggle).not.toBeChecked();
    expect(screen.queryByTestId('balance-group-by')).toBeNull();
    const rules = screen.getByText('Targeting Rules');
    expect(toggle.compareDocumentPosition(rules) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('the single-page form offers the same switch', () => {
    routerAt({ advanced: '' });
    render(<NewExperimentPage />);
    expect(screen.getByLabelText(SWITCH_LABEL)).not.toBeChecked();
  });

  it('turning it on asks what to group by, with the dashboard breakdown keys and two more', () => {
    routerAt({ advanced: '' });
    render(<NewExperimentPage />);
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    const select = screen.getByLabelText('Group users by');
    expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'Platform',
      'Country',
      'User Tier',
      'Another attribute…',
      'Saved segments…',
    ]);
    expect(screen.getByText(HELPER)).toBeInTheDocument();
    expect(screen.getByText(COMES_FIRST)).toBeInTheDocument();
  });

  it('a key that is not a closed set asks for its values, as chips each with a named remove button', () => {
    routerAt({ advanced: '' });
    render(<NewExperimentPage />);
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    const input = screen.getByLabelText('Values to keep apart (up to 20)');
    fireEvent.change(input, { target: { value: 'ios' } });
    click('balance-value-add');
    fireEvent.change(input, { target: { value: 'android' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(screen.getByRole('button', { name: 'Remove value ios' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Remove value android' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Remove value ios' }));
    expect(screen.queryByRole('button', { name: 'Remove value ios' })).toBeNull();
    expect((input as HTMLInputElement).value).toBe('');
  });

  it('a closed web_ key needs no values, and says so', () => {
    routerAt({ advanced: '' });
    render(<NewExperimentPage />);
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'custom');
    set('balance-custom-key', 'web_device');
    expect(screen.queryByLabelText('Values to keep apart (up to 20)')).toBeNull();
    expect(screen.getByTestId('balance-closed-note')).toHaveTextContent('web_device is phone, tablet or desktop: there is nothing to list.');
  });

  it('what is sent: nothing while off; the key and values on; no values for a closed key; segments in order', async () => {
    routerAt({ advanced: '' });
    render(<NewExperimentPage />);
    set('experiment-name', 'A');
    click('submit-experiment');
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect('balance_within' in postedBody()).toBe(false);
  });

  it('sends the attribute and its values', async () => {
    routerAt({ advanced: '' });
    render(<NewExperimentPage />);
    set('experiment-name', 'A');
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    fireEvent.change(screen.getByLabelText('Values to keep apart (up to 20)'), { target: { value: 'ios' } });
    click('balance-value-add');
    fireEvent.change(screen.getByLabelText('Values to keep apart (up to 20)'), { target: { value: 'android' } });
    click('balance-value-add');
    click('submit-experiment');
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(postedBody().balance_within).toEqual({ attribute: 'platform', values: ['ios', 'android'] });
  });

  it('sends a closed key without values, and saved segments in the order listed', async () => {
    routerAt({ advanced: '' });
    const view = render(<NewExperimentPage />);
    set('experiment-name', 'A');
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'custom');
    set('balance-custom-key', 'web_device');
    click('submit-experiment');
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(postedBody().balance_within).toEqual({ attribute: 'web_device' });
    view.unmount();

    mockedApiFetch.mockClear();
    api();
    render(<NewExperimentPage />);
    set('experiment-name', 'B');
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'segments');
    await waitFor(() => expect(screen.getByRole('option', { name: /High value/ })).toBeInTheDocument());
    set('balance-segment-add', SEG_B);
    set('balance-segment-add', SEG_A);
    click('balance-segment-up-1');
    expect(screen.getByTestId('balance-segment-0')).toHaveTextContent('High value');
    click('submit-experiment');
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(postedBody().balance_within).toEqual({ segments: [SEG_A, SEG_B] });
  });

  it('a draft that is not balanced is created as before: no key in the body', async () => {
    toVariantsStep();
    click('wizard-next');
    click('wizard-next');
    click('wizard-create');
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect('balance_within' in postedBody()).toBe(false);
  });
});

describe('the review row', () => {
  const reviewWith = async (fill: () => void) => {
    toVariantsStep();
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    fill();
    click('wizard-next');
    click('wizard-next');
    expect(heading()).toHaveTextContent('Check and create');
  };

  it('says off when it is off', async () => {
    toVariantsStep();
    click('wizard-next');
    click('wizard-next');
    expect(screen.getByTestId('review-balance')).toHaveTextContent('Balance: off');
  });

  it('names the attribute and its values', async () => {
    await reviewWith(() => {
      for (const value of ['ios', 'android', 'web']) {
        fireEvent.change(screen.getByLabelText('Values to keep apart (up to 20)'), { target: { value } });
        click('balance-value-add');
      }
    });
    expect(screen.getByTestId('review-balance')).toHaveTextContent('Balance: within Platform (ios, android, web)');
  });

  it('names the segments, never their ids', async () => {
    await reviewWith(() => {
      set('balance-group-by', 'segments');
    });
    // Nothing chosen yet: say so rather than show an empty list.
    expect(screen.getByTestId('review-balance')).toHaveTextContent('Balance: within segments (none chosen)');
  });

  it('segment names, from the saved list', async () => {
    toVariantsStep();
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'segments');
    await waitFor(() => expect(screen.getByRole('option', { name: /High value/ })).toBeInTheDocument());
    set('balance-segment-add', SEG_A);
    set('balance-segment-add', SEG_B);
    click('wizard-next');
    click('wizard-next');
    const row = screen.getByTestId('review-balance');
    await waitFor(() => expect(row).toHaveTextContent('Balance: within segments High value, Churn risk'));
    expect(row.textContent).not.toContain(SEG_A);
  });

  it('Edit variants on the row goes back to the switch', () => {
    toVariantsStep();
    click('wizard-next');
    click('wizard-next');
    click('review-edit-variants');
    expect(heading()).toHaveTextContent('Set up the versions users will see');
    expect(screen.getByLabelText(SWITCH_LABEL)).toBeInTheDocument();
  });
});

describe('refusals at their fields', () => {
  it('a split that is not in steps of 5 shows at the split as it is typed, and not when the split is allowed', () => {
    toVariantsStep();
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    expect(screen.queryByTestId('balance-split-error')).toBeNull();
    split(34, 33, 33);
    const line = screen.getByTestId('balance-split-error');
    expect(line).toHaveTextContent(STEPS_REFUSAL);
    expect(screen.getByTestId('variant-allocation-0')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByTestId('variant-allocation-0')).toHaveAttribute('aria-describedby', line.id);
    split(40, 30, 30);
    expect(screen.queryByTestId('balance-split-error')).toBeNull();
    split(34, 33, 33);
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    expect(screen.queryByTestId('balance-split-error')).toBeNull();
  });

  it('the steps-of-5 line uses the typed split in its sentence, and waits for a split that adds up to 100', () => {
    toVariantsStep();
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('variant-allocation-0', '33');
    set('variant-allocation-1', '33');
    expect(screen.queryByTestId('balance-split-error')).toBeNull();
    set('variant-allocation-1', '67');
    expect(screen.getByTestId('balance-split-error')).toHaveTextContent(
      'To balance within groups, every share must be a multiple of 5, such as 50/50, 20/80, 35/35/30 or 40/30/30; 33/67 is not.',
    );
  });

  it('guided: Next waits on a split that is not in steps of 5, and focus goes to the split', () => {
    toVariantsStep();
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    split(34, 33, 33);
    click('wizard-next');
    expect(heading()).toHaveTextContent('Set up the versions users will see');
    expect(screen.getByTestId('step-error')).toHaveTextContent(STEPS_REFUSAL);
    expect(screen.getByTestId('variant-allocation-0')).toHaveFocus();
  });

  it('single page: Create sends nothing for a split that is not in steps of 5, and focus goes to the split', () => {
    routerAt({ advanced: '' });
    render(<NewExperimentPage />);
    set('experiment-name', 'A');
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    split(34, 33, 33);
    click('submit-experiment');
    expect(posts()).toEqual([]);
    expect(screen.getByTestId('balance-split-error')).toHaveTextContent(STEPS_REFUSAL);
    expect(screen.getByTestId('variant-allocation-0')).toHaveFocus();
    expect(screen.queryByTestId('form-error')).toBeNull();
  });

  it('a 422 on the attribute shows its text verbatim at the key, and focus goes there', async () => {
    routerAt({ advanced: '' });
    refuse([{ loc: ['body', 'balance_within', 'attribute'], msg: KEY_REFUSAL, type: 'value_error' }]);
    render(<NewExperimentPage />);
    set('experiment-name', 'A');
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'custom');
    set('balance-custom-key', 'user id');
    click('submit-experiment');
    const alert = await screen.findByTestId('balance-error');
    expect(alert).toHaveTextContent(KEY_REFUSAL);
    expect(alert.textContent).toBe(KEY_REFUSAL);
    expect(alert).toHaveAttribute('role', 'alert');
    expect(screen.getByTestId('balance-custom-key')).toHaveFocus();
    expect(screen.getByTestId('balance-custom-key')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByTestId('balance-custom-key')).toHaveAttribute('aria-describedby', alert.id);
    expect(screen.queryByTestId('form-error')).toBeNull();
  });

  it('a 422 on the setting as a whole (values) shows at the values, and focus goes there', async () => {
    routerAt({ advanced: '' });
    refuse([{ loc: ['body', 'balance_within'], msg: VALUES_REFUSAL, type: 'value_error' }]);
    render(<NewExperimentPage />);
    set('experiment-name', 'A');
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'custom');
    set('balance-custom-key', 'tier');
    click('submit-experiment');
    expect((await screen.findByTestId('balance-error')).textContent).toBe(VALUES_REFUSAL);
    expect(screen.getByLabelText('Values to keep apart (up to 20)')).toHaveFocus();
  });

  it('a refusal of the whole setting for a closed key (no values control) shows its text, and focus goes to it', async () => {
    const reworded = 'The setting is not usable here, said in some new words.';
    routerAt({ advanced: '' });
    refuse([{ loc: ['body', 'balance_within'], msg: reworded, type: 'value_error' }]);
    render(<NewExperimentPage />);
    set('experiment-name', 'A');
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'custom');
    set('balance-custom-key', 'web_device');
    click('submit-experiment');
    const alert = await screen.findByTestId('balance-error');
    expect(alert.textContent).toBe(reworded);
    expect(alert).toHaveFocus();
  });

  it('a listed other shows verbatim at the values', async () => {
    routerAt({ advanced: '' });
    refuse([{ loc: ['body', 'balance_within'], msg: LISTED_OTHER_REFUSAL, type: 'value_error' }]);
    render(<NewExperimentPage />);
    set('experiment-name', 'A');
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    fireEvent.change(screen.getByLabelText('Values to keep apart (up to 20)'), { target: { value: 'other' } });
    click('balance-value-add');
    click('submit-experiment');
    expect((await screen.findByTestId('balance-error')).textContent).toBe(LISTED_OTHER_REFUSAL);
  });

  it('a 422 on the segments shows verbatim at the segments, and focus goes there', async () => {
    routerAt({ advanced: '' });
    refuse([{ loc: ['body', 'balance_within', 'segments'], msg: TOO_MANY_SEGMENTS_REFUSAL, type: 'value_error' }]);
    render(<NewExperimentPage />);
    set('experiment-name', 'A');
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'segments');
    click('submit-experiment');
    expect((await screen.findByTestId('balance-error')).textContent).toBe(TOO_MANY_SEGMENTS_REFUSAL);
    expect(screen.getByTestId('balance-segment-add')).toHaveFocus();
  });

  it('segments with none chosen is the API\'s refusal, shown verbatim at the segments', async () => {
    routerAt({ advanced: '' });
    refuse([{ loc: ['body', 'balance_within'], msg: NONE_NAMED_REFUSAL, type: 'value_error' }]);
    render(<NewExperimentPage />);
    set('experiment-name', 'A');
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'segments');
    click('submit-experiment');
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(postedBody().balance_within).toEqual({ segments: [] });
    expect((await screen.findByTestId('balance-error')).textContent).toBe(NONE_NAMED_REFUSAL);
    expect(screen.getByTestId('balance-segment-add')).toHaveFocus();
  });

  it('the steps-of-5 refusal from the API (a split the page did not catch) shows at the split', async () => {
    routerAt({ advanced: '' });
    refuse([{ loc: ['body', 'balance_within'], msg: STEPS_REFUSAL, type: 'value_error' }]);
    render(<NewExperimentPage />);
    set('experiment-name', 'A');
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    // The page's own check passes 50/50; the API's answer is what is shown.
    click('submit-experiment');
    const line = await screen.findByTestId('balance-split-error');
    expect(line.textContent).toBe(STEPS_REFUSAL);
    expect(screen.getByTestId('variant-allocation-0')).toHaveFocus();
  });

  it('a Value error prefix is not shown, and an error about another field stays at the foot', async () => {
    routerAt({ advanced: '' });
    refuse([
      { loc: ['body', 'balance_within', 'attribute'], msg: `Value error, ${KEY_REFUSAL}`, type: 'value_error' },
      { loc: ['body', 'name'], msg: 'Name is too long', type: 'value_error' },
    ]);
    render(<NewExperimentPage />);
    set('experiment-name', 'A');
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'custom');
    set('balance-custom-key', 'x y');
    click('submit-experiment');
    expect((await screen.findByTestId('balance-error')).textContent).toBe(KEY_REFUSAL);
    expect(screen.getByTestId('form-error')).toHaveTextContent('Name is too long');
  });

  it('guided: a 422 shows on Review, "Edit balance" goes to the field with the text and focus there', async () => {
    refuse([{ loc: ['body', 'balance_within', 'attribute'], msg: KEY_REFUSAL, type: 'value_error' }]);
    toVariantsStep();
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'custom');
    set('balance-custom-key', 'a b');
    click('wizard-next');
    click('wizard-next');
    click('wizard-create');
    const row = await screen.findByTestId('balance-review-error');
    expect(row).toHaveTextContent(KEY_REFUSAL);
    click('balance-error-edit');
    await waitFor(() => expect(heading()).toHaveTextContent('Set up the versions users will see'));
    expect(screen.getByTestId('balance-error').textContent).toBe(KEY_REFUSAL);
    await waitFor(() => expect(screen.getByTestId('balance-custom-key')).toHaveFocus());
  });

  it('the text goes away when the setting is changed', async () => {
    routerAt({ advanced: '' });
    refuse([{ loc: ['body', 'balance_within', 'attribute'], msg: KEY_REFUSAL, type: 'value_error' }]);
    render(<NewExperimentPage />);
    set('experiment-name', 'A');
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'custom');
    set('balance-custom-key', 'a b');
    click('submit-experiment');
    await screen.findByTestId('balance-error');
    set('balance-custom-key', 'ab');
    expect(screen.queryByTestId('balance-error')).toBeNull();
  });

  it('an unrelated create error (a taken key) is unchanged by the switch', async () => {
    routerAt({ advanced: '' });
    api([
      {
        method: 'POST',
        path: '/api/v1/experiments',
        handler: () => {
          throw apiError(409, 'key taken');
        },
      },
    ]);
    render(<NewExperimentPage />);
    set('experiment-name', 'A');
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    fireEvent.change(screen.getByLabelText('Values to keep apart (up to 20)'), { target: { value: 'ios' } });
    click('balance-value-add');
    click('submit-experiment');
    expect(await screen.findByTestId('form-error')).toHaveTextContent('already exists');
    expect(screen.queryByTestId('balance-error')).toBeNull();
  });
});

describe('one set of words', () => {
  it('guided and single page use the same copy, and neither says even or stratified', () => {
    const regions: string[] = [];
    routerAt();
    const guided = render(<NewExperimentPage />);
    click('wizard-next');
    set('experiment-name', 'Checkout redesign');
    click('wizard-next');
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    regions.push(screen.getByTestId('balance-within').textContent ?? '');
    guided.unmount();
    routerAt({ advanced: '' });
    render(<NewExperimentPage />);
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    regions.push(screen.getByTestId('balance-within').textContent ?? '');
    expect(regions[0]).toBe(regions[1]);
    expect(regions[0]).toContain(SWITCH_LABEL);
    expect(regions[0]).not.toMatch(/stratif/i);
    expect(regions[0]).not.toMatch(/\beven(?! if)\b/i);
    expect(regions[0]).not.toMatch(/\bbalance_within\b/);
  });
});

describe('axe on the switch', () => {
  it.each([
    ['off', () => undefined],
    ['on, a key and a value', () => {
      fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
      fireEvent.change(screen.getByLabelText('Values to keep apart (up to 20)'), { target: { value: 'ios' } });
      click('balance-value-add');
    }],
    ['on, a closed key', () => {
      fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
      set('balance-group-by', 'custom');
      set('balance-custom-key', 'web_device');
    }],
    ['on, a split that is not in steps of 5', () => {
      fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
      split(34, 33, 33);
    }],
  ])('the single page, %s', async (_name, arrange) => {
    routerAt({ advanced: '' });
    const { container } = render(<NewExperimentPage />);
    arrange();
    expect(await violations(container)).toEqual([]);
  });

  it('the single page with values chosen (each chip has a named remove button)', async () => {
    routerAt({ advanced: '' });
    const { container } = render(<NewExperimentPage />);
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    for (const value of ['ios', 'android']) {
      fireEvent.change(screen.getByLabelText('Values to keep apart (up to 20)'), { target: { value } });
      click('balance-value-add');
    }
    expect(await violations(container)).toEqual([]);
  });

  it('the single page with segments chosen and a refusal at them', async () => {
    routerAt({ advanced: '' });
    refuse([{ loc: ['body', 'balance_within', 'segments'], msg: TOO_MANY_SEGMENTS_REFUSAL, type: 'value_error' }]);
    const { container } = render(<NewExperimentPage />);
    set('experiment-name', 'A');
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'segments');
    await waitFor(() => expect(screen.getByRole('option', { name: /High value/ })).toBeInTheDocument());
    set('balance-segment-add', SEG_A);
    click('submit-experiment');
    await screen.findByTestId('balance-error');
    expect(await violations(container)).toEqual([]);
  });

  it('guided: the Variants step with the switch on, and the Review row with a refusal', async () => {
    refuse([{ loc: ['body', 'balance_within', 'attribute'], msg: KEY_REFUSAL, type: 'value_error' }]);
    toVariantsStep();
    fireEvent.click(screen.getByLabelText(SWITCH_LABEL));
    set('balance-group-by', 'custom');
    set('balance-custom-key', 'a b');
    expect(await violations(screen.getByTestId('guided-setup'))).toEqual([]);
    click('wizard-next');
    click('wizard-next');
    click('wizard-create');
    await screen.findByTestId('balance-review-error');
    expect(await violations(screen.getByTestId('guided-setup'))).toEqual([]);
  });
});
