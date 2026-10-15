import React from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { SampleSizeMeter } from '@/components/results/ResultsDashboard/SampleSizeMeter';
import { SampleSizeResult } from '@/types/results';

jest.mock('next/link', () => {
  const MockLink = ({ children, href, ...rest }: { children: React.ReactNode; href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  );
  MockLink.displayName = 'MockLink';
  return MockLink;
});

/** A plan from an observed control rate, with values no default would give. */
const observed: SampleSizeResult = {
  required_sample_size_per_variant: 5000,
  current_sample_size_per_variant: 2000,
  is_adequate: false,
  achieved_power: 0.45,
  days_to_significance: null,
  projected_completion_date: null,
  baseline_rate: 0.0234,
  mde: 0.137,
  confidence_level: 0.9,
  power_target: 0.95,
  baseline_source: 'observed',
  baseline_users: 2140,
  metric_id: 'm-1',
  metric_name: 'Checkout',
  metric_type: 'conversion',
  analysed_as: 'conversion',
  alpha: 0.1,
  comparisons: 1,
  correction_method: 'none',
  mde_absolute: 0.0032058,
  unavailable_reason: null,
  guide_only_reasons: [],
};

const reached: SampleSizeResult = {
  ...observed,
  required_sample_size_per_variant: 3842,
  current_sample_size_per_variant: 6200,
  is_adequate: true,
  achieved_power: 0.99,
};

/** A new experiment: no users, nothing to plan from. */
const noTraffic: SampleSizeResult = {
  ...observed,
  required_sample_size_per_variant: null,
  current_sample_size_per_variant: 0,
  achieved_power: null,
  baseline_rate: null,
  baseline_source: null,
  baseline_users: null,
  mde: 0.05,
  confidence_level: 0.95,
  power_target: 0.8,
  alpha: 0.05,
  mde_absolute: null,
  unavailable_reason: 'no_control_data',
};

describe('SampleSizeMeter', () => {
  describe('the inputs it was calculated from (4c)', () => {
    it('shows each input with where it came from', () => {
      render(<SampleSizeMeter data={observed} onRecalculate={jest.fn()} />);
      expect(screen.getByText('Calculated from')).toBeInTheDocument();
      expect(screen.getByLabelText('Baseline conversion rate (%)')).toHaveValue('2.34');
      expect(screen.getByTestId('sample-size-baseline-source')).toHaveTextContent(
        'Observed in control so far: 2.34% (2,140 users).'
      );
      // The results are cached for up to five minutes; this tab is not.
      expect(screen.getByTestId('sample-size-baseline-source')).toHaveTextContent(
        'It can be up to five minutes newer than the rate on the Overview.'
      );
      expect(screen.getByLabelText('Minimum detectable effect, relative (%)')).toHaveValue('13.7');
      expect(screen.getByTestId('sample-size-mde-source')).toHaveTextContent(
        'Default — no effect was saved with this experiment. Change it to the smallest lift worth shipping.'
      );
      expect(screen.getByLabelText('Power')).toHaveDisplayValue('95%');
      expect(screen.getByLabelText('Significance (two-sided)')).toHaveDisplayValue('10%');
      expect(screen.getByTestId('sample-size-metric')).toHaveTextContent(
        'Planned for Checkout, analysed as a conversion rate (a two-proportion test). Variants: 2 (from this experiment).'
      );
      expect(screen.getByText('These inputs are not saved with the experiment. Change them to see a different plan.')).toBeInTheDocument();
    });

    // #1257: the effect saved with the experiment is named as such; an
    // answer with no mde_source (an API older than 0.34.3) is the default.
    it.each([
      ['experiment', 'Set when this experiment was created. 5% means 12% → 12.6%, not 17%.'],
      [
        'default',
        'Default — no effect was saved with this experiment. Change it to the smallest lift worth shipping. 5% means 12% → 12.6%, not 17%.',
      ],
      [
        undefined,
        'Default — no effect was saved with this experiment. Change it to the smallest lift worth shipping. 5% means 12% → 12.6%, not 17%.',
      ],
    ] as const)('says where the effect came from (mde_source %p)', (source, text) => {
      const data: SampleSizeResult = { ...observed, mde: 0.1, mde_source: source };
      if (source === undefined) delete data.mde_source;
      render(<SampleSizeMeter data={data} onRecalculate={jest.fn()} />);
      expect(screen.getByLabelText('Minimum detectable effect, relative (%)')).toHaveValue('10');
      expect(screen.getByTestId('sample-size-mde-source').textContent).toBe(text);
    });

    it('says which inputs the user entered', () => {
      render(
        <SampleSizeMeter
          data={{ ...observed, baseline_source: 'request', baseline_users: null }}
          overrides={{ baseline_conversion_rate: 0.0234, power_target: 0.95 }}
          onRecalculate={jest.fn()}
        />
      );
      expect(screen.getByTestId('sample-size-baseline-source')).toHaveTextContent('Entered by you.');
      expect(screen.getByTestId('sample-size-power-source')).toHaveTextContent('Entered by you.');
      // The confidence level is the experiment's stored one unless changed here (#580).
      expect(screen.getByTestId('sample-size-significance-source')).toHaveTextContent(
        "From the experiment's settings."
      );
      expect(screen.getByRole('button', { name: 'Use the observed rate' })).toBeInTheDocument();
    });

    it('says the correction is the experiment\'s, until the user changes it', () => {
      const { rerender } = render(
        <SampleSizeMeter data={{ ...observed, comparisons: 2, correction_method: 'benjamini_hochberg' }} />
      );
      expect(screen.getByTestId('sample-size-correction-source')).toHaveTextContent(
        "From the experiment's settings."
      );
      expect(screen.getByLabelText('Correction')).toHaveAccessibleDescription(
        "From the experiment's settings."
      );
      rerender(
        <SampleSizeMeter
          data={{ ...observed, comparisons: 2, correction_method: 'bonferroni' }}
          overrides={{ correction_method: 'bonferroni' }}
        />
      );
      expect(screen.getByTestId('sample-size-correction-source')).toHaveTextContent('Entered by you.');
    });

    it('labels the no-correction option "None", with no claim about the results', () => {
      render(<SampleSizeMeter data={{ ...observed, comparisons: 2 }} />);
      const options = Array.from(
        (screen.getByLabelText('Correction') as HTMLSelectElement).options
      ).map((o) => o.textContent);
      expect(options).toEqual(['None', 'Bonferroni', 'Benjamini-Hochberg']);
    });

    it('renders a stored 92% confidence level truthfully: 8% significance (PE condition 14)', () => {
      render(
        <SampleSizeMeter
          data={{ ...observed, confidence_level: 0.92 }}
          onRecalculate={jest.fn()}
        />
      );
      const select = screen.getByLabelText('Significance (two-sided)') as HTMLSelectElement;
      expect(select.selectedOptions[0].textContent).toBe("8% (this experiment's setting)");
      expect(screen.getByTestId('sample-size-required')).toHaveTextContent('8% significance, two-sided.');
      expect(screen.getByTestId('sample-size-significance-source')).toHaveTextContent(
        "From the experiment's settings."
      );
      expect(screen.queryByTestId('sample-size-stale')).not.toBeInTheDocument();
    });
  });

  describe('the result', () => {
    it('shows the plan and the smallest variant against it', () => {
      render(<SampleSizeMeter data={observed} />);
      expect(screen.getByTestId('sample-size-required')).toHaveTextContent(
        '5,000 users per variant (10,000 in total across 2 variants) at 95% power and 10% significance, two-sided.'
      );
      expect(screen.getByTestId('sample-size-label')).toHaveTextContent(
        'Smallest variant: 2,000 of 5,000 (40%)'
      );
      expect(screen.getByTestId('sample-size-status')).toHaveTextContent('40% of planned sample');
      expect(screen.getByTestId('power-label')).toHaveTextContent(
        'Power to detect a 13.7% lift at the current sample: 45%'
      );
      const bar = screen.getByRole('progressbar');
      expect(bar).toHaveAttribute('aria-valuenow', '40');
      expect(bar).toHaveAttribute('aria-label', 'Planned sample: 40%');
    });

    it('says "Planned sample reached" and caps the bar at 100%', () => {
      render(<SampleSizeMeter data={reached} />);
      expect(screen.getByTestId('sample-size-status')).toHaveTextContent('Planned sample reached');
      expect(screen.getByTestId('sample-size-bar')).toHaveStyle({ width: '100%' });
      expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100');
    });

    it('never uses the old Adequate / Insufficient or "days to significance" wording', () => {
      for (const data of [observed, reached, noTraffic]) {
        const { unmount } = render(<SampleSizeMeter data={data} />);
        const meter = screen.getByTestId('sample-size-meter');
        expect(meter).not.toHaveTextContent(/Adequate|Insufficient|significance:/);
        expect(meter).not.toHaveTextContent(/days to/i);
        unmount();
      }
    });
  });

  describe('no traffic yet', () => {
    it('renders the meter with a prompt, and does not crash on null', () => {
      render(<SampleSizeMeter data={noTraffic} onRecalculate={jest.fn()} />);
      expect(screen.getByTestId('sample-size-meter')).toBeInTheDocument();
      expect(screen.getByTestId('sample-size-no-traffic')).toHaveTextContent(
        'No users have been assigned yet. The plan uses your inputs; progress appears once traffic arrives.'
      );
      expect(screen.getByTestId('sample-size-unavailable')).toHaveTextContent(
        'Not enough control data yet — enter the rate you expect.'
      );
      expect(screen.getByLabelText('Baseline conversion rate (%)')).toHaveValue('');
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
      expect(screen.queryByTestId('power-label')).not.toBeInTheDocument();
    });

    it.each([
      ['no_control_conversions', 'No control user has converted yet'],
      ['rate_at_boundary', 'Every control user has converted so far'],
      ['effect_out_of_range', 'raised by this effect reaches 100% or more'],
      ['effect_too_small', 'raised by this effect changes too little to estimate a sample size'],
      ['no_metric', 'This experiment has no metric'],
    ] as const)('explains %s', (reason, text) => {
      render(
        <SampleSizeMeter
          data={{ ...noTraffic, current_sample_size_per_variant: 10, unavailable_reason: reason }}
        />
      );
      expect(screen.getByTestId('sample-size-unavailable')).toHaveTextContent(text);
    });
  });

  describe('notes', () => {
    // The Power Calculator plans a conversion rate too, so the note must not
    // send a non-conversion metric there as if it could plan one.
    it('labels a non-conversion metric and says the Power Calculator plans conversion rates only', () => {
      render(<SampleSizeMeter data={{ ...observed, metric_name: 'Revenue', metric_type: 'revenue' }} />);
      const note = screen.getByTestId('sample-size-metric-type-note');
      expect(note).toHaveTextContent(
        'Revenue is a revenue metric. Every metric is analysed as a conversion today, so this plan is for a conversion rate. ' +
          'The Power Calculator plans conversion rates only, so it cannot plan a revenue metric either.'
      );
      expect(note).not.toHaveTextContent(/use the Power Calculator/i);
      expect(within(note).queryByRole('link')).not.toBeInTheDocument();
    });

    it('shows no metric-type note for a conversion metric', () => {
      render(<SampleSizeMeter data={observed} />);
      expect(screen.queryByTestId('sample-size-metric-type-note')).not.toBeInTheDocument();
    });

    it.each([
      ['adaptive_allocation', 'it adapts how traffic is split between variants'],
      ['unequal_allocation', 'its variants are not split evenly'],
      ['sequential_testing', 'the Sequential tab shows when you can stop'],
      ['bayesian', 'it is analysed with Bayesian statistics'],
    ] as const)('says a fixed sample size is a guide only for %s', (reason, text) => {
      render(<SampleSizeMeter data={{ ...observed, guide_only_reasons: [reason] }} />);
      const note = screen.getByTestId('sample-size-guide-only');
      expect(note).toHaveTextContent('A fixed sample size is a guide only for this experiment:');
      expect(note).toHaveTextContent(text);
    });

    it('has no guide-only note for an even fixed split', () => {
      render(<SampleSizeMeter data={observed} />);
      expect(screen.queryByTestId('sample-size-guide-only')).not.toBeInTheDocument();
    });

    it('says a 3-variant plan makes no correction, as the experiment is set', () => {
      render(
        <SampleSizeMeter data={{ ...observed, comparisons: 2, confidence_level: 0.95, alpha: 0.05 }} />
      );
      expect(screen.getByTestId('sample-size-correction-note')).toHaveTextContent(
        'With 3 variants, this plan makes no correction for comparing several variants with the control: the experiment is set to no correction. Choose a correction to plan each comparison at 2.5% significance.'
      );
    });

    it("says a plan uses the experiment's own correction", () => {
      render(
        <SampleSizeMeter
          data={{
            ...observed,
            comparisons: 2,
            confidence_level: 0.95,
            alpha: 0.025,
            correction_method: 'benjamini_hochberg',
          }}
        />
      );
      expect(screen.getByTestId('sample-size-correction-note')).toHaveTextContent(
        "With 3 variants, this plan uses the experiment's correction (Benjamini-Hochberg): each of the 2 comparisons with the control is planned at 2.5% significance."
      );
    });

    it('says what a correction chosen on the tab did', () => {
      render(
        <SampleSizeMeter
          data={{
            ...observed,
            comparisons: 3,
            confidence_level: 0.95,
            alpha: 0.05 / 3,
            correction_method: 'bonferroni',
          }}
          overrides={{ correction_method: 'bonferroni' }}
        />
      );
      expect(screen.getByTestId('sample-size-correction-note')).toHaveTextContent(
        'With 4 variants, each of the 3 comparisons with the control is planned at 1.667% significance (Bonferroni).'
      );
    });

    it('has no correction sentence for two variants', () => {
      render(<SampleSizeMeter data={observed} />);
      expect(screen.queryByTestId('sample-size-correction-note')).not.toBeInTheDocument();
    });
  });

  describe('Recalculate', () => {
    it('sends only what the user changed', async () => {
      const onRecalculate = jest.fn();
      render(<SampleSizeMeter data={observed} onRecalculate={onRecalculate} />);
      const mde = screen.getByLabelText('Minimum detectable effect, relative (%)');
      await userEvent.clear(mde);
      await userEvent.type(mde, '10');
      expect(screen.getByTestId('sample-size-stale')).toHaveTextContent(
        'Inputs changed. Press Recalculate to update the plan.'
      );
      await userEvent.click(screen.getByRole('button', { name: 'Recalculate' }));
      expect(onRecalculate).toHaveBeenCalledWith({ mde: 0.1 });
    });

    it('sends nothing when nothing changed', async () => {
      const onRecalculate = jest.fn();
      render(<SampleSizeMeter data={observed} onRecalculate={onRecalculate} />);
      expect(screen.queryByTestId('sample-size-stale')).not.toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: 'Recalculate' }));
      expect(onRecalculate).toHaveBeenCalledWith({});
    });

    it('sends a typed baseline, power, significance and correction', async () => {
      const onRecalculate = jest.fn();
      render(
        <SampleSizeMeter
          data={{ ...noTraffic, comparisons: 2 }}
          onRecalculate={onRecalculate}
        />
      );
      await userEvent.type(screen.getByLabelText('Baseline conversion rate (%)'), '12');
      await userEvent.selectOptions(screen.getByLabelText('Power'), '90%');
      await userEvent.selectOptions(screen.getByLabelText('Significance (two-sided)'), '1%');
      await userEvent.selectOptions(screen.getByLabelText('Correction'), 'Bonferroni');
      await userEvent.click(screen.getByRole('button', { name: 'Recalculate' }));
      expect(onRecalculate).toHaveBeenCalledWith({
        baseline_conversion_rate: 0.12,
        power_target: 0.9,
        confidence_level: 0.99,
        correction_method: 'bonferroni',
      });
    });

    it('keeps earlier overrides, and "Use the observed rate" drops the baseline', async () => {
      const onRecalculate = jest.fn();
      render(
        <SampleSizeMeter
          data={{ ...observed, baseline_source: 'request', mde: 0.1 }}
          overrides={{ baseline_conversion_rate: 0.0234, mde: 0.1 }}
          onRecalculate={onRecalculate}
        />
      );
      await userEvent.click(screen.getByRole('button', { name: 'Recalculate' }));
      expect(onRecalculate).toHaveBeenLastCalledWith({ baseline_conversion_rate: 0.0234, mde: 0.1 });
      await userEvent.click(screen.getByRole('button', { name: 'Use the observed rate' }));
      expect(onRecalculate).toHaveBeenLastCalledWith({ mde: 0.1 });
    });

    it.each([
      ['Baseline conversion rate (%)', '0', 'Enter a baseline conversion rate between 0.01% and 99.99%.'],
      ['Minimum detectable effect, relative (%)', '0.01', 'Enter a minimum detectable effect of at least 0.1%.'],
      ['Minimum detectable effect, relative (%)', '100', 'Enter a minimum detectable effect below 100%.'],
    ])('refuses %s = %s in the browser, without sending it', async (label, value, message) => {
      const onRecalculate = jest.fn();
      render(<SampleSizeMeter data={observed} onRecalculate={onRecalculate} />);
      const input = screen.getByLabelText(label);
      await userEvent.clear(input);
      await userEvent.type(input, value);
      await userEvent.click(screen.getByRole('button', { name: 'Recalculate' }));
      expect(screen.getByRole('alert')).toHaveTextContent(message);
      expect(onRecalculate).not.toHaveBeenCalled();
    });

    it('refuses a baseline the effect would raise past 100%', async () => {
      const onRecalculate = jest.fn();
      render(<SampleSizeMeter data={observed} onRecalculate={onRecalculate} />);
      const baseline = screen.getByLabelText('Baseline conversion rate (%)');
      await userEvent.clear(baseline);
      await userEvent.type(baseline, '95');
      await userEvent.click(screen.getByRole('button', { name: 'Recalculate' }));
      expect(screen.getByRole('alert')).toHaveTextContent(
        'This baseline raised by this effect reaches 100% or more. Lower the baseline rate or the effect.'
      );
      expect(onRecalculate).not.toHaveBeenCalled();
    });

    it('is read-only without a handler', () => {
      render(<SampleSizeMeter data={observed} />);
      expect(screen.queryByRole('button', { name: 'Recalculate' })).not.toBeInTheDocument();
      expect(screen.getByLabelText('Baseline conversion rate (%)')).toHaveAttribute('readonly');
      expect(screen.getByLabelText('Power')).toBeDisabled();
    });
  });

  describe('accessibility (axe-core in jsdom; colour contrast is not computable here)', () => {
    const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

    it.each([
      ['in progress', observed],
      ['reached', reached],
      ['no traffic', noTraffic],
      ['three variants with notes', { ...observed, comparisons: 2, guide_only_reasons: ['bayesian'] }],
    ] as const)('has no violations when %s', async (_, data) => {
      const { container } = render(
        <SampleSizeMeter data={data as SampleSizeResult} onRecalculate={jest.fn()} />
      );
      const result = await axe.run(container, axeOptions);
      expect(result.violations.map((v) => v.id)).toEqual([]);
    });
  });
});
