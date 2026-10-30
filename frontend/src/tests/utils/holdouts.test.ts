/**
 * The global-holdout page's rules (#1455): where a holdout is in its life,
 * when its size locks, the size's units (a whole percent, QA #17), the name
 * check (exact and case-sensitive), what an edit sends, and fixed copy.
 */
import { ApiError } from '@/services/api';
import { UNREACHABLE } from '@/components/experiments/ExperimentManageSection';
import { HoldoutDetail } from '@/types/eligibility';
import {
  HOLDOUT_COPY,
  canActivate,
  hasBeenActive,
  holdoutChanges,
  holdoutErrorCopy,
  holdoutLabel,
  holdoutNameTaken,
  holdoutStatus,
  points,
  pValueText,
  ratePercent,
  sizePercent,
} from '@/utils/holdouts';

const base: HoldoutDetail = { id: 'h1', name: 'Q4', holdout_percentage: 5, is_active: false };
const never = base;
const active = { ...base, is_active: true, activated_at: '2026-10-01T09:00:00' };
const legacyActive = { ...base, is_active: true, activated_at: null };
const ended = { ...base, activated_at: '2026-06-01T09:00:00', deactivated_at: '2026-09-01T09:00:00' };
const endedLegacy = { ...base, deactivated_at: '2026-09-01T09:00:00' };

describe('a holdout in its life, as the API decides it', () => {
  it.each([
    ['never activated', never, 'not_activated', false, true],
    ['active', active, 'active', true, false],
    ['active since before membership was recorded', legacyActive, 'active', true, false],
    ['ended', ended, 'ended', true, false],
    ['ended, never stamped activated', endedLegacy, 'ended', true, false],
  ] as const)('%s', (_, holdout, status, locked, activatable) => {
    expect(holdoutStatus(holdout)).toBe(status);
    expect(hasBeenActive(holdout)).toBe(locked);
    expect(canActivate(holdout)).toBe(activatable);
  });
});

describe('the size: a whole percent from 1 to 20, sent as is (5% is 5)', () => {
  it.each([
    ['5', 5],
    ['1', 1],
    ['20', 20],
    [' 7 ', 7],
  ])('%p sends %p', (text, value) => {
    expect(sizePercent(text)).toBe(value);
  });

  it.each(['0', '21', '2.5', '', '-3', 'abc', '100'])('%p is refused', (text) => {
    expect(sizePercent(text)).toBeNull();
  });
});

describe('names: exact, case included', () => {
  const list = [{ id: 'h1', name: 'Q4 holdout' }];
  it('the same name clashes; another case does not; a holdout keeps its own', () => {
    expect(holdoutNameTaken('Q4 holdout', list)).toBe(true);
    expect(holdoutNameTaken('q4 holdout', list)).toBe(false);
    expect(holdoutNameTaken('Q4 holdout', list, 'h1')).toBe(false);
  });
});

describe('an edit sends what changed, and never a size once the holdout has been active', () => {
  it('a new size for a holdout never activated', () => {
    expect(holdoutChanges(never, { name: 'Q4', description: '', size: '3' })).toEqual({ holdout_percentage: 3 });
  });

  it.each([active, ended])('no size for one that has been active', (holdout) => {
    expect(holdoutChanges(holdout, { name: 'Q4', description: '', size: '3' })).toEqual({});
  });

  it('a name and a description', () => {
    expect(holdoutChanges(active, { name: 'Q4b', description: 'Why', size: '5' })).toEqual({
      name: 'Q4b',
      description: 'Why',
    });
  });
});

describe('fixed copy', () => {
  const err = (status: number) => new ApiError({ status, detail: 'PLANTED' });
  it.each([
    [409, false, HOLDOUT_COPY.nameTaken],
    [409, true, HOLDOUT_COPY.conflict],
    [422, false, HOLDOUT_COPY.changed],
    [403, false, HOLDOUT_COPY.adminOnly],
    [404, false, HOLDOUT_COPY.notFound],
    [500, false, HOLDOUT_COPY.saveFailed],
    [0, false, UNREACHABLE],
  ])('%i (activates: %s)', (status, activates, copy) => {
    expect(holdoutErrorCopy(err(status), { activates })).toBe(copy);
  });

  it("the active holdout's sentence is the experiment page's", () => {
    expect(holdoutLabel({ name: 'Q4 baseline', holdout_percentage: 5 })).toBe(
      'Q4 baseline: keeps 5% of new users out of every experiment',
    );
  });

  it('numbers', () => {
    expect(ratePercent(0.1234)).toBe('12.3%');
    expect(ratePercent(null)).toBe('—');
    expect(points(0.02)).toBe('+2.0');
    expect(points(-0.011)).toBe('−1.1');
    expect(points(0)).toBe('0.0');
    expect(pValueText(0.0312)).toBe('0.031');
    expect(pValueText(0.0004)).toBe('< 0.001');
    expect(pValueText(null)).toBe('—');
  });
});
