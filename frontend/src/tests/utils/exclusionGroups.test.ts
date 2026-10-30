/**
 * The exclusion-group pages' rules (#1455): the share's units (QA #17), the
 * name check (PE 4: exact and case-sensitive, as the API's unique constraint
 * compares), what a rename sends, and the fixed copy for each failure.
 */
import { ApiError } from '@/services/api';
import {
  GROUP_COPY,
  draftProblems,
  groupChanges,
  groupErrorCopy,
  nameTaken,
  shareFraction,
  sharePercent,
} from '@/utils/exclusionGroups';
import { UNREACHABLE } from '@/components/experiments/ExperimentManageSection';

const GROUPS = [
  { id: 'g1', name: 'Checkout' },
  { id: 'g2', name: 'Pricing page' },
];

describe('the share of traffic: shown as a whole percent, sent as a fraction', () => {
  it.each([
    ['50', 0.5],
    ['0', 0],
    ['100', 1],
    ['7', 0.07],
    [' 25 ', 0.25],
  ])('%p sends %p', (text, fraction) => {
    expect(shareFraction(text)).toBe(fraction);
  });

  it.each(['', '-1', '101', '50.5', '1e2', 'abc', '1000'])('%p is refused', (text) => {
    expect(shareFraction(text)).toBeNull();
  });

  it.each([
    [0.5, 50],
    [1, 100],
    [0, 0],
    [0.333, 33],
  ])('a stored %p shows as %p', (fraction, percent) => {
    expect(sharePercent(fraction)).toBe(percent);
  });
});

describe('the name check (exact, case included)', () => {
  it('finds the same name', () => {
    expect(nameTaken('Checkout', GROUPS)).toBe(true);
  });

  it.each(['checkout', 'CHECKOUT', 'Checkout ', ' Checkout', 'Pricing'])('%p is another name', (name) => {
    expect(nameTaken(name, GROUPS)).toBe(false);
  });

  it('lets a group keep its own name on a rename', () => {
    expect(nameTaken('Checkout', GROUPS, 'g1')).toBe(false);
    expect(nameTaken('Pricing page', GROUPS, 'g1')).toBe(true);
  });

  it('names each problem, in field order', () => {
    expect(draftProblems({ name: '', description: '', share: '100' }, GROUPS)).toEqual(['name-required']);
    expect(draftProblems({ name: '   ', description: '', share: '101' }, GROUPS)).toEqual([
      'name-required',
      'share-invalid',
    ]);
    expect(draftProblems({ name: 'Checkout', description: '', share: '50' }, GROUPS)).toEqual(['name-taken']);
    expect(draftProblems({ name: 'checkout', description: '', share: '50' }, GROUPS)).toEqual([]);
  });
});

describe('a rename or traffic change sends what changed, and nothing else', () => {
  const group = {
    id: 'g1',
    name: 'Checkout',
    description: null,
    traffic_allocation: 0.333,
    status: 'active',
    experiments: [],
  };

  it('an unchanged form sends nothing, so a stored 0.333 is never rounded to 0.33', () => {
    expect(groupChanges(group, { name: 'Checkout', description: '', share: '33' })).toEqual({});
  });

  it('a new name only', () => {
    expect(groupChanges(group, { name: 'checkout', description: '', share: '33' })).toEqual({ name: 'checkout' });
  });

  it('a new share only, as a fraction: 50% sends 0.5', () => {
    expect(groupChanges(group, { name: 'Checkout', description: '', share: '50' })).toEqual({
      traffic_allocation: 0.5,
    });
  });

  it('a description', () => {
    expect(groupChanges(group, { name: 'Checkout', description: 'Checkout flow', share: '33' })).toEqual({
      description: 'Checkout flow',
    });
  });
});

describe('every failure reads as fixed copy, chosen by its status', () => {
  const planted = (status: number) => new ApiError({ status, detail: 'PLANTED-detail', message: 'PLANTED-message' });

  it.each([
    ['save', 409, GROUP_COPY.nameTaken],
    ['save', 403, GROUP_COPY.refused],
    ['save', 404, GROUP_COPY.notFound],
    ['save', 422, GROUP_COPY.saveFailed],
    ['save', 500, GROUP_COPY.saveFailed],
    ['add', 400, GROUP_COPY.inAnotherGroup],
    ['add', 403, GROUP_COPY.refused],
    ['remove', 400, GROUP_COPY.saveFailed],
    ['archive', 403, GROUP_COPY.archiveAdminOnly],
    ['load', 403, GROUP_COPY.refused],
    ['load', 404, GROUP_COPY.notFound],
    ['load', 500, GROUP_COPY.loadFailed],
    ['save', 0, UNREACHABLE],
  ] as const)('%s, %i', (action, status, copy) => {
    const text = groupErrorCopy(action, planted(status));
    expect(text).toBe(copy);
    expect(text).not.toContain('PLANTED');
  });

  it('a failure that is not an API error is the fallback', () => {
    expect(groupErrorCopy('save', new Error('PLANTED'))).toBe(GROUP_COPY.saveFailed);
  });
});
