/**
 * The balance setting's pure parts: the split check, the stored setting
 * read back into the form and sent out again, and how a refusal's place tells
 * which control it is about.
 */
import {
  balanceStateFromStored,
  balanceSummary,
  buildBalancePayload,
  sameBalance,
  splitProblem,
  closedKeyNote,
  takeBalanceIssue,
} from '@/components/experiments/new/balanceWithin';
import {
  createInitialFormState,
  experimentFormReducer,
  ExperimentFormAction,
  ExperimentFormState,
} from '@/components/experiments/new/formState';

const run = (state: ExperimentFormState, ...actions: ExperimentFormAction[]) =>
  actions.reduce(experimentFormReducer, state);

const STEPS_TAIL = ', such as 50/50, 20/80, 35/35/30 or 40/30/30; ';

describe('splitProblem', () => {
  it.each([
    [[50, 50]],
    [[20, 80]],
    [[35, 35, 30]],
    [[40, 30, 30]],
    [[100]],
    [[5, 95]],
  ])('%j is allowed', (shares) => {
    expect(splitProblem(shares)).toBeNull();
  });

  it.each([
    [[34, 33, 33], '34/33/33'],
    [[33, 67], '33/67'],
    [[1, 99], '1/99'],
    [[25, 25, 26, 24], '25/25/26/24'],
  ])('%j is refused, naming the split as typed', (shares, text) => {
    expect(splitProblem(shares)).toBe(`To balance within groups, every share must be a multiple of 5${STEPS_TAIL}${text} is not.`);
  });

  it('says nothing about a split that does not add up to 100 yet', () => {
    expect(splitProblem([34, 33])).toBeNull();
    expect(splitProblem([])).toBeNull();
    expect(splitProblem([60, 60])).toBeNull();
  });

  it('a share left at 0 does not count against the split', () => {
    expect(splitProblem([50, 50, 0])).toBeNull();
  });
});

describe('the stored setting and the form', () => {
  it.each([
    [{ attribute: 'platform', values: ['ios', 'android'] }],
    [{ attribute: 'tier', values: ['gold'] }],
    [{ attribute: 'web_device' }],
    [{ segments: ['8d4c6f0e-3a1b-4c2d-9e8f-7a6b5c4d3e2f'] }],
  ])('%j is read into the form and sent back as it was', (stored) => {
    const state = balanceStateFromStored(stored);
    expect(state.on).toBe(true);
    expect(sameBalance(buildBalancePayload(state), stored)).toBe(true);
  });

  it('null, undefined and a value that is not an object are off', () => {
    for (const stored of [null, undefined, 'x' as never, 3 as never]) {
      expect(balanceStateFromStored(stored).on).toBe(false);
      expect(buildBalancePayload(balanceStateFromStored(stored))).toBeNull();
    }
  });

  it('an open key sends its list even when it is empty; a closed key sends none unless it has values', () => {
    const open = { ...balanceStateFromStored({ attribute: 'tier', values: [] }), values: [] as string[] };
    expect(buildBalancePayload(open)).toEqual({ attribute: 'tier', values: [] });
    const closed = balanceStateFromStored({ attribute: 'web_visitor' });
    expect(buildBalancePayload(closed)).toEqual({ attribute: 'web_visitor' });
  });

  it('null and absent are the same setting, and a different list is not', () => {
    expect(sameBalance(null, undefined)).toBe(true);
    expect(sameBalance({ attribute: 'tier', values: ['a'] }, { attribute: 'tier', values: ['a', 'b'] })).toBe(false);
    expect(sameBalance({ segments: ['a', 'b'] }, { segments: ['b', 'a'] })).toBe(false);
  });
});

describe('the values belong to the key they were listed for', () => {
  const withValues = () =>
    run(
      createInitialFormState(),
      { type: 'setBalanceOn', on: true },
      { type: 'addBalanceValue', value: 'ios' },
      { type: 'addBalanceValue', value: 'android' },
    );

  it('changing "Group users by" drops the values listed for the old key', () => {
    const before = withValues();
    expect(before.balance.values).toEqual(['ios', 'android']);
    for (const groupBy of ['country', 'user_tier', 'custom', 'segments'] as const) {
      const after = run(before, { type: 'setBalanceGroupBy', groupBy });
      expect(after.balance.values).toEqual([]);
      expect(buildBalancePayload(after.balance)).toEqual(groupBy === 'segments' ? { segments: [] } : { attribute: groupBy === 'custom' ? '' : groupBy, values: [] });
    }
  });

  it('typing in the custom key keeps the values; settling on another key drops them', () => {
    const custom = run(withValues(), { type: 'setBalanceGroupBy', groupBy: 'custom' }, { type: 'setBalanceCustomKey', customKey: 'tier' }, { type: 'addBalanceValue', value: 'gold' });
    const typing = run(custom, { type: 'setBalanceCustomKey', customKey: 'tie' }, { type: 'setBalanceCustomKey', customKey: 'tier' });
    expect(typing.balance.values).toEqual(['gold']);
    expect(run(typing, { type: 'commitBalanceCustomKey', from: 'tier' }).balance.values).toEqual(['gold']);

    const changed = run(typing, { type: 'setBalanceCustomKey', customKey: 'plan' });
    expect(changed.balance.values).toEqual(['gold']);
    const settled = run(changed, { type: 'commitBalanceCustomKey', from: 'tier' });
    expect(settled.balance.values).toEqual([]);
    expect(buildBalancePayload(settled.balance)).toEqual({ attribute: 'plan', values: [] });
  });
});

describe('closedKeyNote', () => {
  it('names the API\'s closed sets, and for a browser says what any other one is', () => {
    expect(closedKeyNote('web_device')).toBe('web_device is phone, tablet or desktop: there is nothing to list.');
    expect(closedKeyNote('web_visitor')).toBe('web_visitor is new or returning: there is nothing to list.');
    expect(closedKeyNote('web_browser')).toBe(
      'web_browser is chrome, edge, firefox, safari, samsung or opera, and any other browser is balanced as other: there is nothing to list.',
    );
    expect(closedKeyNote('tier')).toBeNull();
  });
});

describe('balanceSummary', () => {
  it('names a breakdown key by its label and any other key as typed', () => {
    expect(balanceSummary({ attribute: 'user_tier', values: ['gold'] }, null)).toBe('within User Tier (gold)');
    expect(balanceSummary({ attribute: 'plan', values: ['a', 'b'] }, null)).toBe('within plan (a, b)');
    expect(balanceSummary(null, null)).toBe('off');
  });

  it('never shows a segment id: a name, a count while they load, or "an unknown segment"', () => {
    const setting = { segments: ['id-1', 'id-2'] };
    expect(balanceSummary(setting, new Map([['id-1', 'VIP']]))).toBe('within segments VIP, an unknown segment');
    expect(balanceSummary(setting, null)).toBe('within 2 saved segments');
    expect(balanceSummary({ segments: ['id-1'] }, null)).toBe('within 1 saved segment');
  });
});

describe('takeBalanceIssue', () => {
  const state = balanceStateFromStored({ attribute: 'tier', values: ['a'] });
  const segmentState = balanceStateFromStored({ segments: ['x'] });
  const item = (loc: unknown[], msg: string) => ({ loc, msg, type: 'value_error' });

  it('puts a refusal at the control its place names', () => {
    expect(takeBalanceIssue([item(['body', 'balance_within', 'attribute'], 'A')], state, [50, 50]).issue).toEqual({
      message: 'A',
      field: 'attribute',
    });
    expect(takeBalanceIssue([item(['body', 'balance_within', 'segments'], 'B')], state, [50, 50]).issue?.field).toBe(
      'segments',
    );
  });

  it('a refusal of the setting as a whole is about the values, the segments, or the split', () => {
    const whole = (msg: string) => [item(['body', 'balance_within'], msg)];
    expect(takeBalanceIssue(whole('List the values'), state, [50, 50]).issue?.field).toBe('values');
    expect(takeBalanceIssue(whole('Balance within an attribute'), segmentState, [50, 50]).issue?.field).toBe('segments');
    expect(takeBalanceIssue(whole('anything'), state, [34, 33, 33]).issue?.field).toBe('split');
    expect(
      takeBalanceIssue(whole(`To balance within groups, every share must be a multiple of 5${STEPS_TAIL}x is not.`), state, [50, 50])
        .issue?.field,
    ).toBe('split');
  });

  it('keeps the rest of the body, takes the first refusal, and strips a "Value error" prefix', () => {
    const other = item(['body', 'name'], 'Name is too long');
    const result = takeBalanceIssue(
      [other, item(['body', 'balance_within', 'attribute'], 'Value error, A'), item(['body', 'balance_within'], 'B')],
      state,
      [50, 50],
    );
    expect(result.issue).toEqual({ message: 'A', field: 'attribute' });
    expect(result.rest).toEqual([other]);
  });

  it('finds nothing in a body that is not about the setting', () => {
    expect(takeBalanceIssue([item(['body', 'name'], 'x')], state, [50, 50])).toEqual({ issue: null, rest: undefined });
    expect(takeBalanceIssue('a string', state, [50, 50])).toEqual({ issue: null, rest: undefined });
    expect(takeBalanceIssue([item(['body', 'balance_within'], '  ')], state, [50, 50]).issue).toBeNull();
  });
});
