import React from 'react';
import { render, screen } from '@testing-library/react';
import { EarlyStoppingBanner } from '@/components/results/Sequential/EarlyStoppingBanner';
import { ConfidenceSequence, MSPRTResult } from '@/types/sequential';

const mockMSPRT: MSPRTResult = {
  lambda_ratio: 25.42,
  always_valid_p_value: 0.0394,
  can_stop: true,
  evidence_strength: 'strong_for_effect',
  boundary: 20.0,
};

// 10% -> 13% and 10% -> 7% at 10,000 users per arm, alpha 0.05, tau^2 0.001
// (run_sequential_analysis): both stop_for_effect / strong_for_effect, with
// confidence sequences 0.0157 to 0.0443 and -0.0427 to -0.0173 -- opposite
// sides of zero, and not mirror images, because the variance is larger at 13%.
const CS_UP: ConfidenceSequence = {
  lower: 0.0157,
  upper: 0.0443,
  width: 0.0287,
  sample_size: 20000,
};
const CS_DOWN: ConfidenceSequence = {
  lower: -0.0427,
  upper: -0.0173,
  width: 0.0253,
  sample_size: 20000,
};
const CS_STRADDLE: ConfidenceSequence = {
  lower: -0.0105,
  upper: 0.0105,
  width: 0.021,
  sample_size: 20000,
};

describe('EarlyStoppingBanner', () => {
  it('renders the banner with data-testid', () => {
    render(
      <EarlyStoppingBanner msprtResult={mockMSPRT} recommendedAction="stop_for_effect" />
    );
    expect(screen.getByTestId('early-stopping-banner')).toBeInTheDocument();
  });

  it('shows green banner for stop_for_effect when the variant is better', () => {
    render(
      <EarlyStoppingBanner
        msprtResult={mockMSPRT}
        recommendedAction="stop_for_effect"
        confidenceSequence={CS_UP}
        lowerIsBetter={false}
      />
    );
    const banner = screen.getByTestId('early-stopping-banner');
    expect(banner.className).toContain('bg-green-50');
    expect(screen.getByTestId('action-label')).toHaveTextContent(
      /ready to stop/i
    );
  });

  it('shows amber banner for stop_for_futility', () => {
    render(
      <EarlyStoppingBanner msprtResult={mockMSPRT} recommendedAction="stop_for_futility" />
    );
    const banner = screen.getByTestId('early-stopping-banner');
    expect(banner.className).toContain('bg-amber-50');
    expect(screen.getByTestId('action-label')).toHaveTextContent(
      /consider stopping/i
    );
  });

  it('shows blue banner for continue', () => {
    render(
      <EarlyStoppingBanner msprtResult={mockMSPRT} recommendedAction="continue" />
    );
    const banner = screen.getByTestId('early-stopping-banner');
    expect(banner.className).toContain('bg-blue-50');
    expect(screen.getByTestId('action-label')).toHaveTextContent(
      /continue testing/i
    );
  });

  it('displays lambda ratio and boundary', () => {
    render(
      <EarlyStoppingBanner msprtResult={mockMSPRT} recommendedAction="stop_for_effect" />
    );
    const lambda = screen.getByTestId('lambda-display');
    expect(lambda).toHaveTextContent('25.42');
    expect(lambda).toHaveTextContent('20.0');
  });

  it('displays evidence detail with p-value', () => {
    render(
      <EarlyStoppingBanner msprtResult={mockMSPRT} recommendedAction="stop_for_effect" />
    );
    const detail = screen.getByTestId('evidence-detail');
    expect(detail).toHaveTextContent(/strong evidence for effect/i);
    expect(detail).toHaveTextContent('0.0394');
  });

  it('renders without msprtResult (null)', () => {
    render(
      <EarlyStoppingBanner msprtResult={null} recommendedAction="continue" />
    );
    expect(screen.getByTestId('early-stopping-banner')).toBeInTheDocument();
    expect(screen.getByTestId('action-label')).toHaveTextContent(
      /continue testing/i
    );
    expect(screen.queryByTestId('lambda-display')).not.toBeInTheDocument();
    expect(screen.queryByTestId('evidence-detail')).not.toBeInTheDocument();
  });

  it('has correct aria role', () => {
    render(
      <EarlyStoppingBanner msprtResult={mockMSPRT} recommendedAction="continue" />
    );
    expect(screen.getByRole('status')).toBeInTheDocument();
  });

  // #1147: the mSPRT is two-sided, so stop_for_effect fires for a variant that
  // is worse as readily as for one that is better. The banner says which.
  describe('stop_for_effect follows the direction of the change (#1147)', () => {
    const banner = (
      confidenceSequence: ConfidenceSequence | null,
      lowerIsBetter?: boolean
    ) =>
      render(
        <EarlyStoppingBanner
          msprtResult={mockMSPRT}
          recommendedAction="stop_for_effect"
          confidenceSequence={confidenceSequence}
          lowerIsBetter={lowerIsBetter}
        />
      );

    it('a higher-is-better metric with a worse variant (10% -> 7%) shows harm, not ship', () => {
      banner(CS_DOWN, false);
      const el = screen.getByTestId('early-stopping-banner');
      const label = screen.getByTestId('action-label');
      expect(label).not.toHaveTextContent(/ready to stop/i);
      expect(label).not.toHaveTextContent(/significant effect detected/i);
      expect(el.className).not.toContain('bg-green-50');
      expect(el.className).toContain('bg-red-50');
      expect(label).toHaveTextContent(/significantly worse/i);
      expect(el).toHaveAttribute('data-direction', 'worse');
      // Words as well as colour.
      expect(screen.getByTestId('action-detail')).toHaveTextContent(/do not ship/i);
    });

    it('a higher-is-better metric with a better variant (10% -> 13%) shows ready to stop', () => {
      banner(CS_UP, false);
      const el = screen.getByTestId('early-stopping-banner');
      expect(el).toHaveAttribute('data-direction', 'better');
      expect(el.className).toContain('bg-green-50');
      expect(screen.getByTestId('action-label')).toHaveTextContent(
        /ready to stop.*significantly better/i
      );
      expect(screen.queryByTestId('action-detail')).not.toBeInTheDocument();
    });

    it('a lower-is-better metric (bounce, latency) with a negative difference shows success', () => {
      banner(CS_DOWN, true);
      const el = screen.getByTestId('early-stopping-banner');
      expect(el).toHaveAttribute('data-direction', 'better');
      expect(el.className).toContain('bg-green-50');
      expect(screen.getByTestId('action-label')).toHaveTextContent(/ready to stop/i);
    });

    it('a lower-is-better metric with a positive difference shows harm', () => {
      banner(CS_UP, true);
      const el = screen.getByTestId('early-stopping-banner');
      expect(screen.getByTestId('action-label')).not.toHaveTextContent(/ready to stop/i);
      expect(el.className).toContain('bg-red-50');
      expect(screen.getByTestId('action-label')).toHaveTextContent(/significantly worse/i);
      expect(el).toHaveAttribute('data-direction', 'worse');
    });

    it('a confidence sequence that straddles zero shows a neutral message', () => {
      banner(CS_STRADDLE, false);
      const el = screen.getByTestId('early-stopping-banner');
      expect(el).toHaveAttribute('data-direction', 'unclear');
      expect(el.className).toContain('bg-slate-50');
      expect(el.className).not.toContain('bg-green-50');
      expect(el.className).not.toContain('bg-red-50');
      expect(screen.getByTestId('action-label')).toHaveTextContent(/direction unclear/i);
      expect(screen.getByTestId('action-label')).not.toHaveTextContent(/ready to stop/i);
    });

    it.each([
      ['a bound at exactly zero', { ...CS_UP, lower: 0 }],
      ['a missing confidence sequence', null],
    ])('%s is neutral, not a ship message', (_label, cs) => {
      banner(cs, false);
      expect(screen.getByTestId('early-stopping-banner')).toHaveAttribute(
        'data-direction',
        'unclear'
      );
      expect(screen.getByTestId('action-label')).not.toHaveTextContent(/ready to stop/i);
    });

    it('does not guess when it cannot tell whether higher or lower is better', () => {
      banner(CS_DOWN, undefined);
      const el = screen.getByTestId('early-stopping-banner');
      expect(el).toHaveAttribute('data-direction', 'unknown');
      expect(el.className).toContain('bg-slate-50');
      expect(screen.getByTestId('action-label')).toHaveTextContent(/direction unknown/i);
      expect(screen.getByTestId('action-label')).not.toHaveTextContent(/ready to stop/i);
      expect(screen.getByTestId('action-detail')).toHaveTextContent(/not a conversion/i);
      expect(screen.getByTestId('action-detail')).toHaveTextContent(/share of users/i);
    });

    it('leaves the other actions as they were, whatever the direction', () => {
      render(
        <EarlyStoppingBanner
          msprtResult={mockMSPRT}
          recommendedAction="continue"
          confidenceSequence={CS_DOWN}
          lowerIsBetter={false}
        />
      );
      const el = screen.getByTestId('early-stopping-banner');
      expect(el.className).toContain('bg-blue-50');
      expect(el).not.toHaveAttribute('data-direction');
      expect(screen.getByTestId('action-label')).toHaveTextContent(/continue testing/i);
    });
  });
});
