import { parseApiTimestamp } from '@/utils/apiTimestamp';

// 2026-10-11 05:02 UTC, as epoch milliseconds.
const INSTANT = Date.UTC(2026, 9, 11, 5, 2, 0);

describe('parseApiTimestamp', () => {
  it('runs in a zone that is not UTC, or none of this proves anything', () => {
    // jest.global-setup.js pins America/Los_Angeles. In UTC a time read as
    // local and the same time read as UTC are equal and every case below
    // would pass without the fix.
    expect(new Date(0).getTimezoneOffset()).not.toBe(0);
    expect(new Date(INSTANT).toLocaleString('en-US')).toBe('10/10/2026, 10:02:00 PM');
  });

  describe('a date-time with no zone is UTC', () => {
    it.each([
      ['seconds', '2026-10-11T05:02:00'],
      ['minutes only', '2026-10-11T05:02'],
      ['milliseconds', '2026-10-11T05:02:00.000'],
      ['microseconds, as the API writes them', '2026-10-11T05:02:00.000000'],
      ['a space for the T', '2026-10-11 05:02:00'],
    ])('%s', (_name, value) => {
      expect(parseApiTimestamp(value).getTime()).toBe(INSTANT);
    });

    it('keeps the fraction', () => {
      expect(parseApiTimestamp('2026-10-11T05:02:00.391716').getTime()).toBe(INSTANT + 391);
    });

    it('is not the local reading, which `new Date` gives', () => {
      const value = '2026-10-11T05:02:00';
      expect(new Date(value).getTime()).not.toBe(INSTANT);
      expect(parseApiTimestamp(value).getTime()).toBe(INSTANT);
    });

    it('is right on both sides of a daylight-saving change', () => {
      // Los Angeles is UTC-7 on 10 October 2026 and UTC-8 on 10 November.
      expect(parseApiTimestamp('2026-11-10T05:02:00').toLocaleString('en-US')).toBe(
        '11/9/2026, 9:02:00 PM',
      );
      expect(parseApiTimestamp('2026-10-10T05:02:00').toLocaleString('en-US')).toBe(
        '10/9/2026, 10:02:00 PM',
      );
    });
  });

  describe('a date-time that names its zone is unchanged', () => {
    it.each([
      ['Z', '2026-10-11T05:02:00Z'],
      ['z', '2026-10-11T05:02:00z'],
      ['+00:00, as the API writes it', '2026-10-11T05:02:00+00:00'],
      ['+hh:mm', '2026-10-11T07:32:00+02:30'],
      ['-hh:mm', '2026-10-10T22:02:00-07:00'],
      ['+hhmm', '2026-10-11T07:32:00+0230'],
      ['-hhmm', '2026-10-10T22:02:00-0700'],
      ['microseconds with +00:00', '2026-10-11T05:02:00.000000+00:00'],
    ])('%s', (_name, value) => {
      expect(parseApiTimestamp(value).getTime()).toBe(INSTANT);
      expect(parseApiTimestamp(value).getTime()).toBe(new Date(value).getTime());
    });
  });

  it('leaves a date alone (`...Z` would make it invalid)', () => {
    expect(parseApiTimestamp('2026-10-11').getTime()).toBe(new Date('2026-10-11').getTime());
    expect(parseApiTimestamp('2026-10-11').getTime()).toBe(Date.UTC(2026, 9, 11));
  });

  it('passes an epoch number and a Date through', () => {
    expect(parseApiTimestamp(INSTANT).getTime()).toBe(INSTANT);
    expect(parseApiTimestamp(new Date(INSTANT)).getTime()).toBe(INSTANT);
  });

  it.each(['', 'not a time', '2026-13-45T99:99:99', '2026-10-11T25:00:00', 'T05:02:00', '5:02'])(
    'gives an Invalid Date for %p, as new Date does',
    (value) => {
      expect(Number.isNaN(parseApiTimestamp(value).getTime())).toBe(true);
      expect(Number.isNaN(new Date(value).getTime())).toBe(true);
    },
  );

  it('treats anything else exactly as new Date does', () => {
    // A zone before the time, a trailing word and a long string are not "no
    // zone" and are not rewritten.
    for (const value of [
      'Sun, 11 Oct 2026 05:02:00 GMT',
      'Oct 11, 2026 05:02:00',
      '2026-10-11T05:02:00 UTC',
      '2026-10-11T07:02:00+02',
      `2026-10-11T05:02:00${'0'.repeat(10000)}`,
    ]) {
      expect(parseApiTimestamp(value).getTime()).toEqual(new Date(value).getTime());
    }
  });
});
