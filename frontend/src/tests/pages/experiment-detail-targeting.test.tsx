/**
 * "Who can join" on the experiment page (#523): the rules shown to every
 * reader, the Edit gate (role, state, and whether the builder can keep the
 * stored value), "Replace rules" for a value the builder cannot show, the
 * paused-save confirmation, the exact PUT body, and how a save's outcome is
 * announced.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import ExperimentDetailPage from '@/pages/experiments/[id]';
import {
  ACTIVE_REASON,
  OUTSIDE_BUILDER_NOTE,
  PAUSED_SAVE_NOTICE,
  roleReason,
} from '@/components/experiments/TargetingSection';
import { ApiError, ApiFetchOptions, apiFetch } from '@/services/api';
import { Experiment } from '@/types/experiments';
import { isEditableTargeting, isNoRules, targetingPayload } from '@/utils/experimentTargeting';
import { jsonToRules } from '@/utils/targeting';
import vocabularyRefusal from '@/tests/fixtures/targeting-vocabulary-refusal.json';
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

/** A value the dashboard's builder wrote, `id` keys included. */
const BUILDER_RULES = {
  logical_operator: 'AND',
  groups: [
    {
      id: 'g-1',
      logical_operator: 'AND',
      conditions: [{ id: 'c-1', attribute: 'user.country', operator: 'in', value: ['US', 'CA'] }],
    },
  ],
};

/** The id the server gives a starting experiment's partial-rollout rule (#533). */
const STAMPED_ID = '3f6a2c1e-9b8d-4e7f-a5c2-1d0e9f8b7a6c';

/** The documented-once flat shape: shown as JSON, never in the builder. */
const FLAT_RULES = { country: ['US'] };

const BASE: Experiment = {
  id: 'exp-1',
  name: 'Checkout button colour',
  key: 'checkout-button-colour',
  description: null,
  hypothesis: null,
  experiment_type: 'a_b',
  status: 'draft',
  targeting_rules: BUILDER_RULES,
  tags: null,
  owner_id: 'user-1',
  start_date: null,
  end_date: null,
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-01T10:00:00Z',
  variants: [
    { id: 'v-1', name: 'Control', is_control: true, traffic_allocation: 50 },
    { id: 'v-2', name: 'Blue CTA', is_control: false, traffic_allocation: 50 },
  ],
  metrics: [{ id: 'm-1', name: 'Purchase', event_name: 'purchase', metric_type: 'conversion', is_primary: true }],
};

type PutHandler = (body: Record<string, unknown>) => unknown;

/** Routes the page's calls; returns the bodies of every PUT it sent. */
function install(overrides: Partial<Experiment> = {}, onPut?: PutHandler) {
  let state: Experiment = { ...BASE, ...overrides };
  const puts: Record<string, unknown>[] = [];
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/experiments/exp-1', handler: () => state },
      {
        method: 'PUT',
        path: '/api/v1/experiments/exp-1',
        handler: (_p: string, options: ApiFetchOptions) => {
          const body = options.json as Record<string, unknown>;
          puts.push(body);
          if (onPut) return onPut(body);
          state = { ...state, targeting_rules: body.targeting_rules as Experiment['targeting_rules'] };
          return state;
        },
      },
      {
        method: 'POST',
        path: '/api/v1/experiments/exp-1/pause',
        handler: () => {
          state = { ...state, status: 'paused' };
          return state;
        },
      },
    ]) as unknown as typeof apiFetch,
  );
  return puts;
}

function signIn(role: string, extra: Record<string, unknown> = {}) {
  mockUseAuth.mockReturnValue({
    user: { id: 'user-1', email: `${role.toLowerCase()}@demo.com`, username: role.toLowerCase(), role, ...extra },
    status: 'authenticated',
  });
}

async function section() {
  await screen.findByTestId('experiment-detail');
  return screen.getByTestId('targeting-section');
}

const editButton = () => screen.queryByRole('button', { name: 'Edit' });

beforeEach(() => {
  mockedApiFetch.mockReset();
  signIn('ADMIN');
});

describe('which stored values the builder may edit', () => {
  it.each([
    ['null', null],
    ['{}', {}],
    ['{"groups": []}', { groups: [] }],
    ['a builder value with id keys', BUILDER_RULES],
    ['a top-level rule id', { ...BUILDER_RULES, id: STAMPED_ID }],
  ])('%s is editable', (_name, value) => {
    expect(isEditableTargeting(value as Record<string, unknown> | null)).toBe(true);
  });

  it.each([
    ['a flat dict', FLAT_RULES],
    ['the native shape', { rules: [] }],
    ['a top-level rollout_percentage', { ...BUILDER_RULES, rollout_percentage: 50 }],
    ['a stamped rule that admits part of the users', { ...BUILDER_RULES, id: STAMPED_ID, rollout_percentage: 50 }],
    ['an empty top-level id', { ...BUILDER_RULES, id: '' }],
    ['a top-level id that is not text', { ...BUILDER_RULES, id: 7 }],
    ['a NOT logical operator', { ...BUILDER_RULES, logical_operator: 'NOT' }],
    ['an unknown group key', { groups: [{ ...BUILDER_RULES.groups[0], name: 'x' }] }],
    ['an unknown condition key', { groups: [{ conditions: [{ attribute: 'user.plan', operator: 'equals', value: 'pro', extra: 1 }] }] }],
    ['an operator the builder does not offer for the attribute', { groups: [{ conditions: [{ attribute: 'plan', operator: 'semver_gte', value: '1.0.0' }] }] }],
    ['a group with no conditions', { groups: [{ conditions: [] }] }],
  ])('%s is not editable', (_name, value) => {
    expect(isEditableTargeting(value as Record<string, unknown>)).toBe(false);
  });

  it('treats only null, {} and empty groups as no rules', () => {
    expect(isNoRules(null)).toBe(true);
    expect(isNoRules({})).toBe(true);
    expect(isNoRules({ groups: [] })).toBe(true);
    expect(isNoRules({ logical_operator: 'AND', groups: [] })).toBe(true);
    expect(isNoRules(FLAT_RULES)).toBe(false);
    expect(isNoRules({ rules: [] })).toBe(false);
  });
});

describe('an untouched open and save keeps what is stored', () => {
  const cond = (attribute: string, operator: string, value: unknown) => ({ attribute, operator, value });
  const one = (condition: Record<string, unknown>, op: string | undefined = 'AND') => {
    const group: Record<string, unknown> = { conditions: [condition] };
    const rules: Record<string, unknown> = { groups: [group] };
    if (op !== undefined) {
      group.logical_operator = op;
      rules.logical_operator = op;
    }
    return rules;
  };

  // Every row the gate calls editable must come back exactly as stored.
  it.each([
    ['is_null with null', one(cond('user.plan', 'is_null', null))],
    ['is_not_null with null', one(cond('user.plan', 'is_not_null', null))],
    ['equals false', one(cond('session.new_user', 'equals', false))],
    ['equals 0', one(cond('user.age', 'equals', 0))],
    ['equals an empty string', one(cond('user.plan', 'equals', ''))],
    ['in a list', one(cond('user.country', 'in', ['US', 'CA']))],
    ['not_in a list', one(cond('user.country', 'not_in', ['DE']))],
    ['in a comma-separated string', one(cond('user.country', 'in', 'US, CA'))],
    ['equals text with commas', one(cond('user.email', 'equals', 'a,b, c'))],
    ['OR at both levels', one(cond('user.plan', 'equals', 'pro'), 'OR')],
    [
      'two groups, AND of OR',
      {
        logical_operator: 'AND',
        groups: [
          { logical_operator: 'OR', conditions: [cond('user.plan', 'equals', 'pro'), cond('user.age', 'greater_than', 18)] },
          { logical_operator: 'AND', conditions: [cond('app.version', 'semver_gte', '2.0.0')] },
        ],
      },
    ],
    ['a top-level rule id (#533)', { ...one(cond('user.plan', 'equals', 'pro')), id: STAMPED_ID }],
    [
      'a rule id with two groups, OR at the top',
      {
        id: 'checkout-half',
        logical_operator: 'OR',
        groups: [
          { logical_operator: 'AND', conditions: [cond('user.plan', 'equals', 'pro')] },
          { logical_operator: 'OR', conditions: [cond('user.country', 'in', ['US', 'CA'])] },
        ],
      },
    ],
  ])('%s', (_name, stored) => {
    expect(isEditableTargeting(stored)).toBe(true);
    expect(targetingPayload(jsonToRules(stored), stored)).toEqual(stored);
  });

  it('the rule id is sent back unchanged after an edit, and not when every group is removed', () => {
    const stored = { ...one(cond('user.plan', 'equals', 'pro')), id: STAMPED_ID };
    const rules = jsonToRules(stored);
    rules.groups[0].conditions[0].value = 'team';
    expect(targetingPayload(rules, stored)).toEqual({ ...one(cond('user.plan', 'equals', 'team')), id: STAMPED_ID });
    expect(targetingPayload({ ...rules, groups: [] }, stored)).toEqual({});
  });

  // Normalisations the API evaluates identically, each justified:
  it('an absent logical_operator comes back as AND (the API reads an absent one as AND)', () => {
    const stored = one(cond('user.plan', 'equals', 'pro'), undefined);
    expect(isEditableTargeting(stored)).toBe(true);
    expect(targetingPayload(jsonToRules(stored))).toEqual(one(cond('user.plan', 'equals', 'pro'), 'AND'));
  });

  it('group and condition ids are dropped (the API reads no id below the top level)', () => {
    expect(isEditableTargeting(BUILDER_RULES)).toBe(true);
    expect(targetingPayload(jsonToRules(BUILDER_RULES))).toEqual({
      logical_operator: 'AND',
      groups: [{ logical_operator: 'AND', conditions: [cond('user.country', 'in', ['US', 'CA'])] }],
    });
  });

  // Values the builder would change are not offered for editing at all.
  it.each([
    ['equals with null', one(cond('user.plan', 'equals', null))],
    ['contains with null', one(cond('user.plan', 'contains', null))],
    ['starts_with with null', one(cond('user.plan', 'starts_with', null))],
    ['in with null', one(cond('user.country', 'in', null))],
    ['is_null with an empty string', one(cond('user.plan', 'is_null', ''))],
    ['is_null with no value key', one({ attribute: 'user.plan', operator: 'is_null' })],
    ['lower-case and (the AND/OR toggle cannot show it)', one(cond('user.plan', 'equals', 'pro'), 'and')],
    ['lower-case or', one(cond('user.plan', 'equals', 'pro'), 'or')],
  ])('%s is not editable', (_name, stored) => {
    expect(isEditableTargeting(stored)).toBe(false);
  });
});

describe('Who can join — read', () => {
  it.each(['ADMIN', 'DEVELOPER', 'ANALYST', 'VIEWER'])('shows the rules in words to %s', async (role) => {
    signIn(role);
    install();
    render(<ExperimentDetailPage />);
    const s = await section();
    expect(within(s).getByRole('heading', { level: 2, name: 'Who can join' })).toBeInTheDocument();
    expect(within(s).getByTestId('targeting-summary')).toHaveTextContent(
      'People who match all of these conditions:user.country is one of US, CA',
    );
  });

  it.each([
    ['null', null],
    ['{}', {}],
    ['{"groups": []}', { groups: [] }],
  ])('says everyone is eligible for %s', async (_name, value) => {
    signIn('VIEWER');
    install({ targeting_rules: value as Experiment['targeting_rules'] });
    render(<ExperimentDetailPage />);
    const s = await section();
    expect(within(s).getByTestId('targeting-everyone')).toHaveTextContent('Everyone is eligible.');
  });

  it('says how groups combine in words, not only by colour', async () => {
    signIn('VIEWER');
    install({
      targeting_rules: {
        logical_operator: 'OR',
        groups: [
          { logical_operator: 'AND', conditions: [{ attribute: 'user.plan', operator: 'equals', value: 'pro' }] },
          { logical_operator: 'OR', conditions: [{ attribute: 'device.type', operator: 'equals', value: 'mobile' }] },
        ],
      },
    });
    render(<ExperimentDetailPage />);
    const summary = within(await section()).getByTestId('targeting-summary');
    expect(summary).toHaveTextContent('People who match any of these groups:');
    expect(summary).toHaveTextContent('Group 1: all of these conditions');
    expect(summary).toHaveTextContent('Group 2: any of these conditions');
  });
});

describe('Who can join — the Edit gate', () => {
  it.each(['ADMIN', 'DEVELOPER'])('offers Edit to %s in draft and in paused', async (role) => {
    for (const status of ['draft', 'paused'] as const) {
      signIn(role);
      install({ status });
      const { unmount } = render(<ExperimentDetailPage />);
      await section();
      expect(editButton()).toBeInTheDocument();
      unmount();
    }
  });

  it('offers Edit to a superuser whatever the role', async () => {
    signIn('VIEWER', { is_superuser: true });
    install();
    render(<ExperimentDetailPage />);
    await section();
    expect(editButton()).toBeInTheDocument();
  });

  it.each(['ANALYST', 'VIEWER'])('gives %s the role reason, no Edit and no editor', async (role) => {
    signIn(role);
    install();
    render(<ExperimentDetailPage />);
    const s = await section();
    expect(editButton()).toBeNull();
    expect(within(s).queryByTestId('targeting-editor')).toBeNull();
    expect(within(s).queryByRole('combobox')).toBeNull();
    expect(within(s).getByTestId('targeting-role-reason')).toHaveTextContent(roleReason(role));
    expect(within(s).queryByTestId('targeting-state-reason')).toBeNull();
  });

  it('gives a VIEWER the role reason, not the state reason, on an active experiment', async () => {
    signIn('VIEWER');
    install({ status: 'active' });
    render(<ExperimentDetailPage />);
    const s = await section();
    expect(within(s).getByTestId('targeting-role-reason')).toBeInTheDocument();
    expect(within(s).queryByText(ACTIVE_REASON)).toBeNull();
    expect(within(s).queryByRole('button', { name: 'Pause experiment' })).toBeNull();
  });

  it('tells an ADMIN on an active experiment to pause, with the pause action beside it', async () => {
    install({ status: 'active' });
    render(<ExperimentDetailPage />);
    const s = await section();
    expect(editButton()).toBeNull();
    expect(within(s).getByTestId('targeting-state-reason')).toHaveTextContent(ACTIVE_REASON);
    expect(within(s).queryByTestId('targeting-role-reason')).toBeNull();

    fireEvent.click(within(s).getByRole('button', { name: 'Pause experiment' }));
    await waitFor(() => expect(screen.getByTestId('experiment-status')).toHaveAttribute('data-status', 'paused'));
    expect(
      mockedApiFetch.mock.calls.some(([p, o]) => p === '/api/v1/experiments/exp-1/pause' && o?.method === 'POST'),
    ).toBe(true);
    expect(editButton()).toBeInTheDocument();
    expect(within(s).queryByTestId('targeting-state-reason')).toBeNull();
  });

  it.each([
    ['completed', 'Who can join cannot be changed after an experiment is completed.'],
    ['archived', 'Who can join cannot be changed after an experiment is archived.'],
  ] as const)('gives the state reason on a %s experiment', async (status, text) => {
    install({ status });
    render(<ExperimentDetailPage />);
    const s = await section();
    expect(editButton()).toBeNull();
    expect(within(s).getByTestId('targeting-state-reason')).toHaveTextContent(text);
    expect(within(s).queryByRole('button', { name: 'Pause experiment' })).toBeNull();
  });
});

describe('Who can join — rules the builder cannot show', () => {
  // The flag page offers version operators on any attribute outside the
  // suggested ones (#535); the experiment page does not, so the same rules
  // stay read-only here.
  it.each([
    ['os_version semver_gte (StreamPulse AI search)', 'os_version'],
    ['app_version semver_gte (StreamPulse story step 5)', 'app_version'],
  ])('keeps %s read-only, offering only Replace rules', async (_name, attribute) => {
    const stored = {
      logical_operator: 'AND',
      groups: [{ logical_operator: 'AND', conditions: [{ attribute, operator: 'semver_gte', value: '17.0.0' }] }],
    };
    install({ targeting_rules: stored });
    render(<ExperimentDetailPage />);
    const s = await section();
    expect(within(s).getByTestId('targeting-raw')).toHaveTextContent(OUTSIDE_BUILDER_NOTE);
    expect(editButton()).toBeNull();
    expect(within(s).getByRole('button', { name: 'Replace rules' })).toBeInTheDocument();
  });

  it('shows a flat value as JSON with the note and the docs link, never in the builder', async () => {
    signIn('VIEWER');
    install({ targeting_rules: FLAT_RULES });
    render(<ExperimentDetailPage />);
    const s = await section();
    const raw = within(s).getByTestId('targeting-raw');
    expect(raw).toHaveTextContent(OUTSIDE_BUILDER_NOTE);
    expect(raw.querySelector('pre')?.textContent).toBe(JSON.stringify(FLAT_RULES, null, 2));
    expect(within(raw).getByRole('link', { name: 'Read about targeting rules' }).getAttribute('href')).toMatch(
      /docs\/api\/endpoints(\.md)?\/?#targeting-rules$/,
    );
    expect(within(s).queryByTestId('targeting-summary')).toBeNull();
    expect(within(s).queryByTestId('targeting-everyone')).toBeNull();
    expect(editButton()).toBeNull();
    expect(within(s).queryByRole('button', { name: 'Replace rules' })).toBeNull();
  });

  it('shows a stamped rule with a rollout_percentage as JSON, like any rollout_percentage', async () => {
    install({ targeting_rules: { ...BUILDER_RULES, id: STAMPED_ID, rollout_percentage: 50 } });
    render(<ExperimentDetailPage />);
    const s = await section();
    expect(within(s).getByTestId('targeting-raw')).toBeInTheDocument();
    expect(editButton()).toBeNull();
  });

  it('shows a top-level rollout_percentage as JSON, since the builder would drop it', async () => {
    install({ targeting_rules: { ...BUILDER_RULES, rollout_percentage: 50 } });
    render(<ExperimentDetailPage />);
    const s = await section();
    expect(within(s).getByTestId('targeting-raw')).toBeInTheDocument();
    expect(editButton()).toBeNull();
    expect(within(s).getByRole('button', { name: 'Replace rules' })).toBeInTheDocument();
  });

  it('offers no Replace rules on an active experiment', async () => {
    install({ status: 'active', targeting_rules: FLAT_RULES });
    render(<ExperimentDetailPage />);
    const s = await section();
    expect(within(s).queryByRole('button', { name: 'Replace rules' })).toBeNull();
    expect(within(s).getByTestId('targeting-state-reason')).toHaveTextContent(ACTIVE_REASON);
  });

  it('replaces a flat value after a confirmation that shows what will be discarded', async () => {
    const puts = install({ targeting_rules: FLAT_RULES });
    render(<ExperimentDetailPage />);
    const s = await section();

    fireEvent.click(within(s).getByRole('button', { name: 'Replace rules' }));
    const dialog = within(s).getByRole('dialog', { name: 'Replace rules' });
    expect(dialog).toHaveTextContent('These rules will be discarded when you save new ones.');
    expect(dialog.querySelector('pre')?.textContent).toBe(JSON.stringify(FLAT_RULES, null, 2));
    expect(within(s).queryByTestId('targeting-editor')).toBeNull();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Start with no rules' }));
    expect(within(s).queryByRole('dialog', { name: 'Replace rules' })).toBeNull();
    const editor = within(s).getByTestId('targeting-editor');
    expect(within(editor).getByText('No targeting rules — all users will match')).toBeInTheDocument();

    fireEvent.click(within(editor).getByRole('button', { name: '+ Add Group' }));
    fireEvent.change(within(editor).getByRole('combobox', { name: 'Group 1, condition 1 attribute' }), {
      target: { value: 'user.plan' },
    });
    fireEvent.change(within(editor).getByRole('textbox', { name: 'Group 1, condition 1 value' }), {
      target: { value: 'pro' },
    });
    fireEvent.click(within(s).getByRole('button', { name: 'Save rules' }));

    await within(s).findByText('Saved. The new rules apply when the experiment starts.');
    expect(puts).toEqual([
      {
        targeting_rules: {
          logical_operator: 'AND',
          groups: [
            { logical_operator: 'AND', conditions: [{ attribute: 'user.plan', operator: 'equals', value: 'pro' }] },
          ],
        },
      },
    ]);
  });

  it('a cancelled Replace rules changes nothing', async () => {
    const puts = install({ targeting_rules: FLAT_RULES, status: 'paused' });
    render(<ExperimentDetailPage />);
    const s = await section();
    fireEvent.click(within(s).getByRole('button', { name: 'Replace rules' }));
    fireEvent.click(within(s).getByRole('button', { name: 'Cancel' }));
    expect(within(s).queryByRole('dialog')).toBeNull();
    expect(within(s).getByTestId('targeting-raw')).toBeInTheDocument();
    expect(puts).toEqual([]);
  });
});

describe('Who can join — saving', () => {
  it('names every builder control', async () => {
    install();
    render(<ExperimentDetailPage />);
    const s = await section();
    fireEvent.click(editButton()!);
    const editor = within(s).getByTestId('targeting-editor');
    expect(within(editor).getByRole('combobox', { name: 'Group 1, condition 1 attribute' })).toHaveValue('user.country');
    expect(within(editor).getByRole('combobox', { name: 'Group 1, condition 1 operator' })).toHaveValue('in');
    expect(within(editor).getByRole('textbox', { name: 'Group 1, condition 1 value' })).toHaveValue('US,CA');
    const combine = within(editor).getByRole('group', { name: 'How the conditions in group 1 combine' });
    expect(within(combine).getByRole('button', { name: 'AND', pressed: true })).toBeInTheDocument();
    expect(within(combine).getByRole('button', { name: 'OR', pressed: false })).toBeInTheDocument();

    fireEvent.click(within(editor).getByRole('button', { name: '+ Add Group' }));
    const root = within(editor).getByRole('group', { name: 'How the groups combine' });
    expect(within(root).getByRole('button', { name: 'AND', pressed: true })).toBeInTheDocument();
    expect(within(editor).getByRole('combobox', { name: 'Group 2, condition 1 operator' })).toBeInTheDocument();
  });

  it('sends only targeting_rules on a draft experiment and announces the save', async () => {
    const puts = install();
    render(<ExperimentDetailPage />);
    const s = await section();
    fireEvent.click(editButton()!);
    fireEvent.click(within(s).getByRole('button', { name: 'Save rules' }));

    const status = await within(s).findByRole('status');
    await waitFor(() => expect(status).toHaveTextContent('Saved. The new rules apply when the experiment starts.'));
    expect(puts).toEqual([
      {
        targeting_rules: {
          logical_operator: 'AND',
          groups: [
            {
              logical_operator: 'AND',
              conditions: [{ attribute: 'user.country', operator: 'in', value: ['US', 'CA'] }],
            },
          ],
        },
      },
    ]);
    expect(Object.keys(puts[0])).toEqual(['targeting_rules']);
    expect(within(s).queryByTestId('targeting-editor')).toBeNull();
    expect(within(s).getByTestId('targeting-summary')).toHaveTextContent('user.country is one of US, CA');
  });

  it('edits a rule with a stored id and sends the id back unchanged (#533)', async () => {
    const puts = install({ status: 'paused', targeting_rules: { ...BUILDER_RULES, id: STAMPED_ID } });
    render(<ExperimentDetailPage />);
    const s = await section();
    expect(within(s).getByTestId('targeting-summary')).toHaveTextContent('user.country is one of US, CA');
    fireEvent.click(editButton()!);
    fireEvent.click(within(s).getByRole('button', { name: 'Save rules' }));
    fireEvent.click(
      within(within(s).getByRole('dialog', { name: 'Save rules while paused' })).getByRole('button', {
        name: 'Save rules',
      }),
    );
    await within(s).findByText('Saved. The new rules apply when you resume the experiment.');
    expect(puts).toHaveLength(1);
    expect(puts[0]).toEqual({
      targeting_rules: {
        id: STAMPED_ID,
        logical_operator: 'AND',
        groups: [
          {
            logical_operator: 'AND',
            conditions: [{ attribute: 'user.country', operator: 'in', value: ['US', 'CA'] }],
          },
        ],
      },
    });
  });

  it('sends {} when every group is removed', async () => {
    const puts = install();
    render(<ExperimentDetailPage />);
    const s = await section();
    fireEvent.click(editButton()!);
    fireEvent.click(within(s).getByRole('button', { name: 'Remove group' }));
    fireEvent.click(within(s).getByRole('button', { name: 'Save rules' }));
    await within(s).findByText('Saved. The new rules apply when the experiment starts.');
    expect(puts).toEqual([{ targeting_rules: {} }]);
    expect(within(s).getByTestId('targeting-everyone')).toBeInTheDocument();
  });

  it('asks before saving on a paused experiment, with the full notice, then sends only targeting_rules', async () => {
    const puts = install({ status: 'paused' });
    render(<ExperimentDetailPage />);
    const s = await section();
    fireEvent.click(editButton()!);
    fireEvent.change(within(s).getByRole('textbox', { name: 'Group 1, condition 1 value' }), {
      target: { value: 'DE' },
    });
    fireEvent.click(within(s).getByRole('button', { name: 'Save rules' }));

    const dialog = within(s).getByRole('dialog', { name: 'Save rules while paused' });
    expect(dialog).toHaveTextContent(
      'People already in the experiment keep their variant. People not yet in it, including anyone turned away ' +
        'before, are checked against the new rules after you resume. Apps using an SDK may keep an earlier answer ' +
        'for up to 5 minutes. Results will combine people admitted under the old and the new rules.',
    );
    expect(PAUSED_SAVE_NOTICE).toContain('Results will combine people admitted under the old and the new rules.');
    expect(puts).toEqual([]);

    fireEvent.click(within(dialog).getByRole('button', { name: 'Keep editing' }));
    expect(within(s).queryByRole('dialog')).toBeNull();
    expect(puts).toEqual([]);

    fireEvent.click(within(s).getByRole('button', { name: 'Save rules' }));
    fireEvent.click(
      within(within(s).getByRole('dialog', { name: 'Save rules while paused' })).getByRole('button', {
        name: 'Save rules',
      }),
    );
    await within(s).findByText('Saved. The new rules apply when you resume the experiment.');
    expect(puts).toEqual([
      {
        targeting_rules: {
          logical_operator: 'AND',
          groups: [{ logical_operator: 'AND', conditions: [{ attribute: 'user.country', operator: 'in', value: 'DE' }] }],
        },
      },
    ]);
  });

  // EM condition 9 (#580): the API refuses correction_method or
  // confidence_level in a PUT once the experiment has left draft, so the only
  // PUT the dashboard sends must never carry them, even when the experiment it
  // read does.
  it('never sends the stored correction or confidence level in its PUT on a paused experiment', async () => {
    const puts = install({ status: 'paused', correction_method: 'none', confidence_level: 0.9 });
    render(<ExperimentDetailPage />);
    const s = await section();
    expect(screen.getByTestId('experiment-analysis')).toHaveTextContent('90% confidence · no correction');
    fireEvent.click(editButton()!);
    fireEvent.change(within(s).getByRole('textbox', { name: 'Group 1, condition 1 value' }), {
      target: { value: 'DE' },
    });
    fireEvent.click(within(s).getByRole('button', { name: 'Save rules' }));
    fireEvent.click(
      within(within(s).getByRole('dialog', { name: 'Save rules while paused' })).getByRole('button', {
        name: 'Save rules',
      }),
    );
    await within(s).findByText('Saved. The new rules apply when you resume the experiment.');
    expect(puts).toHaveLength(1);
    expect(Object.keys(puts[0])).toEqual(['targeting_rules']);
    expect(puts[0]).not.toHaveProperty('correction_method');
    expect(puts[0]).not.toHaveProperty('confidence_level');
  });

  it('does not use window.confirm', async () => {
    const confirmSpy = jest.spyOn(window, 'confirm');
    install({ status: 'paused' });
    render(<ExperimentDetailPage />);
    const s = await section();
    fireEvent.click(editButton()!);
    fireEvent.click(within(s).getByRole('button', { name: 'Save rules' }));
    expect(confirmSpy).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('stops an unfinished condition before sending, with a focused alert', async () => {
    const puts = install({ targeting_rules: {} });
    render(<ExperimentDetailPage />);
    const s = await section();
    fireEvent.click(editButton()!);
    fireEvent.click(within(s).getByRole('button', { name: '+ Add Group' }));
    fireEvent.click(within(s).getByRole('button', { name: 'Save rules' }));

    const alert = await within(s).findByRole('alert');
    expect(alert).toHaveTextContent('Finish or remove these conditions before saving:');
    expect(alert).toHaveTextContent('Group 1, Condition 1: attribute is required');
    await waitFor(() => expect(alert).toHaveFocus());
    expect(puts).toEqual([]);
  });

  it('maps a 422 to the condition, in an alert that takes focus', async () => {
    install({}, () => {
      throw new ApiError({
        status: 422,
        detail: [
          {
            loc: ['body', 'targeting_rules'],
            msg: 'Value error, groups[0].conditions[0].operator: unknown operator',
            type: 'value_error',
          },
        ],
      });
    });
    render(<ExperimentDetailPage />);
    const s = await section();
    fireEvent.click(editButton()!);
    fireEvent.click(within(s).getByRole('button', { name: 'Save rules' }));

    const alert = await within(s).findByRole('alert');
    expect(alert).toHaveTextContent('The rules were not accepted:');
    expect(alert).toHaveTextContent('Group 1, Condition 1: unknown operator');
    await waitFor(() => expect(alert).toHaveFocus());
    expect(within(s).getByTestId('targeting-editor')).toBeInTheDocument();
    expect(within(s).getByRole('status')).toHaveTextContent('');
  });

  it('shows an unknown distance unit (#1204) in the section as one line in the builder’s words', async () => {
    install({}, () => {
      throw new ApiError({
        status: 422,
        detail: [{ loc: ['body', 'targeting_rules'], msg: vocabularyRefusal.msg, type: 'value_error' }],
      });
    });
    render(<ExperimentDetailPage />);
    const s = await section();
    fireEvent.click(editButton()!);
    fireEvent.click(within(s).getByRole('button', { name: 'Save rules' }));

    const alert = await within(s).findByRole('alert');
    expect(alert).toHaveTextContent('The rules were not accepted:');
    expect(within(alert).getAllByRole('listitem').map((li) => li.textContent)).toEqual([vocabularyRefusal.shown]);
  });

  it.each([
    [400, 'Targeting can be changed only while the experiment is draft or paused; it is active.'],
    [409, 'The experiment changed since you opened it.'],
  ])('shows a %i in an alert that takes focus, with the server reason', async (status, detail) => {
    install({}, () => {
      throw apiError(status, detail);
    });
    render(<ExperimentDetailPage />);
    const s = await section();
    fireEvent.click(editButton()!);
    fireEvent.click(within(s).getByRole('button', { name: 'Save rules' }));

    const alert = await within(s).findByRole('alert');
    expect(alert).toHaveTextContent(detail);
    await waitFor(() => expect(alert).toHaveFocus());
  });
});
