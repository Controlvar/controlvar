/**
 * Targeting rules on `/experiments/new`, in both views (#523).
 *
 * "+ Add Group" adds a group holding one blank condition. Before, the page
 * sent it as it was, the API stored it, and assignment read it as "no rules":
 * everyone was eligible while Review said "Targeting: 1 rule group". Now the
 * page checks the rules with `validateRules` before sending, and a 422 the API
 * gives for `targeting_rules` is shown at the rules, not as a raw array message
 * at the foot of the form.
 *
 * Like `experiment-new-create-errors.test.tsx`, the real API client runs
 * against a stubbed `fetch`, so a 422 body goes through `ApiError` as it would
 * in the browser.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import NewExperimentPage from '@/pages/experiments/new';
import { AuthProvider } from '@/contexts/AuthContext';
import { RequireAuth } from '@/components/RequireAuth';
import { navigation, TOKEN_STORAGE_KEY, UserMe } from '@/services/api';
import { STEP_HEADINGS } from '@/components/experiments/new/Wizard';
import {
  describeTargetingIssue,
  TARGETING_INCOMPLETE,
  TARGETING_REFUSED,
} from '@/components/experiments/new/createErrors';
import { checkTargeting } from '@/components/experiments/new/formState';
import vocabularyRefusal from '@/tests/fixtures/targeting-vocabulary-refusal.json';
import { makeRouter } from './helpers/apiMock';

let mockRouter = makeRouter();
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const DEVELOPER: UserMe = {
  id: 'u-2',
  email: 'dev@demo.com',
  username: 'dev',
  full_name: 'Demo Developer',
  role: 'DEVELOPER',
  is_superuser: false,
  is_active: true,
  auth_provider: 'local',
};

interface Reply {
  status: number;
  body?: unknown;
}

function response({ status, body }: Reply): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'STATUS',
    headers: {
      get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/json' : null),
    },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(body === undefined ? '' : JSON.stringify(body)),
  } as unknown as Response;
}

const mockFetch = jest.fn();
let createReplies: Reply[] = [];

beforeEach(() => {
  global.fetch = mockFetch as unknown as typeof fetch;
  mockFetch.mockReset();
  createReplies = [];
  mockFetch.mockImplementation(async (url: string, init: RequestInit = {}) => {
    const path = new URL(url, 'http://localhost').pathname;
    const method = (init.method ?? 'GET').toUpperCase();
    if (path === '/api/v1/auth/me') return response({ status: 200, body: DEVELOPER });
    if (path === '/api/v1/experiments' && method === 'POST') {
      const next = createReplies.shift();
      if (!next) throw new Error('unexpected create');
      return response(next);
    }
    throw new Error(`No stub for ${method} ${path}`);
  });
  localStorage.clear();
  localStorage.setItem(TOKEN_STORAGE_KEY, 'token-1');
  jest.spyOn(navigation, 'assign').mockImplementation(() => {});
});

afterEach(() => {
  jest.restoreAllMocks();
});

const creates = () =>
  mockFetch.mock.calls.filter(
    ([url, init]) =>
      new URL(url as string, 'http://localhost').pathname === '/api/v1/experiments' &&
      ((init as RequestInit | undefined)?.method ?? 'GET').toUpperCase() === 'POST',
  );

const sentBody = (index: number) =>
  JSON.parse((creates()[index][1] as RequestInit).body as string) as Record<string, unknown>;

type View = 'guided' | 'advanced';

const setValue = (el: HTMLElement, value: string) => fireEvent.change(el, { target: { value } });

/**
 * Render the page, fill in the name, and let `editRules` work the rule
 * builder (on the Variants step in guided setup). Ends on Review in guided
 * setup, and on the form in the advanced view.
 */
async function fillIn(view: View, editRules: () => void) {
  mockRouter = makeRouter({
    pathname: '/experiments/new',
    asPath: view === 'advanced' ? '/experiments/new?advanced' : '/experiments/new',
    query: view === 'advanced' ? { advanced: '' } : {},
  });
  render(
    <AuthProvider>
      <RequireAuth>
        <NewExperimentPage />
      </RequireAuth>
    </AuthProvider>,
  );
  if (view === 'guided') {
    fireEvent.click(await screen.findByTestId('wizard-next'));
    setValue(screen.getByTestId('experiment-name'), 'Checkout redesign');
    fireEvent.click(screen.getByTestId('wizard-next'));
    expect(screen.getByTestId('wizard-step-heading')).toHaveTextContent(STEP_HEADINGS.variants);
    editRules();
    fireEvent.click(screen.getByTestId('wizard-next'));
    fireEvent.click(screen.getByTestId('wizard-next'));
    expect(screen.getByTestId('wizard-step-heading')).toHaveTextContent(STEP_HEADINGS.review);
  } else {
    await screen.findByTestId('new-experiment-form');
    setValue(screen.getByTestId('experiment-name'), 'Checkout redesign');
    editRules();
  }
}

const pressCreate = (view: View) =>
  fireEvent.click(screen.getByTestId(view === 'guided' ? 'wizard-create' : 'submit-experiment'));

/** Where the targeting rules are: the section in the form, the Variants row on Review. */
const targetingArea = (view: View) =>
  view === 'guided'
    ? (screen.getByTestId('review-targeting').parentElement as HTMLElement)
    : screen.getByTestId('targeting-section');

/** One condition, filled in: user.country equals US. */
function addCountryCondition() {
  fireEvent.click(screen.getByTestId('add-group'));
  setValue(screen.getByTestId('condition-attribute'), 'user.country');
  setValue(screen.getByTestId('condition-value'), 'US');
}

describe.each<View>(['guided', 'advanced'])('%s view: targeting rules', (view) => {
  it('stops at a blank condition from "+ Add Group", says so at the rules, and sends nothing', async () => {
    await fillIn(view, () => fireEvent.click(screen.getByTestId('add-group')));

    pressCreate(view);

    const alert = await screen.findByTestId('targeting-error');
    expect(alert).toHaveAttribute('role', 'alert');
    expect(alert).toHaveTextContent(TARGETING_INCOMPLETE);
    expect(alert).toHaveTextContent('Group 1, Condition 1: attribute is required');
    expect(alert).toHaveTextContent('Group 1, Condition 1: value is required');
    expect(within(targetingArea(view)).getByTestId('targeting-error')).toBe(alert);
    expect(alert).toHaveFocus();
    expect(screen.queryByTestId('form-error')).not.toBeInTheDocument();
    expect(creates()).toHaveLength(0);
  });

  it('stops at a group whose conditions were all removed', async () => {
    await fillIn(view, () => {
      fireEvent.click(screen.getByTestId('add-group'));
      fireEvent.click(screen.getByTestId('condition-remove'));
    });

    pressCreate(view);

    const alert = await screen.findByTestId('targeting-error');
    expect(alert).toHaveTextContent('Group 1: add a condition or remove the group');
    expect(creates()).toHaveLength(0);
  });

  it('sends complete rules, and shows a 422 on targeting_rules at the rules in the builder’s words', async () => {
    createReplies = [
      {
        status: 422,
        body: {
          detail: [
            {
              type: 'value_error',
              loc: ['body', 'targeting_rules'],
              msg: 'Value error, groups[0].conditions[0].value: value is not valid for the operator',
            },
          ],
        },
      },
    ];
    await fillIn(view, addCountryCondition);

    pressCreate(view);

    const alert = await screen.findByTestId('targeting-error');
    expect(creates()).toHaveLength(1);
    expect(sentBody(0).targeting_rules).toMatchObject({
      logical_operator: 'AND',
      groups: [{ conditions: [{ attribute: 'user.country', operator: 'equals', value: 'US' }] }],
    });
    expect(alert).toHaveTextContent(TARGETING_REFUSED);
    expect(alert).toHaveTextContent('Group 1, Condition 1: value is not valid for the operator');
    expect(alert).toHaveFocus();
    expect(within(targetingArea(view)).getByTestId('targeting-error')).toBe(alert);
    // Not the raw array message the API client would otherwise build.
    expect(document.body).not.toHaveTextContent('targeting_rules');
    expect(document.body).not.toHaveTextContent('Value error');
    expect(screen.queryByTestId('form-error')).not.toBeInTheDocument();
  });

  it('shows an unknown distance unit (#1204) as the API words it, in the builder’s words', async () => {
    createReplies = [
      {
        status: 422,
        body: { detail: [{ type: 'value_error', loc: ['body', 'targeting_rules'], msg: vocabularyRefusal.msg }] },
      },
    ];
    await fillIn(view, addCountryCondition);

    pressCreate(view);

    const alert = await screen.findByTestId('targeting-error');
    expect(within(alert).getAllByRole('listitem').map((li) => li.textContent)).toEqual([vocabularyRefusal.shown]);
  });

  it('shows both parts of a mixed 422: targeting at the rules, the rest at the foot of the form', async () => {
    createReplies = [
      {
        status: 422,
        body: {
          detail: [
            { type: 'value_error', loc: ['body', 'name'], msg: 'Value error, name is odd' },
            {
              type: 'value_error',
              loc: ['body', 'targeting_rules'],
              msg: 'Value error, groups[0].conditions[0].operator: unknown operator',
            },
          ],
        },
      },
    ];
    await fillIn(view, addCountryCondition);

    pressCreate(view);

    const alert = await screen.findByTestId('targeting-error');
    expect(alert).toHaveTextContent('Group 1, Condition 1: unknown operator');
    expect(alert).not.toHaveTextContent('name is odd');
    const rest = screen.getByTestId('form-error');
    expect(rest).toHaveTextContent('name: Value error, name is odd');
    expect(rest).not.toHaveTextContent('targeting_rules');
  });

  it('shows a 422 on another field at the foot of the form, as before', async () => {
    createReplies = [
      {
        status: 422,
        body: { detail: [{ type: 'value_error', loc: ['body', 'name'], msg: 'Value error, name is odd' }] },
      },
    ];
    await fillIn(view, () => {});

    pressCreate(view);

    expect(await screen.findByTestId('form-error')).toHaveTextContent('name: Value error, name is odd');
    expect(screen.queryByTestId('targeting-error')).not.toBeInTheDocument();
  });

  it('creates with no targeting when no group was added', async () => {
    createReplies = [{ status: 201, body: { id: 'exp-9', status: 'draft' } }];
    await fillIn(view, () => {});

    pressCreate(view);

    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-9'));
    expect(sentBody(0).targeting_rules).toBeNull();
  });
});

it('guided: "Edit targeting" goes to the step with the rule builder and clears the error', async () => {
  await fillIn('guided', () => fireEvent.click(screen.getByTestId('add-group')));
  pressCreate('guided');
  await screen.findByTestId('targeting-error');

  fireEvent.click(screen.getByTestId('targeting-error-edit'));

  expect(screen.getByTestId('wizard-step-heading')).toHaveTextContent(STEP_HEADINGS.variants);
  expect(mockRouter.push).toHaveBeenCalledWith(
    { pathname: '/experiments/new', query: { step: 'variants' } },
    undefined,
    { shallow: true },
  );
  expect(screen.queryByTestId('targeting-error')).not.toBeInTheDocument();
  expect(screen.getByTestId('condition-attribute')).toHaveValue('');
});

describe('describeTargetingIssue', () => {
  it.each([
    ['Value error, groups[0].conditions[1].operator: unknown operator', 'Group 1, Condition 2: unknown operator'],
    ['Value error, groups[2].conditions: at least one condition is required', 'Group 3: at least one condition is required'],
    ['Value error, groups[0].logical_operator: must be and, or or not', 'Group 1: must be and, or or not'],
    ['Value error, unknown key', 'unknown key'],
  ])('%s', (msg, expected) => {
    expect(describeTargetingIssue(msg)).toBe(expected);
  });
});

describe('checkTargeting', () => {
  it('passes no groups, and names an empty group', () => {
    expect(checkTargeting({ logical_operator: 'AND', groups: [] })).toEqual([]);
    expect(
      checkTargeting({
        logical_operator: 'AND',
        groups: [
          { id: 'g1', logical_operator: 'AND', conditions: [] },
          {
            id: 'g2',
            logical_operator: 'AND',
            conditions: [{ id: 'c1', attribute: 'user.plan', operator: 'is_null', value: null }],
          },
        ],
      }),
    ).toEqual(['Group 1: add a condition or remove the group']);
  });
});
