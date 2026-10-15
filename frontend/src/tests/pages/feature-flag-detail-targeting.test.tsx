/**
 * Targeting rules on the flag page (#535, PR A).
 *
 * - Stored rules the builder cannot show are displayed read-only, with
 *   "Replace rules" as the only change offered.
 * - A save sends `targeting_rules` only when the rules were changed or
 *   replaced, so a save that moves only the rollout percentage never touches
 *   the stored rules (before: it sent what the builder showed, `null` for
 *   rules it could not read, and erased them).
 * - Incomplete rules are stopped before sending; a 422 on `targeting_rules`
 *   is shown inside the Targeting Rules section, in an alert that takes focus.
 * - The flag builder offers version operators on any attribute outside the
 *   suggested ones, so the StreamPulse demo flags stay editable here, while
 *   the experiment page keeps them read-only.
 * - Rules the builder cannot show only because of a NOT group are shown as
 *   stored under NOT_GROUPS_NOTE, which says they are applied as written,
 *   instead of the general note that says they may not be (#918). A save
 *   that moves only the rollout still leaves them untouched.
 */
import React from 'react';
import fs from 'fs';
import path from 'path';
import axe from 'axe-core';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import FeatureFlagDetailPage from '@/pages/feature-flags/[id]';
import { OUTSIDE_BUILDER_NOTE } from '@/components/experiments/TargetingSection';
import { ApiError, ApiFetchOptions, apiFetch } from '@/services/api';
import { FeatureFlag } from '@/services/featureFlags';
import { isEditableTargeting } from '@/utils/experimentTargeting';
import { NOT_GROUPS_NOTE, isEditableFlagTargeting, targetingToSend, usesNotGroups } from '@/utils/flagTargeting';
import { FLAG_OPERATOR_OPTIONS, createEmptyRules, getOperatorsForAttribute, jsonToRules } from '@/utils/targeting';
import { apiError, makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/feature-flags/[id]', query: { id: 'flag-1' } });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const STREAMPULSE: Record<string, Record<string, unknown>> = JSON.parse(
  fs.readFileSync(path.join(__dirname, '..', 'fixtures', 'streampulse-flag-rules.json'), 'utf8'),
);

/** Builder-written rules: group and condition ids, and no logical operator anywhere. */
const BUILDER_IDS_NO_OPERATOR = {
  groups: [{ id: 'g-1', conditions: [{ id: 'c-1', attribute: 'user.plan', operator: 'equals', value: 'pro' }] }],
};

/** Shapes the builder cannot show. */
const NATIVE = { rules: [{ id: 'r-1', conditions: [{ attribute: 'country', operator: 'equals', value: 'DE' }] }] };
const LEGACY_LIST = [{ type: 'context', conditions: [{ attribute: 'plan', operator: 'eq', value: 'pro' }] }];
const SEED_OPERATOR_RULES = { operator: 'and', rules: [] };
const UNKNOWN_OPERATOR = {
  logical_operator: 'AND',
  groups: [{ logical_operator: 'AND', conditions: [{ attribute: 'country', operator: 'equalz', value: 'DE' }] }],
};

/**
 * NOT groups (#918). The API accepts `not` in any case, stores it as sent and
 * applies it; the builder has no NOT. Each shape would be editable if its
 * NOT were an AND, so NOT is the only reason the page shows it as stored.
 */
const NOT_DE = { attribute: 'country', operator: 'equals', value: 'DE' };
const GROUP_NOT = { logical_operator: 'AND', groups: [{ logical_operator: 'NOT', conditions: [NOT_DE] }] };
const TOP_LEVEL_NOT = { logical_operator: 'NOT', groups: [{ logical_operator: 'AND', conditions: [NOT_DE] }] };
const LOWERCASE_NOT = { logical_operator: 'AND', groups: [{ logical_operator: 'not', conditions: [NOT_DE] }] };
const MIXED_CASE_NOT = { logical_operator: 'and', groups: [{ logical_operator: 'not', conditions: [NOT_DE] }] };
const NOT_SHAPES: [string, unknown][] = [
  ['a group NOT', GROUP_NOT],
  ['a top-level NOT', TOP_LEVEL_NOT],
  ['a lowercase not', LOWERCASE_NOT],
  ['a mixed-case and with a not group', MIXED_CASE_NOT],
];

function flag(rules: unknown, overrides: Partial<FeatureFlag> = {}): FeatureFlag {
  return {
    id: 'flag-1',
    key: 'checkout_v2',
    name: 'Checkout v2',
    description: null,
    status: 'inactive',
    rollout_percentage: 25,
    rules: rules as FeatureFlag['rules'],
    owner_id: 'user-1',
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-05T10:00:00Z',
    ...overrides,
  };
}

type PutHandler = (body: Record<string, unknown>) => unknown;

/**
 * Routes the page's calls against a stored flag that a PUT updates the way
 * the API does (an omitted field is kept); returns every PUT body sent.
 */
function install(rules: unknown, onPut?: PutHandler) {
  let state = flag(rules);
  const puts: Record<string, unknown>[] = [];
  mockedApiFetch.mockImplementation(
    routedApi([
      { path: '/api/v1/feature-flags/flag-1', handler: () => state },
      { path: '/api/v1/rollout-schedules', handler: () => ({ items: [], total: 0, skip: 0, limit: 50 }) },
      {
        path: '/api/v1/safety/feature-flags/flag-1/check',
        handler: () => ({ feature_flag_id: 'flag-1', is_healthy: true, metrics: [], last_checked: '2026-09-11T08:00:00Z' }),
      },
      {
        method: 'PUT',
        path: '/api/v1/feature-flags/flag-1',
        handler: (_p: string, options: ApiFetchOptions) => {
          const body = options.json as Record<string, unknown>;
          puts.push(body);
          if (onPut) return onPut(body);
          state = {
            ...state,
            rollout_percentage: (body.rollout_percentage as number) ?? state.rollout_percentage,
            ...('targeting_rules' in body ? { rules: body.targeting_rules as FeatureFlag['rules'] } : {}),
          };
          return state;
        },
      },
    ]) as unknown as typeof apiFetch,
  );
  return puts;
}

async function section() {
  await screen.findByTestId('flag-detail');
  return screen.getByTestId('flag-targeting-section');
}

async function slideAndSave(value: string) {
  fireEvent.change(screen.getByTestId('rollout-percentage'), { target: { value } });
  fireEvent.click(screen.getByTestId('save-flag'));
  await screen.findByTestId('save-success');
}

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe('the change test', () => {
  const stored = { ...BUILDER_IDS_NO_OPERATOR, id: 'rule-1' };

  it('an untouched builder sends nothing, for builder ids and no logical operator', () => {
    expect(targetingToSend(jsonToRules(stored), stored, false)).toBeUndefined();
  });

  it('an edited builder sends the dashboard shape, with the stored rule id', () => {
    const rules = jsonToRules(stored);
    rules.groups[0].conditions[0].value = 'team';
    expect(targetingToSend(rules, stored, false)).toEqual({
      id: 'rule-1',
      logical_operator: 'AND',
      groups: [{ logical_operator: 'AND', conditions: [{ attribute: 'user.plan', operator: 'equals', value: 'team' }] }],
    });
  });

  it('read-only rules send nothing; a confirmed replacement always sends, {} when empty', () => {
    expect(targetingToSend(null, NATIVE, false)).toBeUndefined();
    expect(targetingToSend(createEmptyRules(), NATIVE, true)).toEqual({});
    expect(targetingToSend(createEmptyRules(), {}, true)).toEqual({});
  });

  it('no rules stored and none added sends nothing', () => {
    expect(targetingToSend(createEmptyRules(), null, false)).toBeUndefined();
    expect(targetingToSend(createEmptyRules(), {}, false)).toBeUndefined();
  });
});

describe('a save that moves only the rollout', () => {
  it.each([
    ['editable rules with builder ids and no logical operator', BUILDER_IDS_NO_OPERATOR],
    ['the native shape', NATIVE],
    ['the legacy list', LEGACY_LIST],
    ['the seed operator/rules value', SEED_OPERATOR_RULES],
    ['an operator the builder does not offer', UNKNOWN_OPERATOR],
    ['a value that is not an object', 42],
    ...NOT_SHAPES,
  ])('sends no targeting_rules: %s', async (_name, rules) => {
    const puts = install(rules);
    render(<FeatureFlagDetailPage />);
    await section();
    await slideAndSave('60');
    expect(puts).toEqual([{ rollout_percentage: 60 }]);
  });

  it('after a rules change has been saved, the next slider-only save sends none', async () => {
    const puts = install(BUILDER_IDS_NO_OPERATOR);
    render(<FeatureFlagDetailPage />);
    const s = await section();
    fireEvent.change(within(s).getByLabelText('Group 1, condition 1 value'), { target: { value: 'team' } });
    fireEvent.click(screen.getByTestId('save-flag'));
    await screen.findByTestId('save-success');
    expect(puts[0]).toEqual({
      rollout_percentage: 25,
      targeting_rules: {
        logical_operator: 'AND',
        groups: [{ logical_operator: 'AND', conditions: [{ attribute: 'user.plan', operator: 'equals', value: 'team' }] }],
      },
    });

    fireEvent.change(screen.getByTestId('rollout-percentage'), { target: { value: '70' } });
    fireEvent.click(screen.getByTestId('save-flag'));
    await waitFor(() => expect(puts).toHaveLength(2));
    expect(puts[1]).toEqual({ rollout_percentage: 70 });
  });
});

describe('stored rules the builder cannot show', () => {
  it('are shown read-only under the note, with only "Replace rules" offered', async () => {
    install(NATIVE);
    render(<FeatureFlagDetailPage />);
    const s = await section();
    expect(within(s).getByTestId('targeting-raw-note')).toHaveTextContent(OUTSIDE_BUILDER_NOTE);
    expect(within(s).getByTestId('targeting-raw-note')).not.toHaveTextContent(NOT_GROUPS_NOTE);
    expect(within(s).getByRole('link', { name: 'Read about targeting rules' })).toHaveAttribute(
      'href',
      expect.stringContaining('targeting-rules'),
    );
    expect(JSON.parse(within(s).getByTestId('targeting-raw-json').textContent ?? '')).toEqual(NATIVE);
    expect(within(s).getByTestId('targeting-raw')).toBeInTheDocument();
    expect(within(s).queryByTestId('add-group')).toBeNull();
    expect(within(s).queryByRole('button', { name: '+ Add Group' })).toBeNull();
    expect(within(s).getByRole('button', { name: 'Replace rules' })).toBeInTheDocument();
  });

  it('replace, cancel: nothing changes and nothing is sent', async () => {
    const puts = install(NATIVE);
    render(<FeatureFlagDetailPage />);
    const s = await section();
    fireEvent.click(within(s).getByRole('button', { name: 'Replace rules' }));
    expect(within(s).getByRole('dialog', { name: 'Replace rules' })).toBeInTheDocument();
    fireEvent.click(within(s).getByRole('button', { name: 'Cancel' }));
    expect(within(s).getByTestId('targeting-raw')).toBeInTheDocument();
    await slideAndSave('30');
    expect(puts).toEqual([{ rollout_percentage: 30 }]);
  });

  it('replace, confirm, leave empty, save: sends targeting_rules {}', async () => {
    const puts = install(NATIVE);
    render(<FeatureFlagDetailPage />);
    const s = await section();
    fireEvent.click(within(s).getByRole('button', { name: 'Replace rules' }));
    fireEvent.click(within(s).getByRole('button', { name: 'Start with no rules' }));
    expect(within(s).getByText('No targeting rules — all users will match')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('save-flag'));
    await screen.findByTestId('save-success');
    expect(puts).toEqual([{ rollout_percentage: 25, targeting_rules: {} }]);
    // The saved value is the new baseline: the builder now shows it, and a
    // further save sends no rules.
    expect(within(s).queryByTestId('targeting-raw')).toBeNull();
    fireEvent.change(screen.getByTestId('rollout-percentage'), { target: { value: '40' } });
    fireEvent.click(screen.getByTestId('save-flag'));
    await waitFor(() => expect(puts).toHaveLength(2));
    expect(puts[1]).toEqual({ rollout_percentage: 40 });
  });

  it('replace, then "Keep the stored rules" goes back to the read-only view', async () => {
    const puts = install(LEGACY_LIST);
    render(<FeatureFlagDetailPage />);
    const s = await section();
    fireEvent.click(within(s).getByRole('button', { name: 'Replace rules' }));
    fireEvent.click(within(s).getByRole('button', { name: 'Start with no rules' }));
    fireEvent.click(within(s).getByRole('button', { name: 'Keep the stored rules' }));
    expect(within(s).getByTestId('targeting-raw')).toBeInTheDocument();
    await slideAndSave('35');
    expect(puts).toEqual([{ rollout_percentage: 35 }]);
  });
});

describe('NOT groups (#918)', () => {
  it.each(NOT_SHAPES)('usesNotGroups is true for %s, which the builder cannot show', (_name, rules) => {
    expect(usesNotGroups(rules)).toBe(true);
    expect(isEditableFlagTargeting(rules)).toBe(false);
  });

  it.each([
    ['a NOT rule that also has a rollout_percentage', { ...GROUP_NOT, rollout_percentage: 50 }],
    ['the native shape', NATIVE],
    ['an AND/OR rule', { logical_operator: 'OR', groups: [{ logical_operator: 'AND', conditions: [NOT_DE] }] }],
    ['no rules', null],
  ])('usesNotGroups is false for %s', (_name, rules) => {
    expect(usesNotGroups(rules)).toBe(false);
  });

  it.each(NOT_SHAPES)('%s is shown as stored under the NOT note, with "Replace rules"', async (_name, rules) => {
    install(rules);
    render(<FeatureFlagDetailPage />);
    const s = await section();
    const note = within(s).getByTestId('targeting-raw-note');
    expect(note).toHaveTextContent(NOT_GROUPS_NOTE);
    expect(note).not.toHaveTextContent(OUTSIDE_BUILDER_NOTE);
    expect(within(s).getByRole('link', { name: 'Read about targeting rules' })).toHaveAttribute(
      'href',
      expect.stringContaining('targeting-rules'),
    );
    expect(JSON.parse(within(s).getByTestId('targeting-raw-json').textContent ?? '')).toEqual(rules);
    expect(within(s).getByTestId('targeting-raw')).toBeInTheDocument();
    expect(within(s).queryByTestId('add-group')).toBeNull();
    expect(within(s).getByRole('button', { name: 'Replace rules' })).toBeInTheDocument();
  });

  it('a NOT rule that is not editable for another reason keeps the general note', async () => {
    install({ ...GROUP_NOT, rollout_percentage: 50 });
    render(<FeatureFlagDetailPage />);
    const s = await section();
    const note = within(s).getByTestId('targeting-raw-note');
    expect(note).toHaveTextContent(OUTSIDE_BUILDER_NOTE);
    expect(note).not.toHaveTextContent(NOT_GROUPS_NOTE);
  });

  it('replace, confirm, leave empty, save: a NOT rule is replaced only through the confirmed path', async () => {
    const puts = install(GROUP_NOT);
    render(<FeatureFlagDetailPage />);
    const s = await section();
    fireEvent.click(within(s).getByRole('button', { name: 'Replace rules' }));
    expect(within(s).getByRole('dialog', { name: 'Replace rules' })).toBeInTheDocument();
    fireEvent.click(within(s).getByRole('button', { name: 'Start with no rules' }));
    fireEvent.click(screen.getByTestId('save-flag'));
    await screen.findByTestId('save-success');
    expect(puts).toEqual([{ rollout_percentage: 25, targeting_rules: {} }]);
  });
});

describe('a save the rules stop', () => {
  it('stops an unfinished condition before sending, in a focused alert in the section', async () => {
    const puts = install(null);
    render(<FeatureFlagDetailPage />);
    const s = await section();
    fireEvent.click(within(s).getByRole('button', { name: '+ Add Group' }));
    fireEvent.click(screen.getByTestId('save-flag'));

    const alert = await within(s).findByRole('alert');
    expect(alert).toHaveTextContent('Finish or remove these conditions before saving:');
    expect(within(alert).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Group 1, Condition 1: attribute is required',
      'Group 1, Condition 1: value is required for operator "equals"',
    ]);
    await waitFor(() => expect(alert).toHaveFocus());
    expect(puts).toEqual([]);
  });

  it('does not stop a slider-only save over stored rules the check would flag', async () => {
    const blank = { groups: [{ conditions: [{ attribute: 'user.plan', operator: 'equals', value: '' }] }] };
    const puts = install(blank);
    render(<FeatureFlagDetailPage />);
    await section();
    await slideAndSave('45');
    expect(puts).toEqual([{ rollout_percentage: 45 }]);
  });

  it('shows a 422 on targeting_rules inside the section, focused, as a list', async () => {
    install(BUILDER_IDS_NO_OPERATOR, () => {
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
    render(<FeatureFlagDetailPage />);
    const s = await section();
    fireEvent.change(within(s).getByLabelText('Group 1, condition 1 value'), { target: { value: 'team' } });
    fireEvent.click(screen.getByTestId('save-flag'));

    const alert = await within(s).findByRole('alert');
    expect(alert).toHaveTextContent('The rules were not accepted:');
    expect(within(alert).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Group 1, Condition 1: unknown operator',
    ]);
    await waitFor(() => expect(alert).toHaveFocus());
    expect(screen.queryByTestId('save-error')).toBeNull();
  });

  it('shows the size limit refusal (#828) as the API words it, under the same heading', async () => {
    install(BUILDER_IDS_NO_OPERATOR, () => {
      throw new ApiError({
        status: 422,
        detail: [
          {
            loc: ['body', 'targeting_rules'],
            msg: 'Value error, targeting rules: at most 1000 groups and conditions are allowed',
            type: 'value_error',
          },
        ],
      });
    });
    render(<FeatureFlagDetailPage />);
    const s = await section();
    fireEvent.change(within(s).getByLabelText('Group 1, condition 1 value'), { target: { value: 'team' } });
    fireEvent.click(screen.getByTestId('save-flag'));

    const alert = await within(s).findByRole('alert');
    expect(alert).toHaveTextContent('The rules were not accepted:');
    expect(within(alert).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'targeting rules: at most 1000 groups and conditions are allowed',
    ]);
    await waitFor(() => expect(alert).toHaveFocus());
    expect(screen.queryByTestId('save-error')).toBeNull();
  });

  it('any other failure goes to the save-error alert, which takes focus', async () => {
    install(null, () => {
      throw apiError(500, 'Failed to update feature flag');
    });
    render(<FeatureFlagDetailPage />);
    await section();
    fireEvent.click(screen.getByTestId('save-flag'));
    const box = await screen.findByTestId('save-error');
    expect(box).toHaveAttribute('role', 'alert');
    expect(box).toHaveTextContent('Failed to update feature flag');
    await waitFor(() => expect(box).toHaveFocus());
  });
});

describe('version operators on the flag page', () => {
  it.each([
    ['StreamPulse AI search (os_version semver_gte)', STREAMPULSE.ai_search],
    ['StreamPulse player v2 after story step 5 (app_version semver_gte)', STREAMPULSE.player_v2_after_story_step_5],
    ['StreamPulse player v2 as seeded', STREAMPULSE.player_v2],
  ])('%s is editable on the flag page', (_name, rules) => {
    expect(isEditableFlagTargeting(rules)).toBe(true);
  });

  it.each([
    ['StreamPulse AI search', STREAMPULSE.ai_search],
    ['StreamPulse player v2 after story step 5', STREAMPULSE.player_v2_after_story_step_5],
  ])('%s stays read-only for the experiment page predicate', (_name, rules) => {
    expect(isEditableTargeting(rules)).toBe(false);
  });

  it('the flag builder offers semver for attributes outside the suggestions only', () => {
    expect(getOperatorsForAttribute('os_version', FLAG_OPERATOR_OPTIONS)).toEqual(
      expect.arrayContaining(['equals', 'semver_gte', 'semver_lte']),
    );
    expect(getOperatorsForAttribute('app.version', FLAG_OPERATOR_OPTIONS)).toEqual(getOperatorsForAttribute('app.version'));
    for (const attribute of ['user.country', 'user.age', 'session.new_user', 'user.tags']) {
      expect(getOperatorsForAttribute(attribute, FLAG_OPERATOR_OPTIONS)).toEqual(getOperatorsForAttribute(attribute));
      expect(getOperatorsForAttribute(attribute, FLAG_OPERATOR_OPTIONS)).not.toContain('semver_gte');
    }
    expect(getOperatorsForAttribute('os_version')).not.toContain('semver_gte');
  });

  it('opens the AI search rules in the builder, with the version operator selected, and a slider save sends none', async () => {
    const puts = install(STREAMPULSE.ai_search);
    render(<FeatureFlagDetailPage />);
    const s = await section();
    expect(within(s).queryByTestId('targeting-raw')).toBeNull();
    const operator = within(s).getByLabelText('Group 1, condition 2 operator') as HTMLSelectElement;
    expect(operator.value).toBe('semver_gte');
    expect(within(operator).getByRole('option', { name: 'version >=' })).toBeInTheDocument();
    await slideAndSave('10');
    expect(puts).toEqual([{ rollout_percentage: 10 }]);
  });
});

describe('flag page accessibility (axe-core in jsdom; colour contrast is not computable here)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  async function violations(node: Element) {
    const result = await axe.run(node, axeOptions);
    return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`);
  }

  it('has no violations with a refusal shown', async () => {
    install(BUILDER_IDS_NO_OPERATOR, () => {
      throw new ApiError({
        status: 422,
        detail: [{ loc: ['body', 'targeting_rules'], msg: 'Value error, groups[0].conditions[0].value: value is not valid for the operator', type: 'value_error' }],
      });
    });
    const { container } = render(<FeatureFlagDetailPage />);
    const s = await section();
    fireEvent.change(within(s).getByLabelText('Group 1, condition 1 value'), { target: { value: 'team' } });
    fireEvent.click(screen.getByTestId('save-flag'));
    await within(s).findByRole('alert');
    expect(await violations(container)).toEqual([]);
  });

  it('has no violations with incomplete rules stopped', async () => {
    install(null);
    const { container } = render(<FeatureFlagDetailPage />);
    const s = await section();
    fireEvent.click(within(s).getByRole('button', { name: '+ Add Group' }));
    fireEvent.click(screen.getByTestId('save-flag'));
    await within(s).findByRole('alert');
    expect(await violations(container)).toEqual([]);
  });

  it('has no violations with stored rules shown read-only, and with the replace dialog open', async () => {
    install(NATIVE);
    const { container } = render(<FeatureFlagDetailPage />);
    const s = await section();
    expect(await violations(container)).toEqual([]);
    fireEvent.click(within(s).getByRole('button', { name: 'Replace rules' }));
    expect(await violations(container)).toEqual([]);
  });
});
