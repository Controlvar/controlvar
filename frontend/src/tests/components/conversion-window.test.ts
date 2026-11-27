/**
 * A metric's conversion window (beta, #1934): how the dashboard reads the box,
 * and every sentence it shows for a window, as pure functions. The pages that
 * show them are pinned in experiment-new-window.test.tsx,
 * experiment-detail-window.test.tsx and results-conversion-window.test.tsx.
 */
import {
  parseWindowHours,
  WINDOW_HELP_BLANK,
  WINDOW_LABEL,
  windowEstimateLine,
  windowHelp,
  windowPageLine,
  windowRefusal,
  windowReviewText,
  windowSpan,
  windowSummaryLine,
  windowTag,
  windowText,
} from '@/utils/conversionWindow';
import { checkMetrics, metricWindowProblem, MetricFormData } from '@/components/experiments/new/formState';

describe('windowText', () => {
  it.each<[number, string]>([
    [1, '1 hour'],
    [24, '1 day'],
    [36, '36 hours'],
    [168, '7 days'],
    [8760, '365 days'],
  ])('%i hours reads "%s": a multiple of 24 in days, otherwise hours', (hours, text) => {
    expect(windowText(hours)).toBe(text);
  });

  it('a window in hours is not repeated beside itself', () => {
    expect(windowSpan(168)).toBe('7 days (168 hours)');
    expect(windowSpan(24)).toBe('1 day (24 hours)');
    expect(windowSpan(36)).toBe('36 hours');
    expect(windowSpan(8759)).toBe('8,759 hours');
  });
});

describe('parseWindowHours reads the box the way the API takes it', () => {
  it.each(['', '   ', undefined])('blank (%p) is no window', (text) => {
    expect(parseWindowHours(text)).toEqual({ ok: true, hours: null });
  });

  it.each<[string, number]>([
    ['1', 1],
    ['168', 168],
    [' 168 ', 168],
    ['8760', 8760],
  ])('"%s" is %i hours', (text, hours) => {
    expect(parseWindowHours(text)).toEqual({ ok: true, hours });
  });

  it.each(['0', '8761', '1.5', '7d', '1e2', '-1', '+5', '1 000', '0x10', 'Infinity'])(
    '"%s" is refused, as the API refuses it',
    (text) => {
      expect(parseWindowHours(text)).toEqual({ ok: false });
    },
  );
});

describe('the words, exactly', () => {
  it('the form', () => {
    expect(WINDOW_LABEL).toBe('Conversion window in hours (beta, optional)');
    expect(WINDOW_HELP_BLANK).toBe('Blank: an event counts however long after the assignment it comes.');
    expect(windowHelp(168)).toBe(
      "Counts events in the 7 days (168 hours) from a user's assignment. Fixed once the experiment leaves draft.",
    );
    expect(windowRefusal('Purchase')).toBe(
      '“Purchase”: the conversion window must be a whole number of hours from 1 to 8,760, or blank.',
    );
    expect(windowReviewText(168)).toBe(', conversion window 7 days');
    expect(windowPageLine(168)).toBe('Conversion window: 7 days');
    expect(windowEstimateLine(168)).toBe(
      'The estimate counts users, not their windows: users who join in the last 7 days have less than 7 days when the experiment ends.',
    );
  });

  it('the results', () => {
    expect(windowSummaryLine(168)).toBe(
      "Conversion window: events in the 7 days (168 hours) from each user's assignment count.",
    );
    expect(windowTag(168)).toBe('within 7 days');
  });
});

describe('the metric checks', () => {
  const M = (overrides: Partial<MetricFormData> = {}): MetricFormData => ({
    name: 'Purchase',
    event_name: 'purchase',
    metric_type: 'conversion',
    is_primary: true,
    ...overrides,
  });

  it('a window that cannot be saved is refused last, naming its metric', () => {
    const metrics = [M(), M({ name: 'Refund', event_name: 'refund', is_primary: false, conversion_window_text: '7d' })];
    expect(checkMetrics(metrics)).toBe(windowRefusal('Refund'));
    expect(metricWindowProblem(metrics)).toEqual({ index: 1, message: windowRefusal('Refund') });
    // Every earlier check still comes first, in its own words.
    expect(checkMetrics([M({ conversion_window_text: '0', is_primary: false })])).toBe('Choose a primary metric.');
  });

  it('blank and a whole number of hours pass', () => {
    expect(checkMetrics([M({ conversion_window_text: '' })])).toBeNull();
    expect(checkMetrics([M({ conversion_window_text: '168' })])).toBeNull();
    expect(metricWindowProblem([M()])).toBeNull();
  });
});
