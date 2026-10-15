import React from 'react';
import { render, screen } from '@testing-library/react';
import { LiveResultsPanel } from '@/components/experiments/LiveResultsPanel';
import * as streamHook from '@/hooks/useExperimentStream';

jest.mock('@/hooks/useExperimentStream');

const mockUseExperimentStream = streamHook.useExperimentStream as jest.MockedFunction<
  typeof streamHook.useExperimentStream
>;

const baseResult: streamHook.UseExperimentStreamResult = {
  snapshot: null,
  status: 'disconnected',
  error: null,
  connect: jest.fn(),
  disconnect: jest.fn(),
  refresh: jest.fn(),
  ping: jest.fn(),
};

function mockStream(overrides: Partial<streamHook.UseExperimentStreamResult>) {
  mockUseExperimentStream.mockReturnValue({ ...baseResult, ...overrides });
}

const sampleSnapshot: streamHook.ExperimentSnapshot = {
  event: 'results_update',
  experimentId: 'exp-1',
  timestamp: '2024-06-01T12:00:00Z',
  status: 'active',
  variants: [
    {
      key: 'control',
      name: 'Control',
      participantCount: 1000,
      conversionCount: 100,
      conversionRate: 0.1,
      relativeLift: 0,
      pValue: null,
      isControl: true,
    },
    {
      key: 'variant_a',
      name: 'Variant A',
      participantCount: 1000,
      conversionCount: 120,
      conversionRate: 0.12,
      relativeLift: 0.2,
      pValue: 0.03,
      isControl: false,
    },
  ],
  totalParticipants: 2000,
  daysRunning: 7,
  isSignificant: true,
};

describe('LiveResultsPanel', () => {
  it('renders the panel', () => {
    mockStream({});
    render(<LiveResultsPanel experimentId="exp-1" />);
    expect(screen.getByTestId('live-results-panel')).toBeInTheDocument();
  });

  it('shows loading skeleton when connecting', () => {
    mockStream({ status: 'connecting' });
    render(<LiveResultsPanel experimentId="exp-1" />);
    expect(screen.getByTestId('loading-skeleton')).toBeInTheDocument();
  });

  it('shows no-data message when disconnected with no snapshot', () => {
    mockStream({ status: 'disconnected' });
    render(<LiveResultsPanel experimentId="exp-1" />);
    expect(screen.getByTestId('no-data-message')).toBeInTheDocument();
  });

  it('renders snapshot metadata', () => {
    mockStream({ status: 'connected', snapshot: sampleSnapshot });
    render(<LiveResultsPanel experimentId="exp-1" />);
    expect(screen.getByText('2,000')).toBeInTheDocument();
    expect(screen.getByText('7')).toBeInTheDocument();
  });

  it('shows live indicator when connected', () => {
    mockStream({ status: 'connected', snapshot: sampleSnapshot });
    render(<LiveResultsPanel experimentId="exp-1" />);
    expect(screen.getByTestId('live-indicator')).toBeInTheDocument();
  });

  it('renders variants table', () => {
    mockStream({ status: 'connected', snapshot: sampleSnapshot });
    render(<LiveResultsPanel experimentId="exp-1" />);
    expect(screen.getByTestId('variants-table')).toBeInTheDocument();
    expect(screen.getByTestId('variant-row-control')).toBeInTheDocument();
    expect(screen.getByTestId('variant-row-variant_a')).toBeInTheDocument();
  });

  it('shows error banner with reconnect button', () => {
    mockStream({ status: 'error', error: 'Connection lost' });
    render(<LiveResultsPanel experimentId="exp-1" />);
    expect(screen.getByTestId('error-banner')).toBeInTheDocument();
    expect(screen.getByText('Connection lost')).toBeInTheDocument();
    expect(screen.getByTestId('reconnect-button')).toBeInTheDocument();
  });

  it('shows significance indicator', () => {
    mockStream({ status: 'connected', snapshot: sampleSnapshot });
    render(<LiveResultsPanel experimentId="exp-1" />);
    expect(screen.getByTestId('significance-indicator')).toHaveTextContent('Yes');
  });

  it('shows last updated time', () => {
    mockStream({ status: 'connected', snapshot: sampleSnapshot });
    render(<LiveResultsPanel experimentId="exp-1" />);
    expect(screen.getByTestId('last-updated')).toBeInTheDocument();
  });

  it('disables refresh button when not connected', () => {
    mockStream({ status: 'disconnected' });
    render(<LiveResultsPanel experimentId="exp-1" />);
    expect(screen.getByTestId('refresh-button')).toBeDisabled();
  });

  // #1244: a lift is coloured as a win only below the live threshold, as the
  // Overview table colours only a significant result.
  it('shows a lift that is not below the threshold in a neutral colour', () => {
    const snapshot = {
      ...sampleSnapshot,
      isSignificant: false,
      variants: sampleSnapshot.variants.map((v) =>
        v.isControl ? v : { ...v, relativeLift: 0.527, pValue: 0.132 }
      ),
    };
    mockStream({ status: 'connected', snapshot });
    render(<LiveResultsPanel experimentId="exp-1" />);
    const lift = screen.getByTestId('lift-variant_a');
    expect(lift).toHaveTextContent('+52.7%');
    expect(lift).toHaveClass('text-slate-600');
    expect(lift).not.toHaveClass('text-green-700');
  });

  it('colours a lift below the threshold green', () => {
    mockStream({ status: 'connected', snapshot: sampleSnapshot });
    render(<LiveResultsPanel experimentId="exp-1" />);
    expect(screen.getByTestId('lift-variant_a')).toHaveClass('text-green-700');
  });
});
