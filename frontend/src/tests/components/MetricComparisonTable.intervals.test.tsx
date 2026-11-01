/**
 * The change cell's interval and difference lines, the Impact column, the
 * reconciliation note and the footnotes (#1496, S2-b), rendered from metrics
 * the server computed: `fixtures/results-intervals.json` is written by
 * `python -m backend.scripts.generate_results_interval_fixtures` from the
 * results path's own builders, and a backend test fails when it drifts from
 * them. Only the old-server and noun cases below edit a fixture, and they say
 * so.
 *
 * The checks are the S2-b words' F1-F10, F13 and C16 (plan S, v1.2-ux-s2b-words):
 *  - F1  a wide proportion interval: grouping, one decimal, no exponent;
 *  - F2  a bound beyond ±1,000,000% abbreviated as a true statement;
 *  - F3  a mean with no relative interval says why, and only a mean does;
 *  - F4  a proportion control with no conversions: "Not a percentage";
 *  - F5  a lower bound of -100% is written out, never floored or capped;
 *  - F6  an arm with no users: "Not enough data to estimate", nothing else;
 *  - F7  the control row;
 *  - F8  "unadjusted" on every interval exactly when the p-value is adjusted;
 *  - F9  a response without the fields renders as before them;
 *  - F10 the impact's grouping, nouns and zero;
 *  - F13 the footnotes;
 *  - C16 the note when the interval and the significance disagree.
 */
import React from 'react';
import { render, screen, within } from '@testing-library/react';
import axe from 'axe-core';
import { MetricComparisonTable } from '@/components/results/MetricComparison/MetricComparisonTable';
import { CorrectionMethod, MetricResult, VariantResult } from '@/types/results';
import fixtures from '@/tests/fixtures/results-intervals.json';
import { spokenText, visibleText } from '@/tests/helpers/visibleText';

interface Case {
  metric: MetricResult;
  confidence_level: number;
  correction_method: CorrectionMethod;
}

/** A fresh copy of a server-computed case. */
function serverCase(id: keyof typeof fixtures.cases): Case {
  return JSON.parse(JSON.stringify(fixtures.cases[id])) as Case;
}

function renderCase(c: Case, metrics: MetricResult[] = [c.metric]) {
  return render(
    <MetricComparisonTable
      metrics={metrics}
      confidenceLevel={c.confidence_level}
      correctionMethod={c.correction_method}
    />,
  );
}

function rowOf(variantName: string): HTMLElement {
  const row = screen
    .getAllByRole('row')
    .find((r) => within(r).queryAllByRole('cell')[1]?.textContent?.startsWith(variantName));
  if (!row) throw new Error(`no row for ${variantName}`);
  return row;
}

/** Named cells of a row, read by the column headers. */
function cell(variantName: string, header: string): HTMLElement {
  const headers = screen.getAllByRole('columnheader').map((h) => visibleText(h).replace(/[↕↑↓]/g, '').trim());
  const index = headers.indexOf(header);
  if (index < 0) throw new Error(`no column ${header}: ${headers.join(', ')}`);
  return within(rowOf(variantName)).getAllByRole('cell')[index];
}

function testIdText(variantName: string, testId: string): string | null {
  const el = within(rowOf(variantName)).queryByTestId(testId);
  return el ? visibleText(el).trim() : null;
}

function pageText(): string {
  return visibleText(screen.getByTestId('metric-comparison-table'));
}

describe('F1: 30/1000 against 1/1000 at 0.99, from the server', () => {
  it('writes the relative interval with grouping and one decimal: +226.1% to +26,001.9%', () => {
    renderCase(serverCase('f1_wide_proportion'));
    expect(testIdText('treatment', 'change')).toBe('+2900.0%');
    expect(testIdText('treatment', 'change-interval')).toBe('99% CI +226.1% to +26,001.9%');
    expect(pageText()).not.toMatch(/e\+|Infinity|NaN|undefined|∞/);
  });

  it('gives the difference in percentage points and the impact in converting users', () => {
    renderCase(serverCase('f1_wide_proportion'));
    expect(testIdText('treatment', 'change-difference')).toBe(
      '+2.90 percentage points (99% CI +1.56 to +4.63)',
    );
    expect(visibleText(cell('treatment', 'Impact'))).toBe(
      '+29 converting users (99% CI +16 to +46)',
    );
  });

  it('reads "99% confidence interval" to a screen reader, and keeps each bound on one line', () => {
    renderCase(serverCase('f1_wide_proportion'));
    const line = within(rowOf('treatment')).getByTestId('change-interval');
    expect(spokenText(line).trim()).toBe('99% confidence interval +226.1% to +26,001.9%');
    const kept = line.querySelectorAll('.whitespace-nowrap');
    expect(Array.from(kept).map((s) => visibleText(s))).toEqual(['99% CI +226.1%', '+26,001.9%']);
    // The only text between the two unbreakable pieces is " to ".
    const between = Array.from(line.childNodes)
      .filter((n) => n.nodeType === Node.TEXT_NODE)
      .map((n) => n.textContent)
      .join('');
    expect(between.trim()).toBe('to');
  });
});

describe('F2: a bound beyond ±1,000,000%', () => {
  it('writes an upper bound above +1,000,000% as "above +1,000,000%" (server: 500/1000 against 1/100000)', () => {
    renderCase(serverCase('f2_above_a_million'));
    expect(testIdText('treatment', 'change-interval')).toBe(
      '99% CI +584,771.1% to above +1,000,000%',
    );
  });

  it('writes a lower bound below -1,000,000% as "below −1,000,000%" (server: a custom mean)', () => {
    renderCase(serverCase('f2_below_a_million'));
    expect(testIdText('treatment', 'change-interval')).toBe(
      '95% CI below −1,000,000% to −852,983.8%',
    );
  });

  it('abbreviates only past the boundary: 1,000,000.0 is written out, 1,000,000.04 is not', () => {
    const c = serverCase('f1_wide_proportion');
    const treatment = c.metric.variants.find((v) => !v.is_control) as VariantResult;
    const cases: Array<[[number, number], string]> = [
      [[226.1, 1_234_567.8], '99% CI +226.1% to above +1,000,000%'],
      [[226.1, 1_000_000.0], '99% CI +226.1% to +1,000,000.0%'],
      [[226.1, 999_999.9], '99% CI +226.1% to +999,999.9%'],
      [[226.1, 1_000_000.04], '99% CI +226.1% to above +1,000,000%'],
      [[-1_000_000.0, 12.5], '99% CI −1,000,000.0% to +12.5%'],
      [[-1_000_000.04, 12.5], '99% CI below −1,000,000% to +12.5%'],
    ];
    for (const [pair, expected] of cases) {
      treatment.relative_improvement_pct_ci = pair;
      const { unmount } = renderCase(c);
      expect(testIdText('treatment', 'change-interval')).toBe(expected);
      unmount();
    }
  });
});

describe('F3: a mean whose control is too uncertain around 0 for a relative interval', () => {
  const sentence =
    "No interval for the change: the control's average is too uncertain around 0 to bound the " +
    'relative change at the 95% level.';

  it('says why there is no interval, from the server: the change, then the sentence, then the difference', () => {
    renderCase(serverCase('f3_mean_no_relative_interval'));
    expect(testIdText('treatment', 'change')).toBe('+40.0%');
    expect(testIdText('treatment', 'change-no-interval')).toBe(sentence);
    expect(testIdText('treatment', 'change-interval')).toBeNull();
    expect(testIdText('treatment', 'change-difference')).toBe(
      '+1.00 per user (95% CI −6.07 to +8.07)',
    );
    const line = within(rowOf('treatment')).getByTestId('change-no-interval');
    expect(line.tagName).toBe('P');
    expect(line).not.toHaveAttribute('role');
    expect(line.textContent).not.toMatch(/rate|data|CI/);
  });

  it("states the response's level: 99%", () => {
    renderCase(serverCase('f3_mean_no_relative_interval_99'));
    expect(testIdText('treatment', 'change-no-interval')).toBe(
      "No interval for the change: the control's average is too uncertain around 0 to bound the " +
        'relative change at the 99% level.',
    );
  });

  it('never says it for a proportion, whatever its fields (F5 defect)', () => {
    for (const id of ['f4_control_no_conversions', 'f1_wide_proportion', 'f6_empty_treatment_proportion'] as const) {
      const c = serverCase(id);
      const treatment = c.metric.variants.find((v) => !v.is_control) as VariantResult;
      // The shape that reads as line (b) for a mean: a finite change, no interval.
      treatment.relative_improvement_pct = 12.5;
      treatment.relative_improvement_pct_ci = null;
      const { unmount } = renderCase(c);
      expect(screen.queryByTestId('change-no-interval')).toBeNull();
      expect(pageText()).not.toMatch(/No interval/);
      unmount();
    }
  });

  it('is not said when the field is absent (an older server) or the difference has no interval', () => {
    const absent = serverCase('f3_mean_no_relative_interval');
    const treatment = absent.metric.variants.find((v) => !v.is_control) as VariantResult;
    delete treatment.relative_improvement_pct_ci;
    const { unmount } = renderCase(absent);
    expect(screen.queryByTestId('change-no-interval')).toBeNull();
    unmount();

    const noDifferenceInterval = serverCase('f3_mean_no_relative_interval');
    const t2 = noDifferenceInterval.metric.variants.find((v) => !v.is_control) as VariantResult;
    t2.absolute_difference_ci = null;
    renderCase(noDifferenceInterval);
    expect(screen.queryByTestId('change-no-interval')).toBeNull();
  });
});

describe('F4: a proportion whose control had no conversions', () => {
  it('reads "Not a percentage: the control had no conversions", with the difference and the impact', () => {
    renderCase(serverCase('f4_control_no_conversions'));
    expect(testIdText('treatment', 'change')).toBe(
      'Not a percentage: the control had no conversions',
    );
    expect(testIdText('treatment', 'change-interval')).toBeNull();
    expect(testIdText('treatment', 'change-no-interval')).toBeNull();
    expect(testIdText('treatment', 'change-difference')).toBe(
      '+2.50 percentage points (95% CI +0.14 to +5.72)',
    );
    expect(visibleText(cell('treatment', 'Impact'))).toBe(
      '+5 converting users (95% CI 0 to +11)',
    );
  });
});

describe("F4 for a mean: (d) the control's mean is 0", () => {
  it("reads \"Not a percentage: the control's mean is 0\", with the difference and the impact", () => {
    renderCase(serverCase('d_mean_control_zero'));
    expect(testIdText('treatment', 'change')).toBe("Not a percentage: the control's mean is 0");
    expect(testIdText('treatment', 'change-interval')).toBeNull();
    expect(testIdText('treatment', 'change-no-interval')).toBeNull();
    expect(testIdText('treatment', 'change-difference')).toBe(
      '+0.10 per user (95% CI +0.04 to +0.16)',
    );
    expect(visibleText(cell('treatment', 'Impact'))).toBe('+10 events (95% CI +4 to +16)');
  });

  it('before the first value (every total 0, no interval) still reads "Not enough data to estimate"', () => {
    renderCase(serverCase('before_first_value'));
    expect(testIdText('treatment', 'change')).toBe('Not enough data to estimate');
    expect(testIdText('treatment', 'change-difference')).toBeNull();
    expect(visibleText(cell('treatment', 'Impact'))).toBe('Not enough data to estimate');
  });
});

describe('F5: a lower bound at -100%', () => {
  it('writes the server\'s -100% lower bound as "−100.0%" (0/50 against 10/50)', () => {
    renderCase(serverCase('f5_treatment_no_conversions'));
    expect(testIdText('treatment', 'change-interval')).toBe('95% CI −100.0% to −60.3%');
  });

  it('adds no floor or cap: a proportion bound is written as the server sent it', () => {
    renderCase(serverCase('f5_low_counts'));
    expect(testIdText('treatment', 'change-interval')).toBe('95% CI −34.2% to +1,206.3%');
  });

  it('writes a lower bound below −100% verbatim: the test of the server, not the page, catches it', () => {
    // Hand-built: the server never sends a proportion bound below -100%
    // (the next test checks its fixtures). Were it to, the page shows it.
    const c = serverCase('f5_treatment_no_conversions');
    const treatment = c.metric.variants.find((v) => !v.is_control) as VariantResult;
    treatment.relative_improvement_pct_ci = [-150.04, -60.3];
    renderCase(c);
    expect(testIdText('treatment', 'change-interval')).toBe('95% CI −150.0% to −60.3%');
  });

  it('every server-computed proportion lower bound on the page is at or above −100.0%', () => {
    let checked = 0;
    for (const id of Object.keys(fixtures.cases)) {
      const { metric } = serverCase(id as keyof typeof fixtures.cases);
      if (metric.analysis !== 'proportion') continue;
      for (const v of metric.variants) {
        const ci = v.relative_improvement_pct_ci;
        if (!ci) continue;
        checked += 1;
        expect([id, ci[0] >= -100]).toEqual([id, true]);
      }
    }
    expect(checked).toBeGreaterThan(5);
  });
});

describe('F6: an arm with no users', () => {
  it.each([
    ['a proportion', 'f6_empty_treatment_proportion'],
    ['a mean', 'f6_empty_treatment_mean'],
  ] as const)('%s: "Not enough data to estimate" for the change and the impact, nothing else', (_, id) => {
    renderCase(serverCase(id));
    expect(testIdText('treatment', 'change')).toBe('Not enough data to estimate');
    expect(testIdText('treatment', 'change-interval')).toBeNull();
    expect(testIdText('treatment', 'change-no-interval')).toBeNull();
    expect(testIdText('treatment', 'change-difference')).toBeNull();
    expect(visibleText(cell('treatment', 'Impact'))).toBe('Not enough data to estimate');
    expect(pageText()).not.toMatch(/NaN|Infinity|undefined|−100\.0%/);
  });
});

describe('F7: the control row', () => {
  it('shows "—" for the change and the impact, and no interval line', () => {
    renderCase(serverCase('f1_wide_proportion'));
    expect(visibleText(cell('control', 'Change'))).toBe('—');
    expect(visibleText(cell('control', 'Impact'))).toBe('—');
    expect(within(rowOf('control')).queryByTestId('change-interval')).toBeNull();
    expect(within(rowOf('control')).queryByTestId('reconciliation-note')).toBeNull();
  });
});

describe('F8: "unadjusted"', () => {
  it('is on every interval of a row whose p-value is shown adjusted (three variants, Benjamini-Hochberg)', () => {
    renderCase(serverCase('f8_three_variants_bh'));
    expect(testIdText('one_page', 'change-interval')).toBe('95% CI, unadjusted, +2.6% to +67.3%');
    expect(testIdText('one_page', 'change-difference')).toBe(
      '+3.10 percentage points (95% CI, unadjusted, +0.30 to +5.91)',
    );
    expect(visibleText(cell('one_page', 'Impact'))).toBe(
      '+31 converting users (95% CI, unadjusted, +3 to +59)',
    );
    expect(testIdText('two_step', 'change-interval')).toBe('95% CI, unadjusted, −19.8% to +34.8%');
    expect(testIdText('two_step', 'change-difference')).toBe(
      '+0.40 percentage points (95% CI, unadjusted, −2.26 to +3.07)',
    );
    expect(visibleText(cell('two_step', 'Impact'))).toBe(
      '+4 converting users (95% CI, unadjusted, −23 to +31)',
    );
    // Real text, read out: not a tooltip or a title.
    expect(spokenText(within(rowOf('one_page')).getByTestId('change-interval')).trim()).toBe(
      '95% confidence interval, unadjusted, +2.6% to +67.3%',
    );
    expect(screen.getByTestId('metric-comparison-table').querySelector('[title]')).toBeNull();
  });

  it('is on none when the p-value is not adjusted (one treatment, or no correction)', () => {
    const one = serverCase('c16_not_significant_excluding_zero');
    renderCase({ ...one, correction_method: 'benjamini_hochberg' });
    expect(pageText()).not.toMatch(/unadjusted,/);
  });
});

describe('F9: a response without the fields', () => {
  /** The fixture as a server older than #1496 sends it. */
  function oldServer(c: Case): Case {
    for (const v of c.metric.variants) {
      delete v.relative_improvement_pct_ci;
      delete v.incremental_impact;
      delete v.incremental_impact_ci;
      if (c.metric.analysis === 'proportion') {
        v.absolute_difference = null;
        v.absolute_difference_ci = null;
      }
    }
    return c;
  }

  it('renders as before them: no Impact column, no interval line, no footnote', () => {
    renderCase(oldServer(serverCase('f1_wide_proportion')));
    expect(screen.queryByRole('columnheader', { name: 'Impact' })).toBeNull();
    expect(visibleText(cell('treatment', 'Change'))).toBe('+2900.0% · better');
    expect(screen.queryByTestId('change-interval')).toBeNull();
    expect(screen.queryByTestId('change-difference')).toBeNull();
    expect(screen.queryByTestId('interval-footnote')).toBeNull();
    expect(screen.queryByTestId('impact-footnote')).toBeNull();
    expect(screen.queryByTestId('reconciliation-note')).toBeNull();
  });

  it('keeps an older mean row as it was: the difference per user, no "Not a percentage", no note', () => {
    renderCase(oldServer(serverCase('d_mean_control_zero')));
    expect(visibleText(cell('treatment', 'Change'))).toBe(
      'Not enough data to estimate +0.10 per user (95% CI +0.04 to +0.16)',
    );
    expect(screen.queryByTestId('interval-footnote')).toBeNull();
  });

  it('with `analysis` absent too, nothing reads "mean", "undefined" or "NaN"', () => {
    const c = oldServer(serverCase('f1_wide_proportion'));
    delete c.metric.analysis;
    renderCase(c);
    expect(pageText()).not.toMatch(/\bmean\b|undefined|NaN/);
  });
});

describe('F10: the impact', () => {
  it('groups whole numbers of events: +2,700 events (server: 10,000 users per arm)', () => {
    renderCase(serverCase('f10_count_events'));
    expect(visibleText(cell('treatment', 'Impact'))).toBe('+2,700 events (95% CI +2,506 to +2,894)');
  });

  it('writes values to 2 places with grouping and no currency: in revenue, in duration', () => {
    renderCase(serverCase('f10_revenue_values'));
    expect(visibleText(cell('treatment', 'Impact'))).toBe(
      '+7,600.00 in revenue (95% CI +4,463.73 to +10,736.27)',
    );
    expect(pageText()).not.toMatch(/[$€£¥]/);
  });

  it('writes a duration as "in duration" and a custom value as "in value"', () => {
    renderCase(serverCase('duration_values'));
    expect(visibleText(cell('treatment', 'Impact'))).toBe(
      '+2,100.00 in duration (95% CI +296.90 to +3,903.10)',
    );
  });

  it('writes a zero impact as "0", never "+0" or "−0" (server: 10/100 against 10/100)', () => {
    renderCase(serverCase('f10_zero_impact'));
    expect(visibleText(cell('treatment', 'Impact'))).toBe('0 converting users (95% CI −9 to +9)');
  });

  it('names the users of a non-conversion metric analysed as a share "users who sent the event"', () => {
    // An older server analysed a count as the share of users who sent its
    // event; only the metric's type is changed here.
    const c = serverCase('f1_wide_proportion');
    c.metric.metric_type = 'count';
    renderCase(c);
    expect(visibleText(cell('treatment', 'Impact'))).toBe(
      '+29 users who sent the event (99% CI +16 to +46)',
    );
  });

  it('heads its column "Impact", a plain header with scope "col" that does not sort', () => {
    renderCase(serverCase('f10_count_events'));
    const header = screen.getByRole('columnheader', { name: 'Impact' });
    expect(header).toHaveAttribute('scope', 'col');
    expect(within(header).queryByRole('button')).toBeNull();
    expect(header).not.toHaveAttribute('aria-sort');
  });
});

describe('C16: the interval and the significance disagree', () => {
  it('says so under the badge when significant but the interval reaches 0 (server: 3/50 against 18/100)', () => {
    renderCase(serverCase('c16_significant_reaching_zero'));
    expect(testIdText('treatment', 'reconciliation-note')).toBe(
      "Significant: Fisher's exact test gives p 0.0492, below 0.05, although the interval " +
        'reaches 0. The test decides; near the threshold the two can differ.',
    );
    expect(testIdText('treatment', 'change-difference')).toBe(
      '−12.00 percentage points (95% CI −21.52 to 0.00)',
    );
  });

  it('says so when not significant but the interval excludes 0 (server: 128/1000 against 100/1000)', () => {
    renderCase(serverCase('c16_not_significant_excluding_zero'));
    expect(testIdText('treatment', 'reconciliation-note')).toBe(
      "Not significant: Fisher's exact test gives p 0.0573, not below 0.05, although the " +
        'interval excludes 0. The test decides; near the threshold the two can differ.',
    );
  });

  it('names the correction when the adjusted p decides (server: Benjamini-Hochberg, k = 2)', () => {
    renderCase(serverCase('f8_three_variants_bh'));
    expect(testIdText('one_page', 'reconciliation-note')).toBe(
      'Not significant after the Benjamini-Hochberg correction for 2 comparisons (adjusted p ' +
        '0.0713), although the unadjusted interval excludes 0.',
    );
    expect(testIdText('two_step', 'reconciliation-note')).toBeNull();
  });

  it('never prints a p below the threshold as the threshold (server: 45/200 against 5/50, p 0.04981)', () => {
    renderCase(serverCase('c16_significant_p_near_alpha'));
    expect(testIdText('treatment', 'reconciliation-note')).toBe(
      "Significant: Fisher's exact test gives p 0.0498, below 0.05, although the interval " +
        'reaches 0. The test decides; near the threshold the two can differ.',
    );
    // The same p as the p-value column.
    expect(visibleText(cell('treatment', 'p-value'))).toBe('0.0498unadjusted');
  });

  it('adds digits at 0.99 when 4 would print the threshold (server: 15/200 against 2/150, p 0.0099972)', () => {
    renderCase(serverCase('c16_significant_p_near_alpha_99'));
    expect(testIdText('treatment', 'reconciliation-note')).toBe(
      "Significant: Fisher's exact test gives p 0.009997, below 0.01, although the interval " +
        'reaches 0. The test decides; near the threshold the two can differ.',
    );
  });

  it('writes a p at the threshold as not below it at 0.99 (server: 67/200 against 68/300, p 0.0100040)', () => {
    renderCase(serverCase('c16_not_significant_p_near_alpha_99'));
    expect(testIdText('treatment', 'reconciliation-note')).toBe(
      "Not significant: Fisher's exact test gives p 0.0100, not below 0.01, although the " +
        'interval excludes 0. The test decides; near the threshold the two can differ.',
    );
  });

  it('says nothing when they agree', () => {
    renderCase(serverCase('f1_wide_proportion'));
    expect(screen.queryByTestId('reconciliation-note')).toBeNull();
  });
});

describe('F13: the footnotes', () => {
  const unadjustedIntervals = (level: string) =>
    `Intervals are ${level} confidence intervals (CI) of the difference from the control. They ` +
    'are not adjusted for comparing several variants with the control; significance is, so the ' +
    'two can disagree, and the row says so when they do. A percentage interval can be very ' +
    'wide when counts are small.';
  const testDecides = (level: string) =>
    `Intervals are ${level} confidence intervals (CI) of the difference from the control. ` +
    'Significance comes from the test, not the interval, so near the threshold the two can ' +
    'disagree; the row says so when they do. A percentage interval can be very wide when ' +
    'counts are small.';

  it('says significance is adjusted when a row shows an adjusted p-value (server: two treatments, Benjamini-Hochberg)', () => {
    renderCase(serverCase('f8_three_variants_bh'));
    expect(screen.getByTestId('interval-footnote')).toHaveTextContent(unadjustedIntervals('95%'));
    expect(screen.getByTestId('interval-footnote').tagName).toBe('P');
  });

  it('says the test decides under no correction (server: two treatments, correction none)', () => {
    renderCase(serverCase('f13_two_treatments_no_correction'));
    expect(screen.getByTestId('interval-footnote')).toHaveTextContent(testDecides('95%'));
    expect(screen.getByTestId('interval-footnote').textContent).not.toMatch(/significance is/);
  });

  it('says the test decides with one treatment (server: one treatment, Benjamini-Hochberg)', () => {
    renderCase(serverCase('f13_one_treatment_bh'));
    expect(screen.getByTestId('interval-footnote')).toHaveTextContent(testDecides('95%'));
  });

  it("explains the intervals, spelled out, at the response's level, whenever one is shown", () => {
    renderCase(serverCase('f1_wide_proportion'));
    expect(screen.getByTestId('interval-footnote')).toHaveTextContent(testDecides('99%'));
    expect(screen.getByTestId('interval-footnote').textContent).toContain('very wide');
    expect(screen.getByTestId('interval-footnote').textContent).toContain('confidence intervals (CI)');
  });

  it('explains the impact', () => {
    renderCase(serverCase('f10_count_events'));
    expect(screen.getByTestId('impact-footnote')).toHaveTextContent(
      "Impact: the difference per user times the variant's users (its Sample Size): how many " +
        "more converting users, events or value the variant's users had than as many users at " +
        "the control's rate or mean. Users and events are rounded to whole numbers. Values are " +
        'added as sent; no currency or unit is read.',
    );
  });

  it('shows no interval footnote when no interval is shown', () => {
    renderCase(serverCase('f6_empty_treatment_proportion'));
    expect(screen.queryByTestId('interval-footnote')).toBeNull();
    // The Impact column is there (the server reports the field), so its footnote is.
    expect(screen.getByTestId('impact-footnote')).toBeInTheDocument();
  });
});

describe('every server case', () => {
  it.each(Object.keys(fixtures.cases))('%s: no "NaN", "Infinity", "∞", "undefined" or exponent', (id) => {
    renderCase(serverCase(id as keyof typeof fixtures.cases));
    expect(pageText()).not.toMatch(/NaN|Infinity|∞|undefined|\de[+-]?\d/);
  });

  it('has no axe violations with every line, note and footnote on the page', async () => {
    const metrics = (
      [
        'f1_wide_proportion',
        'f3_mean_no_relative_interval',
        'f4_control_no_conversions',
        'f8_three_variants_bh',
        'c16_significant_reaching_zero',
        'f6_empty_treatment_proportion',
      ] as const
    ).map((id, i) => ({ ...serverCase(id).metric, metric_name: `Metric ${i}` }));
    const { container } = render(
      <MetricComparisonTable
        metrics={metrics}
        confidenceLevel={0.95}
        correctionMethod="benjamini_hochberg"
      />,
    );
    expect(screen.getAllByTestId('reconciliation-note').length).toBeGreaterThan(0);
    expect(screen.getByTestId('change-no-interval')).toBeInTheDocument();
    const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations).toEqual([]);
  });
});
