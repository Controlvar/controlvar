/**
 * The formatting rules behind the results page's intervals (#1496, S2-b),
 * one function at a time. The rendered lines are pinned from server-computed
 * metrics in MetricComparisonTable.intervals.test.tsx; these are the edges
 * no server case reaches.
 */
import {
  finitePair,
  formatGroupedSignedPct,
  formatPctBound,
  intervalSpoken,
  intervalText,
} from '@/components/results/shared/resultFormat';

describe('finitePair', () => {
  it('accepts two finite numbers', () => {
    expect(finitePair([-0.5, 2])).toEqual([-0.5, 2]);
  });

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['a number', 3],
    ['one number', [1]],
    ['three numbers', [1, 2, 3]],
    ['NaN', [NaN, 1]],
    ['Infinity', [1, Infinity]],
    ['-Infinity', [-Infinity, 1]],
    ['a string', ['1', 2]],
    ['a null bound', [null, 2]],
  ])('refuses %s, so nothing is drawn', (_, value) => {
    expect(finitePair(value)).toBeNull();
  });
});

describe('formatPctBound', () => {
  it('writes grouping, one decimal, a sign and a real minus', () => {
    expect(formatPctBound(26001.878944641772, 'upper')).toBe('+26,001.9%');
    expect(formatPctBound(-34.18048952850553, 'lower')).toBe('−34.2%');
    expect(formatPctBound(-100, 'lower')).toBe('−100.0%');
  });

  it('abbreviates strictly beyond 1,000,000, and only on its own side', () => {
    expect(formatPctBound(1_000_000, 'upper')).toBe('+1,000,000.0%');
    expect(formatPctBound(999_999.9, 'upper')).toBe('+999,999.9%');
    expect(formatPctBound(1_000_000.04, 'upper')).toBe('above +1,000,000%');
    expect(formatPctBound(-1_000_000, 'lower')).toBe('−1,000,000.0%');
    expect(formatPctBound(-1_000_000.04, 'lower')).toBe('below −1,000,000%');
    // "above" is never a lower bound, "below" never an upper one.
    expect(formatPctBound(2_000_000, 'lower')).toBe('+2,000,000.0%');
    expect(formatPctBound(-2_000_000, 'upper')).toBe('−2,000,000.0%');
  });

  it('never writes an exponent, however large the number', () => {
    expect(formatGroupedSignedPct(1e20)).toBe('+100,000,000,000,000,000,000.0%');
    expect(formatPctBound(-1e300, 'upper')).not.toMatch(/e/);
  });

  it('writes a bound that rounds to 0 as "0.0%", with no sign', () => {
    expect(formatGroupedSignedPct(0.02158325433871955)).toBe('0.0%');
    expect(formatGroupedSignedPct(-0.04)).toBe('0.0%');
  });
});

describe('intervalText and intervalSpoken', () => {
  const parts = { level: '95%', unadjusted: false, lower: '+2.1%', upper: '+22.5%' };

  it('write "95% CI …", and "95% confidence interval …" for a screen reader', () => {
    expect(intervalText(parts)).toBe('95% CI +2.1% to +22.5%');
    expect(intervalSpoken(parts)).toBe('95% confidence interval +2.1% to +22.5%');
  });

  it('carry "unadjusted" as words', () => {
    expect(intervalText({ ...parts, unadjusted: true })).toBe('95% CI, unadjusted, +2.1% to +22.5%');
    expect(intervalSpoken({ ...parts, unadjusted: true })).toBe(
      '95% confidence interval, unadjusted, +2.1% to +22.5%',
    );
  });
});
